"""Data-layer endpoints (M1): recorder health and the data-quality report."""
from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query

from server.schemas import (CoverageMatrixDTO, DataQualityResponse, GapDTO, GapPageDTO, InstrumentQualityDTO,
                            RecorderStatusDTO, RecorderStatusResponse)

router = APIRouter(prefix="/api/data", tags=["data"])

def _market_open() -> bool:
    """True from the pre-open to the close on a trading day, by the dated session rules (the recorder reports only then)."""
    from stockify import timeutil
    from stockify.daemon.market_day import inside_market_hours

    try:
        return inside_market_hours(timeutil.now_ns())
    except Exception:  # noqa: BLE001 — no session rule in force: say "closed" rather than fail the page
        return False


def _finite(x) -> float | None:  # noqa: ANN001
    return x if isinstance(x, (int, float)) and math.isfinite(x) else None


def _recorder_dto(row: dict) -> RecorderStatusDTO:
    from stockify import timeutil
    from stockify.daemon import recorder_health

    ages = json.loads(row.get("last_packet_age_ms_json") or "{}")
    age_s = max(0.0, (timeutil.now_ns() - row["wall_ns"]) / 1e9)
    stale = age_s > recorder_health.STALE_AFTER_S
    return RecorderStatusDTO(
        wall_ns=row["wall_ns"], connected=bool(row["connected"]) and not stale, stale=stale, market_open=_market_open(),
        subs_full=row["subs_full"] or 0, subs_quote=row["subs_quote"] or 0,
        packets_per_s=_finite(row["packets_per_s"]) or 0.0, queue_depth=row["queue_depth"] or 0,
        writer_lag_ms=_finite(row["writer_lag_ms"]), last_packet_age_ms=ages,
        bytes_today=row["bytes_today"] or 0,
        age_s=age_s,
    )


@router.get("/recorder", response_model=RecorderStatusResponse)
async def recorder_status() -> RecorderStatusResponse:
    from stockify import store

    row = store.latest_recorder_status()
    return RecorderStatusResponse(status=None if row is None else _recorder_dto(row))


@router.get("/quality", response_model=DataQualityResponse)
async def data_quality(date: str = Query(..., description="IST trade date YYYY-MM-DD")) -> DataQualityResponse:
    from datetime import date as _date

    from stockify.data import quality

    try:
        day = _date.fromisoformat(date)
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    try:
        rows, symbols = quality.stored_readonly(day)
    except quality.StoreUnreadable as e:
        # An unreadable store is named, not a 500; the sentence is fixed (no path, no exception text).
        return DataQualityResponse(date=day.isoformat(), errors=[str(e)], error_stores=[e.store_key])
    if not rows:
        raise HTTPException(status_code=404, detail=f"no quality report for {day}; run `python -m stockify data report --date {day}`")
    items = [InstrumentQualityDTO(instrument_id=iid, symbol=symbols.get(iid), metrics=metrics)
             for iid, metrics in sorted(rows.items()) if iid != 0]
    return DataQualityResponse(date=day.isoformat(), day=rows.get(0, {}), instruments=items)



_ISO_DAY = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def _day(text: str, name: str) -> date:
    """Strict YYYY-MM-DD only: date.fromisoformat also takes 20261001 and 2026-W41-1."""
    if not _ISO_DAY.fullmatch(text):
        raise HTTPException(status_code=422, detail=f"{name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{name} is not a real date")


@router.get("/coverage", response_model=CoverageMatrixDTO)
async def data_coverage(from_: str | None = Query(None, alias="from", description="IST date YYYY-MM-DD"),
                        to: str | None = Query(None, description="IST date YYYY-MM-DD")) -> CoverageMatrixDTO:
    """Per date and dataset: recorded, archived, downloaded; plus bar ranges and storage. Read-only."""
    from stockify import research, timeutil
    from stockify.reporting import coverage

    cfg = research.settings()["coverage"]
    default_days, max_days = int(cfg["default_days"]), int(cfg["max_days"])
    end = _day(to, "to") if to is not None else timeutil.ist_today()
    start = _day(from_, "from") if from_ is not None else end - timedelta(days=default_days - 1)
    if start > end:
        raise HTTPException(status_code=422, detail="from is after to")
    if (end - start).days + 1 > max_days:
        raise HTTPException(status_code=422, detail=f"at most {max_days} days at a time")
    m = coverage.matrix(start, end)
    body = coverage.to_dict(m)
    body["recorder"] = _recorder_dto(m.recorder) if m.recorder else None
    return CoverageMatrixDTO(**body)


@router.get("/gaps", response_model=GapPageDTO)
async def data_gaps(date: str = Query(..., description="IST trade date YYYY-MM-DD"),
                    offset: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000)) -> GapPageDTO:
    """One page of the day's gaps in start order, with the day's total."""
    from stockify.reporting import coverage

    day = _day(date, "date")
    try:
        found = coverage.gaps(day)
    except coverage.NoRecording:
        raise HTTPException(status_code=404, detail=f"no recording for {day.isoformat()}")
    except (OSError, sqlite3.Error):
        raise HTTPException(status_code=409, detail="the recording for this day cannot be read right now") from None
    return GapPageDTO(total=len(found), offset=offset, limit=limit,
                      gaps=[GapDTO(**g) for g in found[offset:offset + limit]])

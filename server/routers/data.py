"""Data-layer endpoints (M1): recorder health and the data-quality report."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query

from server.schemas import (DataQualityResponse, InstrumentQualityDTO, RecorderStatusDTO,
                            RecorderStatusResponse)

router = APIRouter(prefix="/api/data", tags=["data"])


@router.get("/recorder", response_model=RecorderStatusResponse)
async def recorder_status() -> RecorderStatusResponse:
    from stockify import store, timeutil

    row = store.latest_recorder_status()
    if row is None:
        return RecorderStatusResponse(status=None)
    ages = json.loads(row.get("last_packet_age_ms_json") or "{}")
    return RecorderStatusResponse(status=RecorderStatusDTO(
        wall_ns=row["wall_ns"], connected=bool(row["connected"]),
        subs_full=row["subs_full"] or 0, subs_quote=row["subs_quote"] or 0,
        packets_per_s=row["packets_per_s"] or 0.0, queue_depth=row["queue_depth"] or 0,
        writer_lag_ms=row["writer_lag_ms"], last_packet_age_ms=ages,
        bytes_today=row["bytes_today"] or 0,
        age_s=max(0.0, (timeutil.now_ns() - row["wall_ns"]) / 1e9),
    ))


@router.get("/quality", response_model=DataQualityResponse)
async def data_quality(date: str = Query(..., description="IST trade date YYYY-MM-DD")) -> DataQualityResponse:
    from datetime import date as _date

    from stockify.data import instruments, quality

    try:
        day = _date.fromisoformat(date)
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    rows = quality.stored(day)
    if not rows:
        raise HTTPException(status_code=404, detail=f"no quality report for {day}; run `python -m stockify data report --date {day}`")
    items = []
    for iid, metrics in sorted(rows.items()):
        if iid == 0:
            continue
        info = instruments.get(iid) or {}
        items.append(InstrumentQualityDTO(instrument_id=iid, symbol=info.get("tradingsymbol"), metrics=metrics))
    return DataQualityResponse(date=day.isoformat(), day=rows.get(0, {}), instruments=items)


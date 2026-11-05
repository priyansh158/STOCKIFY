"""Data-layer endpoints (M1): recorder health; data quality comes with M1.9."""
from __future__ import annotations

import json

from fastapi import APIRouter

from server.schemas import RecorderStatusDTO, RecorderStatusResponse

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

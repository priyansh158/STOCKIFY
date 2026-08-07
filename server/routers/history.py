"""History store endpoints.

GET    /api/history?ticker=&verdict=&since=&limit=&offset=  list (cards)
GET    /api/history/{id}                                    full record (replay)
DELETE /api/history/{id}

All wrappers around `analysis_history.py`. JSON shape matches `schemas.py`
(`HistoryListEntry` / `HistoryRecord`).
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

import analysis_history
from server.schemas import HistoryListEntry, HistoryRecord

log = logging.getLogger("stockify.history")

router = APIRouter(prefix="/api/history", tags=["history"])


def _to_list_entry(rec: analysis_history.AnalysisRecord) -> HistoryListEntry:
    return HistoryListEntry(
        id=rec.id,
        ticker=rec.ticker,
        company_name=rec.company_name,
        sector=rec.sector or None,
        verdict=rec.verdict,
        verdict_reason=rec.verdict_reason or None,
        bottom_line=rec.bottom_line or None,
        created_at=rec.created_at,
    )


def _to_full_record(rec: analysis_history.AnalysisRecord) -> HistoryRecord:
    return HistoryRecord(
        id=rec.id,
        ticker=rec.ticker,
        company_name=rec.company_name,
        sector=rec.sector or None,
        verdict=rec.verdict,
        verdict_reason=rec.verdict_reason or None,
        bottom_line=rec.bottom_line or None,
        created_at=rec.created_at,
        profile=rec.profile or {},
        financials=rec.financials or {},
        geo=rec.geo or {},
        technical=rec.technical or {},
        summary=rec.summary or {},
        macro_snapshot=rec.macro_snapshot,
        position_context=rec.position_context,
    )


@router.get("", response_model=list[HistoryListEntry])
async def list_history(
    ticker: Optional[str] = Query(None),
    verdict: Optional[str] = Query(None, description="Substring match (case-insensitive)"),
    since: Optional[int] = Query(None, description="Unix seconds; only newer rows"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[HistoryListEntry]:
    records = analysis_history.list_analyses(
        limit=limit, offset=offset,
        ticker=ticker, verdict_contains=verdict, since_ts=since,
    )
    return [_to_list_entry(r) for r in records]


@router.get("/{record_id}", response_model=HistoryRecord)
async def get_history(record_id: int) -> HistoryRecord:
    rec = analysis_history.get_analysis(record_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="record not found")
    return _to_full_record(rec)


@router.delete("/{record_id}")
async def delete_history(record_id: int) -> dict:
    ok = analysis_history.delete_analysis(record_id)
    if not ok:
        raise HTTPException(status_code=404, detail="record not found")
    return {"deleted": record_id}

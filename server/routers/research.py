"""Research reports over the API (M6.4 deflated Sharpe, M6.8 bootstrap; WF 6.1, A2 §6).

`GET /api/tests/{id}/research` lives in routers/tests.py (it shares that path space) and uses `run_research_dto`.
Here: the trial families. Family ids contain `/`, so the detail route takes a `:path`; the id only ever reaches a
parameterised database lookup. Every float is finite or None (non-finite values become None), so a response never
carries NaN or Infinity (allow_nan=False semantics). Money stays int paise.
"""
from __future__ import annotations

import dataclasses
import math
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from server.schemas import (BootstrapDTO, DsrDTO, FamilyDetailDTO, FamilySummaryDTO, IntervalDTO, PboDTO,
                            RunResearchDTO, TrialRowDTO)

router = APIRouter(tags=["research"])

_INTERVALS = ("sharpe", "mean_daily_return", "max_drawdown")


def _clean(v: Any) -> Any:
    """Dataclasses and containers as plain data, with every non-finite float replaced by None."""
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return {f.name: _clean(getattr(v, f.name)) for f in dataclasses.fields(v)}
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


def run_research_dto(run_id: str) -> RunResearchDTO:
    """The caller has checked that the run exists (404 there)."""
    from stockify.research import report
    d = _clean(report.run_research(run_id))
    b = d["bootstrap"]
    if b is not None:
        b.pop("seed", None)
        for k in _INTERVALS:
            b[k] = IntervalDTO(**b[k])
        d["bootstrap"] = BootstrapDTO(**b)
    if d["dsr"] is not None:
        d["dsr"] = DsrDTO(**d["dsr"])
    return RunResearchDTO(**d)


@router.get("/api/research/families", response_model=list[FamilySummaryDTO])
def list_families(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=500)) -> list[FamilySummaryDTO]:
    from stockify.research import report
    return [FamilySummaryDTO(**_clean(f)) for f in report.list_families()[offset:offset + limit]]


@router.get("/api/research/families/{family_id:path}", response_model=FamilyDetailDTO)
def family_detail(family_id: str) -> FamilyDetailDTO:
    from stockify import store
    from stockify.research import report
    if store.get_family(family_id) is None:
        raise HTTPException(status_code=404, detail="no such family")
    d = _clean(report.family_report(family_id))
    if d["best"] is not None:
        d["best"] = DsrDTO(**d["best"])
    if d["pbo"] is not None:
        d["pbo"] = PboDTO(**d["pbo"])
    d["trials"] = [TrialRowDTO(**t) for t in d["trials"]]
    d["others"] = [TrialRowDTO(**t) for t in d["others"]]
    return FamilyDetailDTO(**d)

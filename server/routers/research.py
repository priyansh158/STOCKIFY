"""Research reports over the API (M6.4 deflated Sharpe, M6.8 bootstrap; WF 6.1, A2 §6).

`GET /api/tests/{id}/research` lives in routers/tests.py (it shares that path space) and uses `run_research_dto`.
Here: the trial families. Family ids contain `/`, so the detail route takes a `:path`; the id only ever reaches a
parameterised database lookup. Every float is finite or None (non-finite values become None), so a response never
carries NaN or Infinity (allow_nan=False semantics). Money stays int paise.
"""
from __future__ import annotations

import dataclasses
import json
import math
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from server.schemas import (BootstrapDTO, DsrDTO, FamilyDetailDTO, FamilySummaryDTO, IntervalDTO, PboDTO,
                            ResearchCountsDTO, ResearchJobDTO, RunResearchDTO, SweepAxisDTO, SweepBestCellDTO, SweepDTO,
                            SweepHeatmapDTO, SweepRowDTO, TrialRowDTO, WalkforwardDTO, WalkforwardSummaryDTO,
                            WalkforwardWindowDTO)

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
    from stockify.research import plain, report
    rr = report.run_research(run_id)
    d = _clean(rr)
    b = d["bootstrap"]
    if b is not None:
        b.pop("seed", None)
        for k in _INTERVALS:
            b[k] = IntervalDTO(**b[k])
        d["bootstrap"] = BootstrapDTO(**b)
    if rr.regimes is not None:       # one plain sentence per bucket, written by the server (IL 6.2)
        for dim in ("month", "weekday", "expiry", "vix", "gap"):
            for row, bucket in zip(d["regimes"][dim], getattr(rr.regimes, dim)):
                row["plain"] = plain.regime_sentence(bucket)
    if d["dsr"] is not None:
        d["dsr"] = DsrDTO(**d["dsr"])
    return RunResearchDTO(**d)


@router.get("/api/research/families", response_model=list[FamilySummaryDTO])
def list_families(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=500)) -> list[FamilySummaryDTO]:
    from stockify.research import plain, report
    rows = [_clean(f) for f in report.list_families()[offset:offset + limit]]
    return [FamilySummaryDTO(**r, best_plain=plain.family_best_sentence(r["n_trials"], r["best_dsr"], r["objective"]))
            for r in rows]


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


# ── Parameter sweeps (WF 6.3) ─────────────────────────────────────────────────
# Read only. A sweep's result is the document `run_sweep` stored in the job row (`state_json`); nothing here opens a
# file, and the job id and axis names only ever meet a parameterised lookup or a comparison with the stored axes.

_SWEEP_SCHEMA = "stockify.sweep.result/1"
_OBJECTIVE_FORMAT = {"sharpe": "ratio", "sortino": "ratio", "calmar": "ratio", "cagr": "percent", "net_pnl": "paise"}
_SIGNED = frozenset(_OBJECTIVE_FORMAT)        # every objective can be negative: the page uses a diverging scale
_KIND = r"^[a-z0-9_]{1,32}$"


@router.get("/api/research/jobs", response_model=list[ResearchJobDTO])
def list_jobs(kind: str | None = Query(None, pattern=_KIND), offset: int = Query(0, ge=0, le=1_000_000),
              limit: int = Query(50, ge=1, le=500)) -> list[ResearchJobDTO]:
    from stockify import store
    from stockify.research import plain
    return [ResearchJobDTO(**{k: j.get(k) for k in ResearchJobDTO.model_fields if k != "plain"},
                           plain=plain.job_status_sentence(j["status"], j.get("error")))
            for j in store.list_research_jobs(kind, offset, limit)]


@router.get("/api/research/counts", response_model=ResearchCountsDTO)
def research_counts() -> ResearchCountsDTO:
    from stockify import store
    return ResearchCountsDTO(families=len(store.list_families()), sweeps=store.count_research_jobs("sweep"),
                             walkforwards=store.count_research_jobs("walkforward"))


def _label(v: Any) -> str:
    if v is None:
        return "none"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _key(v: Any) -> str:
    return json.dumps(v, sort_keys=True)


def _stored_sweep(job_id: str) -> dict:
    """The stored result of a finished sweep job, checked for shape; anything else is a 404 (never a 500)."""
    from stockify import store
    job = store.get_research_job(job_id)
    if job is None or job.get("kind") != "sweep":
        raise HTTPException(status_code=404, detail="no such sweep")
    if job.get("status") != "done":
        raise HTTPException(status_code=404, detail=f"sweep not finished yet (status: {job.get('status')})")
    try:
        doc = json.loads(job.get("state_json") or "")
    except ValueError:
        doc = None
    if (not isinstance(doc, dict) or doc.get("schema") != _SWEEP_SCHEMA or doc.get("job_id") != job_id
            or doc.get("objective") not in _OBJECTIVE_FORMAT or not isinstance(doc.get("axes"), dict)
            or not doc["axes"] or not isinstance(doc.get("rows"), list)
            or not all(isinstance(v, list) for v in doc["axes"].values())
            or not all(isinstance(r, dict) and isinstance(r.get("params"), dict) for r in doc["rows"])):
        raise HTTPException(status_code=404, detail="the sweep has no readable stored result")
    return _clean(doc)


def _pick_axes(axes: list[str], x: str | None, y: str | None) -> tuple[str, str | None]:
    for name in (x, y):
        if name is not None and name not in axes:
            raise HTTPException(status_code=422, detail=f"unknown axis {name!r}; this sweep has {axes}")
    if x is not None and x == y:
        raise HTTPException(status_code=422, detail=f"x and y are both {x!r}; choose two different axes")
    if x is None:
        x = next((a for a in axes if a != y), None)
        if x is None:
            raise HTTPException(status_code=422, detail=f"y is {y!r}, the only axis; there is no other axis for x")
    if y is None:
        y = next((a for a in axes if a != x), None)
    return x, y


def _best_of(rows: list[dict]) -> dict | None:
    """The row with the highest finite objective value among done rows; a tie goes to the lowest grid index."""
    best, best_key = None, None
    for r in rows:
        v = r.get("objective_value")
        if r.get("status") != "done" or isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        k = (-v, r.get("index", 0))
        if best_key is None or k < best_key:
            best, best_key = r, k
    return best


def _heatmap(doc: dict, x: str, y: str | None) -> SweepHeatmapDTO:
    axes = doc["axes"]
    xv = axes[x]
    yv = axes[y] if y is not None else [None]
    buckets: dict[tuple[int, int], list[dict]] = {}
    xi_of = {_key(v): i for i, v in enumerate(xv)}
    yi_of = {_key(v): i for i, v in enumerate(yv)}
    for r in doc["rows"]:
        p = r["params"]
        xi = xi_of.get(_key(p.get(x)))
        yi = yi_of.get(_key(p.get(y))) if y is not None else 0
        if xi is not None and yi is not None:
            buckets.setdefault((yi, xi), []).append(r)
    cells = [[None] * len(xv) for _ in yv]
    run_ids = [[None] * len(xv) for _ in yv]
    for (yi, xi), rs in buckets.items():
        b = _best_of(rs)
        if b is not None:
            cells[yi][xi], run_ids[yi][xi] = float(b["objective_value"]), b.get("run_id")
    best = None
    for yi, line in enumerate(run_ids):
        for xi, rid in enumerate(line):
            if rid is not None and rid == doc.get("best_run_id"):
                best = SweepBestCellDTO(xi=xi, yi=yi, run_id=rid, value=cells[yi][xi])
    obj = doc["objective"]
    return SweepHeatmapDTO(x=x, y=y, xs=[_label(v) for v in xv], ys=[_label(v) for v in yv] if y is not None
                           else [obj], x_values=xv, y_values=yv if y is not None else [None], cells=cells,
                           run_ids=run_ids, best=best, signed=obj in _SIGNED, format=_OBJECTIVE_FORMAT[obj],
                           hidden_axes=[a for a in axes if a not in (x, y)])


def _sweep_sentence(doc: dict, h: SweepHeatmapDTO) -> str:
    from stockify.research import plain as words
    obj = doc["objective"]
    if h.hidden_axes:
        what = (f"Each square is the best {obj} over every setting of {', '.join(h.hidden_axes)}, "
                "not an average and not one fixed value.")
    else:
        what = f"Each square is the {obj} of one trial."
    b = h.best
    if b is None:
        return f"{what} No trial finished with a usable {obj}, so there is no best one."
    value = words.rupees(b.value / 100) if obj == "net_pnl" else f"{b.value:.4g}"
    where = f"{h.x} = {h.xs[b.xi]}" + (f" and {h.y} = {h.ys[b.yi]}" if h.y is not None else "")
    return f"{what} The best of all {len(doc['rows'])} trials has {where}, with {obj} {value}."


@router.get("/api/research/sweeps/{job_id}", response_model=SweepDTO)
def sweep_detail(job_id: str, x: str | None = Query(None, max_length=200),
                 y: str | None = Query(None, max_length=200)) -> SweepDTO:
    doc = _stored_sweep(job_id)
    chosen_x, chosen_y = _pick_axes(list(doc["axes"]), x, y)
    h = _heatmap(doc, chosen_x, chosen_y)
    return SweepDTO(
        job_id=job_id, name=str(doc.get("name", "")), family_id=str(doc.get("family_id", "")),
        objective=doc["objective"],
        axes=[SweepAxisDTO(path=a, labels=[_label(v) for v in vs], values=vs) for a, vs in doc["axes"].items()],
        rows=[SweepRowDTO(**{k: r.get(k) for k in SweepRowDTO.model_fields}) for r in doc["rows"]],
        best_run_id=doc.get("best_run_id"), cells=h, plain=_sweep_sentence(doc, h))

# ---- walk-forward report (M6.5, WF 6.6; Task 22) ----------------------------------------------------------------
# The prefix `/api/research/walkforward/` is its own: no `{job_id}` route of another kind can shadow it, and the
# family route above (`/api/research/families/{family_id:path}`) lives under a different prefix.

@router.get("/api/research/walkforward/{job_id}", response_model=WalkforwardDTO)
def walkforward_detail(job_id: str) -> WalkforwardDTO:
    """A finished walk-forward job, read from its walkforward.json: 404 for an unknown job (or one of another
    kind), 409 for a job that is not finished or whose file is gone. Read-only; a stored error text is never
    echoed, and a window's own error only when it is a plain line (no paths)."""
    from stockify.research import plain, wf_report
    try:
        d = _clean(wf_report.detail(job_id))
    except wf_report.WfReportError as e:
        raise HTTPException(status_code=404 if e.kind == "unknown" else 409, detail=str(e))
    s = d["summary"]
    s["dsr"] = DsrDTO(**s["dsr"]) if s.get("dsr") is not None else None
    d["summary"] = WalkforwardSummaryDTO(**s)
    d["windows"] = [WalkforwardWindowDTO(**{**w, "error": plain.plain_reason(w.get("error"))}) for w in d["windows"]]
    return WalkforwardDTO(**d)

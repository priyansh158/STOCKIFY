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
from fastapi.responses import JSONResponse

from server.schemas import (BootstrapDTO, DsrDTO, FamilyDetailDTO, FamilySummaryDTO, IntervalDTO, PboDTO,
                            ResearchCountsDTO, ResearchJobDTO, RunResearchDTO, SweepAxisDTO, SweepBestCellDTO, SweepDTO,
                            SweepHeatmapDTO, SweepRowDTO, TrialRowDTO, WalkforwardChartDTO, WalkforwardChartWindowDTO,
                            WalkforwardDTO, WalkforwardSummaryDTO, WalkforwardWindowDTO)

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
    return [FamilySummaryDTO(**r, title=_family_title(r["family_id"], r.get("description")), best_plain=plain.family_best_sentence(r["n_trials"], r["best_dsr"], r["objective"], r["n_finished"]))
            for r in rows]


def _family_title(family_id: str, description: str | None) -> str:
    """The family's name on a page (stockify.display.family_title); a sweep's or walk-forward's own job name for its family."""
    from stockify import display, store
    job_name = None
    kind, _, job_id = family_id.partition("/")
    if kind in ("sweep", "walkforward") and job_id:
        try:
            job_name = (store.get_research_job(job_id) or {}).get("name")
        except Exception:  # noqa: BLE001 — a title is a nicety
            job_name = None
    return display.family_title(family_id, description, job_name)


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
    from stockify import display
    d["trials"] = [TrialRowDTO(**t, name_label=display.name_label(t.get("name"))) for t in d["trials"]]
    d["others"] = [TrialRowDTO(**t, name_label=display.name_label(t.get("name"))) for t in d["others"]]
    d["sessions"] = [{**s, "name_label": display.name_label(s.get("name"))} for s in d["sessions"]]
    return FamilyDetailDTO(**d, title=_family_title(family_id, d.get("description")))


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
    from stockify.research import plain, walkforward

    def _row(j: dict) -> ResearchJobDTO:
        from stockify import display
        shown = walkforward.plain_error(j)                      # a plain reason, never the raw error
        return ResearchJobDTO(**{k: j.get(k) for k in ResearchJobDTO.model_fields if k != "plain"}
                              | {"error": shown, "name_label": display.name_label(j.get("name"))},
                              plain=plain.job_status_sentence(j["status"], None if shown == "Failed" else shown))

    return [_row(j)
            for j in store.list_research_jobs(kind, offset, limit)]


@router.get("/api/research/counts", response_model=ResearchCountsDTO)
def research_counts() -> ResearchCountsDTO:
    from stockify import store
    return ResearchCountsDTO(families=len(store.list_families()), sweeps=store.count_research_jobs("sweep"),
                             walkforwards=store.count_research_jobs("walkforward"))


def _key(v: Any) -> str:
    return json.dumps(v, sort_keys=True)


def _stored_sweep(job_id: str) -> dict:
    """The stored result of a finished sweep job, checked for shape; anything else is a 404 (never a 500)."""
    from stockify import store
    job = store.get_research_job(job_id)
    if job is None or job.get("kind") != "sweep":
        raise HTTPException(status_code=404, detail="no such sweep")
    if job.get("status") != "done":
        detail = f"sweep not finished yet (status: {job.get('status')})"
        if job.get("status") == "interrupted":
            detail += ": it was stopped before it finished, because STOCKIFY was closed"
        if job.get("status") == "cancelled":
            detail += ": you stopped it before it finished"
        if job.get("status") == "failed" and job.get("error"):
            from stockify.research import plain
            detail += f": {plain.plain_reason(job['error'])}"          # a plain reason from the fixed table, never the raw text
        raise HTTPException(status_code=404, detail=detail)
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


def _heatmap(doc: dict, x: str, y: str | None, params: dict[str, dict] | None = None) -> SweepHeatmapDTO:
    from stockify import display
    params = params or {}
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
    return SweepHeatmapDTO(x=x, y=y, xs=[display.param_value_text(params.get(x), v) for v in xv],
                           ys=[display.param_value_text(params.get(y), v) for v in yv] if y is not None
                           else [obj], x_values=xv, y_values=yv if y is not None else [None], cells=cells,
                           run_ids=run_ids, best=best, signed=obj in _SIGNED, format=_OBJECTIVE_FORMAT[obj],
                           hidden_axes=[a for a in axes if a not in (x, y)])


def _objective_label(objective: str) -> str:
    """The one name for the objective, on the page and in the sentence: a name a reader knows, never a code."""
    from stockify.research import plain
    return plain.OBJECTIVE_LABELS.get(objective, objective)


def _sweep_sentence(doc: dict, h: SweepHeatmapDTO, labels: dict[str, str]) -> str:
    from stockify.research import plain as words
    obj = _objective_label(doc["objective"])
    if h.hidden_axes:
        what = (f"Each square is the best {obj} over every setting of {', '.join(labels[a] for a in h.hidden_axes)}, "
                "not an average and not one fixed value.")
    else:
        what = f"Each square is the {obj} of one trial."
    b = h.best
    if b is None:
        return f"{what} No trial finished with a usable {obj}, so there is no best one."
    value = words.rupees(b.value / 100) if doc["objective"] == "net_pnl" else f"{b.value:.4g}"
    where = f"{labels[h.x]} = {h.xs[b.xi]}" + (f" and {labels[h.y]} = {h.ys[b.yi]}" if h.y is not None else "")
    return f"{what} The best of all {len(doc['rows'])} trials has {where}, with {obj} {value}."


def _row_reason(r: dict) -> str | None:
    """A failed trial's reason: the plain one stored with the row, else made now from its raw text (an older sweep)."""
    from stockify.research import plain
    stored = r.get("failure_reason")
    if isinstance(stored, str) and stored in plain.PLAIN_SENTENCES:
        return stored
    return plain.plain_reason(r.get("error")) if r.get("status") != "done" else None


def _axis_params(job_id: str, paths: list[str]) -> dict[str, dict]:
    """The params-schema entry of each `strategy.params.<name>` axis, from the sweep's strategy; empty for every other
    axis, or when the strategy is unknown."""
    try:
        from stockify import store
        from stockify.strategies import _catalogue
        spec = json.loads((store.get_research_job(job_id) or {}).get("spec_json") or "{}")
        strat = spec["base"]["strategy"]
        schema = _catalogue.schema_of(strat["kind"], strat["ref"]) or []
    except Exception:  # noqa: BLE001 — a label is a nicety: any trouble means the path
        return {}
    by_name = {p.get("name"): p for p in schema if isinstance(p, dict)}
    out = {}
    for path in paths:
        name = path.removeprefix("strategy.params.") if path.startswith("strategy.params.") else None
        if name and name in by_name:
            out[path] = by_name[name]
    return out


def _axis_labels(job_id: str, paths: list[str], params: dict[str, dict] | None = None) -> dict[str, str]:
    """A plain label for each `strategy.params.<name>` axis from the sweep's strategy (its params schema); every other
    axis, or a sweep whose strategy is unknown, is labelled by its path."""
    from stockify import display
    labels = {p: display.field_label(p) for p in paths}     # never a path: an unknown setting is still in words
    for path, param in (_axis_params(job_id, paths) if params is None else params).items():
        label = param.get("label")
        if isinstance(label, str) and label.strip():
            labels[path] = label.strip()
    return labels


@router.get("/api/research/sweeps/{job_id}", response_model=SweepDTO)
def sweep_detail(job_id: str, x: str | None = Query(None, max_length=200),
                 y: str | None = Query(None, max_length=200)) -> SweepDTO:
    doc = _stored_sweep(job_id)
    chosen_x, chosen_y = _pick_axes(list(doc["axes"]), x, y)
    params = _axis_params(job_id, list(doc["axes"]))
    h = _heatmap(doc, chosen_x, chosen_y, params)
    labels = _axis_labels(job_id, list(doc["axes"]), params)
    from stockify import display
    return SweepDTO(
        job_id=job_id, name=str(doc.get("name", "")), name_label=display.name_label(doc.get("name")), family_id=str(doc.get("family_id", "")),
        objective=doc["objective"], objective_label=_objective_label(doc["objective"]),
        axes=[SweepAxisDTO(path=a, label=labels[a], labels=[display.param_value_text(params.get(a), v) for v in vs], values=vs)
              for a, vs in doc["axes"].items()],
        rows=[SweepRowDTO(**{**{k: r.get(k) for k in SweepRowDTO.model_fields}, "error": _row_reason(r)})
              for r in doc["rows"]],
        best_run_id=doc.get("best_run_id"), cells=h, plain=_sweep_sentence(doc, h, labels))

# ---- walk-forward report (M6.5, WF 6.6; Task 22) ----------------------------------------------------------------
# The prefix `/api/research/walkforward/` is its own: no `{job_id}` route of another kind can shadow it, and the
# family route above (`/api/research/families/{family_id:path}`) lives under a different prefix.

def _not_done(job_id: str, message: str) -> JSONResponse:
    """The 409 for a job that is not finished: the plain message, its status and, for a failed job, the stored
    reason only as the job's stored plain reason (`walkforward.failure_reason`, from a fixed table keyed by the error
    class), never the raw error, so the page can say "still running" or "failed: <reason>" without echoing text."""
    from stockify import store
    from stockify.research import walkforward
    job = store.get_research_job(job_id) or {}
    status = str(job.get("status", ""))
    reason = walkforward.failure_reason(job) if status == "failed" else None
    return JSONResponse(status_code=409, content={"detail": message, "status": status, "reason": reason})


def _window_params(windows: list[dict]) -> dict[str, dict]:
    """The params-schema entry (name to entry) of the strategy of the first window run that can be read, for the plain
    labels and values of the settings a walk-forward's windows chose; empty when none can (the key's own words then)."""
    try:
        from stockify import store
        from stockify.strategies import _catalogue
        for w in windows:
            for key in ("chosen_run_id", "test_run_id"):
                row = store.get_run(w[key]) if w.get(key) else None
                strat = (json.loads(row["config_json"]) if row and row.get("config_json") else {}).get("strategy")
                schema = _catalogue.schema_of(strat["kind"], strat["ref"]) if isinstance(strat, dict) else None
                if schema:
                    return {p["name"]: p for p in schema if isinstance(p, dict) and p.get("name")}
    except Exception:  # noqa: BLE001 — a label is a nicety: any trouble means the key's own words
        pass
    return {}


def _chosen_text(params: dict | None, schema: dict[str, dict]) -> str:
    """The settings a window chose in plain words ("Opening range (minutes) = 15, Also sell short = No"; a setting with
    choices or a percentage reads as the form shows it: "Bar length = 5 minutes")."""
    from stockify import display
    if not params:
        return "None chosen"
    labels = {n: p["label"] for n, p in schema.items() if p.get("label")}
    out = []
    for k, v in params.items():
        param = schema.get(k.removeprefix("strategy.params."))
        shown = display.param_value_text(param, v) if param and (param.get("choices") or param.get("percent"))             else ("none" if v is None else display.setting_text(k, v))
        out.append(f"{display.field_label(k, labels)} = {shown}")
    return ", ".join(out)


@router.get("/api/research/walkforward/{job_id}", response_model=WalkforwardDTO,
            responses={409: {"description": "Not finished (the body has `status` and, for a failed job, a plain "
                                            "`reason`), or finished with its result file gone (`detail` only)."}})
def walkforward_detail(job_id: str) -> WalkforwardDTO:
    """A finished walk-forward job, read from its walkforward.json: 404 for an unknown job (or one of another
    kind), 409 for a job that is not finished or whose file is gone. Read-only; a stored error text is never
    echoed, and a window's own error only when it is a plain line (no paths)."""
    from stockify.research import plain, wf_report
    try:
        d = _clean(wf_report.detail(job_id))
    except wf_report.WfReportError as e:
        if e.kind == "not_done":
            return _not_done(job_id, str(e))
        raise HTTPException(status_code=404 if e.kind == "unknown" else 409, detail=str(e))
    s = d["summary"]
    s["dsr"] = DsrDTO(**s["dsr"]) if s.get("dsr") is not None else None
    d["summary"] = WalkforwardSummaryDTO(**s)
    schema = _window_params(d["windows"])
    d["windows"] = [WalkforwardWindowDTO(**{**w, "error": plain.plain_reason(w.get("error"), fallback=None),
                                            "chosen_text": _chosen_text(w.get("chosen_params"), schema)})
                    for w in d["windows"]]
    from stockify import display
    return WalkforwardDTO(**d, name_label=display.name_label(d.get("name")))


_INT64 = 2**63 - 1
_NS = 1_000_000_000


@router.get("/api/research/walkforward/{job_id}/chart", response_model=WalkforwardChartDTO)
def walkforward_chart(job_id: str, instrument: int | None = Query(None, ge=0, le=_INT64),
                      from_ns: int | None = Query(None, ge=0, le=_INT64),
                      to_ns: int | None = Query(None, ge=0, le=_INT64),
                      interval_s: str | None = Query(None)) -> WalkforwardChartDTO:
    """The out-of-sample chart of a finished walk-forward job (CH 6.12): each window's test run, one after another,
    as `RunChartDTO` plus `windows`. Marker ids carry their window (`w1-f3`). 404 for an unknown or unsafe job id (the
    id is never echoed) or an instrument no window has, 409 for a job that is not finished or whose windows are gone,
    422 for a bad query; every message is fixed words, never stored or exception text. Read-only."""
    from server.routers.tests import _chart_dto
    from stockify.reporting import chart_wf
    if interval_s is not None:                       # each window is drawn at its own run's interval: refuse, do not ignore
        raise HTTPException(status_code=422, detail="this chart does not take interval_s")
    if from_ns is not None and to_ns is not None and from_ns >= to_ns:
        raise HTTPException(status_code=422, detail="from_ns must be before to_ns")
    try:
        model = chart_wf.walkforward_chart(job_id, instrument, from_ns, to_ns)
    except chart_wf.WfChartError as e:
        code = 409 if e.kind in ("not_done", "missing") else 404
        raise HTTPException(status_code=code, detail=str(e)) from None
    except ValueError:
        raise HTTPException(status_code=422, detail="the chart request is not valid") from None
    base = _chart_dto(model)
    windows = [WalkforwardChartWindowDTO(index=w["index"], from_t=w["from_ns"] // _NS, to_t=w["to_ns"] // _NS,
                                         from_d=w["from_d"], to_d=w["to_d"], run_id=w["run_id"], status=w["status"],
                                         result=w["result"], no_sharpe=w["no_sharpe"], excluded=w["excluded"],
                                         session_open_s=w["session_open_s"])
               for w in model["windows"]]
    return WalkforwardChartDTO(**base.model_dump(), windows=windows)

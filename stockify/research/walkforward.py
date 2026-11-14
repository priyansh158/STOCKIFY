"""M6.5 (WF 6.4): walk-forward. Rolling windows over calendar days, a training grid per window run in one pool call,
the best candidate by the objective per window, and that candidate's out-of-sample run on the window's test range.

Dates are inclusive, as pack specs are: a window of `train` days from `train_from` ends on
`train_from + train - 1`, and the test range starts the day after. The windows start `step` days apart; `step >= test`
keeps the out-of-sample pieces from overlapping. A job is its own family (`walkforward/<job id>`), so the number of
tries N is windows x grid; the out-of-sample runs have role `oos` and are not tries. The holdout is checked for every
window before any run starts, so a locked range fails the job at once (the runs check it again).
"""
from __future__ import annotations

import copy
import json
import logging
import math
import os
import re
import secrets
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone

from stockify import paths, research, runconfig, store, timeutil
from stockify.research import fileio, holdout, plain, pool, registry, stats, sweep

_SPAN = re.compile(r"[0-9]+d", re.ASCII)
_NAME_PART_IN_JOB = 24


class WalkforwardError(ValueError):
    pass


class WalkforwardRunError(WalkforwardError):
    """A failure after the job exists (the pool misbehaved): the job is marked failed. The command exits 1 for it, not
    2 (usage or refusal)."""


_log = logging.getLogger(__name__)

# What a failed job says about itself to the page and the API comes from plain.py's one table (`PLAIN_FAILURES`,
# keyed by the error class name), so no exception text (a path, a value, a message) ever leaves the machine. The raw
# text stays in the job's `error` column (the command's retry message reads it) and in the log.
_PLAIN_FAILURE_OTHER = "an internal error; see the log"


def plain_failure(e: BaseException) -> str:
    """The plain reason for a failure: plain.py's line for the first class in the error's MRO it names, else the generic line."""
    for cls in type(e).__mro__:
        line = plain.failure_line(cls.__name__)
        if line is not None:
            return line
    return _PLAIN_FAILURE_OTHER


def failure_reason(job: dict) -> str | None:
    """The plain reason stored beside a failed job's state, or None (older job, no reason stored)."""
    try:
        reason = json.loads(job.get("state_json") or "")["failure_reason"]
    except (ValueError, KeyError, TypeError):
        return None
    return reason if isinstance(reason, str) and reason else None


def plain_error(job: dict) -> str | None:
    """What a job list may say about a job's failure, for any kind of research job (sweep, sensitivity, walk-forward):
    the stored plain reason; else, for an older row, the table's line for the error class its raw text starts with
    (`ClassName: ...`); else "Failed" when there is an error at all; None when there is none. The raw text is never
    returned."""
    stored = failure_reason(job)
    if stored:
        return stored
    raw = job.get("error")
    if not raw:
        return None
    return plain.plain_reason(str(raw), fallback="Failed")


def _mark_failed(job_id: str, e: BaseException) -> None:
    """Mark a job failed: raw text in the `error` column and the log, the plain reason in the state beside the windows."""
    raw = f"{type(e).__name__}: {e}"
    _log.error("walk-forward job %s failed: %s", job_id, raw)
    try:
        try:
            state = json.loads((store.get_research_job(job_id) or {}).get("state_json") or "")
        except ValueError:
            state = None
        state = state if isinstance(state, dict) else {}
        state["failure_reason"] = plain_failure(e)
        store.update_research_job(job_id, status="failed", finished_ns=timeutil.now_ns(), error=raw[:500],
                                  state_json=json.dumps(state, sort_keys=True))
    except Exception:  # noqa: BLE001 — a failed bookkeeping write must not hide the error that caused it
        pass


@dataclass(frozen=True)
class Window:
    index: int
    train_from: date
    train_to: date
    test_from: date
    test_to: date


@dataclass(frozen=True)
class WindowResult:
    window: Window
    train_run_ids: list[str]
    chosen: dict | None
    test_run_id: str | None
    test_status: str                 # done | failed | skipped (no eligible training run)
    error: str | None


@dataclass(frozen=True)
class WalkforwardResult:
    job_id: str
    name: str
    family_id: str
    objective: str
    train: int
    test: int
    step: int
    span: tuple[str, str]
    windows: list[WindowResult]


def parse_span(text: str) -> int:
    """`"120d"` is 120 calendar days. Anything but whole digits and a `d` is refused (months and trading-day counts
    are not units here)."""
    if not isinstance(text, str) or not _SPAN.fullmatch(text) or int(text[:-1]) < 1:
        raise ValueError(f"{text!r} is not a span: write a whole number of calendar days, like 120d")
    return int(text[:-1])


def windows(start: date, end: date, train: int, test: int, step: int) -> list[Window]:
    """Window k starts at `start + k*step`; kept only while its test range ends on or before `end`."""
    for label, v in (("train", train), ("test", test), ("step", step)):
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise ValueError(f"{label} must be a whole number of days >= 1, not {v!r}")
    if step < test:
        raise ValueError(f"step ({step}d) is smaller than test ({test}d): the out-of-sample pieces would overlap")
    out = []
    k = 0
    while True:
        train_from = start + timedelta(days=k * step)
        train_to = train_from + timedelta(days=train - 1)
        test_from = train_to + timedelta(days=1)
        test_to = test_from + timedelta(days=test - 1)
        if test_to > end:
            return out
        out.append(Window(k, train_from, train_to, test_from, test_to))
        k += 1


def window_config(cfg: dict, from_: date, to: date) -> dict:
    """A copy of `cfg` with `from` and `to` of every pack spec replaced (inclusive dates)."""
    out = copy.deepcopy(cfg)
    packs = (out.get("data") or {}).get("packs")
    if not isinstance(packs, list) or not packs:
        raise WalkforwardError("walk-forward needs pack specs: the config has no packs")
    for p in packs:
        if not isinstance(p, dict) or not isinstance(p.get("spec"), dict):
            raise WalkforwardError("walk-forward needs pack specs: a pack named only by its hash has no dates "
                                   "to move")
        p["spec"]["from"], p["spec"]["to"] = from_.isoformat(), to.isoformat()
    return out


select_best = registry.select_best          # moved to registry (Task 18); kept here for existing callers


def _job_id(name: str, now_ns: int) -> str:
    part = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)[:_NAME_PART_IN_JOB].rstrip(".") or "run"
    stamp = datetime.fromtimestamp(now_ns / 1e9, tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"wf-{part}-{stamp}-{secrets.token_hex(3)}"


def _state(results: list[dict]) -> str:
    return json.dumps({"windows": results}, sort_keys=True, allow_nan=False)


def plan(spec: sweep.SweepSpec, *, train: int, test: int, step: int, objective: str, start: date | None = None,
         end: date | None = None) -> tuple[list[Window], int, date, date]:
    """Everything a walk-forward checks before a job exists: the objective, the span (from the base config's pack dates
    unless given), the windows, and the number of training runs against `sweep.max_trials`. Returns (windows, grid
    size, start, end); WalkforwardError / ValueError for anything refused. The command and the web form both call it."""
    if objective not in registry.OBJECTIVES:
        raise ValueError(f"objective must be one of {list(registry.OBJECTIVES)}, not {objective!r}")
    if start is None or end is None:
        found = holdout.run_span(spec.base)
        if found is None:
            raise WalkforwardError("walk-forward needs pack specs with dates: give start and end")
        if end is None and found[1] is None:
            raise WalkforwardError("the base config has no end date: give end")
        start, end = start or found[0], end or found[1]
    wins = windows(start, end, train, test, step)
    if not wins:
        raise WalkforwardError(f"no window fits: {train}d of training and {test}d of testing need "
                               f"{train + test} days, and {start.isoformat()} to {end.isoformat()} has "
                               f"{(end - start).days + 1}")
    size = math.prod(len(v) for v in spec.axes.values())          # the grid's size, before any config is built
    grid = size if spec.search["kind"] == "grid" else min(size, spec.search["trials"])
    runs = len(wins) * grid
    if runs > sweep.max_trials():
        raise WalkforwardError(f"{len(wins)} windows x {grid} grid points is {runs} training runs, above "
                               f"research.json sweep.max_trials ({sweep.max_trials()}): narrow the grid, lengthen "
                               "the windows or shorten the span")
    return wins, grid, start, end



def run_windows(spec: sweep.SweepSpec, *, train: int, test: int, step: int, objective: str,
                start: date | None = None, end: date | None = None, unlock_reason: str | None = None,
                workers: int | None = None, pool_run=pool.run_all,  # noqa: ANN001
                now_ns: int | None = None, on_job=None) -> WalkforwardResult:
    """Walk `spec` forward. WalkforwardError / ValueError before a job exists (bad sizes, no window fits, an
    unknown objective); from then on any failure (a locked holdout, a pool that raises) marks the job `failed`
    with the message and is re-raised."""
    wins, grid, start, end = plan(spec, train=train, test=test, step=step, objective=objective, start=start, end=end)
    points = sweep.expand(spec)
    now = timeutil.now_ns() if now_ns is None else now_ns
    job_id = _job_id(spec.name, now)
    family = registry.job_family_id("walkforward", job_id)
    span = (start.isoformat(), end.isoformat())
    registry.ensure_family(family, objective)
    store.create_research_job({
        "job_id": job_id, "kind": "walkforward", "name": spec.name, "family_id": family, "status": "running",
        "created_ns": now,
        "spec_json": json.dumps({"name": spec.name, "train": train, "test": test, "step": step,
                                 "objective": objective, "span": list(span), "grid": len(points),
                                 "windows": len(wins), "unlock_given": unlock_reason is not None}, sort_keys=True)})
    state: list[dict] = []
    try:
        if on_job is not None:
            on_job(job_id)                                       # the web app learns the id here

        def cfg_for(point, frm, to, name):  # noqa: ANN001, ANN202
            cfg = window_config(point.config, frm, to)
            cfg["name"], cfg["family_id"] = name, family
            cfg.pop("unlock_holdout", None)
            if unlock_reason is not None:
                cfg["unlock_holdout"] = unlock_reason
            cfg = runconfig.validate(cfg)
            holdout.enforce(cfg)                                 # fails closed before any run is made
            return cfg
        train_jobs, slots = [], []
        for w in wins:
            for p in points:
                name = f"{spec.name}-w{w.index:02d}-t{p.index:03d}"
                train_jobs.append(pool.PoolJob(cfg_for(p, w.train_from, w.train_to, name), role="trial",
                                               job_id=job_id))
                slots.append((w.index, p))
        for w in wins:                                           # the test ranges too, so a lock stops everything
            for p in points[:1]:
                cfg_for(p, w.test_from, w.test_to, f"{spec.name}-w{w.index:02d}-oos")
        outcomes = pool_run(train_jobs, workers=workers if workers is not None else spec.workers)
        if len(outcomes) != len(train_jobs):
            raise WalkforwardRunError(f"the pool returned {len(outcomes)} outcomes for {len(train_jobs)} runs")
        rows: dict[int, list[dict]] = {w.index: [] for w in wins}
        for (wi, p), o in zip(slots, outcomes):
            row = {"index": p.index, "params": p.params, "status": o.status, "run_id": o.run_id, "error": o.error,
                   "objective_value": None}
            if o.status == "done" and o.run_id:
                stored = store.get_run(o.run_id) or {}
                row["objective_value"] = registry.objective_value(stored, objective)
            rows[wi].append(row)
        results = []
        for w in wins:
            chosen = select_best(rows[w.index], objective)
            results.append({"window": w, "train_run_ids": [r["run_id"] for r in rows[w.index] if r["run_id"]],
                            "chosen": chosen, "test_run_id": None,
                            "test_status": "pending" if chosen else "skipped",
                            "error": None if chosen else "no training run finished with a usable objective value"})
        state = _dump(results)
        store.update_research_job(job_id, state_json=_state(state))
        tested = [r for r in results if r["chosen"] is not None]
        test_jobs = [pool.PoolJob(cfg_for(next(p for p in points if p.index == r["chosen"]["index"]),
                                          r["window"].test_from, r["window"].test_to,
                                          f"{spec.name}-w{r['window'].index:02d}-oos"),
                                  role="oos", job_id=job_id) for r in tested]
        outs = pool_run(test_jobs, workers=workers if workers is not None else spec.workers) if test_jobs else []
        if len(outs) != len(test_jobs):
            raise WalkforwardRunError(f"the pool returned {len(outs)} outcomes for {len(test_jobs)} runs")
        for r, o in zip(tested, outs):
            r["test_run_id"], r["test_status"] = o.run_id, o.status
            r["error"] = None if o.status == "done" else (o.error or "the run failed")
        state = _dump(results)
        store.update_research_job(job_id, state_json=_state(state))   # `running` until Task 21's finish
    except BaseException as e:
        _mark_failed(job_id, e)
        raise
    return WalkforwardResult(
        job_id=job_id, name=spec.name, family_id=family, objective=objective, train=train, test=test, step=step,
        span=span, windows=[WindowResult(r["window"], r["train_run_ids"], r["chosen"], r["test_run_id"],
                                         r["test_status"], r["error"]) for r in results])


def _dump(results: list[dict]) -> list[dict]:
    """The state_json form of the windows (ISO dates, the chosen parameters and run ids)."""
    out = []
    for r in results:
        w, c = r["window"], r["chosen"]
        out.append({"index": w.index, "train_from": w.train_from.isoformat(), "train_to": w.train_to.isoformat(),
                    "test_from": w.test_from.isoformat(), "test_to": w.test_to.isoformat(),
                    "train_run_ids": r["train_run_ids"], "chosen_params": c["params"] if c else None,
                    "chosen_run_id": c["run_id"] if c else None, "test_run_id": r["test_run_id"],
                    "test_status": r["test_status"], "error": r["error"]})
    return out


# ── M6.5 (WF 6.5): stitching, in-sample vs out-of-sample, finish ──────────────────────────────────────────────────

SCHEMA = "stockify.walkforward/1"


def min_oos_days() -> int:
    """research.json walk_forward.min_oos_days (D-102): below this many out-of-sample days no Sharpe figure is shown."""
    return int(research.settings()["walk_forward"]["min_oos_days"])


@dataclass(frozen=True)
class WalkforwardSummary:
    windows: int                       # every window of the job, tested or not
    is_windows: int                    # how many windows entered is_sharpe (a stitched test piece and a defined Sharpe)
    oos_days: int                      # days in the stitched series
    is_sharpe: float | None            # annualised; mean over the stitched windows of the chosen training run's Sharpe
    oos_sharpe: float | None           # annualised Sharpe of the stitched series (excess of the risk-free rate)
    ratio: float | None                # oos_sharpe / is_sharpe; None when is_sharpe is undefined or <= 0
    chosen_modal_share: float | None   # share of the windows with a chosen set that chose the most common set
    n_trials: int                      # windows x grid: every training run is a try
    var_sharpe: float                  # per-period variance of the training runs' Sharpes (V in the DSR)
    dsr: stats.DsrResult | None


@dataclass(frozen=True)
class WalkforwardFinished:
    summary: WalkforwardSummary
    windows: list[dict]
    stitched_dates: list[str]
    stitched_index: list[float]


def stitch(pieces: list[tuple[list[str], list[float]]]) -> tuple[list[str], list[float]]:
    """The test windows' `(ISO dates, plain per-day returns)`, in window order, compounded into one index:
    `index_d = index_{d-1} * (1 + r_d)` with the index at 1.0 before the first day (so `index[0] = 1 + r_0`, and the
    lists are as long as the returns). Capital is reinvested and no state carries between windows; the gap between
    two windows is not filled (I-7). Dates must increase through the pieces, else ValueError."""
    dates: list[str] = []
    index: list[float] = []
    level = 1.0
    for ds, rs in pieces:
        if len(ds) != len(rs):
            raise ValueError(f"a piece has {len(ds)} dates but {len(rs)} returns")
        for d, r in zip(ds, rs):
            if isinstance(r, bool) or not isinstance(r, (int, float)) or not math.isfinite(r):
                raise ValueError(f"a return is not a finite number: {r!r}")
            if dates and d <= dates[-1]:
                raise ValueError(f"pieces are not in window order: {d} comes after {dates[-1]}")
            level *= 1 + r
            dates.append(d)
            index.append(level)
    return dates, index


def _series(run_id: str | None) -> tuple[int, list[str], list[int]] | None:
    """`(capital paise, dates, NAV paise)` of a run from the database alone; None when it has no usable rows."""
    if not run_id:
        return None
    row = store.get_run(run_id)
    if not row:
        return None
    try:
        capital = json.loads(row.get("config_json") or "{}").get("capital_paise")
    except ValueError:
        return None
    nav = store.run_nav_series(run_id)
    if isinstance(capital, bool) or not isinstance(capital, int) or capital <= 0 or not nav:
        return None
    return capital, [d for d, _ in nav], [n for _, n in nav]


def _per_period_sharpe(series, rf_rows: list[dict]) -> float | None:  # noqa: ANN001
    if series is None:
        return None
    capital, dates, navs = series
    return stats.sharpe_per_period(stats.excess_returns(dates, stats.returns_from_nav(capital, navs), rf_rows))


def _annual(sr: float | None) -> float | None:
    return None if sr is None else stats.annualised(sr)


def _period_metrics(series, rf_rows: list[dict]) -> dict:  # noqa: ANN001
    capital, dates, navs = series
    return {"from": dates[0], "to": dates[-1], "days": len(dates), "net_pnl_paise": navs[-1] - capital,
            "sharpe": _annual(_per_period_sharpe(series, rf_rows))}


def _mean(xs: list[float]) -> float:
    total = 0.0
    for v in xs:
        total += v
    return total / len(xs)


def finish(job_id: str) -> WalkforwardFinished:
    """Stitch the out-of-sample pieces, compare in-sample with out-of-sample, write `walkforward.json` and
    `walkforward.png` in the job folder, then mark the job `done` (the only writer of `done` for a walk-forward
    job: a job never reads done without its file). Reads only the database. On a `done` job it rewrites the same
    files and leaves the job as it is; a `failed` job is refused. Any other failure marks a `running` job
    `failed` and is re-raised. No `report.md` here: `wf_report.render` owns it."""
    job = store.get_research_job(job_id)
    if job is None:
        raise WalkforwardError(f"no such research job: {job_id!r}")
    if job["kind"] != "walkforward":
        raise WalkforwardError(f"{job_id!r} is not a walk-forward job (it is a {job['kind']} job)")
    if job["status"] not in ("running", "done", "failed"):
        raise WalkforwardError(f"{job_id!r} has status {job['status']!r}: finish works on a running, done or "
                               "failed job")
    if job["status"] == "failed":      # retry only when the failure came from finish: every window has its result
        missing = _pending_windows(job)
        if missing:
            raise WalkforwardError(f"{job_id!r} failed ({job.get('error') or 'no reason recorded'}) before every "
                                   f"window had its test result: window {', '.join(map(str, missing))} is missing "
                                   "it, so run_windows did not finish and there is nothing to finish")
    was_running = job["status"] != "done"
    try:
        finished = _compute(job)
        folder = paths.research_path(job_id)
        folder.mkdir(parents=True, exist_ok=True)
        spec = json.loads(job["spec_json"])
        doc = {"schema": SCHEMA, "job_id": job_id, "name": job["name"], "objective": spec.get("objective"),
               "train_days": spec.get("train"), "test_days": spec.get("test"), "step_days": spec.get("step"),
               "span": spec.get("span"), "summary": asdict(finished.summary), "windows": finished.windows,
               "dates": finished.stitched_dates, "index": finished.stitched_index}
        fileio.atomic_write_text(folder / "walkforward.json",
                                 json.dumps(doc, sort_keys=True, indent=2, allow_nan=False) + "\n")
        _equity_png(folder / "walkforward.png", finished)
        if was_running:
            store.update_research_job(job_id, status="done", finished_ns=timeutil.now_ns(), error=None)
    except BaseException as e:
        if was_running:
            _mark_failed(job_id, e)
        raise
    return finished


def _pending_windows(job: dict) -> list[int]:
    """Indexes of the windows whose test phase never ran (state `pending`); all of them when no state is stored."""
    try:
        state = json.loads(job["state_json"] or "")["windows"]
    except (ValueError, KeyError, TypeError):
        return [-1]
    return [w["index"] for w in state if w.get("test_status") == "pending"]


def _compute(job: dict) -> WalkforwardFinished:
    try:
        state = json.loads(job["state_json"] or "")["windows"]
        spec = json.loads(job["spec_json"])
    except (ValueError, KeyError, TypeError) as e:
        raise WalkforwardError(f"the job has no stored windows to finish: {e}") from None
    rf_rows = stats.risk_free_rows()
    out_windows, pieces, is_srs, chosen_sets, trial_srs = [], [], [], [], []
    for w in state:
        row = {k: w[k] for k in ("index", "train_from", "train_to", "test_from", "test_to", "chosen_params",
                                 "chosen_run_id", "test_run_id", "test_status", "error")}
        row["train"], row["test"] = None, None
        for rid in w["train_run_ids"]:                               # every training run is a try
            trial_srs.append(_per_period_sharpe(_series(rid), rf_rows))
        if w["chosen_params"] is not None:
            chosen_sets.append(json.dumps(w["chosen_params"], sort_keys=True))
        train = _series(w["chosen_run_id"])
        if train is not None:
            row["train"] = {"run_id": w["chosen_run_id"], **_period_metrics(train, rf_rows)}
        if w["test_run_id"] and w["test_status"] == "done":
            test = _series(w["test_run_id"])
            if test is None:
                row["test_status"], row["error"] = "failed", "the test run has no stored equity"
            else:
                capital, dates, navs = test
                row["test"] = {"run_id": w["test_run_id"], **_period_metrics(test, rf_rows)}
                pieces.append((dates, stats.returns_from_nav(capital, navs)))
                srs = _per_period_sharpe(train, rf_rows)
                if srs is not None:
                    is_srs.append(stats.annualised(srs))
        out_windows.append(row)
    dates, index = stitch(pieces)
    flat = [r for _, rs in pieces for r in rs]
    x = stats.excess_returns(dates, flat, rf_rows)
    oos = _annual(stats.sharpe_per_period(x))
    is_sharpe = _mean(is_srs) if is_srs else None
    ratio = oos / is_sharpe if oos is not None and is_sharpe is not None and is_sharpe > 0 else None
    n_trials = int(spec["windows"]) * int(spec["grid"]) if "windows" in spec and "grid" in spec else len(trial_srs)
    modal = max(Counter(chosen_sets).values()) / len(chosen_sets) if chosen_sets else None
    summary = WalkforwardSummary(
        windows=len(state), is_windows=len(is_srs), oos_days=len(dates), is_sharpe=is_sharpe, oos_sharpe=oos, ratio=ratio,
        chosen_modal_share=modal, n_trials=n_trials, var_sharpe=stats.sharpe_variance(trial_srs),
        dsr=stats.deflated_sharpe(x, trial_srs, n_trials))
    return WalkforwardFinished(summary, out_windows, dates, index)


def _segments(finished: WalkforwardFinished) -> tuple[list[tuple[list[str], list[float]]], list[str]]:
    """The stitched index cut back into one piece per tested window, and each such window's test start."""
    segs, starts = [], []
    for w in finished.windows:
        t = w["test"]
        if t is None:
            continue
        pick = [i for i, d in enumerate(finished.stitched_dates) if t["from"] <= d <= t["to"]]
        segs.append(([finished.stitched_dates[i] for i in pick], [finished.stitched_index[i] for i in pick]))
        starts.append(w["test_from"])
    return segs, starts


def _figure(segments: list[tuple[list[str], list[float]]], starts: list[str], note: str | None = None):  # noqa: ANN202
    """One line per out-of-sample piece (so a gap between windows is a gap, not a bridge), a light vertical line at
    each test start, and a marker on every point so a one-day piece shows. With a `note` (too few days to judge) it
    is written on the figure and the vertical range keeps 1.0 inside at least +/-5 %, so noise does not fill the
    panel."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(8, 3.2), dpi=110)
    for d in starts:
        ax.axvline(date.fromisoformat(d), linewidth=0.8, color="#bbbbbb", linestyle=":", label="test start")
    for ds, idx in segments:
        ax.plot([date.fromisoformat(d) for d in ds], idx, linewidth=1.2, color="#0072B2", marker="o", markersize=2.5,
                label="out-of-sample")
    ax.axhline(1.0, linewidth=0.8, color="#999999")
    if note:
        lo, hi = ax.get_ylim()
        ax.set_ylim(min(lo, 0.95), max(hi, 1.05))
        ax.text(0.02, 0.96, note, transform=ax.transAxes, va="top", fontsize=9, color="#444444")
    ax.set_ylabel("growth of 1 (capital = 1.0)")
    ax.grid(alpha=0.3)
    ax.spines[["top", "right"]].set_visible(False)
    fig.autofmt_xdate()
    fig.tight_layout()
    return fig


def _equity_png(path, finished: WalkforwardFinished) -> None:  # noqa: ANN001
    """The stitched out-of-sample curve, drawn piece by piece. Not drawn when the stitch is empty."""
    if not finished.stitched_dates:
        return
    import matplotlib.pyplot as plt
    segs, starts = _segments(finished)
    days = finished.summary.oos_days
    fig = _figure(segs, starts, f"{days} out-of-sample days only" if days < min_oos_days() else None)
    tmp = path.with_name(path.name + ".tmp.png")
    try:
        fig.savefig(tmp)
        os.replace(tmp, path)
    finally:
        plt.close(fig)
        tmp.unlink(missing_ok=True)

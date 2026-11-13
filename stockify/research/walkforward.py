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
import math
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from stockify import runconfig, store
from stockify.research import holdout, pool, registry, sweep

_SPAN = re.compile(r"[0-9]+d", re.ASCII)
_NAME_PART_IN_JOB = 24


class WalkforwardError(ValueError):
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
    """`"120d"` is 120 calendar days. Anything but whole digits and a `d` is refused (months and trading days
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


def select_best(rows: list[dict], objective: str) -> dict | None:
    """The `done` row with the highest finite objective value; a tie goes to the lowest grid `index`. A row's
    value is its `objective_value`, or (key absent) read from its `metrics_json`. None when no row qualifies."""
    best, best_key = None, None
    for row in rows:
        if row.get("status") != "done":
            continue
        v = row["objective_value"] if "objective_value" in row else registry.objective_value(row, objective)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            continue
        key = (-v, row["index"])
        if best_key is None or key < best_key:
            best, best_key = row, key
    return best


def _job_id(name: str, now_ns: int) -> str:
    part = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)[:_NAME_PART_IN_JOB].rstrip(".") or "run"
    stamp = datetime.fromtimestamp(now_ns / 1e9, tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"wf-{part}-{stamp}-{secrets.token_hex(3)}"


def _state(results: list[dict]) -> str:
    return json.dumps({"windows": results}, sort_keys=True, allow_nan=False)


def run_windows(spec: sweep.SweepSpec, *, train: int, test: int, step: int, objective: str,
                start: date | None = None, end: date | None = None, unlock_reason: str | None = None,
                workers: int | None = None, pool_run=pool.run_all,  # noqa: ANN001
                now_ns: int | None = None) -> WalkforwardResult:
    """Walk `spec` forward. WalkforwardError / ValueError before a job exists (bad sizes, no window fits, an
    unknown objective); from then on any failure (a locked holdout, a pool that raises) marks the job `failed`
    with the message and is re-raised."""
    from stockify import timeutil
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
                                 "windows": len(wins), "unlocked": unlock_reason is not None}, sort_keys=True)})
    state: list[dict] = []
    try:
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
            raise WalkforwardError(f"the pool returned {len(outcomes)} outcomes for {len(train_jobs)} runs")
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
            raise WalkforwardError(f"the pool returned {len(outs)} outcomes for {len(test_jobs)} runs")
        for r, o in zip(tested, outs):
            r["test_run_id"], r["test_status"] = o.run_id, o.status
            r["error"] = None if o.status == "done" else (o.error or "the run failed")
        state = _dump(results)
        store.update_research_job(job_id, state_json=_state(state), status="done",
                                  finished_ns=timeutil.now_ns())
    except BaseException as e:
        store.update_research_job(job_id, status="failed", finished_ns=timeutil.now_ns(),
                                  error=f"{type(e).__name__}: {e}"[:500])
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

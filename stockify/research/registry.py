"""The trial registry (M6.4, AM 6.3): every run and session has a family, every run has a role, and only
`trial` runs that finished (`done`) count as tries (N, V in the deflated Sharpe).

    registry.default_family_id(cfg)        # "<strategy>/<dataset>-<interval>-<universe>/<objective>"
    registry.trials(family_id)             # TrialRun list, from the database only (a pruned folder still counts)
    registry.best_trial(family_id)         # the highest value of the family's own objective

Everything here reads `runs`, `run_equity` and `trial_families`; never a run folder.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

from stockify import paths, store

ROLES = ("trial", "sensitivity", "oos", "rerun")
OBJECTIVES = ("sharpe", "sortino", "calmar", "cagr", "net_pnl")

# The runconfig rule: every "/"-separated part passes paths.safe_segment(part, 64) and the whole is at most 128.
# 50 + 64 + 7 (the longest objective) + 2 slashes = 123, so the whole is always inside 128.
STRATEGY_MAX = 50
MIDDLE_MAX = 64
_PIECE_MAX = 16                    # a dataset or an interval, so the universe keeps room in the middle part
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _clean(text: object) -> str:
    return _UNSAFE.sub("_", str(text))


def _segment(text: str, limit: int) -> str:
    """`text` made into one safe segment of at most `limit` characters (paths.safe_segment)."""
    s = _clean(text)[:limit]
    if not s or not s[0].isalnum():
        s = ("s" + s)[:limit]
    if s.endswith("."):
        s = s[:-1] + "_"
    if not paths.safe_segment(s, limit):                  # a Windows device name (CON, NUL, COM1 ...)
        s = ("x_" + s)[:limit]
    return s


def _strategy_part(cfg: dict) -> str:
    s = cfg.get("strategy") or {}
    kind, ref = s.get("kind"), s.get("ref")
    if kind == "python" and isinstance(ref, str):
        return ref.split(":", 1)[0].split(".")[-1] or "script"
    if kind == "cpp" and isinstance(ref, str) and ref:
        return ref
    return "script"


def _instrument_text(sel: object) -> str:
    if isinstance(sel, str):
        return sel
    if isinstance(sel, dict) and sel:
        return sorted(sel)[0]                             # a selector object: its one form (chain, ...)
    return "instrument"


def _universe_part(instruments: list, budget: int) -> str:
    first = _clean(_instrument_text(instruments[0])) if instruments else "none"
    more = f"_plus{len(instruments) - 1}" if len(instruments) > 1 else ""
    first = first[:max(1, budget - len(more))]            # the over-long part goes first; the suffix stays
    return first + more


def _family(cfg: dict, dataset: str, interval: str | None, instruments: list, objective: str) -> str:
    if objective not in OBJECTIVES:
        raise ValueError(f"objective must be one of {list(OBJECTIVES)}, not {objective!r}")
    head = _clean(dataset)[:_PIECE_MAX] + ("-" + _clean(interval)[:_PIECE_MAX] if interval else "")
    if instruments:
        middle = head + "-" + _universe_part(instruments, MIDDLE_MAX - len(head) - 1)
    else:
        middle = head
    return "/".join((_segment(_strategy_part(cfg), STRATEGY_MAX), _segment(middle, MIDDLE_MAX), objective))


def default_family_id(cfg: dict, objective: str = "sharpe") -> str:
    """`<strategy>/<dataset>-<interval>-<universe>/<objective>` of a run config. It IGNORES `cfg["family_id"]`
    (the caller that wants to keep an explicit one checks it first, as `runs.queue` does). `<strategy>` is a
    python ref's module name (last part), a cpp ref's registered name, `script` otherwise; the dataset and
    interval come from the first pack spec (`packs` when packs are named by hash only); `<universe>` is the first
    instrument, unsafe characters turned to `_`, plus `_plus<n>` for n more. Always a valid `family_id`."""
    packs = (cfg.get("data") or {}).get("packs") or []
    spec = packs[0].get("spec") if packs and isinstance(packs[0], dict) else None
    if not isinstance(spec, dict):
        return _family(cfg, "packs", None, [], objective)
    instruments = spec.get("instruments")
    return _family(cfg, spec.get("dataset") or "packs", spec.get("interval"),
                   instruments if isinstance(instruments, list) else [], objective)


def job_family_id(kind: str, job_id: str) -> str:
    """The family of a sweep or walk-forward job: each job is its own family (`kind` sweep or walkforward)."""
    return f"{kind}/{job_id}"


def family_for_session(cfg: dict, objective: str = "sharpe") -> str:
    """The default family of a paper session config: the same rule, dataset `live`, the session's universe."""
    universe = cfg.get("universe")
    return _family(cfg, "live", None, universe if isinstance(universe, list) else [], objective)


def ensure_family(family_id: str, objective: str = "sharpe") -> None:
    """Make sure the `trial_families` row exists (an existing row, and a name the owner gave it, is untouched)."""
    store.add_family(family_id, objective=objective)


def check_role(role: object) -> str:
    """`role` if it is one of ROLES, else ValueError (a typo would otherwise hide a trial from N)."""
    if not isinstance(role, str) or role not in ROLES:
        raise ValueError(f"role must be one of {list(ROLES)}, not {role!r}")
    return role


def objective_value(row: dict, objective: str) -> float | None:
    """The run's value of `objective` from its `metrics_json`; `None` when the metric is null or missing.
    `net_pnl` is `nav_end_paise - nav_start_paise`, a number of paise; the others are unitless."""
    if objective not in OBJECTIVES:
        raise ValueError(f"objective must be one of {list(OBJECTIVES)}, not {objective!r}")
    text = row.get("metrics_json")
    if not text:
        return None
    try:
        m = json.loads(text)
    except ValueError:
        return None
    if not isinstance(m, dict):
        return None
    if objective == "net_pnl":
        a, b = m.get("nav_start_paise"), m.get("nav_end_paise")
        if isinstance(a, bool) or isinstance(b, bool) or not isinstance(a, int) or not isinstance(b, int):
            return None
        return float(b - a)
    v = m.get(objective)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def select_best(rows: list[dict], objective: str) -> dict | None:
    """The `done` row with the highest finite objective value; a tie goes to the lowest grid `index`. A row's
    value is its `objective_value`, or (key absent) read from its `metrics_json`. None when no row qualifies."""
    best, best_key = None, None
    for row in rows:
        if row.get("status") != "done":
            continue
        v = row["objective_value"] if "objective_value" in row else objective_value(row, objective)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            continue
        key = (-v, row["index"])
        if best_key is None or key < best_key:
            best, best_key = row, key
    return best


@dataclass(frozen=True)
class TrialRun:
    run_id: str
    name: str | None
    job_id: str | None
    dates: tuple[str, ...]
    navs: tuple[int, ...]
    capital_paise: int
    seed: int
    objective_value: float | None


def family_objective(family_id: str) -> str:
    """The family's own objective: its row's, else the last segment of its id when that is one, else sharpe."""
    fam = store.get_family(family_id)
    if fam and fam.get("objective") in OBJECTIVES:
        return fam["objective"]
    last = family_id.rsplit("/", 1)[-1]
    return last if last in OBJECTIVES else "sharpe"


def trials(family_id: str) -> list[TrialRun]:
    """The family's tries: runs with role `trial` and status `done`, ordered by name then run id. Built from
    `run_equity`, `config_json` and `metrics_json` only, so a pruned folder changes nothing."""
    objective = family_objective(family_id)
    out = []
    for row in store.family_runs(family_id, roles=("trial",), status="done"):
        try:
            cfg = json.loads(row["config_json"]) if row.get("config_json") else {}
        except ValueError:
            cfg = {}
        series = store.run_nav_series(row["run_id"])
        cap, seed = cfg.get("capital_paise"), cfg.get("seed")
        out.append(TrialRun(run_id=row["run_id"], name=row["name"], job_id=row.get("job_id"),
                            dates=tuple(d for d, _ in series), navs=tuple(n for _, n in series),
                            capital_paise=cap if isinstance(cap, int) else 0,
                            seed=seed if isinstance(seed, int) else 0,
                            objective_value=objective_value(row, objective)))
    return out


def counted_runs(family_id: str) -> list[dict]:
    """The family's role-`trial` runs that were tried: finished or failed (a queued, running or cancelled run was
    never tried). Newest rule, owner decision 2026-11-13 (D-85): a failed run still counts toward N."""
    return [r for r in store.family_runs(family_id, roles=("trial",), status=None)
            if r.get("status") in ("done", "failed")]


def n_tries(family_id: str) -> int:
    """N for the deflated Sharpe: every setting tried, finished or not (D-85, D-133). The tried trial runs, or for
    a finished sweep or walk-forward job the number its own spec says (a point that never made a run still
    counts: sweep `trials`, walk-forward windows x grid), whichever is larger."""
    n = len(counted_runs(family_id))
    kind, _, job_id = family_id.partition("/")
    if kind in ("sweep", "walkforward") and job_id:
        job = store.get_research_job(job_id)
        if job and job.get("status") == "done":
            try:
                spec = json.loads(job.get("spec_json") or "")
                planned = (int(spec["trials"]) if kind == "sweep"
                           else int(spec["windows"]) * int(spec["grid"]))
            except (ValueError, KeyError, TypeError):
                planned = 0
            n = max(n, planned)
    return n


def best_trial(family_id: str) -> TrialRun | None:
    """The trial with the highest objective value under the family's objective; ties go to the lowest name, then
    run id. A trial whose value is None is not eligible; no eligible trial gives None."""
    eligible = [t for t in trials(family_id) if t.objective_value is not None]
    if not eligible:
        return None
    return min(eligible, key=lambda t: (-t.objective_value, t.name or "", t.run_id))


def _family_of_row(row: dict) -> str | None:
    try:
        cfg = json.loads(row["config_json"])
        return default_family_id(cfg) if isinstance(cfg, dict) else None
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def backfill(dry_run: bool = False) -> int:
    """Give every run whose `family_id` is NULL the default family of its stored config; the number of runs
    set (a dry run: that would be set, and nothing is written, not even a family row). Rows with other families
    are never touched."""
    if dry_run:
        n, offset = 0, 0
        while True:
            page = store.list_runs(offset=offset, limit=200)
            if not page:
                return n
            n += sum(1 for r in page if r["family_id"] is None and _family_of_row(r) is not None)
            offset += len(page)

    def compute(row: dict) -> str | None:
        fam = _family_of_row(row)
        if fam is not None:
            ensure_family(fam)                  # the family row first: a crash can leave a row, never a run without
        return fam

    return store.backfill_null_families(compute)

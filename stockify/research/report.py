"""The research report of a run and of a family (M6.4 deflated Sharpe, M6.8 bootstrap; IL 6.3).

    report.run_research(run_id)      # RunResearch: rank among the family's tries, DSR, bootstrap, plain sentences
    report.family_report(family_id)  # FamilyReport: the tries, the other runs, sessions, unlocks
    report.list_families()           # FamilySummary list

Everything is read from the database (never a run folder), so a pruned folder changes nothing. Rank, N, V, the
deflated Sharpe and the sentences are recomputed on every call (they change whenever a trial joins the family).
Only the bootstrap, which is slow and depends on this run alone, is cached in `runs/<id>/research.json`, keyed by
a hash of everything it was computed from; a missing, unreadable or odd file is simply a cache miss.

Units: money is int paise; the Sharpe in `DsrResult` is per day (the page shows it annualised); the bootstrap mean
return is a per-day fraction and the drawdown a fraction of the peak.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal

from stockify import paths, store
from stockify.research import bootstrap, fileio, plain, registry, settings, stats
from stockify.research.bootstrap import BootstrapResult, Interval
from stockify.research.stats import DsrResult

CODE_VERSION = "1"        # bump when the bootstrap or the way its inputs are built changes: it invalidates the cache
CACHE_NAME = "research.json"
_NOT_A_TRY = "a variant, check or re-run, not a try, so it is not counted among the tries"


@dataclass(frozen=True)
class RunResearch:
    run_id: str
    family_id: str | None
    role: str
    objective: str
    n_trials: int
    rank: int | None
    is_best: bool
    picked_run_id: str | None
    picked_name: str | None
    dsr: DsrResult | None
    bootstrap: BootstrapResult | None
    regimes: dict | None
    holdout_unlocks: int
    plain: dict[str, str]


@dataclass(frozen=True)
class TrialRow:
    run_id: str
    name: str | None
    role: str
    status: str
    objective_value: float | None
    sharpe_per_period: float | None
    net_pnl_paise: int | None
    days: int
    job_id: str | None


@dataclass(frozen=True)
class FamilyReport:
    family_id: str
    description: str | None
    objective: str
    named: bool
    n_trials: int
    var_sharpe: float | None
    best_run_id: str | None
    best: DsrResult | None
    trials: list[TrialRow]
    others: list[TrialRow]
    sessions: list[dict]
    holdout_unlocks: list[dict]
    plain: dict[str, str]


@dataclass(frozen=True)
class FamilySummary:
    family_id: str
    description: str | None
    named: bool
    objective: str
    n_trials: int
    n_other_runs: int
    n_sessions: int
    best_run_id: str | None
    best_dsr: float | None
    unlocks: int


# ── shared pieces ─────────────────────────────────────────────────────────────────────────────

def _config(row: dict) -> dict:
    try:
        cfg = json.loads(row["config_json"]) if row.get("config_json") else {}
    except ValueError:
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _int_or_none(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _series(dates, navs, capital: int) -> tuple[list[float], list[float]]:
    """(r, x): the plain and the excess daily returns of a NAV series."""
    r = stats.returns_from_nav(capital, list(navs))
    return r, stats.excess_returns(list(dates), r, stats.risk_free_rows())


def _trial_sharpe(t: registry.TrialRun) -> float | None:
    if not t.navs:
        return None
    _, x = _series(t.dates, t.navs, t.capital_paise)
    return stats.sharpe_per_period(x)


def _net_pnl(row: dict) -> int | None:
    try:
        m = json.loads(row["metrics_json"]) if row.get("metrics_json") else {}
    except ValueError:
        return None
    if not isinstance(m, dict):
        return None
    a, b = _int_or_none(m.get("nav_start_paise")), _int_or_none(m.get("nav_end_paise"))
    return None if a is None or b is None else b - a


def _unlock_count(family_id: str | None) -> int:
    return len(store.holdout_unlock_rows(family_id)) if family_id else 0


# ── the bootstrap and its cache ───────────────────────────────────────────────────────────────

def _settings() -> dict:
    s = settings()["bootstrap"]
    return {"mean_block": float(Decimal(str(s["mean_block"]))), "ci": float(Decimal(str(s["ci"]))),
            "resamples": int(s["resamples"]), "min_days": int(s["min_days"])}


def _cache_key(r: list[float], dates: list[str], rf_rows: list[dict], cfg: dict, seed: int, last: date) -> str:
    blob = {"returns": r, "dates": dates, "risk_free": rf_rows, "bootstrap": cfg, "seed": seed,
            "last_date": last.isoformat(), "code_version": CODE_VERSION}
    text = json.dumps(blob, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _num(v, *, integer: bool = False):
    """`v` as a finite number (an int when `integer`; None allowed for floats), else ValueError."""
    if isinstance(v, bool):
        raise ValueError("bool")
    if integer:
        if not isinstance(v, int):
            raise ValueError("int")
        return v
    if v is None:
        return None
    if not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError("number")
    return float(v)


def _interval(d) -> Interval:
    if not isinstance(d, dict) or set(d) != {"point", "lo", "hi"}:
        raise ValueError("interval")
    return Interval(_num(d["point"]), _num(d["lo"]), _num(d["hi"]))


def _from_cache(d) -> BootstrapResult:
    """Rebuild a BootstrapResult from cached JSON, checking every field; anything odd is a ValueError."""
    fields = {"n_days", "mean_block", "resamples", "ci", "seed", "sharpe", "mean_daily_return", "max_drawdown",
              "enough_days"}
    if not isinstance(d, dict) or set(d) != fields or not isinstance(d["enough_days"], bool):
        raise ValueError("bootstrap")
    return BootstrapResult(_num(d["n_days"], integer=True), _num(d["mean_block"]), _num(d["resamples"], integer=True),
                           _num(d["ci"]), _num(d["seed"], integer=True), _interval(d["sharpe"]),
                           _interval(d["mean_daily_return"]), _interval(d["max_drawdown"]), d["enough_days"])


def _read_cache(path, key: str) -> BootstrapResult | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("key") != key:
            return None
        return _from_cache(data.get("bootstrap"))
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return None


def _write_cache(path, key: str, result: BootstrapResult) -> None:
    try:
        if not path.parent.is_dir():                       # a pruned or never-made folder: no cache, no error
            return
        text = json.dumps({"key": key, "bootstrap": asdict(result)}, sort_keys=True, allow_nan=False)
        fileio.atomic_write_text(path, text)
    except (OSError, ValueError):
        pass


def _cache_path(run_id: str):
    try:
        return paths.run_path(run_id) / CACHE_NAME
    except ValueError:
        return None


def _bootstrap(run_id: str, row: dict, dates, navs, use_cache: bool) -> BootstrapResult | None:
    if not navs:
        return None
    cfg = _config(row)
    capital = _int_or_none(cfg.get("capital_paise")) or 0
    seed = _int_or_none(cfg.get("seed")) or 0
    r, x = _series(dates, navs, capital)
    last = date.fromisoformat(dates[-1])
    s = _settings()
    path = _cache_path(run_id) if use_cache else None
    key = _cache_key(r, list(dates), stats.risk_free_rows(), s, seed, last)
    if path is not None:
        hit = _read_cache(path, key)
        if hit is not None:
            return hit
    result = bootstrap.bootstrap_ci(r, x, mean_block=s["mean_block"], resamples=s["resamples"], ci=s["ci"],
                                    min_days=s["min_days"], seed=seed, last_date=last)
    if path is not None:
        _write_cache(path, key, result)
    return result


# ── the run report ────────────────────────────────────────────────────────────────────────────

def _ranked(trials: list[registry.TrialRun]) -> list[registry.TrialRun]:
    """The family's eligible tries, best first: the objective's order, ties by name then run id."""
    return sorted((t for t in trials if t.objective_value is not None),
                  key=lambda t: (-t.objective_value, t.name or "", t.run_id))


def run_research(run_id: str, *, use_cache: bool = True) -> RunResearch:
    row = store.get_run(run_id)
    if row is None:
        raise ValueError(f"no such run: {run_id!r}")
    family_id, role = row.get("family_id"), row.get("role") or "trial"
    objective = registry.family_objective(family_id) if family_id else "sharpe"
    tries = registry.trials(family_id) if family_id else []
    n = len(tries)
    series = store.run_nav_series(run_id)
    dates, navs = [d for d, _ in series], [v for _, v in series]
    capital = _int_or_none(_config(row).get("capital_paise")) or 0

    ranked = _ranked(tries)
    rank = next((i + 1 for i, t in enumerate(ranked) if t.run_id == run_id), None)
    picked = registry.best_trial(family_id) if family_id else None
    is_try = any(t.run_id == run_id for t in tries)

    res, flat = None, True
    if navs:
        _, x = _series(dates, navs, capital)
        flat = stats.sharpe_per_period(x) is None
        res = stats.deflated_sharpe(x, [_trial_sharpe(t) for t in tries], max(n, 1))

    texts: dict[str, str] = {}
    if flat:
        texts["dsr"] = plain.dsr_sentence(max(n, 1), rank or 1, None, flat=True)
        texts["psr"] = texts["dsr"]
    else:
        if not is_try:
            texts["dsr"] = _NOT_A_TRY if role != "trial" else "did not finish, so it is not a try"
        elif rank is None:
            texts["dsr"] = "no value for the family's objective, so it is not ranked among the tries"
        else:
            texts["dsr"] = plain.dsr_sentence(n, rank, res.dsr if res else None, objective=objective,
                                              picked_name=picked.name if picked else None)
        texts["psr"] = plain.psr_sentence(res.psr if res else None)
    boot = _bootstrap(run_id, row, dates, navs, use_cache)
    if boot is not None:
        texts["bootstrap_sharpe"] = plain.interval_sentence("Sharpe", boot.sharpe, "", ci=boot.ci)
        texts["bootstrap_mean_return"] = plain.interval_sentence("Mean daily return", boot.mean_daily_return, "%",
                                                                 ci=boot.ci)
        texts["bootstrap_max_drawdown"] = plain.interval_sentence("Max drawdown", boot.max_drawdown, "%", ci=boot.ci)
    return RunResearch(run_id=run_id, family_id=family_id, role=role, objective=objective, n_trials=n,
                       rank=rank, is_best=rank == 1, picked_run_id=picked.run_id if picked else None,
                       picked_name=picked.name if picked else None, dsr=res if is_try and not flat else None,
                       bootstrap=boot, regimes=None, holdout_unlocks=_unlock_count(family_id), plain=texts)


# ── the family report ─────────────────────────────────────────────────────────────────────────

def _row(row: dict, objective: str) -> TrialRow:
    series = store.run_nav_series(row["run_id"])
    sharpe = None
    if series:
        capital = _int_or_none(_config(row).get("capital_paise")) or 0
        _, x = _series([d for d, _ in series], [v for _, v in series], capital)
        sharpe = stats.sharpe_per_period(x)
    return TrialRow(run_id=row["run_id"], name=row.get("name"), role=row.get("role") or "trial",
                    status=row.get("status") or "", objective_value=registry.objective_value(row, objective),
                    sharpe_per_period=sharpe, net_pnl_paise=_net_pnl(row), days=len(series), job_id=row.get("job_id"))


def _best_dsr(tries: list[registry.TrialRun], best: registry.TrialRun | None) -> DsrResult | None:
    if best is None or not best.navs:
        return None
    _, x = _series(best.dates, best.navs, best.capital_paise)
    return stats.deflated_sharpe(x, [_trial_sharpe(t) for t in tries], len(tries))


def _family_sessions(family_id: str, sessions: list[dict]) -> list[dict]:
    return [{"session_id": s["session_id"], "name": s.get("name"), "status": s.get("status"),
             "created_ns": s.get("created_ns")} for s in sessions if s.get("family_id") == family_id]


def family_report(family_id: str) -> FamilyReport:
    fam = store.get_family(family_id)
    if fam is None:
        raise ValueError(f"no such family: {family_id!r}")
    objective = registry.family_objective(family_id)
    tries = registry.trials(family_id)
    best = registry.best_trial(family_id)
    dsr = _best_dsr(tries, best)
    counted = {t.run_id for t in tries}
    rows = store.family_runs(family_id, roles=None, status=None)
    texts: dict[str, str] = {}
    if best is not None:
        flat = best.navs and _trial_sharpe(best) is None
        texts["dsr"] = plain.dsr_sentence(len(tries), 1, dsr.dsr if dsr else None, objective=objective,
                                          flat=bool(flat))
    return FamilyReport(family_id=family_id, description=fam.get("description"), objective=objective,
                        named=bool(fam.get("named")), n_trials=len(tries),
                        var_sharpe=dsr.var_sharpe if dsr else None, best_run_id=best.run_id if best else None,
                        best=dsr, trials=[_row(r, objective) for r in rows if r["run_id"] in counted],
                        others=[_row(r, objective) for r in rows if r["run_id"] not in counted],
                        sessions=_family_sessions(family_id, store.list_sessions()),
                        holdout_unlocks=store.holdout_unlock_rows(family_id), plain=texts)


def list_families() -> list[FamilySummary]:
    sessions = store.list_sessions()
    out = []
    for fam in store.list_families():
        fid = fam["family_id"]
        tries = registry.trials(fid)
        best = registry.best_trial(fid)
        dsr = _best_dsr(tries, best)
        out.append(FamilySummary(
            family_id=fid, description=fam.get("description"), named=bool(fam.get("named")),
            objective=registry.family_objective(fid), n_trials=len(tries),
            n_other_runs=len(store.family_runs(fid, roles=None, status=None)) - len(tries),
            n_sessions=len(_family_sessions(fid, sessions)),
            best_run_id=best.run_id if best else None, best_dsr=dsr.dsr if dsr else None,
            unlocks=_unlock_count(fid)))
    return out


# ── plain text for the console (the pages show the same strings) ──────────────────────────────

def _paise_text(paise: int | None) -> str:
    return "-" if paise is None else plain.rupees(paise / 100)


def _annual(sr: float | None) -> str:
    return "none" if sr is None else f"{stats.annualised(sr):.2f}"


def run_text(res: RunResearch) -> str:
    lines = [f"run {res.run_id}   family {res.family_id or '(none)'}   role {res.role}",
             f"tries: {res.plain.get('dsr', '-')}"]
    if res.dsr is not None:
        d = res.dsr
        lines.append(f"Sharpe {_annual(d.sr)} a year over {d.t} days; luck alone would give the best of "
                     f"{d.n_trials} tries {_annual(d.sr0)}")
    lines.append(f"on its own: {res.plain.get('psr', '-')}")
    if res.picked_name is not None and not res.is_best:
        lines.append(f"picked by the family's objective ({res.objective}): {res.picked_name} ({res.picked_run_id})")
    for key, label in (("bootstrap_sharpe", "Sharpe a year"), ("bootstrap_mean_return", "mean daily return"),
                       ("bootstrap_max_drawdown", "biggest fall")):
        if key in res.plain:
            lines.append(f"{label}: {res.plain[key]}")
    if res.holdout_unlocks:
        lines.append(f"holdout looked at {res.holdout_unlocks} time(s) in this family")
    return "\n".join(lines)


def families_table(fams: list[FamilySummary]) -> str:
    head = ("family", "name", "objective", "tries", "other", "sessions", "best", "DSR", "unlocks")
    rows = [head] + [(f.family_id, f.description or "-", f.objective, str(f.n_trials), str(f.n_other_runs),
                      str(f.n_sessions), f.best_run_id or "-",
                      "-" if f.best_dsr is None else f"{f.best_dsr * 100:.1f}%", str(f.unlocks)) for f in fams]
    widths = [max(len(r[i]) for r in rows) for i in range(len(head))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows)


def family_text(rep: FamilyReport) -> str:
    lines = [f"family {rep.family_id}" + (f"   {rep.description}" if rep.description else ""),
             f"objective {rep.objective}; {rep.n_trials} tries; {len(rep.others)} other runs (not counted); "
             f"{len(rep.sessions)} paper sessions (not counted); holdout looked at {len(rep.holdout_unlocks)} time(s)"]
    if "dsr" in rep.plain:
        lines.append(f"best ({rep.best_run_id}): {rep.plain['dsr']}")
    for t in rep.trials:
        lines.append(f"  {t.run_id:<24} {t.name or '-':<20} Sharpe {_annual(t.sharpe_per_period):>6}  "
                     f"net {_paise_text(t.net_pnl_paise):>12}  {t.days} days")
    return "\n".join(lines)

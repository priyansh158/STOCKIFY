"""The research report of a run and of a family (M6.4 deflated Sharpe, M6.8 bootstrap; IL 6.3).

    report.run_research(run_id)      # RunResearch: rank among the family's tries, DSR, bootstrap, plain sentences
    report.family_report(family_id)  # FamilyReport: the tries, the other runs, sessions, unlocks
    report.list_families()           # FamilySummary list

Everything is read from the database (never a run folder), so a pruned folder changes nothing. Rank, N, V, the
deflated Sharpe and the sentences are recomputed on every call (they change whenever a trial joins the family).
The bootstrap (slow, depends on this run alone) and the regime split (reads refdata) are cached in
`runs/<id>/research.json`, each under its own key, a hash of everything it was computed from; a missing,
unreadable or odd file is simply a cache miss.

Units: money is int paise; the Sharpe in `DsrResult` is per day (the page shows it annualised); the bootstrap mean
return is a per-day fraction and the drawdown a fraction of the peak.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal

from stockify import paths, store
from stockify.research import bootstrap, fileio, pbo, plain, regimes, registry, settings, stats
from stockify.research.bootstrap import BootstrapResult, Interval
from stockify.research.pbo import PboResult
from stockify.research.stats import DsrResult

CODE_VERSION = "1"        # bump when the bootstrap or the way its inputs are built changes: it invalidates the cache
CACHE_NAME = "research.json"
_NO_SERIES = "no usable equity series (no days, or no starting capital), so no Sharpe to judge"
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
    regimes: regimes.RegimeReport | None
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
    pbo: PboResult | None = None
    pbo_note: str | None = None
    pbo_dropped: list[str] = field(default_factory=list)   # tries left out of the PBO for want of shared days
    pbo_flat: list[str] = field(default_factory=list)      # tries left out of the PBO for being flat


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


def _usable(navs, capital: int) -> bool:
    """A NAV series can be turned into returns only with a positive starting capital."""
    return bool(navs) and capital > 0


def _series(dates, navs, capital: int, rf_rows: list[dict]) -> tuple[list[float], list[float]]:
    """(r, x): the plain and the excess daily returns of a NAV series; two empty lists when there is no usable
    series (no rows, or a capital that is missing or not positive)."""
    if not _usable(navs, capital):
        return [], []
    r = stats.returns_from_nav(capital, list(navs))
    return r, stats.excess_returns(list(dates), r, rf_rows)


def _trial_sharpe(t: registry.TrialRun, rf_rows: list[dict]) -> float | None:
    _, x = _series(t.dates, t.navs, t.capital_paise, rf_rows)
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


def _load(path) -> dict:
    """The cache file as a dict; {} when it is missing, unreadable or not an object (a miss for every entry)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def _save(path, data: dict) -> None:
    try:
        if not path.parent.is_dir():                       # a pruned or never-made folder: no cache, no error
            return
        fileio.atomic_write_text(path, json.dumps(data, sort_keys=True, allow_nan=False))
    except (OSError, ValueError):
        pass


def _write_cache(path, key: str, result: BootstrapResult) -> None:
    """Replace the bootstrap entry ({"key", "bootstrap"} at the top, as since Task 8), keeping the regimes entry."""
    data = {"key": key, "bootstrap": asdict(result)}
    kept = _load(path).get("regimes")
    if isinstance(kept, dict):
        data["regimes"] = kept
    _save(path, data)


def _read_regimes(path, key: str) -> regimes.RegimeReport | None:
    try:
        entry = _load(path).get("regimes")
        if not isinstance(entry, dict) or set(entry) != {"key", "value"} or entry["key"] != key:
            return None
        return regimes.from_dict(entry["value"])
    except (ValueError, TypeError, KeyError, RecursionError):
        return None


def _write_regimes(path, key: str, rep: regimes.RegimeReport) -> None:
    """Replace the regimes entry, keeping whatever the file holds for the bootstrap (stale or not: it has its own key)."""
    data = {k: v for k, v in _load(path).items() if k in ("key", "bootstrap")}
    data["regimes"] = {"key": key, "value": regimes.to_dict(rep)}
    _save(path, data)


def _cache_path(run_id: str):
    try:
        return paths.run_path(run_id) / CACHE_NAME
    except ValueError:
        return None


def _bootstrap(run_id: str, row: dict, dates, navs, use_cache: bool, rf_rows: list[dict]) -> BootstrapResult | None:
    cfg = _config(row)
    capital = _int_or_none(cfg.get("capital_paise")) or 0
    if not _usable(navs, capital):
        return None
    seed = _int_or_none(cfg.get("seed")) or 0
    r, x = _series(dates, navs, capital, rf_rows)
    last = date.fromisoformat(dates[-1])
    s = _settings()
    path = _cache_path(run_id) if use_cache else None
    key = _cache_key(r, list(dates), rf_rows, s, seed, last)
    if path is not None:
        hit = _read_cache(path, key)
        if hit is not None:
            return hit
    result = bootstrap.bootstrap_ci(r, x, mean_block=s["mean_block"], resamples=s["resamples"], ci=s["ci"],
                                    min_days=s["min_days"], seed=seed, last_date=last)
    if path is not None:
        _write_cache(path, key, result)
    return result


def _regimes(run_id: str, dates, navs, capital: int, use_cache: bool) -> regimes.RegimeReport | None:
    """The regime split, cached under its own key (NAV series, settings, refdata digest, code version): a late
    bhavcopy recomputes only this, never the bootstrap. A refdata that cannot be read leaves it out."""
    if not navs:
        return None
    try:
        path = _cache_path(run_id) if use_cache else None
        key = regimes.cache_key(list(dates), list(navs), capital)
        if path is not None:
            hit = _read_regimes(path, key)
            if hit is not None:
                return hit
        rep = regimes.build(list(dates), list(navs), capital)
        if path is not None:
            _write_regimes(path, key, rep)
        return rep
    except (sqlite3.Error, OSError):
        return None


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

    rf_rows = stats.risk_free_rows()
    usable = _usable(navs, capital)
    res, flat = None, True
    if usable:
        _, x = _series(dates, navs, capital, rf_rows)
        flat = stats.sharpe_per_period(x) is None
        res = stats.deflated_sharpe(x, [_trial_sharpe(t, rf_rows) for t in tries], max(n, 1))

    texts: dict[str, str] = {}
    if not usable:
        texts["dsr"] = texts["psr"] = _NO_SERIES
    elif flat:
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
    boot = _bootstrap(run_id, row, dates, navs, use_cache, rf_rows)
    if boot is not None:
        texts["bootstrap_sharpe"] = plain.interval_sentence("Sharpe", boot.sharpe, "", ci=boot.ci)
        texts["bootstrap_mean_return"] = plain.interval_sentence("Mean daily return", boot.mean_daily_return, "%",
                                                                 ci=boot.ci)
        texts["bootstrap_max_drawdown"] = plain.interval_sentence("Max drawdown", boot.max_drawdown, "%", ci=boot.ci)
    reg = _regimes(run_id, dates, navs, capital, use_cache)
    if reg is not None:
        texts["regimes_caveat"] = plain.REGIME_CAVEAT
        for dim in ("month", "weekday", "expiry", "vix", "gap"):
            texts[f"regime_{dim}"] = plain.meaning(f"regime_{dim}")
    texts["trials"] = plain.meaning("trials")
    texts["dsr_meaning"] = plain.meaning("dsr")           # `dsr` itself is this run's sentence
    texts["holdout_unlocks"] = plain.meaning("holdout_unlocks")
    return RunResearch(run_id=run_id, family_id=family_id, role=role, objective=objective, n_trials=n,
                       rank=rank, is_best=rank == 1, picked_run_id=picked.run_id if picked else None,
                       picked_name=picked.name if picked else None, dsr=res if is_try and not flat else None,
                       bootstrap=boot, regimes=reg, holdout_unlocks=_unlock_count(family_id), plain=texts)


# ── the family report ─────────────────────────────────────────────────────────────────────────

def _row(row: dict, objective: str, rf_rows: list[dict]) -> TrialRow:
    series = store.run_nav_series(row["run_id"])
    capital = _int_or_none(_config(row).get("capital_paise")) or 0
    _, x = _series([d for d, _ in series], [v for _, v in series], capital, rf_rows)
    return TrialRow(run_id=row["run_id"], name=row.get("name"), role=row.get("role") or "trial",
                    status=row.get("status") or "", objective_value=registry.objective_value(row, objective),
                    sharpe_per_period=stats.sharpe_per_period(x), net_pnl_paise=_net_pnl(row), days=len(series),
                    job_id=row.get("job_id"))


def _family_stats(tries: list[registry.TrialRun], best: registry.TrialRun | None,
                  rf_rows: list[dict]) -> tuple[float, DsrResult | None]:
    """(V, the best trial's DSR). V comes from every trial's own Sharpe, whether or not the best trial has one;
    the DSR is None only when the best trial itself has no Sharpe."""
    srs = [_trial_sharpe(t, rf_rows) for t in tries]
    v = stats.sharpe_variance(srs)
    if best is None:
        return v, None
    _, x = _series(best.dates, best.navs, best.capital_paise, rf_rows)
    return v, stats.deflated_sharpe(x, srs, len(tries))


def _family_sessions(family_id: str, sessions: list[dict]) -> list[dict]:
    return [{"session_id": s["session_id"], "name": s.get("name"), "status": s.get("status"),
             "created_ns": s.get("created_ns")} for s in sessions if s.get("family_id") == family_id]


def family_report(family_id: str) -> FamilyReport:
    fam = store.get_family(family_id)
    if fam is None:
        raise ValueError(f"no such family: {family_id!r}")
    rf_rows = stats.risk_free_rows()
    objective = registry.family_objective(family_id)
    tries = registry.trials(family_id)
    best = registry.best_trial(family_id)
    var, dsr = _family_stats(tries, best, rf_rows)
    counted = {t.run_id for t in tries}
    rows = store.family_runs(family_id, roles=None, status=None)
    texts: dict[str, str] = {}
    if best is not None:
        texts["dsr"] = plain.dsr_sentence(len(tries), 1, dsr.dsr if dsr else None, objective=objective,
                                          flat=dsr is None)
    texts["objective"] = plain.OBJECTIVE_LABELS[objective]
    texts["trials"] = plain.meaning("trials")
    texts["scores_caveat"] = plain.SCORES_CAVEAT
    texts["dsr_meaning"] = plain.meaning("dsr")           # `dsr` itself is the best try's sentence
    texts["holdout_unlocks"] = plain.meaning("holdout_unlocks")
    outcome = pbo.family_pbo(family_id, rf_rows)
    overfit = outcome.result
    if overfit is not None:
        texts["pbo"] = plain.pbo_sentence(overfit.pbo, overfit.n_trials, outcome.n_total)
    return FamilyReport(family_id=family_id, description=fam.get("description"), objective=objective,
                        named=bool(fam.get("named")), n_trials=len(tries),
                        var_sharpe=var if tries else None, best_run_id=best.run_id if best else None,
                        best=dsr, trials=[_row(r, objective, rf_rows) for r in rows if r["run_id"] in counted],
                        others=[_row(r, objective, rf_rows) for r in rows if r["run_id"] not in counted],
                        sessions=_family_sessions(family_id, store.list_sessions()),
                        holdout_unlocks=store.holdout_unlock_rows(family_id), plain=texts,
                        pbo=overfit, pbo_note=outcome.note, pbo_dropped=outcome.dropped,
                        pbo_flat=outcome.flat)


def list_families() -> list[FamilySummary]:
    sessions = store.list_sessions()
    rf_rows = stats.risk_free_rows()
    out = []
    for fam in store.list_families():
        fid = fam["family_id"]
        tries = registry.trials(fid)
        best = registry.best_trial(fid)
        _, dsr = _family_stats(tries, best, rf_rows)
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
    if "pbo" in rep.plain:
        lines.append(f"overfitting check: {rep.plain['pbo']}")
    elif rep.pbo_note:
        lines.append(f"overfitting check not made: {rep.pbo_note}")
    for t in rep.trials:
        lines.append(f"  {t.run_id:<24} {t.name or '-':<20} Sharpe {_annual(t.sharpe_per_period):>6}  "
                     f"net {_paise_text(t.net_pnl_paise):>12}  {t.days} days")
    return "\n".join(lines)

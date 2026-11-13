"""Probability of backtest overfitting by combinatorially symmetric cross-validation (M6.11, LF 6.1).

Bailey, Borwein, Lopez de Prado and Zhu (2015). Given a family's trial matrix `x` (T days by N tries, excess daily
returns), cut the days into S equal consecutive blocks and, for each of the C(S, S/2) ways to pick half of them as
the in-sample set, find the try that is best in sample and see where it ranks out of sample. The PBO is the share of
splits where it ranks at or below the median (logit of its relative rank <= 0).

    trial_matrix(family_id)   # TrialMatrix: the tries over the days they all share
    cscv(x, slices=16)        # PboResult; no randomness, so the same matrix always gives the same answer

Differences from the paper, on purpose: performance is the per-period Sharpe (ddof 1) from per-block sums; a union on
which a try is flat scores 0.0 (never NaN); the rank of a tie is its midrank; the in-sample best on a tie is the
lowest column; rows left over after S equal blocks are dropped from the end and counted.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

import numpy as np

from stockify.research import registry, settings, stats

_CHUNK = 1024                       # combinations per numpy gather: bounds the working set to a few MB


@dataclass(frozen=True)
class TrialMatrix:
    run_ids: list[str]              # the kept tries, one per column of x, in registry order
    dates: list[str]                # the days every kept try shares, sorted, one per row of x
    x: np.ndarray                   # T x N excess daily returns
    dropped: list[str]              # tries with no day in common with the rest (or no usable series)
    flat: list[str]                 # tries flat over those days (Sharpe undefined), left out of x


@dataclass(frozen=True)
class PboResult:
    pbo: float                      # share of combinations whose in-sample best ranks at or below the median
    n_trials: int
    n_days: int                     # rows actually used (T minus rows_dropped)
    slices: int
    combinations: int
    rows_dropped: int
    median_logit: float
    prob_loss: float                # share of combinations where the in-sample best lost out of sample


def _excess(t: registry.TrialRun, rf_rows: list[dict]) -> dict[str, float] | None:
    if not t.navs or t.capital_paise <= 0:
        return None
    r = stats.returns_from_nav(t.capital_paise, list(t.navs))
    x = stats.excess_returns(list(t.dates), r, rf_rows)
    return dict(zip(t.dates, x))


def trial_matrix(family_id: str, rf_rows: list[dict] | None = None) -> TrialMatrix:
    """The family's tries on the days they all share. A try that shares no day with the tries already kept is
    dropped (longest series first, then run id, so the choice never depends on anything else); a kept try that is
    flat on the shared days is listed in `flat` and left out of `x`."""
    if rf_rows is None:
        rf_rows = stats.risk_free_rows()
    tries = registry.trials(family_id)
    series = {t.run_id: _excess(t, rf_rows) for t in tries}
    dropped = [t.run_id for t in tries if series[t.run_id] is None]
    order = sorted((t for t in tries if series[t.run_id] is not None),
                   key=lambda t: (-len(series[t.run_id]), t.run_id))
    common: set[str] | None = None
    kept: set[str] = set()
    for t in order:
        days = set(series[t.run_id])
        nxt = days if common is None else common & days
        if nxt:
            common = nxt
            kept.add(t.run_id)
        else:
            dropped.append(t.run_id)
    dates = sorted(common or ())
    run_ids, flat, cols = [], [], []
    for t in tries:
        if t.run_id not in kept:
            continue
        col = [series[t.run_id][d] for d in dates]
        if stats.sharpe_per_period(col) is None:
            flat.append(t.run_id)
        else:
            run_ids.append(t.run_id)
            cols.append(col)
    x = np.array(cols, dtype=float).T if cols else np.empty((len(dates), 0))
    return TrialMatrix(run_ids, dates, x, sorted(dropped), flat)


def _sharpe_from_sums(n, s, ss, lo, hi) -> np.ndarray:
    """Per-period Sharpe, ddof 1, of unions given their count, sum, sum of squares, min and max (C x N); 0.0 where
    the union is flat, found exactly from min and max rather than from a rounded variance."""
    mean = s / n
    var = np.maximum((ss - n * mean * mean) / (n - 1), 0.0)
    sd = np.sqrt(var)
    flat = (lo == hi) | (sd == 0.0)
    return np.where(flat, 0.0, mean / np.where(flat, 1.0, sd))


def cscv(x: np.ndarray, slices: int = 16) -> PboResult:
    """The PBO of the T x N matrix `x`. ValueError for an odd or < 4 `slices`, fewer tries than
    `research.settings()["pbo"]["min_trials"]`, or T < 2 * slices."""
    if isinstance(slices, bool) or not isinstance(slices, int) or slices < 4 or slices % 2:
        raise ValueError(f"slices must be an even number of at least 4, not {slices!r}")
    x = np.asarray(x, dtype=float)
    if x.ndim != 2:
        raise ValueError("x must be a days-by-tries matrix")
    t, n = x.shape
    min_trials = int(settings()["pbo"]["min_trials"])
    if n < min_trials:
        raise ValueError(f"need at least {min_trials} tries, got {n}")
    if t < 2 * slices:
        raise ValueError(f"need at least {2 * slices} days for {slices} slices, got {t}")
    if not np.isfinite(x).all():
        raise ValueError("x holds a value that is not finite")
    size = t // slices
    used = size * slices
    blocks = x[:used].reshape(slices, size, n)
    b_s, b_ss = blocks.sum(axis=1), (blocks * blocks).sum(axis=1)       # S x N
    b_lo, b_hi = blocks.min(axis=1), blocks.max(axis=1)
    half = slices // 2
    combos = np.array(list(itertools.combinations(range(slices), half)), dtype=np.intp)   # C x half, lexicographic
    chosen = np.zeros((len(combos), slices), dtype=bool)
    np.put_along_axis(chosen, combos, True, axis=1)
    comp = np.nonzero(~chosen)[1].reshape(len(combos), half)           # the rest, ascending, per combination
    n_in = size * half
    lam_parts, loss = [], 0
    for a in range(0, len(combos), _CHUNK):
        ci, co = combos[a:a + _CHUNK], comp[a:a + _CHUNK]
        perf = []
        for idx in (ci, co):
            perf.append(_sharpe_from_sums(n_in, b_s[idx].sum(axis=1), b_ss[idx].sum(axis=1),
                                          b_lo[idx].min(axis=1), b_hi[idx].max(axis=1)))
        ins, outs = perf
        star = np.argmax(ins, axis=1)                                   # first maximum: ties go to the lowest column
        v = outs[np.arange(len(star)), star]
        less = (outs < v[:, None]).sum(axis=1)
        equal = (outs == v[:, None]).sum(axis=1)                        # includes the try itself
        omega = (less + (equal + 1) / 2) / (n + 1)
        lam_parts.append(np.log(omega / (1 - omega)))
        loss += int((v < 0).sum())
    lam = np.concatenate(lam_parts)
    count = len(combos)
    return PboResult(pbo=float((lam <= 0).sum()) / count, n_trials=n, n_days=used, slices=slices,
                     combinations=count, rows_dropped=t - used, median_logit=float(np.median(lam)),
                     prob_loss=loss / count)


def family_pbo(family_id: str, rf_rows: list[dict] | None = None) -> tuple[PboResult | None, str | None]:
    """(result, None) or (None, why not, in plain words) for a family. A walk-forward family is recognised by its
    id: its windows cover different days, so its tries cannot be compared day by day."""
    cfg = settings()["pbo"]
    if family_id.startswith("walkforward/"):
        return None, "the windows cover different days, so the tries cannot be compared day by day"
    m = trial_matrix(family_id, rf_rows)
    total = len(m.run_ids) + len(m.flat) + len(m.dropped)
    need = int(cfg["min_trials"])
    if total < need:
        return None, f"fewer than {need} tries"
    if len(m.run_ids) < need:
        if m.dropped and len(m.run_ids) + len(m.flat) < need:
            return None, "the tries cover different days, so they cannot be compared day by day"
        return None, f"fewer than {need} tries that traded on the shared days"
    try:
        return cscv(m.x, int(cfg["slices"])), None
    except ValueError:
        return None, "too few common days"

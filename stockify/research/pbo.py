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
from dataclasses import dataclass, field

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
    reasons: dict[str, str] = field(default_factory=dict)   # why each dropped try was dropped


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


def _key(t: registry.TrialRun) -> tuple[str, str]:
    return (t.name or "", t.run_id)


def trial_matrix(family_id: str, rf_rows: list[dict] | None = None, *, slices: int | None = None,
                 min_trials: int | None = None) -> TrialMatrix:
    """The family's tries on the days they all share. Tries with no usable series are dropped. While the shared
    days number fewer than 2 * slices and more than `min_trials` tries remain, the one try whose removal adds the
    most shared days is dropped (ties: the lowest name, then run id), so one odd try cannot cut everyone's days
    and the result never depends on the order the tries came in. A kept try that is flat on the shared days is
    listed in `flat` and left out of `x`. Columns of `x` are ordered by (name, run id)."""
    cfg = settings()["pbo"]
    slices = int(cfg["slices"]) if slices is None else slices
    min_trials = int(cfg["min_trials"]) if min_trials is None else min_trials
    if rf_rows is None:
        rf_rows = stats.risk_free_rows()
    tries = sorted(registry.trials(family_id), key=_key)
    series = {t.run_id: _excess(t, rf_rows) for t in tries}
    reasons = {t.run_id: "no usable daily returns" for t in tries if series[t.run_id] is None}
    active = [t for t in tries if series[t.run_id] is not None]
    need_days = 2 * slices

    def shared(group) -> set[str]:
        days = None
        for t in group:
            days = set(series[t.run_id]) if days is None else days & set(series[t.run_id])
        return days or set()

    common = shared(active)
    while len(common) < need_days and len(active) > min_trials:
        count: dict[str, int] = {}
        for t in active:
            for d in series[t.run_id]:
                count[d] = count.get(d, 0) + 1
        n = len(active)
        near = [d for d, c in count.items() if c == n - 1]
        gains = [(sum(1 for d in near if d not in series[t.run_id]), i) for i, t in enumerate(active)]
        gain, i = max(gains, key=lambda g: (g[0], -g[1]))              # the largest gain, then the lowest key
        if gain == 0:
            break                                                      # dropping any one try adds no day
        reasons[active[i].run_id] = "too few days in common with the others"
        del active[i]
        common = shared(active)
    dates = sorted(common)
    run_ids, flat, cols = [], [], []
    for t in active:
        col = [series[t.run_id][d] for d in dates]
        if stats.sharpe_per_period(col) is None:
            flat.append(t.run_id)
        else:
            run_ids.append(t.run_id)
            cols.append(col)
    x = np.array(cols, dtype=float).T if cols else np.empty((len(dates), 0))
    return TrialMatrix(run_ids, dates, x, sorted(reasons), flat, reasons)


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


@dataclass(frozen=True)
class PboOutcome:
    result: PboResult | None
    note: str | None                # why there is no result, in plain words (or, with one, what was left out)
    n_used: int                     # tries in the matrix
    n_total: int                    # every counted try of the family
    dropped: list[str]              # names (run id when unnamed) of the tries left out for want of shared days
    flat: list[str]                 # names of the tries left out for being flat on the shared days


def family_pbo(family_id: str, rf_rows: list[dict] | None = None) -> PboOutcome:
    """The PBO of a family, or why there is none, in plain words. A walk-forward family is recognised by its id:
    its windows cover different days, so its tries cannot be compared day by day. Only the two refusals that are
    plain conditions (too few tries, too few common days) become notes; any other error propagates."""
    cfg = settings()["pbo"]
    slices, need = int(cfg["slices"]), int(cfg["min_trials"])
    if family_id.startswith("walkforward/"):
        return PboOutcome(None, "the windows cover different days, so the tries cannot be compared day by day",
                          0, 0, [], [])
    m = trial_matrix(family_id, rf_rows, slices=slices, min_trials=need)
    names = {t.run_id: t.name or t.run_id for t in registry.trials(family_id)}
    total = len(m.run_ids) + len(m.flat) + len(m.dropped)
    dropped, flat = [names[r] for r in m.dropped], [names[r] for r in m.flat]
    used = len(m.run_ids)

    def out(result, note):
        return PboOutcome(result, note, used, total, dropped, flat)

    if total < need:
        return out(None, f"fewer than {need} tries")
    if used < need:
        if m.dropped and used + len(m.flat) < need:
            return out(None, "the tries cover different days, so they cannot be compared day by day")
        return out(None, f"fewer than {need} tries that traded on the shared days")
    if len(m.dates) < 2 * slices:
        return out(None, "too few common days")
    result = cscv(m.x, slices)
    note = None
    if used < total:
        left = [f"{n} (too few days in common with the others)" for n in dropped] + [f"{n} (flat)" for n in flat]
        note = f"based on {used} of {total} tries; left out: " + ", ".join(left)
    return out(result, note)

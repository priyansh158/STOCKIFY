"""Stationary block bootstrap (Politis and Romano) of daily returns: seeded 90% intervals for the annualised Sharpe,
the mean daily return and the max drawdown. Plain loops, stdlib only; every undefined value is None.

Units: r and x are per-day decimal returns; the Sharpe is annualised only through stats.annualised; the drawdown is a
fraction of the peak in [0, 1]."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from stockify.oracle.metrics import max_drawdown
from stockify.oracle.rng import Pcg32
from stockify.research import seeding, stats


def percentile(sorted_values: list[float], q: float) -> float:
    """Linear interpolation between ranks (rank = q * (n - 1)); q in [0, 1]; the input must be sorted."""
    if not sorted_values:
        raise ValueError("percentile of an empty list")
    if not 0.0 <= q <= 1.0:
        raise ValueError("q must be in [0, 1]")
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def stationary_indices(n: int, mean_block: float, rng: Pcg32) -> list[int]:
    """n indices in [0, n). The first is uniform (rng.below, unbiased); at each later step the run restarts at a new
    uniform index when next_u32() < round(2**32 / mean_block), else it takes the next index, wrapping circularly.
    mean_block must be >= 1 (1 restarts every step: iid resampling)."""
    if n < 1:
        raise ValueError("n must be >= 1")
    if not mean_block >= 1.0:
        raise ValueError("mean_block must be >= 1")
    threshold = round((1 << 32) / mean_block)
    out = [rng.below(n)]
    cur = out[0]
    for _ in range(n - 1):
        if rng.next_u32() < threshold:
            cur = rng.below(n)
        else:
            cur = cur + 1 if cur + 1 < n else 0
        out.append(cur)
    return out


@dataclass(frozen=True)
class Interval:
    point: float | None
    lo: float | None
    hi: float | None


@dataclass(frozen=True)
class BootstrapResult:
    n_days: int
    mean_block: float
    resamples: int
    ci: float
    seed: int
    sharpe: Interval
    mean_daily_return: Interval
    max_drawdown: Interval
    enough_days: bool


_NONE = Interval(None, None, None)


def _mean(v: list[float]) -> float:
    return sum(v) / len(v)


def _max_dd(r: list[float]) -> float:
    nav, navs = 1.0, [1.0]
    for v in r:
        nav *= 1.0 + v
        navs.append(nav)
    return max_drawdown(navs)[0]


def _sharpe_annual(x: list[float]) -> float | None:
    s = stats.sharpe_per_period(x)
    return None if s is None else stats.annualised(s)


def _interval(point: float | None, values: list[float], ci: float) -> Interval:
    if not values:
        return Interval(point, None, None)
    values.sort()
    tail = (1.0 - ci) / 2.0
    return Interval(point, percentile(values, tail), percentile(values, 1.0 - tail))


def bootstrap_ci(r: list[float], x: list[float], *, mean_block: float, resamples: int, ci: float, min_days: int,
                 seed: int, last_date: date) -> BootstrapResult:
    """r: plain daily returns; x: the matching excess returns. The generator is seeding.stream(seed, "bootstrap",
    last_date); each resample draws one index list applied to both series. A resample whose Sharpe is undefined
    is dropped from the Sharpe interval only."""
    if len(r) != len(x):
        raise ValueError("r and x must have the same length")
    n = len(r)
    if n < min_days or n < 1:
        return BootstrapResult(n, mean_block, resamples, ci, seed, _NONE, _NONE, _NONE, False)
    rng = seeding.stream(seed, "bootstrap", last_date)
    sh: list[float] = []
    mn: list[float] = []
    dd: list[float] = []
    for _ in range(resamples):
        idx = stationary_indices(n, mean_block, rng)
        rr = [r[i] for i in idx]
        s = _sharpe_annual([x[i] for i in idx])
        if s is not None:
            sh.append(s)
        mn.append(_mean(rr))
        dd.append(_max_dd(rr))
    return BootstrapResult(
        n, mean_block, resamples, ci, seed,
        _interval(_sharpe_annual(x), sh, ci) if sh else _NONE,
        _interval(_mean(r), mn, ci),
        _interval(_max_dd(r), dd, ci),
        True)

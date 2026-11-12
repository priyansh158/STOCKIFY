"""Sharpe, probabilistic Sharpe (PSR) and deflated Sharpe (DSR) from the stdlib (M6.4, IL 6.1).

Pure functions, no I/O. Wherever a number must equal `metrics.json` (A4 section 8) the sums are plain
left-to-right loops (no `sum()`, no numpy reductions). Every undefined statistic is `None`, never NaN or inf.

The Sharpe here is per period (per day) until `annualised`; the PSR and DSR work in the per-period unit
(Bailey and Lopez de Prado), so `sr`, `sr0` and `var_sharpe` are all per-period.

Carry-forward for callers (the research report, Task 7): a family's Sharpes, and their variance V, are computed
from each trial's `navs` through `returns_from_nav` -> `excess_returns` -> `sharpe_per_period`, never from a
registry `objective_value`, which can be net P&L in paise rather than a Sharpe.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist

from stockify import numerics
from stockify.config import loader
from stockify.oracle import metrics

EULER_GAMMA = 0.5772156649015329
_PHI = NormalDist()


@dataclass(frozen=True)
class Moments:
    n: int
    mean: float
    sd: float                 # sample standard deviation (ddof 1); 0.0 for n < 2
    skew: float | None        # m3 / m2**1.5, population moments; None when m2 == 0
    kurt: float | None        # m4 / m2**2 (NOT excess: a normal series gives 3); None when m2 == 0


def moments(x: list[float]) -> Moments:
    n = len(x)
    if n == 0:
        return Moments(0, 0.0, 0.0, None, None)
    s = 0.0
    for v in x:
        s += v
    mean = s / n
    ss = m3s = m4s = 0.0
    for v in x:
        dv = v - mean
        ss += dv ** 2
        m3s += dv ** 3
        m4s += dv ** 4
    sd = math.sqrt(ss / (n - 1)) if n >= 2 else 0.0
    if n >= 2 and min(x) == max(x):       # all equal: the deviation is exactly 0, not rounding noise
        return Moments(n, mean, 0.0, None, None)
    m2, m3, m4 = ss / n, m3s / n, m4s / n
    if m2 == 0:
        return Moments(n, mean, sd, None, None)
    return Moments(n, mean, sd, m3 / m2 ** 1.5, m4 / m2 ** 2)


def risk_free_rows() -> list[dict]:
    """The dated risk-free rows as `metrics.rate_on` reads them: each decimal-string `rate` gets its exact
    `rate_ppb` (what `bundle._ppb_fields` adds for the engine), so a report sees the rate a run was judged by."""
    rows = []
    for r in loader.load_json("pricing.json")["risk_free"]:
        r = dict(r)
        if isinstance(r.get("rate"), str):
            r["rate_ppb"] = numerics.to_ppb(r["rate"])
        rows.append(r)
    return rows


def returns_from_nav(capital: int, navs: list[int]) -> list[float]:
    """r_d = NAV_d / NAV_{d-1} - 1 with NAV_0 = capital. Paise become float here, once."""
    out = []
    prev = float(capital)
    for v in navs:
        cur = float(v)
        out.append(cur / prev - 1)
        prev = cur
    return out


def excess_returns(dates: list[str], r: list[float], rf_rows: list[dict]) -> list[float]:
    """x_d = r_d - rate_on(rf_rows, dates[d]) / 252 (a per-day decimal), as in `metrics.compute`."""
    return [r[d] - metrics.rate_on(rf_rows, dates[d]) / 252 for d in range(len(r))]


def sharpe_per_period(x: list[float]) -> float | None:
    """mean / sd. None when n < 2 or all values are equal (sd exactly 0, as in metrics.json)."""
    if len(x) < 2 or min(x) == max(x):
        return None
    m = moments(x)
    if m.sd == 0:
        return None
    return m.mean / m.sd


def annualised(sr: float) -> float:
    return sr * math.sqrt(252)


def expected_max_sharpe(n_trials: int, var_sharpe: float) -> float:
    """SR0: the expected maximum Sharpe of N independent trials with Sharpe variance V (per-period)."""
    if n_trials <= 1 or var_sharpe <= 0:
        return 0.0
    n = float(n_trials)
    return math.sqrt(var_sharpe) * ((1 - EULER_GAMMA) * _PHI.inv_cdf(1 - 1 / n)
                                    + EULER_GAMMA * _PHI.inv_cdf(1 - 1 / (n * math.e)))


def probabilistic_sharpe(sr: float, n: int, skew: float, kurt: float, benchmark: float = 0.0) -> float | None:
    """P(true Sharpe > benchmark); `kurt` is non-excess. None when n < 2 or the radicand is <= 0."""
    if n < 2:
        return None
    rad = 1 - skew * sr + ((kurt - 1) / 4) * sr ** 2
    if rad <= 0:
        return None
    return _PHI.cdf((sr - benchmark) * math.sqrt(n - 1) / math.sqrt(rad))


@dataclass(frozen=True)
class DsrResult:
    sr: float
    t: int
    skew: float
    kurt: float
    n_trials: int
    var_sharpe: float
    sr0: float
    dsr: float | None
    psr: float | None


def deflated_sharpe(x: list[float], trial_srs: list[float | None], n_trials: int) -> DsrResult | None:
    """`x`: the selected run's excess returns; `trial_srs`: the family's per-period Sharpes (a flat trial is
    None: it counts in `n_trials` but not in V). V is the sample variance (ddof 1) of the defined ones."""
    sr = sharpe_per_period(x)
    if sr is None:
        return None
    mom = moments(x)
    if mom.skew is None or mom.kurt is None:
        return None
    defined = [s for s in trial_srs if s is not None]
    var = 0.0
    if len(defined) >= 2:
        s = 0.0
        for v in defined:
            s += v
        mean = s / len(defined)
        ss = 0.0
        for v in defined:
            ss += (v - mean) ** 2
        var = ss / (len(defined) - 1)
    sr0 = expected_max_sharpe(n_trials, var)
    t = len(x)
    return DsrResult(sr=sr, t=t, skew=mom.skew, kurt=mom.kurt, n_trials=n_trials, var_sharpe=var, sr0=sr0,
                     dsr=probabilistic_sharpe(sr, t, mom.skew, mom.kurt, sr0),
                     psr=probabilistic_sharpe(sr, t, mom.skew, mom.kurt, 0.0))

"""Black-76: European options on a forward or futures price (M4.3, C-13).

The oracle's readable version; engine/src/pricing/black76.cpp is the independent C++ one (C-10). Prices are in
rupees as floats (callers convert paise at the edge); `t` is in years, ACT/365; `r` is a decimal rate.

    D  = exp(-r·t)
    d1 = (ln(F/K) + σ²·t/2) / (σ·√t),   d2 = d1 − σ·√t
    call = D·(F·N(d1) − K·N(d2)),       put = D·(K·N(−d2) − F·N(−d1))

N is the standard normal CDF, computed as erfc(−x/√2)/2 so that the far tails keep their precision.

Implied volatility checks the no-arbitrage bounds first and returns NaN with a reason instead of clamping:
BELOW_INTRINSIC, ABOVE_FORWARD (a call above D·F), ABOVE_STRIKE (a put above D·K), BELOW_TICK (under one
tick), ABOVE_MAX_VOL (inside the bounds but needing σ > 5.0; D-56), LOW_VEGA (the price does not pin σ down).
Otherwise Brent's method on σ ∈ [0.001, 5.0] until the model price is within 1e-10 of the target, falling back to
bisection if Brent does not converge.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

SIGMA_MIN = 0.001
SIGMA_MAX = 5.0
PRICE_TOL = 1e-10          # absolute, in rupees (A3 §3)
MAX_ITER = 200
NS_PER_YEAR = 365 * 86_400 * 1_000_000_000
MIN_T_NS = 1_000_000_000   # time to expiry is floored at one second
DEFAULT_TICK = 0.05        # ₹0.05, the NSE option tick

# IV reasons (returned beside NaN).
BELOW_INTRINSIC = "BELOW_INTRINSIC"
ABOVE_FORWARD = "ABOVE_FORWARD"
ABOVE_STRIKE = "ABOVE_STRIKE"
BELOW_TICK = "BELOW_TICK"
ABOVE_MAX_VOL = "ABOVE_MAX_VOL"
LOW_VEGA = "LOW_VEGA"

_SQRT2 = math.sqrt(2.0)
_INV_SQRT_2PI = 1.0 / math.sqrt(2.0 * math.pi)


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / _SQRT2)


def norm_pdf(x: float) -> float:
    return _INV_SQRT_2PI * math.exp(-0.5 * x * x)


@dataclass(frozen=True)
class Greeks:
    """Price and sensitivities. Delta and gamma are with respect to F; vega per 1.00 of volatility (divide by
    100 for a vol point); theta per year (divide by 365 for a calendar day); rho per 1.00 of rate."""
    price: float
    delta: float
    gamma: float
    vega: float
    theta: float
    rho: float


def _check(f: float, k: float, t: float, sigma: float) -> None:
    if not (f > 0 and k > 0 and t > 0 and sigma > 0) or not all(map(math.isfinite, (f, k, t, sigma))):
        raise ValueError(f"Black-76 needs F, K, t, sigma > 0 and finite (F={f}, K={k}, t={t}, sigma={sigma})")


def _d1_d2(f: float, k: float, t: float, sigma: float) -> tuple[float, float]:
    vol_sqrt_t = sigma * math.sqrt(t)
    d1 = (math.log(f / k) + 0.5 * sigma * sigma * t) / vol_sqrt_t
    return d1, d1 - vol_sqrt_t


def price(is_call: bool, f: float, k: float, t: float, r: float, sigma: float) -> float:
    _check(f, k, t, sigma)
    disc = math.exp(-r * t)
    d1, d2 = _d1_d2(f, k, t, sigma)
    if is_call:
        return disc * (f * norm_cdf(d1) - k * norm_cdf(d2))
    return disc * (k * norm_cdf(-d2) - f * norm_cdf(-d1))


def greeks(is_call: bool, f: float, k: float, t: float, r: float, sigma: float) -> Greeks:
    _check(f, k, t, sigma)
    disc = math.exp(-r * t)
    sqrt_t = math.sqrt(t)
    d1, d2 = _d1_d2(f, k, t, sigma)
    pdf1 = norm_pdf(d1)
    if is_call:
        value = disc * (f * norm_cdf(d1) - k * norm_cdf(d2))
        delta = disc * norm_cdf(d1)
    else:
        value = disc * (k * norm_cdf(-d2) - f * norm_cdf(-d1))
        delta = -disc * norm_cdf(-d1)
    gamma = disc * pdf1 / (f * sigma * sqrt_t)
    vega = disc * f * pdf1 * sqrt_t
    theta = -disc * f * pdf1 * sigma / (2.0 * sqrt_t) + r * value
    rho = -t * value        # only the discount factor depends on r when F is given
    return Greeks(value, delta, gamma, vega, theta, rho)


def _check_int64(v: int) -> int:
    if not -(2**63) <= v < 2**63:
        raise OverflowError(f"{v} does not fit in int64")
    return v


def year_fraction(now_ns: int, last_trading_ns: int) -> float:
    """ACT/365 from now to the contract's last trading moment, floored at one second. Both integers are turned into
    floats first, as the engine does: Python's int / int rounds once, the engine's double / double twice, and the
    two differ for spans over 2^53 ns (about 104 days)."""
    return float(max(_check_int64(int(last_trading_ns) - int(now_ns)), MIN_T_NS)) / float(NS_PER_YEAR)


def rate_on(rows: list[dict], d: str) -> float:
    """The bundle's dated `pricing.risk_free` row in force on IST date `d` (rate_ppb → decimal); 0 if none."""
    best = None
    for row in rows:
        if row["effective_from"] <= d and (best is None or row["effective_from"] > best["effective_from"]):
            best = row
    return best["rate_ppb"] / 1_000_000_000 if best else 0.0


# ── Implied volatility ───────────────────────────────────────────────────────

def _brent(fn, a: float, b: float, fa: float, fb: float) -> float | None:  # noqa: ANN001
    """A root of fn in [a, b] (fa, fb of opposite signs) by Brent's method; None if it does not converge.

    Each step tries inverse quadratic interpolation (or the secant when two of the last three values agree) and
    takes it only if it lands between (3a + b)/4 and b and shrinks faster than bisection would; otherwise it
    bisects (Brent 1973, ch. 4). Stops when |fn(b)| ≤ PRICE_TOL."""
    if abs(fa) < abs(fb):
        a, b, fa, fb = b, a, fb, fa
    c, fc = a, fa
    d = c                  # the point before c; only read after a non-bisection step
    bisected = True        # the last step was a bisection
    delta = 1e-15          # smallest step worth taking in σ
    for _ in range(MAX_ITER):
        if abs(fb) <= PRICE_TOL:
            return b
        if fa != fc and fb != fc:
            s = (a * fb * fc / ((fa - fb) * (fa - fc))
                 + b * fa * fc / ((fb - fa) * (fb - fc))
                 + c * fa * fb / ((fc - fa) * (fc - fb)))
        else:
            s = b - fb * (b - a) / (fb - fa)
        lo, hi = min((3.0 * a + b) / 4.0, b), max((3.0 * a + b) / 4.0, b)
        if (not (lo < s < hi)
                or (bisected and abs(s - b) >= abs(b - c) / 2.0)
                or (not bisected and abs(s - b) >= abs(c - d) / 2.0)
                or (bisected and abs(b - c) < delta)
                or (not bisected and abs(c - d) < delta)):
            s = 0.5 * (a + b)
            bisected = True
        else:
            bisected = False
        fs = fn(s)
        d, c, fc = c, b, fb
        if (fa < 0) != (fs < 0):
            b, fb = s, fs
        else:
            a, fa = s, fs
        if abs(fa) < abs(fb):
            a, b, fa, fb = b, a, fb, fa
    return None


def _bisect(fn, a: float, b: float, fa: float) -> float:  # noqa: ANN001
    """Plain bisection until |fn| ≤ PRICE_TOL or the bracket stops shrinking (the bracket has a sign change)."""
    m = 0.5 * (a + b)
    for _ in range(2000):
        m = 0.5 * (a + b)
        fm = fn(m)
        if abs(fm) <= PRICE_TOL or m in (a, b):
            return m
        if (fa < 0) != (fm < 0):
            b = m
        else:
            a, fa = m, fm
    return m


def implied_vol(is_call: bool, target: float, f: float, k: float, t: float, r: float,
                tick: float = DEFAULT_TICK) -> tuple[float, str | None]:
    """(σ, None), or (NaN, reason). The reasons are checked in this order: the bounds, the tick, the bracket,
    then whether vega is large enough for σ to mean anything."""
    nan = float("nan")
    if not (f > 0 and k > 0 and t > 0) or not math.isfinite(target):
        raise ValueError(f"implied_vol needs F, K, t > 0 and a finite price (F={f}, K={k}, t={t}, price={target})")
    disc = math.exp(-r * t)
    intrinsic = disc * max(f - k, 0.0) if is_call else disc * max(k - f, 0.0)
    if target < intrinsic - PRICE_TOL:       # a model price can round a hair under intrinsic
        return nan, BELOW_INTRINSIC
    if is_call and target > disc * f:
        return nan, ABOVE_FORWARD
    if not is_call and target > disc * k:
        return nan, ABOVE_STRIKE
    if target < tick:
        return nan, BELOW_TICK

    def fn(sigma: float) -> float:
        return price(is_call, f, k, t, r, sigma) - target

    f_lo, f_hi = fn(SIGMA_MIN), fn(SIGMA_MAX)
    if abs(f_lo) <= PRICE_TOL:
        sigma = SIGMA_MIN
    elif abs(f_hi) <= PRICE_TOL:
        sigma = SIGMA_MAX
    elif f_lo > 0:
        return nan, LOW_VEGA          # even σ = 0.001 is too dear: no time value to read a volatility from
    elif f_hi < 0:
        return nan, ABOVE_MAX_VOL
    else:
        found = _brent(fn, SIGMA_MIN, SIGMA_MAX, f_lo, f_hi)
        sigma = found if found is not None else _bisect(fn, SIGMA_MIN, SIGMA_MAX, f_lo)
    # Half a tick of price error moving σ by more than 1.00 means the price does not identify σ.
    if greeks(is_call, f, k, t, r, sigma).vega * 1.0 < tick / 2.0:
        return nan, LOW_VEGA
    return sigma, None

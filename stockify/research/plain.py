"""Plain English for every statistic: one meaning per key and the sentence builders, written once.

The CLI, report.md and the web pages all ship these strings, so the owner reads the same words everywhere.
Rules for every string here: one line, no Greek letters, no code names, no statistician's vocabulary,
and true to the statistic (a plain but wrong sentence is worse than a jargon one).
Fixed phrases other code and tests rely on: "best of N tries" and "after allowing for luck".

interval_sentence accepts `label` (the brief's signature) but does not show it, so the sentence can sit beside a
table row that already names the statistic. Its unit "%" takes fractions (0.021 prints as 2.1%).

Later tasks add only their own builder: fragile_sentence (Task 16),
wf_sentence (Task 22). pbo_sentence is here (Task 23). Their meanings are already in KEYS.
"""
from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from stockify.research import registry

if TYPE_CHECKING:
    from stockify.research.regimes import Bucket

RUPEE = "₹"
TOO_FEW_DAYS = "too few days to say"
_NAME_LIMIT = 40

# Every line is at most 110 characters (tested).
_MEANINGS: dict[str, str] = {
    # run metrics
    "net_pnl": f"Money made or lost over the run, after all trading costs, in {RUPEE}.",
    "return": "Net profit as a share of the money the run started with.",
    "cagr": "Return restated as a yearly rate, as if it had compounded evenly.",
    "sharpe": "Return per unit of day-to-day swing, scaled to a year. Higher means more return for the swing taken.",
    "sortino": "Like Sharpe, but only losing days count as swing. Higher is better.",
    "max_drawdown": "The biggest fall from a high point to a later low, as a share of that high.",
    "calmar": "Yearly return divided by the biggest fall. Higher means more gain for the worst dip.",
    "trades": "How many round trips the run made: a buy and its sell, or a sell and its buy-back.",
    "win_rate": "Share of trades that made money after costs. It says nothing about how big the wins or losses were.",
    "costs": f"Everything paid to trade: brokerage, taxes and fees, in {RUPEE}.",
    "cost_share": "Costs as a share of the profit before costs. Near 100% means costs eat the edge.",
    # research
    "dsr": "Chance the real Sharpe beats what the luckiest of all your tries would show by chance alone.",
    "psr": "Chance the real Sharpe is above zero, judging this one run. It ignores how many tries you made.",
    "expected_best_luck": "The Sharpe the luckiest of your tries would show by chance alone, with no real edge.",
    "trials": "How many variations you tried on this idea. The more tries, the easier it is to get lucky.",
    "bootstrap_sharpe": "The range Sharpe takes when this run's own days are re-drawn in blocks. Not a forecast.",
    "bootstrap_mean_return": "The range of the average daily return when this run's own days are re-drawn. Not a forecast.",
    "bootstrap_max_drawdown": "The range of the biggest fall when this run's own days are re-drawn. Not a forecast.",
    "holdout_unlocks": "Times you looked at the recent days you had set aside. Each look makes them a less fair test.",
    # regimes
    "regime_month": "Results split by calendar month, to show whether the profit comes from only a few months.",
    "regime_weekday": "Results split by day of the week, to show whether the profit sits on one or two days.",
    "regime_expiry": "Results on expiry days compared with all other days.",
    "regime_vix": "Results split into four bands of the day before's India VIX (calm to jumpy), plus a no-data group.",
    "regime_gap": "Results split into five bands of how far the market opened from the previous close, plus no data.",
    # sensitivity
    "fragile": "Fragile means a small rise in costs or slightly worse fills turns the profit into a loss.",
    "cost_scale": "Re-runs with every trading cost multiplied (say 2x) to see whether the profit survives.",
    "extra_ticks": "Re-runs with every fill a few price ticks worse (a tick is the smallest price step).",
    "latency": "Re-runs with orders reaching the market later, to see whether the profit survives slower fills.",
    # walk-forward and PBO
    "wf_ratio": "Result on days the settings never saw, divided by the result on days they were fitted to.",
    "wf_stability": "How much the chosen settings change from one window to the next. Steady settings are safer.",
    "pbo": "Chance the best setting on past days lands in the worse half on fresh days. Lower is better.",
}

KEYS: tuple[str, ...] = tuple(_MEANINGS)

# What each registry objective is called in a sentence. A test checks this covers registry.OBJECTIVES.
OBJECTIVE_LABELS: dict[str, str] = {
    "sharpe": "steadiness of returns (Sharpe)",
    "sortino": "steadiness, counting only losing days (Sortino)",
    "calmar": "yearly return per worst fall (Calmar)",
    "cagr": "yearly growth rate",
    "net_pnl": "profit after costs",
}


def meaning(key: str) -> str:
    """The one-line plain meaning of a statistic. Raises KeyError for an unknown key."""
    return _MEANINGS[key]


def ordinal(n: int) -> str:
    """1st, 2nd, 3rd, 4th ... 11th, 12th, 13th ... 21st, 101st, 111th."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _usable(p: float | None) -> float | None:
    """None for a missing or non-finite probability; a value a hair outside [0, 1] is clamped into it."""
    if p is None or not math.isfinite(p):
        return None
    return min(1.0, max(0.0, float(p)))


def _percent_text(p: float) -> str:
    """Whole percent, but never a rounded-up 100% or rounded-down 0% for a value that is not exactly 1 or 0."""
    for places in (0, 1, 2, 3):
        d = Decimal(repr(p * 100)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        if p in (0.0, 1.0) or Decimal(0) < d < Decimal(100):
            return f"{_trim(d)}%"
    return f"{_trim(d)}%"


def _trim(d: Decimal) -> str:
    text = format(d, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _indian_group(whole: str) -> str:
    sign = "-" if whole.startswith("-") else ""
    digits = whole.lstrip("-")
    if len(digits) <= 3:
        return sign + digits
    head, tail = digits[:-3], digits[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return sign + ",".join(parts) + "," + tail


def rupees(value: float) -> str:
    """Rupees with Indian grouping, rounded first: two decimals (trimmed) below 1,000, whole rupees from there."""
    q = Decimal(repr(float(value))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if abs(q) >= 1000:
        body = _indian_group(str(q.copy_abs().quantize(Decimal(1), rounding=ROUND_HALF_UP)))
    else:
        body = _trim(q.copy_abs())
    return f"{'-' if q < 0 else ''}{RUPEE}{body}"


def _number(x: float) -> str:
    """Two significant figures below 1, two decimals from 1 up, never scientific notation."""
    d = Decimal(repr(float(x)))
    if d == 0:
        return "0"
    if abs(d) < 1:
        exp = d.adjusted()  # position of the leading digit
        return _trim(d.quantize(Decimal(1).scaleb(exp - 1), rounding=ROUND_HALF_UP))
    return _trim(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _with_unit(x: float, unit: str) -> str:
    if unit == RUPEE:
        return rupees(x)
    if unit == "%":
        return _number(x * 100) + "%"
    return _number(x) + (f" {unit}" if unit else "")


def _ci_phrase(ci: float) -> str:
    if not 0 < ci < 1:
        raise ValueError(f"ci must be between 0 and 1, not {ci!r}")
    for denominator in (10, 20):
        k = ci * denominator
        if math.isclose(k, round(k), abs_tol=1e-9) and 1 <= round(k) < denominator:
            return f"{round(k)} of {denominator}"
    return _percent_text(ci) + " of"


def dsr_sentence(n_trials: int, rank: int, dsr: float | None, *, objective: str = "sharpe",
                 picked_name: str | None = None, flat: bool = False) -> str:
    """Where this run stands among all the tries of its family, in one line.

    The deflated Sharpe is only meaningful for the best try, so a runner-up gets its rank instead.
    """
    if n_trials < 1 or not 1 <= rank <= n_trials:
        raise ValueError(f"rank must be between 1 and n_trials, got rank={rank}, n_trials={n_trials}")
    if flat:
        return "no trades, so no Sharpe to judge"
    if n_trials == 1:
        return "the only try, so nothing to allow for"
    try:
        label = OBJECTIVE_LABELS[objective]
    except KeyError:
        raise ValueError(f"objective must be one of {list(registry.OBJECTIVES)}, not {objective!r}") from None
    if rank == 1:
        p = _usable(dsr)
        verdict = TOO_FEW_DAYS if p is None else f"{_percent_text(p)} chance the edge beats luck"
        # the luck allowance is always computed on the picked run's Sharpe, whatever ranked the tries
        scope = "" if objective == "sharpe" else " on its Sharpe"
        return f"best of {n_trials} tries by {label}, after allowing for luck{scope}: {verdict}"
    head = f"{ordinal(rank)} best of {n_trials} tries by {label}"
    if picked_name:
        name = picked_name if len(picked_name) <= _NAME_LIMIT else picked_name[:_NAME_LIMIT - 3] + "..."
        return f"{head}; the job picked {name}, so this is a runner-up"
    return f"{head}, so this is a runner-up"


def psr_sentence(psr: float | None) -> str:
    """The probabilistic Sharpe: one run on its own, with no allowance for the number of tries."""
    p = _usable(psr)
    if p is None:
        return TOO_FEW_DAYS
    return f"{_percent_text(p)} confident the real Sharpe is above zero, judging this run alone"


def interval_sentence(label: str, interval, unit: str, *, ci: float = 0.90) -> str:
    """A bootstrap range: what the statistic did across re-draws (with repeats, in blocks) of this run's own days.

    `interval` has point/lo/hi (bootstrap.Interval). `unit` is "" for a bare number, "%" when the values are
    fractions to show as percentages, or the rupee sign for money; any other text is appended after the number.
    `label` names the statistic for the caller's table; the sentence itself stays label-free so it can sit
    beside it. `ci` is the interval's coverage (the bootstrap default is 0.90, so "9 of 10").
    """
    lo, hi = interval.lo, interval.hi
    if lo is None or hi is None or not math.isfinite(lo) or not math.isfinite(hi):
        return TOO_FEW_DAYS
    return (f"between {_with_unit(lo, unit)} and {_with_unit(hi, unit)} "
            f"in {_ci_phrase(ci)} re-draws of your own days")


REGIME_CAVEAT = ("These splits only describe this run's own days. A group can look better or worse by chance alone, "
                 "most of all a small one.")
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def regime_sentence(bucket: "Bucket") -> str:
    """One line about one bucket of the regime split. It only describes what this run's days did: it never says a
    bucket is better or worse than the others, and a bucket with fewer days than the configured minimum says so
    instead of quoting a share."""
    label = bucket.label
    head = label + "s" if label in _WEEKDAYS else label[:1].upper() + label[1:]
    n = bucket.days
    if n <= 0 or bucket.mean_pnl_paise is None:
        return f"{head}: no days"
    days = f"{n} day" + ("" if n == 1 else "s")
    value = bucket.mean_pnl_paise / 100
    average = ("+" if value > 0 else "") + rupees(value)
    if bucket.thin or bucket.up_share is None:
        return f"{head}: only {days}, take with care (average {average} a day)"
    return f"{head}: {days}, average {average} a day, up on {_percent_text(bucket.up_share)} of them"


def pbo_sentence(pbo: float | None, n_trials: int, n_total: int | None = None) -> str:
    """The chance of overfitting a family of tries, in one line: how often the try that looked best on half of the
    days landed at or below the middle on the other half, over every way of splitting the days in two. When some
    tries had to be left out, it says "based on N of M tries"."""
    p = _usable(pbo)
    if p is None:
        return TOO_FEW_DAYS
    tries = f"based on {n_trials} of {n_total} tries" if n_total and n_total > n_trials else f"{n_trials} tries"
    return (f"in {_percent_text(p)} of the ways to split your days in two, the try that looked best on one half "
            f"landed at or below the middle on the other ({tries})")

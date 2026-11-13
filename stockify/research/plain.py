"""Plain English for every statistic: one meaning per key and the sentence builders, written once.

The CLI, report.md and the web pages all ship these strings, so the owner reads the same words everywhere.
Rules for every string here: one line, no Greek letters, no code names, no statistician's vocabulary,
and true to the statistic (a plain but wrong sentence is worse than a jargon one).
Fixed phrases other code and tests rely on: "best of N tries" and "after allowing for luck".

interval_sentence accepts `label` (the brief's signature) but does not show it, so the sentence can sit beside a
table row that already names the statistic. Its unit "%" takes fractions (0.021 prints as 2.1%).

Later tasks add only their own builder: fragile_sentence (Task 16),
wf_sentence (Task 22, done). pbo_sentence is here (Task 23). Their meanings are already in KEYS.
"""
from __future__ import annotations

import math
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from stockify.research import registry

if TYPE_CHECKING:
    from stockify.research.regimes import Bucket

RUPEE = "₹"
TOO_FEW_DAYS = "too few days to say"
# Shown in place of a Sharpe figure, a ratio or a deflated Sharpe when the out-of-sample days are fewer than
# research.json walk_forward.min_oos_days (D-102): the figure would look precise and mean nothing.
NOT_ENOUGH_OOS_SHORT = "too few days"          # the same, for a table cell, with the sentence said once above the table
NOT_ENOUGH_OOS = "not enough out-of-sample days to judge ({n} of {m} needed)"
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


# Under the trial table: the scores are in-sample; only the picked try is deflated (a caveat, so not a one-line meaning).
SCORES_CAVEAT = "Scores are measured on the days the tries were fitted to. Only the picked try is adjusted for luck, in the sentence above."


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
                 picked_name: str | None = None, flat: bool = False, n_finished: int | None = None) -> str:
    """Where this run stands among all the tries of its family, in one line. Every setting tried counts, finished
    or not (D-85); when some did not finish (`n_finished` below `n_trials`) the line says "N tries (M finished)".

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
    tries = (f"{n_trials} tries ({n_finished} finished)" if n_finished is not None and n_finished < n_trials
             else f"{n_trials} tries")
    if rank == 1:
        p = _usable(dsr)
        verdict = TOO_FEW_DAYS if p is None else f"{_percent_text(p)} chance the edge beats luck"
        # the luck allowance is always computed on the picked run's Sharpe, whatever ranked the tries
        scope = "" if objective == "sharpe" else " on its Sharpe"
        partial = n_finished is not None and n_finished < n_trials
        note = "; the luck allowance uses only the finished tries' results" if partial else ""
        return f"best of {tries} by {label}, after allowing for luck{scope}: {verdict}{note}"
    head = f"{ordinal(rank)} best of {tries} by {label}"
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


# ---- walk-forward (M6.5, WF 6.6) ---------------------------------------------------------------------------------

def wf_ratio_sentence(summary) -> str:  # noqa: ANN001
    """What the days the settings never saw kept of the result on the days they were fitted to. The share is only
    quoted when the in-sample Sharpe is positive (the summary's ratio is None otherwise) and the out-of-sample one
    is not below zero: a ratio of a loss is not a share that was "kept"."""
    if summary.oos_days <= 0 or summary.oos_sharpe is None or not math.isfinite(summary.oos_sharpe):
        return "no out-of-sample days with a usable Sharpe, so there is no ratio"
    if summary.is_sharpe is None or not math.isfinite(summary.is_sharpe):
        return "no in-sample Sharpe to compare with, so there is no ratio"
    if summary.is_sharpe <= 0:
        return "in-sample was not positive, so there is no ratio"
    if summary.oos_sharpe < 0:
        return "the Sharpe out of sample was below zero, so no part of the in-sample Sharpe was kept"
    ratio = summary.ratio if summary.ratio is not None else summary.oos_sharpe / summary.is_sharpe
    return f"kept {_percent_text(ratio)} of the in-sample Sharpe out of sample"


def wf_stability_sentence(summary, *, chosen_windows: int | None = None) -> str:  # noqa: ANN001
    """How often the same settings won. `chosen_windows` is the number of windows that picked any settings; with it
    the line reads "N of M windows that picked one", without it the share is counted over every window and shown
    as a percentage when it is not a whole number of them."""
    share = summary.chosen_modal_share
    if share is None or not math.isfinite(share):
        return "no window picked settings, so there is no stability to report"
    if chosen_windows is None and summary.windows <= 1:
        return "only one window, so there is no stability to report"
    if chosen_windows is not None:
        n = chosen_windows
        if n <= 1:
            return "only one window picked settings, so there is no stability to report"
        return f"the same settings won {round(share * n)} of {n} windows that picked one"
    n = summary.windows
    won = share * n
    if math.isclose(won, round(won), abs_tol=1e-9):
        return f"the same settings won {round(won)} of {n} windows"
    return f"the same settings won {_percent_text(min(1.0, max(0.0, share)))} of the windows"


def wf_dsr_sentence(n_trials: int, dsr: float | None, *, oos_days: int | None = None,
                    min_oos_days: int | None = None) -> str:
    """The deflated Sharpe of the stitched out-of-sample days. They are not "the best of N": each window's settings
    were picked on training days and tried on days the pick never saw. The allowance for luck still counts every
    training try (N = windows x grid), so the figure is a cautious one, and the sentence says so."""
    if n_trials < 1:
        raise ValueError(f"n_trials must be at least 1, not {n_trials!r}")
    if n_trials == 1:
        return "only one try, so nothing to allow for"
    p = _usable(dsr)
    verdict = TOO_FEW_DAYS if p is None else f"{_percent_text(p)} chance the out-of-sample edge beats luck"
    if oos_days is not None and min_oos_days is not None and oos_days < min_oos_days:
        verdict = NOT_ENOUGH_OOS.format(n=oos_days, m=min_oos_days)
    return (f"after allowing for luck over {n_trials} tries (every setting run in training, in every window), "
            f"{verdict}; the out-of-sample days were never used to choose, so this is a cautious figure")


def wf_sentence(summary, *, chosen_windows: int | None = None) -> str:  # noqa: ANN001
    """The ratio and the stability of a walk-forward summary, in one line (a WalkforwardSummary)."""
    return f"{wf_ratio_sentence(summary)}; {wf_stability_sentence(summary, chosen_windows=chosen_windows)}"


# ---- sensitivity (M6.7, FW 6.4) ---------------------------------------------------------------------------------

def signed_rupees(paise: int) -> str:
    """Rupees with an explicit sign: +₹41,200, -₹3,100, ₹0."""
    return ("+" if paise > 0 else "") + rupees(paise / 100)


def _words_scale(scale: str, kind: str) -> str:
    """The cost multiplier in words. `kind`: "break" (if all costs ...), "survive" (N times all costs), "run"."""
    d = Decimal(scale).normalize()
    text = format(d, "f")
    if kind == "survive":
        return f"{text} times all costs"
    if kind == "run":
        return f"{text}-times-costs"
    if d == Decimal("1.5"):
        return "all costs rise by half"
    if d == 2:
        return "all costs double"
    return f"all costs are {text} times as high"


def _words_ticks(ticks: int, kind: str) -> str:
    one = ticks == 1
    n = "one" if one else str(ticks)
    if kind == "run":
        return f"{n}-extra-tick" if one else f"{n}-extra-ticks"
    return f"{n} extra tick{'' if one else 's'}" + (" of slippage" if kind in ("break", "survive") else "")


# What a failed run or job may say about itself on a page or in the API. A fixed table keyed by the error class (the
# class name a stored `ClassName: text` starts with), so exception text (a path, a value, a message) never leaves the
# machine; the raw text stays in the stored row and the log. This is the only such table: walkforward.py's
# `plain_failure` and `plain_error` read it.
GENERIC_FAILURE = "the run failed; see the log"
PLAIN_FAILURES: tuple[tuple[str, str], ...] = (
    ("HoldoutLocked", "the holdout is locked"),
    ("RunConfigError", "the settings were not accepted"),
    ("SweepSpecError", "the sweep file has a problem; run `python -m stockify sweep --config FILE` to see it"),
    ("WalkforwardRunError", "the run pool returned the wrong number of results"),
    ("SensitivityRunError", "the run pool returned the wrong number of results"),
)
# Why a sensitivity check could not be run for a strategy: the validator's or the resolver's refusal, put into one of these
# lines by what it is about (NOT_RUN_NEEDLES). The refusal's own text carries values and paths, so it is never shown.
NOT_RUN_RAW_PRICES = "this fill model only works with raw prices, and this run uses adjusted prices"
NOT_RUN_EXTRA_TICKS = "the extra slippage is above the most a run allows"
NOT_RUN_NO_QUOTES = "the recorded data has no quotes for this fill model"
NOT_RUN_DATA = "the recorded data this run needs could not be found or does not match"
NOT_RUN_REASONS: tuple[str, ...] = (NOT_RUN_RAW_PRICES, NOT_RUN_EXTRA_TICKS, NOT_RUN_NO_QUOTES, NOT_RUN_DATA)
NOT_RUN_NEEDLES: tuple[tuple[str, str], ...] = (
    ("price_basis", NOT_RUN_RAW_PRICES),
    ("extra_ticks", NOT_RUN_EXTRA_TICKS),
    ("quote", NOT_RUN_NO_QUOTES),
    ("pack", NOT_RUN_DATA),
)
# The sentences the code itself writes into a stored error (pool.py, sweep.py, sensitivity.py) and the table's lines.
PLAIN_SENTENCES: frozenset[str] = frozenset({
    "the run did not report its folder",
    "the run failed",
    "the run finished but has no stored result",
    "the settings were not accepted",
    GENERIC_FAILURE,
    *(text for _cls, text in PLAIN_FAILURES),
    *NOT_RUN_REASONS,
})


def failure_line(class_name: str) -> str | None:
    """The table's line for an error class name, or None when the table does not name it."""
    return next((line for cls, line in PLAIN_FAILURES if cls == class_name), None)


_CLASS_PREFIX = re.compile(r"([A-Za-z_][A-Za-z0-9_]*):")


def plain_reason(error: object, *, fallback: str | None = GENERIC_FAILURE) -> str | None:
    """A stored failure text as a sentence that is safe to show: None for nothing; the text itself only when it is one of
    the code's own sentences (PLAIN_SENTENCES); else the table's line for the class a leading `ClassName:` names; else
    `fallback` (the generic line, or None for a caller that would rather say nothing). Exception text is never returned."""
    if not isinstance(error, str):
        return None
    text = error.strip().rstrip(".")
    if not text:
        return None
    if text in PLAIN_SENTENCES:
        return text
    m = _CLASS_PREFIX.match(text)
    line = failure_line(m.group(1)) if m else None
    return line if line is not None else fallback


def not_run_reason(error: object) -> str | None:
    """Why a check could not be run, from the refusal text the validator or resolver gave: one of NOT_RUN_REASONS when the
    text is about one of those things, the code's own sentence or a class-table line as plain_reason gives them, else the
    generic line (a raw exception, a path, anything else); None for nothing."""
    known = plain_reason(error, fallback=None)
    if known is not None or not isinstance(error, str) or not error.strip():
        return known
    low = error.lower()
    if re.match(r"[A-Z][A-Za-z0-9]*:", error.strip()):          # "ClassName: text" is an exception's repr, not a refusal
        return GENERIC_FAILURE
    for needle, line in NOT_RUN_NEEDLES:
        if needle in low:
            return line
    return GENERIC_FAILURE


def family_best_sentence(n_trials: int, dsr: float | None, objective: str, n_finished: int | None = None) -> str:
    """The /research overview's line for a family: its best try and how likely its edge is more than luck. It says
    "N tries (M finished)" when some tries did not finish (D-85)."""
    if n_trials < 1 or n_finished == 0:
        return "no finished tries yet"
    return dsr_sentence(n_trials, 1, dsr, objective=objective, n_finished=n_finished)


def job_status_sentence(status: str, error: str | None) -> str:
    """A research job's state in words: running, finished, or failed with its readable reason."""
    if status == "running":
        return "still running"
    if status == "done":
        return "finished"
    if status == "failed":
        why = plain_reason(error)
        return f"failed: {why}" if why else "failed, and no readable reason was saved"
    return f"in an unrecognised state ({status})" if status.isalpha() else "in an unrecognised state"


def fragile_sentence(result, *, fragile_cost_scale: str | None = None,  # noqa: ANN001
                     fragile_extra_ticks: int | None = None) -> str:
    """One plain sentence for a SensitivityResult, true to its verdict (stockify.research.sensitivity._verdict):
    fragile = the run made money and turns negative with all costs multiplied by the configured scale or with the
    configured number of extra slippage ticks; robust = both of those held; inconclusive = neither flipped but one
    could not be run (so it is not called robust); already_losing = nothing to break. The two checks default to the
    research settings and can be passed (the stored result file carries them)."""
    from stockify.research import sensitivity as _s
    if fragile_cost_scale is None or fragile_extra_ticks is None:
        scale, ticks = _s._fragile_settings(_s._settings())
        fragile_cost_scale = scale if fragile_cost_scale is None else fragile_cost_scale
        fragile_extra_ticks = ticks if fragile_extra_ticks is None else fragile_extra_ticks
    rows = {r.key: r for r in result.rows}
    base = result.baseline_net_paise
    verdict = result.verdict
    if verdict == "already_losing":
        if base == 0:
            return f"Made no money ({signed_rupees(base)}), so there is nothing to break; it is not called fragile."
        return f"Already losing money ({signed_rupees(base)}), so there is nothing to break; it is not called fragile."
    checks = ((_s.variant_key("costs", fragile_cost_scale), "costs"),
              (_s.variant_key("slippage", fragile_extra_ticks), "ticks"))

    def phrase(axis: str, kind: str) -> str:
        return (_words_scale(fragile_cost_scale, kind) if axis == "costs"
                else _words_ticks(fragile_extra_ticks, kind))

    def outcome(key: str) -> tuple[str, int | None, str | None]:
        row = rows.get(key)
        if row is None:                                   # the check is the baseline itself
            return "done", base, None
        return (row.status, row.net_pnl_paise if row.status == "done" else None, row.error)

    results = [(axis, *outcome(key)) for key, axis, in checks]
    if verdict == "fragile":
        broke = [(axis, net) for axis, status, net, _e in results if status == "done" and net is not None and net < 0]
        if not broke:
            raise ValueError("a fragile result has no check below zero")
        parts = []
        for n, (axis, net) in enumerate(broke):
            lead = "Breaks " if n == 0 else "It also breaks "
            word = phrase(axis, "break")
            joint = f"if {word}" if axis == "costs" else f"with {word}"
            parts.append(f"{lead}{joint}: net P&L turns from {signed_rupees(base)} to {signed_rupees(net)}.")
        return " ".join(parts)
    if verdict == "robust":
        nets = [net for _, status, net, _e in results if status == "done" and net is not None]
        if len(nets) != 2:
            raise ValueError("a robust result needs both checks")
        if base < 0 or any(n < 0 for n in nets):
            raise ValueError("a robust result has a check below zero")
        return (f"Survives {phrase('costs', 'survive')} and {phrase('ticks', 'survive')}: net P&L does not turn "
                f"negative ({signed_rupees(nets[0])} and {signed_rupees(nets[1])}, from {signed_rupees(base)}).")
    if verdict == "inconclusive":
        clauses = []
        for axis, status, _net, error in results:
            if status == "done":
                continue
            what = phrase(axis, "survive")
            if status == "not_applicable":
                why = not_run_reason(error)
                clauses.append(f"{what} couldn't be run for this strategy" + (f" ({why})" if why else ""))
            else:
                clauses.append(f"the run with {what} didn't finish")
        if not clauses:
            raise ValueError("an inconclusive result has every check run")
        return f"Can't say yet: {'; and '.join(clauses)}, so it isn't called robust."
    raise ValueError(f"unknown sensitivity verdict {verdict!r}")

"""Which headline figures of a run may be shown, and the plain reason when one may not (D-135).

`metrics.json` stays exactly as the engine and the oracle wrote it (it is a hashed, deterministic artifact). Every
place that SHOWS a run's figures (the run page, the compare table, `report.md`) goes through `present`, so a figure
that is meaningless for a run is withheld the same way, with the same words, everywhere:

* the yearly figures (CAGR, Calmar, the yearly Sharpe, Sortino, volatility and turnover) are scaled by 252/days or
  sqrt(252), so a run shorter than `research.json` `reporting.min_days_yearly` trading days gives numbers like
  "-4.38 % a year" from one bad day: they read "too short to say";
* costs as a share of the profit before costs has no meaning when there was no profit before costs.
"""
from __future__ import annotations

from typing import Any

from stockify import research

# The metrics.json keys that are scaled to a year.
YEARLY_KEYS = ("cagr", "volatility", "sharpe", "sortino", "calmar", "turnover")
TOO_SHORT = "too short to say"
NO_GROSS = "no gross profit"


def min_days_yearly() -> int:
    return int(research.settings()["reporting"]["min_days_yearly"])


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def cost_share(m: dict) -> float | None:
    """Costs / gross profit, only when the gross profit is above zero. Works from the stored paise (a run made before
    this rule stored the ratio for a loss too)."""
    gross, costs = m.get("gross_pnl_paise"), m.get("costs_total_paise")
    if _is_num(gross) and _is_num(costs):
        return costs / gross if gross > 0 else None
    stored = m.get("cost_share_of_gross")
    return stored if _is_num(stored) and stored >= 0 else None


def present(m: dict, *, min_days: int | None = None) -> tuple[dict, dict[str, str]]:
    """(figures, withheld): a copy of one run's metrics with every figure that may not be shown set to None, and
    `withheld` mapping each such key (a yearly key, or `cost_share_of_gross`) to its plain reason. A figure that was
    already None for another reason is not listed (there is nothing to explain by length or sign)."""
    limit = min_days_yearly() if min_days is None else min_days
    out, why = dict(m), {}
    days = m.get("days")
    short = not _is_num(days) or days < limit
    if short:
        for k in YEARLY_KEYS:
            if out.get(k) is not None:
                out[k], why[k] = None, TOO_SHORT
    share = cost_share(m)
    if m.get("cost_share_of_gross") is not None or share is not None:
        if share is None:
            why["cost_share_of_gross"] = NO_GROSS
        out["cost_share_of_gross"] = share
    return out, why

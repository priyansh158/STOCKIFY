"""Which headline figures of a run may be shown, and the plain reason when one may not (D-135).

`metrics.json` stays exactly as the engine and the oracle wrote it (it is a hashed, deterministic artifact). Every
place that SHOWS a run's figures (the run page, the compare table, `report.md`) goes through `present`, so a figure
that is meaningless for a run is withheld the same way, with the same words, everywhere:

* the figures that EXTRAPOLATE a short sample to a year (CAGR compounds it, Calmar is built on CAGR, turnover is a
  per-year rate scaled by 252/days) are withheld for a run shorter than `research.json`
  `reporting.min_days_yearly` trading days: one bad day reads "-4.38 % a year". They show "too short to say";
* Sharpe, Sortino and volatility scale a daily dispersion by sqrt(252), a standard convention, so they stay shown
  whenever they exist. When they do not, the reason is the real one (no trades, too few days, no day-to-day swing),
  the same words the research panel uses;
* costs as a share of the profit before costs has no meaning when there was no profit before costs.
"""
from __future__ import annotations

from typing import Any

from stockify import research

# The metrics.json keys that extrapolate a short sample to a year (withheld on a short run).
YEARLY_KEYS = ("cagr", "calmar", "turnover")
TOO_SHORT = "too short to say"
NO_TRADES, FEW_DAYS, NO_SWING = "no trades", "too few days", "no day-to-day swing"
NO_LOSING_DAY = "no losing day"
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
    `withheld` mapping each such key to its plain reason (an extrapolated key, a Sharpe-family key that is missing, or
    `cost_share_of_gross`)."""
    limit = min_days_yearly() if min_days is None else min_days
    out, why = dict(m), {}
    days = m.get("days")
    short = not _is_num(days) or days < limit
    if short:
        for k in YEARLY_KEYS:
            if out.get(k) is not None:
                out[k], why[k] = None, TOO_SHORT
    traded = m.get("trades")
    for k in ("sharpe", "sortino", "volatility"):
        if out.get(k) is None and k in m:
            if k != "volatility" and _is_num(traded) and traded == 0:
                why[k] = NO_TRADES
            elif not _is_num(days) or days < 2:
                why[k] = FEW_DAYS
            elif k == "sharpe":
                why[k] = NO_SWING
            elif k == "sortino":
                why[k] = NO_LOSING_DAY
    share = cost_share(m)
    gross, costs = m.get("gross_pnl_paise"), m.get("costs_total_paise")
    no_profit = _is_num(gross) and _is_num(costs) and gross <= 0 < costs
    if m.get("cost_share_of_gross") is not None or share is not None or no_profit:
        if share is None:
            why["cost_share_of_gross"] = NO_GROSS
        out["cost_share_of_gross"] = share
    return out, why

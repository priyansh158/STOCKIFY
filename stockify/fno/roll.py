"""Rollover helpers for F&O strategies (M4.2). Plain Python, not engine rules: a strategy decides when to roll.

A *contract* is an instrument row as the bundle has it (`ctx._rows()`), a dict with `kind`, `underlying`,
`expiry` (ISO date), optionally `actual_expiry` (D-41: the day NSE really expired it, when that came early) and,
for options, `strike_paise`. Its last day is `actual_expiry` if set, else `expiry`.

These never read the clock: pass `today` (a `date` or ISO string, normally `ctx.today()`). Trading days come from
the shipped trading calendar (`stockify.data.calendar`, dated config, I-1), segment FO.
"""
from __future__ import annotations

from datetime import date, timedelta


def _day(d: date | str) -> date:
    return d if isinstance(d, date) else date.fromisoformat(d)


def last_day(contract: dict) -> date:
    """The contract's last trading day: its actual expiry when NSE expired it early (D-41), else its expiry."""
    return _day(contract.get("actual_expiry") or contract["expiry"])


def days_to_expiry(contract: dict, today: date | str) -> int:
    """Calendar days from `today` to the last day (0 on it, negative after)."""
    return (last_day(contract) - _day(today)).days


def trading_days_to_expiry(contract: dict, today: date | str) -> int:
    """FO trading days after `today` up to and including the last day (0 on the last day or after it)."""
    from stockify.data import calendar
    start, end = _day(today) + timedelta(days=1), last_day(contract)
    return len(calendar.trading_days("FO", start, end)) if start <= end else 0


def roll_due(contract: dict, trading_days_before: int, today: date | str) -> bool:
    """Whether to roll now: at most `trading_days_before` FO trading days remain after today (so 0 means on the
    last day; 1 means the day before it, or on it). Always true on or after the last day."""
    if trading_days_before < 0:
        raise ValueError("trading_days_before must be 0 or more")
    if _day(today) >= last_day(contract):
        return True
    return trading_days_to_expiry(contract, today) <= trading_days_before


def next_contract(contract: dict, contracts: list[dict]) -> dict | None:
    """The same underlying and kind with the next later scheduled expiry (for options also the same strike), or
    None. `contracts` is any list of rows, e.g. `ctx._rows()`; order does not matter."""
    def same(r: dict) -> bool:
        return (r.get("underlying") == contract.get("underlying") and r.get("kind") == contract.get("kind")
                and (contract.get("kind") not in ("CE", "PE") or r.get("strike_paise") == contract.get("strike_paise")))
    later = [r for r in contracts if same(r) and r.get("expiry") and r["expiry"] > contract["expiry"]]
    return min(later, key=lambda r: r["expiry"]) if later else None

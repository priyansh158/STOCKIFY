"""FS 6: the ranking history for the test record. On the first trading day (NSE CM) of each month, the screen's order
(`screen.order`) for each balance in fundamentals.json `balances`, keeping the first `top_kept` symbols.

The screen is point in time (pit.py: a filing counts from the first trading day after its broadcast; market value
from NSE's raw close on the day), so each month's list is what the market could have known on that day and nothing
it learnt later. A month in which the screen places no stock is left out of the lists (the strategy keeps the last
list, or stays in cash before the first).

The answer is the Fundamentals basket's `ranking` parameter: {"0.5": [{"date": "2022-01-03", "symbols": [...]}, ...]}.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from stockify.config import loader
from stockify.data import calendar as nse_calendar
from stockify.fundamentals import pit, screen

log = logging.getLogger("stockify.fundamentals.history")

SEGMENT = "CM"


def _cfg() -> dict:
    return loader.load_json("fundamentals.json")


def top_kept() -> int:
    return int(_cfg()["top_kept"])


def balances() -> list[str]:
    return list(_cfg()["balances"])


def basket_sizes() -> list[int]:
    return list(_cfg()["basket_sizes"])


def rebalance_days(start: date, end: date) -> list[date]:
    """The first NSE trading day of each month, for those from `start` through `end` (inclusive)."""
    out, month = [], start.replace(day=1)
    while month <= end:
        nxt = (month + timedelta(days=32)).replace(day=1)
        days = nse_calendar.trading_days(SEGMENT, month, nxt - timedelta(days=1))
        if days and start <= days[0] <= end:
            out.append(days[0])
        month = nxt
    return out


def rankings(start: date, end: date) -> dict[str, list[dict]]:
    """{balance label: [{"date": ISO, "symbols": the first top_kept of that day's order}]} for every rebalance day
    from `start` through `end`, dates rising; days on which no stock is placed are left out."""
    labels, keep = balances(), top_kept()
    out: dict[str, list[dict]] = {b: [] for b in labels}
    with pit.reads_cached():
        for day in rebalance_days(start, end):
            for b in labels:
                symbols = [r["symbol"] for r in screen.order(day, balance=float(b))][:keep]
                if symbols:
                    out[b].append({"date": day.isoformat(), "symbols": symbols})
            log.info("ranked %s", day.isoformat())
    return out

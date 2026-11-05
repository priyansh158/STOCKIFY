"""Time in STOCKIFY (invariant I-4).

Stored time is UTC epoch nanoseconds. IST (Asia/Kolkata, UTC+05:30, no
daylight saving) is used only at the edges: session logic, display, file
names. The machine's local time zone is never used. This module is the only
place outside `stockify/daemon/clock.py` allowed to read the wall clock.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
NS_PER_S = 1_000_000_000


def now_ns() -> int:
    return time.time_ns()


def ist_today() -> date:
    return datetime.now(tz=IST).date()


def ns_to_ist(t_ns: int) -> datetime:
    return datetime.fromtimestamp(t_ns / NS_PER_S, tz=timezone.utc).astimezone(IST)


def ist_to_ns(dt: datetime) -> int:
    """An aware datetime, or a naive one taken as IST, to UTC epoch ns."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    delta = dt.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86_400 + delta.seconds) * NS_PER_S + delta.microseconds * 1_000

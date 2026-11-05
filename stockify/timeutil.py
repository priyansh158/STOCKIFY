"""Time in STOCKIFY (invariant I-4).

Stored time is UTC epoch nanoseconds. IST (Asia/Kolkata, UTC+05:30, no
daylight saving) is used only at the edges: session logic, display, file
names. The machine's local time zone is never used. This module and
`stockify/daemon/clock.py` are the only places allowed to read the wall
clock (enforced by tests/stockify/test_time_rules.py).
"""
from __future__ import annotations

import time
from datetime import date, datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
NS_PER_S = 1_000_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def now_ns() -> int:
    return time.time_ns()


def now_ist() -> datetime:
    return datetime.now(tz=IST)


def ist_today() -> date:
    return now_ist().date()


def ns_to_ist(t_ns: int) -> datetime:
    """UTC epoch ns to an aware IST datetime (whole microseconds)."""
    seconds, rem = divmod(int(t_ns), NS_PER_S)
    return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(IST).replace(
        microsecond=rem // 1_000)


def s_to_ist(t_s: int | float) -> datetime:
    return datetime.fromtimestamp(t_s, tz=timezone.utc).astimezone(IST)


def ist_to_ns(dt: datetime) -> int:
    """An aware datetime, or a naive one taken as IST, to UTC epoch ns (exact)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    delta = dt.astimezone(timezone.utc) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * NS_PER_S + delta.microseconds * 1_000


def parse_hhmm(text: str) -> dtime:
    """'09:15' or '23:59:59' to a time."""
    parts = [int(p) for p in text.split(":")]
    if len(parts) not in (2, 3):
        raise ValueError(f"not HH:MM[:SS]: {text!r}")
    return dtime(*parts)


def ist_ns(d: date, hhmm: str) -> int:
    """UTC epoch ns of an IST wall time on an IST trading date."""
    return ist_to_ns(datetime.combine(d, parse_hhmm(hhmm), tzinfo=IST))

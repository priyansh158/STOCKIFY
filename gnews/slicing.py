"""Date-window planners to work around the ~100-item Google News RSS cap."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Iterator, Optional

_WHEN_RE = re.compile(r"^(\d+)([hdy])$", re.IGNORECASE)

# Ranges that benefit from day-slicing when maxItems > ~100
_SLICE_UNITS = {"d", "y"}


def normalize_when(value: Optional[str]) -> Optional[str]:
    """Validate openEndedDateRange like 1h, 7d, 1y. Returns normalized lowercase."""
    if not value:
        return None
    v = value.strip().lower()
    if not _WHEN_RE.match(v):
        raise ValueError(f"Invalid openEndedDateRange {value!r}; expected e.g. 1h, 7d, 30d, 1y")
    return v


def parse_iso_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


def when_to_date_range(when: str, *, today: Optional[date] = None) -> tuple[Optional[date], date]:
    """Map when:Xh/Xd/Xy to (start_date, end_date_exclusive).

    Hours return (None, today+1) — caller should use when: operator, not day slices.
    """
    m = _WHEN_RE.match(when.strip().lower())
    if not m:
        raise ValueError(f"Invalid when value: {when!r}")
    n, unit = int(m.group(1)), m.group(2).lower()
    end = today or datetime.now(timezone.utc).date()
    end_excl = end + timedelta(days=1)

    if unit == "h":
        return None, end_excl
    if unit == "d":
        start = end - timedelta(days=max(n - 1, 0))
        return start, end_excl
    if unit == "y":
        start = end - timedelta(days=365 * n)
        return start, end_excl
    raise ValueError(f"Unsupported unit in {when!r}")


def should_day_slice(
    *,
    max_items: int,
    open_ended: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
) -> bool:
    """Day-slice when we need >100 items over a multi-day span."""
    if max_items <= 100:
        return False
    if date_from or date_to:
        return True
    if open_ended:
        m = _WHEN_RE.match(open_ended)
        if m and m.group(2).lower() in _SLICE_UNITS:
            n = int(m.group(1))
            # Multi-day or multi-year ranges
            return n >= 2 or m.group(2).lower() == "y"
    return False


def compose_query(
    base_q: str,
    *,
    open_ended: Optional[str] = None,
    after: Optional[date] = None,
    before: Optional[date] = None,
) -> str:
    """Build Google News q string with when:/after:/before: operators."""
    parts = [base_q.strip()]
    if open_ended and after is None and before is None:
        parts.append(f"when:{open_ended}")
    if after is not None:
        parts.append(f"after:{after.isoformat()}")
    if before is not None:
        parts.append(f"before:{before.isoformat()}")
    return " ".join(p for p in parts if p)


def iter_day_windows(
    date_from: date,
    date_to_exclusive: date,
) -> Iterator[tuple[date, date]]:
    """Yield (after, before) pairs for each calendar day in [from, to_exclusive)."""
    if date_to_exclusive <= date_from:
        return
    cur = date_from
    while cur < date_to_exclusive:
        nxt = cur + timedelta(days=1)
        yield cur, nxt
        cur = nxt


def resolve_windows(
    base_q: str,
    *,
    max_items: int = 100,
    open_ended: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    today: Optional[date] = None,
) -> list[str]:
    """Return list of fully-composed query strings to fetch."""
    open_ended = normalize_when(open_ended) if open_ended else None
    today_d = today or datetime.now(timezone.utc).date()

    start: Optional[date] = parse_iso_date(date_from) if date_from else None
    # date_to is inclusive end-of-day → exclusive = +1 day
    if date_to:
        end_excl = parse_iso_date(date_to) + timedelta(days=1)
    else:
        end_excl = today_d + timedelta(days=1)

    if open_ended and start is None:
        w_start, w_end = when_to_date_range(open_ended, today=today_d)
        if w_start is not None:
            start = w_start
            end_excl = w_end

    if should_day_slice(
        max_items=max_items,
        open_ended=open_ended,
        date_from=date_from,
        date_to=date_to,
    ) and start is not None:
        queries: list[str] = []
        for after, before in iter_day_windows(start, end_excl):
            queries.append(compose_query(base_q, after=after, before=before))
        return queries or [compose_query(base_q, open_ended=open_ended)]

    # Single feed
    if start is not None and (date_from or date_to):
        return [compose_query(base_q, after=start, before=end_excl)]
    return [compose_query(base_q, open_ended=open_ended)]

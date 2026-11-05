"""NSE trading calendar, session phases and expiry rules (M1.2).

Everything comes from dated config (invariant I-1):

* `calendar/nse_holidays.csv`: trading holidays per segment (CM, FO)
* `calendar/special_sessions.csv`: extra sessions (Muhurat, special Saturdays)
* `sessions.json`: phases per group (CM, CM_CAS, FO_OPT, FO_FUT), dated
* `contract_rules.json`: weekly/monthly expiry weekdays, dated, plus the
  holiday shift rule

Expiry dates themselves come from data (Kite dumps, bhavcopy). The rules here
generate expected dates and *validate* the data (I-6); they never override it.

Times in config are IST; everything returned is UTC epoch nanoseconds.
"""
from __future__ import annotations

import calendar as _cal
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache

from stockify import timeutil
from stockify.config import loader

SEGMENTS = ("CM", "FO")
GROUP_SEGMENT = {"CM": "CM", "CM_CAS": "CM", "FO_OPT": "FO", "FO_FUT": "FO"}
WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
NEAR_MONTH_SCOPE = "near_month_futures"
NEXT_MONTH_PREOPEN_DAYS = 5          # X-6: next-month futures join the pre-open in the last 5 trading days


class CalendarError(ValueError):
    """Config cannot answer the question (no rule in force, unknown group)."""


@dataclass(frozen=True)
class Phase:
    phase: str
    start_ns: int
    end_ns: int


# ── Config loading (cached; tests call reload()) ─────────────────────────────

@lru_cache(maxsize=1)
def _holidays() -> frozenset[tuple[str, str]]:
    out = set()
    for row in loader.load_csv("calendar/nse_holidays.csv"):
        for seg in row["segments"].split(";"):
            seg = seg.strip().upper()
            if seg not in SEGMENTS:
                raise CalendarError(f"nse_holidays.csv {row['date']}: unknown segment {seg!r}")
            out.add((row["date"], seg))
    return frozenset(out)


@lru_cache(maxsize=1)
def _special_sessions() -> dict[tuple[str, str], list[dict]]:
    out: dict[tuple[str, str], list[dict]] = {}
    for row in loader.load_csv("calendar/special_sessions.csv"):
        if row["group"] not in GROUP_SEGMENT:
            raise CalendarError(f"special_sessions.csv {row['date']}: unknown group {row['group']!r}")
        out.setdefault((row["date"], row["group"]), []).append(row)
    return out


@lru_cache(maxsize=1)
def _sessions() -> list[dict]:
    return loader.load_json("sessions.json")["versions"]


@lru_cache(maxsize=1)
def _rules() -> dict:
    return loader.load_json("contract_rules.json")


def reload() -> None:
    for fn in (_holidays, _special_sessions, _sessions, _rules):
        fn.cache_clear()


# ── Trading days ─────────────────────────────────────────────────────────────

def _special_on(segment: str, d: date) -> bool:
    return any((d.isoformat(), g) in _special_sessions()
               for g, seg in GROUP_SEGMENT.items() if seg == segment)


def is_trading_day(segment: str, d: date) -> bool:
    segment = segment.upper()
    if segment not in SEGMENTS:
        raise CalendarError(f"unknown segment {segment!r}")
    if _special_on(segment, d):
        return True
    return d.weekday() < 5 and (d.isoformat(), segment) not in _holidays()


def trading_days(segment: str, start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if is_trading_day(segment, d):
            out.append(d)
        d += timedelta(days=1)
    return out


def previous_trading_day(segment: str, d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(segment, d):
        d -= timedelta(days=1)
    return d


def next_trading_day(segment: str, d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(segment, d):
        d += timedelta(days=1)
    return d


# ── Session phases ───────────────────────────────────────────────────────────

def _version(group: str, d: date) -> dict | None:
    rows = [v for v in _sessions() if v["group"] == group and v["effective_from"] <= d.isoformat()]
    return max(rows, key=lambda v: v["effective_from"]) if rows else None


def phases(group: str, d: date, *, preopen: bool = True) -> list[Phase]:
    """The day's phases for a group, in order; [] on a non-trading day.

    `preopen=False` drops phases scoped to near-month futures (the F&O
    pre-open, X-6) for a contract that is not eligible that day; see
    `futures_preopen_eligible`. CM_CAS falls back to CM before the closing
    auction existed.
    """
    if group not in GROUP_SEGMENT:
        raise CalendarError(f"unknown session group {group!r}")
    if not is_trading_day(GROUP_SEGMENT[group], d):
        return []
    special = _special_sessions().get((d.isoformat(), group))
    if special:
        rows = [{"phase": r["phase"], "start": r["start"], "end": r["end"]} for r in special]
    else:
        version = _version(group, d)
        if version is None and group == "CM_CAS":
            version = _version("CM", d)
        if version is None:
            raise CalendarError(f"no sessions.json version for {group} on {d}")
        rows = version["phases"]
    out = []
    for ph in rows:
        if not preopen and ph.get("scope") == NEAR_MONTH_SCOPE:
            continue
        out.append(Phase(ph["phase"], timeutil.ist_ns(d, ph["start"]), timeutil.ist_ns(d, ph["end"])))
    return sorted(out, key=lambda p: p.start_ns)


def phase_at(group: str, t_ns: int, *, preopen: bool = True) -> str | None:
    """Phase name in force at t_ns (start inclusive, end exclusive), or None."""
    d = timeutil.ns_to_ist(t_ns).date()
    for ph in phases(group, d, preopen=preopen):
        if ph.start_ns <= t_ns < ph.end_ns:
            return ph.phase
    return None


def close_vwap_window(group: str, d: date) -> tuple[int, int] | None:
    version = _version(group, d)
    if not version or "close_vwap_window" not in version:
        return None
    start, end = version["close_vwap_window"]
    return timeutil.ist_ns(d, start), timeutil.ist_ns(d, end)


def futures_preopen_eligible(expiry: date, d: date, month_expiries: list[date]) -> bool:
    """X-6: the F&O pre-open covers current-month futures, plus next-month
    futures in the last 5 trading days before the current-month expiry.

    `month_expiries` are the monthly futures expiries listed on `d` (from data).
    """
    upcoming = sorted(e for e in month_expiries if e >= d)
    if not upcoming:
        return False
    near = upcoming[0]
    if expiry == near:
        return True
    if len(upcoming) > 1 and expiry == upcoming[1]:
        remaining = trading_days("FO", d, near)
        return len(remaining) <= NEXT_MONTH_PREOPEN_DAYS
    return False


# ── Expiry rules (validators, I-6) ───────────────────────────────────────────

def _rule_for(d: date) -> dict | None:
    rows = [r for r in _rules()["expiry_rules"] if r["effective_from"] <= d.isoformat()]
    return max(rows, key=lambda r: r["effective_from"]) if rows else None


def _shift(d: date) -> date:
    if is_trading_day("FO", d):
        return d
    policy = _rules().get("holiday_shift", "previous_trading_day")
    if policy != "previous_trading_day":
        raise CalendarError(f"unknown holiday_shift {policy!r}")
    return previous_trading_day("FO", d)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, month, _cal.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def generated_expiries(underlying: str, start: date, end: date) -> list[dict]:
    """Expected expiries in [start, end]: [{date, kind: weekly|monthly, nominal}].

    The rule in force is chosen by the *nominal* expiry date (X-2: the Tuesday
    rule applies to contracts expiring after 2025-08-31). Dates without a rule
    in force produce nothing: history before the first sourced rule is not
    invented. `nominal` is the unshifted rule date; `date` is after the holiday shift.
    """
    underlying = underlying.upper()
    found: dict[date, dict] = {}
    d = start - timedelta(days=7)          # a holiday shift can pull an expiry back into range
    while d <= end + timedelta(days=7):
        rule = _rule_for(d)
        if rule:
            monthly = _last_weekday(d.year, d.month, WEEKDAYS[rule["monthly"]["weekday"]])
            if d == monthly:
                found[d] = {"kind": "monthly", "nominal": d}
            weekly = rule.get("weekly") or {}
            if (underlying in weekly.get("underlyings", [])
                    and d.weekday() == WEEKDAYS[weekly["weekday"]] and d not in found):
                found[d] = {"kind": "weekly", "nominal": d}
        d += timedelta(days=1)
    out = []
    for nominal, info in sorted(found.items()):
        actual = _shift(nominal)
        if start <= actual <= end:
            out.append({"date": actual, "kind": info["kind"], "nominal": nominal})
    return out


def validate_expiries(underlying: str, actual: list[date]) -> dict:
    """Compare data expiries with the rules over the data's own date range.

    Returns {"missing": rule dates absent from data, "unexpected": data dates
    the rules do not produce}. Disagreement is reported, never corrected (I-6).
    """
    if not actual:
        return {"missing": [], "unexpected": []}
    actual_set = set(actual)
    expected = {e["date"] for e in generated_expiries(underlying, min(actual), max(actual))}
    return {"missing": sorted(expected - actual_set), "unexpected": sorted(actual_set - expected)}

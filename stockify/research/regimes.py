"""A run's profit and loss split by month, weekday, expiry day, VIX level and opening gap (M6.9, IR 6.2).

    regimes.report(run_id)                  # RegimeReport, straight from the database
    regimes.build(dates, navs, capital)     # the same from a NAV series (what the report cache wraps)
    regimes.refdata_digest(start, end)      # fingerprint of every refdata row the split reads (cache key part)

The daily P&L of day d is NAV_d - NAV_(d-1), the first day against the starting capital, so for every split the
bucket days add up to the run's days and the bucket P&L to NAV_end - capital exactly (money is int paise, the mean
is rounded half away from zero in integers). Only buckets that hold at least one day are listed, in a fixed order;
a day with no VIX or gap figure goes into a bucket called "no data", never dropped.

Units: VIX is index points x 100 as stored (edges in settings are points); the opening gap is basis points of
open / previous close - 1 (edges in settings are percent, so 0.5 means 50 basis points); weekday and month come
from the IST trading date as stored in run_equity. A day's VIX bucket uses the previous trading day's close,
because the day's own close is not known when the day opens.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction

from stockify import store, timeutil
from stockify.config import loader
from stockify.data import calendar, instruments, refdata, ticks
from stockify.research import settings

CODE_VERSION = "2"            # bump when the buckets or the way they are built change: it invalidates the cache
                              # (2: expiries from the calendar outside refdata's span; only a full day sets the VIX)
NO_DATA = "no data"
EXPIRY_DAY = "expiry day"
OTHER_DAYS = "other days"
SEGMENT = "CM"                # the cash segment's calendar decides "the previous trading day" of an index
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
DIMENSIONS = ("month", "weekday", "expiry", "vix", "gap")
_DERIVATIVE_KINDS = ("FUT", "CE", "PE")
COMPLETE_STATUSES = ("verified", "landing_deleted")   # tick_days statuses of a finished, archived day (compact.ARCHIVED)
CLOSE_FROM_IST = (15, 25)                             # a recorded day with no tick_days row counts if it ran to here


@dataclass(frozen=True)
class Bucket:
    label: str
    days: int
    pnl_paise: int
    mean_pnl_paise: int | None
    up_days: int
    up_share: float | None
    thin: bool


@dataclass(frozen=True)
class RegimeReport:
    month: list[Bucket]
    weekday: list[Bucket]
    expiry: list[Bucket]
    vix: list[Bucket]
    gap: list[Bucket]
    vix_source: dict[str, str]
    notes: list[str]


# ── settings ──────────────────────────────────────────────────────────────────────────────────

def _cfg() -> dict:
    return settings()["regimes"]


def _thin_days() -> int:
    return int(_cfg()["thin_days"])


def _edges(key: str) -> list[Decimal]:
    return [Decimal(str(e)) for e in _cfg()[key]]


def _fmt(d: Decimal) -> str:
    return format(d.normalize(), "f")


def _vix_labels(edges: list[Decimal]) -> list[str]:
    out = [f"VIX below {_fmt(edges[0])}"]
    out += [f"VIX {_fmt(lo)}–{_fmt(hi)}" for lo, hi in zip(edges, edges[1:])]
    return out + [f"VIX {_fmt(edges[-1])} or more"]


def _gap_labels(edges: list[Decimal]) -> list[str]:
    first = edges[0]
    out = [f"gap down more than {_fmt(abs(first))}%" if first <= 0 else f"gap below {_fmt(first)}%"]
    for lo, hi in zip(edges, edges[1:]):
        if lo < 0 < hi:
            out.append(f"flat (within {_fmt(hi)}%)" if lo == -hi else f"{_fmt(lo)}% to {_fmt(hi)}%")
        elif hi <= 0:
            out.append(f"gap down {_fmt(abs(hi))}% to {_fmt(abs(lo))}%")
        else:
            out.append(f"gap up {_fmt(lo)}% to {_fmt(hi)}%")
    last = edges[-1]
    return out + [f"gap up {_fmt(last)}% or more" if last >= 0 else f"{_fmt(last)}% or higher"]


# ── small exact arithmetic ────────────────────────────────────────────────────────────────────

def _div_round(n: int, d: int) -> int:
    """n / d (d > 0) rounded half away from zero, in integers."""
    q = (2 * abs(n) + d) // (2 * d)
    return q if n >= 0 else -q


def _mean_paise(pnl_paise: int, days: int) -> int | None:
    return _div_round(pnl_paise, days) if days > 0 else None


def _bucket(label: str, pnls: list[int]) -> Bucket:
    days, total = len(pnls), sum(pnls)
    up = sum(1 for p in pnls if p > 0)
    return Bucket(label=label, days=days, pnl_paise=total, mean_pnl_paise=_mean_paise(total, days), up_days=up,
                  up_share=up / days if days else None, thin=days < _thin_days())


def _buckets(rows: list[tuple[int, str, int]]) -> list[Bucket]:
    """rows are (order, label, pnl); one bucket per (order, label), in order."""
    groups: dict[tuple[int, str], list[int]] = {}
    for order, label, pnl in rows:
        groups.setdefault((order, label), []).append(pnl)
    return [_bucket(label, pnls) for (_, label), pnls in sorted(groups.items())]


# ── refdata reads ─────────────────────────────────────────────────────────────────────────────

def _symbol_id(symbol: str) -> int | None:
    return instruments.find("NSE", symbol)


def _eod(instrument_id: int, day: date) -> sqlite3.Row | None:
    return refdata._connect().execute(
        "SELECT open_paise, close_paise FROM index_eod WHERE instrument_id = ? AND trade_date = ?",
        (instrument_id, day.isoformat())).fetchone()


def _previous(day: date) -> date:
    return calendar.previous_trading_day(SEGMENT, day)


def _day_is_complete(day: date, last_recv_ns: int | None) -> bool:
    """Whether a recorded day ran the whole session. The `tick_days` status decides when there is a row (verified or
    landing_deleted); with no row, the last VIX print must be at or after 15:25 IST. A partial day (the recorder
    started late or stopped early) would give a mid-session value, not the close."""
    row = refdata._connect().execute("SELECT status FROM tick_days WHERE trade_date = ?",
                                     (day.isoformat(),)).fetchone()
    if row is not None:
        return row["status"] in COMPLETE_STATUSES
    if last_recv_ns is None:
        return False
    t = timeutil.ns_to_ist(last_recv_ns)
    return (t.hour, t.minute) >= CLOSE_FROM_IST


def _recorded_close(day: date, vix_id: int) -> int | None:
    """The last VIX print of the day's tick archive, only for a complete day; None when the day was not recorded,
    is partial or has no VIX packet."""
    try:
        recorded = ticks.open_day(day)
        if recorded is None:
            return None
        last = last_ns = None
        with recorded:
            for p in recorded.packets(["ltp", "recv_ns"], instrument_ids={vix_id}, kinds=("index",)):
                if p["ltp"] is not None:
                    last, last_ns = int(p["ltp"]), p.get("recv_ns")
        return last if last is not None and _day_is_complete(day, last_ns) else None
    except (OSError, ValueError, sqlite3.Error):          # an unreadable archive is a miss, bhavcopy answers instead
        return None


def vix_close(day: date) -> tuple[int, str] | None:
    """(close in points x 100, "recorded" | "bhavcopy") of the VIX on `day`, or None. The recorded tick archive is
    asked first, then the bhavcopy `index_eod` row."""
    vix_id = _symbol_id(_cfg()["vix_symbol"])
    if vix_id is None:
        return None
    rec = _recorded_close(day, vix_id)
    if rec is not None:
        return rec, "recorded"
    row = _eod(vix_id, day)
    if row is not None and row["close_paise"] is not None:
        return int(row["close_paise"]), "bhavcopy"
    return None


def _gap_exact(day: date) -> Fraction | None:
    ref = _symbol_id(_cfg()["reference_index"])
    if ref is None:
        return None
    today, before = _eod(ref, day), _eod(ref, _previous(day))
    if today is None or before is None or today["open_paise"] is None or before["close_paise"] is None:
        return None
    prev = int(before["close_paise"])
    return Fraction((int(today["open_paise"]) - prev) * 10_000, prev) if prev > 0 else None


def opening_gap(day: date) -> int | None:
    """Basis points of open / previous close - 1 of the reference index (index_eod), rounded half away from zero."""
    g = _gap_exact(day)
    return None if g is None else _div_round(g.numerator, g.denominator)


def _span(conn, und: str) -> tuple[date, date] | None:
    """The first and last date refdata holds for the underlying, counting both the listed expiry and the actual
    one, so a contract moved by actual_expiry stays inside the span and its nominal date is not also generated."""
    marks = ",".join("?" * len(_DERIVATIVE_KINDS))
    row = conn.execute(f"SELECT MIN(MIN(expiry), MIN(COALESCE(actual_expiry, expiry))) AS lo, "
                       f"MAX(MAX(expiry), MAX(COALESCE(actual_expiry, expiry))) AS hi "
                       f"FROM instruments WHERE underlying = ? AND kind IN ({marks})",
                       (und, *_DERIVATIVE_KINDS)).fetchone()
    return None if row["lo"] is None else (date.fromisoformat(row["lo"]), date.fromisoformat(row["hi"]))


def _expiries(start: date, end: date) -> tuple[set[date], list[str]]:
    """Per underlying: refdata's expiries inside the span refdata covers, the calendar rules for every date outside
    it (before its first or after its last expiry). Never a union, so an expiry moved by actual_expiry is not
    counted twice."""
    conn = refdata._connect()
    notes: list[str] = []
    unders = sorted(r["key"] for r in conn.execute("SELECT key FROM index_underlyings"))
    if not unders:
        unders = sorted(loader.load_json("contract_rules.json").get("underlyings", {}))
        notes.append("refdata lists no index underlyings, so expiry days come from the calendar rules")
    found: set[date] = set()
    marks = ",".join("?" * len(_DERIVATIVE_KINDS))
    for und in unders:
        span = _span(conn, und)
        if span is None:
            outside = [(start, end)]
        else:
            lo, hi = span
            rows = conn.execute(
                f"SELECT DISTINCT COALESCE(actual_expiry, expiry) AS e FROM instruments WHERE underlying = ? "
                f"AND kind IN ({marks}) AND COALESCE(actual_expiry, expiry) BETWEEN ? AND ?",
                (und, *_DERIVATIVE_KINDS, max(start, lo).isoformat(), min(end, hi).isoformat())).fetchall()
            found.update(date.fromisoformat(r["e"]) for r in rows)
            outside = [(start, min(end, lo - timedelta(days=1))), (max(start, hi + timedelta(days=1)), end)]
        spans = [(a, b) for a, b in outside if a <= b]
        for a, b in spans:
            found.update(e["date"] for e in calendar.generated_expiries(und, a, b))
        if spans and not notes_has_global(notes):
            where = " and ".join(f"{a.isoformat()} to {b.isoformat()}" for a, b in spans)
            notes.append(f"expiry days of {und} from {where} come from the calendar rules: refdata has no "
                         "contracts there")
    return found, notes


def notes_has_global(notes: list[str]) -> bool:
    return any(n.startswith("refdata lists no index underlyings") for n in notes)


def expiry_days(start: date, end: date) -> set[date]:
    """Dates in [start, end] on which an index future or option expires: refdata contracts (COALESCE(actual_expiry,
    expiry), I-6, D-41), the calendar rules for an underlying refdata has no contracts for."""
    return _expiries(start, end)[0]


def refdata_digest(start: date, end: date) -> str:
    """SHA-256 over every refdata row the split reads for a run over [start, end]: the reference index and VIX
    `index_eod` rows from 7 days before start, the index underlyings and their expiry rows in range, and the
    `tick_days` rows (date, status, digest) in range. A late bhavcopy or a newly archived day changes it."""
    conn = refdata._connect()
    lo, hi = (start - timedelta(days=7)).isoformat(), end.isoformat()
    ids = [_symbol_id(_cfg()["reference_index"]), _symbol_id(_cfg()["vix_symbol"])]
    known = [i for i in ids if i is not None]
    eod = []
    if known:
        marks = ",".join("?" * len(known))
        eod = [list(r) for r in conn.execute(
            f"SELECT trade_date, instrument_id, open_paise, close_paise FROM index_eod WHERE instrument_id IN "
            f"({marks}) AND trade_date BETWEEN ? AND ? ORDER BY trade_date, instrument_id", (*known, lo, hi))]
    unders = [list(r) for r in conn.execute("SELECT key, index_symbol, effective_from FROM index_underlyings "
                                            "ORDER BY key")]
    exp = [list(r) for r in conn.execute(
        "SELECT DISTINCT underlying, expiry, actual_expiry FROM instruments WHERE kind IN ('FUT','CE','PE') "
        "AND underlying IN (SELECT key FROM index_underlyings) AND COALESCE(actual_expiry, expiry) BETWEEN ? AND ? "
        "ORDER BY 1, 2, 3", (start.isoformat(), hi))]
    spans = {u[0]: _span(conn, u[0]) for u in unders}
    tick = [list(r) for r in conn.execute(
        "SELECT trade_date, status, digest FROM tick_days WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
        (lo, hi))]
    blob = {"ids": ids, "index_eod": eod, "underlyings": unders, "expiries": exp, "spans": spans, "tick_days": tick}
    text = json.dumps(blob, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── the split ─────────────────────────────────────────────────────────────────────────────────

def build(dates: list[str], navs: list[int], capital: int) -> RegimeReport:
    """The regime report of a NAV series. With no usable capital the first day is measured against its own NAV
    (so it counts as a flat day) and a note says so."""
    notes: list[str] = []
    if not navs:
        return RegimeReport([], [], [], [], [], {}, ["the run has no days"])
    base = capital if capital > 0 else navs[0]
    if capital <= 0:
        notes.append("the run has no starting capital, so its first day counts as a flat day")
    pnls = [navs[0] - base] + [b - a for a, b in zip(navs, navs[1:])]
    days = [date.fromisoformat(d) for d in dates]

    cfg = _cfg()
    vix_edges = [Fraction(e) * 100 for e in _edges("vix_edges")]            # points -> points x 100
    gap_edges = [Fraction(e) * 100 for e in _edges("gap_edges_pct")]        # percent -> basis points
    vix_names, gap_names = _vix_labels(_edges("vix_edges")), _gap_labels(_edges("gap_edges_pct"))

    expiries, expiry_notes = _expiries(days[0], days[-1])
    notes += expiry_notes
    vix_id, ref_id = _symbol_id(cfg["vix_symbol"]), _symbol_id(cfg["reference_index"])
    if vix_id is None:
        notes.append(f"refdata has no {cfg['vix_symbol']} instrument, so the VIX split has no data")
    if ref_id is None:
        notes.append(f"refdata has no {cfg['reference_index']} instrument, so the opening-gap split has no data")

    vix_source: dict[str, str] = {}
    month, weekday, expiry, vix, gap = [], [], [], [], []
    vix_missing = gap_missing = 0
    for d, pnl in zip(days, pnls):
        month.append((0, f"{d.year:04d}-{d.month:02d}", pnl))
        weekday.append((d.weekday(), WEEKDAY_NAMES[d.weekday()], pnl))
        expiry.append((0, EXPIRY_DAY, pnl) if d in expiries else (1, OTHER_DAYS, pnl))
        prev = _previous(d)
        close = vix_close(prev)
        if close is None:
            vix.append((len(vix_names), NO_DATA, pnl))                      # sorts after every real bucket
            vix_missing += 1
        else:
            vix_source[prev.isoformat()] = close[1]
            i = bisect_right(vix_edges, close[0])
            vix.append((i, vix_names[i], pnl))
        g = _gap_exact(d)
        if g is None:
            gap.append((len(gap_names), NO_DATA, pnl))
            gap_missing += 1
        else:
            i = bisect_right(gap_edges, g)
            gap.append((i, gap_names[i], pnl))
    if vix_id is not None and vix_missing:
        notes.append(f"{vix_missing} of {len(days)} days have no VIX close for the day before "
                     "(no recorded ticks and no bhavcopy row)")
    if ref_id is not None and gap_missing:
        notes.append(f"{gap_missing} of {len(days)} days have no opening gap (missing index open or previous close)")
    return RegimeReport(month=_buckets(month), weekday=_buckets(weekday), expiry=_buckets(expiry),
                        vix=_buckets(vix), gap=_buckets(gap), vix_source=vix_source, notes=notes)


def _capital(row: dict) -> int:
    try:
        cfg = json.loads(row["config_json"]) if row.get("config_json") else {}
    except ValueError:
        return 0
    c = cfg.get("capital_paise") if isinstance(cfg, dict) else None
    return c if isinstance(c, int) and not isinstance(c, bool) else 0


def report(run_id: str) -> RegimeReport:
    row = store.get_run(run_id)
    if row is None:
        raise ValueError(f"no such run: {run_id!r}")
    series = store.run_nav_series(run_id)
    return build([d for d, _ in series], [v for _, v in series], _capital(row))


# ── the cache's side: a key, and a strict way back from JSON ─────────────────────────────────

def calendar_digest() -> str:
    """SHA-256 of the calendar data `previous_trading_day` and `generated_expiries` read: holidays, special
    sessions and the contract rules."""
    blob = {"holidays": sorted(calendar._holidays()),
            "special": sorted((k[0], k[1], [dict(r) for r in v]) for k, v in calendar._special_sessions().items()),
            "rules": calendar._rules()}
    text = json.dumps(blob, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cache_key(dates: list[str], navs: list[int], capital: int) -> str:
    """SHA-256 of the NAV series, the capital, the regime settings, the refdata digest and the code version."""
    digest = refdata_digest(date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])) if dates else ""
    blob = {"dates": dates, "navs": navs, "capital": capital, "settings": _cfg(), "refdata": digest,
            "calendar": calendar_digest(), "code_version": CODE_VERSION}
    text = json.dumps(blob, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def to_dict(rep: RegimeReport) -> dict:
    return asdict(rep)


def _int(v) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("int")
    return v


def _bucket_from(d) -> Bucket:
    if not isinstance(d, dict) or set(d) != {"label", "days", "pnl_paise", "mean_pnl_paise", "up_days", "up_share",
                                             "thin"}:
        raise ValueError("bucket")
    share, mean = d["up_share"], d["mean_pnl_paise"]
    if share is not None and (isinstance(share, bool) or not isinstance(share, (int, float))
                              or not 0 <= share <= 1):
        raise ValueError("share")
    if not isinstance(d["label"], str) or not isinstance(d["thin"], bool):
        raise ValueError("bucket")
    return Bucket(d["label"], _int(d["days"]), _int(d["pnl_paise"]), None if mean is None else _int(mean),
                  _int(d["up_days"]), None if share is None else float(share), d["thin"])


def from_dict(d) -> RegimeReport:
    """Rebuild a RegimeReport from cached JSON, checking every field; anything odd is a ValueError."""
    if not isinstance(d, dict) or set(d) != {*DIMENSIONS, "vix_source", "notes"}:
        raise ValueError("report")
    lists = {k: d[k] for k in DIMENSIONS}
    if not all(isinstance(v, list) for v in lists.values()):
        raise ValueError("buckets")
    src, notes = d["vix_source"], d["notes"]
    if (not isinstance(src, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in src.items())
            or not isinstance(notes, list) or not all(isinstance(n, str) for n in notes)):
        raise ValueError("report")
    return RegimeReport(**{k: [_bucket_from(b) for b in v] for k, v in lists.items()}, vix_source=dict(src),
                        notes=list(notes))

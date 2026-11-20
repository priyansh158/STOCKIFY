"""Kite historical candles and the bar cache `bars.sqlite` (M1.4, A1 §2).

Two layers:

* The private helpers moved here from the retired `technical_analyst.py` in
  M0.1 (`_resolve_token`, `_kite_rows`, `_fetch_candles_kite`): one window of
  candles as Kite gives them, used by the old search/analysis screens.
* The bar cache (`fetch`, `fetch_continuous`, `sweep`): integer paise, bar
  **start** in UTC ns, keyed by `instrument_id` (D-06), never by token.

Price basis of everything in `bars.sqlite` is `adjusted` (K-6, I-5): Kite
rewrites past candles after corporate actions. Every fetch for an instrument
that already has cached bars first re-fetches its last few cached daily bars
(C-5, D-18). If the closes moved by one consistent ratio, an
`adjustments_detected` row is written; a **live** instrument's cache is then
dropped and refetched, an **expired** one is only flagged (Kite cannot serve
its minute data again, so nothing is deleted).

Requests go through one process-wide token bucket (`historical_rps`, K-8).
Ranges are split into chunks of at most `historical_max_days` per interval
(`kite_limits.json`); when Kite rejects a range as too long the chunk is
halved, retried, and the smaller limit is remembered in
`$LAB/kite_limits_learned.json`.

Continuous futures (K-5): `continuous=1` with `interval=day` on a live
futures contract, stored under the synthetic instrument `CONT:<underlying>:FUT`
(kind `FUT_CONT`), never mixed into per-contract rows.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

try:
    import kite_client
    KITE_AVAILABLE = True
except ImportError:  # pragma: no cover - kite_client is part of the repo
    kite_client = None  # type: ignore
    KITE_AVAILABLE = False

from india_utils import strip_exchange_suffix
from stockify import dbconn, numerics, paths, timeutil
from stockify.data import migrate

log = logging.getLogger("stockify.data.history")

# ── Old helpers (M0.1) ───────────────────────────────────────────────────────

# Repo interval string -> Kite Connect interval string
_INTERVAL_TO_KITE = {
    "1d":  "day",
    "1wk": "week",
    "1mo": "month",
    "1h":  "60minute",
    "30m": "30minute",
    "15m": "15minute",
    "5m":  "5minute",
    "3m":  "3minute",
    "1m":  "minute",
}


def _resolve_token(symbol: str) -> int | None:
    """Instrument token for a bare symbol, trying NSE then BSE."""
    bare = strip_exchange_suffix(symbol)
    for exch in ("NSE", "BSE"):
        token = kite_client.get_instrument_token(bare, exchange=exch)
        if token:
            return token
    return None


def _kite_rows(rows: list[dict]) -> list[dict]:
    """Normalise Kite historical_data rows; drops rows without a date or
    with non-numeric fields."""
    out: list[dict] = []
    for r in rows:
        dt = r.get("date")
        if not dt:
            continue
        try:
            out.append({
                "date": dt,
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": int(r.get("volume") or 0),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _fetch_candles_kite(symbol: str, from_dt: datetime, to_dt: datetime,
                        interval: str) -> list[dict]:
    """One window of candles for `symbol`; `[]` when Kite is unavailable,
    the interval is unknown, or the symbol does not resolve."""
    if not KITE_AVAILABLE or not kite_client.is_authenticated():
        return []
    kite_interval = _INTERVAL_TO_KITE.get(interval)
    if not kite_interval:
        return []
    token = _resolve_token(symbol)
    if not token:
        return []
    return _kite_rows(kite_client.historical(token, from_dt, to_dt, kite_interval))


# ── bars.sqlite ──────────────────────────────────────────────────────────────

STORE = "bars"
INTERVALS = ("minute", "3minute", "5minute", "10minute", "15minute", "30minute", "60minute", "day")
SEC = 1_000_000_000
DAY_NS = 86_400 * SEC

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _db_path():
    return paths.bars_db()


def _open() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=dbconn.BUSY_TIMEOUT_S, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


_pool = dbconn.Pool(__name__, _open, lambda conn: migrate.apply(conn, STORE))


def _connect() -> sqlite3.Connection:
    """This thread's connection (D-143: one per thread; the writers still take `_lock`)."""
    return _pool.get()


class _Tx:
    def __enter__(self) -> sqlite3.Connection:
        self.conn = _connect()
        _lock.acquire()
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        try:
            self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            _lock.release()


class HistoryError(RuntimeError):
    pass


# ── Limits ───────────────────────────────────────────────────────────────────

def limits() -> dict:
    from stockify.config import loader
    return loader.load_json("kite_limits.json")


def _learned_file():
    return paths.lab_dir() / "kite_limits_learned.json"


def max_days(interval: str, cfg: dict | None = None) -> int:
    cfg = cfg or limits()
    default = int(cfg["historical_max_days"][interval])
    try:
        learned = json.loads(_learned_file().read_text(encoding="utf-8")).get(interval)
    except (FileNotFoundError, json.JSONDecodeError):
        learned = None
    return min(default, int(learned)) if learned else default


def _learn(interval: str, days: int) -> None:
    path = _learned_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if data.get(interval) and int(data[interval]) <= days:
        return
    data[interval] = days
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    log.warning("Kite range limit for %s learned: %d days", interval, days)


# ── Throttle ─────────────────────────────────────────────────────────────────

class TokenBucket:
    """At most `rate` requests in any one-second window: capacity 1, so calls are
    spaced 1/rate apart (a burst of `rate` plus refill would allow rate+1 per
    second). Time comes from a daemon Clock."""

    def __init__(self, rate: float, clock):  # noqa: ANN001
        self.rate, self.capacity, self.clock = float(rate), 1.0, clock
        self.tokens = self.capacity
        self.last = clock.mono_ns()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            while True:
                now = self.clock.mono_ns()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) / SEC * self.rate)
                self.last = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait_ns = int((1.0 - self.tokens) / self.rate * SEC) + 1
                self.clock.sleep_until(self.clock.now_ns() + wait_ns)


_bucket: TokenBucket | None = None


def bucket(clock=None) -> TokenBucket:  # noqa: ANN001
    """The process-wide historical-API bucket (K-8)."""
    global _bucket
    if _bucket is None:
        from stockify.daemon.clock import WallClock
        _bucket = TokenBucket(limits()["historical_rps"], clock or WallClock())
    return _bucket


# ── Conversions ──────────────────────────────────────────────────────────────

def _paise(x) -> int:  # noqa: ANN001
    """A Kite price (float or string) to paise: shortest repr, rounded half-up to 0.01."""
    if isinstance(x, float):
        x = numerics.format_float(x)
    return numerics.check_int64(int((Decimal(str(x)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)))


def _ts_ns(dt) -> int:  # noqa: ANN001
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:                       # Kite candles are IST
        dt = dt.replace(tzinfo=timeutil.IST)
    return int(dt.astimezone(timezone.utc).timestamp()) * SEC + dt.microsecond * 1000


def _ist_dt(t_ns: int) -> datetime:
    return timeutil.ns_to_ist(t_ns).replace(tzinfo=None)


_RANGE_ERROR = re.compile(r"interval|exceed|max(imum)?\s*(limit|days|range)|too (large|long|many)", re.I)


def _is_range_error(e: Exception) -> bool:
    return type(e).__name__ == "InputException" and bool(_RANGE_ERROR.search(str(e)))


# ── Fetching ─────────────────────────────────────────────────────────────────

@dataclass
class FetchStats:
    instrument_id: int
    interval: str
    rows: int = 0
    chunks: int = 0
    requests: int = 0
    adjustment: str | None = None          # refetched | flagged_only
    outcome: str = "ok"                    # ok | no_token | stopped (the deadline came between chunks) | error
    detail: str = ""


class _DeadlineReached(Exception):
    """The caller's deadline passed while waiting for the bucket: the request is not sent."""


def _client():  # noqa: ANN202
    if not KITE_AVAILABLE:
        raise HistoryError("kite_client is not importable")
    return kite_client.get_client()


def _call(client, token: int, from_ns: int, to_ns: int, interval: str, *, continuous: bool,  # noqa: ANN001
          stats: FetchStats, bkt: TokenBucket, deadline_ns: int | None = None) -> list[dict]:
    bkt.acquire()
    if deadline_ns is not None and bkt.clock.now_ns() >= deadline_ns:
        raise _DeadlineReached
    stats.requests += 1
    return client.historical_data(token, _ist_dt(from_ns), _ist_dt(to_ns), interval,
                                  continuous=continuous, oi=True) or []


def _store(conn, iid: int, interval: str, rows: list[dict], fetched_on: str) -> int:  # noqa: ANN001
    out = []
    for r in rows:
        out.append((iid, interval, _ts_ns(r["date"]), _paise(r["open"]), _paise(r["high"]), _paise(r["low"]),
                    _paise(r["close"]), int(r.get("volume") or 0),
                    None if r.get("oi") is None else int(r["oi"]), fetched_on))
    conn.executemany("INSERT OR REPLACE INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", out)
    return len(out)


def _chunks(from_ns: int, to_ns: int, days: int) -> list[tuple[int, int]]:
    out, start = [], from_ns
    while start <= to_ns:
        end = min(to_ns, start + days * DAY_NS - 1)
        out.append((start, end))
        start = end + 1
    return out


def _past(clock, deadline_ns: int | None) -> bool:  # noqa: ANN001
    return deadline_ns is not None and clock.now_ns() >= deadline_ns


def _stop(stats: FetchStats) -> None:
    stats.outcome = "stopped"
    stats.detail = (f"stopped at the deadline after {stats.chunks} chunk(s); the fetch log covers only what was "
                    f"stored, so a later top-up asks for the rest")


def _fetch_range(client, iid: int, token: int, interval: str, from_ns: int, to_ns: int, *,  # noqa: ANN001
                 continuous: bool, stats: FetchStats, bkt: TokenBucket, fetched_on: str, cfg: dict,
                 clock, deadline_ns: int | None = None) -> None:
    """Fetch [from_ns, to_ns] in chunks, oldest first; each chunk's bars and its fetch-log row are one transaction,
    stamped with `clock`'s time when stored. `deadline_ns` (on the bucket's clock) is checked before each chunk and
    again after the bucket's wait: past it nothing more is asked and `stats.outcome` is "stopped". The cache stays
    consistent: the log names only the stored chunks, so a later top-up asks for the rest."""
    pending = _chunks(from_ns, to_ns, max_days(interval, cfg))
    while pending:
        if _past(bkt.clock, deadline_ns):
            _stop(stats)
            return
        a, b = pending.pop(0)
        try:
            rows = _call(client, token, a, b, interval, continuous=continuous, stats=stats, bkt=bkt,
                         deadline_ns=deadline_ns)
        except _DeadlineReached:
            _stop(stats)
            return
        except Exception as e:
            if _is_range_error(e):
                span_days = max(1, -(-(b - a + 1) // DAY_NS))
                if span_days <= 1:
                    raise HistoryError(f"Kite rejects even a one-day {interval} range: {e}") from e
                half = max(1, span_days // 2)
                _learn(interval, half)
                pending[:0] = _chunks(a, b, half)
                continue
            raise
        stats.chunks += 1
        with _Tx() as conn:
            n = _store(conn, iid, interval, rows, fetched_on)
            conn.execute("INSERT INTO bar_fetch_log VALUES (?, ?, ?, ?, ?, ?, 'ok')",
                         (iid, interval, a, b, n, clock.now_ns()))
        stats.rows += n


def _is_live(iid: int, today: date) -> bool:
    from stockify.data import instruments
    info = instruments.get(iid) or {}
    if info.get("kind") == "FUT_CONT":
        return True
    if info.get("expiry") and info["expiry"] < today.isoformat():
        return False
    return instruments.token_at(iid, today) is not None


def _check_adjustment(client, iid: int, token: int | None, *, interval: str, today: date,  # noqa: ANN001
                      stats: FetchStats, bkt: TokenBucket, cfg: dict, continuous: bool = False) -> str | None:
    """Compare the last cached bars with a fresh fetch (C-5). Returns the action taken."""
    conn = _connect()
    check_iv = "day" if conn.execute("SELECT 1 FROM bars WHERE instrument_id=? AND interval='day' LIMIT 1",
                                     (iid,)).fetchone() else interval
    cached = conn.execute("SELECT ts_ns, close_paise FROM bars WHERE instrument_id=? AND interval=? "
                          "ORDER BY ts_ns DESC LIMIT ?", (iid, check_iv, int(cfg["adjustment_check_bars"]))).fetchall()
    if not cached or token is None:
        return None
    first, last = cached[-1]["ts_ns"], cached[0]["ts_ns"]
    fresh = {_ts_ns(r["date"]): _paise(r["close"])
             for r in _call(client, token, first, last + (DAY_NS if check_iv == "day" else SEC), check_iv,
                            continuous=continuous, stats=stats, bkt=bkt)}
    ratios = [Decimal(c["close_paise"]) / Decimal(fresh[c["ts_ns"]])
              for c in cached if fresh.get(c["ts_ns"])]
    if not ratios:
        return None
    mean = sum(ratios) / len(ratios)
    tol, min_ratio = Decimal(cfg["adjustment_ratio_tolerance"]), Decimal(cfg["adjustment_min_ratio"])
    if abs(mean - 1) <= min_ratio or any(abs(r - mean) / mean > tol for r in ratios):
        return None
    live = _is_live(iid, today)
    action = "refetched" if live else "flagged_only"
    with _Tx() as c:
        c.execute("INSERT OR REPLACE INTO adjustments_detected VALUES (?, ?, ?, ?, ?)",
                  (iid, today.isoformat(), float(mean), last, action))
        if live:
            c.execute("DELETE FROM bars WHERE instrument_id=?", (iid,))
            c.execute("DELETE FROM bar_fetch_log WHERE instrument_id=?", (iid,))
    log.warning("instrument %d: cached closes differ from Kite by ratio %.6f (%s)", iid, mean, action)
    return action


def fetch(instrument_id: int, interval: str, from_day: date, to_day: date, *, client=None, clock=None,  # noqa: ANN001
          today: date | None = None, check_adjustments: bool = True, bkt: TokenBucket | None = None,
          deadline_ns: int | None = None) -> FetchStats:
    """Cache bars for one instrument over [from_day, to_day] (IST dates, inclusive). `bkt` is the caller's own
    bucket (default: the process-wide one, or one at `historical_rps` on `clock`); `deadline_ns`, on the bucket's
    clock, is checked before anything is asked and between chunks, and a stop is `outcome` "stopped"."""
    from stockify.data import instruments
    if interval not in INTERVALS:
        raise HistoryError(f"unknown interval {interval!r}; choose from {', '.join(INTERVALS)}")
    cfg = limits()
    today = today or timeutil.ist_today()
    if bkt is None:
        bkt = bucket(clock) if clock is None else TokenBucket(cfg["historical_rps"], clock)
    clk = clock or bkt.clock
    stats = FetchStats(instrument_id, interval)
    info = instruments.get(instrument_id)
    if info is None:
        raise HistoryError(f"unknown instrument {instrument_id}")
    if info["kind"] == "FUT_CONT":
        raise HistoryError("use fetch_continuous for continuous futures")
    if _past(bkt.clock, deadline_ns):
        _stop(stats)
        return stats
    client = client or _client()
    token = instruments.token_at(instrument_id, today)
    if check_adjustments:
        stats.adjustment = _check_adjustment(client, instrument_id, token, interval=interval, today=today,
                                             stats=stats, bkt=bkt, cfg=cfg)
    if token is None:
        # K-5: no Kite history for expired contracts; their minute data cannot come back.
        stats.outcome, stats.detail = "no_token", "no current Kite token (expired or delisted); use bhavcopy"
        return stats
    _fetch_range(client, instrument_id, token, interval, timeutil.ist_ns(from_day, "00:00"),
                 timeutil.ist_ns(to_day, "23:59") + 59 * SEC, continuous=False, stats=stats, bkt=bkt,
                 fetched_on=today.isoformat(), cfg=cfg, clock=clk, deadline_ns=deadline_ns)
    return stats


# ── Daily top-up (CW 1.2, moved from the desk's nightly job) ─────────────────

@dataclass
class TopUp:
    """What `top_up_daily` did: `refetched` when a split or bonus wiped the cache and the years were fetched again,
    `requests` sent to Kite, and `outcome` ok | no_token | stopped (the deadline came first)."""
    refetched: bool = False
    requests: int = 0
    outcome: str = "ok"
    detail: str = ""


def years_before(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:                  # 29 February
        return day.replace(year=day.year - years, day=28)


def fetched_back_to(instrument_id: int, from_day: date) -> bool:
    """True when the bar cache's fetch log has a daily fetch starting at or before `from_day`."""
    row = _connect().execute(
        "SELECT 1 FROM bar_fetch_log WHERE instrument_id=? AND interval='day' AND from_ns<=? LIMIT 1",
        (int(instrument_id), timeutil.ist_ns(from_day, "00:00"))).fetchone()
    return row is not None


def last_daily_bar_day(instrument_id: int) -> date | None:
    """The IST date of the newest cached daily bar, or None."""
    ts = _connect().execute("SELECT MAX(ts_ns) FROM bars WHERE instrument_id=? AND interval='day'",
                            (int(instrument_id),)).fetchone()[0]
    return None if ts is None else timeutil.ns_to_ist(int(ts)).date()


def top_up_daily(instrument_id: int, day: date, *, years: int, client, bkt: TokenBucket | None = None,  # noqa: ANN001
                 deadline_ns: int | None = None) -> TopUp:
    """`years` of daily candles up to `day` in the cache; only what is missing is asked for. A history already
    fetched back that far is fetched from its last cached bar on (so an earlier provisional close is replaced); a
    split or bonus refetch (C-5) takes the years again. `bkt` and `deadline_ns` go to `fetch`."""
    from_day = years_before(day, years)
    last = last_daily_bar_day(instrument_id)
    start = from_day
    if last is not None and fetched_back_to(instrument_id, from_day):
        start = min(day, last)
    stats = fetch(instrument_id, "day", start, day, client=client, today=day, bkt=bkt, deadline_ns=deadline_ns)
    out = TopUp(refetched=getattr(stats, "adjustment", None) == "refetched",
                requests=int(getattr(stats, "requests", 0) or 0), outcome=getattr(stats, "outcome", "ok"),
                detail=getattr(stats, "detail", ""))
    if out.refetched and start != from_day and out.outcome == "ok":      # the cache was cleared: the years again
        again = fetch(instrument_id, "day", from_day, day, client=client, today=day, check_adjustments=False,
                      bkt=bkt, deadline_ns=deadline_ns)
        out.requests += int(getattr(again, "requests", 0) or 0)
        out.outcome, out.detail = getattr(again, "outcome", "ok"), getattr(again, "detail", "")
    return out


def continuous_instrument(underlying: str, today: date) -> int:
    """The synthetic `CONT:<underlying>:FUT` instrument (created on first use)."""
    from stockify.data import refdata
    und = underlying.upper()
    with refdata.transaction() as conn:
        hit = conn.execute("SELECT instrument_id FROM instruments WHERE exchange='NFO' AND underlying=? "
                           "AND kind='FUT_CONT'", (und,)).fetchone()
        if hit:
            return hit["instrument_id"]
        uid = conn.execute("SELECT MAX(underlying_id) AS u FROM instruments WHERE underlying=? AND kind='FUT'",
                           (und,)).fetchone()["u"]
        return conn.execute(
            "INSERT INTO instruments (exchange, kind, tradingsymbol, underlying, underlying_id, first_seen, "
            "last_seen, source) VALUES ('NFO', 'FUT_CONT', ?, ?, ?, ?, ?, 'synthetic')",
            (f"CONT:{und}:FUT", und, uid, today.isoformat(), today.isoformat())).lastrowid


def fetch_continuous(underlying: str, from_day: date, to_day: date, *, client=None, clock=None,  # noqa: ANN001
                     today: date | None = None, check_adjustments: bool = True) -> FetchStats:
    """Day candles across expired contracts via `continuous=1` on the nearest live future (K-5)."""
    from stockify.data import instruments
    cfg = limits()
    today = today or timeutil.ist_today()
    live = [e for e in instruments.expiries(underlying, "FUT", today)]
    token = fut = None
    for e in live:
        fut = instruments.derivative(underlying, "FUT", e)
        token = instruments.token_at(fut, today) if fut else None
        if token:
            break
    if not token:
        raise HistoryError(f"no live {underlying} future with a Kite token on {today}")
    iid = continuous_instrument(underlying, today)
    bkt = bucket(clock) if clock is None else TokenBucket(cfg["historical_rps"], clock)
    client = client or _client()
    stats = FetchStats(iid, "day")
    if check_adjustments:
        stats.adjustment = _check_adjustment(client, iid, token, interval="day", today=today, stats=stats,
                                             bkt=bkt, cfg=cfg, continuous=True)
    clk = clock or bkt.clock
    _fetch_range(client, iid, token, "day", timeutil.ist_ns(from_day, "00:00"),
                 timeutil.ist_ns(to_day, "23:59") + 59 * SEC, continuous=True, stats=stats, bkt=bkt,
                 fetched_on=today.isoformat(), cfg=cfg, clock=clk)
    return stats


def sweep(*, client=None, clock=None, today: date | None = None) -> list[FetchStats]:  # noqa: ANN001
    """Nightly corporate-action check for every cached live instrument (D-18)."""
    from stockify.data import instruments
    cfg = limits()
    today = today or timeutil.ist_today()
    bkt = bucket(clock) if clock is None else TokenBucket(cfg["historical_rps"], clock)
    client = client or _client()
    out = []
    for (iid,) in _connect().execute("SELECT DISTINCT instrument_id FROM bars ORDER BY instrument_id").fetchall():
        if not _is_live(iid, today):
            continue
        info = instruments.get(iid) or {}
        cont = info.get("kind") == "FUT_CONT"
        if cont:
            fut = next((instruments.derivative(info["underlying"], "FUT", e)
                        for e in instruments.expiries(info["underlying"], "FUT", today)), None)
            token = instruments.token_at(fut, today) if fut else None
        else:
            token = instruments.token_at(iid, today)
        stats = FetchStats(iid, "day")
        stats.adjustment = _check_adjustment(client, iid, token, interval="day", today=today, stats=stats,
                                             bkt=bkt, cfg=cfg, continuous=cont)
        out.append(stats)
    return out


# ── Reading ──────────────────────────────────────────────────────────────────

def bars(instrument_id: int, interval: str, from_ns: int | None = None, to_ns: int | None = None) -> list[dict]:
    """Cached bars in ts order (adjusted basis)."""
    q = "SELECT * FROM bars WHERE instrument_id=? AND interval=?"
    args: list = [instrument_id, interval]
    if from_ns is not None:
        q, args = q + " AND ts_ns>=?", args + [from_ns]
    if to_ns is not None:
        q, args = q + " AND ts_ns<=?", args + [to_ns]
    return [dict(r) for r in _connect().execute(q + " ORDER BY ts_ns", args)]


def adjustments(instrument_id: int | None = None) -> list[dict]:
    q, args = "SELECT * FROM adjustments_detected", ()
    if instrument_id is not None:
        q, args = q + " WHERE instrument_id=?", (instrument_id,)
    return [dict(r) for r in _connect().execute(q + " ORDER BY detected_on, instrument_id", args)]


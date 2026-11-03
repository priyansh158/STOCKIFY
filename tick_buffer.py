"""
SQLite ringbuffer for live Kite ticks.

The KiteTicker worker (kite_ticker_worker.py) writes ticks here on a
background thread. Browser clients receive ticks live via the WebSocket
fanout in server/services/ticker_bridge.py; this buffer remains a
server-side persistence layer for replay and rate-limited consumers.
WAL mode lets the writer and readers operate without locking each other.

Idempotency: PRIMARY KEY (instrument_token, ts) absorbs duplicate ticks
that KiteTicker re-fires on socket reconnect storms.

Retention: trim_old() keeps the last 8 hours of data per row count safety.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Iterable, Optional

import stockify_paths


_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ticks (
    instrument_token INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    ltp REAL NOT NULL,
    volume INTEGER,
    oi INTEGER,
    PRIMARY KEY (instrument_token, ts)
);
CREATE INDEX IF NOT EXISTS idx_ticks_token_ts ON ticks(instrument_token, ts DESC);

CREATE TABLE IF NOT EXISTS subscriptions (
    instrument_token INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,        -- 'macro' | 'analysis' | 'portfolio'
    added_ts INTEGER NOT NULL
);
"""

_RETENTION_SECONDS = 8 * 3600  # 8 hours per token


def _db_path():
    return stockify_paths.state_dir() / "ticks.sqlite"


def _connect() -> sqlite3.Connection:
    """Process-wide connection. SQLite is fine for one writer + many readers
    under WAL mode."""
    global _conn
    if _conn is not None:
        return _conn
    with _lock:
        if _conn is not None:
            return _conn
        conn = sqlite3.connect(str(_db_path()), timeout=10.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        _conn = conn
        return _conn


def init_db() -> None:
    """Idempotent — call at app boot and from worker."""
    _connect()


# ── Writers (called from KiteTicker worker thread) ────────────────────────────

def write_ticks(ticks: Iterable[dict]) -> int:
    """Bulk upsert. `ticks` is a list of kiteconnect tick dicts.

    Returns the number of rows written. Idempotent on (token, ts).
    """
    rows = []
    now_floor = int(time.time())
    for t in ticks:
        token = t.get("instrument_token")
        if not token:
            continue
        # Prefer the exchange timestamp, fall back to wall-clock.
        exch_ts = t.get("exchange_timestamp")
        if exch_ts is not None:
            try:
                ts = int(exch_ts.timestamp()) if hasattr(exch_ts, "timestamp") else int(exch_ts)
            except Exception:
                ts = now_floor
        else:
            ts = now_floor
        ltp = t.get("last_price") or t.get("ltp")
        if ltp is None:
            continue
        rows.append((
            int(token), ts, float(ltp),
            int(t.get("volume_traded") or t.get("volume") or 0) or None,
            int(t.get("oi") or 0) or None,
        ))
    if not rows:
        return 0
    conn = _connect()
    try:
        with _lock:
            conn.executemany(
                "INSERT OR REPLACE INTO ticks "
                "(instrument_token, ts, ltp, volume, oi) VALUES (?,?,?,?,?)",
                rows,
            )
            conn.commit()
    except Exception:
        return 0
    return len(rows)


def trim_old(retention_seconds: int = _RETENTION_SECONDS) -> int:
    """Delete ticks older than `retention_seconds`. Returns rows removed."""
    cutoff = int(time.time()) - retention_seconds
    conn = _connect()
    try:
        with _lock:
            cur = conn.execute("DELETE FROM ticks WHERE ts < ?", (cutoff,))
            conn.commit()
            return cur.rowcount or 0
    except Exception:
        return 0


def checkpoint() -> None:
    """Truncate the WAL file. Call periodically (e.g. once an hour)."""
    conn = _connect()
    try:
        with _lock:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass


# ── Readers (must be cheap — called from request handlers + WS bridge) ───────

def latest_tick(instrument_token: int) -> dict | None:
    """Most recent tick for an instrument, or None."""
    if not instrument_token:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT ts, ltp, volume, oi FROM ticks "
            "WHERE instrument_token = ? ORDER BY ts DESC LIMIT 1",
            (int(instrument_token),)
        ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    return {"ts": int(row[0]), "ltp": float(row[1]),
            "volume": row[2], "oi": row[3]}


def latest_ticks_bulk(tokens: list[int]) -> dict[int, dict]:
    """Latest tick for each of N tokens — one query."""
    if not tokens:
        return {}
    conn = _connect()
    placeholders = ",".join("?" for _ in tokens)
    try:
        cur = conn.execute(
            f"SELECT instrument_token, MAX(ts) FROM ticks "
            f"WHERE instrument_token IN ({placeholders}) "
            f"GROUP BY instrument_token",
            tuple(int(t) for t in tokens)
        )
        latest_map = {int(r[0]): int(r[1]) for r in cur.fetchall()}
        if not latest_map:
            return {}
        # Second query for the actual rows
        params = []
        clauses = []
        for tok, ts in latest_map.items():
            clauses.append("(instrument_token = ? AND ts = ?)")
            params.extend([tok, ts])
        cur = conn.execute(
            f"SELECT instrument_token, ts, ltp, volume, oi FROM ticks "
            f"WHERE {' OR '.join(clauses)}",
            params
        )
        return {int(r[0]): {"ts": int(r[1]), "ltp": float(r[2]),
                            "volume": r[3], "oi": r[4]} for r in cur.fetchall()}
    except Exception:
        return {}


# ── Subscriptions (persistent intent — survives worker restarts) ──────────────

def add_subscription(instrument_token: int, kind: str = "analysis") -> None:
    if not instrument_token:
        return
    conn = _connect()
    try:
        with _lock:
            conn.execute(
                "INSERT OR REPLACE INTO subscriptions "
                "(instrument_token, kind, added_ts) VALUES (?, ?, ?)",
                (int(instrument_token), kind, int(time.time()))
            )
            conn.commit()
    except Exception:
        pass


def remove_subscription(instrument_token: int) -> None:
    if not instrument_token:
        return
    conn = _connect()
    try:
        with _lock:
            conn.execute(
                "DELETE FROM subscriptions WHERE instrument_token = ?",
                (int(instrument_token),)
            )
            conn.commit()
    except Exception:
        pass


def remove_subscriptions_of_kind(kind: str) -> None:
    conn = _connect()
    try:
        with _lock:
            conn.execute("DELETE FROM subscriptions WHERE kind = ?", (kind,))
            conn.commit()
    except Exception:
        pass


def subscribed_tokens(kind: str | None = None) -> set[int]:
    conn = _connect()
    try:
        if kind:
            cur = conn.execute(
                "SELECT instrument_token FROM subscriptions WHERE kind = ?", (kind,))
        else:
            cur = conn.execute("SELECT instrument_token FROM subscriptions")
        return {int(r[0]) for r in cur.fetchall()}
    except Exception:
        return set()


def total_rows() -> int:
    conn = _connect()
    try:
        return int(conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0])
    except Exception:
        return 0

"""The only access layer for `lab.sqlite` (A1_schemas.md §4).

Same pattern as `geo_alerts.py`: one process-wide connection, WAL so the API,
the daemon and run workers can share the file, `check_same_thread=False`,
`sqlite3.Row` rows. Tables are created in M0; the read/write functions for
each arrive with the milestone that owns the table (runs M3, sessions M5,
research M6).

Schema changes to this file are migrations, never drops (I-13).
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Optional

from stockify import paths

SCHEMA_VERSION = 1

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

_SCHEMA = """
-- runs (M3)
CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, name TEXT, family_id TEXT, status TEXT,   -- queued|running|done|failed|cancelled
  created_ns INTEGER, started_ns INTEGER, finished_ns INTEGER, folder TEXT,
  bundle_sha256 TEXT, engine_build_id TEXT, impl TEXT,                                  -- engine | oracle
  config_json TEXT, metrics_json TEXT, labels_json TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS run_orders (run_id TEXT, order_id INTEGER, row_json TEXT, PRIMARY KEY (run_id, order_id));
CREATE TABLE IF NOT EXISTS run_fills (run_id TEXT, fill_id INTEGER, order_id INTEGER, t_ns INTEGER, instrument_id INTEGER,
  side TEXT, qty INTEGER, price_paise INTEGER, PRIMARY KEY (run_id, fill_id));
CREATE TABLE IF NOT EXISTS run_equity (run_id TEXT, trade_date TEXT, nav_paise INTEGER, cash_paise INTEGER,
  margin_paise INTEGER, PRIMARY KEY (run_id, trade_date));
CREATE TABLE IF NOT EXISTS run_charges (run_id TEXT, row_no INTEGER, kind TEXT, order_id INTEGER, trade_date TEXT,
  components_json TEXT, total_paise INTEGER, PRIMARY KEY (run_id, row_no));

-- research (M6)
CREATE TABLE IF NOT EXISTS trial_families (family_id TEXT PRIMARY KEY, description TEXT, objective TEXT, created_ns INTEGER);
CREATE TABLE IF NOT EXISTS holdout_unlocks (unlock_ns INTEGER, run_id TEXT, family_id TEXT, reason TEXT);

-- sessions (M5)
CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, name TEXT, family_id TEXT, status TEXT, pause_reason TEXT,
  config_json TEXT, created_ns INTEGER, stopped_ns INTEGER);
CREATE TABLE IF NOT EXISTS session_days (session_id TEXT, trade_date TEXT, bundle_sha256 TEXT, journal_path TEXT,
  sod_state_json TEXT, eod_state_json TEXT, coverage REAL, parity TEXT,   -- pass | fail | pending
  PRIMARY KEY (session_id, trade_date));
CREATE TABLE IF NOT EXISTS session_timeline (session_id TEXT, engine_ns INTEGER, wall_ns INTEGER, kind TEXT, detail_json TEXT);
CREATE TABLE IF NOT EXISTS session_orders (session_id TEXT, trade_date TEXT, order_id INTEGER, row_json TEXT,
  PRIMARY KEY (session_id, trade_date, order_id));
CREATE TABLE IF NOT EXISTS session_order_events (session_id TEXT, trade_date TEXT, event_no INTEGER, row_json TEXT,
  PRIMARY KEY (session_id, trade_date, event_no));
CREATE TABLE IF NOT EXISTS session_fills (session_id TEXT, trade_date TEXT, fill_id INTEGER, row_json TEXT,
  PRIMARY KEY (session_id, trade_date, fill_id));
CREATE TABLE IF NOT EXISTS session_charges (session_id TEXT, trade_date TEXT, row_no INTEGER, row_json TEXT,
  PRIMARY KEY (session_id, trade_date, row_no));
CREATE TABLE IF NOT EXISTS session_positions_eod (session_id TEXT, trade_date TEXT, row_no INTEGER, row_json TEXT,
  PRIMARY KEY (session_id, trade_date, row_no));
CREATE TABLE IF NOT EXISTS session_equity (session_id TEXT, trade_date TEXT, nav_paise INTEGER, cash_paise INTEGER,
  margin_paise INTEGER, PRIMARY KEY (session_id, trade_date));
CREATE TABLE IF NOT EXISTS margin_live (session_id TEXT, wall_ns INTEGER, portfolio_sha256 TEXT, kite_json TEXT,
  model_json TEXT);
CREATE TABLE IF NOT EXISTS parity_results (session_id TEXT, trade_date TEXT, kind TEXT,     -- journal | recorder
  outcome TEXT, summary_json TEXT, created_ns INTEGER, PRIMARY KEY (session_id, trade_date, kind));

-- margin calibration (M4.9)
CREATE TABLE IF NOT EXISTS margin_calibration (sample_id INTEGER PRIMARY KEY, wall_ns INTEGER, underlying TEXT,
  portfolio_json TEXT, market_json TEXT, kite_json TEXT, t0_paise INTEGER, t1_paise INTEGER, t1_breakdown_json TEXT);

-- recorder health (M1.5); rows older than 7 days are pruned daily
CREATE TABLE IF NOT EXISTS recorder_status (wall_ns INTEGER PRIMARY KEY, connected INTEGER, subs_full INTEGER, subs_quote INTEGER,
  packets_per_s REAL, queue_depth INTEGER, writer_lag_ms REAL, last_packet_age_ms_json TEXT, bytes_today INTEGER);
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _db_path():
    return paths.lab_db()


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    with _lock:
        if _conn is not None:
            return _conn
        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
        _conn = conn
        return _conn


def init_db() -> None:
    _connect()


def schema_version() -> int:
    row = _connect().execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    return int(row["value"]) if row else 0


def table_names() -> list[str]:
    rows = _connect().execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]


# ── recorder health (M1.5; A1 §4 recorder_status) ────────────────────────────

RECORDER_STATUS_FIELDS = ("wall_ns", "connected", "subs_full", "subs_quote", "packets_per_s",
                          "queue_depth", "writer_lag_ms", "last_packet_age_ms_json", "bytes_today")


def write_recorder_status(row: dict) -> None:
    conn = _connect()
    with _lock:
        conn.execute(
            f"INSERT OR REPLACE INTO recorder_status ({', '.join(RECORDER_STATUS_FIELDS)}) "
            f"VALUES ({', '.join('?' * len(RECORDER_STATUS_FIELDS))})",
            tuple(row.get(k) for k in RECORDER_STATUS_FIELDS))
        conn.commit()


def latest_recorder_status() -> dict | None:
    row = _connect().execute(
        "SELECT * FROM recorder_status ORDER BY wall_ns DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def prune_recorder_status(older_than_ns: int) -> int:
    """Health rows are kept 7 days (A1 §4); the scheduler calls this daily."""
    conn = _connect()
    with _lock:
        cur = conn.execute("DELETE FROM recorder_status WHERE wall_ns < ?", (older_than_ns,))
        conn.commit()
        return cur.rowcount

"""The only access layer for `lab.sqlite` (A1_schemas.md §4).

Same pattern as `geo_alerts.py`: one process-wide connection, WAL so the API,
the daemon and run workers can share the file, `check_same_thread=False`,
`sqlite3.Row` rows. Tables are created in M0; the read/write functions for
each arrive with the milestone that owns the table (runs M3, sessions M5,
research M6).

Schema changes are numbered files in stockify/data/migrations/lab, never drops (I-13).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Optional

from stockify import paths
from stockify.data import migrate

SCHEMA_VERSION = 2   # = the number of files in stockify/data/migrations/lab

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None



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
        try:
            version = migrate.apply(conn, "lab")
            if version != SCHEMA_VERSION:
                raise migrate.MigrationError(
                    f"lab migrations end at {version}, store.SCHEMA_VERSION is {SCHEMA_VERSION}")
            conn.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                         (str(version),))
            conn.commit()
        except Exception:
            conn.close()
            raise
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


MARGIN_CALIBRATION_FIELDS = ("wall_ns", "underlying", "portfolio_json", "market_json", "kite_json", "t0_paise",
                             "t1_paise", "t1_breakdown_json")


def add_margin_calibration(row: dict) -> int:
    """One M4.9 calibration sample (A1 §4: margin_calibration); keys are column names. Returns its sample_id.
    Never holds credentials: the calibration tool stores only the portfolio, the quotes and Kite's margin answer."""
    conn = _connect()
    with _lock:
        cur = conn.execute(
            f"INSERT INTO margin_calibration ({', '.join(MARGIN_CALIBRATION_FIELDS)}) "
            f"VALUES ({', '.join('?' * len(MARGIN_CALIBRATION_FIELDS))})",
            tuple(row.get(k) for k in MARGIN_CALIBRATION_FIELDS))
        conn.commit()
        return int(cur.lastrowid)


def margin_calibrations(underlying: str | None = None) -> list[dict]:
    """Calibration samples in insertion order, optionally for one underlying (the history is kept: M4.9)."""
    sql, args = "SELECT * FROM margin_calibration", ()
    if underlying is not None:
        sql, args = sql + " WHERE underlying = ?", (underlying,)
    return [dict(r) for r in _connect().execute(sql + " ORDER BY sample_id", args).fetchall()]


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


# ── runs (M3.11; A1 §4 runs, run_orders, run_fills, run_equity, run_charges) ─

RUN_FIELDS = ("run_id", "name", "family_id", "status", "created_ns", "started_ns", "finished_ns", "folder",
              "bundle_sha256", "engine_build_id", "impl", "config_json", "metrics_json", "labels_json", "error")

_RUN_TABLES = {  # table name in run_rows → (SQL table, primary-key order, columns after run_id)
    "orders": ("run_orders", "order_id", ("order_id", "row_json")),
    "fills": ("run_fills", "fill_id", ("fill_id", "order_id", "t_ns", "instrument_id", "side", "qty", "price_paise")),
    "equity": ("run_equity", "trade_date", ("trade_date", "nav_paise", "cash_paise", "margin_paise")),
    "charges": ("run_charges", "row_no", ("row_no", "kind", "order_id", "trade_date", "components_json",
                                          "total_paise")),
}


def create_run(row: dict) -> None:
    """Insert a `runs` row; keys are column names, missing ones NULL. status defaults to queued, created_ns to
    now. An unknown key is a ValueError."""
    from stockify import timeutil
    unknown = set(row) - set(RUN_FIELDS)
    if unknown:
        raise ValueError(f"not runs columns: {sorted(unknown)}")
    full = {"status": "queued", "created_ns": timeutil.now_ns(), **row}
    conn = _connect()
    with _lock:
        conn.execute(f"INSERT INTO runs ({', '.join(RUN_FIELDS)}) VALUES ({', '.join('?' * len(RUN_FIELDS))})",
                     tuple(full.get(k) for k in RUN_FIELDS))
        conn.commit()


def update_run(run_id: str, *, only_if_status: str | tuple[str, ...] | None = None, **fields) -> bool:
    """Update columns of one run. With only_if_status, only when the current status is one of them (a runner
    must never overwrite a cancelled row). Returns whether a row changed."""
    unknown = set(fields) - (set(RUN_FIELDS) - {"run_id"})
    if unknown:
        raise ValueError(f"not updatable runs columns: {sorted(unknown)}")
    if not fields:
        return False
    sql = f"UPDATE runs SET {', '.join(f'{k} = ?' for k in fields)} WHERE run_id = ?"
    args: list = [*fields.values(), run_id]
    if only_if_status is not None:
        allowed = (only_if_status,) if isinstance(only_if_status, str) else tuple(only_if_status)
        sql += f" AND status IN ({', '.join('?' * len(allowed))})"
        args += allowed
    conn = _connect()
    with _lock:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.rowcount > 0


def get_run(run_id: str) -> dict | None:
    row = _connect().execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def list_runs(*, status: str | None = None, family_id: str | None = None, offset: int = 0,
              limit: int = 50) -> list[dict]:
    """Newest created_ns first."""
    where, args = [], []
    if status is not None:
        where.append("status = ?")
        args.append(status)
    if family_id is not None:
        where.append("family_id = ?")
        args.append(family_id)
    sql = "SELECT * FROM runs" + (f" WHERE {' AND '.join(where)}" if where else "")
    sql += " ORDER BY created_ns DESC, run_id LIMIT ? OFFSET ?"
    return [dict(r) for r in _connect().execute(sql, (*args, int(limit), int(offset))).fetchall()]


def replace_run_rows(run_id: str, *, orders: list[dict], fills: list[dict], equity: list[dict],
                     charges: list[dict]) -> None:
    """One transaction: delete the run's child rows, insert the new ones (re-import is idempotent). Derived
    rows only; tick tables are never touched. `orders` items are the order dicts (stored as row_json, keyed
    by order_id); the others carry their table's columns."""
    conn = _connect()
    with _lock:
        try:
            _write_children(conn, run_id, orders, fills, equity, charges)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def _write_children(conn, run_id: str, orders, fills, equity, charges) -> None:  # noqa: ANN001
    """Inside the caller's transaction: replace the run's child rows."""
    import json
    for table, _, _ in _RUN_TABLES.values():
        conn.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
    conn.executemany("INSERT INTO run_orders (run_id, order_id, row_json) VALUES (?, ?, ?)",
                     [(run_id, o["order_id"], json.dumps(o, sort_keys=True)) for o in orders])
    for key, rows in (("fills", fills), ("equity", equity), ("charges", charges)):
        table, _, cols = _RUN_TABLES[key]
        conn.executemany(f"INSERT INTO {table} (run_id, {', '.join(cols)}) "
                         f"VALUES (?, {', '.join('?' * len(cols))})",
                         [(run_id, *(r.get(c) for c in cols)) for r in rows])


def finish_run(run_id: str, fields: dict, *, orders: list[dict], fills: list[dict], equity: list[dict],
               charges: list[dict]) -> bool:
    """A runner's last write, in one transaction: update the row (normally status done) and replace its child
    rows, only while the row is still `running`. A cancelled row gets neither; if any insert fails, nothing
    changes. Returns whether the run was finished."""
    unknown = set(fields) - (set(RUN_FIELDS) - {"run_id"})
    if unknown:
        raise ValueError(f"not updatable runs columns: {sorted(unknown)}")
    conn = _connect()
    with _lock:
        try:
            cur = conn.execute(f"UPDATE runs SET {', '.join(f'{k} = ?' for k in fields)} "
                               "WHERE run_id = ? AND status = 'running'", (*fields.values(), run_id))
            if cur.rowcount == 0:
                conn.rollback()
                return False
            _write_children(conn, run_id, orders, fills, equity, charges)
            conn.commit()
            return True
        except BaseException:
            conn.rollback()
            raise


def run_rows(run_id: str, table: str, *, offset: int = 0, limit: int = 500) -> list[dict]:
    """Child rows of a run in primary-key order; table is orders | fills | equity | charges. For orders the
    row_json is parsed into the dict."""
    import json
    if table not in _RUN_TABLES:
        raise ValueError(f"unknown run table {table!r}")
    sql_table, key, cols = _RUN_TABLES[table]
    rows = _connect().execute(f"SELECT {', '.join(cols)} FROM {sql_table} WHERE run_id = ? ORDER BY {key} "
                              f"LIMIT ? OFFSET ?", (run_id, int(limit), int(offset))).fetchall()
    if table == "orders":
        return [json.loads(r["row_json"]) for r in rows]
    return [dict(r) for r in rows]


# ── sessions (M5.2; A1 §4 sessions, session_days, session_timeline, session_commands) ──

SESSION_DAY_FIELDS = ("bundle_sha256", "journal_path", "sod_state_json", "eod_state_json", "coverage", "parity",
                      "status", "finalised_ns", "coverage_json", "labels_json", "last_frame_no",
                      "last_frame_recv_ns")


def _session_row(row) -> dict:
    d = dict(row)
    d["pause_reasons"] = sorted(set(json.loads(d.pop("pause_reasons_json", None) or "[]")))
    d["config"] = json.loads(d["config_json"]) if d.get("config_json") else None
    return d


def create_session(session_id: str, name: str, cfg: dict, created_ns: int) -> None:
    """Insert a CREATED session. The id becomes a folder name (journals), so it must be one safe path segment
    (else ValueError); a duplicate id raises sqlite3.IntegrityError."""
    if not paths.safe_segment(session_id, 128):
        raise ValueError(f"not a valid session id: {session_id!r}")
    conn = _connect()
    with _lock:
        conn.execute("INSERT INTO sessions (session_id, name, status, pause_reason, pause_reasons_json, config_json, "
                     "created_ns) VALUES (?, ?, 'CREATED', NULL, '[]', ?, ?)",
                     (session_id, name, json.dumps(cfg, sort_keys=True, separators=(",", ":")), int(created_ns)))
        conn.commit()


def get_session(session_id: str) -> dict | None:
    """The session row; `pause_reasons` is a sorted list and `config` the parsed session config."""
    row = _connect().execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    return _session_row(row) if row else None


def list_sessions(*, status: str | None = None) -> list[dict]:
    """Oldest created first."""
    sql, args = "SELECT * FROM sessions", ()
    if status is not None:
        sql, args = sql + " WHERE status = ?", (status,)
    return [_session_row(r) for r in
            _connect().execute(sql + " ORDER BY created_ns, session_id", args).fetchall()]


def set_session_state(session_id: str, status: str, reasons: list[str]) -> None:
    """Write status and the pause-reason set (stored sorted and unique; `pause_reason` keeps the first for
    display). The state machine itself lives in stockify.daemon.lifecycle."""
    rs = sorted(set(reasons))
    conn = _connect()
    with _lock:
        cur = conn.execute("UPDATE sessions SET status = ?, pause_reason = ?, pause_reasons_json = ? "
                           "WHERE session_id = ?",
                           (status, rs[0] if rs else None, json.dumps(rs), session_id))
        conn.commit()
        if cur.rowcount == 0:
            raise KeyError(session_id)


def set_session_stopped_ns(session_id: str, stopped_ns: int) -> None:
    conn = _connect()
    with _lock:
        conn.execute("UPDATE sessions SET stopped_ns = ? WHERE session_id = ?", (int(stopped_ns), session_id))
        conn.commit()


def add_timeline(session_id: str, kind: str, detail: dict, *, wall_ns: int, engine_ns: int | None) -> None:
    conn = _connect()
    with _lock:
        conn.execute("INSERT INTO session_timeline (session_id, engine_ns, wall_ns, kind, detail_json) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (session_id, engine_ns, int(wall_ns), kind, json.dumps(detail, sort_keys=True)))
        conn.commit()


def timeline(session_id: str, *, offset: int = 0, limit: int = 200) -> list[dict]:
    """Rows in the order they were written, each with `detail` parsed."""
    rows = _connect().execute(
        "SELECT session_id, engine_ns, wall_ns, kind, detail_json FROM session_timeline WHERE session_id = ? "
        "ORDER BY rowid LIMIT ? OFFSET ?", (session_id, int(limit), int(offset))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(d.pop("detail_json") or "null")
        out.append(d)
    return out


def upsert_session_day(session_id: str, trade_date: str, **fields) -> None:
    """Create or update one (session, day) row, touching only the given columns; an unknown column is a
    ValueError."""
    unknown = set(fields) - set(SESSION_DAY_FIELDS)
    if unknown:
        raise ValueError(f"not session_days columns: {sorted(unknown)}")
    cols = list(fields)
    action = (f"UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in cols)}" if cols else "NOTHING")
    conn = _connect()
    with _lock:
        conn.execute(
            f"INSERT INTO session_days (session_id, trade_date{''.join(', ' + c for c in cols)}) "
            f"VALUES (?, ?{', ?' * len(cols)}) ON CONFLICT (session_id, trade_date) DO {action}",
            (session_id, trade_date, *fields.values()))
        conn.commit()


def get_session_day(session_id: str, trade_date: str) -> dict | None:
    row = _connect().execute("SELECT * FROM session_days WHERE session_id = ? AND trade_date = ?",
                             (session_id, trade_date)).fetchone()
    return dict(row) if row else None


def add_command(session_id: str | None, command: str, args: dict | None, created_ns: int) -> int:
    """Queue an owner command for the daemon (session_id None = every session). Returns its cmd_id."""
    conn = _connect()
    with _lock:
        cur = conn.execute("INSERT INTO session_commands (session_id, command, args_json, created_ns) "
                           "VALUES (?, ?, ?, ?)",
                           (session_id, command, None if args is None else json.dumps(args, sort_keys=True),
                            int(created_ns)))
        conn.commit()
        return int(cur.lastrowid)


def pending_commands() -> list[dict]:
    """Commands not yet marked, oldest first (created_ns, then cmd_id); `args` parsed."""
    rows = _connect().execute("SELECT * FROM session_commands WHERE applied_ns IS NULL "
                              "ORDER BY created_ns, cmd_id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        raw = d.pop("args_json")
        d["args"] = None if raw is None else json.loads(raw)
        out.append(d)
    return out


def mark_command(cmd_id: int, applied_ns: int, result: str) -> None:
    conn = _connect()
    with _lock:
        conn.execute("UPDATE session_commands SET applied_ns = ?, result = ? WHERE cmd_id = ?",
                     (int(applied_ns), result, int(cmd_id)))
        conn.commit()

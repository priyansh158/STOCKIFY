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

SCHEMA_VERSION = 3   # = the number of files in stockify/data/migrations/lab

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

SESSION_STATES = ("CREATED", "ARMED", "RUNNING", "PAUSED", "STOPPED")
SESSION_PAUSE_REASONS = ("owner", "stop_all", "token_expired", "parity_failure", "determinism", "error", "stopped")

SESSION_DAY_FIELDS = ("bundle_sha256", "journal_path", "sod_state_json", "eod_state_json", "coverage", "parity",
                      "status", "finalised_ns", "coverage_json", "labels_json", "last_frame_no",
                      "last_frame_recv_ns")


def _session_row(row) -> dict:
    d = dict(row)
    d["pause_reasons"] = sorted(set(json.loads(d.pop("pause_reasons_json", None) or "[]")))
    d["config"] = json.loads(d["config_json"]) if d.get("config_json") else None
    return d


def check_session_state(status: str, reasons: list[str]) -> list[str]:
    """Return `reasons` as a list if (status, reasons) is a state the engine can carry, else raise ValueError
    (D-70, A4 3.11): reasons are known, non-empty, strictly ascending; RUNNING (enabled) has none; PAUSED and
    STOPPED (disabled) have some; only STOPPED carries `stopped`, and it always does."""
    if status not in SESSION_STATES:
        raise ValueError(f"not a session state: {status!r}")
    rs = list(reasons)
    for r in rs:
        if r not in SESSION_PAUSE_REASONS:
            raise ValueError(f"not a pause reason: {r!r}")
    if rs != sorted(set(rs)):
        raise ValueError(f"pause reasons must be sorted and unique: {rs!r}")
    if status == "RUNNING" and rs:
        raise ValueError("a RUNNING session has no pause reasons")
    if status == "PAUSED" and not rs:
        raise ValueError("a PAUSED session needs a pause reason")
    if (status == "STOPPED") != ("stopped" in rs):
        raise ValueError("`stopped` is a reason of STOPPED sessions only, and every STOPPED session has it")
    return rs


def create_session(session_id: str, name: str, cfg: dict, created_ns: int) -> None:
    """Insert a CREATED session. The id becomes a folder name (journals), so it must be one safe path segment
    (else ValueError); a duplicate id, ignoring case, raises sqlite3.IntegrityError."""
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


def _write_state(conn: sqlite3.Connection, session_id: str, status: str, reasons: list[str],
                 stopped_ns: int | None = None) -> None:
    rs = check_session_state(status, reasons)
    cur = conn.execute("UPDATE sessions SET status = ?, pause_reason = ?, pause_reasons_json = ?, "
                       "stopped_ns = COALESCE(stopped_ns, ?) WHERE session_id = ?",
                       (status, rs[0] if rs else None, json.dumps(rs), stopped_ns, session_id))
    if cur.rowcount == 0:
        raise KeyError(session_id)


def set_session_state(session_id: str, status: str, reasons: list[str]) -> None:
    """Write status and the pause-reason set, refusing a combination the engine could not carry
    (check_session_state: ValueError) and an unknown session (KeyError). The state machine, with its timeline
    rows, lives in stockify.daemon.lifecycle, which goes through `transition`."""
    conn = _connect()
    with _lock:
        try:
            _write_state(conn, session_id, status, reasons)
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _write_timeline(conn: sqlite3.Connection, session_id: str, kind: str, detail: dict, wall_ns: int,
                    engine_ns: int | None) -> None:
    cur = conn.execute(
        "INSERT INTO session_timeline (session_id, engine_ns, wall_ns, kind, detail_json) "
        "SELECT ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM sessions WHERE session_id = ?)",
        (session_id, engine_ns, int(wall_ns), kind, json.dumps(detail, sort_keys=True), session_id))
    if cur.rowcount == 0:
        raise KeyError(session_id)


def transition(session_id: str, decide, *, wall_ns: int, engine_ns: int | None = None,
               conn: sqlite3.Connection | None = None) -> tuple[dict, bool]:
    """One atomic session state change: BEGIN IMMEDIATE, read the session, `decide(session)` returns the new
    (status, reasons) or None for no change (it may raise to refuse), write the state and its `state` timeline
    row {from, to, reasons}, commit. A crash or an exception leaves neither; two connections (processes) can
    never overwrite each other's change, because the read happens under the write lock. A move into STOPPED
    stamps stopped_ns = wall_ns, once. `conn` is another connection to the lab file (tests, other processes);
    default is this process's. Returns (the session after, whether anything changed); an unknown session is a
    KeyError."""
    own = conn is None
    conn = _connect() if own else conn
    if own:
        _lock.acquire()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            if row is None:
                raise KeyError(session_id)
            before = _session_row(row)
            new = decide(before)
            if new is None or (new[0], sorted(new[1])) == (before["status"], before["pause_reasons"]):
                conn.rollback()
                return before, False
            status, reasons = new[0], list(new[1])
            _write_state(conn, session_id, status, reasons, wall_ns if status == "STOPPED" else None)
            _write_timeline(conn, session_id, "state",
                            {"from": before["status"], "to": status, "reasons": reasons}, wall_ns, engine_ns)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        after = dict(before)
        after.update(status=status, pause_reasons=reasons, pause_reason=reasons[0] if reasons else None)
        if status == "STOPPED" and after.get("stopped_ns") is None:
            after["stopped_ns"] = int(wall_ns)
        return after, True
    finally:
        if own:
            _lock.release()


def add_timeline(session_id: str, kind: str, detail: dict, *, wall_ns: int, engine_ns: int | None) -> None:
    """Append a timeline row; an unknown session is a KeyError."""
    conn = _connect()
    with _lock:
        try:
            _write_timeline(conn, session_id, kind, detail, wall_ns, engine_ns)
            conn.commit()
        except Exception:
            conn.rollback()
            raise


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
    """Queue an owner command for the daemon (session_id None = every session; a named session must exist, else
    KeyError). Returns its cmd_id."""
    conn = _connect()
    with _lock:
        cur = conn.execute(
            "INSERT INTO session_commands (session_id, command, args_json, created_ns) "
            "SELECT ?, ?, ?, ? WHERE ? IS NULL OR EXISTS (SELECT 1 FROM sessions WHERE session_id = ?)",
            (session_id, command, None if args is None else json.dumps(args, sort_keys=True), int(created_ns),
             session_id, session_id))
        conn.commit()
        if cur.rowcount == 0:
            raise KeyError(session_id)
        return int(cur.lastrowid)


def pending_commands() -> list[dict]:
    """Commands not yet marked, first in first out: by cmd_id (insertion order), never by the caller's
    created_ns, which a skewed clock could reorder. `args` parsed."""
    rows = _connect().execute("SELECT * FROM session_commands WHERE applied_ns IS NULL "
                              "ORDER BY cmd_id").fetchall()
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


# ── a paper session's day outputs (M5.3, D-68; A1 §4 session_orders, session_order_events, session_fills, session_charges) ──

_SESSION_ROW_TABLES = {  # name -> (SQL table, key column, key field of the engine's row)
    "orders": ("session_orders", "order_id", "order_id"),
    "order_events": ("session_order_events", "event_no", None),
    "fills": ("session_fills", "fill_id", "fill_id"),
    "charges": ("session_charges", "row_no", "row_no"),
}


# The order status each event leaves behind (engine_trading.cpp); SUBMIT, FILL and MODIFY leave it as it was.
_EVENT_STATUS = {"ACTIVATE": "OPEN", "TRIGGER": "OPEN", "COMPLETE": "COMPLETE", "CANCEL": "CANCELLED",
                 "REJECT": "REJECTED", "SLICE": "SLICED"}


def apply_session_outputs(session_id: str, trade_date: str, outputs: dict, *, first_event_no: int,
                          last_frame: tuple[int, int] | None) -> int:
    """Store what `Engine.drain_outputs()` returned for one batch, in ONE transaction. Orders, fills and charges are
    upserted by their ids and order events are numbered from `first_event_no`, so applying the same outputs again
    changes nothing. The engine drains an order's row once, when it is created; its later changes arrive as events
    alone, so an order's `status` follows its newest status-changing event (`_EVENT_STATUS`). A batch that is older
    than what is stored for an order (a stale repeat) leaves that order as it is. `last_frame` = (frame_no,
    recv_ns), when given, is stored on the (session, day) row. If any row is bad, nothing is stored. Returns the
    next event_no."""
    events = list(outputs.get("order_events", ()))
    conn = _connect()
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO session_order_events (session_id, trade_date, event_no, row_json) "
                "VALUES (?, ?, ?, ?)",
                [(session_id, trade_date, first_event_no + i, json.dumps(e, sort_keys=True))
                 for i, e in enumerate(events)])
            rows = {int(o["order_id"]): o for o in outputs.get("orders", ())}
            changes: dict[int, list[str]] = {}          # order id -> its events in this batch, oldest first
            newest: dict[int, int] = {}
            for i, e in enumerate(events):
                oid = int(e["order_id"])
                changes.setdefault(oid, []).append(e["event"])
                newest[oid] = first_event_no + i
            for oid in sorted(set(rows) | set(changes)):
                if oid in newest:
                    stored = conn.execute(
                        "SELECT MAX(event_no) FROM session_order_events WHERE session_id = ? AND trade_date = ? "
                        "AND json_extract(row_json, '$.order_id') = ?", (session_id, trade_date, oid)).fetchone()[0]
                    if stored is not None and stored > newest[oid]:
                        continue                                    # a later event is stored: this batch is stale
                row = rows.get(oid)         # the engine drains an order's row when it is created, so it is as of then
                if row is None:             # later changes arrive as events alone: the newest one sets the status
                    old = conn.execute("SELECT row_json FROM session_orders WHERE session_id = ? AND trade_date = ? "
                                       "AND order_id = ?", (session_id, trade_date, oid)).fetchone()
                    status = next((_EVENT_STATUS[ev] for ev in reversed(changes[oid]) if ev in _EVENT_STATUS), None)
                    if old is None or status is None:
                        continue
                    row = {**json.loads(old["row_json"]), "status": status}
                conn.execute("INSERT OR REPLACE INTO session_orders (session_id, trade_date, order_id, row_json) "
                             "VALUES (?, ?, ?, ?)", (session_id, trade_date, oid, json.dumps(row, sort_keys=True)))
            conn.executemany(
                "INSERT OR REPLACE INTO session_fills (session_id, trade_date, fill_id, row_json) VALUES (?, ?, ?, ?)",
                [(session_id, trade_date, int(f["fill_id"]), json.dumps(f, sort_keys=True))
                 for f in outputs.get("fills", ())])
            conn.executemany(
                "INSERT OR REPLACE INTO session_charges (session_id, trade_date, row_no, row_json) "
                "VALUES (?, ?, ?, ?)",
                [(session_id, trade_date, int(c["row_no"]), json.dumps(c, sort_keys=True))
                 for c in outputs.get("charges", ())])
            if last_frame is not None:
                conn.execute(
                    "INSERT INTO session_days (session_id, trade_date, last_frame_no, last_frame_recv_ns) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT (session_id, trade_date) DO UPDATE SET "
                    "last_frame_no = excluded.last_frame_no, last_frame_recv_ns = excluded.last_frame_recv_ns",
                    (session_id, trade_date, int(last_frame[0]), int(last_frame[1])))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return first_event_no + len(events)


def session_rows(session_id: str, table: str, *, trade_date: str | None = None, offset: int = 0,
                 limit: int = 500) -> list[dict]:
    """A paper session's stored rows in key order (orders | order_events | fills | charges), optionally of one day,
    as the dicts the engine produced; an order event also carries its `event_no`."""
    if table not in _SESSION_ROW_TABLES:
        raise ValueError(f"unknown session table {table!r}")
    sql_table, key, _ = _SESSION_ROW_TABLES[table]
    sql, args = f"SELECT trade_date, {key}, row_json FROM {sql_table} WHERE session_id = ?", [session_id]
    if trade_date is not None:
        sql, args = sql + " AND trade_date = ?", [*args, trade_date]
    rows = _connect().execute(sql + f" ORDER BY trade_date, {key} LIMIT ? OFFSET ?",
                              (*args, int(limit), int(offset))).fetchall()
    out = []
    for r in rows:
        d = json.loads(r["row_json"])
        if table == "order_events":
            d["event_no"] = r[key]
        out.append(d)
    return out

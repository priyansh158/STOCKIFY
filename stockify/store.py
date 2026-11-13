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

SCHEMA_VERSION = 5  # = the number of files in stockify/data/migrations/lab

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


def _all(sql: str, args=()) -> list:
    """Every row of a read. The connection is shared by every thread and SQLite shows a connection its own
    uncommitted writes, so a read waits for the lock that a writer holds from BEGIN to COMMIT: it sees committed
    state only (GK 5.1, carried from the dispatcher review)."""
    conn = _connect()
    with _lock:
        return conn.execute(sql, args).fetchall()


def _one(sql: str, args=()):  # noqa: ANN202
    """The first row of a read, or None; see `_all`."""
    conn = _connect()
    with _lock:
        return conn.execute(sql, args).fetchone()


def schema_version() -> int:
    row = _one("SELECT value FROM schema_meta WHERE key = 'schema_version'")
    return int(row["value"]) if row else 0


def table_names() -> list[str]:
    rows = _all("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
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
    return [dict(r) for r in _all(sql + " ORDER BY sample_id", args)]


MARGIN_LIVE_FIELDS = ("session_id", "wall_ns", "portfolio_sha256", "kite_json", "model_json", "engine_ns", "trade_date",
                      "legs_json", "market_json", "span_file", "step", "kite_total_paise", "ours_total_paise",
                      "gap_paise", "gap_ppm", "request_ns", "response_ns", "error")


def add_margin_live(row: dict) -> int:
    """One live-margin request of a paper session (IWB 5.1): Kite's answer beside the model's, or the error. Keys are
    column names; a key left out is NULL. Returns the row id. The error text must already be redacted."""
    conn = _connect()
    with _lock:
        cur = conn.execute(
            f"INSERT INTO margin_live ({', '.join(MARGIN_LIVE_FIELDS)}) "
            f"VALUES ({', '.join('?' * len(MARGIN_LIVE_FIELDS))})", tuple(row.get(k) for k in MARGIN_LIVE_FIELDS))
        conn.commit()
        return int(cur.lastrowid)


def margin_live_rows(*, session_id: str | None = None, start_ns: int | None = None,
                     end_ns: int | None = None) -> list[dict]:
    """The live-margin log in request order; `start_ns` inclusive, `end_ns` exclusive (on wall_ns)."""
    where, args = [], []
    if session_id is not None:
        where.append("session_id = ?")
        args.append(session_id)
    if start_ns is not None:
        where.append("wall_ns >= ?")
        args.append(start_ns)
    if end_ns is not None:
        where.append("wall_ns < ?")
        args.append(end_ns)
    sql = "SELECT * FROM margin_live" + (" WHERE " + " AND ".join(where) if where else "")
    return [dict(r) for r in _all(sql + " ORDER BY wall_ns, rowid", tuple(args))]


def write_recorder_status(row: dict) -> None:
    conn = _connect()
    with _lock:
        conn.execute(
            f"INSERT OR REPLACE INTO recorder_status ({', '.join(RECORDER_STATUS_FIELDS)}) "
            f"VALUES ({', '.join('?' * len(RECORDER_STATUS_FIELDS))})",
            tuple(row.get(k) for k in RECORDER_STATUS_FIELDS))
        conn.commit()


def latest_recorder_status() -> dict | None:
    """The newest recorder health row, or None: no rows, no store, or a store that cannot be read.

    A pure read, so it opens lab.sqlite on its own READ-ONLY connection (`mode=ro`), never `_connect()`: that opens
    read-write, creates the file and folder when they are missing, switches on WAL and applies migrations. A missing
    file, a missing table (an older schema) or an unreadable file (locked, corrupt, not a database) all read as no
    status and never raise. It waits for the store's lock like every other reader (`_all`).
    """
    path = _db_path()
    try:
        if not path.exists():
            return None
        with _lock:
            conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5.0)
            try:
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM recorder_status ORDER BY wall_ns DESC LIMIT 1").fetchone()
            finally:
                conn.close()
    except (sqlite3.Error, OSError):
        return None
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
              "bundle_sha256", "engine_build_id", "impl", "config_json", "metrics_json", "labels_json", "error",
              "role", "job_id")

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
    now, started = timeutil.now_ns(), row.get("started_ns")
    # A row is never created after it started (the launcher reads its start time first; the clock may tick between).
    full = {"status": "queued", "role": "trial", "created_ns": now if started is None else min(now, started), **row}
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
    row = _one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
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
    return [dict(r) for r in _all(sql, (*args, int(limit), int(offset)))]


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
    rows = _all(f"SELECT {', '.join(cols)} FROM {sql_table} WHERE run_id = ? ORDER BY {key} "
                f"LIMIT ? OFFSET ?", (run_id, int(limit), int(offset)))
    if table == "orders":
        return [json.loads(r["row_json"]) for r in rows]
    return [dict(r) for r in rows]


# ── research: families, unlock log, jobs (M6.2; A1 §4 trial_families, holdout_unlocks, research_jobs) ──

RESEARCH_JOB_FIELDS = ("job_id", "kind", "name", "family_id", "status", "created_ns", "finished_ns", "folder",
                       "spec_json", "state_json", "error")
_MAX_PAGE = 1000   # list_research_jobs never returns more than this many rows


def add_family(family_id: str, *, objective: str, description: str | None = None, named: bool = False,
               now_ns: int | None = None) -> bool:
    """Insert a trial family if absent; returns whether it inserted. An existing family is never touched, so a
    name the owner gave survives."""
    from stockify import timeutil
    conn = _connect()
    with _lock:
        cur = conn.execute("INSERT OR IGNORE INTO trial_families (family_id, description, objective, created_ns, named) "
                           "VALUES (?, ?, ?, ?, ?)",
                           (family_id, description, objective, timeutil.now_ns() if now_ns is None else now_ns,
                            1 if named else 0))
        conn.commit()
        return cur.rowcount > 0


def name_family(family_id: str, description: str) -> None:
    """The owner's name for a family: sets the description and `named = 1`."""
    conn = _connect()
    with _lock:
        conn.execute("UPDATE trial_families SET description = ?, named = 1 WHERE family_id = ?",
                     (description, family_id))
        conn.commit()


def get_family(family_id: str) -> dict | None:
    row = _one("SELECT * FROM trial_families WHERE family_id = ?", (family_id,))
    return dict(row) if row else None


def list_families() -> list[dict]:
    """Ordered by family_id (never by time)."""
    return [dict(r) for r in _all("SELECT * FROM trial_families ORDER BY family_id")]


def family_runs(family_id: str, *, roles: tuple[str, ...] | None = ("trial",),
                status: str | None = "done") -> list[dict]:
    """A family's runs, ordered by name then run_id (never by time). roles=None or status=None means no filter;
    an empty roles tuple matches nothing."""
    sql, args = "SELECT * FROM runs WHERE family_id = ?", [family_id]
    if roles is not None:
        roles = tuple(roles)
        if not roles:
            return []
        sql += f" AND role IN ({', '.join('?' * len(roles))})"
        args += roles
    if status is not None:
        sql += " AND status = ?"
        args.append(status)
    return [dict(r) for r in _all(sql + " ORDER BY name, run_id", args)]


def run_nav_series(run_id: str) -> list[tuple[str, int]]:
    """`(trade_date, nav_paise)` of a run from run_equity, dates ascending."""
    return [(r["trade_date"], r["nav_paise"]) for r in
            _all("SELECT trade_date, nav_paise FROM run_equity WHERE run_id = ? ORDER BY trade_date", (run_id,))]


def log_holdout_unlock(run_id: str, family_id: str | None, reason: str, *, job_id: str | None = None,
                       now_ns: int | None = None) -> bool:
    """Append one line to the unlock log. A second call with the same (job_id, reason) adds nothing (a job counts
    once); job_id None is a standalone run and always adds. Returns whether a row was added."""
    from stockify import timeutil
    conn = _connect()
    with _lock:
        cur = conn.execute("INSERT OR IGNORE INTO holdout_unlocks (unlock_ns, run_id, family_id, reason, job_id) "
                           "VALUES (?, ?, ?, ?, ?)",
                           (timeutil.now_ns() if now_ns is None else now_ns, run_id, family_id, reason, job_id))
        conn.commit()
        return cur.rowcount > 0


def holdout_unlock_rows(family_id: str | None = None) -> list[dict]:
    """The log in insertion order (rowid), optionally of one family."""
    if family_id is None:
        rows = _all("SELECT * FROM holdout_unlocks ORDER BY rowid")
    else:
        rows = _all("SELECT * FROM holdout_unlocks WHERE family_id = ? ORDER BY rowid", (family_id,))
    return [dict(r) for r in rows]


def create_research_job(row: dict) -> None:
    """Insert a research_jobs row; keys are column names (job_id, kind, status and created_ns are required by the
    table). An unknown key is a ValueError."""
    unknown = set(row) - set(RESEARCH_JOB_FIELDS)
    if unknown:
        raise ValueError(f"not research_jobs columns: {sorted(unknown)}")
    conn = _connect()
    with _lock:
        conn.execute(f"INSERT INTO research_jobs ({', '.join(RESEARCH_JOB_FIELDS)}) "
                     f"VALUES ({', '.join('?' * len(RESEARCH_JOB_FIELDS))})",
                     tuple(row.get(k) for k in RESEARCH_JOB_FIELDS))
        conn.commit()


def update_research_job(job_id: str, **fields) -> bool:
    """Update columns of one job. Names are checked against RESEARCH_JOB_FIELDS (job_id itself is not updatable)
    and the SQL only ever contains names from that constant; values are bound. Returns whether a row changed."""
    unknown = set(fields) - (set(RESEARCH_JOB_FIELDS) - {"job_id"})
    if unknown:
        raise ValueError(f"not updatable research_jobs columns: {sorted(unknown)}")
    if not fields:
        return False
    cols = [c for c in RESEARCH_JOB_FIELDS if c in fields]
    conn = _connect()
    with _lock:
        cur = conn.execute(f"UPDATE research_jobs SET {', '.join(f'{c} = ?' for c in cols)} WHERE job_id = ?",
                           (*(fields[c] for c in cols), job_id))
        conn.commit()
        return cur.rowcount > 0


def get_research_job(job_id: str) -> dict | None:
    row = _one("SELECT * FROM research_jobs WHERE job_id = ?", (job_id,))
    return dict(row) if row else None


def list_research_jobs(kind: str | None = None, offset: int = 0, limit: int = 50) -> list[dict]:
    """Newest created_ns first (job_id breaks ties). limit is clamped to 0..1000, offset to >= 0."""
    limit, offset = max(0, min(int(limit), _MAX_PAGE)), max(0, int(offset))
    sql, args = "SELECT * FROM research_jobs", []
    if kind is not None:
        sql += " WHERE kind = ?"
        args.append(kind)
    sql += " ORDER BY created_ns DESC, job_id LIMIT ? OFFSET ?"
    return [dict(r) for r in _all(sql, (*args, limit, offset))]


def backfill_null_families(compute) -> int:  # noqa: ANN001
    """For each runs row whose family_id IS NULL call `compute(row) -> str | None`; a string is written to that
    column only (and only while it is still NULL). Returns how many rows were set. The lock is not held while
    `compute` runs (it may call any store function); the updates are one transaction, each guarded by
    `family_id IS NULL`, so a row that got a family meanwhile is left alone."""
    rows = _all("SELECT * FROM runs WHERE family_id IS NULL ORDER BY run_id")
    chosen = []
    for row in rows:
        fam = compute(dict(row))
        if fam is not None:
            chosen.append((fam, row["run_id"]))
    conn = _connect()
    with _lock:
        try:
            n = 0
            for fam, run_id in chosen:
                n += conn.execute("UPDATE runs SET family_id = ? WHERE run_id = ? AND family_id IS NULL",
                                  (fam, run_id)).rowcount
            conn.commit()
            return n
        except BaseException:
            conn.rollback()
            raise


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
    row = _one("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
    return _session_row(row) if row else None


def list_sessions(*, status: str | None = None) -> list[dict]:
    """Oldest created first."""
    sql, args = "SELECT * FROM sessions", ()
    if status is not None:
        sql, args = sql + " WHERE status = ?", (status,)
    return [_session_row(r) for r in _all(sql + " ORDER BY created_ns, session_id", args)]


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
    rows = _all(
        "SELECT session_id, engine_ns, wall_ns, kind, detail_json FROM session_timeline WHERE session_id = ? "
        "ORDER BY rowid LIMIT ? OFFSET ?", (session_id, int(limit), int(offset)))
    out = []
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(d.pop("detail_json") or "null")
        out.append(d)
    return out


def previous_session_day(session_id: str, trade_date: str) -> dict | None:
    """The session's newest (session, day) row before `trade_date`, or None."""
    row = _one("SELECT * FROM session_days WHERE session_id = ? AND trade_date < ? ORDER BY trade_date DESC LIMIT 1",
               (session_id, trade_date))
    return dict(row) if row else None


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
    row = _one("SELECT * FROM session_days WHERE session_id = ? AND trade_date = ?", (session_id, trade_date))
    return dict(row) if row else None


def later_session_days(session_id: str, trade_date: str) -> list[dict]:
    """The session's day rows after `trade_date`, oldest first."""
    return [dict(r) for r in _all("SELECT * FROM session_days WHERE session_id = ? AND trade_date > ? "
                                  "ORDER BY trade_date", (session_id, trade_date))]


def session_days(session_id: str) -> list[dict]:
    """Every (session, day) row of the session, oldest day first."""
    return [dict(r) for r in _all("SELECT * FROM session_days WHERE session_id = ? ORDER BY trade_date", (session_id,))]


def session_equities(session_id: str) -> list[dict]:
    """The session's equity rows ({trade_date, nav_paise, cash_paise, margin_paise}), oldest day first."""
    return [dict(r) for r in _all("SELECT trade_date, nav_paise, cash_paise, margin_paise FROM session_equity "
                                  "WHERE session_id = ? ORDER BY trade_date", (session_id,))]


def put_parity_result(session_id: str, trade_date: str, kind: str, outcome: str, summary: dict, *,
                      created_ns: int) -> None:
    """Record a parity result (kind `journal` | `recorder`; outcome `pass` | `fail`) in ONE transaction: the
    `parity_results` row is replaced, and a `journal` result is also the day's `session_days.parity`."""
    if outcome not in ("pass", "fail"):
        raise ValueError(f"a parity result is pass or fail, not {outcome!r}")
    if kind not in ("journal", "recorder"):
        raise ValueError(f"unknown parity kind {kind!r}")
    conn = _connect()
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT OR REPLACE INTO parity_results (session_id, trade_date, kind, outcome, summary_json, "
                "created_ns) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, trade_date, kind, outcome, json.dumps(summary, sort_keys=True), int(created_ns)))
            if kind == "journal":
                conn.execute(
                    "INSERT INTO session_days (session_id, trade_date, parity) VALUES (?, ?, ?) "
                    "ON CONFLICT (session_id, trade_date) DO UPDATE SET parity = excluded.parity",
                    (session_id, trade_date, outcome))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def get_parity_result(session_id: str, trade_date: str, kind: str) -> dict | None:
    """One stored parity result (its summary decoded), or None."""
    row = _one("SELECT * FROM parity_results WHERE session_id = ? AND trade_date = ? AND kind = ?",
               (session_id, trade_date, kind))
    if row is None:
        return None
    out = dict(row)
    out["summary"] = json.loads(out.pop("summary_json") or "{}")
    return out


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
    rows = _all("SELECT * FROM session_commands WHERE applied_ns IS NULL ORDER BY cmd_id")
    out = []
    for r in rows:
        d = dict(r)
        raw = d.pop("args_json")
        d["args"] = None if raw is None else json.loads(raw)
        out.append(d)
    return out


def get_command(cmd_id: int) -> dict | None:
    """One command row (`args` parsed), pending or marked, or None."""
    row = _one("SELECT * FROM session_commands WHERE cmd_id = ?", (int(cmd_id),))
    if row is None:
        return None
    d = dict(row)
    raw = d.pop("args_json")
    d["args"] = None if raw is None else json.loads(raw)
    return d


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


# The order status each event leaves behind (engine_trading.cpp); SUBMIT and FILL leave it as it was.
_EVENT_STATUS = {"MODIFY": "PENDING", "ACTIVATE": "OPEN", "TRIGGER": "OPEN", "COMPLETE": "COMPLETE", "CANCEL": "CANCELLED",
                 "REJECT": "REJECTED", "SLICE": "SLICED"}


_MODIFY_FIELDS = {"qty": "qty", "price": "price_paise", "trigger": "trigger_paise", "type": "type"}


def apply_session_outputs(session_id: str, trade_date: str, outputs: dict, *, first_event_no: int,
                          last_frame: tuple[int, int] | None, carried: list[int] | None = None,
                          latency_ns: int = 0) -> int:
    """Store what `Engine.drain_outputs()` returned for one batch, in ONE transaction. Orders, fills and charges are
    upserted by their ids and order events are numbered from `first_event_no`, so applying the same outputs again
    changes nothing. The engine drains an order's row once, when it is created; its later changes arrive as events
    alone, so an order's `status` follows its newest status-changing event (`_EVENT_STATUS`). A batch that is older
    than what is stored for an order (a stale repeat) leaves that order as it is. `last_frame` = (frame_no,
    recv_ns), when given, is stored on the (session, day) row. If any row is bad, nothing is stored. Returns the
    next event_no.

    After the fills are in, each order they or the batch's events touch has `filled_qty`, `avg_price_paise`
    (`floor(SUM(qty * price) / SUM(qty))`, the engine's own formula), `reason` (its newest CANCEL or REJECT event's
    detail), and what its MODIFY events changed (`qty`, `price_paise`, `trigger_paise`, `type` from the event's
    `k=v,...` detail, newest last; `t_activate_ns` = max(itself, the event's time + `latency_ns`), as the engine's
    modify does) recomputed from the stored fills and events: a
    pure function of the tables, so a repeat or a replay gives the same row. An order with no stored row (one
    carried from a prior day, which the engine never drained today) cannot be refreshed; its ids are appended to
    `carried`, when given, and its events and fills are stored regardless."""
    conn = _connect()
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            n = _apply_outputs(conn, session_id, trade_date, outputs, first_event_no=first_event_no,
                               last_frame=last_frame, carried=carried, latency_ns=latency_ns)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return n


def _apply_outputs(conn, session_id: str, trade_date: str, outputs: dict, *, first_event_no: int,  # noqa: ANN001
                   last_frame: tuple[int, int] | None, carried: list[int] | None, latency_ns: int) -> int:
    """`apply_session_outputs` inside the caller's transaction."""
    events = list(outputs.get("order_events", ()))
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
                continue                                    # (an order carried from a prior day: reported below)
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
    touched = set(newest) | {int(f["order_id"]) for f in outputs.get("fills", ())}
    for oid in sorted(touched):
        if not _refresh_order(conn, session_id, trade_date, oid, latency_ns) and carried is not None:
            carried.append(oid)
    if last_frame is not None:
        conn.execute(
            "INSERT INTO session_days (session_id, trade_date, last_frame_no, last_frame_recv_ns) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (session_id, trade_date) DO UPDATE SET "
            "last_frame_no = excluded.last_frame_no, last_frame_recv_ns = excluded.last_frame_recv_ns",
            (session_id, trade_date, int(last_frame[0]), int(last_frame[1])))
    return first_event_no + len(events)


def _refresh_order(conn, session_id: str, trade_date: str, oid: int, latency_ns: int) -> bool:  # noqa: ANN001
    """Inside the caller's transaction: bring a stored order row's fill totals, reason and activation time in line
    with the stored fills and events. False if the order has no stored row."""
    old = conn.execute("SELECT row_json FROM session_orders WHERE session_id = ? AND trade_date = ? AND order_id = ?",
                       (session_id, trade_date, oid)).fetchone()
    if old is None:
        return False
    row = json.loads(old["row_json"])
    qty, value = conn.execute(
        "SELECT SUM(json_extract(row_json, '$.qty')), "
        "SUM(json_extract(row_json, '$.qty') * json_extract(row_json, '$.price_paise')) FROM session_fills "
        "WHERE session_id = ? AND trade_date = ? AND json_extract(row_json, '$.order_id') = ?",
        (session_id, trade_date, oid)).fetchone()
    if qty:
        row["filled_qty"] = int(qty)
        if value is not None:
            row["avg_price_paise"] = int(value) // int(qty)           # floor_div, orders.hpp avg_price()
    newest = conn.execute(
        "SELECT json_extract(row_json, '$.detail') FROM session_order_events WHERE session_id = ? AND trade_date = ? "
        "AND json_extract(row_json, '$.order_id') = ? AND json_extract(row_json, '$.event') IN ('CANCEL', 'REJECT') "
        "ORDER BY event_no DESC LIMIT 1", (session_id, trade_date, oid)).fetchone()
    if newest is not None and newest[0] is not None:
        row["reason"] = newest[0]
    modifies = conn.execute(
        "SELECT json_extract(row_json, '$.t_ns'), json_extract(row_json, '$.detail') FROM session_order_events "
        "WHERE session_id = ? AND trade_date = ? AND json_extract(row_json, '$.order_id') = ? "
        "AND json_extract(row_json, '$.event') = 'MODIFY' ORDER BY event_no", (session_id, trade_date, oid)).fetchall()
    for t_ns, detail in modifies:                                 # `k=v,k=v`, as the engine's modify words it
        for part in (detail or "").split(","):
            key, _, value = part.partition("=")
            if key in _MODIFY_FIELDS:
                row[_MODIFY_FIELDS[key]] = value if key == "type" else int(value)
        row["t_activate_ns"] = max(int(row.get("t_activate_ns") or 0), int(t_ns) + int(latency_ns))
    conn.execute("UPDATE session_orders SET row_json = ? WHERE session_id = ? AND trade_date = ? AND order_id = ?",
                 (json.dumps(row, sort_keys=True), session_id, trade_date, oid))
    return True


def reset_session_outputs(session_id: str, trade_date: str) -> None:
    """Remove one day's stored outputs (orders, events, fills, charges) and its frame mark, in one transaction,
    before the day's journal is replayed into them. The journal is never touched, and neither is another day or
    session. The frame mark goes too: after an OS crash it may be ahead of what the journal kept."""
    conn = _connect()
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table, _, _ in _SESSION_ROW_TABLES.values():
                conn.execute(f"DELETE FROM {table} WHERE session_id = ? AND trade_date = ?", (session_id, trade_date))
            conn.execute("UPDATE session_days SET last_frame_no = NULL, last_frame_recv_ns = NULL "
                         "WHERE session_id = ? AND trade_date = ?", (session_id, trade_date))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def _delete_day_outputs(conn, session_id: str, trade_date: str) -> None:  # noqa: ANN001
    for table, _, _ in _SESSION_ROW_TABLES.values():
        conn.execute(f"DELETE FROM {table} WHERE session_id = ? AND trade_date = ?", (session_id, trade_date))
    conn.execute("UPDATE session_days SET last_frame_no = NULL, last_frame_recv_ns = NULL "
                 "WHERE session_id = ? AND trade_date = ?", (session_id, trade_date))


def _day_rows(conn, session_id: str, trade_date: str) -> dict[str, dict[int, dict]]:  # noqa: ANN001
    out: dict[str, dict[int, dict]] = {}
    for name, (table, key, _) in _SESSION_ROW_TABLES.items():
        out[name] = {int(r[0]): json.loads(r[1]) for r in conn.execute(
            f"SELECT {key}, row_json FROM {table} WHERE session_id = ? AND trade_date = ? ORDER BY {key}",
            (session_id, trade_date))}
    return out


def session_day_rows(session_id: str, trade_date: str) -> dict[str, dict[int, dict]]:
    """{table name: {SQL key: row}} for one day's stored orders, order_events, fills and charges, keyed by the
    table's own key column (order_id, event_no, fill_id, row_no), not by a field of the row. Reads only."""
    conn = _connect()
    with _lock:
        return _day_rows(conn, session_id, trade_date)


def preview_session_outputs(session_id: str, trade_date: str, outputs: dict, *,
                            latency_ns: int = 0) -> tuple[dict, dict, list[int]]:
    """(stored rows, regenerated rows, carried order ids) of one day, nothing changed: the day's stored rows are
    read, then `outputs` (everything a fresh engine drained for the day's journal, event numbers from 1) are applied
    exactly as `apply_session_outputs` would, to the rows a day with none would have, and read back; the
    transaction is then rolled back. Each side is {table name: {key: row}} (the keys: order_id, event_no, fill_id,
    row_no). Used to compare a replayed day with what the store holds before anything is repaired."""
    conn = _connect()
    carried: list[int] = []
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            stored = _day_rows(conn, session_id, trade_date)
            _delete_day_outputs(conn, session_id, trade_date)
            _apply_outputs(conn, session_id, trade_date, outputs, first_event_no=1, last_frame=None,
                           carried=carried, latency_ns=latency_ns)
            twin = _day_rows(conn, session_id, trade_date)
        finally:
            conn.rollback()
    return stored, twin, sorted(set(carried))


def replace_session_outputs(session_id: str, trade_date: str, outputs: dict, *, latency_ns: int = 0,
                            last_frame: tuple[int, int] | None = None) -> list[int]:
    """Make a day's stored rows exactly what `outputs` (a fresh engine's drain of the day's whole journal) gives:
    the day's rows are removed and the outputs applied, in ONE transaction, so a crash leaves the old rows or the
    new, never a mixture. The day's frame mark is never lost: it is `last_frame` when given, else the mark the day
    row holds (read inside the same transaction), and stays NULL only if it was. Returns the carried order ids (see
    `apply_session_outputs`)."""
    conn = _connect()
    carried: list[int] = []
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if last_frame is None:
                row = conn.execute("SELECT last_frame_no, last_frame_recv_ns FROM session_days "
                                   "WHERE session_id = ? AND trade_date = ?", (session_id, trade_date)).fetchone()
                if row is not None and row[0] is not None:
                    last_frame = (int(row[0]), int(row[1]))
            _delete_day_outputs(conn, session_id, trade_date)
            _apply_outputs(conn, session_id, trade_date, outputs, first_event_no=1, last_frame=last_frame,
                           carried=carried, latency_ns=latency_ns)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return sorted(set(carried))


def session_last_frame(session_id: str, trade_date: str) -> tuple[int, int] | None:
    """The (frame_no, recv_ns) mark stored with a day's outputs, or None."""
    row = get_session_day(session_id, trade_date)
    if row is None or row["last_frame_no"] is None:
        return None
    return int(row["last_frame_no"]), int(row["last_frame_recv_ns"])


def save_session_day_end(session_id: str, trade_date: str, *, orders: list[dict], positions: list[dict],
                         equity: dict, eod_state_json: str, labels: list[str],
                         timeline: list[tuple[str, dict]] = (), wall_ns: int = 0) -> None:
    """A finished day's rows, in ONE transaction (DM 5.5): the day's orders are replaced by the engine's own book
    (`orders.csv` rows), its end-of-day positions and its equity row are replaced, and its `eod_state_json` and
    labels are stored, with the day's timeline rows (`timeline`: (kind, detail with a `day`); an earlier attempt's rows
    of the same kind and day are replaced). Writing the same day again leaves the same rows. The day's `status` is not touched: it
    becomes `finalised` only when everything else about the day is done."""
    conn = _connect()
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table in ("session_orders", "session_positions_eod", "session_equity"):
                conn.execute(f"DELETE FROM {table} WHERE session_id = ? AND trade_date = ?", (session_id, trade_date))
            conn.executemany(
                "INSERT INTO session_orders (session_id, trade_date, order_id, row_json) VALUES (?, ?, ?, ?)",
                [(session_id, trade_date, int(o["order_id"]), json.dumps(o, sort_keys=True)) for o in orders])
            conn.executemany(
                "INSERT INTO session_positions_eod (session_id, trade_date, row_no, row_json) VALUES (?, ?, ?, ?)",
                [(session_id, trade_date, n, json.dumps(p, sort_keys=True)) for n, p in enumerate(positions, 1)])
            conn.execute(
                "INSERT INTO session_equity (session_id, trade_date, nav_paise, cash_paise, margin_paise) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, trade_date, int(equity["nav_paise"]), int(equity["cash_paise"]),
                 int(equity["margin_paise"])))
            conn.execute(
                "INSERT INTO session_days (session_id, trade_date, eod_state_json, labels_json) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (session_id, trade_date) DO UPDATE SET eod_state_json = excluded.eod_state_json, "
                "labels_json = excluded.labels_json",
                (session_id, trade_date, eod_state_json, json.dumps(sorted(set(labels)))))
            for kind, detail in timeline:
                conn.execute("DELETE FROM session_timeline WHERE session_id = ? AND kind = ? "
                             "AND json_extract(detail_json, '$.day') = ?", (session_id, kind, trade_date))
                _write_timeline(conn, session_id, kind, detail, wall_ns, None)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def session_day_end(session_id: str, trade_date: str) -> dict:
    """{"positions": [rows as the engine wrote them], "equity": {nav_paise, cash_paise, margin_paise} | None}."""
    positions = [json.loads(r["row_json"]) for r in _all(
        "SELECT row_json FROM session_positions_eod WHERE session_id = ? AND trade_date = ? ORDER BY row_no",
        (session_id, trade_date))]
    row = _one("SELECT nav_paise, cash_paise, margin_paise FROM session_equity WHERE session_id = ? AND trade_date = ?",
               (session_id, trade_date))
    return {"positions": positions, "equity": dict(row) if row else None}


def previous_equity(session_id: str, trade_date: str) -> dict | None:
    """The session's equity row of its latest day before `trade_date` ({trade_date, nav_paise, cash_paise,
    margin_paise}), or None."""
    row = _one("SELECT trade_date, nav_paise, cash_paise, margin_paise FROM session_equity WHERE session_id = ? "
               "AND trade_date < ? ORDER BY trade_date DESC LIMIT 1", (session_id, trade_date))
    return dict(row) if row else None


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
    rows = _all(sql + f" ORDER BY trade_date, {key} LIMIT ? OFFSET ?", (*args, int(limit), int(offset)))
    out = []
    for r in rows:
        d = json.loads(r["row_json"])
        if table == "order_events":
            d["event_no"] = r[key]
        out.append(d)
    return out

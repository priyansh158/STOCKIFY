"""Versioned SQLite migrations (invariant I-13).

Every store keeps its schema as numbered files `NNNN_name.sql` in a
directory under `stockify/data/migrations/<store>/`. `apply(conn, store)`
runs the files not yet recorded in the store's `schema_version` table, in
order, each in one transaction. A recorded migration whose file has changed
since it was applied is an error: applied migrations are never edited, a fix
is a new file.

Recorded tick data and reference history are irreplaceable, so there are no
down-migrations and no drops.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class MigrationError(RuntimeError):
    """The migration files and the database disagree."""


def _files(store: str, base: Path | None = None) -> list[tuple[int, Path]]:
    directory = (base or MIGRATIONS_DIR) / store
    out = []
    for path in sorted(directory.glob("*.sql")):
        m = _NAME.match(path.name)
        if not m:
            raise MigrationError(f"bad migration file name: {path.name}")
        out.append((int(m.group(1)), path))
    versions = [v for v, _ in out]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"{store}: migrations must be numbered 0001, 0002, … without gaps")
    return out


def apply(conn: sqlite3.Connection, store: str, base: Path | None = None) -> int:
    """Apply pending migrations; return the resulting schema version."""
    # A second process opening the same file waits for the first one's migration instead of failing.
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, sha256 TEXT NOT NULL)"
    )
    version = 0
    for number, path in _files(store, base):
        sql = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        # The write lock is taken first and "applied?" is read inside it, so two processes
        # racing on an old file cannot both decide the same migration is pending.
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT name, sha256 FROM schema_version WHERE version = ?", (number,)).fetchone()
            if row is not None:
                if row[1] != digest:
                    raise MigrationError(
                        f"{store} migration {path.name} changed after it was applied; "
                        "add a new migration instead"
                    )
            else:
                for statement in _statements(sql):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_version (version, name, sha256) VALUES (?, ?, ?)",
                    (number, path.name, digest),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        version = number
    return version


def _statements(sql: str) -> list[str]:
    """Split a migration into statements (sqlite3.complete_statement aware)."""
    out, buf = [], []
    for line in sql.splitlines(keepends=True):
        buf.append(line)
        text = "".join(buf)
        if sqlite3.complete_statement(text):
            if text.strip():
                out.append(text)
            buf = []
    rest = "".join(buf).strip()
    if rest and not rest.startswith("--"):
        raise MigrationError("migration ends with an incomplete statement")
    return out

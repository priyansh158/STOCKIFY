"""lab.sqlite on numbered migrations (XI 5.1): a v1 lab upgrades in place, nothing is dropped."""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import stockify.store as store
from stockify.data import migrate

# The schema text exactly as store.py held it before migrations (a "v1 lab").
V1_SCHEMA = """
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


def _columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


class LabMigrationTests(SandboxedDBTest):
    db_modules = (store,)

    def _make_v1_lab(self):
        path = store._db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(V1_SCHEMA)
        conn.execute("INSERT INTO schema_meta VALUES ('schema_version', '1')")
        for i in range(3):
            conn.execute(
                "INSERT INTO margin_calibration (sample_id, wall_ns, underlying, t0_paise, t1_paise) VALUES (?,?,?,?,?)",
                (i + 1, 1000 + i, "NIFTY", 100 * i, 200 * i))
        conn.execute("INSERT INTO runs (run_id, name, status) VALUES ('r1', 'n', 'done')")
        conn.execute("INSERT INTO sessions (session_id, name, status, pause_reason) VALUES ('s1', 'x', 'paused', 'p')")
        conn.execute("INSERT INTO session_days (session_id, trade_date, parity) VALUES ('s1', '2026-11-05', 'pending')")
        conn.commit()
        conn.close()
        return path

    def test_version_equals_number_of_migration_files(self):
        n = len(list((migrate.MIGRATIONS_DIR / "lab").glob("*.sql")))
        self.assertEqual(store.schema_version(), store.SCHEMA_VERSION)
        self.assertEqual(store.SCHEMA_VERSION, n)

    def test_v1_lab_with_rows_migrates_and_keeps_them(self):
        path = self._make_v1_lab()
        self.assertEqual(len(store.margin_calibrations()), 3)
        self.assertEqual(store.get_run("r1")["status"], "done")
        conn = store._connect()
        self.assertIn("last_frame_no", _columns(conn, "session_days"))
        self.assertEqual(store.schema_version(), store.SCHEMA_VERSION)
        self.assertEqual(tuple(conn.execute("SELECT pause_reason, pause_reasons_json FROM sessions").fetchone()), ("p", "[]"))
        self.assertEqual(tuple(conn.execute("SELECT parity, status FROM session_days").fetchone()), ("pending", None))
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        self.assertEqual([r[0] for r in conn.execute("SELECT version FROM schema_version ORDER BY version")],
                         list(range(1, store.SCHEMA_VERSION + 1)))
        self.assertTrue(path.exists())

    def test_reopening_a_migrated_lab_is_a_no_op(self):
        store.init_db()
        support.reset_sqlite_singleton(store)
        store.init_db()
        self.assertEqual(store.schema_version(), store.SCHEMA_VERSION)

    def test_session_commands_table_and_index(self):
        conn = store._connect()
        self.assertIn("session_commands", store.table_names())
        idx = [r[1] for r in conn.execute("PRAGMA index_list(session_timeline)")]
        self.assertIn("ix_timeline_session", idx)

    def test_two_processes_opening_an_old_lab_at_once_migrate_it_once(self):
        path = self._make_v1_lab()
        barrier = threading.Barrier(2)
        results, errors = [], []

        def opener():
            conn = sqlite3.connect(str(path), timeout=10.0, check_same_thread=False)
            try:
                barrier.wait(timeout=10)
                results.append(migrate.apply(conn, "lab"))
            except Exception as exc:  # noqa: BLE001 — reported below
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=opener) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertEqual(results, [store.SCHEMA_VERSION] * 2)
        conn = sqlite3.connect(str(path))
        self.assertEqual([r[0] for r in conn.execute("SELECT version FROM schema_version ORDER BY version")],
                         list(range(1, store.SCHEMA_VERSION + 1)))
        cols = _columns(conn, "session_days")
        self.assertEqual(cols.count("last_frame_no"), 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM margin_calibration").fetchone()[0], 3)
        self.assertEqual(conn.execute("SELECT status FROM runs WHERE run_id = 'r1'").fetchone()[0], "done")
        conn.close()

    def test_schema_version_history_with_only_the_later_migration_recorded(self):
        # 0002 recorded, 0001 missing: 0001 (idempotent) is applied, 0002 is skipped, not re-run.
        path = self._make_v1_lab()
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, name TEXT NOT NULL, sha256 TEXT NOT NULL)")
        import hashlib
        f = migrate.MIGRATIONS_DIR / "lab" / "0002_sessions.sql"
        digest = hashlib.sha256(f.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        conn.execute("INSERT INTO schema_version VALUES (2, ?, ?)", (f.name, digest))
        conn.commit()
        conn.close()
        self.assertEqual(store.schema_version(), store.SCHEMA_VERSION)
        rows = store._connect().execute("SELECT version FROM schema_version ORDER BY version").fetchall()
        self.assertEqual([r[0] for r in rows], [1, 2])

    def test_schema_version_history_with_a_wrong_checksum_is_refused(self):
        path = self._make_v1_lab()
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, name TEXT NOT NULL, sha256 TEXT NOT NULL)")
        conn.execute("INSERT INTO schema_version VALUES (1, '0001_init.sql', 'bogus')")
        conn.commit()
        conn.close()
        with self.assertRaises(migrate.MigrationError):
            store._connect()


class MigrationFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        shutil.copytree(migrate.MIGRATIONS_DIR / "lab", self.tmp / "lab")

    def test_edited_migration_is_refused(self):
        conn = sqlite3.connect(":memory:")
        migrate.apply(conn, "lab", self.tmp)
        p = self.tmp / "lab" / "0002_sessions.sql"
        p.write_text(p.read_text(encoding="utf-8") + "\n-- edited\n", encoding="utf-8")
        with self.assertRaises(migrate.MigrationError):
            migrate.apply(conn, "lab", self.tmp)

    def test_failed_migration_leaves_nothing_half_applied(self):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        migrate.apply(conn, "lab", self.tmp)
        (self.tmp / "lab" / "0003_bad.sql").write_text(
            "ALTER TABLE sessions ADD COLUMN extra_one TEXT;\n"
            "ALTER TABLE sessions ADD COLUMN strategy_label TEXT;\n", encoding="utf-8")  # duplicate column
        with self.assertRaises(sqlite3.OperationalError):
            migrate.apply(conn, "lab", self.tmp)
        self.assertNotIn("extra_one", _columns(conn, "sessions"))
        self.assertEqual(conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()

"""simlab.store: the A1 §4 schema is created in the sandboxed simlab.sqlite."""
from __future__ import annotations

import unittest

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import simlab.store as store


class StoreTests(SandboxedDBTest):
    db_modules = (store,)

    def test_all_documented_tables_exist(self):
        expected = {
            "runs", "run_orders", "run_fills", "run_equity", "run_charges",
            "trial_families", "holdout_unlocks",
            "sessions", "session_days", "session_timeline", "session_orders",
            "session_order_events", "session_fills", "session_charges",
            "session_positions_eod", "session_equity", "margin_live", "parity_results",
            "margin_calibration", "recorder_status", "schema_meta",
        }
        self.assertEqual(set(store.table_names()), expected)

    def test_schema_version_is_recorded(self):
        self.assertEqual(store.schema_version(), store.SCHEMA_VERSION)

    def test_init_is_idempotent(self):
        store.init_db()
        support.reset_sqlite_singleton(store)
        store.init_db()
        self.assertEqual(store.schema_version(), store.SCHEMA_VERSION)

    def test_wal_mode(self):
        mode = store._connect().execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")


if __name__ == "__main__":
    unittest.main()

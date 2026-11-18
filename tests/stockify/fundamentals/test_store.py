"""FS 1: the fundamentals store (fundamentals.sqlite): migrated like every store, backed up with the others."""
from __future__ import annotations

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

from stockify import backup, paths
from stockify.data import migrate
from stockify.fundamentals import store


class FundamentalsStoreTests(SandboxedDBTest):
    db_modules = (store,)

    def test_store_migrates_and_backup_lists_it(self):
        self.assertEqual(store._db_path(), paths.fundamentals_db())
        self.assertEqual(paths.fundamentals_db().name, "fundamentals.sqlite")
        self.assertEqual(paths.fundamentals_db().parent, paths.lab_dir())
        conn = store._connect()
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertLessEqual({"schema_version", "universe", "sectors"}, tables)
        self.assertEqual(conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0],
                         len(migrate._files(store.STORE)))
        cols = [r[1] for r in conn.execute("PRAGMA table_info(universe)")]
        self.assertEqual(cols[:7], ["as_of", "symbol", "instrument_id", "isin", "company", "median_value_paise",
                                    "liquid"])
        self.assertEqual([r[1] for r in conn.execute("PRAGMA table_info(sectors)")],
                         ["symbol", "as_of", "macro", "sector", "industry", "basic_industry"])
        self.assertIn("fundamentals.sqlite", backup.DATABASES)
        # Raw NSE answers sit under nse/raw, which the backup copies file by file.
        self.assertEqual(paths.fundamentals_raw_dir("xbrl"), paths.nse_raw_dir() / "fundamentals" / "xbrl")

    def test_raw_kind_must_be_one_safe_segment(self):
        for bad in ("..", "a/b", "a\\b", "", "CON"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    paths.fundamentals_raw_dir(bad)

    def test_universe_round_trip(self):
        rows = [{"symbol": "INFY", "instrument_id": 7, "isin": "INE009A01021", "company": "Infosys Ltd.",
                 "list_industry": "Information Technology", "median_value_paise": 9_000_000_000, "liquid": True},
                {"symbol": "ZZZ", "instrument_id": None, "isin": "INE000000000", "company": "Z Ltd.",
                 "list_industry": "Services", "median_value_paise": None, "liquid": False}]
        store.put_universe("2026-11-17", rows)
        got = store.universe_on("2026-11-17")
        self.assertEqual([(r["symbol"], r["liquid"], r["median_value_paise"]) for r in got],
                         [("INFY", 1, 9_000_000_000), ("ZZZ", 0, None)])
        self.assertEqual([r["symbol"] for r in store.universe_on("2026-11-17", liquid_only=True)], ["INFY"])
        self.assertEqual(store.latest_as_of(), "2026-11-17")
        self.assertEqual(store.universe_on("2026-11-16"), [])
        with self.assertRaises(ValueError):
            store.put_universe("2026-11-17", [{"symbol": "X"}])

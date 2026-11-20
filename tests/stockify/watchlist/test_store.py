"""CW 2: the watchlist store (watchlist.sqlite): add and remove, the checks on a symbol, the cap, the change counter,
the once-only alert record."""
from __future__ import annotations

from datetime import date

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest
from tests.stockify.test_bundle import HEADER

from stockify import backup, paths
from stockify.data import instruments, migrate, refdata
from stockify.watchlist import store

DAY = date(2026, 11, 17)
T0 = 1_700_000_000 * 10**9


def install(symbols=("INFY", "TCS", "RELIANCE")) -> dict[str, int]:
    rows = "".join(f"{1000 + i},1,{s},{s},0,,0,0.05,1,EQ,NSE,NSE\n" for i, s in enumerate(symbols))
    rows += "9001,1,NIFTY 50,NIFTY 50,0,,0,0.05,1,EQ,INDICES,NSE\n"
    instruments.sync(DAY, {"NSE": f"{HEADER}\n{rows}"}, underlyings={})
    return {s: instruments.find("NSE", s) for s in (*symbols, "NIFTY 50")}


class StoreCase(SandboxedDBTest):
    db_modules = (store, refdata)

    def setUp(self):
        super().setUp()
        self.ids = install()


class StoreShapeTests(StoreCase):
    def test_migrates_and_is_backed_up(self):
        self.assertEqual(store._db_path(), paths.watchlist_db())
        self.assertEqual(paths.watchlist_db().name, "watchlist.sqlite")
        self.assertEqual(paths.watchlist_db().parent, paths.lab_dir())
        self.assertIn("watchlist.sqlite", backup.DATABASES)
        conn = store._connect()
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertLessEqual({"watchlist", "watch_alerts", "meta"}, tables)
        self.assertEqual(conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0],
                         len(migrate._files(store.STORE)))
        self.assertEqual([r[1] for r in conn.execute("PRAGMA table_info(watchlist)")],
                         ["instrument_id", "symbol", "added_ns", "position"])
        self.assertEqual([r[1] for r in conn.execute("PRAGMA table_info(watch_alerts)")],
                         ["instrument_id", "symbol", "kind", "ref", "level_paise", "prior_paise", "prior_date",
                          "at_ns", "delivery"])


class AddRemoveTests(StoreCase):
    def test_add_lists_in_order_and_resolves_the_symbol(self):
        a = store.add("infy", now_ns=T0)
        store.add("TCS", now_ns=T0 + 1)
        self.assertEqual((a["symbol"], a["instrument_id"]), ("INFY", self.ids["INFY"]))
        self.assertEqual([r["symbol"] for r in store.rows()], ["INFY", "TCS"])
        self.assertEqual([r["position"] for r in store.rows()], [1, 2])
        self.assertEqual(store.symbols(), ["INFY", "TCS"])
        self.assertEqual(store.ids(), [self.ids["INFY"], self.ids["TCS"]])
        self.assertEqual(store.rows()[0]["added_ns"], T0)

    def test_unknown_symbol_is_not_found(self):
        with self.assertRaises(store.NotFound):
            store.add("NOSUCH")
        self.assertEqual(store.rows(), [])

    def test_index_and_non_nse_are_refused(self):
        with self.assertRaises(store.NotAStock):
            store.add("NIFTY 50")
        self.assertEqual(store.rows(), [])

    def test_duplicate_is_refused_in_any_case(self):
        store.add("INFY")
        v = store.version()
        with self.assertRaises(store.AlreadyWatched):
            store.add("infy")
        self.assertEqual(store.version(), v)
        self.assertEqual(len(store.rows()), 1)

    def test_cap_comes_from_the_config(self):
        from unittest import mock
        with mock.patch.object(store, "max_stocks", lambda: 2):
            store.add("INFY")
            store.add("TCS")
            with self.assertRaises(store.ListFull) as cm:
                store.add("RELIANCE")
        self.assertIn("2", str(cm.exception))
        self.assertEqual(store.symbols(), ["INFY", "TCS"])

    def test_default_cap_is_the_shipped_one(self):
        self.assertEqual(store.max_stocks(), 50)

    def test_remove(self):
        store.add("INFY")
        store.add("TCS")
        self.assertTrue(store.remove("infy"))
        self.assertFalse(store.remove("INFY"))
        self.assertFalse(store.remove("NOSUCH"))
        self.assertEqual(store.symbols(), ["TCS"])
        store.add("INFY")                              # back again, at the end
        self.assertEqual(store.symbols(), ["TCS", "INFY"])
        self.assertEqual([r["position"] for r in store.rows()], [2, 3])

    def test_version_moves_on_every_change_and_only_then(self):
        v0 = store.version()
        store.add("INFY")
        v1 = store.version()
        self.assertGreater(v1, v0)
        store.remove("NOSUCH")
        self.assertEqual(store.version(), v1)
        store.remove("INFY")
        self.assertGreater(store.version(), v1)

    def test_a_failed_add_leaves_nothing_behind(self):
        for sym in ("NOSUCH", "NIFTY 50"):
            with self.assertRaises(store.WatchError):
                store.add(sym)
        self.assertEqual((store.rows(), store.ids(), store.version()), ([], [], 0))


class AlertTests(StoreCase):
    def rec(self, kind="high52", ref="2026-11-17", iid=None, **kw):
        args = dict(level_paise=150_000, prior_paise=148_000, prior_date="2026-03-02", at_ns=T0)
        args.update(kw)
        return store.record_alert(iid or self.ids["INFY"], "INFY", kind, ref, **args)

    def test_true_only_when_inserted(self):
        self.assertTrue(self.rec())
        self.assertFalse(self.rec())
        self.assertFalse(self.rec(level_paise=151_000))        # same alert, a later higher level: not a new row
        self.assertEqual(len(store.alerts(0)), 1)
        self.assertEqual(store.alerts(0)[0]["level_paise"], 150_000)

    def test_one_per_kind_and_day(self):
        self.assertTrue(self.rec("high52", "2026-11-17"))
        self.assertTrue(self.rec("low52", "2026-11-17", level_paise=90_000, prior_paise=91_000))
        self.assertTrue(self.rec("high52", "2026-11-18"))
        self.assertTrue(self.rec("high52", "2026-11-17", iid=self.ids["TCS"]))
        self.assertEqual(len(store.alerts(0)), 4)

    def test_a_results_alert_needs_no_levels(self):
        self.assertTrue(store.record_alert(self.ids["INFY"], "INFY", "results", "2026-11-20", at_ns=T0))
        row = store.alerts(0)[0]
        self.assertEqual((row["kind"], row["ref"], row["level_paise"], row["delivery"]), ("results", "2026-11-20", None, "pending"))

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            self.rec("high")

    def test_alerts_since_newest_first_and_kept_after_removal(self):
        store.add("INFY")
        self.rec(ref="2026-11-10", at_ns=T0)
        self.rec(ref="2026-11-17", at_ns=T0 + 5 * 10**9)
        self.assertEqual([a["ref"] for a in store.alerts(T0 + 1)], ["2026-11-17"])
        self.assertEqual([a["ref"] for a in store.alerts(0)], ["2026-11-17", "2026-11-10"])
        store.remove("INFY")
        self.assertEqual(len(store.alerts(0)), 2)

    def test_delivery_can_be_set(self):
        self.rec()
        store.set_delivery(self.ids["INFY"], "high52", "2026-11-17", "sent")
        self.assertEqual(store.alerts(0)[0]["delivery"], "sent")
        with self.assertRaises(ValueError):
            store.set_delivery(self.ids["INFY"], "high52", "2026-11-17", "bogus")

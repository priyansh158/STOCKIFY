"""CW 3: the watched stocks join the recorder's feed (`stockify.watchlist.feed.merged`) in a QUOTE tier."""
from __future__ import annotations

from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.stockify.daemon.test_engine_log import enable_logging
from tests.stockify.watchlist.test_store import DAY, StoreCase

from stockify.config import loader
from stockify.watchlist import feed, store

BASE = {"schema": "stockify.universe/1", "equities_quote": ["NSE:TCS"]}


class MergedTests(StoreCase):
    def setUp(self):
        super().setUp()
        enable_logging(self)

    def test_adds_the_watched_stocks_in_list_order_without_changing_the_input(self):
        store.add("RELIANCE")
        store.add("INFY")
        base = dict(BASE)
        out = feed.merged(base, DAY)
        self.assertEqual(out["watchlist_quote"], ["NSE:RELIANCE", "NSE:INFY"])
        self.assertEqual(out["equities_quote"], ["NSE:TCS"])
        self.assertEqual(base, BASE)                                   # the loaded universe is not changed

    def test_an_empty_list_leaves_the_universe_as_it_is(self):
        self.assertIs(feed.merged(BASE, DAY), BASE)

    def test_at_most_max_stocks(self):
        for s in ("RELIANCE", "INFY", "TCS"):
            store.add(s)
        with mock.patch.object(store, "max_stocks", return_value=2):
            self.assertEqual(feed.merged(BASE, DAY)["watchlist_quote"], ["NSE:RELIANCE", "NSE:INFY"])

    def test_any_error_logs_and_returns_the_universe_unchanged(self):
        with mock.patch.object(store, "symbols", side_effect=RuntimeError("disk")), \
                self.assertLogs("stockify.watchlist.feed", "ERROR") as cm:
            self.assertIs(feed.merged(BASE, DAY), BASE)
        self.assertIn("recording goes on", "\n".join(cm.output))

    def test_a_corrupt_watchlist_file_returns_the_universe_unchanged(self):
        support.reset_sqlite_singleton(store)
        store._db_path().write_bytes(b"this is not a database" * 64)
        with self.assertLogs("stockify.watchlist.feed", "ERROR"):
            self.assertIs(feed.merged(BASE, DAY), BASE)


class RoomTests(StoreCase):
    def test_the_shipped_universe_plus_a_full_watchlist_stays_under_80_percent_of_the_connection_cap(self):
        """The widest the shipped universe can get (every chain at n_quote plus its hysteresis on both sides, both
        sides of the book) plus max_stocks watched stocks stays below 80 % of Kite's 3,000 per connection (K-1)."""
        u = loader.load_json("universe.json")
        cap = int(loader.load_json("kite_limits.json")["ws_max_instruments"])
        static = (len(u.get("indices_full", [])) + len(u.get("equities_full", [])) + len(u.get("equities_quote", []))
                  + sum(len(f["months"]) for f in u.get("futures_full", []) + u.get("stock_futures_full", [])))
        chains = sum(2 * (2 * (c["n_quote"] + c.get("hysteresis", 2)) + 1) for c in u.get("chains", []))
        total = static + chains + store.max_stocks()
        self.assertEqual(cap, 3000)
        self.assertLess(total, 0.8 * cap, (static, chains, store.max_stocks()))

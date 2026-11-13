"""M4.1 (CB 4.2): index freeze limits are dated from NSE circulars (X-5) in stockify/config/freeze_limits.json.

For an underlying with circular rows, those decide; `qtyfreeze.csv` snapshots in refdata (S-6) are only a
cross-check, since they lag. Stocks, with no circular history here, use the snapshots as before."""
from __future__ import annotations

import unittest

import tests.support  # noqa: F401  — must precede project imports

from stockify import bundle
from stockify.config import loader
from stockify.data import refdata
from tests.stockify.test_bundle import BundleTest

EXPECTED = [  # (underlying, date, freeze qty), from 01_facts X-5
    ("NIFTY", "2026-03-01", None), ("NIFTY", "2026-03-02", 1800), ("NIFTY", "2026-10-04", 1800),
    ("NIFTY", "2026-10-05", 3510), ("NIFTY", "2026-11-01", 3510), ("NIFTY", "2026-11-02", 1800),
    ("BANKNIFTY", "2026-03-02", 600), ("BANKNIFTY", "2026-10-05", 1440), ("BANKNIFTY", "2026-11-02", 900),
    ("FINNIFTY", "2026-08-31", 1200), ("FINNIFTY", "2026-09-01", 1800), ("FINNIFTY", "2026-10-05", 3240),
    ("FINNIFTY", "2026-11-06", 1800),
    ("MIDCPNIFTY", "2026-03-02", 2800), ("MIDCPNIFTY", "2026-10-05", 5760), ("MIDCPNIFTY", "2026-11-02", 2800),
    ("NIFTYNXT50", "2026-03-02", 600), ("NIFTYNXT50", "2026-10-05", 1125), ("NIFTYNXT50", "2026-11-02", 600),
    ("NIFTYFPI", "2026-09-01", 8500), ("NIFTYFPI", "2026-10-05", 53900), ("NIFTYFPI", "2026-11-02", 8500),
]


class ConfigTests(unittest.TestCase):
    def test_the_file_loads_and_cites_x5(self):
        doc = loader.load_json("freeze_limits.json")
        self.assertEqual(doc["schema"], "stockify.freeze_limits/1")
        self.assertTrue(all("X-5" in r["source"] for r in doc["limits"]))
        self.assertEqual({r["underlying"] for r in doc["limits"]},
                         {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "NIFTYFPI"})


class FreezeLookupTests(BundleTest):
    def _snapshot(self, underlying: str, day: str, qty: int) -> None:
        conn = refdata._connect()
        conn.execute("INSERT OR REPLACE INTO freeze_limits (underlying, effective_from, freeze_qty, source) "
                     "VALUES (?, ?, ?, 'qtyfreeze.csv')", (underlying, day, qty))
        conn.commit()

    def test_dated_circular_limits(self):
        conn = refdata._connect()
        for und, day, qty in EXPECTED:
            with self.subTest(underlying=und, date=day):
                self.assertEqual(bundle._freeze(conn, und, day), qty)

    def test_circulars_beat_a_lagging_snapshot_and_stocks_use_the_snapshot(self):
        self._snapshot("NIFTY", "2026-10-06", 1800)        # the CSV still shows the old limit after 10-05
        self._snapshot("ACME", "2026-11-05", 25000)
        conn = refdata._connect()
        self.assertEqual(bundle._freeze(conn, "NIFTY", "2026-10-06"), 3510)
        self.assertEqual(bundle._freeze(conn, "ACME", "2026-11-05"), 25000)
        self.assertIsNone(bundle._freeze(conn, "ACME", "2026-11-04"))
        self.assertIsNone(bundle._freeze(conn, None, "2026-11-05"))

    def test_cross_check_reports_disagreements(self):
        self._snapshot("NIFTY", "2026-10-06", 1800)        # disagrees with the 10-05 circular
        self._snapshot("BANKNIFTY", "2026-11-05", 900)     # agrees with the 11-02 circular
        self._snapshot("ACME", "2026-11-05", 25000)        # a stock: nothing to compare
        self.assertEqual(bundle.freeze_cross_check(refdata._connect()),
                         [{"underlying": "NIFTY", "date": "2026-10-06", "snapshot": 1800, "circular": 3510}])




class LastTradingMomentTests(unittest.TestCase):
    """M4.1: a derivative's last trading moment is the FO close on its last day, from sessions.json (X-7):
    15:30 IST before 2026-08-03, 15:40 IST from then on."""

    def test_fo_close_before_and_after_2026_08_03(self):
        from datetime import date
        from stockify import timeutil
        for day, hhmm in ((date(2026, 7, 30), "15:30"), (date(2026, 7, 31), "15:30"),
                          (date(2026, 8, 3), "15:40"), (date(2026, 8, 4), "15:40")):
            d = bundle.resolve_days(day, day, {})[0]
            for group in ("FO_FUT", "FO_OPT"):
                with self.subTest(day=day, group=group):
                    self.assertEqual(bundle.group_close_ns(d, group), timeutil.ist_ns(day, hhmm))


if __name__ == "__main__":
    unittest.main()

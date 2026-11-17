"""HD 4.1: the desk store (desk.sqlite): evidence, stored views and chain snapshots, kept for every day."""
from __future__ import annotations

import unittest

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

from stockify import paths
from stockify.desk import store


def _evidence(day: str, iid: int, ref: str, grade: str = "some", **kw) -> dict:
    row = {"trade_date": day, "instrument_id": iid, "ref": ref, "params": {"symbol": "NSE:X"},
           "from_date": "2021-01-04", "to_date": "2025-06-30", "trades": 12, "cagr": 0.1, "buy_hold_cagr": 0.08,
           "sharpe": 0.9, "buy_hold_sharpe": 0.7, "max_drawdown": 0.2, "win_rate": 0.5,
           "windows": [{"year": 2021, "sharpe": 1.0, "buy_hold_sharpe": 0.5, "won": True}],
           "windows_won": 1, "windows_total": 1, "grade": grade, "note": "Beat holding the stock in 1 of 1 years."}
    row.update(kw)
    return row


class DeskStoreTests(SandboxedDBTest):
    db_modules = (store,)

    def test_the_store_lives_in_the_lab_folder(self):
        self.assertEqual(store._db_path(), paths.desk_db())
        self.assertEqual(paths.desk_db().name, "desk.sqlite")
        self.assertEqual(paths.desk_db().parent, paths.lab_dir())

    def test_store_keeps_every_day(self):
        store.put_view("2026-11-16", 7, "INFY", "Hold", {"verdict": "Hold", "n": 1}, 100)
        store.put_view("2026-11-17", 7, "INFY", "Exit", {"verdict": "Exit", "n": 2}, 200)
        store.put_view("2026-11-17", 9, "TCS", "Hold", {"verdict": "Hold"}, 300)
        latest = store.latest_view(7)
        self.assertEqual((latest["trade_date"], latest["verdict"], latest["view"]["n"], latest["made_ns"]),
                         ("2026-11-17", "Exit", 2, 200))
        days = store.views_for(7)
        self.assertEqual([v["trade_date"] for v in days], ["2026-11-16", "2026-11-17"])
        self.assertEqual(days[0]["view"], {"verdict": "Hold", "n": 1})
        self.assertEqual([(v["instrument_id"], v["symbol"]) for v in store.latest_views()], [(7, "INFY"), (9, "TCS")])
        self.assertIsNone(store.latest_view(8))

    def test_a_view_rewritten_on_the_same_day_replaces_only_that_day(self):
        store.put_view("2026-11-16", 7, "INFY", "Hold", {}, 100)
        store.put_view("2026-11-17", 7, "INFY", "Hold", {}, 200)
        store.put_view("2026-11-17", 7, "INFY", "Trim", {"again": True}, 250)
        self.assertEqual([(v["trade_date"], v["verdict"]) for v in store.views_for(7)],
                         [("2026-11-16", "Hold"), ("2026-11-17", "Trim")])

    def test_evidence_round_trip_and_latest_day_only(self):
        store.put_evidence([_evidence("2026-11-16", 7, "a:A", grade="weak"), _evidence("2026-11-16", 7, "b:B")])
        store.put_evidence([_evidence("2026-11-17", 7, "a:A", grade="strong", sharpe=None),
                            _evidence("2026-11-17", 8, "a:A")])
        rows = store.latest_evidence(7)
        self.assertEqual([(r["trade_date"], r["ref"], r["grade"]) for r in rows], [("2026-11-17", "a:A", "strong")])
        r = rows[0]
        self.assertIsNone(r["sharpe"])
        self.assertEqual(r["params"], {"symbol": "NSE:X"})
        self.assertEqual(r["windows"], [{"year": 2021, "sharpe": 1.0, "buy_hold_sharpe": 0.5, "won": True}])
        self.assertEqual((r["windows_won"], r["windows_total"], r["trades"]), (1, 1, 12))
        self.assertEqual(len(store.evidence_for(7, "2026-11-16")), 2)        # the older day is still there
        self.assertEqual(store.latest_evidence(99), [])

    def test_evidence_rows_must_carry_every_column(self):
        bad = _evidence("2026-11-17", 7, "a:A")
        del bad["grade"]
        with self.assertRaisesRegex(ValueError, "grade"):
            store.put_evidence([bad])
        self.assertEqual(store.latest_evidence(7), [])

    def test_chain_snapshots_are_kept_and_the_newest_is_read(self):
        store.put_chain(7, 1_000, {"strikes": [1]})
        store.put_chain(7, 3_000, {"strikes": [3]})
        store.put_chain(7, 2_000, {"strikes": [2]})
        self.assertEqual(store.latest_chain(7), {"instrument_id": 7, "taken_ns": 3_000, "snapshot": {"strikes": [3]}})
        self.assertIsNone(store.latest_chain(8))
        self.assertEqual(store._connect().execute("SELECT COUNT(*) FROM chain_snapshots").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()

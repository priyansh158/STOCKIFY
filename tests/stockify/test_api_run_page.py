"""RF 7.x: what the run page is told about instruments, strategies and fills (review fixes, track B)."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest
from tests.stockify.charts.test_chart_model import T, fill, make_run, order

import stockify.store as store

REPO = Path(__file__).resolve().parents[2]

# A bhavcopy-only option: refdata holds no tradingsymbol for it (A1 §1), only the contract's natural key.
OPT = {"id": 707289, "tradingsymbol": None, "kind": "CE", "underlying": "NIFTY", "expiry": "2026-11-09",
       "strike_paise": 2450000, "session_group": "FO_OPT", "cas_dates": []}
FUT = {"id": 5, "tradingsymbol": "NIFTY26NOVFUT", "kind": "FUT", "underlying": "NIFTY", "expiry": "2026-11-26",
       "strike_paise": None, "session_group": "FO_FUT", "cas_dates": []}


class DisplayNameTests(unittest.TestCase):
    def test_symbol_wins_and_bhavcopy_contracts_are_named_from_their_key(self):
        from stockify.data.instruments import display_name
        self.assertEqual(display_name(FUT), "NIFTY26NOVFUT")
        self.assertEqual(display_name(OPT), "NIFTY 24500 CE (9 Nov 2026)")
        self.assertEqual(display_name({**OPT, "strike_paise": 2450050}), "NIFTY 24500.50 CE (9 Nov 2026)")
        self.assertEqual(display_name({**FUT, "tradingsymbol": None}), "NIFTY FUT (26 Nov 2026)")
        self.assertIsNone(display_name({"id": 1, "tradingsymbol": None}))
        self.assertEqual(display_name({"tradingsymbol": None, "underlying": "RELIANCE", "kind": "EQ"}), "RELIANCE")
        self.assertEqual(display_name({"tradingsymbol": None, "underlying": "NIFTY 50", "kind": "INDEX"}), "NIFTY 50")
        self.assertIsNone(display_name(None))


@support.requires_fastapi
class InstrumentNamesTests(SandboxedDBTest):
    db_modules = (store,)

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from server.main import app
        self.client = TestClient(app, base_url=support.API_BASE_URL)

    def sql(self, text: str, args: tuple = ()) -> None:
        with store._lock:
            conn = store._connect()
            conn.execute(text, args)
            conn.commit()

    def run_id(self):
        o, f = order(1, iid=707289), fill(1, 1, T(9, 16), "SELL", 65, 18480, iid=707289)
        rid = make_run(orders=[o], fills=[f], instruments=[OPT, FUT])
        store.create_run({"run_id": rid, "status": "done"})
        store.replace_run_rows(rid, orders=[o], fills=[f], equity=[], charges=[])
        return rid

    def test_fills_name_a_contract_that_has_no_tradingsymbol(self):
        rid = self.run_id()
        got = self.client.get(f"/api/tests/{rid}/fills").json()
        self.assertEqual(got[0]["tradingsymbol"], "NIFTY 24500 CE (9 Nov 2026)")

    def test_chart_picker_names_it_too(self):
        rid = self.run_id()
        d = self.client.get(f"/api/tests/{rid}/chart").json()
        self.assertEqual([i["symbol"] for i in d["instruments"]], ["NIFTY 24500 CE (9 Nov 2026)"])

    def test_a_truly_unknown_instrument_says_so(self):
        rid = make_run(orders=[order(1)], fills=[fill(1, 1, T(9, 16), "BUY", 1, 100)], instruments=[OPT])
        store.create_run({"run_id": rid, "status": "done"})
        d = self.client.get(f"/api/tests/{rid}/chart").json()
        self.assertEqual(d["instruments"][0]["symbol"], "instrument 1")


    def test_a_run_without_an_evidence_column_has_no_close_basis(self):
        o, f = order(1, iid=707289), fill(1, 1, T(9, 16), "SELL", 65, 18480, iid=707289)
        rid = make_run(orders=[o], fills=[f], instruments=[OPT])
        store.create_run({"run_id": rid, "status": "done"})
        store.replace_run_rows(rid, orders=[o], fills=[f], equity=[], charges=[])
        from stockify import paths
        p = paths.run_path(rid) / "fills.csv"
        p.write_text("fill_id,order_id,t_ns,instrument_id,side,qty,price_paise" + chr(10) + "1,1,1,707289,SELL,65,18480" + chr(10), encoding="utf-8")
        got = self.client.get(f"/api/tests/{rid}/fills").json()
        self.assertEqual((got[0]["evidence"], got[0]["at_close"]), ("", False))

    def test_fills_carry_their_own_reason_and_close_basis(self):
        o1, o2 = order(1, iid=707289, note="Sell the straddle", tag="entry"), order(2, iid=5, side="BUY", tag="exit")
        f1 = fill(1, 1, T(9, 16), "SELL", 65, 18480, iid=707289)
        f2 = fill(2, 2, T(9, 17), "BUY", 65, 100, iid=5)
        f1["evidence"], f2["evidence"] = "eod_close", "bar_open"
        rid = make_run(orders=[o1, o2], fills=[f1, f2], instruments=[OPT, FUT])
        store.create_run({"run_id": rid, "status": "done"})
        store.replace_run_rows(rid, orders=[o1, o2], fills=[f1, f2], equity=[], charges=[])
        got = self.client.get(f"/api/tests/{rid}/fills").json()
        self.assertEqual([(g["note"], g["tag"]) for g in got], [("Sell the straddle", "entry"), ("", "exit")])
        self.assertEqual([(g["evidence"], g["at_close"]) for g in got], [("eod_close", True), ("bar_open", False)])

    def test_one_unreadable_order_row_costs_only_its_own_note(self):
        """Review M4: a malformed stored order row (not JSON, or not an object) is "no note" for that order, not a 500."""
        o1, o2, o3 = (order(1, iid=707289, note="Sell the straddle", tag="entry"), order(2, iid=5, note="n2"),
                      order(3, iid=5, note="n3"))
        fs = [fill(1, 1, T(9, 16), "SELL", 65, 18480, iid=707289), fill(2, 2, T(9, 17), "BUY", 65, 100, iid=5),
              fill(3, 3, T(9, 18), "BUY", 65, 100, iid=5)]
        rid = make_run(orders=[o1, o2, o3], fills=fs, instruments=[OPT, FUT])
        store.create_run({"run_id": rid, "status": "done"})
        store.replace_run_rows(rid, orders=[o1, o2, o3], fills=fs, equity=[], charges=[])
        self.sql("UPDATE run_orders SET row_json = ? WHERE run_id = ? AND order_id = 2", ("{not json", rid))
        self.sql("UPDATE run_orders SET row_json = ? WHERE run_id = ? AND order_id = 3", ("[1, 2]", rid))
        r = self.client.get(f"/api/tests/{rid}/fills")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([(g["note"], g["tag"]) for g in r.json()], [("Sell the straddle", "entry"), ("", ""), ("", "")])


@unittest.skipUnless(shutil.which("node"), "node is not installed: the run page helper checks need node >= 22.18")
class RunViewHelperChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run(["node", str(Path(__file__).with_name("run_view_check.ts"))], capture_output=True,
                             text=True, timeout=60, cwd=REPO, encoding="utf-8")
        if out.returncode != 0:
            raise AssertionError("run_view_check.ts failed: " + out.stderr)
        cls.r = json.loads(out.stdout)

    def test_a_close_fill_reads_as_the_close_and_explains_its_stamp(self):
        c = self.r["close"]
        self.assertEqual(c["text"], "5 Oct 2026, at the close")
        self.assertIn("19:00 IST", c["hint"])
        # driven by the server's flag, not by the clock text: another stamp reads the same, and 19:00 alone proves nothing
        self.assertEqual(self.r["closeOther"]["text"], "12 Oct 2026, at the close")
        self.assertIn("20:30 IST", self.r["closeOther"]["hint"])
        self.assertEqual(self.r["intraday"], {"text": "5 Oct 2026, 19:00", "hint": None})  # the one date format people read (D-148)

    def test_why_is_always_the_reason_never_the_chart_hint(self):
        self.assertEqual(self.r["whyNote"], {"text": "Range broke up", "tag": None, "raw": None})
        self.assertEqual(self.r["whyNone"], {"text": "Sold 65 (no note from the strategy)", "tag": "Entry", "raw": "entry"})
        self.assertEqual(self.r["whyBare"], {"text": "Bought 3 (no note from the strategy)", "tag": None, "raw": None})

    def test_live_session_without_prices_says_they_have_not_arrived(self):
        live, past, pruned = self.r["pruned"]
        self.assertIn("No prices have arrived yet", live)
        self.assertIn("No price data was recorded", past)
        self.assertIn("no longer on disk", pruned)

    def test_chart_hint_only_when_a_chart_is_drawn_and_lacks_the_fill(self):
        none, hint, marked, undrawn = self.r["hints"]
        self.assertIsNone(none)
        self.assertIn("Not on the chart now showing", hint)
        self.assertIsNone(marked)
        self.assertIsNone(undrawn)


if __name__ == "__main__":
    unittest.main()

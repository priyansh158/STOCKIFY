"""RF 7.x: what the run page is told about instruments, strategies and fills (review fixes, track B)."""
from __future__ import annotations

import unittest

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest
from tests.stockify.charts.test_chart_model import T, fill, make_run, order

import stockify.store as store

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
        self.assertIsNone(display_name(None))


@support.requires_fastapi
class InstrumentNamesTests(SandboxedDBTest):
    db_modules = (store,)

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from server.main import app
        self.client = TestClient(app, base_url=support.API_BASE_URL)

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


if __name__ == "__main__":
    unittest.main()

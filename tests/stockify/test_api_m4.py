"""M4.14 (WP 4.1): the tests API's F&O additions, /api/tests/{id}/margin and /api/tests/{id}/settlements.

Built on real run folders: G10 (index options settled in cash, at the FSP or the fallback) and G11 (stock F&O
settled by delivery), run by the oracle and imported, as the queue would leave them."""
from __future__ import annotations

import copy
import unittest

import tests.support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import stockify.store as store
from stockify import golden, paths, runs
from stockify.oracle import run_bundle


def _variant(gid: str, name: str) -> dict:
    g = next(g for _, g in golden.goldens([gid]))
    v = next(v for v in g["variants"] if v["name"] == name)
    h = {k: copy.deepcopy(x) for k, x in g.items() if k != "variants"}
    for part in ("bundle", "run", "strategy"):
        if part in v:
            h[part] = golden._merge(h.get(part, {}), v[part])
    if "drop_events" in v:
        h["events"] = [e for n, e in enumerate(g["events"], start=1) if n not in v["drop_events"]]
    return h


class FnoApiTests(SandboxedDBTest):
    db_modules = (store,)

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from server.main import app
        self.client = TestClient(app)

    def _run(self, run_id: str, gid: str, variant: str):  # noqa: ANN202
        h = _variant(gid, variant)
        scratch = paths.lab_dir() / f"scratch-{run_id}"
        scratch.mkdir()
        folder = paths.run_path(run_id)
        run_bundle(golden.build_bundle(h), [golden.build_pack(h, scratch)], folder, report=False)
        runs.import_run(folder)
        return folder

    def test_cash_settlements_at_the_fsp_and_the_fallback(self):
        self._run("g10-fsp", "G10", "fsp")
        self._run("g10-fallback", "G10", "fallback")
        for run_id, source in (("g10-fsp", "settle"), ("g10-fallback", "fallback")):
            with self.subTest(run=run_id):
                r = self.client.get(f"/api/tests/{run_id}/settlements")
                self.assertEqual(r.status_code, 200)
                rows = r.json()
                self.assertEqual(len(rows), 3)
                self.assertEqual({x["kind"] for x in rows}, {"cash"})
                self.assertEqual({x["source"] for x in rows}, {source})
                self.assertEqual({x["price_paise"] for x in rows}, {2450000})
                self.assertEqual({x["trade_date"] for x in rows}, {"2026-03-03"})
                self.assertTrue(all(x["tradingsymbol"] for x in rows))
                self.assertEqual(sorted(x["charges_paise"] for x in rows), [2360, 2360, 6423])

    def test_a_delivery(self):
        self._run("g11", "G11", "fsp")
        rows = self.client.get("/api/tests/g11/settlements").json()
        # G11: the future takes 100 shares at the FSP, the ITM call takes 100 at its strike, the ITM put gives 100
        deliveries = [(x["product"], x["side"], x["qty"], x["price_paise"], x["source"])
                      for x in rows if x["kind"] == "delivery"]
        self.assertEqual(deliveries, [("CNC", "BUY", 100, 102000, "settle"), ("CNC", "BUY", 100, 95000, "settle"),
                                      ("CNC", "SELL", 100, 105000, "settle")])
        self.assertEqual({x["tradingsymbol"] for x in rows if x["kind"] == "delivery"}, {"ACME"})
        self.assertEqual(len([x for x in rows if x["kind"] == "cash"]), 3)

    def test_margin_rows_from_margin_daily(self):
        folder = self._run("g10-margin", "G10", "fsp")
        self.assertEqual(self.client.get("/api/tests/g10-margin/margin").json(), [])     # flat model: no rows
        (folder / "margin_daily.csv").write_text(
            "trade_date,underlying_id,model,step,scan,somc,elm,addons,total\n"
            "2026-03-02,1,spanlite,B1_span_nse_elm,1200000,0,300000,0,1500000\n"
            "2026-03-03,1,spanlite,B4_spanlite_placeholder,900000,10000,250000,50000,1210000\n", encoding="utf-8", newline="")
        rows = self.client.get("/api/tests/g10-margin/margin").json()
        self.assertEqual([(r["trade_date"], r["model"], r["scan_paise"], r["elm_paise"], r["addons_paise"], r["total_paise"])
                          for r in rows],
                         [("2026-03-02", "spanlite", 1200000, 300000, 0, 1500000),
                          ("2026-03-03", "spanlite", 900000, 250000, 50000, 1210000)])
        self.assertEqual([r["step"] for r in rows], ["B1_span_nse_elm", "B4_spanlite_placeholder"])
        self.assertTrue(rows[0]["underlying"])

    def test_unknown_runs_404(self):
        for path in ("margin", "settlements"):
            self.assertEqual(self.client.get(f"/api/tests/nope/{path}").status_code, 404)


if __name__ == "__main__":
    unittest.main()

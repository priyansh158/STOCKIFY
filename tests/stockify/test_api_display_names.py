"""AM 7.x: the API tells the web every name in plain words (strategy, order tag, chart line, compared setting)."""
from __future__ import annotations

import json
import unittest

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest
from tests.stockify.charts.test_chart_model import T, fill, make_run, order

import stockify.store as store

ORB = "stockify.strategies.opening_range_breakout:OpeningRangeBreakout"


def _cfg(ref=ORB, **extra) -> str:
    return json.dumps({"strategy": {"kind": "python", "ref": ref, "params": {"range_minutes": 15}},
                       "fill_model": {"kind": "bar"}, "margin": {"model": "staircase"}, **extra})


@support.requires_fastapi
class DisplayNameApiTests(SandboxedDBTest):
    db_modules = (store,)

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from server.main import app
        self.client = TestClient(app, base_url=support.API_BASE_URL)

    def test_strategies_carry_display_names_and_param_labels(self):
        got = self.client.get("/api/strategies").json()
        self.assertTrue(got)
        for s in got:
            self.assertTrue(s["display_name"].strip() and "_" not in s["display_name"], s["ref"])
            for p in s["params"]:
                self.assertTrue(p["label"].strip() and "_" not in p["label"], f"{s['ref']}.{p['name']}")
        by = {s["ref"]: s["display_name"] for s in got}
        self.assertEqual(by[ORB], "Opening Range Breakout")

    def test_run_list_and_detail_send_the_strategy_name(self):
        store.create_run({"run_id": "r1", "name": "orb_reliance_minute", "status": "done", "config_json": _cfg()})
        store.create_run({"run_id": "r2", "name": "mine", "status": "done",
                          "config_json": _cfg("my_scripts.mean_reversion:MeanReversion")})
        names = {r["run_id"]: r["strategy_name"] for r in self.client.get("/api/tests").json()}
        self.assertEqual(names, {"r1": "Opening Range Breakout", "r2": "Mean Reversion"})
        self.assertEqual(self.client.get("/api/tests/r1").json()["run"]["strategy_name"], "Opening Range Breakout")
        store.create_run({"run_id": "r3", "name": "x", "status": "done"})      # no config at all
        self.assertIsNone(self.client.get("/api/tests/r3").json()["run"]["strategy_name"])

    def test_names_the_person_chose_come_as_words_with_acronyms_upright(self):
        store.create_run({"run_id": "n1", "name": "orb_reliance_minute", "status": "done", "config_json": _cfg()})
        store.create_run({"run_id": "n2", "name": "a_-_b", "status": "done", "config_json": _cfg()})
        got = {r["run_id"]: (r["name"], r["name_label"]) for r in self.client.get("/api/tests").json()}
        self.assertEqual(got["n1"], ("orb_reliance_minute", "ORB reliance minute"))   # the stored name is untouched
        self.assertEqual(got["n2"], ("a_-_b", "A - b"))
        d = self.client.get("/api/tests/compare?ids=n1&ids=n2").json()
        self.assertEqual(d["name_labels"], ["ORB reliance minute", "A - b"])
        self.assertEqual(d["names"], ["orb_reliance_minute", "a_-_b"])

    def test_fills_and_chart_markers_send_a_plain_tag(self):
        o = order(1, tag="orb_long", side="BUY")
        f = fill(1, 1, T(9, 30), "BUY", 5, 1000)
        rid = make_run(orders=[o], fills=[f], plots=[{"t_ns": T(9, 30), "instrument_id": 1, "name": "range_high",
                                                     "kind": "price", "value": 1000}])
        store.create_run({"run_id": rid, "status": "done"})
        store.replace_run_rows(rid, orders=[o], fills=[f], equity=[], charges=[])
        got = self.client.get(f"/api/tests/{rid}/fills").json()[0]
        self.assertEqual((got["tag"], got["tag_label"]), ("orb_long", "Long entry"))
        chart = self.client.get(f"/api/tests/{rid}/chart").json()
        self.assertEqual([(m["tag"], m["tag_label"]) for m in chart["markers"]], [("orb_long", "Long entry")])
        self.assertEqual([(p["name"], p["label"]) for p in chart["plots"]], [("range_high", "Range high")])

    def test_compare_says_what_differs_in_plain_words(self):
        a = _cfg()
        b = json.dumps({"strategy": {"kind": "python", "ref": ORB, "params": {"range_minutes": 30}},
                        "fill_model": {"kind": "eod_close"}, "margin": {"model": "flat"}})
        for rid, cfg in (("c1", a), ("c2", b)):
            store.create_run({"run_id": rid, "name": rid, "status": "done", "config_json": cfg})
        d = self.client.get("/api/tests/compare?ids=c1&ids=c2").json()
        rows = {r["field"]: r for r in d["manifest_diff"]}
        self.assertEqual(rows["strategy.ref"]["label"], "Strategy")
        self.assertEqual(rows["strategy.ref"]["shown"], ["Opening Range Breakout"] * 2)
        self.assertEqual(rows["strategy.params.range_minutes"]["label"], "Opening range (minutes)")
        self.assertEqual(rows["strategy.params.range_minutes"]["shown"], ["15", "30"])
        self.assertEqual(rows["fill_model"]["label"], "How orders fill")
        self.assertEqual(rows["fill_model"]["shown"], ["From bars", "At the day's close (end-of-day files)"])
        self.assertEqual(rows["margin.model"]["label"], "How margin is worked out")
        self.assertEqual(rows["margin.model"]["shown"], ["Best available, SPAN files first", "Flat percentage (approximate)"])
        for r in d["manifest_diff"]:
            self.assertTrue(r["label"] and "_" not in r["label"] and "." not in r["label"], r["field"])


if __name__ == "__main__":
    unittest.main()

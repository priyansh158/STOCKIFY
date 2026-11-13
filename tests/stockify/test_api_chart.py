"""CH 6.11: GET /api/tests/{run_id}/chart and its DTOs. Run folders are written by the C10 test helpers."""
from __future__ import annotations

import json
import unittest

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest
from tests.stockify.charts.test_chart_model import DAY, FUT, NS, T, bar_world, fill, make_run, order

import stockify.store as store
from stockify import paths


def mk(**kw) -> str:
    """A run folder (C10 helper) plus the lab.sqlite row the sibling routes also require."""
    rid = make_run(**kw)
    store.create_run({"run_id": rid, "status": "done"})
    return rid


def plot_rows(price: int = 10001, level: int = 543217) -> list[dict]:
    return [{"t_ns": T(9, 15, 5), "instrument_id": 1, "name": "sma", "kind": "price", "value": price},
            {"t_ns": T(9, 17, 5), "instrument_id": 0, "name": "rsi", "kind": "level", "value": level}]


@support.requires_fastapi
class ChartApiTests(SandboxedDBTest):
    db_modules = (store,)

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from server.main import app
        self.client = TestClient(app, base_url=support.API_BASE_URL)

    def get(self, rid, **q):
        return self.client.get(f"/api/tests/{rid}/chart", params=q)

    def simple(self, **kw):
        return mk(orders=[order(1, note="<b>breakout</b> & more"), order(2, side="SELL")],
                        fills=[fill(1, 1, T(9, 16, 5), "BUY", 10, 10010), fill(2, 2, T(9, 18, 5), "SELL", 10, 10050)],
                        **kw)

    def test_chart_returns_bars_markers_and_plots(self):
        rid = self.simple(plots=plot_rows())
        r = self.get(rid)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual((d["data_state"], d["interval_s"], d["instrument_id"], d["truncated"]), ("ok", 60, 1, False))
        self.assertEqual(d["instruments"], [{"id": 1, "symbol": "TEST", "kind": "equity"}])
        b = d["bars"][0]
        self.assertEqual((b["t"], b["d"], b["volume"]), (T(9, 15) // NS, DAY, 100))
        self.assertEqual(list(b), ["t", "d", "open", "high", "low", "close", "volume"])
        m = d["markers"]
        self.assertEqual([x["id"] for x in m], ["f1", "f2"])
        self.assertEqual((m[0]["side"], m[1]["side"], m[0]["qty"], m[0]["price"]), ("BUY", "SELL", 10, "100.10"))
        self.assertEqual((m[0]["t"], m[0]["bar_t"], m[0]["bar_d"]), (T(9, 16, 5) // NS, T(9, 16) // NS, DAY))
        self.assertEqual(m[1]["trade_pnl_paise"], 400)
        self.assertEqual({p["kind"] for p in d["plots"]}, {"price", "level"})
        for k in ("omitted_markers", "notice", "pnl_complete", "skipped_settlements", "idle_settlements", "plain"):
            self.assertIn(k, d)

    def test_prices_are_exact_rupee_strings(self):
        ev = [{"kind": "BAR", "instrument": 1, "interval_s": 60, "start": T(9, 15), "o": 241840, "h": 241845,
               "l": 5, "c": 100, "v": 7}]
        rid = mk(orders=[order(1)], fills=[fill(1, 1, T(9, 15, 30), "BUY", 1, 241840)],
                       world={"id": "px", "events": ev}, plots=plot_rows(price=241840, level=-12345))
        text = self.get(rid).text
        d = json.loads(text)
        b = d["bars"][0]
        self.assertEqual((b["open"], b["high"], b["low"], b["close"]), ("2418.40", "2418.45", "0.05", "1.00"))
        self.assertEqual((d["markers"][0]["price"], d["markers"][0]["price_paise"]), ("2418.40", 241840))
        by = {p["kind"]: p["points"][0]["value"] for p in d["plots"]}
        self.assertEqual(by, {"price": "2418.40", "level": "-1.2345"})
        self.assertNotRegex(text, r'"(open|high|low|close|price|value)":\s*-?\d')       # never a JSON number

    def test_range_request_matches_the_whole_run_slice(self):
        rid = self.simple()
        whole = self.get(rid).json()
        lo, hi = T(9, 16), T(9, 18)
        part = self.get(rid, from_ns=lo, to_ns=hi).json()
        self.assertEqual(part["bars"], [b for b in whole["bars"] if lo // NS <= b["t"] < hi // NS])
        self.assertEqual([m["id"] for m in part["markers"]], ["f1"])
        self.assertEqual(part["omitted_markers"], 1)
        self.assertFalse(part["truncated"])

    def test_paging_through_next_from_ns(self):
        from stockify.reporting import chart as model
        rid = mk(orders=[order(1)], fills=[fill(1, 1, T(9, 15, 30), "BUY", 1, 10000)], world=bar_world(7))
        old = model.MAX_BARS
        model.MAX_BARS = 3
        self.addCleanup(setattr, model, "MAX_BARS", old)
        got, frm = [], T(9, 15)
        for _ in range(5):
            d = self.get(rid, from_ns=frm, to_ns=T(10, 0)).json()
            got += [b["t"] for b in d["bars"]]
            if not d["truncated"]:
                break
            frm = d["next_from_ns"]
        self.assertEqual(got, [T(9, 15 + k) // NS for k in range(7)])

    def test_interval_param_and_notice(self):
        d = self.get(self.simple(), interval_s=300).json()
        self.assertEqual(d["interval_s"], 60)
        self.assertIn("60-second bars only", d["notice"])

    def test_chart_404_for_unknown_run(self):
        r = self.get("no-such-run")
        self.assertEqual(r.status_code, 404)
        self.assertNotIn("no-such-run", r.text)

    def test_unknown_instrument_is_404(self):
        self.assertEqual(self.get(self.simple(), instrument=99).status_code, 404)

    def test_chart_for_a_pruned_run_is_200_with_data_state(self):
        r = self.get(self.simple(sha="cd" * 32))
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual((d["data_state"], d["bars"]), ("pruned", []))
        self.assertEqual(len(d["markers"]), 2)
        self.assertTrue(d["plain"])

    def test_a_pack_that_cannot_be_read_is_200_with_its_state(self):
        sha = "ab" * 32
        (paths.packs_dir() / f"{sha}.evt").write_bytes(b"not a pack at all" * 10)
        r = self.get(self.simple(sha=sha))
        self.assertEqual((r.status_code, r.json()["data_state"]), (200, "pack unreadable"))
        self.assertTrue(r.json()["plain"])
        self.assertNotIn("PackError", r.text)

    def test_settlement_marker_fields(self):
        log = f"{T(15, 30)} INFO settled 1 MIS at FSP 10100 (settle)\n"
        rid = mk(orders=[order(1)], fills=[fill(1, 1, T(9, 15, 30), "BUY", 10, 10000)], log=log, instruments=FUT)
        d = self.get(rid).json()
        s = d["markers"][1]
        self.assertEqual((s["kind"], s["side"], s["fill_id"], s["order_id"], s["fsp_paise"], s["price"]),
                         ("settlement", "SELL", None, None, 10100, "101.00"))
        self.assertTrue(s["id"].startswith("x"))
        self.assertEqual((d["pnl_complete"], d["skipped_settlements"], d["idle_settlements"]), (True, 0, 0))

    def test_instrument_kind_comes_from_the_bundle(self):
        kinds = [("FUT", "future"), ("CE", "option"), ("PE", "option"), ("EQ", "equity"), ("WEIRD", None)]
        for raw, want in kinds:
            rid = mk(orders=[order(1)], fills=[fill(1, 1, T(9, 16), "BUY", 1, 10000)],
                           instruments=[{"id": 1, "tradingsymbol": "X", "kind": raw}])
            self.assertEqual(self.get(rid).json()["instruments"][0]["kind"], want, raw)

    def test_bad_query_is_422(self):
        rid = self.simple()
        for q in ({"instrument": -1}, {"instrument": "x"}, {"from_ns": -1}, {"to_ns": "abc"},
                  {"from_ns": 5, "to_ns": 5}, {"from_ns": 9, "to_ns": 5}, {"interval_s": 0}, {"interval_s": -60},
                  {"interval_s": "x"}):
            self.assertEqual(self.get(rid, **q).status_code, 422, q)
        self.assertIn("interval", self.get(rid, interval_s=0).text)

    def test_responses_have_no_nan(self):
        d = self.get(self.simple(plots=plot_rows())).text
        for token in ("NaN", "Infinity"):
            self.assertNotIn(token, d)
        json.loads(d, parse_constant=lambda c: self.fail(c))

    def test_non_loopback_host_refused(self):
        r = self.client.get(f"/api/tests/{self.simple()}/chart", headers={"Host": "evil.example"})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("bars", r.text)

    def test_note_is_returned_verbatim_not_escaped(self):
        m = self.get(self.simple()).json()["markers"][0]
        self.assertEqual(m["note"], "<b>breakout</b> & more")

    def test_run_id_cannot_escape_the_runs_folder(self):
        for rid in ("..%5C..%5Cx", "%2E%2E%2F%2E%2E%2Fx", "%2E%2E", "a%00b", "x%2F..%2F..%2Fy"):
            r = self.client.get(f"/api/tests/{rid}/chart")
            self.assertEqual(r.status_code, 404, rid)
            # an id with an encoded slash matches no route at all (the framework's own 404); the rest reach run_path
            self.assertIn(r.json()["detail"], ("no such test run", "Not Found"), rid)
        from stockify import paths
        for bad in ("..\\..\\x", "../x", "a\x00b"):
            with self.assertRaises(ValueError):
                paths.run_path(bad)

    def test_a_folder_without_a_run_row_is_an_unknown_run(self):
        rid = make_run(orders=[order(1)], fills=[fill(1, 1, T(9, 16), "BUY", 1, 10000)])    # no lab.sqlite row
        self.assertEqual(self.get(rid).status_code, 404)

    def test_no_pack_state(self):
        d = self.get(self.simple(sha=None)).json()
        self.assertEqual((d["data_state"], d["bars"]), ("no_pack", []))
        self.assertTrue(d["plain"])

    def test_interval_over_a_year_and_ns_over_int64_are_422(self):
        rid = self.simple()
        for q in ({"interval_s": 366 * 86400 + 1}, {"from_ns": 2**63}, {"to_ns": 2**63}, {"instrument": 2**63}):
            self.assertEqual(self.get(rid, **q).status_code, 422, q)
        self.assertEqual(self.get(rid, interval_s=366 * 86400).status_code, 200)

    def test_a_model_value_error_is_a_plain_422(self):
        from unittest.mock import patch
        with patch("stockify.reporting.chart.run_chart", side_effect=ValueError("secret C:\\x\\y")):
            r = self.get(self.simple())
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json(), {"detail": "the chart request is not valid"})

    def test_an_incomplete_profit_always_has_a_notice(self):
        from server.routers.tests import _chart_dto
        from stockify.reporting.chart import run_chart
        m = run_chart(self.simple())
        self.assertIsNone(m["notice"])
        m["pnl_complete"] = False                            # a model that says so but gave no sentence
        d = _chart_dto(m)
        self.assertTrue(d.notice)
        self.assertNotIn("Traceback", d.notice)
        m["notice"] = "kept as is"
        self.assertEqual(_chart_dto(m).notice, "kept as is")

    def test_literal_fields_refuse_values_the_types_do_not_list(self):
        from pydantic import ValidationError
        from server.schemas import ChartInstrumentDTO, ChartPlotDTO
        with self.assertRaises(ValidationError):
            ChartInstrumentDTO(id=1, symbol="X", kind="bond")
        with self.assertRaises(ValidationError):
            ChartPlotDTO(name="a", kind="volume", instrument_id=1, points=[])
        self.assertEqual(ChartInstrumentDTO(id=1, symbol="X").kind, None)

    def test_a_continuous_future_is_a_future(self):
        rid = mk(orders=[order(1)], fills=[fill(1, 1, T(9, 16), "BUY", 1, 10000)],
                 instruments=[{"id": 1, "tradingsymbol": "NIFTY-I", "kind": "FUT_CONT"}])
        self.assertEqual(self.get(rid).json()["instruments"][0]["kind"], "future")

    def test_fixed_paths_are_not_shadowed(self):
        r = self.client.get("/api/tests/compare")          # the compare route (needs ids), not a run called "compare"
        self.assertEqual(r.status_code, 422)
        self.assertNotIn("no such", r.text)

    def test_dto_has_the_c10_fields(self):
        from server.schemas import ChartMarkerDTO, RunChartDTO
        for k in ("data_state", "omitted_markers", "notice", "pnl_complete", "skipped_settlements",
                  "idle_settlements", "plain", "next_from_ns"):
            self.assertIn(k, RunChartDTO.model_fields)
        self.assertIn("fsp_paise", ChartMarkerDTO.model_fields)


if __name__ == "__main__":
    unittest.main()

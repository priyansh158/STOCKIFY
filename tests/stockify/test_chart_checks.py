"""The chart helpers (web/src/lib/chart.ts) on hand cases: times, markers, the trade card, paging (CH 6.14).

Needs node (>= 22.18, which runs .ts directly); skipped with a message when it is missing."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

REPO = Path(__file__).resolve().parents[2]
CHECK = Path(__file__).with_name("chart_check.ts")
NOTE = "<img src=x onerror=alert(1)>"


def _strings(value):
    """Every string inside a nested JSON value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


@unittest.skipUnless(shutil.which("node"), "node is not installed: the chart helper checks need node >= 22.18")
class ChartHelperChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run(["node", str(CHECK)], capture_output=True, text=True, timeout=60, cwd=REPO,
                             encoding="utf-8")
        if out.returncode != 0:
            raise AssertionError(f"chart_check.ts failed:\n{out.stderr}")
        cls.r = json.loads(out.stdout)

    def test_ist_shift_is_five_and_a_half_hours(self):
        self.assertEqual(self.r["istShift"], 19800)

    def test_intraday_candle_time_is_shifted_into_ist(self):
        c = self.r["candleIntraday"]
        self.assertEqual(c["time"], 1_762_913_100 + 19_800)
        self.assertEqual((c["open"], c["high"], c["low"], c["close"]), (2400.0, 2420.5, 2399.95, 2418.4))

    def test_daily_candle_uses_the_date_string(self):
        self.assertEqual(self.r["candleDaily"]["time"], "2025-11-12")

    def test_volume_follows_the_same_time_rule(self):
        self.assertEqual(self.r["volumeIntraday"], {"time": 1_762_913_100 + 19_800, "value": 1200})
        self.assertEqual(self.r["volumeDaily"], {"time": "2025-11-12", "value": 1200})

    def test_buy_is_an_up_arrow_below_the_bar_and_sell_a_down_arrow_above(self):
        buy, sell = self.r["markerBuy"], self.r["markerSell"]
        self.assertEqual((buy["position"], buy["shape"], buy["color"], buy["text"]),
                         ("belowBar", "arrowUp", "var-up", "B 50"))
        self.assertEqual((sell["position"], sell["shape"], sell["color"], sell["text"]),
                         ("aboveBar", "arrowDown", "var-down", "S 50"))
        self.assertEqual(buy["time"], 1_762_913_100 + 19_800)
        self.assertEqual(self.r["markerDaily"]["time"], "2025-11-12")
        self.assertNotEqual(buy["id"], sell["id"])

    def test_a_settlement_is_a_warn_coloured_square_in_the_bar(self):
        m = self.r["markerSettlement"]
        self.assertEqual((m["shape"], m["position"], m["color"], m["text"]), ("square", "inBar", "var-warn", "X 50"))

    def test_marker_text_is_the_side_and_quantity_never_the_note(self):
        self.assertEqual(self.r["markerWithNote"]["text"], "B 50")

    def test_card_for_a_note_shows_it_verbatim_with_costs_and_profit(self):
        c = self.r["cardNote"]
        self.assertEqual(c["reason"], "Opening range broke up")
        labels = {x["label"]: x["value"] for x in c["lines"]}
        self.assertEqual(labels["Price"], "₹2,418.40")
        self.assertEqual(labels["Quantity"], "50")
        self.assertEqual(labels["Costs"], "₹21.45")
        self.assertEqual(c["pnl"]["tone"], "up")
        self.assertTrue(c["pnl"]["value"].startswith("+"), c["pnl"])
        self.assertIn("₹1,250.00", c["pnl"]["value"])

    def test_card_with_no_note_says_so_and_only_the_tag_line_is_code(self):
        c = self.r["cardNoNote"]
        self.assertEqual(c["reason"], "Sold 50 (no note from the strategy)")
        self.assertNotIn("reasonIsCode", c)
        self.assertIn({"label": "Tag", "value": "orb_exit", "code": True}, c["lines"])
        self.assertEqual([x["label"] for x in c["lines"] if x["code"]], ["Tag"])
        self.assertFalse(any(x["code"] for x in self.r["cardNote"]["lines"]))

    def test_an_opening_fill_has_no_profit_line(self):
        self.assertIsNone(self.r["cardOpening"]["pnl"])

    def test_a_loss_is_tone_down_and_says_loss_in_words(self):
        p = self.r["cardLoss"]["pnl"]
        self.assertEqual(p["tone"], "down")
        self.assertIn("oss", p["label"])
        self.assertIn("₹530.50", p["value"])

    def test_a_zero_result_is_flat(self):
        self.assertEqual(self.r["cardFlat"]["pnl"]["tone"], "flat")

    def test_card_text_is_never_html(self):
        card, marker = self.r["cardHtml"], self.r["markerHtml"]
        self.assertEqual(card["reason"], NOTE)
        for s in list(_strings({k: v for k, v in card.items() if k != "reason"})) + list(_strings(marker)):
            self.assertNotRegex(s, r"[<>]", s)
        self.assertEqual(marker["text"], "B 50")

    def test_merge_pages_keeps_bars_unique_and_sorted(self):
        self.assertEqual(self.r["merged"], [[50, "2418.40"], [100, "2418.40"], [200, "2418.40"],
                                            [300, "2418.40"], [400, "2418.40"]])
        self.assertEqual(self.r["mergedEmpty"], 1)

    def test_range_around_is_symmetric_and_never_negative(self):
        t = 1_762_913_100
        self.assertEqual(self.r["rangeAround"], {"from_ns": (t - 6000) * 10**9, "to_ns": (t + 6000) * 10**9})
        self.assertEqual(self.r["rangeNearZero"]["from_ns"], 0)

    def test_settlements_at_the_same_instant_and_quantity_get_different_ids(self):
        a, b = self.r["settlementIds"]
        self.assertNotEqual(a, b)

    def test_generated_merge_pages_is_unique_sorted_idempotent_and_keeps_held_bars(self):
        self.assertEqual(self.r["propMerge"], [])

    def test_generated_pnl_tone_matches_the_sign(self):
        self.assertEqual(self.r["propTone"], [])

    def test_generated_marker_text_never_holds_the_note(self):
        self.assertEqual(self.r["propMarkerText"], [])

    def test_empty_and_error_states(self):
        v = self.r["viewState"]
        self.assertEqual(v["loading"], "loading")
        self.assertEqual(v["error"], "error")
        self.assertEqual(v["pruned"], "pruned")
        self.assertEqual(v["noPack"], "pruned")
        self.assertEqual(v["empty"], "empty")
        self.assertEqual(v["emptyNoBars"], "empty")
        self.assertEqual(v["full"], "full")
        self.assertEqual(v["fullNoBars"], "full")

    def test_marker_list_is_complete(self):
        rows = self.r["listRows"]
        self.assertEqual([r["id"] for r in rows], self.r["listIds"])
        self.assertEqual(len(set(self.r["listIds"])), 4)
        self.assertEqual(self.r["listEmpty"], [])
        self.assertEqual(rows[0]["title"], "Bought 50 at ₹2,418.40")
        self.assertEqual(rows[0]["reason"], "one")
        self.assertEqual(rows[0]["when"], "2025-11-12 07:35")
        self.assertEqual(self.r["listDaily"], "2025-11-12")

    def test_plot_points_follow_the_candle_time_rule(self):
        self.assertEqual(self.r["plotIntraday"], {"time": 1_762_913_100 + 19800, "value": 2412.5})
        self.assertEqual(self.r["plotDaily"], {"time": "2025-11-12", "value": 54.3217})

    def test_edges_ask_for_the_neighbouring_range(self):
        self.assertEqual(self.r["edgeNone"], [])
        self.assertEqual(self.r["edgeNoBars"], [])
        self.assertEqual(self.r["edgeBefore"], [{"side": "before", "from_ns": (10_000 - 6000) * 10**9, "to_ns": 10_000 * 10**9}])
        self.assertEqual(self.r["edgeAfter"], [{"side": "after", "from_ns": 20_000 * 10**9, "to_ns": 26_000 * 10**9}])
        self.assertEqual(self.r["edgeBoth"], 2)
        self.assertEqual(self.r["edgeZero"]["from_ns"], 0)

    def test_window_shading_follows_the_window_bounds(self):
        self.assertEqual(self.r["shade"], [[100 + 19800, 0], [199 + 19800, 0], [200 + 19800, 1]])

    def test_chart_query_lists_only_what_is_set(self):
        self.assertEqual(self.r["queryAll"], "")
        self.assertEqual(self.r["queryZero"], "?instrument=0")
        self.assertEqual(self.r["queryFull"], "?instrument=3&from_ns=5&to_ns=9&trade_date=2026-11-12")

    def test_settlement_card_names_the_price_by_contract_kind(self):
        def line(card, label):
            return [x["value"] for x in card["lines"] if x["label"] == label]
        opt, fut, unk = self.r["cardSettleOption"], self.r["cardSettleFuture"], self.r["cardSettleUnknown"]
        self.assertEqual(line(opt, "Value at expiry"), ["₹112.00"])
        self.assertEqual(line(opt, "Settlement price"), ["₹24,410.00"])
        self.assertEqual(line(opt, "Price"), [])
        self.assertEqual(line(fut, "Settlement price"), ["₹112.00"])
        self.assertEqual(line(fut, "Value at expiry"), [])
        self.assertEqual(line(unk, "Settlement price"), ["₹112.00"])
        self.assertEqual(line(self.r["cardFillWithFsp"], "Price"), ["₹2,418.40"])
        self.assertEqual(self.r["kinds"], ["option", "option", "future", "future"])


if __name__ == "__main__":
    unittest.main()

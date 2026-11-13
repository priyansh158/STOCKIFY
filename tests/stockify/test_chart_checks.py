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


if __name__ == "__main__":
    unittest.main()

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
        self.assertEqual(self.r["kinds"], ["option", "future", None, None, None])

    def test_marker_row_join_keeps_one_row_per_fill_with_the_note_when_there_is_one(self):
        rows = self.r["join"]
        self.assertEqual([r["fill"]["fill_id"] for r in rows], [1, 2, 3, 4])
        self.assertEqual([r["markerId"] for r in rows], [None, "f2", "f3", None])
        self.assertIsNone(rows[0]["why"])
        self.assertEqual(rows[1]["why"], {"text": "Range broke up", "tag": None})
        # no note: the same sentence as the chart card, with the tag beside it
        self.assertEqual(rows[2]["why"], {"text": "Bought 50 (no note from the strategy)", "tag": "orb_exit"})
        self.assertEqual(self.r["joinNone"][0]["markerId"], None)
        # any tag on a note-less fill is a code label, whether or not the API calls it a system tag
        strat, bare = self.r["joinStrategyTag"]
        self.assertEqual(strat["why"], {"text": "Bought 50 (no note from the strategy)", "tag": "sma_exit"})
        self.assertEqual(bare["why"], {"text": "Bought 50 (no note from the strategy)", "tag": None})
        tag = lambda lines: [x for x in lines if x["label"] == "Tag"]
        self.assertEqual(tag(self.r["cardStrategyTag"]), [{"label": "Tag", "value": "sma_exit", "code": True}])
        self.assertEqual(tag(self.r["cardNoteTag"]), [{"label": "Tag", "value": "orb_entry", "code": False}])
        self.assertEqual(self.r["joinEmpty"], [])
        self.assertEqual(self.r["joinOrder"], [9, 3])

    def test_trial_param_resolution(self):
        r = self.r
        self.assertEqual((r["trialMissing"]["kind"], r["trialMissing"]["row"]["run_id"]), ("best", "r1"))
        self.assertEqual(r["trialEmpty"]["kind"], "best")
        self.assertEqual((r["trialValid"]["kind"], r["trialValid"]["row"]["run_id"]), ("picked", "r0"))
        # An unknown id, or a trial that did not finish, is the empty state, never an error and never the best run.
        self.assertEqual((r["trialUnknown"]["kind"], r["trialUnknown"]["row"]), ("unknown", None))
        self.assertEqual((r["trialNotDone"]["kind"], r["trialNotDone"]["row"]), ("unknown", None))
        self.assertEqual((r["trialNoBest"]["kind"], r["trialNoBest"]["row"]), ("none", None))
        self.assertEqual(r["trialBestNotInRows"]["kind"], "none")

    def test_trial_link_keeps_the_picked_axes_and_lands_on_the_chart(self):
        self.assertEqual(self.r["trialHref"], "/research/sweeps/sw%201?x=fast&y=slow&trial=run%2F1#trial-chart")
        self.assertEqual(self.r["trialHrefOneAxis"], "/research/sweeps/sw1?x=fast&trial=r1#trial-chart")

    def test_trial_title_names_each_setting_in_words(self):
        self.assertEqual(self.r["trialWords"], "Fast average 5, Slow average 20, Stop none, Filter true")

    def test_card_lists_each_closed_chunk_with_its_profit_or_loss(self):
        rows = self.r["cardChunks"]["chunks"]
        self.assertEqual([c["label"] for c in rows], ["Closed 20, opened at ₹2,400.00",
                                                      "Closed 30, opened at ₹2,410.00",
                                                      "Closed 5, opened at ₹2,418.40"])
        self.assertEqual([c["value"] for c in rows], ["+₹15.00", "−₹5.00".replace("−", "-"), "₹0.00"])
        self.assertEqual([c["tone"] for c in rows], ["up", "down", "flat"])
        self.assertEqual(self.r["cardNoChunks"], [])

    def test_canvas_summary_names_symbol_interval_bars_and_trades(self):
        a, b, c, d = self.r["summary"]
        self.assertEqual(a, "Price chart of RELIANCE, 1 one-minute bar, 1 trade; the trades are listed below.")
        self.assertEqual(b, "Price chart of RELIANCE, 2 one-minute bars, no trades.")
        self.assertEqual(c, "Price chart, 1 daily bar, 2 trades; the trades are listed below.")
        self.assertEqual(d, "Price chart of RELIANCE with no price data; the trades are listed below.")

    def test_a_pruned_chart_still_gets_a_time_axis_holding_every_marker(self):
        self.assertEqual(self.r["axis"], [6000 + 19800, 6060 + 19800, 6120 + 19800, 6180 + 19800])
        self.assertEqual(self.r["axisDaily"], ["2025-11-12"])
        self.assertEqual(self.r["axisEmpty"], [])
        self.assertEqual(self.r["axisHuge"], 2)

    def test_hover_tip_is_the_cards_title_and_reason_as_text(self):
        self.assertEqual(self.r["tip"], {"title": "Bought 50 at ₹2,418.40", "reason": "Range broke up", "more": 0})
        self.assertEqual(self.r["tipMore"]["more"], 2)
        self.assertEqual(self.r["tipMore"]["reason"], "one")
        self.assertIsNone(self.r["tipNone"])

    def test_poll_interval_only_while_live_and_visible(self):
        r = self.r
        self.assertEqual(r["pollLive"], 5000)
        self.assertIsNone(r["pollHidden"])
        self.assertIsNone(r["pollClosed"])
        self.assertIsNone(r["pollClosedHidden"])
        self.assertIsNone(r["pollNoMs"])

    def test_a_failing_poll_backs_off_to_thirty_seconds_and_a_success_resets_it(self):
        self.assertEqual(self.r["backoff"], [5000, 10000, 20000, 30000, 30000, 30000])
        # no interval means no polling; a negative count counts as none; the cap never goes below the base
        self.assertEqual(self.r["backoffOdd"], [0, 5000, 40000, 30000])

    def test_a_day_with_no_recording_or_an_unreadable_one_is_a_plain_state_not_no_trades(self):
        r = self.r
        self.assertEqual(r["viewNoPack"], "pruned")
        self.assertEqual(r["viewUnreadable"], "pruned")
        self.assertEqual(r["viewOkEmpty"], "empty")
        self.assertEqual(r["wordsNoPackPaper"], "No price data was recorded for this day. The trades are listed below.")
        self.assertEqual(r["wordsNoPackRun"], "There is no price recording for this day. The trades are listed below.")
        self.assertEqual(r["wordsUnreadable"], "The price recording for this day cannot be read right now. Reload to try again; the trades are listed below.")
        self.assertEqual(r["wordsPruned"], "The price data for this run is no longer on disk. The trades are listed below.")


if __name__ == "__main__":
    unittest.main()

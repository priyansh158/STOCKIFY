"""stockify.display: one rule for turning identifiers into plain words, and the catalogue's display names."""
from __future__ import annotations

import unittest

import tests.support  # noqa: F401  — must precede project imports

from stockify import display
from stockify.strategies import _catalogue


class HumanizeTests(unittest.TestCase):
    def test_snake_kebab_dotted_and_camel_case(self):
        for raw, want in [("opening_range_breakout", "Opening Range Breakout"), ("short-straddle-stop", "Short Straddle Stop"),
                          ("my.script_v2", "My Script V2"), ("SmaCross", "SMA Cross"), ("buyAndHold", "Buy and Hold"),
                          ("HTTPServer", "Http Server")]:
            self.assertEqual(display.humanize(raw), want, raw)

    def test_acronyms_stay_upright(self):
        self.assertEqual(display.humanize("orb_cpp"), "ORB C++")
        self.assertEqual(display.humanize("eod_short_straddle"), "EOD Short Straddle")
        self.assertEqual(display.humanize("NIFTY weekly"), "NIFTY Weekly")
        self.assertEqual(display.humanize("sma_cross_cpp"), "SMA Cross C++")

    def test_never_empty_never_raises(self):
        for raw in (None, "", "   ", 5, [], "___", "..."):
            out = display.humanize(raw)
            self.assertTrue(isinstance(out, str) and out.strip(), repr(raw))

    def test_idempotent_on_its_own_output(self):
        for raw in ("opening_range_breakout", "orb_cpp", "SmaCross", "a_b_c"):
            once = display.humanize(raw)
            self.assertEqual(display.humanize(once), once)

    def test_sentence_case(self):
        self.assertEqual(display.sentence("range_high"), "Range high")
        self.assertEqual(display.sentence("sma_fast"), "SMA fast")
        self.assertEqual(display.sentence("fast"), "Fast")

    def test_tags_in_plain_words_and_unknown_ones_humanized(self):
        self.assertEqual([display.tag_label(t) for t in ("orb_long", "orb_short", "entry", "stop", "orb_exit", "time_exit")],
                         ["Long entry", "Short entry", "Entry", "Stop", "Exit", "Exit at the set time"])
        self.assertEqual(display.tag_label("AUTO_SQOFF"), "Automatic square-off")
        self.assertEqual(display.tag_label("my_custom_tag"), "My Custom Tag")
        self.assertEqual(display.tag_label(""), "")
        self.assertEqual(display.tag_label(None), "")


class SettingsAndFamilyTests(unittest.TestCase):
    def test_config_keys_have_plain_labels(self):
        for field in ("fill_model", "margin.model", "price_basis", "costs.broker", "policies.max_slices", "seed"):
            label = display.field_label(field)
            self.assertTrue(label and "_" not in label and "." not in label, field)
        self.assertEqual(display.field_label("fill_model.kind"), "How orders fill")
        self.assertEqual(display.field_label("strategy.params.fast", {"fast": "Fast average (bars)"}), "Fast average (bars)")
        self.assertEqual(display.field_label("strategy.params.exit_hhmm"), "Exit hhmm")   # no schema: still words
        self.assertEqual(display.field_label("something.unmapped_key"), "Unmapped key")

    def test_values_in_words(self):
        self.assertEqual(display.setting_text("fill_model", '{"kind": "bar"}'), "From bars")
        self.assertEqual(display.setting_text("price_basis", "raw"), "Actual (as traded)")
        self.assertEqual(display.setting_text("costs.broker", "zerodha"), "Zerodha")
        self.assertEqual(display.setting_text("strategy.params.symbol", "NSE:RELIANCE"), "NSE:RELIANCE")
        self.assertEqual(display.setting_text("strategy.params.fast", "20"), "20")
        self.assertEqual(display.setting_text("strategy.params.short", "true"), "Yes")
        self.assertIsNone(display.setting_text("seed", None))
        self.assertEqual(display.setting_text("fill_model", '{"kind": "bar", "half_spread_ticks": 1, "slippage": {"kind": "none"}}'),
                         "From bars; Half the spread (ticks): 1; Slippage: None")

    def test_family_titles(self):
        self.assertEqual(display.family_title("sma_cross/bars-day-RELIANCE/sharpe"),
                         "SMA Cross on RELIANCE, daily bars, judged by steadiness of returns (Sharpe)")
        self.assertEqual(display.family_title("orb/bars-minute-RELIANCE_plus2/net_pnl"),
                         "ORB on RELIANCE and 2 more, minute bars, judged by profit after costs")
        self.assertEqual(display.family_title("x/y/z", "My idea"), "My idea")
        self.assertEqual(display.family_title("sweep/abc123", None, "orb sweep"), "Parameter sweep: orb sweep")
        self.assertEqual(display.family_title("sweep/abc123"), "Parameter sweep")
        self.assertNotIn("/", display.family_title("odd_one"))


class CatalogueNameTests(unittest.TestCase):
    def test_every_catalogue_strategy_declares_a_display_name(self):
        entries = _catalogue.python_strategies()
        self.assertGreaterEqual(len(entries), 6)
        for e in entries:
            cls = _catalogue._class_of(e["kind"], e["ref"])
            self.assertTrue(getattr(cls, "DISPLAY_NAME", ""), f"{e['ref']} has no DISPLAY_NAME: add one to the class")
            self.assertEqual(e["display_name"], cls.DISPLAY_NAME)
            self.assertNotIn("_", e["display_name"])

    def test_cpp_entries_have_names_and_params_have_labels(self):
        known = {"sma_cross_cpp": "SMA Crossover (C++)", "orb_cpp": "Opening Range Breakout (C++)"}
        for name, want in known.items():
            self.assertEqual(_catalogue.CPP_DISPLAY_NAMES[name], want)
            self.assertEqual(_catalogue.display_name_of("cpp", name), want)
        for e in _catalogue.python_strategies():
            for p in e["params"]:
                self.assertTrue(p.get("label", "").strip() and "_" not in p["label"], f"{e['ref']}.{p['name']}")

    def test_every_description_is_a_plain_sentence(self):
        for e in _catalogue.catalogue():
            doc = e["doc"]
            self.assertTrue(doc.endswith("."), e["ref"])
            for bad in ("`", "_", "(M", "bhavcopy", "C++ port"):
                self.assertNotIn(bad, doc, e["ref"])

    def test_the_agreed_names(self):
        by_ref = {e["ref"].split(":")[1]: e["display_name"] for e in _catalogue.python_strategies()}
        self.assertEqual(by_ref, {
            "BuyAndHold": "Buy and Hold", "EodShortStraddle": "End-of-Day Short Straddle", "IronFly": "Iron Fly",
            "OpeningRangeBreakout": "Opening Range Breakout", "ShortStraddleStop": "Short Straddle with Stop",
            "SmaCross": "SMA Crossover"})

    def test_a_ref_without_a_declared_name_is_humanized(self):
        self.assertEqual(_catalogue.display_name_of("python", "my_scripts.mean_reversion:MeanReversion"), "Mean Reversion")
        self.assertEqual(_catalogue.display_name_of("python", "stockify.strategies._margin_watch:MarginWatch"), "Margin Watch")
        self.assertEqual(_catalogue.display_name_of("cpp", "noop_cpp"), "Noop C++")
        self.assertEqual(_catalogue.display_name_of("python", ""), "Unnamed")
        self.assertEqual(_catalogue.display_name_of("python", None), "Unnamed")


if __name__ == "__main__":
    unittest.main()

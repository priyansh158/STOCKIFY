"""M3.14: the strategy catalogue and its params checks."""
from __future__ import annotations

import unittest

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine
from stockify.strategies import _catalogue as C

REFS = {"stockify.strategies.buy_and_hold:BuyAndHold", "stockify.strategies.sma_cross:SmaCross",
        "stockify.strategies.opening_range_breakout:OpeningRangeBreakout",
        "stockify.strategies.eod_short_straddle:EodShortStraddle",
        "stockify.strategies.short_straddle_stop:ShortStraddleStop", "stockify.strategies.iron_fly:IronFly",
        "stockify.strategies.rsi_pullback:RsiPullback", "stockify.strategies.macd_cross:MacdCross",
        "stockify.strategies.bollinger_reversion:BollingerReversion",
        "stockify.strategies.donchian_breakout:DonchianBreakout", "stockify.strategies.supertrend:Supertrend",
        "stockify.strategies.ema_trend:EmaTrend", "stockify.strategies.chandelier_exit:ChandelierExit"}


class CatalogueTests(unittest.TestCase):
    def test_python_strategies_are_the_reference_ones(self):
        got = C.python_strategies()
        self.assertEqual({s["ref"] for s in got}, REFS)
        for s in got:
            self.assertEqual(s["kind"], "python")
            self.assertTrue(s["doc"], s["ref"])
            self.assertTrue(s["params"], s["ref"])

    def test_schemas_are_well_formed(self):
        for s in C.python_strategies():
            names = [p["name"] for p in s["params"]]
            self.assertEqual(len(names), len(set(names)), s["ref"])
            for p in s["params"]:
                self.assertEqual(set(p) - {"min", "max", "unit", "choices", "percent"}, {"name", "label", "type", "default", "required", "help"},
                                 (s["ref"], p))
                self.assertIn(p["type"], C.PARAM_TYPES)
                for bound in ("min", "max"):           # REG 5.3: a bound is a decimal string and says its unit
                    if bound not in p:
                        continue
                    self.assertIn(p["type"], ("int", "decimal"), (s["ref"], p))
                    self.assertIsInstance(p[bound], str)
                    self.assertTrue(p.get("unit"), (s["ref"], p))
                if p["required"]:
                    self.assertIsNone(p["default"])
                else:
                    self.assertEqual(C.validate_params(s["params"], {**self._required(s), p["name"]: p["default"]}),
                                     [], (s["ref"], p["name"]))

    @staticmethod
    def _required(s: dict) -> dict:
        sample = {"symbol": "NSE:TEST", "int": 3}
        return {p["name"]: sample.get(p["type"], 3) for p in s["params"] if p["required"]}

    def test_schemas_match_what_the_strategies_read(self):
        """Every ctx.params key a strategy reads is in its schema, and nothing else."""
        import inspect
        import re
        for s in C.python_strategies():
            module = __import__(s["ref"].split(":")[0], fromlist=["x"])
            src = inspect.getsource(module)
            if any(b.__name__ == "SwingLong" for b in __import__(s["ref"].split(":")[0], fromlist=["x"]).__dict__[
                    s["ref"].split(":")[1]].__mro__):
                src += inspect.getsource(__import__("stockify.strategies._swing", fromlist=["x"]).SwingLong)   # symbol, interval_s
            read = set(re.findall(r"""(?:params|\bp)(?:\.get\(|\[)["'](\w+)["']""", src))
            self.assertEqual(read, {p["name"] for p in s["params"]}, s["ref"])

    @unittest.skipUnless(engine.available(), "stockify._engine is not built")
    def test_cpp_strategies(self):
        got = {s["ref"]: s for s in C.cpp_strategies()}
        self.assertEqual(set(got), {"sma_cross_cpp", "orb_cpp"})
        from stockify.strategies.sma_cross import SmaCross
        self.assertEqual(got["sma_cross_cpp"]["params"], SmaCross.PARAMS)
        self.assertEqual(C.schema_for("cpp", "orb_cpp")[0]["name"], "symbol")
        self.assertIsNone(C.schema_for("cpp", "nope"))

    @unittest.skipUnless(engine.available(), "stockify._engine is not built")
    def test_a_registered_strategy_without_a_schema_is_left_out(self):
        from unittest import mock
        real = engine.load()
        fake = mock.Mock(wraps=real)
        fake.strategies.return_value = ["noop_cpp", "orb_cpp", "sma_cross_cpp"]
        with mock.patch.object(engine, "load", return_value=fake):
            self.assertEqual([s["ref"] for s in C.cpp_strategies()], ["orb_cpp", "sma_cross_cpp"])
            self.assertIsNone(C.schema_for("cpp", "noop_cpp"))

    def test_validate_params(self):
        from stockify.strategies.sma_cross import SmaCross
        from stockify.strategies.opening_range_breakout import OpeningRangeBreakout
        v = C.validate_params
        self.assertEqual(v(SmaCross.PARAMS, {"symbol": "NSE:RELIANCE", "fast": 20, "slow": "50"}), [])
        self.assertEqual(v(SmaCross.PARAMS, {"symbol": "NSE:X", "fast": 2.0, "slow": 3, "long_short": True,
                                             "product": "NRML", "lots": "2"}), [])
        self.assertEqual(v(SmaCross.PARAMS, {"fast": 20}), ["symbol: required", "slow: required"])
        self.assertEqual(v(SmaCross.PARAMS, {"symbol": "NSE:X", "fast": "twenty", "slow": 2.5, "colour": 1}),
                         ["colour: unknown parameter", "fast: is not a whole number", "slow: is not a whole number"])
        self.assertEqual(v(SmaCross.PARAMS, {"symbol": "", "fast": 1, "slow": 2, "long_short": "yes"}),
                         ["symbol: is not a symbol like NSE:RELIANCE", "long_short: is not true or false"])
        orb = {"symbol": "NSE:X", "qty": 10}
        for ok in ("15:10", "9:40", "15:07:30"):
            self.assertEqual(v(OpeningRangeBreakout.PARAMS, {**orb, "exit_hhmm": ok}), [], ok)
        for bad in ("25:00", "15", "3pm", 1510):
            self.assertEqual(v(OpeningRangeBreakout.PARAMS, {**orb, "exit_hhmm": bad}),
                             ["exit_hhmm: is not a time like 15:10"], bad)
        from stockify.strategies.eod_short_straddle import EodShortStraddle
        self.assertEqual(v(EodShortStraddle.PARAMS, {"stop_multiple": "abc"}), ["stop_multiple: is not a decimal number"])
        self.assertEqual(v(EodShortStraddle.PARAMS, {"stop_multiple": 2}), [])
        self.assertEqual(v(SmaCross.PARAMS, []), ["params must be an object"])

    def test_protection_bp_is_refused_outside_1_to_10000_when_the_config_is_checked(self):
        """C review I1: protection_bp is the same 1..10000 bp ctx.place takes, refused at config time, not mid-run."""
        from stockify.strategies.eod_short_straddle import EodShortStraddle
        from stockify.strategies.iron_fly import IronFly
        from stockify.strategies.short_straddle_stop import ShortStraddleStop
        unit = "basis points (5000 = 50 %)"
        for params in (IronFly.PARAMS, ShortStraddleStop.PARAMS, EodShortStraddle.PARAMS):
            for ok in (1, 5000, 10000):
                self.assertEqual(C.validate_params(params, {"protection_bp": ok}), [], ok)
            self.assertEqual(C.validate_params(params, {"protection_bp": 0}),
                             [f"protection_bp: 0 is below 1: it is {unit}"])
            self.assertEqual(C.validate_params(params, {"protection_bp": 20000}),
                             [f"protection_bp: 20000 is above 10000: it is {unit}"])

    def test_a_bound_is_checked_and_its_error_states_the_unit(self):
        """REG 5.3 (dimensional F3): stop_pct is a fraction; "30" (meant as 30 %) is refused."""
        from stockify.strategies.iron_fly import IronFly
        from stockify.strategies.short_straddle_stop import ShortStraddleStop
        for params in (IronFly.PARAMS, ShortStraddleStop.PARAMS):
            entry = {p["name"]: p for p in params}["stop_pct"]
            self.assertEqual((entry["max"], entry["unit"]), ("1", 'a fraction ("0.3" = 30 %)'))
            for ok in ("0.3", "1", "1.00", 1, 0.5, "0"):
                self.assertEqual(C.validate_params(params, {"stop_pct": ok}), [], ok)
            for bad in ("30", "1.0001", 2, 1.5, "1e2"):
                self.assertEqual(C.validate_params(params, {"stop_pct": bad}),
                                 [f'stop_pct: {bad!r} is above 1: it is a fraction ("0.3" = 30 %)'], bad)
            self.assertEqual(C.validate_params(params, {"stop_pct": "abc"}), ["stop_pct: is not a decimal number"])
        whole = [{"name": "n", "label": "", "type": "int", "default": 1, "required": False, "help": "", "max": "10",
                  "unit": "a count"}]
        self.assertEqual(C.validate_params(whole, {"n": "10"}), [])
        self.assertEqual(C.validate_params(whole, {"n": True}), [])
        self.assertEqual(C.validate_params(whole, {"n": 11.0}), ["n: 11.0 is above 10: it is a count"])

    def test_help_texts_are_sentences(self):
        """RW 7.2: a help line starts with a capital letter and ends with a full stop, whatever the strategy."""
        for s in C.python_strategies():
            for p in s["params"]:
                h = p["help"]
                self.assertTrue(h[:1].isupper() or h[:1].isdigit(), (s["ref"], p["name"], h))
                self.assertTrue(h.endswith("."), (s["ref"], p["name"], h))

    def test_bar_length_is_a_choice_list_of_the_supported_intervals(self):
        """RW 7.2: `interval_s` offers plain labels; the stored value stays seconds and only the data layer's
        intervals are offered."""
        from stockify.data.packs import INTERVAL_S
        seen = 0
        for s in C.python_strategies():
            for p in s["params"]:
                if p["name"] != "interval_s":
                    continue
                seen += 1
                self.assertEqual(p["label"], "Bar length", s["ref"])
                values = [c["value"] for c in p["choices"]]
                self.assertEqual(values, sorted(INTERVAL_S.values()))
                self.assertIn(p["default"], values)
                labels = {c["value"]: c["label"] for c in p["choices"]}
                self.assertEqual((labels[60], labels[300], labels[3600], labels[86400]),
                                 ("1 minute", "5 minutes", "1 hour", "1 day"))
                self.assertEqual(C.validate_params(s["params"] if False else [p], {"interval_s": 300}), [])
                self.assertEqual(C.validate_params([p], {"interval_s": "900"}), [])
                self.assertEqual(C.validate_params([p], {"interval_s": 7}),
                                 ["interval_s: 7 is not one of 60, 180, 300, 600, 900, 1800, 3600, 86400"])
        self.assertGreaterEqual(seen, 2)

    def test_invest_is_shown_as_a_percentage_but_stored_as_a_fraction(self):
        from stockify.strategies.buy_and_hold import BuyAndHold
        p = {q["name"]: q for q in BuyAndHold.PARAMS}["invest"]
        self.assertTrue(p["percent"])
        self.assertEqual(p["default"], "0.95")

    def test_schema_of_finds_a_catalogue_strategy_without_listing_the_catalogue(self):
        """REG 5.3: the lookup every run config passes through; it never loads the engine."""
        from unittest import mock
        with mock.patch.object(engine, "available", side_effect=AssertionError("the engine was asked for")), \
                mock.patch.object(C, "catalogue", side_effect=AssertionError("the catalogue was listed")):
            for ref in sorted(REFS):
                module, cls = ref.split(":")
                self.assertEqual(C.schema_of("python", ref), getattr(__import__(module, fromlist=["x"]), cls).PARAMS)
            from stockify.strategies.opening_range_breakout import OpeningRangeBreakout
            self.assertEqual(C.schema_of("cpp", "orb_cpp"), OpeningRangeBreakout.PARAMS)
            for kind, ref in (("cpp", "noop_cpp"), ("cpp", ""), ("python", "stockify.strategies._script:Script"),
                              ("python", "stockify.strategies._margin_watch:MarginWatch"),
                              ("python", "stockify.strategies.nope:Nope"),
                              ("python", "stockify.strategies.sma_cross:Nope"),
                              ("python", "stockify.strategies.sma_cross:Strategy"),
                              ("python", "stockify.strategies.sma_cross.x:SmaCross"),
                              ("python", "tests.stockify.run_fixtures:FailOnSecondBar"), ("python", "os:path"),
                              ("python", "nonsense"), ("script", "x"), ("python", None)):
                self.assertIsNone(C.schema_of(kind, ref), (kind, ref))
        for s in C.python_strategies():
            self.assertEqual(C.schema_of("python", s["ref"]), s["params"])


if __name__ == "__main__":
    unittest.main()

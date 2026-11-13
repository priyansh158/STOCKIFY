"""M3.14: the strategy catalogue and its params checks."""
from __future__ import annotations

import unittest

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine
from stockify.strategies import _catalogue as C

REFS = {"stockify.strategies.buy_and_hold:BuyAndHold", "stockify.strategies.sma_cross:SmaCross",
        "stockify.strategies.opening_range_breakout:OpeningRangeBreakout",
        "stockify.strategies.eod_short_straddle:EodShortStraddle"}


class CatalogueTests(unittest.TestCase):
    def test_python_strategies_are_the_reference_four(self):
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
                self.assertEqual(set(p), {"name", "label", "type", "default", "required", "help"}, (s["ref"], p))
                self.assertIn(p["type"], C.PARAM_TYPES)
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


if __name__ == "__main__":
    unittest.main()

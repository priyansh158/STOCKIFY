"""A run config that is written wrong is refused, not quietly reinterpreted (REG 5.1 onward)."""
from __future__ import annotations

import copy
import unittest

import tests.support  # noqa: F401  — must precede project imports

from stockify import runconfig
from tests.stockify.test_bundle import run

SPEC = {"dataset": "bars", "interval": "day", "instruments": ["NSE:RELIANCE"], "from": "2025-12-22",
        "to": "2025-12-31"}
CHAIN = {"underlying": "NIFTY", "expiry": "nearest", "strikes": 10}


def _packs(spec: dict | None = None, **item) -> dict:
    return {"packs": [{"spec": {**SPEC, **(spec or {})}, **item}]}


class UnknownKeyTests(unittest.TestCase):
    """REG 5.1 (audit H-1): a key the run config does not know is an error that names the key, its section
    and the closest known key; before, `{"margin": {"modle": "flat"}}` ran under the default model."""

    # (the change to a valid config, the section named, the unknown key, the suggestion or None)
    CASES = [
        ({"latency": 250}, "run", "latency", "latency_ms"),
        ({"capital": 1}, "run", "capital", "capital_paise"),
        ({"margin_model": "flat"}, "run", "margin_model", "margin"),
        ({"notes": "x"}, "run", "notes", None),
        ({"margin": {"modle": "flat"}}, "margin", "modle", "model"),
        ({"costs": {"allow_unverified": True, "dp_charge": False}}, "costs", "dp_charge", "dp_charges"),
        ({"policies": {"max_slice": 3}}, "policies", "max_slice", "max_slices"),
        ({"fill_model": {"kind": "bar", "slipage": {"kind": "none"}}}, "fill_model", "slipage", "slippage"),
        ({"fill_model": {"kind": "bar", "slippage": {"kind": "fixed_ticks", "k": 1, "ticks": 2}}},
         "fill_model.slippage", "ticks", None),
        ({"fill_model": {"kind": "bar", "slippage": {"kind": "none", "k": 1}}}, "fill_model.slippage", "k", None),
        ({"fill_model": {"kind": "bar", "slippage": {"kind": "bps", "x": "0.0005", "k": 1}}},
         "fill_model.slippage", "k", None),
        ({"strategy": {"kind": "python", "ref": "stockify.runconfig:RunConfigError", "parms": {}}}, "strategy",
         "parms", "params"),
        ({"strategy": {"kind": "script", "actions": [], "ref": "a:B"}}, "strategy", "ref", None),
        ({"strategy": {"kind": "cpp", "ref": "sma_cross_cpp", "actions": []}}, "strategy", "actions", None),
        ({"data": {**_packs(), "pack": []}}, "data", "pack", "packs"),
        ({"data": _packs(note="x")}, "data.packs[0]", "note", None),
        ({"data": _packs({"intervl": "minute"})}, "data.packs[0].spec", "intervl", "interval"),
        ({"data": _packs({"skip_missing_day": True})}, "data.packs[0].spec", "skip_missing_day",
         "skip_missing_days"),
        ({"data": _packs({"instruments": [{"symbol": ["NSE:RELIANCE"]}]})}, "data.packs[0].spec.instruments[0]",
         "symbol", "symbols"),
        ({"data": _packs({"instruments": ["NSE:RELIANCE", {"chain": {**CHAIN, "strike": 5}}]})},
         "data.packs[0].spec.instruments[1].chain", "strike", "strikes"),
    ]

    def test_an_unknown_key_is_refused_with_its_section_and_the_closest_known_key(self):
        for change, section, key, close in self.CASES:
            with self.subTest(section=section, key=key):
                with self.assertRaises(runconfig.RunConfigError) as caught:
                    runconfig.load(run(**change))
                text = str(caught.exception)
                self.assertTrue(text.startswith(f"{section}: unknown key {key!r}"), text)
                if close is None:
                    self.assertNotIn("did you mean", text)
                    self.assertIn("known keys:", text)
                else:
                    self.assertIn(f"did you mean {close!r}?", text)

    def test_the_audit_example_no_longer_runs_under_the_default_model(self):
        with self.assertRaisesRegex(runconfig.RunConfigError, r"^margin: unknown key 'modle' \(did you mean 'model'\?"):
            runconfig.load(run(margin={"modle": "flat"}))

    def test_every_known_key_is_still_accepted(self):
        full = run(family_id="a/b", latency_ms=5, mark="mid", seed=7, seed_mode="per_day", unlock_holdout=None,
                   costs={"broker": "zerodha", "allow_unverified": True, "auto_square_off_fee": False,
                          "dp_charges": False},
                   margin={"model": "flat"},
                   policies={"preopen": "reject", "cas": "close_price", "freeze": "reject", "max_slices": 2,
                             "rms_square_off": True},
                   fill_model={"kind": "bar", "limit_touch_policy": "touch", "participation": "0.25",
                               "depth_take": "0.5", "slippage": {"kind": "random_ticks", "k": 2},
                               "half_spread_ticks": 0},
                   strategy={"kind": "script", "actions": [], "params": {}},
                   data={"packs": [{"sha256": "a" * 64},
                                   {"spec": {"dataset": "ticks", "from": "2026-11-09", "to": "2026-11-10",
                                             "tiers": ["full"], "clock_cadence_ms": 1000, "skip_missing_days": True,
                                             "include_gaps": False,
                                             "instruments": ["NSE:NIFTY 50", {"ids": [1]}, {"symbols": ["NSE:X"]},
                                                             {"chain": {**CHAIN, "kinds": ["CE"],
                                                                        "futures": True}}]}}]})
        cfg = runconfig.load(full)
        self.assertEqual(cfg["margin"], {"model": "flat"})
        self.assertEqual(cfg["data"], full["data"])

    def test_a_validated_config_validates_again_to_itself(self):
        """runs.launch(run_id=…) re-loads the stored normalised config, which carries the derived keys
        (participation_ppm, depth_take_ppm, slippage.bp): they are accepted when they are what the config says."""
        for change in ({}, {"fill_model": {"kind": "bar", "slippage": {"kind": "bps", "x": "0.0005"}}},
                       {"seed_mode": "per_day"}):
            with self.subTest(change=change):
                once = runconfig.load(run(**change))
                self.assertEqual(runconfig.load(copy.deepcopy(once)), once)

    def test_a_derived_key_that_disagrees_with_its_source_is_refused(self):
        once = runconfig.load(run(fill_model={"kind": "bar", "slippage": {"kind": "bps", "x": "0.0005"}}))
        for path, value in ((("participation_ppm",), 999_999), (("depth_take_ppm",), 1), (("slippage", "bp"), 50)):
            with self.subTest(key=".".join(path)):
                bad = copy.deepcopy(once)
                node = bad["fill_model"]
                for step in path[:-1]:
                    node = node[step]
                node[path[-1]] = value
                with self.assertRaisesRegex(runconfig.RunConfigError, "^fill_model." + r"\.".join(path)):
                    runconfig.load(bad)

    def test_a_section_that_is_not_an_object_is_refused(self):
        for section in ("costs", "margin", "policies"):
            with self.subTest(section=section), self.assertRaisesRegex(runconfig.RunConfigError, f"^{section}: "):
                runconfig.load(run(**{section: "flat"}))


def _bps(x) -> dict:  # noqa: ANN001
    return run(fill_model={"kind": "bar", "slippage": {"kind": "bps", "x": x}})


class BpsSlippageTests(unittest.TestCase):
    """REG 5.2 (audit H-2): `x` of bps slippage is a fraction written as a decimal string, from "0" to "0.1"
    (0 to 1,000 bp). Before, `"x": "5"` (meant as 5 bp) ran as 50,000 bp, and a float was read through str()."""

    UNIT = 'a fraction written as a decimal string, from "0" to "0.1" ("0.0005" = 5 bp)'

    def test_a_fraction_in_range_gives_its_basis_points(self):
        for x, bp in (("0.0005", 5), ("0", 0), ("0.0001", 1), ("0.1", 1000), ("0.10", 1000), ("1e-3", 10)):
            with self.subTest(x=x):
                slip = runconfig.load(_bps(x))["fill_model"]["slippage"]
                self.assertEqual(slip, {"kind": "bps", "x": x, "bp": bp})

    def test_a_value_outside_the_range_is_refused_with_the_unit(self):
        for x in ("5", "0.1001", "0.11", "1", "-0.0005", "-1", "1e30", "-1e30"):
            with self.subTest(x=x), self.assertRaises(runconfig.RunConfigError) as caught:
                runconfig.load(_bps(x))
            text = str(caught.exception)
            self.assertTrue(text.startswith(f"fill_model.slippage.x: {x!r} is outside the range: it must be"), text)
            self.assertIn(self.UNIT, text)

    def test_a_value_that_is_not_a_string_is_refused_with_the_unit(self):
        for x in (0.0005, 5, 0, True, None, ["0.0005"]):
            with self.subTest(x=x), self.assertRaises(runconfig.RunConfigError) as caught:
                runconfig.load(_bps(x))
            text = str(caught.exception)
            self.assertTrue(text.startswith(f"fill_model.slippage.x: {x!r} is not a string: it must be"), text)
            self.assertIn(self.UNIT, text)

    def test_a_missing_x_is_refused_with_the_unit(self):
        with self.assertRaises(runconfig.RunConfigError) as caught:
            runconfig.load(run(fill_model={"kind": "bar", "slippage": {"kind": "bps"}}))
        self.assertIn("fill_model.slippage.x: None is not a string", str(caught.exception))

    def test_a_string_that_is_not_a_whole_number_of_basis_points_is_refused_with_the_unit(self):
        for x in ("0.00005", "five", "", "NaN", "Infinity", "0,0005"):
            with self.subTest(x=x), self.assertRaises(runconfig.RunConfigError) as caught:
                runconfig.load(_bps(x))
            text = str(caught.exception)
            self.assertTrue(text.startswith("fill_model.slippage.x: "), text)
            self.assertIn(self.UNIT, text)

    def test_the_examples_and_the_how_to_state_the_unit(self):
        import json
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        seen = 0
        for path in sorted((root / "examples" / "runs").glob("*.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            for cfg in [r["config"] for r in doc["runs"]] if "runs" in doc else [doc]:
                slip = cfg.get("fill_model", {}).get("slippage", {})
                if slip.get("kind") == "bps":
                    seen += 1
                    self.assertEqual(runconfig.load(cfg)["fill_model"]["slippage"]["bp"], 5, path.name)
        self.assertGreater(seen, 0)
        howto = (root / "docs" / "plan" / "HOWTO_strategy.md").read_text(encoding="utf-8")
        self.assertIn('`"0.0005"` = 5 bp', howto)


if __name__ == "__main__":
    unittest.main()

"""FW 6.2: `fill_model.slippage.extra_ticks`, k extra adverse ticks on top of any slippage model (A4 §4.4)."""
from __future__ import annotations

import copy
import random
import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine, numerics, runconfig
from stockify.oracle.matcher import slipped
from stockify.oracle.rng import Pcg32
from tests.stockify.test_bundle import run

KINDS = ({"kind": "none"}, {"kind": "fixed_ticks", "k": 1}, {"kind": "random_ticks", "k": 3},
         {"kind": "bps", "x": "0.001"})


def _cfg(slip: dict) -> dict:
    return runconfig.load(run(fill_model={"kind": "bar", "slippage": slip}))


class ValidationTests(unittest.TestCase):
    def test_extra_ticks_zero_is_left_out_and_changes_no_hash(self):
        for slip in KINDS:
            with self.subTest(kind=slip["kind"]):
                plain, zero = _cfg(slip), _cfg({**slip, "extra_ticks": 0})
                self.assertNotIn("extra_ticks", zero["fill_model"]["slippage"])
                self.assertEqual(numerics.dumps_canonical(plain), numerics.dumps_canonical(zero))
                self.assertEqual(_cfg({**slip, "extra_ticks": 2})["fill_model"]["slippage"]["extra_ticks"], 2)

    def test_extra_ticks_accepted_for_every_kind_and_refused_below_zero_above_1000_or_non_int(self):
        for slip in KINDS:
            for ok in (0, 1, 1000):
                with self.subTest(kind=slip["kind"], extra=ok):
                    _cfg({**slip, "extra_ticks": ok})
            for bad in (-1, 1001, 1.0, "2", True, None):
                with self.subTest(kind=slip["kind"], bad=bad), self.assertRaisesRegex(
                        runconfig.RunConfigError, "extra_ticks"):
                    _cfg({**slip, "extra_ticks": bad})


class SlippedTests(unittest.TestCase):
    def test_slipped_adds_extra_ticks_for_each_kind(self):
        # buy at 10000, tick 5: bps 10 -> ceil_to(10, 5) = 10; + 2 extra ticks = +20
        self.assertEqual(slipped("BUY", 10000, 5, {"kind": "bps", "bp": 10, "extra_ticks": 2}, None, None), 10020)
        self.assertEqual(slipped("BUY", 10000, 5, {"kind": "fixed_ticks", "k": 1, "extra_ticks": 2}, None, None), 10015)
        self.assertEqual(slipped("BUY", 10000, 5, {"kind": "none", "extra_ticks": 2}, None, None), 10010)
        r = Pcg32(42, 1)
        base = slipped("BUY", 10000, 5, {"kind": "random_ticks", "k": 3}, Pcg32(42, 1), None)
        self.assertEqual(slipped("BUY", 10000, 5, {"kind": "random_ticks", "k": 3, "extra_ticks": 2}, r, None),
                         base + 10)

    def test_sell_side_moves_down(self):
        self.assertEqual(slipped("SELL", 10000, 5, {"kind": "fixed_ticks", "k": 1, "extra_ticks": 2}, None, None), 9985)

    def test_cap_still_limits_the_move(self):
        slip = {"kind": "fixed_ticks", "k": 1, "extra_ticks": 2}
        self.assertEqual(slipped("BUY", 10000, 5, slip, None, 10010), 10010)
        self.assertEqual(slipped("SELL", 10000, 5, slip, None, 9990), 9990)
        self.assertEqual(slipped("BUY", 10000, 5, slip, None, 10100), 10015)

    def test_random_ticks_rng_draw_is_unchanged_by_extra_ticks(self):
        slip = {"kind": "random_ticks", "k": 3}
        a, b = Pcg32(7, 1), Pcg32(7, 1)
        slipped("BUY", 10000, 5, slip, a, None)
        slipped("BUY", 10000, 5, {**slip, "extra_ticks": 4}, b, None)
        self.assertEqual((a.state, a.inc), (b.state, b.inc))
        self.assertEqual(a.next_u32(), b.next_u32())

    def test_property_extra_ticks_is_additive_before_the_cap_and_never_moves_the_stream(self):
        rng = random.Random(1406)
        for _ in range(3000):
            tick = rng.choice([1, 5, 10])
            price = rng.randint(1, 500_000) * tick
            side = rng.choice(["BUY", "SELL"])
            slip = rng.choice([{"kind": "none"}, {"kind": "fixed_ticks", "k": rng.randint(0, 6)},
                               {"kind": "random_ticks", "k": rng.randint(0, 6)},
                               {"kind": "bps", "bp": rng.randint(0, 500)}])
            extra = rng.randint(0, 1000)
            cap = rng.choice([None, price + rng.randint(-50, 5000) * tick])
            seed = rng.getrandbits(32)
            a, b = Pcg32(seed, 1), Pcg32(seed, 1)
            without = slipped(side, price, tick, slip, a, None)
            withx = slipped(side, price, tick, {**slip, "extra_ticks": extra}, b, None)
            sign = 1 if side == "BUY" else -1
            self.assertEqual(withx - without, sign * extra * tick)
            self.assertEqual((a.state, a.inc), (b.state, b.inc))
            capped = slipped(side, price, tick, {**slip, "extra_ticks": extra}, Pcg32(seed, 1), cap)
            if cap is None:
                self.assertEqual(capped, withx)
            else:
                self.assertEqual(capped, min(withx, cap) if side == "BUY" else max(withx, cap))


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class ParityTests(unittest.TestCase):
    def test_engine_and_oracle_agree_with_extra_ticks(self):
        from stockify.tools import fuzz
        seen: set[tuple[str, str]] = set()
        want = {(f, s["kind"]) for f in ("bar", "snapshot") for s in KINDS}
        for seed in range(4000, 4400):
            world = fuzz.fuzz_world(seed)
            fill = world["run"]["fill_model"]["kind"]
            fam = "bar" if fill == "bar" else "snapshot" if fill.startswith("snapshot") else None
            if fam is None:
                continue
            for s in KINDS:
                if (fam, s["kind"]) in seen:
                    continue
                w = copy.deepcopy(world)
                w["run"]["fill_model"]["slippage"] = {**s, "extra_ticks": 2}
                with self.subTest(seed=seed, fill=fill, slip=s["kind"]), tempfile.TemporaryDirectory() as tmp:
                    self.assertIsNone(fuzz.check_world(w, Path(tmp)))
                seen.add((fam, s["kind"]))
                break
            if seen >= want:
                break
        self.assertEqual(seen, want)


if __name__ == "__main__":
    unittest.main()

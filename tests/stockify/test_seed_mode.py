"""VOL 5.1 (D-66, A4 §4.5): `seed_mode: per_day` reseeds both PCG32 streams at each trading day.

A day's seed is the first 8 bytes, little-endian, of SHA-256 of `stockify-seed:<seed>:<YYYYMMDD>`, written into
the bundle as `days[].seed`; both implementations re-create stream 1 (slippage) and stream 2 (`ctx.rng()`) from it
before the first event of that IST date. The default, `seed_mode: run`, leaves every bundle and golden unchanged.
"""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import bundle, engine, golden, runconfig, timeutil
from stockify.oracle import run_bundle as oracle_run
from stockify.oracle.rng import STRATEGY_STREAM, Pcg32
from stockify.strategy import Strategy
from stockify.tools.fuzz import _strategy, fuzz_world
from tests.stockify.test_engine_parity import assert_same_artifacts

SEEDS = range(4000, 4050)


def _per_day(world: dict) -> dict:
    w = copy.deepcopy(world)
    w["run"]["seed_mode"] = "per_day"
    return w


class DrawPerDay(Strategy):
    """Draws ctx.rng() once, on the first bar of each IST date, and logs it: stream 2's first number that day."""

    def __init__(self, ids: list[int]):
        self.ids = ids
        self.seen: set[str] = set()

    def on_start(self, ctx):  # noqa: ANN001
        for iid in self.ids:
            ctx.subscribe_bars(iid, 60)

    def on_bar(self, ctx, bar):  # noqa: ANN001
        d = timeutil.ns_to_ist(ctx.now()).date().isoformat()
        if d not in self.seen:
            self.seen.add(d)
            ctx.log("INFO", f"draw {d} {ctx.rng().below(1_000_000_007)}")


def _draws(run: Path) -> dict[str, int]:
    out = {}
    for line in (run / "events_log.txt").read_text(encoding="utf-8").splitlines():
        if " INFO draw " in line:
            d, v = line.split(" INFO draw ")[1].split()
            out[d] = int(v)
    return out


class SeedModeTests(unittest.TestCase):
    def test_day_seed_matches_a4(self):
        self.assertEqual(bundle.day_seed(7, date(2026, 11, 17)),
                         int.from_bytes(hashlib.sha256(b"stockify-seed:7:20261117").digest()[:8], "little"))
        self.assertEqual(bundle.day_seed(2 ** 64 - 1, date(2026, 1, 2)),
                         int.from_bytes(hashlib.sha256(f"stockify-seed:{2 ** 64 - 1}:20260102".encode()).digest()[:8],
                                        "little"))

    def test_seed_mode_is_validated(self):
        base = {"schema": "stockify.run/1", "name": "x", "strategy": {"kind": "script", "actions": []},
                "data": {"packs": [{"sha256": "0" * 64}]}, "capital_paise": 1, "price_basis": "raw",
                "fill_model": {"kind": "bar"}}
        self.assertNotIn("seed_mode", runconfig.load(base))
        self.assertEqual(runconfig.load({**base, "seed_mode": "run"}), runconfig.load(base))
        self.assertEqual(runconfig.load({**base, "seed_mode": "per_day"})["seed_mode"], "per_day")
        with self.assertRaisesRegex(runconfig.RunConfigError, "seed_mode"):
            runconfig.load({**base, "seed_mode": "daily"})

    def test_per_day_bundle_carries_each_days_seed(self):
        world = _per_day(fuzz_world(4000))
        b = golden.build_bundle(world)
        self.assertEqual(b["seed"], world["run"]["seed"])          # the top-level seed stays the run's
        for d in b["days"]:
            self.assertEqual(d["seed"], bundle.day_seed(world["run"]["seed"], date.fromisoformat(d["date"])))

    def test_seed_mode_run_changes_nothing(self):
        for path, g in golden.goldens():
            with self.subTest(golden=g["id"]):
                explicit = golden._merge(g, {"run": {"seed_mode": "run"}})
                a, b = golden.build_bundle(g), golden.build_bundle(explicit)
                self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
                self.assertTrue(all("seed" not in d for d in a["days"]))
                impls = ("oracle", "engine") if engine.available() else ("oracle",)
                for impl in impls:
                    self.assertEqual(golden.run_one(explicit, impl), [], f"{g['id']} on {impl}")

    @unittest.skipUnless(engine.available(), "stockify._engine is not built")
    def test_per_day_reseeds_both_streams_on_both_engines(self):
        changed = 0
        for seed in SEEDS:
            world = _per_day(fuzz_world(seed))
            with self.subTest(seed=seed), tempfile.TemporaryDirectory(prefix="seedmode-") as tmp:
                folder = Path(tmp)
                pack = golden.build_pack(world, folder)
                b = golden.build_bundle(world)
                errors = []
                for impl, runner in (("oracle", oracle_run), ("engine", engine.run_bundle)):
                    try:
                        runner(b, [pack], folder / impl, strategy=_strategy(world), report=False)
                        errors.append(None)
                    except Exception as e:  # noqa: BLE001 — both sides must fail alike
                        errors.append(e)
                if errors[0] or errors[1]:
                    self.assertTrue(errors[0] is not None and errors[1] is not None, repr(errors))
                    self.assertEqual(str(errors[0]), str(errors[1]))
                    continue
                assert_same_artifacts(self, folder / "oracle", folder / "engine")
                runwise = golden.build_bundle(fuzz_world(seed))
                oracle_run(runwise, [pack], folder / "run_mode", strategy=_strategy(world), report=False)
                if (folder / "run_mode" / "events_log.txt").read_bytes() != \
                        (folder / "oracle" / "events_log.txt").read_bytes():
                    changed += 1
        self.assertGreater(changed, len(SEEDS) // 2, "per_day reseeding changed too few runs")

    @unittest.skipUnless(engine.available(), "stockify._engine is not built")
    def test_each_days_first_draw_comes_from_that_days_seed(self):
        world = _per_day(fuzz_world(4001))
        with tempfile.TemporaryDirectory(prefix="seedmode-draw-") as tmp:
            folder = Path(tmp)
            pack = golden.build_pack(world, folder)
            b = golden.build_bundle(world)
            ids = [i["id"] for i in b["instruments"]]
            draws = {}
            for impl, runner in (("oracle", oracle_run), ("engine", engine.run_bundle)):
                runner(b, [pack], folder / impl, strategy=DrawPerDay(ids), report=False)
                draws[impl] = _draws(folder / impl)
            self.assertEqual(draws["oracle"], draws["engine"])
            self.assertGreaterEqual(len(draws["oracle"]), 2)
            for d, v in draws["oracle"].items():
                fresh = Pcg32(bundle.day_seed(world["run"]["seed"], date.fromisoformat(d)), STRATEGY_STREAM)
                self.assertEqual(v, fresh.below(1_000_000_007), d)


if __name__ == "__main__":
    unittest.main()

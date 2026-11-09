"""M3.12: the fuzz shrinker and failures saved as goldens (`stockify.tools.fuzz`, A3 P-04).

The engine agrees with the oracle on every seed, so a disagreement is planted in the comparison (the
`_engine_bytes` seam), never in the engine: one paisa added to the price of every fill of instrument 1.
Skipped when the engine is not built (`run.ps1 -BuildEngine`).
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine, golden
from stockify.oracle import run_bundle as oracle_run
from stockify.tools import fuzz

CSVS = ("orders.csv", "order_events.csv", "fills.csv", "charges.csv", "positions_eod.csv", "equity.csv")


def _tampered(path: Path) -> bytes:
    """The engine's file, with 1 paisa added to each fill price of instrument 1."""
    data = path.read_bytes()
    if path.name != "fills.csv":
        return data
    lines = data.decode("utf-8").split("\n")
    for n, line in enumerate(lines[1:], start=1):
        cells = line.split(",")
        if len(cells) > 6 and cells[3] == "1":
            cells[6] = str(int(cells[6]) + 1)
            lines[n] = ",".join(cells)
    return "\n".join(lines).encode("utf-8")


def _fills_of_1(world: dict) -> int:
    """Fills of instrument 1 in the world's oracle run; 0 for a world the oracle refuses (a span_exact world it
    cannot price, MEZ 4.10), which has no fills to plant a disagreement in."""
    with tempfile.TemporaryDirectory(prefix="fuzz-probe-") as tmp:
        folder = Path(tmp)
        try:
            oracle_run(golden.build_bundle(world), [golden.build_pack(world, folder)], folder / "run",
                       strategy=fuzz._strategy(world), report=False)
        except Exception:  # noqa: BLE001
            return 0
        return sum(1 for r in golden._read_csv(folder / "run" / "fills.csv") if r["instrument_id"] == "1")


def _daily(world: dict) -> bool:
    return world["events"][0].get("interval_s") == 86400


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class FuzzToolTests(unittest.TestCase):
    def test_a_recording_replays_exactly(self):
        """The recorded calls, replayed, give the live trader's run (its own log lines aside)."""
        for seed in (4000, 4003, 4011, 4017):
            world = fuzz.fuzz_world(seed)
            with self.subTest(seed=seed), tempfile.TemporaryDirectory(prefix="fuzz-replay-") as tmp:
                folder = Path(tmp)
                pack = golden.build_pack(world, folder)
                oracle_run(golden.build_bundle(world), [pack], folder / "live", strategy=fuzz._strategy(world),
                           report=False)
                replay = fuzz.record(world)
                self.assertEqual(replay["strategy"]["ref"], fuzz.REPLAY_REF)
                self.assertGreater(len(replay["strategy"]["params"]["calls"]), 10)
                for impl, runner in (("oracle", oracle_run), ("engine", engine.run_bundle)):
                    runner(golden.build_bundle(replay), [pack], folder / impl, report=False)
                    for name in CSVS:
                        self.assertEqual((folder / impl / name).read_bytes(), (folder / "live" / name).read_bytes(),
                                         f"{impl} {name}")

    def test_seeds_agree_and_the_seed_parser(self):
        self.assertEqual(fuzz.parse_seeds("4000-4002,4010"), [4000, 4001, 4002, 4010])
        self.assertEqual(fuzz.run_seeds(range(4000, 4006)), [])

    def test_a_planted_disagreement_is_shrunk_and_saved_as_a_golden(self):
        # a daily-bar world (21 bars) shrinks in seconds; a minute-bar one needs the CLI's larger budget
        seed = next(s for s in range(4000, 4200) if _daily(fuzz.fuzz_world(s)) and _fills_of_1(fuzz.fuzz_world(s)) >= 2)
        world = fuzz.fuzz_world(seed)
        with mock.patch.object(fuzz, "_engine_bytes", _tampered):
            failures = fuzz.run_seeds([seed])
            self.assertEqual(len(failures), 1)
            f = failures[0]
            self.assertEqual((f.seed, f.file), (seed, "fills.csv"))
            small, f2 = fuzz.shrink(f.world, f, max_runs=600)
            self.assertEqual(f2.file, "fills.csv")
            with tempfile.TemporaryDirectory(prefix="fuzz-shrunk-") as tmp:
                self.assertIsNotNone(fuzz.check_world(small, Path(tmp)), "the shrunk world must still fail")
        calls = small["strategy"]["params"]["calls"]
        self.assertLess(len(small["events"]), len(world["events"]) // 2)
        self.assertLess(len(small["bundle"]["instruments"]), len(world["bundle"]["instruments"]))
        self.assertLessEqual(len([c for c in calls if c[1] == "place"]), 3)

        with tempfile.TemporaryDirectory(prefix="fuzz-golden-") as tmp, \
                mock.patch.object(golden, "GOLDEN_DIR", Path(tmp)):
            path = fuzz.save_golden(small, f2, Path(tmp))
            self.assertEqual(path.name, f"F{seed}.yaml")
            found = [g for _, g in golden.goldens()]
            self.assertEqual([g["id"] for g in found], [f"F{seed}"])
            g = found[0]
            self.assertIn("fills.csv", g["title"])
            self.assertGreaterEqual(g["expect"]["fills_count"], 1)
            self.assertEqual(golden.run_one(g, "oracle"), [])
            self.assertEqual(golden.run_one(g, "engine"), [])   # the real engine agrees: the tamper was planted
            g["expect"]["fills"][0]["price_paise"] += 1          # and the golden does check the values
            self.assertTrue(any("price_paise" in p for p in golden.run_one(g, "oracle")))

    def test_the_cli_reports_and_exits_nonzero_on_a_disagreement(self):
        import io
        out = io.StringIO()
        self.assertEqual(fuzz.main([4000, 4001], False, False, out=out), 0)
        self.assertIn("2 seeds, 0 disagreements", out.getvalue())
        seed = next(s for s in range(4000, 4100) if _fills_of_1(fuzz.fuzz_world(s)) >= 1)
        out = io.StringIO()
        with mock.patch.object(fuzz, "_engine_bytes", _tampered):
            self.assertEqual(fuzz.main([seed], False, False, out=out), 1)
        self.assertIn(f"FAIL seed {seed}: fills.csv line", out.getvalue())


if __name__ == "__main__":
    unittest.main()

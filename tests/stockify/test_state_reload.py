"""ST 5.9: a state either runner writes is accepted by its own loader (and the other's), and holds one bar per start.

A pack can carry real bars and ticks for one instrument. Ticks used to build 60 s bars until the first real bar had
arrived (A4 section 2.2), and the first minute's built bar and its real bar then landed in `bar_history` with the
same start: the oracle's own state was refused by its own loader the next morning ("start_ns must rise").
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import bundle as bundle_mod, engine, golden
from stockify.oracle import sod_state
from stockify.oracle.runner import Engine, load_records
from stockify.strategy import Strategy
from tests.stockify.test_engine_sod_state import engine_run, split_pack
from tests.stockify.test_sod_state import _eq, _run, day_bundles, slice_records

DAYS = ["2026-03-02", "2026-03-03"]
NEEDS_ENGINE = unittest.skipUnless(engine.available(), "stockify._engine is not built")


class SubscribedBars(Strategy):
    def on_start(self, ctx):  # noqa: ANN001
        ctx.subscribe_bars(1, 60)
        ctx.subscribe_ticks(1)


def mixed_world(real_bars_from: int = 0) -> dict:
    """One stock with a real 60 s bar every minute (arriving at the bar's end) and a tick 10 s into each minute.
    `real_bars_from`: the minute (0..29) the real bars start at; the ticks come all along."""
    events = []
    for n in range(30):
        hhmm = f"09:{15 + n:02d}"
        events.append({"kind": "TICK_QUOTE", "instrument": 1, "t": f"{DAYS[0]}T{hhmm}:10+05:30",
                       "ltp": 10_000 + n, "volume": 1000 * (n + 1)})
        if n >= real_bars_from:
            events.append({"kind": "BAR", "instrument": 1, "interval_s": 60, "start": f"{DAYS[0]}T{hhmm}:00+05:30",
                           "o": 10_000, "h": 10_050, "l": 9_950, "c": 10_000 + n, "v": 100})
    return {"id": "MIXBARS", "bundle": {"dates": DAYS, "instruments": [_eq(1, "AAA")]},
            "run": {"fill_model": {"kind": "snapshot_ltp"}, "capital_paise": 10**9, "price_basis": "raw",
                    "seed_mode": "per_day"},
            "events": events, "strategy": {"actions": []}}


def day_two(bundle: dict, state: str) -> dict:
    b = day_bundles(bundle)[1]
    b["sod_state"] = json.loads(state)
    return json.loads(bundle_mod.dumps(b))


class StateReloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(tests.support.SANDBOX_ROOT))
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    def day_one(self, world: dict):
        bundle, pack = golden.build_bundle(world), golden.build_pack(world, self.folder)
        records = load_records([pack])
        state = _run(day_bundles(bundle)[0], slice_records(records, bundle["days"])[0], SubscribedBars(),
                     self.folder / "o0").eod_state()
        return bundle, pack, state

    def test_one_bar_per_start_and_the_oracle_reloads_its_own_state(self):
        for from_minute in (0, 1, 7):
            with self.subTest(real_bars_from=from_minute):
                bundle, _, state = self.day_one(mixed_world(from_minute))
                for key, rows in json.loads(state)["bar_history"].items():
                    starts = [r[0] for r in rows]
                    self.assertEqual(starts, sorted(set(starts)), key)
                Engine(day_two(bundle, state), [], Strategy())          # the constructor applies sod_state

    @NEEDS_ENGINE
    def test_the_engine_writes_the_same_state_and_reloads_it(self):
        for from_minute in (0, 1, 7):
            with self.subTest(real_bars_from=from_minute):
                bundle, pack, oracle_state = self.day_one(mixed_world(from_minute))
                (self.folder / f"p{from_minute}").mkdir()
                parts = split_pack(pack, bundle["days"], self.folder / f"p{from_minute}")
                engine_state = engine_run(day_bundles(bundle)[0], parts[0], SubscribedBars(),
                                          self.folder / f"e{from_minute}")
                self.assertEqual(engine_state, oracle_state)
                b2 = day_two(bundle, engine_state)
                (self.folder / f"r{from_minute}").mkdir()
                bundle_mod.write(b2, self.folder / f"r{from_minute}")
                engine.load().Engine(str(self.folder / f"r{from_minute}" / "bundle.json"))

    def test_the_oracle_loader_still_refuses_unordered_bars(self):
        """The rule stays as strict as it was: a state with two bars of one start is refused."""
        bundle, _, state = self.day_one(mixed_world(0))
        doc = json.loads(state)
        key = next(k for k in doc["bar_history"] if doc["bar_history"][k])
        doc["bar_history"][key].append(list(doc["bar_history"][key][-1]))
        with self.assertRaisesRegex(Exception, "start_ns must rise"):
            Engine(day_two(bundle, json.dumps(doc)), [], Strategy())
        self.assertTrue(sod_state.dumps)


class FuzzWorldStateTests(unittest.TestCase):
    """Property: over the fuzz worlds, every day's state is accepted by the loader of the next day's run (the
    day-by-day runs load each, on the oracle and on the engine), and no bar history repeats or reorders a start."""

    def test_every_state_of_every_world_reloads_and_has_rising_bar_starts(self):
        from tests.stockify.test_engine_sod_state import SEEDS, FuzzOutcomes
        checked = 0
        for seed in SEEDS:
            o = FuzzOutcomes.get(seed)
            if o.error is not None:
                continue
            states = list(o.oracle_states) + (list(o.engine_states) if engine.available() else [])
            self.assertTrue(states, seed)
            for text in states:
                for key, rows in json.loads(text)["bar_history"].items():
                    starts = [r[0] for r in rows]
                    self.assertEqual(starts, sorted(set(starts)), f"seed {seed} {key}")
                checked += 1
        self.assertGreater(checked, 100)


if __name__ == "__main__":
    unittest.main()

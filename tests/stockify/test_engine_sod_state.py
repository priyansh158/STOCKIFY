"""VOL 5.3 (D-47 closed, A1 §6.4, A4 §1.10): the C++ engine carries a paper session's state day to day, byte for byte
as the oracle does.

The oracle's split-equals-whole harness (tests.stockify.test_sod_state) is reused: the same one-day bundles, the same
record slices (here as one pack per day), the same joined artifacts. For every fuzz seed the oracle's and the engine's
states must be the same bytes on every day, the engine's day-by-day artifacts must equal the oracle's, and the engine
run day by day must equal the engine run whole.
"""
from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import bundle as bundle_mod, engine, golden, numerics
from stockify.oracle import sod_state
from stockify.oracle.runner import load_records
from stockify.strategy import Strategy
from stockify.tools import fuzz
from tests.stockify.test_sod_state import (IndicatorTrader, _join, _script, _shipped_worlds, day_bundles,
                                           delivery_window_world, dp_world, first_difference, fsp_world,
                                           long_history_world, run_day_by_day, run_whole, slice_records)

SEEDS = range(4000, 4100)


# ── Running the engine whole and day by day ──────────────────────────────────

def split_pack(pack: Path, days: list[dict], folder: Path) -> list[Path | None]:
    """One pack per trading day, the records sliced as test_sod_state.slice_records slices them (a record belongs to
    the first day whose DAY_END is at or after it; later ones to the last day); None for a day with no record."""
    from stockify.data import packs as P
    data = pack.read_bytes()
    header = P.HEADER.unpack_from(data, 0)
    recs, off = [], 64
    while off < len(data):
        size = struct.unpack_from("<H", data, off + 2)[0]
        recs.append(data[off:off + size])
        off += size
    ends = [d["day_end_ns"] for d in days]
    parts: list[list[bytes]] = [[] for _ in days]
    for r in recs:
        t = struct.unpack_from("<q", r, 8)[0]
        i = next((n for n, end in enumerate(ends) if t <= end), len(days) - 1)
        parts[i].append(r)
    out: list[Path | None] = []
    for i, part in enumerate(parts):
        if not part:
            out.append(None)
            continue
        ts = [struct.unpack_from("<q", r, 8)[0] for r in part]
        body = b"".join(P._with_seq(r, n) for n, r in enumerate(part, start=1))
        head = P.HEADER.pack(header[0], header[1], header[2], 64, len(part), min(ts), max(ts), header[7])
        p = folder / f"{pack.stem}_day{i}.evt"
        p.write_bytes(head + body)
        out.append(p)
    return out


def engine_run(bundle: dict, pack: Path | None, strategy, folder: Path) -> str:  # noqa: ANN001
    """One bundle on the C++ engine, from its canonical bundle.json: the run folder's artifacts, then eod_state()."""
    folder.mkdir(parents=True, exist_ok=True)
    bundle_mod.write(json.loads(bundle_mod.dumps(bundle)), folder)
    e = engine.load().Engine(str(folder / "bundle.json"))
    try:
        if pack is not None:
            e.add_pack(str(pack))
        if isinstance(strategy, str):
            e.set_strategy_cpp(strategy)
        else:
            e.set_strategy_py(strategy)
        e.run()
        e.write_artifacts(str(folder))
        return e.eod_state()
    finally:
        del e


def engine_whole(bundle: dict, pack: Path, make, folder: Path) -> str:  # noqa: ANN001
    return engine_run(bundle, pack, make(), folder / "whole")


def engine_day_by_day(bundle: dict, pack: Path, make, folder: Path) -> list[str]:  # noqa: ANN001
    """Day by day from each previous day's eod_state; the joined artifacts go to folder/parts. Returns the states."""
    folder.mkdir(parents=True, exist_ok=True)
    packs = split_pack(pack, bundle["days"], folder)
    state = None
    folders, states = [], []
    for i, b in enumerate(day_bundles(bundle)):
        b["sod_state"] = json.loads(state) if state is not None else None
        state = engine_run(b, packs[i], make(), folder / f"day{i}")
        states.append(state)
        folders.append(folder / f"day{i}")
    _join(folders, folder / "parts")
    return states


def _world_inputs(world: dict, folder: Path) -> tuple[dict, Path]:
    return golden.build_bundle(world), golden.build_pack(world, folder)


def _records(pack: Path) -> list[bytes]:
    data = pack.read_bytes()
    out, off = [], 64
    while off < len(data):
        size = struct.unpack_from("<H", data, off + 2)[0]
        out.append(data[off:off + size])
        off += size
    return out


# ── Both implementations on one world ────────────────────────────────────────

class Outcome:
    """One world on both sides: whole and day by day. `error` is set when the whole run fails (then both sides and both
    ways must fail alike, and nothing is compared)."""

    def __init__(self, bundle: dict, pack: Path, make, folder: Path, model: str | None):  # noqa: ANN001
        self.folder = folder
        self.error = None
        records = load_records([pack])
        try:
            run_whole(bundle, records, make, folder / "oracle")
        except Exception as e:  # noqa: BLE001 — then every other run must fail with the same error
            self.error = e
            for go in (lambda: run_day_by_day(bundle, records, make, folder / "oracle"),
                       lambda: engine_whole(bundle, pack, make, folder / "engine"),
                       lambda: engine_day_by_day(bundle, pack, make, folder / "engine")):
                try:
                    go()
                except Exception as x:  # noqa: BLE001
                    if model == "span_exact" and str(x) != str(e):
                        raise AssertionError(f"failed differently: {x!r} vs {e!r}") from x
                else:
                    raise AssertionError(f"the whole oracle run failed ({e!r}) but another run did not")
            return
        self.oracle_states = run_day_by_day(bundle, records, make, folder / "oracle")
        engine_whole(bundle, pack, make, folder / "engine")
        self.engine_states = engine_day_by_day(bundle, pack, make, folder / "engine")
        self.days = len(bundle["days"])

    def engine_vs_oracle_day(self, i: int) -> str | None:
        return first_difference(self.folder / "oracle" / f"day{i}", self.folder / "engine" / f"day{i}")

    def engine_parts_vs_whole(self) -> str | None:
        return first_difference(self.folder / "engine" / "whole", self.folder / "engine" / "parts")

    def oracle_vs_engine_whole(self) -> str | None:
        return first_difference(self.folder / "oracle" / "whole", self.folder / "engine" / "whole")


class FuzzOutcomes:
    """The 100 fuzz worlds, run once for the three tests below."""
    _by_seed: dict[int, Outcome] = {}

    @classmethod
    def get(cls, seed: int) -> Outcome:
        if seed not in cls._by_seed:
            world = fuzz.fuzz_world(seed)
            world["run"]["seed_mode"] = "per_day"                   # paper sessions run per_day (A4 §4.5)
            ids = [i["id"] for i in world["bundle"]["instruments"]]
            folder = Path(tempfile.mkdtemp(prefix=f"sod-{seed}-", dir=str(tests.support.SANDBOX_ROOT)))
            bundle, pack = _world_inputs(world, folder)

            def make(ids=ids, p=world["fuzz"]["p_act"]):  # noqa: ANN001, ANN202
                return fuzz.RandomTrader(ids, p)
            cls._by_seed[seed] = Outcome(bundle, pack, make, folder, world["run"].get("margin", {}).get("model"))
        return cls._by_seed[seed]


class LongHistoryOutcome:
    _outcome: Outcome | None = None

    @classmethod
    def get(cls) -> Outcome:
        if cls._outcome is None:
            world, sat = long_history_world()
            folder = Path(tempfile.mkdtemp(prefix="sod-long-", dir=str(tests.support.SANDBOX_ROOT)))
            bundle, pack = _world_inputs(world, folder)
            cls._outcome = Outcome(bundle, pack, lambda: IndicatorTrader(2, 1, 10, sat), folder, None)
            assert cls._outcome.error is None
        return cls._outcome


# ── Tests ────────────────────────────────────────────────────────────────────

@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class EngineStateParityTests(unittest.TestCase):
    def test_engine_state_equals_oracle_state_byte_for_byte(self):
        compared = 0
        for seed in SEEDS:
            o = FuzzOutcomes.get(seed)
            if o.error is not None:
                continue
            for i in range(o.days):
                self.assertEqual(o.oracle_states[i], o.engine_states[i], (seed, i))
            compared += 1
        self.assertGreater(compared, 70)
        o = LongHistoryOutcome.get()
        self.assertEqual(o.oracle_states, o.engine_states)
        self.assertEqual(len(json.loads(o.engine_states[2])["bar_history"]["2:10"]), 6000)   # never trimmed

    def test_engine_split_equals_whole(self):
        for seed in SEEDS:
            o = FuzzOutcomes.get(seed)
            if o.error is None:
                self.assertIsNone(o.engine_parts_vs_whole(), seed)
        self.assertIsNone(LongHistoryOutcome.get().engine_parts_vs_whole())

    def test_engine_day_by_day_equals_oracle_day_by_day(self):
        for seed in SEEDS:
            o = FuzzOutcomes.get(seed)
            if o.error is None:
                for i in range(o.days):
                    self.assertIsNone(o.engine_vs_oracle_day(i), (seed, i))
                self.assertIsNone(o.oracle_vs_engine_whole(), seed)
        o = LongHistoryOutcome.get()
        for i in range(o.days):
            self.assertIsNone(o.engine_vs_oracle_day(i), i)
        self.assertIsNone(o.oracle_vs_engine_whole())


class Probe(Strategy):
    calls: list = []

    def on_start(self, ctx):  # noqa: ANN001
        Probe.calls.append("on_start")
        ctx.subscribe_bars(1, 60)
        ctx.schedule_every(3600 * 10**9, ctx.now() + 60 * 10**9)

    def save_state(self) -> dict:
        return {"n": 7}

    def load_state(self, state: dict) -> None:
        Probe.calls.append(f"load_state {state}")


class WorldChecks(unittest.TestCase):
    """The oracle's split-equals-whole worlds, on the engine as well (review I3): the oracle's day-by-day states, the
    engine's, and every artifact of both must agree, and the engine split must equal the engine whole."""

    def assert_engine_world(self, bundle: dict, pack: Path, make, label) -> Outcome:  # noqa: ANN001
        folder = Path(tempfile.mkdtemp(prefix="sod-world-", dir=str(tests.support.SANDBOX_ROOT)))
        o = Outcome(bundle, pack, make, folder, bundle["run"].get("margin", {}).get("model"))
        if o.error is None:
            self.assertEqual(o.oracle_states, o.engine_states, label)
            self.assertIsNone(o.engine_parts_vs_whole(), label)
            self.assertIsNone(o.oracle_vs_engine_whole(), label)
            for i in range(o.days):
                self.assertIsNone(o.engine_vs_oracle_day(i), (label, i))
        return o


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class EngineCarriedPartsTests(WorldChecks):
    """Task 5's CarriedPartsTests worlds (a margin window across the split, a DP charge, a settlement price)."""

    def test_carried_parts_worlds_on_the_engine(self):
        for name, world_of in (("delivery window", delivery_window_world), ("dp", dp_world), ("fsp", fsp_world)):
            with self.subTest(world=name):
                world = world_of()
                folder = Path(tempfile.mkdtemp(prefix="sod-carried-", dir=str(tests.support.SANDBOX_ROOT)))
                bundle, pack = _world_inputs(world, folder)
                o = self.assert_engine_world(bundle, pack, lambda b=bundle: _script(b), world["id"])
                self.assertIsNone(o.error, o.error)


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class EngineShippedStrategyTests(WorldChecks):
    def test_every_shipped_strategy_split_equals_whole_on_the_engine(self):
        from stockify import strategy as S
        for ref, (world, params, build) in _shipped_worlds().items():
            with self.subTest(strategy=ref):
                folder = Path(tempfile.mkdtemp(prefix="sod-shipped-", dir=str(tests.support.SANDBOX_ROOT)))
                bundle = build(world)
                bundle["strategy"]["params"] = params
                pack = golden.build_pack(world, folder)
                o = self.assert_engine_world(bundle, pack, lambda r=ref: S.load(r), ref)
                self.assertIsNone(o.error, o.error)

    def test_script_goldens_split_equals_whole_on_the_engine(self):
        multi = [(p, g) for p, g in golden.goldens() if len(g["bundle"]["dates"]) > 1 and not g["strategy"].get("ref")]
        self.assertGreaterEqual(len(multi), 4)
        for _, g in multi:
            with self.subTest(golden=g["id"]):
                folder = Path(tempfile.mkdtemp(prefix="sod-golden-", dir=str(tests.support.SANDBOX_ROOT)))
                bundle, pack = _world_inputs(g, folder)
                self.assert_engine_world(bundle, pack, lambda b=bundle: _script(b), g["id"])


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class EngineStateBindingTests(unittest.TestCase):
    def _first_day(self, folder: Path, seed: int = 4003) -> tuple[dict, dict, Path | None, Path, list[int]]:
        """A fuzz world's whole bundle, its first-day bundle, the first day's pack and the whole pack."""
        world = fuzz.fuzz_world(seed)
        world["run"]["seed_mode"] = "per_day"
        folder.mkdir(parents=True, exist_ok=True)
        bundle, pack = _world_inputs(world, folder)
        b0 = day_bundles(bundle)[0]
        day0 = split_pack(pack, bundle["days"], folder)[0]
        return bundle, b0, day0, pack, [i["id"] for i in world["bundle"]["instruments"]]

    def test_state_round_trips_through_the_binding(self):
        with tempfile.TemporaryDirectory(prefix="sod-bind-") as tmp:
            folder = Path(tmp)
            bundle, b0, day0, _, ids = self._first_day(folder)
            b1 = day_bundles(bundle)[1]
            text = engine_run(b0, day0, fuzz.RandomTrader(ids, 40), folder / "d0")
            self.assertEqual(text, numerics.dumps_canonical(json.loads(text)))   # canonical JSON
            st = json.loads(text)
            self.assertEqual(st["schema"], sod_state.SCHEMA)
            self.assertEqual(st["trade_date"], bundle["days"][0]["date"])
            self.assertEqual(list(st), sorted(sod_state.KEYS))
            # load_sod_state on a fresh engine, then eod_state: the same bytes
            (folder / "d1").mkdir()
            bundle_mod.write(json.loads(bundle_mod.dumps(b1)), folder / "d1")
            e = engine.load().Engine(str(folder / "d1" / "bundle.json"))
            e.set_strategy_py(fuzz.RandomTrader(ids, 40))
            e.load_sod_state(text)
            self.assertEqual(e.eod_state(), text)
            with self.assertRaisesRegex(RuntimeError, "already"):
                e.load_sod_state(text)
            del e
            # the constructor applies the bundle's sod_state
            b1["sod_state"] = st
            bundle_mod.write(json.loads(bundle_mod.dumps(b1)), folder / "d1")
            e = engine.load().Engine(str(folder / "d1" / "bundle.json"))
            e.set_strategy_py(fuzz.RandomTrader(ids, 40))
            self.assertEqual(e.eod_state(), text)
            del e
            # refusals, as the oracle's (A1 §6.4)
            b1["sod_state"] = None
            bundle_mod.write(json.loads(bundle_mod.dumps(b1)), folder / "d1")

            def fresh():  # noqa: ANN202
                x = engine.load().Engine(str(folder / "d1" / "bundle.json"))
                x.set_strategy_py(Strategy())
                return x
            e = fresh()
            e.run()
            with self.assertRaisesRegex(RuntimeError, "before the first event"):
                e.load_sod_state(text)
            del e
            bad = dict(st)
            del bad["timers"]
            with self.assertRaisesRegex(RuntimeError, "timers"):
                fresh().load_sod_state(numerics.dumps_canonical(bad))
            with self.assertRaisesRegex(RuntimeError, "schema"):
                fresh().load_sod_state(numerics.dumps_canonical({**st, "schema": "stockify.sod_state/0"}))
            for bad in ({**st, "marks": {}}, {**st, "counters": {**st["counters"], "event_no": 0}}):
                with self.assertRaisesRegex(RuntimeError, "unknown keys"):
                    fresh().load_sod_state(numerics.dumps_canonical(bad))
            with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
                fresh().load_sod_state("{not json")

    def test_book_lists_open_positions(self):
        with tempfile.TemporaryDirectory(prefix="sod-book-") as tmp:
            folder = Path(tmp)
            found = False
            for seed in (4003, 4005, 4011, 4017):
                bundle, b0, day0, _, ids = self._first_day(folder / str(seed), seed)
                (folder / str(seed) / "d0").mkdir(parents=True)
                bundle_mod.write(json.loads(bundle_mod.dumps(b0)), folder / str(seed) / "d0")
                e = engine.load().Engine(str(folder / str(seed) / "d0" / "bundle.json"))
                if day0 is not None:
                    e.add_pack(str(day0))
                e.set_strategy_py(fuzz.RandomTrader(ids, 40))
                self.assertEqual(e.book(), [])
                e.run()
                st = json.loads(e.eod_state())
                expected = [(p["instrument_id"], p["product"], p["d"] * p["q"]) for p in st["ledger"]["positions"]
                            if p["q"]]
                self.assertEqual(e.book(), expected)
                found = found or bool(expected)
                del e
            self.assertTrue(found, "no world left a position open")

    def test_on_start_runs_only_on_the_first_day(self):
        with tempfile.TemporaryDirectory(prefix="sod-probe-") as tmp:
            folder = Path(tmp)
            world = fuzz.fuzz_world(4001)
            bundle, pack = _world_inputs(world, folder)
            Probe.calls = []
            states = engine_day_by_day(bundle, pack, Probe, folder)
            self.assertEqual(Probe.calls, ["on_start", "load_state {'n': 7}", "load_state {'n': 7}"])
            for text in states:                                       # restored timers are never duplicated
                self.assertEqual([t[0] for t in json.loads(text)["timers"]], [1])

    def test_a_bad_strategy_state_fails_at_day_end_naming_the_strategy(self):
        class NanState(Strategy):
            def save_state(self) -> dict:
                return {"ema": float("nan")}

        class Fragile(Strategy):
            def load_state(self, state: dict) -> None:
                state["missing"]
        with tempfile.TemporaryDirectory(prefix="sod-bad-") as tmp:
            folder = Path(tmp)
            bundle, b0, day0, _, ids = self._first_day(folder)
            with self.assertRaisesRegex(RuntimeError, r"strategy NanState: save_state\(\) returned state\['ema'\] = nan"):
                engine_run(b0, day0, NanState(), folder / "nan")
            text = engine_run(b0, day0, Strategy(), folder / "plain")
            b1 = day_bundles(bundle)[1]
            (folder / "d1").mkdir()
            bundle_mod.write(json.loads(bundle_mod.dumps(b1)), folder / "d1")
            e = engine.load().Engine(str(folder / "d1" / "bundle.json"))
            e.set_strategy_py(Fragile())
            with self.assertRaisesRegex(RuntimeError, r"strategy Fragile\.load_state\(\) raised KeyError: 'missing'"):
                e.load_sod_state(text)
            del e

    def test_next_days_are_validated(self):
        with tempfile.TemporaryDirectory(prefix="sod-days-") as tmp:
            folder = Path(tmp)
            whole = golden.build_bundle(fuzz.fuzz_world(4003))
            b0 = day_bundles(whole)[0]
            for bad, words in ((list(reversed(b0["next_days"])), "is not after"), ([b0["days"][0]], "is not after"),
                               ([{**b0["next_days"][0], "seed": 1}], "seed"), ({"x": 1}, "must be a list"),
                               ([{"date": "2099-01-01"}], "day object")):
                bundle_mod.write(json.loads(bundle_mod.dumps({**b0, "next_days": bad})), folder)
                with self.assertRaisesRegex(RuntimeError, words):
                    engine.load().Engine(str(folder / "bundle.json"))

    def test_run_bundle_runs_a_day_bundle(self):
        # the Task 5 guard in stockify.engine.run_bundle is gone: a day bundle runs like any other
        with tempfile.TemporaryDirectory(prefix="sod-rb-") as tmp:
            folder = Path(tmp)
            bundle, b0, day0, _, ids = self._first_day(folder)
            res = engine.run_bundle(b0, [day0] if day0 else [], folder / "run", strategy=fuzz.RandomTrader(ids, 40),
                                    report=False)
            self.assertTrue((folder / "run" / "orders.csv").exists())
            self.assertEqual(res.run_id, "run")


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class LivePushReseedTests(unittest.TestCase):
    def test_push_reseeds_per_day_like_run(self):
        """VOL 5.1 on the live path (the Task 4 review): with seed_mode per_day, pushing a world's records in batches
        re-creates both streams at each day's first event exactly as run() does, so the log, the artifacts and the
        end-of-day state are the same; and the same as the oracle's."""
        import random
        rng = random.Random(5301)
        for seed in (4002, 4007):
            with self.subTest(seed=seed), tempfile.TemporaryDirectory(prefix="sod-push-") as tmp:
                folder = Path(tmp)
                world = fuzz.fuzz_world(seed)
                world["run"]["seed_mode"] = "per_day"
                bundle, pack = _world_inputs(world, folder)
                ids = [i["id"] for i in world["bundle"]["instruments"]]
                self.assertTrue(all("seed" in d for d in bundle["days"]))
                whole = engine_run(bundle, pack, fuzz.RandomTrader(ids, 40), folder / "run")
                records = _records(pack)
                pieces, i = [], 0
                while i < len(records):
                    k = rng.randint(1, 6)
                    pieces.append(b"".join(records[i:i + k]))
                    i += k
                (folder / "push").mkdir()
                bundle_mod.write(json.loads(bundle_mod.dumps(bundle)), folder / "push")
                e = engine.load().Engine(str(folder / "push" / "bundle.json"))
                e.set_strategy_py(fuzz.RandomTrader(ids, 40))
                for b in pieces:
                    e.push(b)
                e.finish()
                e.write_artifacts(str(folder / "push"))
                pushed = e.eod_state()
                del e
                self.assertEqual(pushed, whole)
                self.assertIsNone(first_difference(folder / "run", folder / "push"))
                oracle = run_whole(bundle, load_records([pack]), lambda: fuzz.RandomTrader(ids, 40), folder / "o")
                self.assertEqual(oracle.eod_state(), pushed)
                self.assertIsNone(first_difference(folder / "o" / "whole", folder / "push"))


if __name__ == "__main__":
    unittest.main()

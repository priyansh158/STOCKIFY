"""CH 6.21: engine arithmetic on pack and bundle values is checked, and the oracle refuses the same inputs.

The c-review of the engine (2026-11-13) found plain signed int64 arithmetic (undefined behaviour) on values that a
pack or a bundle supplies: volumes, bar prices, timer periods, quotes, strikes, calendar times, a double narrowed to
int64. The engine now throws (OverflowError; a NaN is a ValueError) where the result leaves int64, and the Python
oracle, whose ints never overflow, checks the same results at the same points. Each test runs one crafted world on
both runners and asserts that they refuse alike, and that a value one step inside the edge runs and writes the same
files. Skipped when the engine is not built.
"""
from __future__ import annotations

import math
import random
import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine, golden, numerics
from stockify.fno import black76
from stockify.oracle import matcher as oracle_matcher
from stockify.oracle import run_bundle as oracle_run
from stockify.strategy import Strategy
from tests.stockify.test_engine_parity import assert_same_artifacts

REPO = Path(__file__).resolve().parents[2]
MIN, MAX = numerics.INT64_MIN, numerics.INT64_MAX
EDGES = [MIN, MIN + 1, -(2**62), -1, 0, 1, 2**62, MAX - 1, MAX]
T0 = 1772423000 * 10**9          # inside 2026-03-02 (09:13 IST)


def _tick(m: int, s: int, vol: int = 0, ltp: int = 10000) -> dict:
    return {"kind": "TICK_QUOTE", "instrument": 1, "t": f"2026-03-02T09:{m:02d}:{s:02d}+05:30", "ltp": ltp,
            "volume": vol}


def _world(events: list, fill: str = "snapshot_ltp") -> dict:
    return {"id": "CHK", "bundle": {"dates": ["2026-03-02"], "instruments": [
        {"id": 1, "kind": "EQ", "tradingsymbol": "T", "specs": [{"from": "2026-01-01", "lot": 1, "tick_paise": 5}]}]},
            "run": {"fill_model": {"kind": fill}, "capital_paise": 10_000_000, "price_basis": "raw"},
            "events": events, "strategy": {"actions": []}}


def _golden(name: str) -> dict:
    return golden.load(REPO / "tests" / "golden" / f"{name}.yaml")


class _Timer(Strategy):
    def __init__(self, period: int):
        self.period = period

    def on_start(self, ctx):  # noqa: ANN001
        ctx.subscribe_ticks(ctx.resolve("NSE:T"))
        ctx.schedule_every(self.period, T0 + 5 * 60 * 10**9)


class _Chain(Strategy):
    def on_start(self, ctx):  # noqa: ANN001
        for iid in (2, 3, 4):
            ctx.subscribe_ticks(iid)

    def on_tick(self, ctx, tick):  # noqa: ANN001
        ctx.chain("NIFTY", "2026-03-03")


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class CheckedWorldTests(unittest.TestCase):
    def run_both(self, g: dict, mutate=None, strategy=None) -> dict:  # noqa: ANN001
        """{impl: (exception class name | None, run folder)}; a fresh strategy per runner."""
        tmp = tempfile.TemporaryDirectory(prefix="checked-")
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        pack = golden.build_pack(g, folder)
        out = {}
        for impl, go in (("oracle", oracle_run), ("engine", engine.run_bundle)):
            b = golden.build_bundle(g)
            if mutate:
                mutate(b)
            run = folder / impl
            try:
                go(b, [pack], run, strategy=strategy() if strategy else None, report=False)
                out[impl] = (None, run)
            except Exception as e:  # noqa: BLE001 — the outcome under comparison
                out[impl] = (type(e).__name__, run)
        return out

    def refused_alike(self, g: dict, mutate=None, strategy=None, error: str = "OverflowError") -> None:  # noqa: ANN001
        got = self.run_both(g, mutate, strategy)
        self.assertEqual({k: v[0] for k, v in got.items()}, {"oracle": error, "engine": error})

    def runs_alike(self, g: dict, mutate=None, strategy=None) -> None:  # noqa: ANN001
        got = self.run_both(g, mutate, strategy)
        self.assertEqual({k: v[0] for k, v in got.items()}, {"oracle": None, "engine": None})
        assert_same_artifacts(self, got["oracle"][1], got["engine"][1])

    # ── day volume: evidence (snapshot models) and bars built from ticks ─────────────────────────────────────

    def test_a_volume_step_past_int64_is_refused_by_the_snapshot_evidence(self):
        for v0, v1 in ((-1, MAX), (1, MIN), (MIN, 1), (MAX, -2)):
            with self.subTest(v0=v0, v1=v1):
                self.refused_alike(_world([_tick(15, 0, v0), _tick(15, 1, v1)]))

    def test_a_volume_step_just_inside_int64_runs_the_same(self):
        for v0, v1 in ((-1, MAX - 1), (0, MAX), (MAX, 0), (1, MIN + 1), (MIN, -1), (MAX, -1)):
            with self.subTest(v0=v0, v1=v1):
                self.runs_alike(_world([_tick(15, 0, v0), _tick(15, 1, v1)]))

    def test_a_volume_step_past_int64_is_refused_by_the_bar_builder(self):
        for v0, v1 in ((-1, MAX), (1, MIN)):
            with self.subTest(v0=v0, v1=v1):
                self.refused_alike(_world([_tick(15, 0, v0), _tick(15, 1, v1)], fill="bar"))

    def test_a_built_bar_volume_that_sums_past_int64_is_refused(self):
        ticks = [_tick(15, 0, 0), _tick(15, 1, MAX), _tick(15, 2, 0), _tick(15, 3, MAX)]
        self.refused_alike(_world(ticks, fill="bar"))

    # ── timers ───────────────────────────────────────────────────────────────────────────────────────────────

    def test_a_timer_period_that_carries_the_next_firing_past_int64_is_refused(self):
        g = _world([_tick(15, 0), _tick(20, 0), _tick(25, 0)])
        self.refused_alike(g, strategy=lambda: _Timer(MAX))

    def test_an_ordinary_timer_period_runs_the_same(self):
        g = _world([_tick(15, 0), _tick(20, 0), _tick(25, 0)])
        self.runs_alike(g, strategy=lambda: _Timer(60 * 10**9))

    # ── option settlement: intrinsic value from the FSP and a strike ─────────────────────────────────────────

    def test_an_fsp_so_far_below_the_strike_that_the_difference_leaves_int64_is_refused(self):
        g = _golden("G10")
        for e in g["events"]:
            if e["kind"] == "SETTLE":
                e["price"] = MIN
        self.refused_alike(g)

    def test_the_golden_fsp_runs_the_same(self):
        self.runs_alike(_golden("G10"))

    # ── exposure: continuous-phase spans from the bundle calendar ────────────────────────────────────────────

    def test_a_calendar_span_longer_than_int64_is_refused(self):
        def wide(b: dict) -> None:
            for p in b["days"][0]["groups"]["CM"]:
                if p["phase"] == "CONTINUOUS":
                    p["start_ns"], p["end_ns"] = MIN, MAX
        self.refused_alike(_golden("G01"), mutate=wide)

    # ── chain mids: bid + ask ─────────────────────────────────────────────────────────────────────────────────

    def _chain_world(self, bid: int, ask: int) -> dict:
        g = _golden("G10")
        g["events"].append({"kind": "TICK_FULL", "instrument": 2, "t": "2026-03-03T09:15:40+05:30", "ltp": 10000,
                            "bids": [[bid, 1, 1]], "asks": [[ask, 1, 1]]})
        g["run"]["fill_model"] = {"kind": "bar"}
        g["strategy"] = {"actions": []}
        return g

    def test_a_quote_whose_bid_plus_ask_leaves_int64_is_refused_by_the_chain_mid(self):
        self.refused_alike(self._chain_world(MAX, 1), strategy=_Chain)

    def test_an_ordinary_quote_gives_the_same_chain(self):
        self.runs_alike(self._chain_world(9990, 10010), strategy=_Chain)


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class CheckedHelperParityTests(unittest.TestCase):
    """Seeded edge-value loops through both runners' own functions (the engine's `testing` hooks)."""

    @staticmethod
    def _outcome(fn, *args):  # noqa: ANN001, ANN002
        try:
            return "ok", fn(*args)
        except Exception as e:  # noqa: BLE001 — the outcome under comparison
            return type(e).__name__, None

    def test_year_fraction_refuses_a_span_past_int64_on_both(self):
        t = engine.load().testing
        rng = random.Random(6210)
        pairs = [(a, b) for a in EDGES for b in EDGES] + [(rng.randint(MIN, MAX), rng.randint(MIN, MAX))
                                                          for _ in range(300)]
        refused = 0
        for now, last in pairs:
            o, e = self._outcome(black76.year_fraction, now, last), self._outcome(t.b76_year_fraction, now, last)
            self.assertEqual(o, e, (now, last))
            refused += o[0] == "OverflowError"
        self.assertGreater(refused, 10)
        self.assertEqual(self._outcome(t.b76_year_fraction, MAX, -1)[0], "OverflowError")
        self.assertEqual(self._outcome(t.b76_year_fraction, MAX - 1, MAX)[0], "ok")

    def test_first_reach_and_the_fraction_refuse_alike(self):
        t = engine.load().testing
        rng = random.Random(6211)
        cases = [([rng.choice(EDGES) for _ in range(4)], rng.choice(EDGES), rng.random() < 0.5) for _ in range(4000)]
        cases += [([MAX, MIN, MIN, MAX], 0, True), ([MIN, MAX, 1, 2], MAX, False), ([MIN, 5, 5, 5], MAX, True)]
        seen = set()
        for points, x, down in cases:
            o = self._outcome(oracle_matcher.first_reach, points, x, down)
            e = self._outcome(t.bar_first_reach, points, x, down)
            seen.add(o[0])
            if o[0] == "ok":
                want = None if o[1] is None else (o[1][0], o[1][1].numerator, o[1][1].denominator)
                self.assertEqual(("ok", want), e, (points, x, down))
            else:
                self.assertEqual(o[0], e[0], (points, x, down))
        self.assertEqual(seen, {"ok", "OverflowError"})

    def test_a_fraction_whose_sign_flip_leaves_int64_is_refused(self):
        t = engine.load().testing
        self.assertEqual(self._outcome(t.frac_of, MIN, -1)[0], "OverflowError")
        self.assertEqual(self._outcome(oracle_matcher._frac, MIN, -1)[0], "OverflowError")
        self.assertEqual(self._outcome(t.frac_of, MIN + 1, -1), ("ok", (MAX, 1)))
        self.assertEqual(self._outcome(t.frac_of, 6, -4), ("ok", (-3, 2)))

    def test_a_double_narrowed_to_int64_is_refused_alike(self):
        t = engine.load().testing
        edge = float(2**63)
        for x in (math.nan, math.inf, -math.inf, edge, -edge, math.nextafter(edge, 0), math.nextafter(-edge, 0),
                  1e30, -1e30, 0.0, 0.25, -0.25, 123.0001, 9.2e18):
            with self.subTest(x=x):
                o = self._outcome(lambda v: numerics.check_int64(int(math.ceil(v))), x)
                self.assertEqual(o, self._outcome(t.ceil_to_int64, x))
        self.assertEqual(self._outcome(t.ceil_to_int64, math.nan)[0], "ValueError")
        self.assertEqual(self._outcome(t.ceil_to_int64, math.inf)[0], "OverflowError")


if __name__ == "__main__":
    unittest.main()

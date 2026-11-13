"""M3.10: strategies on the engine (A2 §2–§4, A3 DT-02).

* Every M2 reference strategy (Python) runs unchanged on the engine and its run folder equals the oracle's.
* The C++ ports `sma_cross_cpp` and `orb_cpp` give the same run folders as the Python originals on the oracle.
* A strategy that raises stops the run: the traceback is in events_log.txt, nothing else is written, and the
  exception reaches the caller.
* drain_outputs() hands over, piece by piece, exactly the rows the CSVs end up with.

Random worlds, seeded. Skipped when the engine is not built (`run.ps1 -BuildEngine`).
"""
from __future__ import annotations

import copy
import csv
import random
import struct
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine, golden
from stockify import engine as engine_mod
from stockify.oracle import run_bundle as oracle_run
from stockify.strategy import Strategy
from tests.stockify.test_engine_fuzz import _real_charges
from tests.stockify.test_engine_parity import assert_same_artifacts

SPEC = [{"from": "2026-01-01", "lot": 1, "tick_paise": 5}]


def _weekdays(start: str, n: int) -> list[str]:
    d, out = date.fromisoformat(start), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _world(wid: str, events: list, instruments: list, dates: list[str], rng: random.Random, run=None, **bundle) -> dict:
    run = {"fill_model": {"kind": "bar"}, "capital_paise": rng.choice([2_000_000, 10_000_000, 100_000_000]),
           "price_basis": "raw", **(run or {})}
    return {"id": wid, "bundle": {"dates": dates, "instruments": instruments, **bundle}, "run": run,
            "events": events, "strategy": {"actions": []}}


def _bar(iid: int, isec: int, start: str, o: int, h: int, lo: int, c: int, v: int, t: str | None = None) -> dict:
    e = {"kind": "BAR", "instrument": iid, "interval_s": isec, "start": start, "o": o, "h": h, "l": lo, "c": c, "v": v}
    if t is not None:
        e["t"] = t
    return e


def _step(rng: random.Random, p: int, scale: int) -> int:
    """A tick-aligned move: mostly small, sometimes a jump past the protection band."""
    m = rng.choice([0, 1, 1, 2, 3, 5, 8, 30]) * rng.choice([-1, 1]) * scale
    return max(5 * scale, (p + m) // 5 * 5)


def daily_world(seed: int, future: bool = False) -> tuple[dict, dict]:
    """An equity on daily bars: for buy_and_hold and sma_cross (both implementations). With `future`, the bars
    are an index future's instead, traded NRML in lots, long-only or long/short."""
    rng = random.Random(seed)
    days = _weekdays("2026-03-02", rng.randint(12, 45))
    p, prev, evs = rng.choice([1_000, 25_000, 250_000]), None, []
    for d in days:
        o = p if prev is None else prev
        c = _step(rng, o, max(1, o // 400))
        hi, lo = max(o, c) + rng.randint(0, 2) * 5, max(5, min(o, c) - rng.randint(0, 2) * 5)
        evs.append(_bar(1, 86400, f"{d}T00:00:00+05:30", o, hi, lo, c, rng.choice([0, 500, 100_000, 10**7])))
        prev = c
        p = c
    inst = [{"id": 1, "kind": "EQ", "tradingsymbol": "TEST", "specs": SPEC}]
    if future:
        inst = [{"id": 1, "kind": "FUT", "tradingsymbol": "TEST", "underlying": "IDX", "underlying_id": 2,
                 "expiry": "2026-12-31", "specs": [{"from": "2026-01-01", "lot": rng.choice([15, 50]), "tick_paise": 5}]},
                {"id": 2, "kind": "INDEX", "tradingsymbol": "IDX"}]
    g = _world(f"D{seed}", evs, inst, days, rng)
    if rng.random() < 0.5:
        g["bundle"]["charges"] = _real_charges()
    as_int = rng.choice([int, str, float])                    # the params as a config might spell them
    fast = rng.randint(1, 5)
    params = {"symbol": rng.choice(["NSE:TEST", "TEST"]), "fast": as_int(fast), "slow": as_int(fast + rng.randint(0, 6))}
    if future:
        params.update(symbol=params["symbol"].replace("NSE:", "NFO:"), product="NRML", long_short=rng.random() < 0.7,
                      lots=rng.choice([1, 2, "3"]))
    return g, params


def minute_world(seed: int) -> tuple[dict, dict]:
    """An equity on minute bars over a few days, MIS: for opening_range_breakout (both implementations)."""
    rng = random.Random(seed)
    days = _weekdays("2026-03-02", rng.randint(1, 4))
    evs, p = [], rng.choice([2_000, 50_000, 300_000])
    for d in days:
        times = [(9, 15 + i) for i in range(rng.randint(3, 45))] + [(15, m) for m in range(0, 16)]
        if rng.random() < 0.3:
            times = [(9, 14)] + times                         # a bar before the open (ignored)
        for hh, mm in times:
            hh, mm = hh + mm // 60, mm % 60
            o = p
            c = _step(rng, o, max(1, o // 2000))
            hi, lo = max(o, c) + rng.randint(0, 3) * 5, max(5, min(o, c) - rng.randint(0, 3) * 5)
            evs.append(_bar(1, 60, f"{d}T{hh:02d}:{mm:02d}:00+05:30", o, hi, lo, c, rng.choice([0, 200, 50_000])))
            p = c
    g = _world(f"M{seed}", evs, [{"id": 1, "kind": "EQ", "tradingsymbol": "TEST", "specs": SPEC}], days, rng,
               mis_cutoff={"CM": rng.choice(["15:12", "15:20"])})
    if rng.random() < 0.5:
        g["bundle"]["charges"] = _real_charges()
        g["bundle"]["sqoff_fee_paise"] = 5000
    params = {"symbol": "NSE:TEST", "range_minutes": rng.randint(1, 6), "qty": rng.choice([1, 10, 250]),
              "short": rng.random() < 0.5}
    if rng.random() < 0.5:
        params["exit_hhmm"] = rng.choice(["15:05", "15:10", "9:40", "15:07:30"])
    return g, params


def straddle_world(seed: int) -> tuple[dict, dict]:
    """An index and two expiries of options on end-of-day bars: for eod_short_straddle."""
    rng = random.Random(seed)
    days = _weekdays("2026-03-02", rng.randint(4, 9))
    exp1, exp2 = days[min(len(days) - 1, rng.randint(1, 4))], days[-1]
    spot = rng.choice([2_400_000, 5_000_000])
    strikes = [spot + k * 5_000 for k in range(-3, 4)]
    inst = [{"id": 1, "kind": "INDEX", "tradingsymbol": "NIFTY 50"}]
    opt_spec = [{"from": "2026-01-01", "lot": rng.choice([25, 65, 75]), "tick_paise": 5}]
    legs = []
    for exp in sorted({exp1, exp2}):
        for k in strikes:
            for kind in ("CE", "PE"):
                iid = len(inst) + 1
                inst.append({"id": iid, "kind": kind, "tradingsymbol": f"N{exp}{k}{kind}", "underlying": "NIFTY",
                             "underlying_id": 1, "expiry": exp, "strike_paise": k, "specs": opt_spec})
                legs.append((iid, kind, k, exp))
    evs = []
    for d in days:
        spot = _step(rng, spot, 500)
        for iid, kind, k, exp in legs:
            if exp < d or rng.random() < 0.05:
                continue                                       # expired, or no row in today's file
            intrinsic = max(0, spot - k) if kind == "CE" else max(0, k - spot)
            c = max(5, (intrinsic + rng.randint(0, 30_000)) // 5 * 5)
            evs.append(_bar(iid, 86400, f"{d}T00:00:00+05:30", c, c, c, c, rng.choice([0, 100_000]),
                            t=f"{d}T19:00:00+05:30"))
        evs.append(_bar(1, 86400, f"{d}T00:00:00+05:30", spot, spot, spot, spot, 0, t=f"{d}T19:00:00+05:30"))
        if d in (exp1, exp2):
            evs.append({"kind": "SETTLE", "instrument": 1, "t": f"{d}T16:00:00+05:30", "date": d, "price": spot,
                        "subtype": "final"})
    g = _world(f"S{seed}", evs, inst, days, rng,
               run={"fill_model": {"kind": "eod_close", "half_spread_ticks": rng.randint(0, 2)},
                    "capital_paise": 100_000_000})
    params = {"min_days": rng.randint(0, 3), "lots": rng.randint(1, 2), "stop_multiple": rng.choice(["1.2", "1.5", "3"])}
    return g, params


def _materialise(g: dict, params: dict, folder: Path) -> tuple[Path, dict]:
    pack = golden.build_pack(g, folder)
    b = golden.build_bundle(g)
    b["strategy"]["params"] = copy.deepcopy(params)
    return pack, b


def _load(ref: str) -> Strategy:
    from stockify import strategy as S
    return S.load(ref)


REFS = {
    "buy_and_hold": "stockify.strategies.buy_and_hold:BuyAndHold",
    "sma_cross": "stockify.strategies.sma_cross:SmaCross",
    "orb": "stockify.strategies.opening_range_breakout:OpeningRangeBreakout",
    "straddle": "stockify.strategies.eod_short_straddle:EodShortStraddle",
}


def _fills(folder: Path) -> int:
    return len((folder / "fills.csv").read_text(encoding="utf-8").splitlines()) - 1


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class ReferenceStrategyTests(unittest.TestCase):
    """The same strategy object type on both implementations: identical run folders (DT-02)."""

    def _same(self, ref: str, g: dict, params: dict, cpp: str | None = None) -> int:
        with tempfile.TemporaryDirectory(prefix="strategies-") as tmp:
            folder = Path(tmp)
            pack, b = _materialise(g, params, folder)
            oracle_run(b, [pack], folder / "oracle", strategy=_load(ref), report=False)
            engine_mod.run_bundle(b, [pack], folder / "engine", strategy=_load(ref), report=False)
            assert_same_artifacts(self, folder / "oracle", folder / "engine")
            if cpp is not None:
                engine_mod.run_bundle(b, [pack], folder / "cpp", strategy=cpp, report=False)
                assert_same_artifacts(self, folder / "oracle", folder / "cpp")
            return _fills(folder / "oracle")

    def test_buy_and_hold(self):
        fills = 0
        for seed in range(5100, 5120):
            g, params = daily_world(seed)
            params = {"symbol": params["symbol"], **({"invest": "0.5"} if seed % 3 == 0 else {})}
            with self.subTest(seed=seed):
                fills += self._same(REFS["buy_and_hold"], g, params)
        self.assertGreater(fills, 10)

    def test_sma_cross_and_sma_cross_cpp(self):
        fills = 0
        for seed in range(5200, 5260):
            g, params = daily_world(seed, future=seed % 3 == 0)
            with self.subTest(seed=seed):
                fills += self._same(REFS["sma_cross"], g, params, cpp="sma_cross_cpp")
        self.assertGreater(fills, 60)

    def test_opening_range_breakout_and_orb_cpp(self):
        fills = 0
        for seed in range(5300, 5340):
            g, params = minute_world(seed)
            with self.subTest(seed=seed):
                fills += self._same(REFS["orb"], g, params, cpp="orb_cpp")
        self.assertGreater(fills, 40)

    def test_orb_partial_and_rejected_entries_agree_on_all_three_implementations(self):
        from tests.stockify.test_strategies import StrategyTests
        helper = StrategyTests("test_sources_pass_the_load_check")
        params = {"symbol": "NSE:TEST", "range_minutes": 2, "qty": 10}
        for label, kw in (("partial", {"breakout_volume": 40}), ("rejected", {"capital_paise": 1_000})):
            with self.subTest(case=label):
                self._same(REFS["orb"], helper._orb_world(**kw), params, cpp="orb_cpp")

    def test_eod_short_straddle(self):
        fills = 0
        for seed in range(5400, 5425):
            g, params = straddle_world(seed)
            with self.subTest(seed=seed):
                fills += self._same(REFS["straddle"], g, params)
        self.assertGreater(fills, 20)

    def test_a_cpp_bundle_runs_its_registered_strategy(self):
        g, params = daily_world(5250)
        with tempfile.TemporaryDirectory(prefix="strategies-") as tmp:
            folder = Path(tmp)
            pack, b = _materialise(g, params, folder)
            oracle_run(b, [pack], folder / "oracle", strategy=_load(REFS["sma_cross"]), report=False)
            b["strategy"] = {"kind": "cpp", "ref": "sma_cross_cpp", "params": params, "source_sha256": "0" * 64}
            engine_mod.run_bundle(b, [pack], folder / "cpp", report=False)
            assert_same_artifacts(self, folder / "oracle", folder / "cpp")
        names = engine.load().strategies()
        self.assertEqual(names, sorted(names))
        self.assertLessEqual({"orb_cpp", "sma_cross_cpp"}, set(names))


class _Boom(Strategy):
    def __init__(self, after: int):
        self.after = after

    def on_start(self, ctx):  # noqa: ANN001
        ctx.subscribe_bars(1, 86400)

    def on_bar(self, ctx, bar):  # noqa: ANN001
        self.after -= 1
        if self.after == 0:
            ctx.log("info", "about to fail")
            _ = 1 // 0


class _BadTimer(Strategy):
    def on_start(self, ctx):  # noqa: ANN001
        ctx.schedule_at(ctx.now())                             # not after now(): ValueError, not caught here


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class FailureTests(unittest.TestCase):
    def _fail(self, strategy, params: dict | None = None):  # noqa: ANN001, ANN202
        g, p = daily_world(5500)
        tmp = tempfile.TemporaryDirectory(prefix="strategies-fail-")
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        pack, b = _materialise(g, params if params is not None else p, folder)
        return folder / "run", lambda: engine_mod.run_bundle(b, [pack], folder / "run", strategy=strategy, report=False)

    def test_a_python_exception_stops_the_run_with_its_traceback(self):
        run, go = self._fail(_Boom(after=3))
        with self.assertRaises(ZeroDivisionError):
            go()
        log = (run / "events_log.txt").read_text(encoding="utf-8").splitlines()
        i = next(n for n, x in enumerate(log) if x.endswith(" INFO about to fail"))
        tail = [x.split(" ", 2) for x in log[i + 1:]]
        self.assertTrue(all(lvl == "ERROR" for _, lvl, _ in tail))
        self.assertEqual(tail[0][2], "the strategy raised an exception; the run stops")
        self.assertEqual(tail[1][2], "Traceback (most recent call last):")
        self.assertIn("on_bar", "\n".join(x[2] for x in tail))
        self.assertEqual(tail[-1][2], "ZeroDivisionError: integer division or modulo by zero")
        self.assertEqual({tail[0][0]}, {x[0] for x in tail})   # all at the failing moment
        self.assertFalse((run / "orders.csv").exists())
        self.assertFalse((run / "manifest.json").exists())

    def test_a_context_error_inside_a_hook_is_not_swallowed(self):
        run, go = self._fail(_BadTimer())
        with self.assertRaisesRegex(ValueError, "schedule_at needs a time after now"):
            go()
        text = (run / "events_log.txt").read_text(encoding="utf-8")
        self.assertIn("ERROR ValueError: schedule_at needs a time after now()", text)

    def test_a_cpp_strategy_error_stops_the_run(self):
        run, go = self._fail("sma_cross_cpp", {"symbol": "NSE:TEST", "fast": 2, "slow": 3, "long_short": True})
        with self.assertRaisesRegex(ValueError, "long_short needs a derivative"):
            go()
        self.assertIn("ERROR the run stops: long_short needs a derivative",
                      (run / "events_log.txt").read_text(encoding="utf-8"))

    def test_a_bad_cpp_param_names_the_key(self):
        run, go = self._fail("sma_cross_cpp", {"symbol": "NSE:TEST", "fast": "two", "slow": 3})
        with self.assertRaisesRegex(RuntimeError, "strategy param 'fast' is not an integer"):
            go()

    # The day-to-day state (eod_state / load_sod_state, VOL 5.3, D-47 closed) is covered by test_engine_sod_state.

    def test_an_unknown_cpp_strategy(self):
        g, p = daily_world(5501)
        with tempfile.TemporaryDirectory(prefix="strategies-fail-") as tmp:
            pack, b = _materialise(g, p, Path(tmp))
            b["strategy"] = {"kind": "cpp", "ref": "nope", "params": {}, "source_sha256": "0" * 64}
            with self.assertRaisesRegex(RuntimeError, r"no C\+\+ strategy is registered as 'nope' \(known: .*orb_cpp, sma_cross_cpp"):
                engine_mod.run_bundle(b, [pack], Path(tmp) / "run", report=False)


def _records(pack: Path) -> list[bytes]:
    data = pack.read_bytes()
    out, off = [], 64
    while off < len(data):
        size = struct.unpack_from("<H", data, off + 2)[0]
        out.append(data[off:off + size])
        off += size
    return out


def _csv_text(v) -> str:  # noqa: ANN001
    return "" if v is None else ("true" if v else "false") if isinstance(v, bool) else str(v)


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class DrainTests(unittest.TestCase):
    """drain_outputs() between live batches adds up to the run's CSVs and log (A2 §3, for M5)."""

    def test_drained_rows_add_up_to_the_artifacts(self):
        mod = engine.load()
        cases = [(daily_world(s), REFS["sma_cross"]) for s in (5600, 5601, 5602)]
        cases += [(minute_world(s), REFS["orb"]) for s in (5603, 5604, 5605)]
        cases += [(straddle_world(s), REFS["straddle"]) for s in (5606, 5607)]
        rng = random.Random(5610)
        for n, ((g, params), ref) in enumerate(cases):
            with self.subTest(case=n), tempfile.TemporaryDirectory(prefix="strategies-drain-") as tmp:
                folder = Path(tmp)
                pack, b = _materialise(g, params, folder)
                oracle_run(b, [pack], folder / "oracle", strategy=_load(ref), report=False)
                e = mod.Engine(str(folder / "oracle" / "bundle.json"))
                e.set_strategy_py(_load(ref))
                got = {"orders": [], "order_events": [], "fills": [], "charges": [], "log": []}
                recs, i = _records(pack), 0
                empty = e.drain_outputs()
                self.assertEqual(empty, {k: [] for k in got})
                while i < len(recs):
                    k = rng.randint(1, 8)
                    e.push(b"".join(recs[i:i + k]))
                    i += k
                    for key, rows in e.drain_outputs().items():
                        got[key] += rows
                e.finish()
                for key, rows in e.drain_outputs().items():
                    got[key] += rows
                self.assertEqual(e.drain_outputs(), {k: [] for k in got})
                e.write_artifacts(str(folder / "engine"))
                del e
                assert_same_artifacts(self, folder / "oracle", folder / "engine")

                def table(name: str) -> list[dict]:
                    with (folder / "engine" / name).open(encoding="utf-8", newline="") as f:
                        return list(csv.DictReader(f))
                for key in ("order_events", "fills", "charges"):
                    self.assertEqual([{c: _csv_text(v) for c, v in r.items()} for r in got[key]], table(f"{key}.csv"), key)
                final = table("orders.csv")
                self.assertEqual([r["order_id"] for r in got["orders"]], [int(r["order_id"]) for r in final])
                fixed = ("order_id", "parent_id", "instrument_id", "side", "product", "type", "validity", "tag",
                         "t_submit_ns")
                self.assertEqual([[_csv_text(r[c]) for c in fixed] for r in got["orders"]],
                                 [[r[c] for c in fixed] for r in final])
                self.assertEqual(got["log"],
                                 (folder / "engine" / "events_log.txt").read_text(encoding="utf-8").splitlines())


if __name__ == "__main__":
    unittest.main()

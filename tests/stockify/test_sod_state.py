"""VOL 5.2 (D-66, A1 §6.4, A4 §1.10): a paper session's start-of-day state, in the oracle.

A run split into one-day bundles, each started from the previous day's `eod_state()`, must write exactly what the
whole run writes. `run_day_by_day` (below) slices the bundle into one-day bundles (`days` = that day, `next_days` =
the days after it, `sod_state` = the previous day's state), slices the pack records by trading day (a record belongs
to the first day whose DAY_END is at or after it; records after the last DAY_END belong to the last day), runs each
day with a fresh strategy object, and joins the per-day artifacts: `orders.csv` by order id (a later day's row wins,
since an order queued overnight appears on both days), every other file by concatenation. `metrics.json` is per day
and is not compared.
"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import bundle as bundle_mod, golden, numerics, runconfig
from stockify.oracle import artifacts, sod_state
from stockify.oracle.runner import Engine, RunError, load_records
from stockify.strategy import Strategy
from stockify.tools import fuzz

FILES = ("orders.csv", "order_events.csv", "fills.csv", "charges.csv", "positions_eod.csv", "equity.csv",
         "margin_daily.csv", "events_log.txt")


# ── Running whole and day by day ─────────────────────────────────────────────

def _run(bundle: dict, records: list, strategy, folder: Path) -> Engine:
    """Run one bundle as run_bundle does (from its canonical bytes) and write the run folder's artifacts."""
    b = json.loads(bundle_mod.dumps(bundle))
    e = Engine(b, records, strategy)
    e.run()
    artifacts.write(folder, e, {}, {})
    return e


def day_bundles(whole: dict) -> list[dict]:
    """One bundle per day of `whole`, without state: that day, and the days after it as next_days."""
    out = []
    for i, day in enumerate(whole["days"]):
        b = copy.deepcopy(whole)
        b["days"] = [copy.deepcopy(day)]
        b["next_days"] = [{k: v for k, v in d.items() if k != "seed"} for d in whole["days"][i + 1:]]
        out.append(b)
    return out


def slice_records(records: list, days: list[dict]) -> list[list]:
    parts: list[list] = [[] for _ in days]
    ends = [d["day_end_ns"] for d in days]
    for key, rec in records:
        i = next((n for n, end in enumerate(ends) if key[0] <= end), len(days) - 1)
        parts[i].append((key, rec))
    return parts


def _join(folders: list[Path], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        texts = [(f / name).read_text(encoding="utf-8") for f in folders]
        if name == "events_log.txt":
            (out / name).write_bytes("".join(texts).encode("utf-8"))
            continue
        header = texts[0].splitlines()[0]
        rows = [line for t in texts for line in t.splitlines()[1:]]
        if name == "orders.csv":
            by_id: dict[int, str] = {}
            for line in rows:
                by_id[int(line.split(",", 1)[0])] = line
            rows = [by_id[k] for k in sorted(by_id)]
        (out / name).write_bytes(("\n".join([header] + rows) + "\n").encode("utf-8"))


def run_whole(bundle: dict, records: list, make_strategy, folder: Path) -> Engine:
    return _run(bundle, records, make_strategy(), folder / "whole")


def run_day_by_day(bundle: dict, records: list, make_strategy, folder: Path) -> list[str]:
    """Day by day from each previous day's eod_state; the joined artifacts go to folder/parts. Returns the states."""
    state = None
    folders, states = [], []
    parts = slice_records(records, bundle["days"])
    for i, b in enumerate(day_bundles(bundle)):
        b["sod_state"] = json.loads(state) if state is not None else None
        e = _run(b, parts[i], make_strategy(), folder / f"day{i}")
        state = e.eod_state()
        states.append(state)
        folders.append(folder / f"day{i}")
    _join(folders, folder / "parts")
    return states


def first_difference(a: Path, b: Path) -> str | None:
    for name in FILES:
        x, y = (a / name).read_bytes(), (b / name).read_bytes()
        if x != y:
            la, lb = x.decode().splitlines(), y.decode().splitlines()
            for n in range(max(len(la), len(lb))):
                p = la[n] if n < len(la) else "<end>"
                q = lb[n] if n < len(lb) else "<end>"
                if p != q:
                    return f"{name} line {n + 1}\n  whole: {p}\n  parts: {q}"
    return None


def _world_inputs(world: dict, folder: Path) -> tuple[dict, list]:
    return golden.build_bundle(world), load_records([golden.build_pack(world, folder)])


# ── Strategies ───────────────────────────────────────────────────────────────

class IndicatorTrader(Strategy):
    """Trades one future and one stock on ema(50) / rsi(14) / atr(14) over the full bar history, logging every value
    on every bar; a periodic timer (it fires on the weekend too) and a one-shot Saturday timer draw ctx.rng();
    orders placed in on_session_end queue for the next trading day."""

    def __init__(self, fut: int, eq: int, isec: int, saturday_ns: int):
        self.fut, self.eq, self.isec, self.saturday_ns = fut, eq, isec, saturday_ns
        self.bars_seen = 0
        self.timer_hits = 0

    def on_start(self, ctx):  # noqa: ANN001
        for iid in (self.fut, self.eq):
            ctx.subscribe_bars(iid, self.isec)
        ctx.subscribe_ticks(self.fut)
        ctx.schedule_every(7 * 3600 * 10**9, ctx.now() + 3600 * 10**9)
        ctx.schedule_at(self.saturday_ns)
        ctx.log("info", "on_start")

    def on_bar(self, ctx, bar):  # noqa: ANN001
        self.bars_seen += 1
        e, r, a = ctx.ema(bar.instrument, self.isec, 50), ctx.rsi(bar.instrument, self.isec, 14), \
            ctx.atr(bar.instrument, self.isec, 14)
        ctx.log("info", f"ind {bar.instrument} {bar.start_ns} ema={e!r} rsi={r!r} atr={a!r} n={self.bars_seen}")
        if e != e or r != r:                         # NaN until the lookback is full
            return
        pos = ctx.position(bar.instrument, "NRML" if bar.instrument == self.fut else "CNC")
        held = pos.qty if pos else 0
        lot = (ctx.spec(bar.instrument) or {"lot": 1})["lot"]
        if bar.close > e and r < 70 and held <= 0:
            ctx.place(bar.instrument, "BUY", lot, "NRML" if bar.instrument == self.fut else "CNC", "MARKET")
        elif bar.close < e and r > 30 and held > 0:
            ctx.place(bar.instrument, "SELL", held, "NRML" if bar.instrument == self.fut else "CNC", "LIMIT",
                      price=(bar.close - int(a)) // 10 * 10 if a == a else bar.close)

    def on_timer(self, ctx, timer_id):  # noqa: ANN001
        self.timer_hits += 1
        ctx.log("info", f"timer {timer_id} draw {ctx.rng().below(1000)} hits={self.timer_hits}")

    def on_fill(self, ctx, fill):  # noqa: ANN001
        ctx.log("info", f"fill {fill!r}")

    def on_session_end(self, ctx):  # noqa: ANN001
        ctx.log("info", f"end {ctx.funds()!r} {ctx.positions()!r}")
        ctx.place(self.eq, "BUY", 1, "CNC", "LIMIT", price=200_000)       # queued for the next day's open

    def save_state(self) -> dict:
        return {"bars_seen": self.bars_seen, "timer_hits": self.timer_hits}

    def load_state(self, state: dict) -> None:
        self.bars_seen, self.timer_hits = state["bars_seen"], state["timer_hits"]


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


def long_history_world() -> tuple[dict, int]:
    """fuzz.long_history_world(): 3 trading days (Fri, Mon, Tue) of 2,000 bars each (10-second bars: 2,000 one-minute
    bars do not fit in one session) for a stock future and its stock; returns the world and Saturday 10:00 IST."""
    world = fuzz.long_history_world()
    sat = golden.ts("2026-03-07T10:00:00+05:30")
    return world, sat


# ── Tests ────────────────────────────────────────────────────────────────────

class SodStateTests(unittest.TestCase):
    def test_split_equals_whole_on_fuzz_worlds(self):
        compared = 0
        for seed in range(4000, 4040):
            world = fuzz.fuzz_world(seed)
            world["run"]["seed_mode"] = "per_day"
            ids = [i["id"] for i in world["bundle"]["instruments"]]

            def make(ids=ids, p=world["fuzz"]["p_act"]):  # noqa: ANN001, ANN202
                return fuzz.RandomTrader(ids, p)
            with tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                bundle, records = _world_inputs(world, folder)
                try:
                    run_whole(bundle, records, make, folder)
                except Exception as e:  # noqa: BLE001 — then the split run must fail alike
                    with self.assertRaises(type(e), msg=seed) as ctx:
                        run_day_by_day(bundle, records, make, folder)
                    self.assertEqual(str(ctx.exception), str(e), seed)
                    continue
                run_day_by_day(bundle, records, make, folder)
                self.assertIsNone(first_difference(folder / "whole", folder / "parts"), seed)
                compared += 1
        self.assertGreater(compared, 30)

    def test_long_history_ema_strategy_split_equals_whole(self):
        world, sat = long_history_world()
        self.assertEqual(world["run"]["seed_mode"], "per_day")

        def make():  # noqa: ANN202
            return IndicatorTrader(2, 1, 10, sat)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            bundle, records = _world_inputs(world, folder)
            run_whole(bundle, records, make, folder)
            states = run_day_by_day(bundle, records, make, folder)
            self.assertIsNone(first_difference(folder / "whole", folder / "parts"))
            log = (folder / "whole" / "events_log.txt").read_text(encoding="utf-8")
            self.assertEqual(log.count(" ind "), 2 * 3 * 2000)        # every bar's indicators, equal in both
            self.assertIn(f"{sat} INFO timer 2 draw", log)            # the Saturday one-shot timer fired
            fills = (folder / "whole" / "fills.csv").read_text(encoding="utf-8").splitlines()
            self.assertGreater(len(fills), 10)
            day1 = json.loads(states[0])
            self.assertEqual(len(day1["bar_history"]["2:10"]), 2000)  # the full history, never trimmed
            self.assertEqual(len(json.loads(states[2])["bar_history"]["2:10"]), 6000)
            self.assertTrue(any(p["q"] for p in day1["ledger"]["positions"]))   # positions carried overnight
            self.assertTrue(day1["pending_orders"])                   # the on_session_end order, queued

    def test_on_start_runs_only_on_the_first_day(self):
        world = fuzz.fuzz_world(4001)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            bundle, records = _world_inputs(world, folder)
            Probe.calls = []
            states = run_day_by_day(bundle, records, Probe, folder)
            self.assertEqual(Probe.calls, ["on_start", "load_state {'n': 7}", "load_state {'n': 7}"])
            for text in states:                                       # restored timers are never duplicated
                self.assertEqual([t[0] for t in json.loads(text)["timers"]], [1])

    def test_state_round_trip_is_byte_identical(self):
        world, sat = long_history_world()
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            bundle, records = _world_inputs(world, folder)
            b0, b1 = day_bundles(bundle)[:2]
            e0 = _run(b0, slice_records(records, bundle["days"])[0], IndicatorTrader(2, 1, 10, sat), folder / "d0")
            text = e0.eod_state()
            self.assertEqual(text, numerics.dumps_canonical(json.loads(text)))   # canonical JSON
            st = json.loads(text)
            self.assertEqual(st["schema"], sod_state.SCHEMA)
            self.assertEqual(st["trade_date"], bundle["days"][0]["date"])
            for key in sod_state.KEYS:
                self.assertIn(key, st)
            e1 = Engine(json.loads(bundle_mod.dumps(b1)), [], IndicatorTrader(2, 1, 10, sat))
            e1.load_sod_state(text)
            self.assertEqual(e1.eod_state(), text)

    def test_state_holds_no_floats_outside_the_strategy(self):
        world, sat = long_history_world()
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            bundle, records = _world_inputs(world, folder)
            b0 = day_bundles(bundle)[0]
            e0 = _run(b0, slice_records(records, bundle["days"])[0], IndicatorTrader(2, 1, 10, sat), folder / "d0")
            st = json.loads(e0.eod_state())
            st.pop("strategy")

            def walk(v):  # noqa: ANN001, ANN202
                self.assertNotIsInstance(v, float)
                for x in (v.values() if isinstance(v, dict) else v if isinstance(v, list) else []):
                    walk(x)
            walk(st)

    def test_constructor_applies_bundle_sod_state(self):
        world = fuzz.fuzz_world(4003)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            bundle, records = _world_inputs(world, folder)
            b0, b1 = day_bundles(bundle)[:2]
            ids = [i["id"] for i in world["bundle"]["instruments"]]
            text = _run(b0, slice_records(records, bundle["days"])[0], fuzz.RandomTrader(ids, 40), folder / "d0").eod_state()
            b1["sod_state"] = json.loads(text)
            e1 = Engine(json.loads(bundle_mod.dumps(b1)), [], fuzz.RandomTrader(ids, 40))
            self.assertEqual(e1.eod_state(), text)
            with self.assertRaisesRegex(RunError, "already"):
                e1.load_sod_state(text)

    def test_load_after_first_event_raises(self):
        world = fuzz.fuzz_world(4003)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            bundle, records = _world_inputs(world, folder)
            b0, b1 = day_bundles(bundle)[:2]
            text = _run(b0, slice_records(records, bundle["days"])[0], Strategy(), folder / "d0").eod_state()
            e = Engine(json.loads(bundle_mod.dumps(b1)), [], Strategy())
            e.run()
            with self.assertRaisesRegex(RunError, "before the first event"):
                e.load_sod_state(text)
            st = json.loads(text)
            del st["timers"]
            with self.assertRaisesRegex(RunError, "timers"):
                Engine(json.loads(bundle_mod.dumps(b1)), [], Strategy()).load_sod_state(numerics.dumps_canonical(st))
            st = {**json.loads(text), "schema": "stockify.sod_state/0"}
            with self.assertRaisesRegex(RunError, "schema"):
                Engine(json.loads(bundle_mod.dumps(b1)), [], Strategy()).load_sod_state(numerics.dumps_canonical(st))

    def test_size_warning_past_threshold(self):
        self.assertIsNone(sod_state.size_warning("x" * 100, 100))
        msg = sod_state.size_warning("x" * 101, 100)
        self.assertIn("101", msg)
        self.assertEqual(sod_state.warn_bytes(), 64 * 1024 * 1024)        # paper.json sod_state_warn_mb

    def test_bar_row_size_matches_the_a1_note(self):
        row = numerics.dumps_canonical([1772770500000000000, 2512340, 2513450, 2511120, 2512890, 123450, 4567800])
        self.assertLessEqual(len(row) + 1, 70)                     # with its comma: one row ≈ 70 bytes

    def test_seed_mode_helper(self):
        self.assertEqual(runconfig.seed_mode({}), "run")
        self.assertEqual(runconfig.seed_mode({"seed_mode": "per_day"}), "per_day")


if __name__ == "__main__":
    unittest.main()

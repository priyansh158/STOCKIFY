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
from unittest import mock

import tests.support  # noqa: F401  — must precede project imports

from stockify import bundle as bundle_mod, engine as engine_mod, golden, numerics, runconfig
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
        b["prev_days"] = [{k: v for k, v in d.items() if k != "seed"} for d in whole["days"][:i]]
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

class SplitCase(unittest.TestCase):
    def assert_split_equals_whole(self, bundle: dict, records: list, make, folder: Path, msg=None) -> bool:  # noqa: ANN001
        """Split equals whole, or both fail with the same error; True when artifacts were compared."""
        try:
            run_whole(bundle, records, make, folder)
        except Exception as e:  # noqa: BLE001 — then the split run must fail alike
            with self.assertRaises(type(e), msg=msg) as ctx:
                run_day_by_day(bundle, records, make, folder)
            self.assertEqual(str(ctx.exception), str(e), msg)
            return False
        run_day_by_day(bundle, records, make, folder)
        self.assertIsNone(first_difference(folder / "whole", folder / "parts"), msg)
        return True



class SodStateTests(SplitCase):
    def _fuzz_split(self, seed_mode: str) -> None:
        compared = 0
        for seed in range(4000, 4040):
            world = fuzz.fuzz_world(seed)
            world["run"]["seed_mode"] = seed_mode
            ids = [i["id"] for i in world["bundle"]["instruments"]]

            def make(ids=ids, p=world["fuzz"]["p_act"]):  # noqa: ANN001, ANN202
                return fuzz.RandomTrader(ids, p)
            with tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                bundle, records = _world_inputs(world, folder)
                compared += self.assert_split_equals_whole(bundle, records, make, folder, (seed, seed_mode))
        self.assertGreater(compared, 30)

    def test_split_equals_whole_on_fuzz_worlds(self):
        self._fuzz_split("per_day")

    def test_split_equals_whole_on_fuzz_worlds_with_seed_mode_run(self):
        # one stream position for the whole run: every world needs the rng carry (the reviewer's 40/40)
        self._fuzz_split("run")

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


# ── Fix round 1: shipped strategies, strategy state checks, carried parts proved by worlds ───────────────────────

def _drop_on_restore(key: str, empty):  # noqa: ANN001, ANN202
    """Patch sod_state.restore so the loaded state has `key` emptied (a mutation: split must then differ)."""
    orig = sod_state.restore

    def restore(engine, state):  # noqa: ANN001, ANN202
        return orig(engine, {**state, key: copy.deepcopy(empty)})
    return mock.patch.object(sod_state, "restore", restore)


def _flat_margin_without_next_days():  # noqa: ANN202
    from stockify.oracle import margin
    orig = margin.FlatMargin.__init__

    def init(self, bundle):  # noqa: ANN001, ANN202
        orig(self, {**bundle, "next_days": []})
    return mock.patch.object(margin.FlatMargin, "__init__", init)


def _tick(iid: int, at: str, ltp: int, volume: int) -> dict:
    return {"kind": "TICK_QUOTE", "instrument": iid, "t": at, "ltp": ltp, "volume": volume, "atp": ltp,
            "high": ltp + 100, "low": ltp - 100, "open": ltp}


def _eq(iid: int = 1, sym: str = "TEST") -> dict:
    return {"id": iid, "kind": "EQ", "tradingsymbol": sym, "specs": [{"from": "2015-01-01", "lot": 1, "tick_paise": 5}]}


def delivery_window_world() -> dict:
    """D-60 across a split: a long ITM stock call (physically settled) bought on Friday and held into its expiry on
    Tuesday, with the shipped Z-4 schedule. Friday is E-2 and Monday E-1, which only next_days lets a one-day bundle
    count."""
    days = ["2026-03-06", "2026-03-09", "2026-03-10"]
    inst = [_eq(1, "AAA"),
            {"id": 2, "kind": "CE", "tradingsymbol": "AAACE", "underlying": "AAA", "underlying_id": 1,
             "expiry": days[2], "strike_paise": 240_000, "underlying_class": "STOCK", "settlement": "physical",
             "specs": [{"from": "2015-01-01", "lot": 250, "tick_paise": 5}]}]
    evs = []
    for d in days:
        for n, hhmm in enumerate(("09:15", "09:16", "09:17", "09:18")):
            evs.append(_tsbar(1, d, hhmm, 250_000 + 100 * n))
            evs.append(_tsbar(2, d, hhmm, 12_000 + 50 * n))
    evs.append({"kind": "SETTLE", "instrument": 1, "t": f"{days[2]}T16:05:00+05:30", "date": days[2],
                "price": 250_300, "subtype": "final"})
    actions = [{"at_ns": f"{days[0]}T09:16:30+05:30",
                "place": {"instrument": 2, "side": "BUY", "qty": 250, "product": "NRML", "type": "LIMIT",
                          "price": 13_000}}]
    return {"id": "D60SPLIT", "bundle": {"dates": days, "instruments": inst, "delivery_schedule": True},
            "run": {"fill_model": {"kind": "bar", "participation": "1"}, "capital_paise": 10**9, "price_basis": "raw"},
            "events": evs, "strategy": {"actions": actions}}


def _tsbar(iid: int, d: str, hhmm: str, c: int, v: int = 100_000) -> dict:
    return {"kind": "BAR", "instrument": iid, "interval_s": 60, "start": f"{d}T{hhmm}:00+05:30", "o": c, "h": c + 50,
            "l": c - 50, "c": c, "v": v}


def dp_world() -> dict:
    """dp_done across a split: CNC shares bought on Monday from ticks (snapshot_ltp); on Tuesday a small sell (B)
    completes and takes the day's DP charge, a large one (A) fills only in part. The stock also has daily bars
    (published at the next midnight), so A rests past Tuesday's close (D-44) and is cancelled by Tuesday's daily bar at
    Wednesday 00:00, in Wednesday's part: its charge, dated Tuesday, must not take a second DP charge."""
    days = ["2026-03-02", "2026-03-03", "2026-03-04"]
    evs = []
    for d in days:
        evs.append({"kind": "BAR", "instrument": 1, "interval_s": 86400, "start": f"{d}T00:00:00+05:30",
                    "o": 10_000, "h": 10_100, "l": 9_900, "c": 10_000, "v": 1_000_000})
        for n in range(30):
            mm = 20 + n
            evs.append(_tick(1, f"{d}T09:{mm:02d}:00+05:30", 10_000, 100 * (n + 1)))
    actions = [{"at_ns": f"{days[0]}T09:22:30+05:30",
                "place": {"instrument": 1, "side": "BUY", "qty": 100, "product": "CNC", "type": "MARKET"}},
               {"at_ns": f"{days[1]}T09:44:30+05:30",
                "place": {"instrument": 1, "side": "SELL", "qty": 10, "product": "CNC", "type": "MARKET"}},
               {"at_ns": f"{days[1]}T09:44:31+05:30",
                "place": {"instrument": 1, "side": "SELL", "qty": 90, "product": "CNC", "type": "LIMIT",
                          "price": 9_000}}]
    return {"id": "DPSPLIT", "bundle": {"dates": days, "instruments": [_eq()], "charges": fuzz._real_charges()},
            "run": {"fill_model": {"kind": "snapshot_ltp", "participation": "0.10"}, "capital_paise": 10**8,
                    "price_basis": "raw", "costs": {"allow_unverified": True, "dp_charges": True}},
            "events": evs, "strategy": {"actions": actions}}


def fsp_world() -> dict:
    """fsp_seen across a split: eod_close fills on daily bars published at the next midnight. A call sold on its
    expiry day (Tuesday) fills at Tuesday's close, which arrives at Wednesday 00:00 (Wednesday's part); the FSP came
    at Tuesday 16:00 (Tuesday's part), and the fill settles at it."""
    days = ["2026-03-02", "2026-03-03", "2026-03-04"]
    inst = [{"id": 1, "kind": "INDEX", "tradingsymbol": "NIFTY 50"},
            {"id": 2, "kind": "CE", "tradingsymbol": "NC", "underlying": "NIFTY", "underlying_id": 1,
             "expiry": days[1], "strike_paise": 2_400_000, "specs": [{"from": "2015-01-01", "lot": 65, "tick_paise": 5}]}]
    evs = []
    for d, spot, ce in zip(days, (2_400_000, 2_402_000, 2_410_000), (10_000, 9_000, 9_500)):
        for iid, c in ((1, spot), (2, ce)):
            evs.append({"kind": "BAR", "instrument": iid, "interval_s": 86400, "start": f"{d}T00:00:00+05:30",
                        "o": c, "h": c, "l": c, "c": c, "v": 100_000})
    evs.append({"kind": "SETTLE", "instrument": 1, "t": f"{days[1]}T16:00:00+05:30", "date": days[1],
                "price": 2_405_000, "subtype": "final"})
    actions = [{"at_ns": f"{days[1]}T08:00:00+05:30",
                "place": {"instrument": 2, "side": "SELL", "qty": 65, "product": "NRML", "type": "MARKET",
                          "protection": 5000}}]
    return {"id": "FSPSPLIT", "bundle": {"dates": days, "instruments": inst},
            "run": {"fill_model": {"kind": "eod_close", "half_spread_ticks": 1}, "capital_paise": 10**8,
                    "price_basis": "raw"},
            "events": evs, "strategy": {"actions": actions}}


def _script(bundle: dict):  # noqa: ANN202
    from stockify.strategies._script import Script
    return lambda: Script(copy.deepcopy(bundle["strategy"]["actions"]))


def _shipped_worlds() -> dict:
    """Every catalogue strategy with a multi-day world and params: ref -> (world, params, bundle builder)."""
    from tests.stockify import test_strategies as TS
    from tests.stockify.test_chain_view import EXPIRY, chain_bundle, chain_world
    closes = [10000, 10000, 10000, 10100, 10200, 10300, 10150, 10000, 9900, 9850]
    daily = TS.world(TS.walk(closes), dates=TS.DAYS)
    orb_days = TS.DAYS[:3]
    orb = []
    for d in orb_days:
        orb += [TS.minute(d, "09:15", 10000, 10050, 9950, 10000), TS.minute(d, "09:16", 10000, 10060, 9980, 10040),
                TS.minute(d, "09:17", 10040, 10120, 10040, 10100), TS.minute(d, "09:18", 10100, 10150, 10090, 10140),
                TS.minute(d, "15:10", 10200, 10210, 10190, 10200), TS.minute(d, "15:11", 10200, 10210, 10190, 10200)]
    orb_world = TS.world(orb, dates=orb_days, mis_cutoff={"CM": "15:20"})
    eod_days = ["2026-03-02", "2026-03-03", "2026-03-04"]
    inst = [{"id": 1, "kind": "INDEX", "tradingsymbol": "NIFTY 50"},
            {"id": 2, "kind": "CE", "tradingsymbol": "NC", "underlying": "NIFTY", "underlying_id": 1,
             "expiry": "2026-03-04", "strike_paise": 2400000, "specs": [{"from": "2026-01-01", "lot": 65, "tick_paise": 5}]},
            {"id": 3, "kind": "PE", "tradingsymbol": "NP", "underlying": "NIFTY", "underlying_id": 1,
             "expiry": "2026-03-04", "strike_paise": 2400000, "specs": [{"from": "2026-01-01", "lot": 65, "tick_paise": 5}]}]
    eod = []
    for day, spot, ce, pe in zip(eod_days, (2400000, 2402000, 2410000), (10000, 9000, 10000), (10000, 8000, 0)):
        for iid, c in ((2, ce), (3, pe), (1, spot)):
            eod.append({"kind": "BAR", "instrument": iid, "interval_s": 86400, "start": f"{day}T00:00:00+05:30",
                        "t": f"{day}T19:00:00+05:30", "o": c, "h": c, "l": c, "c": c, "v": 100_000})
    eod.append({"kind": "SETTLE", "instrument": 1, "t": "2026-03-04T16:00:00+05:30", "date": "2026-03-04",
                "price": 2410000, "subtype": "final"})
    eod_world = TS.world(eod, instruments=inst, dates=eod_days,
                         run={"fill_model": {"kind": "eod_close", "half_spread_ticks": 1}, "capital_paise": 100_000_000})
    chain = chain_world(7, with_future=True)                # two sessions: 2026-08-03 and its copy on the expiry day
    day = 86_400 * 1_000_000_000
    chain["events"] += [{**copy.deepcopy(e), "t": golden.ts(e["t"]) + day} for e in chain["events"]]
    chain["bundle"]["dates"] = ["2026-08-03", EXPIRY]
    chain["run"]["capital_paise"] = 10**9
    opt = {"entry_hhmm": "09:20", "exit_hhmm": "10:05", "stop_pct": "0.05"}
    return {
        "stockify.strategies.buy_and_hold:BuyAndHold": (daily, {"symbol": "NSE:TEST"}, golden.build_bundle),
        "stockify.strategies.sma_cross:SmaCross": (daily, {"symbol": "NSE:TEST", "fast": 2, "slow": 3},
                                                   golden.build_bundle),
        "stockify.strategies.rsi_pullback:RsiPullback": (
            TS.world(TS.walk([10000, 9900, 9800, 9700, 9800, 9900, 10000, 10100, 10000, 9900]), dates=TS.DAYS),
            {"symbol": "NSE:TEST", "rsi_n": 2}, golden.build_bundle),
        **{f"stockify.strategies.{m}": (daily, {"symbol": "NSE:TEST", **pr}, golden.build_bundle) for m, pr in (
            ("macd_cross:MacdCross", {"fast": 2, "slow": 3, "signal": 2}),
            ("bollinger_reversion:BollingerReversion", {"n": 3, "k": "0.5"}),
            ("donchian_breakout:DonchianBreakout", {"entry_n": 2, "exit_n": 2}),
            ("supertrend:Supertrend", {"n": 2, "mult": "1.0"}),
            ("ema_trend:EmaTrend", {"fast": 2, "slow": 3}),
            ("chandelier_exit:ChandelierExit", {"n": 3, "mult": "1.0", "entry_n": 2}))},
        "stockify.strategies.opening_range_breakout:OpeningRangeBreakout": (
            orb_world, {"symbol": "NSE:TEST", "range_minutes": 2, "qty": 10}, golden.build_bundle),
        "stockify.strategies.eod_short_straddle:EodShortStraddle": (eod_world, {"min_days": 1}, golden.build_bundle),
        "stockify.strategies.short_straddle_stop:ShortStraddleStop": (chain, dict(opt), chain_bundle),
        "stockify.strategies.iron_fly:IronFly": (chain, {**opt, "wing_strikes": 1}, chain_bundle),
        # the comparison wrapper (not in the catalogue) around a catalogue strategy
        "stockify.strategies._margin_watch:MarginWatch": (
            daily, {"inner_ref": "stockify.strategies.sma_cross:SmaCross", "symbol": "NSE:TEST", "fast": 2, "slow": 3},
            golden.build_bundle),
    }


class StrategyStateTests(SplitCase):
    """I-1 / I-2: every shipped strategy carries its own state; strategy states are checked at day end."""

    def test_every_shipped_strategy_split_equals_whole(self):
        from stockify import strategy as S
        from stockify.strategies import _catalogue
        worlds = _shipped_worlds()
        refs = {s["ref"] for s in _catalogue.python_strategies()}
        self.assertLessEqual(refs, set(worlds), "a catalogue strategy has no split test world")
        for ref, (world, params, build) in worlds.items():
            with tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                bundle = build(world)
                bundle["strategy"]["params"] = params
                records = load_records([golden.build_pack(world, folder)])
                self.assertTrue(self.assert_split_equals_whole(bundle, records, lambda r=ref: S.load(r), folder, ref),
                                ref)
                fills = (folder / "whole" / "fills.csv").read_text(encoding="utf-8").splitlines()
                self.assertGreater(len(fills), 1, f"{ref} made no trade: the world proves nothing")

    def test_script_goldens_split_equals_whole(self):
        multi = [(p, g) for p, g in golden.goldens() if len(g["bundle"]["dates"]) > 1 and not g["strategy"].get("ref")]
        self.assertGreaterEqual(len(multi), 4)
        for _, g in multi:
            with tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                bundle, records = _world_inputs(g, folder)
                self.assert_split_equals_whole(bundle, records, _script(bundle), folder, g["id"])

    def test_default_state_round_trips_decimal_and_tuple(self):
        from decimal import Decimal

        from stockify import strategy as S

        class Mine(S.Strategy):
            pass
        a = Mine()
        a.share, a.last, a.legs, a.cfg, a.none = Decimal("0.95"), (1, "x", (2, Decimal("1E+2"))), [1, 2], {"k": [True]}, None
        st = a.save_state()
        text = numerics.dumps_canonical(st)
        b = Mine()
        b.load_state(json.loads(text))
        self.assertEqual(vars(b), vars(a))
        self.assertIsInstance(b.last, tuple)
        self.assertIsInstance(b.last[2], tuple)
        self.assertEqual(str(b.last[2][1]), "1E+2")
        self.assertEqual(list(st), sorted(st))                     # sorted by attribute name

    def test_default_state_refuses_other_types_naming_class_and_attribute(self):
        from datetime import date

        from stockify import strategy as S

        class Mine(S.Strategy):
            pass
        for value, attr in ((date(2026, 1, 1), "when"), ({3: "x"}, "by_id"), (1.5, "ratio"), ({"$decimal": "1"}, "raw")):
            m = Mine()
            setattr(m, attr, value)
            with self.assertRaises(S.StrategyStateError) as ctx:
                m.save_state()
            self.assertIn(f"Mine.{attr}", str(ctx.exception))
            self.assertIn("save_state/load_state", str(ctx.exception))

    def _one_day(self, strategy, folder: Path) -> Engine:
        world = fuzz.fuzz_world(4003)
        bundle, records = _world_inputs(world, folder)
        b0 = day_bundles(bundle)[0]
        e = Engine(json.loads(bundle_mod.dumps(b0)), slice_records(records, bundle["days"])[0], strategy)
        return e

    def test_bad_strategy_state_fails_at_day_end_naming_the_strategy(self):
        class NanState(Strategy):
            def save_state(self) -> dict:
                return {"ema": float("nan")}

        class TupleState(Strategy):
            def save_state(self) -> dict:
                return {"last": (1, 2)}

        class IntKeys(Strategy):
            def save_state(self) -> dict:
                return {"by_id": {3: 1}}

        class Raises(Strategy):
            def save_state(self) -> dict:
                raise KeyError("placed")
        for cls, words in ((NanState, "NaN"), (TupleState, "tuple"), (IntKeys, "keys must be strings"),
                           (Raises, "save_state() raised KeyError")):
            with tempfile.TemporaryDirectory() as tmp:
                e = self._one_day(cls(), Path(tmp))
                with self.assertRaises(RunError) as ctx:
                    e.run()                                         # at DAY_END, not later at finalisation
                self.assertIn(cls.__name__, str(ctx.exception))
                self.assertIn(words, str(ctx.exception))

    def test_load_state_errors_name_the_strategy(self):
        class Fragile(Strategy):
            def load_state(self, state: dict) -> None:
                state["missing"]
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            world = fuzz.fuzz_world(4003)
            bundle, records = _world_inputs(world, folder)
            b0, b1 = day_bundles(bundle)[:2]
            text = _run(b0, slice_records(records, bundle["days"])[0], Strategy(), folder / "d0").eod_state()
            with self.assertRaisesRegex(RunError, r"Fragile\.load_state\(\) raised KeyError: 'missing'"):
                Engine(json.loads(bundle_mod.dumps(b1)), [], Fragile()).load_sod_state(text)

    def test_unknown_state_keys_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            world = fuzz.fuzz_world(4003)
            bundle, records = _world_inputs(world, folder)
            b0, b1 = day_bundles(bundle)[:2]
            st = json.loads(_run(b0, slice_records(records, bundle["days"])[0], Strategy(), folder / "d0").eod_state())
            for bad in ({**st, "marks": {}}, {**st, "counters": {**st["counters"], "event_no": 0}}):
                with self.assertRaisesRegex(RunError, "unknown keys"):
                    Engine(json.loads(bundle_mod.dumps(b1)), [], Strategy()).load_sod_state(numerics.dumps_canonical(bad))

    def test_next_days_are_validated(self):
        world = fuzz.fuzz_world(4003)
        whole = golden.build_bundle(world)
        b0 = day_bundles(whole)[0]
        for bad, words in ((list(reversed(b0["next_days"])), "is not after"), ([b0["days"][0]], "is not after"),
                           ([{**b0["next_days"][0], "seed": 1}], "seed"), ({"x": 1}, "must be a list"),
                           ([{"date": "2099-01-01"}], "day object")):
            with self.assertRaisesRegex(RunError, words):
                Engine(json.loads(bundle_mod.dumps({**b0, "next_days": bad})), [], Strategy())

    @unittest.skipUnless(engine_mod.available(), "stockify._engine is not built")
    def test_cpp_engine_refuses_a_bad_sod_state_before_running(self):
        # VOL 5.3: the C++ engine runs day bundles (tests.stockify.test_engine_sod_state); a bundle whose sod_state is
        # not a complete state is refused by its constructor, as the oracle's is
        from stockify import engine as cpp
        world = fuzz.fuzz_world(4003)
        whole = golden.build_bundle(world)
        for b in ({**whole, "sod_state": {"schema": sod_state.SCHEMA}}, {**whole, "sod_state": 3}):
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "run"
                with self.assertRaisesRegex(RuntimeError, "sod_state"):
                    cpp.run_bundle(b, [], out, strategy=Strategy(), report=False)
                self.assertFalse((out / "orders.csv").exists())


class CarriedPartsTests(SplitCase):
    """Each carried part that only some worlds need has a world that breaks without it (M-1, I-3)."""

    def _split(self, world: dict, make=None) -> Path:  # noqa: ANN001
        folder = Path(tempfile.mkdtemp(dir=str(tests.support.SANDBOX_ROOT)))
        bundle, records = _world_inputs(world, folder)
        make = make or _script(bundle)
        self.assertTrue(self.assert_split_equals_whole(bundle, records, make, folder, world["id"]))
        return folder

    def _split_differs(self, world: dict) -> str | None:
        folder = Path(tempfile.mkdtemp(dir=str(tests.support.SANDBOX_ROOT)))
        bundle, records = _world_inputs(world, folder)
        run_whole(bundle, records, _script(bundle), folder)
        run_day_by_day(bundle, records, _script(bundle), folder)
        return first_difference(folder / "whole", folder / "parts")

    def test_delivery_margin_window_crosses_a_split(self):
        world = delivery_window_world()
        folder = self._split(world)
        eq = (folder / "whole" / "equity.csv").read_text(encoding="utf-8").splitlines()[1:]
        self.assertTrue(all(int(r.split(",")[3]) > 0 for r in eq[:2]), eq)   # E-2, E-1: Z-4 margin blocked
        with _flat_margin_without_next_days():
            diff = self._split_differs(world)
        self.assertIsNotNone(diff)
        self.assertIn("equity.csv", diff)

    def test_dp_done_carries_a_days_dp_charge_across_a_split(self):
        world = dp_world()
        folder = self._split(world)
        charges = (folder / "whole" / "charges.csv").read_text(encoding="utf-8").splitlines()[1:]
        orders = (folder / "whole" / "orders.csv").read_text(encoding="utf-8").splitlines()[1:]
        self.assertEqual(sum(1 for r in charges if r.split(",")[1] == "dp"), 1, charges)
        self.assertIn("CANCELLED", orders[2])                      # A: part filled, cancelled by the daily bar
        self.assertNotEqual(orders[2].split(",")[15], "0")
        with _drop_on_restore("dp_done", []):
            diff = self._split_differs(world)
        self.assertIsNotNone(diff)
        self.assertIn("charges.csv", diff)

    def test_prev_days_give_a_midnight_daily_bar_its_session(self):
        # a daily bar dated Monday arrives at Tuesday 00:00, in Tuesday's part: matching needs Monday's CONTINUOUS
        # start, which only prev_days give a one-day bundle (the sold call otherwise never fills)
        world = fsp_world()
        self._split(world)
        orig = day_bundles

        def without_prev(whole):  # noqa: ANN001, ANN202
            out = orig(whole)
            for b in out:
                b["prev_days"] = []
            return out
        with mock.patch(f"{__name__}.day_bundles", without_prev):
            diff = self._split_differs(world)
        self.assertIsNotNone(diff)

    def test_fsp_seen_settles_a_fill_after_midnight(self):
        world = fsp_world()
        folder = self._split(world)
        fills = (folder / "whole" / "fills.csv").read_text(encoding="utf-8").splitlines()[1:]
        self.assertEqual(len(fills), 1, fills)
        pos = (folder / "whole" / "positions_eod.csv").read_text(encoding="utf-8").splitlines()[1:]
        self.assertFalse([r for r in pos if r.split(",")[1] == "2" and r.split(",")[3] != "0"], pos)   # settled
        with _drop_on_restore("fsp_seen", []):
            diff = self._split_differs(world)
        self.assertIsNotNone(diff)


if __name__ == "__main__":
    unittest.main()

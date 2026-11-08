"""Differential fuzzing of the oracle against the engine (A3 P-04, M3.12): worlds, the shrinker, saved goldens.

    python -m stockify tools fuzz --seeds 4000-8999 [--shrink] [--save]

is the weekly 5,000-seed job (A3; run by hand, there is no scheduler or CI, D-26). The regular test run uses
seeds 4000–4199 (`tests/stockify/test_engine_fuzz.py`, which imports the world and the trader from here).

* `fuzz_world(seed)`: a random three-day world in the golden format (bars, daily bars or ticks; an equity, a
  CAS stock, an index with futures and options expiring mid-run, a stock future; gaps, bands, settlement
  prices that sometimes never arrive) with random run settings; `RandomTrader` trades it from ctx.rng() only.
* `run_seeds(seeds)`: every seed on both implementations; a `Failure` for each one whose run folders differ.
* `shrink(world, failure)`: greedy delta debugging. The trader is first recorded (its context calls, tagged with
  the callback they were made in) and replayed by `stockify.tools.replay:Replay`; then instruments, days,
  events and trader calls are dropped and run settings simplified, each step kept only if oracle and engine
  still differ in the same file. Deterministic order, capped number of runs.
* `save_golden(world, failure)`: `tests/golden/F<seed>.yaml`, with the oracle's artifacts as the expected values.
"""
from __future__ import annotations

import copy
import json
import math
import random
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from stockify import engine, golden, timeutil
from stockify.oracle import run_bundle as oracle_run
from stockify.strategy import Strategy
from stockify.tools.replay import REPLAYED

MIN = 60 * 1_000_000_000
REPLAY_REF = "stockify.tools.replay:Replay"
COMPARED = ("orders.csv", "order_events.csv", "fills.csv", "charges.csv", "positions_eod.csv", "equity.csv",
            "margin_daily.csv", "events_log.txt")


# ── Worlds ───────────────────────────────────────────────────────────────────

def _real_charges() -> dict:
    """The shipped Zerodha rows, with the loader's VERIFY dates resolved (A1 §7.2)."""
    from stockify.config import loader
    return copy.deepcopy(loader.load_json("charges.json")["components"])


def _trading_days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def fuzz_world(seed: int) -> dict:
    rng = random.Random(seed)
    days = _trading_days(rng.choice([date(2026, 3, 30), date(2026, 7, 29), date(2025, 12, 4), date(2026, 1, 12)]), 3)
    iso = [d.isoformat() for d in days]
    expiry = iso[1]
    later = (days[2] + timedelta(days=30)).isoformat()
    eq = [{"from": "2015-01-01", "lot": 1, "tick_paise": 5}]
    instruments = [
        {"id": 1, "kind": "EQ", "tradingsymbol": "AAA", "specs": eq},
        {"id": 2, "kind": "EQ", "tradingsymbol": "CASX", "session_group": "CM_CAS", "cas_dates": [iso[2]], "specs": eq},
        {"id": 3, "kind": "INDEX", "tradingsymbol": "IDX", "specs": eq},
        {"id": 4, "kind": "FUT", "tradingsymbol": "IDXFUT", "underlying": "IDX", "underlying_id": 3, "expiry": expiry,
         "specs": [{"from": "2015-01-01", "lot": 50, "tick_paise": 10, "freeze_qty": 600},
                   {"from": iso[1], "lot": 25, "tick_paise": 10, "freeze_qty": 600}]},
        {"id": 5, "kind": "CE", "tradingsymbol": "IDXCE", "underlying": "IDX", "underlying_id": 3, "expiry": expiry,
         "strike_paise": 2_500_000, "specs": [{"from": "2015-01-01", "lot": 75, "tick_paise": 5, "freeze_qty": 1800}]},
        {"id": 6, "kind": "PE", "tradingsymbol": "IDXPE", "underlying": "IDX", "underlying_id": 3, "expiry": expiry,
         "strike_paise": 2_500_000, "specs": [{"from": "2015-01-01", "lot": 75, "tick_paise": 5}]},
        {"id": 7, "kind": "FUT", "tradingsymbol": "AAAFUT", "underlying": "AAA", "underlying_id": 1, "expiry": later,
         "underlying_class": "STOCK", "settlement": "physical",
         "specs": [{"from": "2015-01-01", "lot": 250, "tick_paise": 10}]},
    ]
    # M4.6: some worlds let the stock future expire mid-run with stock options beside it, settled by delivery
    # (A4 §7.3). Drawn from its own RNG, so the worlds without it are exactly as before.
    alt = random.Random(seed * 7919 + 4006)
    delivery = alt.random() < 0.35
    if delivery:
        instruments[6]["expiry"] = expiry
        for iid, kind, strike in ((8, "CE", alt.choice([240_000, 250_000, 260_000])),
                                  (9, "PE", alt.choice([240_000, 250_000, 260_000]))):
            instruments.append({"id": iid, "kind": kind, "tradingsymbol": f"AAA{kind}", "underlying": "AAA",
                                "underlying_id": 1, "expiry": expiry, "strike_paise": strike,
                                "underlying_class": "STOCK", "settlement": "physical",
                                "specs": [{"from": "2015-01-01", "lot": 250, "tick_paise": 5}]})
    dataset = rng.choice(["bars", "bars", "daily", "ticks", "ticks"])
    kind = {"bars": "bar", "daily": rng.choice(["eod_close", "bar"]), "ticks": rng.choice(["snapshot_ltp", "snapshot_quote"])}[dataset]
    slip = rng.choice([{"kind": "none"}, {"kind": "fixed_ticks", "k": 1}, {"kind": "random_ticks", "k": 3},
                       {"kind": "bps", "x": "0.0005"}])
    run = {"fill_model": {"kind": kind, "participation": rng.choice(["0.05", "0.10", "0.5", "1"]),
                          "limit_touch_policy": rng.choice(["through", "touch"]),
                          "depth_take": rng.choice(["0.25", "0.50", "1"]), "slippage": slip,
                          "half_spread_ticks": rng.choice([0, 1, 2])},
           "latency_ms": rng.choice([0, 0, 0, 250, 1000, 90_000]),
           "policies": {"preopen": rng.choice(["queue", "reject"]), "cas": rng.choice(["none", "close_price"]),
                        "freeze": rng.choice(["slice", "reject"]), "max_slices": rng.choice([1, 3, 10]),
                        "rms_square_off": False},
           "mark": rng.choice(["ltp", "mid"]), "capital_paise": rng.choice([5 * 10**6, 10**8, 10**10]),
           "seed": rng.getrandbits(64), "price_basis": "raw",
           "costs": {"allow_unverified": True, "auto_square_off_fee": rng.random() < 0.7,
                     "dp_charges": rng.random() < 0.7}}
    bundle = {"dates": iso, "instruments": instruments, "mis_cutoff": {"CM": "15:20", "FO": "15:25"},
              "protection_bp": rng.choice([50, 200, 500]),
              "sessions_by_date": {iso[2]: {"CM_CAS": [["PREOPEN", "09:00", "09:15"], ["CONTINUOUS", "09:15", "15:15"],
                                                       ["CAS", "15:15", "15:35"], ["POSTCLOSE", "15:50", "16:00"]]}}}
    if rng.random() < 0.8:
        bundle["charges"] = _real_charges()
    if alt.random() < 0.5:                       # M4.7: the expiry-day ELM on short index options (X-9)
        bundle["expiry_addon"] = "0.02"
    if delivery:                                 # M4.7: the Z-4 delivery-margin schedule (D-60)
        bundle["delivery_schedule"] = True
    if delivery and "charges" not in bundle:
        bundle["physical_delivery"] = {"brokerage_pct": "0.0025", "netted_brokerage_pct": "0.001",
                                       "stt_each_side_pct_of_value": "0.001"}
    if rng.random() < 0.5:
        bundle["sqoff_fee_paise"] = 5000
    price = {1: 250_000, 2: 100_000, 3: 2_500_000, 4: 2_502_000, 5: 15_000, 6: 14_000, 7: 251_000}
    tick = {1: 5, 2: 5, 3: 5, 4: 10, 5: 5, 6: 5, 7: 10}
    if delivery:
        price.update({8: 6_000, 9: 5_000})
        tick.update({8: 5, 9: 5})
    events: list[dict] = []
    for day in days:
        def at(hhmm: str, d=day) -> int:  # noqa: ANN001
            return timeutil.ist_ns(d, hhmm)
        vol = {i: 0 for i in price}
        if dataset == "daily":
            for iid in price:
                p = price[iid]
                hi, lo = p + tick[iid] * rng.randint(0, 40), max(tick[iid], p - tick[iid] * rng.randint(0, 40))
                c = rng.randint(lo // tick[iid], hi // tick[iid]) * tick[iid]
                events.append({"kind": "BAR", "instrument": iid, "interval_s": 86400, "start": at("09:15"),
                               "t": at("19:00"), "o": p, "h": hi, "l": lo, "c": c, "v": rng.randint(0, 50_000)})
                price[iid] = c
        else:
            for minute in sorted(rng.sample(range(0, 405), 70)):  # 09:00 … 15:45
                t0 = at("09:00") + minute * MIN
                for iid in rng.sample(list(price), rng.randint(1, 4)):
                    p = price[iid] = max(tick[iid] * 2, price[iid] + tick[iid] * rng.randint(-8, 8))
                    if dataset == "bars":
                        hi, lo = p + tick[iid] * rng.randint(0, 6), max(tick[iid], p - tick[iid] * rng.randint(0, 6))
                        o = rng.randint(lo // tick[iid], hi // tick[iid]) * tick[iid]
                        events.append({"kind": "BAR", "instrument": iid, "interval_s": 60, "start": t0, "o": o, "h": hi,
                                       "l": lo, "c": p, "v": rng.choice([0, 100, 1000, 10_000, 100_000])})
                    elif iid == 3:
                        events.append({"kind": "INDEX", "instrument": 3, "t": t0 + rng.randint(0, MIN - 1), "value": p})
                    else:
                        vol[iid] += rng.choice([0, 75, 250, 1000, 5000])
                        e = {"kind": rng.choice(["TICK_FULL", "TICK_FULL", "TICK_QUOTE"]), "instrument": iid,
                             "t": t0 + rng.randint(0, MIN - 1), "ltp": p, "volume": vol[iid],
                             "atp": p - tick[iid] * rng.randint(-2, 2), "high": p + tick[iid] * 10,
                             "low": max(tick[iid], p - tick[iid] * 10), "open": p}
                        if e["kind"] == "TICK_FULL":
                            e["bids"] = [[p - tick[iid] * j, rng.choice([0, 50, 75, 500, 5000]), j]
                                         for j in range(1, rng.randint(0, 5) + 1)]
                            e["asks"] = [[p + tick[iid] * j, rng.choice([0, 50, 75, 500, 5000]), j]
                                         for j in range(1, rng.randint(0, 5) + 1)]
                        events.append(e)
                if rng.random() < 0.04:
                    events.append({"kind": "GAP", "instrument": rng.choice([0, 1, 4]), "t": t0 + 30 * 10**9,
                                   "phase": rng.choice(["start", "end"]), "reason": rng.choice(["disconnect", "stall"])})
                if rng.random() < 0.03:
                    events.append({"kind": "PRICE_BAND", "instrument": 1, "t": t0 + 5 * 10**9,
                                   "lower": price[1] - 300, "upper": price[1] + 300})
        if day.isoformat() == expiry and rng.random() < 0.7:
            events.append({"kind": "SETTLE", "instrument": 3, "t": at("16:00"), "date": expiry, "price": price[3],
                           "subtype": "final"})
        if rng.random() < 0.6:
            events.append({"kind": "SETTLE", "instrument": 4, "t": at("18:00"), "date": day.isoformat(),
                           "price": price[4], "subtype": "daily"})
        if delivery and day.isoformat() == expiry and alt.random() < 0.8:   # else the §7.2 fallback at DAY_END
            events.append({"kind": "SETTLE", "instrument": 1, "t": at("16:05"), "date": expiry, "price": price[1],
                           "subtype": "final"})
    return {"id": f"FUZZ{seed}", "bundle": bundle, "run": run, "events": events, "strategy": {"actions": []},
            "fuzz": {"seed": seed, "p_act": random.Random(seed + 1).choice([5, 15, 40])}}


class RandomTrader(Strategy):
    """Random orders, modifies and cancels from ctx.rng() only, so both implementations act alike."""

    TAGS = ["", "", "x", "a,b", 'q"t', "AUTO_SQOFF"]

    def __init__(self, ids: list[int], p_act: int):
        self.ids = ids
        self.p_act = p_act
        self.placed: list[int] = []

    def on_start(self, ctx):  # noqa: ANN001
        for iid in self.ids:
            ctx.subscribe_ticks(iid)
            ctx.subscribe_bars(iid, 60)
            ctx.subscribe_bars(iid, 86400)
        ctx.schedule_every(37 * MIN, ctx.now() + 11 * MIN)

    def _pick(self, r, xs):  # noqa: ANN001, ANN202
        return xs[r.below(len(xs))]

    def _place(self, ctx) -> None:  # noqa: ANN001
        r = ctx.rng()
        iid = self._pick(r, self.ids)
        spec = ctx.spec(iid) or {"lot": 1, "tick_paise": 5}
        lot, tick = spec["lot"], spec["tick_paise"]
        deriv = iid >= 4
        product = self._pick(r, (["NRML", "NRML", "MIS"] if deriv else ["CNC", "CNC", "MIS"]) + ["CNC" if deriv else "NRML"])
        if r.below(40) == 0:
            product = "XYZ"
        side = self._pick(r, ["BUY", "SELL", "BUY", "SELL"] + (["buy"] if r.below(30) == 0 else []))
        typ = self._pick(r, ["MARKET", "LIMIT", "LIMIT", "SL", "SL-M"] + (["STOP"] if r.below(40) == 0 else []))
        k = r.below(20)
        qty = lot * (1 + r.below(4)) if k > 3 else (lot * 30 if k == 3 else (lot + 1 if k == 2 else (0 if k == 1 else -lot)))
        last = ctx.last_tick(iid)
        ref = last.ltp if last else 100_000
        price = max(0, ref + tick * (r.below(41) - 20)) // tick * tick
        if r.below(25) == 0:
            price += 1
        trigger = max(0, ref + tick * (r.below(21) - 10)) // tick * tick
        validity = self._pick(r, ["DAY", "DAY", "IOC"] + (["GTC"] if r.below(50) == 0 else []))
        protection = "AUTO" if r.below(3) else r.below(600)
        tag = self._pick(r, self.TAGS)
        oid = ctx.place(iid, side, qty, product, typ, price=price, trigger=trigger, validity=validity,
                        protection=protection, tag=tag)
        self.placed.append(oid)
        ctx.log("info", f"placed {oid} {iid} {side} {qty} {product} {typ} {price} {trigger} {validity} {protection}")

    def _act(self, ctx) -> None:  # noqa: ANN001
        r = ctx.rng()
        k = r.below(100)
        if k < 45:
            self._place(ctx)
        elif k < 62 and self.placed:
            oid = self._pick(r, self.placed + [max(self.placed) + 5])
            changes = {}
            if r.below(2):
                changes["qty"] = r.below(400)
            if r.below(2):
                o = ctx.order(oid)
                changes["price"] = (o.price if o else 100_000) + 5 * (r.below(9) - 4)
            if r.below(3) == 0:
                changes["trigger"] = 5 * r.below(60_000)
            if r.below(4) == 0:
                changes["type"] = self._pick(r, ["MARKET", "LIMIT", "SL", "SL-M", "WHAT"])
            ctx.log("info", f"modify {oid} {sorted(changes.items())} -> {ctx.modify(oid, **changes)}")
        elif k < 74 and self.placed:
            oid = self._pick(r, self.placed + [0, max(self.placed) + 3])
            ctx.log("info", f"cancel {oid} -> {ctx.cancel(oid)}")
        elif k < 80:
            ctx.log("info", f"funds {ctx.funds()!r} positions {ctx.positions()!r}")

    def _maybe(self, ctx) -> None:  # noqa: ANN001
        if ctx.rng().below(100) < self.p_act:
            self._act(ctx)

    def on_bar(self, ctx, bar):  # noqa: ANN001
        self._maybe(ctx)

    def on_tick(self, ctx, tick):  # noqa: ANN001
        self._maybe(ctx)

    def on_timer(self, ctx, timer_id):  # noqa: ANN001
        self._act(ctx)

    def on_order_update(self, ctx, order):  # noqa: ANN001
        ctx.log("info", f"update {order!r}")
        if ctx.rng().below(25) == 0 and order.status in ("OPEN", "PENDING", "TRIGGER_PENDING"):
            ctx.log("info", f"cancel-in-callback {order.order_id} -> {ctx.cancel(order.order_id)}")

    def on_fill(self, ctx, fill):  # noqa: ANN001
        ctx.log("info", f"fill {fill!r} pos {ctx.position(fill.instrument, 'MIS')!r} {ctx.position(fill.instrument, 'NRML')!r}")
        if ctx.rng().below(6) == 0:
            self._place(ctx)

    def on_session_phase(self, ctx, change):  # noqa: ANN001
        if change.phase == "CONTINUOUS" and ctx.rng().below(3) == 0:
            self._act(ctx)

    def on_session_end(self, ctx):  # noqa: ANN001
        ctx.log("info", f"end funds {ctx.funds()!r} positions {ctx.positions()!r}")


# ── Recording a strategy's context calls (replayed by stockify.tools.replay) ──

class _RecordingCtx:
    def __init__(self, ctx, rec: "Recorder"):  # noqa: ANN001
        self._ctx, self._rec = ctx, rec

    def __getattr__(self, name: str):  # noqa: ANN204
        attr = getattr(self._ctx, name)
        if name not in REPLAYED:
            return attr

        def call(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            self._rec.calls.append([self._rec.k, name, list(args), dict(kwargs)])
            return attr(*args, **kwargs)
        return call


class Recorder(Strategy):
    """Wraps a strategy and records its replayed context calls with the callback number they came from."""

    def __init__(self, inner: Strategy):
        self.inner, self.k, self.calls = inner, 0, []

    def on_start(self, ctx):  # noqa: ANN001
        self.inner.on_start(_RecordingCtx(ctx, self))

    def _hook(self, name: str, ctx, *args) -> None:  # noqa: ANN001
        self.k += 1
        getattr(self.inner, name)(_RecordingCtx(ctx, self), *args)

    def on_bar(self, ctx, bar):  # noqa: ANN001
        self._hook("on_bar", ctx, bar)

    def on_tick(self, ctx, tick):  # noqa: ANN001
        self._hook("on_tick", ctx, tick)

    def on_timer(self, ctx, timer_id):  # noqa: ANN001
        self._hook("on_timer", ctx, timer_id)

    def on_fill(self, ctx, fill):  # noqa: ANN001
        self._hook("on_fill", ctx, fill)

    def on_order_update(self, ctx, order):  # noqa: ANN001
        self._hook("on_order_update", ctx, order)

    def on_session_phase(self, ctx, change):  # noqa: ANN001
        self._hook("on_session_phase", ctx, change)

    def on_session_end(self, ctx):  # noqa: ANN001
        self._hook("on_session_end", ctx)


# ── Running and comparing ────────────────────────────────────────────────────

@dataclass
class Failure:
    seed: int | None
    file: str                       # the first file that differs, or "error" when only one side failed
    line: int = 0                   # 1-based line in that file (0 for metrics.json and errors)
    oracle: str = ""
    engine: str = ""
    world: dict = field(default_factory=dict, repr=False)

    def describe(self) -> str:
        where = f"{self.file} line {self.line}" if self.line else self.file
        return f"seed {self.seed}: {where}\n  oracle: {self.oracle}\n  engine: {self.engine}"


def _engine_bytes(path: Path) -> bytes:
    """The engine's file as compared (a seam: tests plant a disagreement here, never in the engine)."""
    return path.read_bytes()


def _close(a, b) -> bool:  # noqa: ANN001
    if isinstance(a, dict) and isinstance(b, dict):
        return sorted(a) == sorted(b) and all(_close(a[k], b[k]) for k in a)
    if isinstance(a, float) or isinstance(b, float):
        return type(a) is type(b) and math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-300)
    return a == b


def compare(oracle_dir: Path, engine_dir: Path) -> Failure | None:
    """DT-02 (A4 §9.3): CSVs and events_log byte-identical, metrics.json within a relative 1e-12."""
    for name in COMPARED:
        a, b = (oracle_dir / name).read_bytes(), _engine_bytes(engine_dir / name)
        if a != b:
            la, lb = a.decode("utf-8").splitlines(), b.decode("utf-8").splitlines()
            for n in range(max(len(la), len(lb))):
                x = la[n] if n < len(la) else "<end of file>"
                y = lb[n] if n < len(lb) else "<end of file>"
                if x != y:
                    return Failure(None, name, n + 1, x, y)
    ma = json.loads((oracle_dir / "metrics.json").read_text(encoding="utf-8"))
    mb = json.loads(_engine_bytes(engine_dir / "metrics.json").decode("utf-8"))
    if not _close(ma, mb):
        return Failure(None, "metrics.json", 0, json.dumps(ma, sort_keys=True), json.dumps(mb, sort_keys=True))
    return None


def _strategy(world: dict):  # noqa: ANN202
    """A fresh strategy for one run: the live trader, or None (a recorded world names Replay by ref)."""
    if world["strategy"].get("ref"):
        return None
    return RandomTrader([i["id"] for i in world["bundle"]["instruments"]], world["fuzz"]["p_act"])


def check_world(world: dict, folder: Path) -> Failure | None:
    """Both implementations on one world. Both failing counts as agreement (P-04); one failing is a Failure."""
    pack = golden.build_pack(world, folder)
    bundle = golden.build_bundle(world)
    errors: dict[str, Exception] = {}
    for impl, runner in (("oracle", oracle_run), ("engine", engine.run_bundle)):
        try:
            runner(bundle, [pack], folder / impl, strategy=_strategy(world), report=False)
        except Exception as e:  # noqa: BLE001 — both sides must fail alike
            errors[impl] = e
    seed = world.get("fuzz", {}).get("seed")
    if errors:
        if len(errors) == 2:
            return None
        return Failure(seed, "error", 0, repr(errors.get("oracle")), repr(errors.get("engine")), world)
    f = compare(folder / "oracle", folder / "engine")
    if f is not None:
        f.seed, f.world = seed, world
    return f


def run_both(seed: int, folder: Path) -> tuple[Exception | None, Exception | None]:
    """The P-04 test's step: run one seed's world on both sides into folder/oracle and folder/engine."""
    world = fuzz_world(seed)
    pack = golden.build_pack(world, folder)
    bundle = golden.build_bundle(world)
    errors: list[Exception | None] = []
    for runner in (oracle_run, engine.run_bundle):
        try:
            runner(bundle, [pack], folder / ("oracle" if runner is oracle_run else "engine"),
                   strategy=_strategy(world), report=False)
            errors.append(None)
        except Exception as e:  # noqa: BLE001 — both sides must fail alike
            errors.append(e)
    return errors[0], errors[1]


def run_seeds(seeds, progress=None) -> list[Failure]:  # noqa: ANN001
    """Every seed's world on both implementations; the failures, in seed order."""
    out = []
    for n, seed in enumerate(seeds, start=1):
        with tempfile.TemporaryDirectory(prefix=f"fuzz-{seed}-") as tmp:
            f = check_world(fuzz_world(seed), Path(tmp))
        if f is not None:
            out.append(f)
        if progress:
            progress(n, seed, f)
    return out


# ── Shrinking ────────────────────────────────────────────────────────────────

def record(world: dict) -> dict:
    """The world with its live trader replaced by a recording of the oracle run's context calls."""
    if world["strategy"].get("ref"):
        return copy.deepcopy(world)
    rec = Recorder(_strategy(world))
    with tempfile.TemporaryDirectory(prefix="fuzz-record-") as tmp:
        folder = Path(tmp)
        oracle_run(golden.build_bundle(world), [golden.build_pack(world, folder)], folder / "run", strategy=rec,
                   report=False)
    out = copy.deepcopy(world)
    out["strategy"] = {"ref": REPLAY_REF, "params": {"calls": rec.calls}}
    return out


def _event_date(e: dict) -> str:
    return timeutil.ns_to_ist(golden.ts(e.get("start", e.get("t")))).date().isoformat()


def _without_instrument(world: dict, iid: int) -> dict:
    gone = {iid}
    rows = world["bundle"]["instruments"]
    while True:                                   # derivatives of a removed underlying go too
        more = {r["id"] for r in rows if r.get("underlying_id") in gone} - gone
        if not more:
            break
        gone |= more
    w = copy.deepcopy(world)
    w["bundle"]["instruments"] = [r for r in rows if r["id"] not in gone]
    w["events"] = [e for e in w["events"] if e.get("instrument", 0) not in gone]
    if w["strategy"].get("ref"):
        w["strategy"]["params"]["calls"] = [c for c in w["strategy"]["params"]["calls"]
                                            if not (c[1] in ("place", "subscribe_ticks", "subscribe_bars")
                                                    and c[2] and c[2][0] in gone)]
    return w


def _without_date(world: dict, d: str) -> dict:
    w = copy.deepcopy(world)
    w["bundle"]["dates"] = [x for x in w["bundle"]["dates"] if x != d]
    w["events"] = [e for e in w["events"] if _event_date(e) != d]
    return w


# Run settings tried back at their plain values, one at a time.
_PLAIN = [("bundle", "charges", None), ("bundle", "sqoff_fee_paise", None), ("run", "latency_ms", 0),
          ("run", "mark", "ltp"), ("bundle", "protection_bp", 200)]


class _Budget:
    def __init__(self, runs: int):
        self.left = runs

    def take(self) -> bool:
        self.left -= 1
        return self.left >= 0


def shrink(world: dict, failure: Failure, max_runs: int = 2000) -> tuple[dict, Failure]:
    """The smallest world found that still disagrees in `failure.file`, and its failure."""
    budget = _Budget(max_runs)
    best = failure

    def still(w: dict) -> Failure | None:
        nonlocal best
        if not budget.take():
            return None
        with tempfile.TemporaryDirectory(prefix="fuzz-shrink-") as tmp:
            f = check_world(w, Path(tmp))
        if f is not None and f.file == failure.file:
            best = f
            return f
        return None

    w = record(world)
    if still(w) is None:                          # the replay must still show it; else shrink the live world
        w = copy.deepcopy(world)

    def ddmin(get, put) -> None:  # noqa: ANN001
        nonlocal w
        items = get(w)
        n = 2
        while len(items) >= 1 and budget.left > 0:
            chunk = max(1, math.ceil(len(items) / n))
            for i in range(0, len(items), chunk):
                cand = put(w, items[:i] + items[i + chunk:])
                if still(cand):
                    w, items = cand, get(cand)
                    n = max(n - 1, 2)
                    break
            else:
                if chunk == 1:
                    return
                n = min(len(items), n * 2)

    def put_events(x: dict, items: list) -> dict:
        y = copy.deepcopy(x)
        y["events"] = items
        return y

    def put_calls(x: dict, items: list) -> dict:
        y = copy.deepcopy(x)
        y["strategy"]["params"]["calls"] = items
        return y

    for iid in [r["id"] for r in world["bundle"]["instruments"]]:   # the big, cheap cuts first
        if any(r["id"] == iid for r in w["bundle"]["instruments"]):
            cand = _without_instrument(w, iid)
            if still(cand):
                w = cand
    for d in list(w["bundle"]["dates"]):
        if len(w["bundle"]["dates"]) > 1:
            cand = _without_date(w, d)
            if still(cand):
                w = cand
    ddmin(lambda x: x["events"], put_events)
    if w["strategy"].get("ref"):
        ddmin(lambda x: x["strategy"]["params"]["calls"], put_calls)
    for part, key, plain in _PLAIN:
        if key in w[part] and w[part][key] != plain:
            cand = copy.deepcopy(w)
            if plain is None:
                del cand[part][key]
            else:
                cand[part][key] = plain
            if still(cand):
                w = cand
    if budget.left > 0:                           # a second pass: fewer calls often free more events
        ddmin(lambda x: x["events"], put_events)
    best.world = w
    return w, best


# ── Saving as a golden ───────────────────────────────────────────────────────

def _cell(v: str):  # noqa: ANN202
    try:
        return int(v)
    except ValueError:
        return v


def save_golden(world: dict, failure: Failure, folder: Path | None = None) -> Path:
    """tests/golden/F<seed>.yaml: the (recorded) world, with the oracle's artifacts as the expected values."""
    import yaml
    w = record(world)
    seed = failure.seed if failure.seed is not None else world.get("fuzz", {}).get("seed", 0)
    gid = f"F{seed}"
    g = {"id": gid,
         "title": f"Fuzz seed {seed}: oracle and engine differ in {failure.file}"
                  + (f" line {failure.line}" if failure.line else "") + f" (oracle: {failure.oracle[:120]})",
         "semantics": ["A3 P-04"],
         "notes": "Saved by `python -m stockify tools fuzz --shrink --save`. Expected values are the oracle's run; "
                  "decide from A4 which side is right before changing either.",
         "bundle": w["bundle"], "run": w["run"], "events": w["events"], "strategy": w["strategy"],
         "fuzz": w.get("fuzz", {})}
    with tempfile.TemporaryDirectory(prefix="fuzz-save-") as tmp:
        tmp_dir = Path(tmp)
        try:
            oracle_run(golden.build_bundle(g), [golden.build_pack(g, tmp_dir)], tmp_dir / "run", report=False)
        except Exception as e:  # noqa: BLE001 — a world the oracle refuses is saved as an expected error
            g["expect"] = {"error": str(e)}
        else:
            run = tmp_dir / "run"
            expect: dict = {}
            for name, file in (("orders", "orders.csv"), ("fills", "fills.csv"), ("charges", "charges.csv"),
                               ("equity", "equity.csv"), ("positions", "positions_eod.csv")):
                rows = [{k: _cell(v) for k, v in r.items()} for r in golden._read_csv(run / file)]
                expect[name] = rows
                expect[f"{name}_count"] = len(rows)
            g["expect"] = expect
    out = (folder or golden.GOLDEN_DIR) / f"{gid}.yaml"
    out.write_text(yaml.safe_dump(g, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
    return out


# ── CLI: python -m stockify tools fuzz ───────────────────────────────────────

def parse_seeds(text: str) -> list[int]:
    """'4000-8999' or '4000,4007,4100-4110'."""
    out: list[int] = []
    for part in text.split(","):
        a, _, b = part.strip().partition("-")
        out += list(range(int(a), int(b) + 1)) if b else [int(a)]
    return out


def main(seeds: list[int], do_shrink: bool, do_save: bool, out=sys.stdout) -> int:  # noqa: ANN001
    def progress(n: int, seed: int, f: Failure | None) -> None:
        if f is not None:
            print(f"FAIL {f.describe()}", file=out, flush=True)
        if n % 250 == 0 or n == len(seeds):
            print(f"{n}/{len(seeds)} seeds checked", file=sys.stderr, flush=True)

    failures = run_seeds(seeds, progress)
    for f in failures:
        world = f.world
        if do_shrink:
            world, f = shrink(world, f)
            print(f"shrunk seed {f.seed}: {len(world['events'])} events, "
                  f"{len(world['bundle']['instruments'])} instruments, {len(world['bundle']['dates'])} days, "
                  f"{len(world['strategy'].get('params', {}).get('calls', []))} trader calls\n{f.describe()}",
                  file=out, flush=True)
        if do_save:
            print(f"saved {save_golden(world, f)}", file=out, flush=True)
    print(f"{len(seeds)} seeds, {len(failures)} disagreements", file=out)
    return 1 if failures else 0

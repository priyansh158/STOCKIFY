"""A3 P-04: differential fuzzing of the oracle against the engine (M3.12).

Each seed builds a random three-day world (bars, daily bars or ticks; an equity, a CAS stock, an index with
expiring futures and options, a stock future; gaps, price bands, settlement prices that sometimes never
arrive) and random run settings (fill model, slippage, latency, policies, mark, real charge rows). A random
trader, drawing only from ctx.rng() so both implementations make the same choices, places, modifies and
cancels orders with valid and invalid values. The run folders must agree (A4 §9.3). A failing seed is
reproduced with `fuzz_world(seed)`.
"""
from __future__ import annotations

import json
import random
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine, golden, timeutil
from stockify.oracle import run_bundle
from stockify.strategy import Strategy
from tests.stockify.test_engine_parity import assert_same_artifacts

MIN = 60 * 1_000_000_000
SEEDS = range(4000, 4200)  # 200 seeds in the regular run (A3 P-04); the weekly job uses 5,000


def _real_charges() -> dict:
    """The shipped Zerodha rows, with the loader's VERIFY dates resolved (A1 §7.2)."""
    import copy
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
    if rng.random() < 0.5:
        bundle["sqoff_fee_paise"] = 5000
    price = {1: 250_000, 2: 100_000, 3: 2_500_000, 4: 2_502_000, 5: 15_000, 6: 14_000, 7: 251_000}
    tick = {1: 5, 2: 5, 3: 5, 4: 10, 5: 5, 6: 5, 7: 10}
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
    return {"id": f"FUZZ{seed}", "bundle": bundle, "run": run, "events": events, "strategy": {"actions": []}}


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


def causality_problems(run_dir: Path, pack: Path) -> list[str]:
    """A3 P-07 (A4 §4.0), checked against the pack itself rather than either implementation: a bar fill's bar
    starts (effectively, at the continuous open) at or after the order's activation; a fill on snapshot
    evidence has its earlier snapshot s0 at or after it; a book fill has its snapshot s1 strictly after it."""
    import csv
    from collections import defaultdict
    from stockify.data import packs as P

    def ist_date(t: int) -> str:
        return timeutil.ns_to_ist(t).date().isoformat()

    bundle = json.loads((run_dir / "bundle.json").read_text(encoding="utf-8"))
    cont: dict[tuple[str, str], int] = {}
    for d in bundle["days"]:
        for g, phases in d["groups"].items():
            starts = [p["start_ns"] for p in phases if p["phase"] == "CONTINUOUS"]
            if starts:
                cont[(g, d["date"])] = min(starts)
    rows = {i["id"]: i for i in bundle["instruments"]}

    def group(iid: int, d: str) -> str:
        g = rows[iid]["session_group"]
        return ("CM_CAS" if d in rows[iid]["cas_dates"] else "CM") if g == "CM_CAS" else g

    _, recs = P.read(pack)
    bars: dict[tuple[int, int], list[dict]] = defaultdict(list)
    s0_times: dict[tuple[int, int], list[int | None]] = defaultdict(list)
    state: dict[int, tuple[str, dict | None]] = {}  # iid -> (day, last snapshot)
    for r in recs:
        k, iid, t = r["kind"], r["instrument_id"], r["t_event"]
        if k == "GAP":
            for j in ([iid] if r["scope"] == 1 else list(state)):
                if j in state:
                    state[j] = (state[j][0], None)
        elif k == "BAR":
            bars[(iid, t)].append(r)
        elif k in ("TICK_QUOTE", "TICK_FULL"):
            day, last = state.get(iid, (None, None))
            if day != ist_date(t):
                last = None
            s0_times[(iid, t)].append(last["t_event"] if last else None)
            state[iid] = (ist_date(t), r)
        elif k == "INDEX":
            day, last = state.get(iid, (None, None))
            state[iid] = (ist_date(t), None if day != ist_date(t) else last)
    with (run_dir / "orders.csv").open(encoding="utf-8") as fh:
        t_act = {int(o["order_id"]): int(o["t_activate_ns"]) for o in csv.DictReader(fh)}
    # orders.csv holds the final t_activate; a modify moves it to max(old, now + latency) (A4 §3.6). So the
    # final value is the one in force for fills after an order's last MODIFY; earlier fills are skipped.
    last_modify: dict[int, int] = {}
    with (run_dir / "order_events.csv").open(encoding="utf-8") as fh:
        for e in csv.DictReader(fh):
            if e["event"] == "MODIFY":
                last_modify[int(e["order_id"])] = int(e["t_ns"])
    problems = []
    with (run_dir / "fills.csv").open(encoding="utf-8") as fh:
        for f in csv.DictReader(fh):
            iid, t, ev, ta = int(f["instrument_id"]), int(f["t_ns"]), f["evidence"], t_act[int(f["order_id"])]
            if t <= last_modify.get(int(f["order_id"]), -1):
                continue
            if ev in ("bar_open", "bar_path", "eod_close"):
                b = min(bars[(iid, t)], key=lambda x: x["interval_s"])
                d = ist_date(b["start_ns"])
                eff = max(b["start_ns"], cont.get((group(iid, d), d), b["start_ns"]))
                ok = eff >= ta
            elif ev.startswith("book_lvl_") or ev == "book_cross":
                ok = t > ta
            else:  # vwap_int, ltp, trade_through
                ok = any(s0 is not None and s0 >= ta for s0 in s0_times[(iid, t)])
            if not ok:
                problems.append(f"fill {f['fill_id']} ({ev}) of order {f['order_id']} at {t}: t_activate {ta}")
    return problems


def run_both(seed: int, folder: Path) -> tuple[Exception | None, Exception | None]:
    g = fuzz_world(seed)
    pack = golden.build_pack(g, folder)
    bundle = golden.build_bundle(g)
    ids = [i["id"] for i in bundle["instruments"]]
    p_act = random.Random(seed + 1).choice([5, 15, 40])
    errors: list[Exception | None] = []
    for impl, runner in (("oracle", run_bundle), ("engine", engine.run_bundle)):
        try:
            runner(bundle, [pack], folder / impl, strategy=RandomTrader(ids, p_act), report=False)
            errors.append(None)
        except Exception as e:  # noqa: BLE001 — both sides must fail alike
            errors.append(e)
    return errors[0], errors[1]


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class DifferentialFuzzTests(unittest.TestCase):
    def test_p04_oracle_and_engine_agree_on_random_worlds(self):
        for seed in SEEDS:
            with self.subTest(seed=seed), tempfile.TemporaryDirectory(prefix="fuzz-") as tmp:
                folder = Path(tmp)
                oracle_error, engine_error = run_both(seed, folder)
                if oracle_error or engine_error:
                    self.assertTrue(oracle_error is not None and engine_error is not None,
                                    f"seed {seed}: oracle {oracle_error!r} / engine {engine_error!r}")
                    continue
                assert_same_artifacts(self, folder / "oracle", folder / "engine")
                self.assertEqual(causality_problems(folder / "engine", folder / f"FUZZ{seed}.evt"), [])

    def test_dt01_the_same_engine_run_twice_is_byte_identical(self):
        for seed in list(SEEDS)[:12]:
            g = fuzz_world(seed)
            with self.subTest(seed=seed), tempfile.TemporaryDirectory(prefix="dt01-") as tmp:
                folder = Path(tmp)
                pack = golden.build_pack(g, folder)
                bundle = golden.build_bundle(g)
                ids = [i["id"] for i in bundle["instruments"]]
                runs = []
                for n in (1, 2):
                    res = engine.run_bundle(bundle, [pack], folder / f"run{n}", strategy=RandomTrader(ids, 30),
                                            report=False)
                    runs.append((folder / f"run{n}" / "determinism.json").read_bytes())
                    self.assertEqual(len(res.hashes), 9)
                self.assertEqual(runs[0], runs[1])


if __name__ == "__main__":
    unittest.main()

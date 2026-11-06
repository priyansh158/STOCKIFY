"""M4.4: the option-chain view, ctx.chain(underlying, expiry) (C-13).

Chain worlds: an index, optionally a future of the same expiry, and calls and puts on 9 strikes quoted around their
Black-76 prices (rounded to the tick, sometimes one-sided or quote-only, with gaps that make quotes stale). A
recorder logs the whole chain view on every index tick. The oracle's answers are checked here; the engine must log
exactly the same (parity class below).
"""
from __future__ import annotations

import math
import random
import tempfile
import unittest
from datetime import date
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine, golden, timeutil
from stockify.fno import black76 as B
from stockify.oracle import run_bundle
from stockify.strategy import ChainView, Strategy

EXPIRY = "2026-08-04"
STRIKES = [2_480_000 + 5_000 * i for i in range(9)]  # ₹24,800 … ₹25,200 in paise


class ChainRecorder(Strategy):
    """Logs ctx.chain("NIFTY", EXPIRY) in full on every index tick."""

    def on_start(self, ctx):  # noqa: ANN001
        ctx.subscribe_ticks(1)
        ctx.log("info", f"start chain={ctx.chain('NIFTY', EXPIRY)!r} none={ctx.chain('NOPE', EXPIRY)!r}")

    def on_tick(self, ctx, tick):  # noqa: ANN001
        if tick.instrument != 1:
            return
        c = ctx.chain("NIFTY", EXPIRY)
        again = ctx.chain("NIFTY", EXPIRY)   # the per-step cache: equal, but fresh objects
        assert c == again and c is not again and (not c or c[0] is not again[0])
        ctx.log("info", f"F={c.forward_paise!r} src={c.forward_source} atm={c.atm_strike_paise} rows={list(c)!r}")


def chain_world(seed: int, with_future: bool) -> dict:
    rng = random.Random(seed)
    lot = [{"from": "2026-01-01", "lot": 75, "tick_paise": 5}]
    inst = [{"id": 1, "kind": "INDEX", "tradingsymbol": "NIFTY 50"}]
    if with_future:
        inst.append({"id": 2, "kind": "FUT", "tradingsymbol": "NIFTYFUT", "underlying": "NIFTY", "underlying_id": 1,
                     "expiry": EXPIRY, "specs": [{"from": "2026-01-01", "lot": 75, "tick_paise": 10}]})
    options = []
    for n, k in enumerate(STRIKES):
        for kind in ("CE", "PE"):
            iid = 10 + 2 * n + (kind == "PE")
            inst.append({"id": iid, "kind": kind, "tradingsymbol": f"NIFTY{k // 100}{kind}", "underlying": "NIFTY",
                         "underlying_id": 1, "expiry": EXPIRY, "strike_paise": k, "specs": lot})
            options.append((iid, kind == "CE", k))
    g = {"id": f"CHAIN{seed}", "bundle": {"dates": ["2026-08-03", EXPIRY], "instruments": inst},
         "run": {"fill_model": {"kind": "snapshot_quote"}, "capital_paise": 10_000_000, "price_basis": "raw"},
         "strategy": {"actions": []}, "events": []}
    d = date(2026, 8, 3)
    expiry_ns = timeutil.ist_ns(date(2026, 8, 4), "15:40")   # pricing only; the chain reads the bundle's
    spot = 2_500_000 + rng.randint(-8_000, 8_000)
    moment = timeutil.ist_ns(d, "09:16")
    for _ in range(45):
        # one moment: most options quoted within ~2 s, maybe the future, then the index tick (the chain is read
        # there); now and then a gap, or a longer pause that leaves every quote stale
        moment += rng.choice([20, 40, 60, 60, 600]) * 1_000_000_000
        spot += rng.randint(-1_500, 1_500)
        events = []
        for iid, is_call, k in options:
            if rng.random() < 0.25:
                continue
            t = moment + rng.randint(0, 2_000) * 1_000_000
            years = (expiry_ns - t) / B.NS_PER_YEAR
            sigma = 0.12 + 0.4 * ((k - spot) / spot) ** 2 + rng.uniform(-0.01, 0.01)   # a little smile
            fair = B.price(is_call, spot / 100.0, k / 100.0, years, 0.065, sigma) * 100.0
            half = rng.choice([5, 10, 25])
            bid = max(0, int(fair - half) // 5 * 5)
            ask = int(fair + half) // 5 * 5 + 5
            e = {"kind": rng.choice(["TICK_FULL", "TICK_FULL", "TICK_FULL", "TICK_QUOTE"]), "instrument": iid, "t": t,
                 "ltp": max(5, int(fair) // 5 * 5), "volume": rng.randint(0, 10**5), "oi": rng.randint(0, 10**6)}
            if e["kind"] == "TICK_FULL":
                e["bids"] = [[bid, 75, 1]] if bid > 0 and rng.random() < 0.9 else []
                e["asks"] = [[ask, 75, 1]] if rng.random() < 0.9 else []
            events.append(e)
        if with_future and rng.random() < 0.6:
            f = (spot + 2_000 + rng.randint(-200, 200)) // 10 * 10
            events.append({"kind": "TICK_FULL", "instrument": 2, "t": moment + rng.randint(0, 2_000) * 1_000_000,
                           "ltp": f, "bids": [[f - 10, 75, 1]], "asks": [[f + 10, 75, 1]],
                           "oi": rng.randint(0, 10**6)})
        if rng.random() < 0.1:
            events.append({"kind": "GAP", "instrument": rng.choice([0, 1, 10, 11]), "t": moment + 2_100_000_000,
                           "phase": rng.choice(["start", "end"]), "reason": "disconnect"})
        events.append({"kind": "INDEX", "instrument": 1, "t": moment + 2_500_000_000, "value": spot})
        g["events"] += sorted(events, key=lambda e: e["t"])
    return g


def chain_bundle(g: dict) -> dict:
    """The golden bundle with a 6.5 % risk-free rate (golden bundles carry none), so discounting is exercised."""
    bundle = golden.build_bundle(g)
    bundle["pricing"]["risk_free"] = [{"effective_from": "2015-01-01", "rate_ppb": 65_000_000}]
    return bundle


def run_oracle(g: dict, folder: Path) -> tuple[list[str], dict]:
    pack = golden.build_pack(g, folder)
    bundle = chain_bundle(g)
    run_bundle(bundle, [pack], folder / "oracle", strategy=ChainRecorder(), report=False)
    return (folder / "oracle" / "events_log.txt").read_text(encoding="utf-8").splitlines(), bundle


class OracleChainTests(unittest.TestCase):
    def test_chain_rows_forward_and_ivs(self):
        seen_sources, ivs, checked = set(), [], 0
        for seed, with_future in ((4400, False), (4401, True), (4402, False), (4403, True)):
            with self.subTest(seed=seed), tempfile.TemporaryDirectory(prefix="chain-") as tmp:
                log, bundle = run_oracle(chain_world(seed, with_future), Path(tmp))
                lines = [x for x in log if " INFO F=" in x]
                self.assertGreater(len(lines), 20)
                start = next(x for x in log if " INFO start chain=" in x)
                self.assertIn("none=[]", start)
                for x in lines:
                    src = x.split(" src=")[1].split(" ")[0]
                    seen_sources.add(src)
                    if with_future:
                        self.assertIn(src, ("future", "synthetic", "forward_from_index", "None"))
                    else:
                        self.assertNotEqual(src, "future")
                checked += len(lines)
        self.assertTrue({"synthetic", "future", "forward_from_index"} <= seen_sources, seen_sources)

    def test_one_chain_in_detail(self):
        """A hand-checked moment: rows in (strike, kind) order with every key; the ATM strike nearest F; IV at mid
        reprices the mid; Greeks are Black-76's at that IV; quotes missing a side have no mid and no IV."""
        captured: list[ChainView] = []

        class Grab(ChainRecorder):
            def on_tick(self, ctx, tick):  # noqa: ANN001
                if tick.instrument == 1:
                    captured.append((ctx.now(), ctx.chain("NIFTY", EXPIRY)))

        with tempfile.TemporaryDirectory(prefix="chain-") as tmp:
            g = chain_world(4404, with_future=False)
            pack = golden.build_pack(g, Path(tmp))
            bundle = chain_bundle(g)
            run_bundle(bundle, [pack], Path(tmp) / "oracle", strategy=Grab(), report=False)
        keys = ["instrument_id", "kind", "strike_paise", "bid_paise", "ask_paise", "mid_paise", "ltp_paise", "stale",
                "oi", "iv_bid", "iv_ask", "iv_mid", "iv_reason", "delta", "gamma", "vega", "theta"]
        last_ns = next(r["last_trading_ns"] for r in bundle["instruments"] if r["kind"] == "CE")
        rate = B.rate_on(bundle["pricing"]["risk_free"], "2026-08-03")
        self.assertEqual(rate, 0.065)
        full = 0
        for now, c in captured:
            self.assertEqual([(r["strike_paise"], r["kind"]) for r in c],
                             [(k, kind) for k in STRIKES for kind in ("CE", "PE")])
            for r in c:
                self.assertEqual(list(r), keys)
                if r["bid_paise"] is None or r["ask_paise"] is None:
                    self.assertIsNone(r["mid_paise"])
                    self.assertIsNone(r["iv_mid"])
                    continue
                self.assertEqual(r["mid_paise"], (r["bid_paise"] + r["ask_paise"]) / 2)
                if c.forward_paise is None or r["iv_mid"] is None:
                    continue
                t = B.year_fraction(now, last_ns)
                f, k, call = c.forward_paise / 100, r["strike_paise"] / 100, r["kind"] == "CE"
                self.assertLessEqual(abs(B.price(call, f, k, t, rate, r["iv_mid"]) - r["mid_paise"] / 100),
                                     B.PRICE_TOL)
                g = B.greeks(call, f, k, t, rate, r["iv_mid"])
                self.assertEqual((r["delta"], r["gamma"], r["vega"], r["theta"]), (g.delta, g.gamma, g.vega, g.theta))
                self.assertTrue(0.05 < r["iv_mid"] < 0.6)
                full += 1
            if c.forward_paise is not None:
                strikes = sorted({r["strike_paise"] for r in c})
                self.assertEqual(c.atm_strike_paise, min(strikes, key=lambda x: (abs(x - c.forward_paise), x)))
        self.assertGreater(full, 100)

    def test_forward_sources(self):
        """The future's mid wins when it is quoted; with no future, the synthetic forward needs two strikes with
        both sides quoted and fresh; with neither, the index value; with nothing, None."""
        from stockify.oracle.context import Context

        class M:  # a market entry
            def __init__(self, bid=None, ask=None, ltp=None, t=0):  # noqa: ANN001
                self.bid, self.ask, self.ltp, self.t, self.oi = bid, ask, ltp, t, None

        class E:  # just enough engine for _forward
            def __init__(self, rows, market):  # noqa: ANN001
                self.bundle = {"instruments": rows, "pricing": {"stale_after_ms": 5000}}
                self.market, self.now = market, 1_000_000_000

        rows = [{"id": 10 + i, "kind": kind, "underlying": "N", "underlying_id": 1, "expiry": "E", "strike_paise": k}
                for i, (k, kind) in enumerate([(100_00, "CE"), (100_00, "PE"), (110_00, "CE"), (110_00, "PE")])]
        fut = {"id": 2, "kind": "FUT", "underlying": "N", "underlying_id": 1, "expiry": "E", "strike_paise": None}

        def forward(market, with_future=False):  # noqa: ANN001, ANN202
            ctx = Context.__new__(Context)
            ctx._e = E(rows + ([fut] if with_future else []), market)
            return ctx._forward("N", "E", rows, 1.0)

        quotes = {10: M(900, 1100, t=10**9), 11: M(400, 600, t=10**9), 12: M(450, 550, t=10**9),
                  13: M(900, 1100, t=10**9)}
        # C − P = 500 at K=100.00 → 10,500; C − P = −500 at K=110.00 → 10,500 (paise); median of two = mean
        self.assertEqual(forward(quotes), (10_500.0, "synthetic"))
        self.assertEqual(forward({**quotes, 2: M(10_480, 10_500)}, with_future=True), (10_490.0, "future"))
        one_sided = {**quotes, 13: M(900, None, t=10**9)}
        self.assertEqual(forward({**one_sided, 1: M(ltp=10_400)}), (10_400.0, "forward_from_index"))
        stale = {**quotes, 13: M(900, 1100, t=-10**12)}                      # quoted long ago
        self.assertEqual(forward(stale), (None, None))
        self.assertEqual(forward({}), (None, None))


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class ChainParityTests(unittest.TestCase):
    """The engine logs exactly what the oracle logs on chain worlds (DT-02), Greeks and IVs included."""

    def test_chain_worlds_log_the_same(self):
        mod = engine.load()
        for seed, with_future in ((4410, False), (4411, True), (4412, False), (4413, True), (4414, True)):
            with self.subTest(seed=seed), tempfile.TemporaryDirectory(prefix="chain-parity-") as tmp:
                folder = Path(tmp)
                expected, _ = run_oracle(chain_world(seed, with_future), folder)
                e = mod.Engine(str(folder / "oracle" / "bundle.json"))
                e.add_pack(str(next(folder.glob("*.evt"))))
                e.set_strategy_py(ChainRecorder())
                e.run()
                got = e.events_log()
                del e
                for n, (a, b) in enumerate(zip(got, expected)):
                    if a != b:
                        self.fail(f"line {n + 1} differs\n engine: {a}\n oracle: {b}")
                self.assertEqual(len(got), len(expected))
                self.assertGreater(sum(" INFO F=" in x for x in expected), 20)


if __name__ == "__main__":
    unittest.main()

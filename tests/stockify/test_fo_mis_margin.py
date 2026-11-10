"""D-80 (A4 §6.5): an intraday (MIS) futures or options order needs the margin of the same NRML order.

Before D-80 both runners margined every MIS order as a share of its traded value. That is right for shares; for
futures and options the `FO` row of `mis_margin_pct` means "the full NRML margin", so a sold call blocked about a
thirtieth of what the broker blocks and a bought future was asked for its whole contract value. Here:

* the two rupee examples, under the flat model, SPAN-lite and exact SPAN, on the oracle and the engine alike;
* MIS and NRML versions of one order sequence block the same at every step (shares still use the MIS rate);
* a hedge split across the two products is margined as the hedge;
* an `FO` rate other than 1 is refused by the config loader and by both runners;
* random order streams: trading some contracts as MIS never changes a decision or a blocked amount;
* a day's state saved with open MIS futures and options restores with the same blocked margin.
"""
from __future__ import annotations

import copy
import csv
import json
import random
import re
import shutil
import tempfile
import unittest
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import engine, golden, numerics
from stockify.config import loader
from stockify.oracle import run_bundle as oracle_run
from stockify.oracle.runner import RunError
from stockify.strategy import Strategy
from tests.stockify.test_chain_view import chain_bundle
from tests.stockify.test_engine_parity import CSV_AND_LOG, assert_same_artifacts
from tests.stockify.test_span_parity import SpanTrader, staircase_bundle, staircase_world
from tests.stockify.test_spanlite_parity import OptionTrader, spanlite_bundle, spanlite_world

NOT_MODELLED = "intraday F&O leverage is not modelled"

# ── A flat-model world: an index at 24,500, a call, a further call, a future at 25,000 and a share ─────────────

DAY = "2026-03-02"
SPOT = 2_450_000
LOT = 65
CALL, FUT, WING, SHARE = 2, 3, 4, 5
PRICE = {CALL: 10_000, FUT: 2_500_000, WING: 4_000, SHARE: 100_000}     # ₹100, ₹25,000, ₹40, ₹1,000
MINUTES = 16


def _contract(iid: int, kind: str, strike: int | None = None) -> dict:
    row = {"id": iid, "kind": kind, "tradingsymbol": f"NIFTY{kind}{iid}", "underlying": "NIFTY", "underlying_id": 1,
           "expiry": "2026-03-31", "specs": [{"from": "2026-01-01", "lot": LOT, "tick_paise": 5}]}
    if strike is not None:
        row["strike_paise"] = strike
    return row


def flat_world(**bundle) -> dict:
    inst = [{"id": 1, "kind": "INDEX", "tradingsymbol": "NIFTY 50"}, _contract(CALL, "CE", 2_500_000),
            _contract(FUT, "FUT"), _contract(WING, "CE", 2_550_000),
            {"id": SHARE, "kind": "EQ", "tradingsymbol": "ACME", "specs": [{"from": "2026-01-01", "lot": 1,
                                                                            "tick_paise": 5}]}]
    ev = [{"kind": "INDEX", "instrument": 1, "t": f"{DAY}T09:15:30+05:30", "value": SPOT}]
    for m in range(MINUTES):
        for iid, px in PRICE.items():
            ev.append({"kind": "BAR", "instrument": iid, "interval_s": 60, "start": f"{DAY}T09:{15 + m}:00+05:30",
                       "o": px, "h": px, "l": px, "c": px, "v": 100_000})
    return {"id": "D80", "bundle": {"dates": [DAY], "instruments": inst, **bundle},
            "run": {"fill_model": {"kind": "bar"}, "capital_paise": 100_000_000, "price_basis": "raw"},
            "events": ev, "strategy": {"actions": []}}


class BarSteps(Strategy):
    """On the call's n-th minute bar: logs funds and every order's status so far, then places step n's LIMIT orders
    at the instrument's (constant) price; they fill on the next bar."""

    def __init__(self, steps: list[list[tuple]]):
        self.steps, self.n, self.ids = steps, 0, []

    def on_start(self, ctx):  # noqa: ANN001
        ctx.subscribe_bars(CALL, 60)

    def on_bar(self, ctx, bar):  # noqa: ANN001
        f = ctx.funds()
        seen = " ".join(f"{ctx.order(i).status}:{ctx.order(i).reason}" for i in self.ids)
        ctx.log("info", f"step {self.n} blocked={f.blocked} available={f.available} reserved={f.reserved} [{seen}]")
        for iid, side, qty, product in (self.steps[self.n] if self.n < len(self.steps) else ()):
            self.ids.append(ctx.place(iid, side, qty, product, "LIMIT", price=PRICE[iid]))
        self.n += 1


class TickSteps(Strategy):
    """Chain worlds: on the n-th index tick logs funds and every order's status so far, then places step n's MARKET
    orders (they fill on a later quote). The step counter and the order ids are carried day to day."""

    def __init__(self, steps: dict[int, list[tuple]]):
        self.steps, self.n, self.ids = steps, 0, []

    def on_start(self, ctx):  # noqa: ANN001
        ctx.subscribe_ticks(1)

    def on_tick(self, ctx, tick):  # noqa: ANN001
        if tick.instrument != 1:
            return
        self.n += 1
        seen = " ".join(f"{ctx.order(i).status}:{ctx.order(i).reason}" for i in self.ids)
        ctx.log("info", f"tick {self.n} funds {ctx.funds()!r} [{seen}]")
        for iid, side, qty, product in self.steps.get(self.n, ()):
            self.ids.append(ctx.place(iid, side, qty, product, "MARKET", protection=5000))
            ctx.log("info", f"tick {self.n} placed {iid} {side} {qty} funds {ctx.funds()!r}")

    def save_state(self) -> dict:
        return {"n": self.n, "ids": self.ids}

    def load_state(self, state: dict) -> None:
        self.n, self.ids = state["n"], list(state["ids"])


def log_lines(run: Path) -> list[str]:
    """Every INFO line of a run's log, without its time."""
    return [x.split(" ", 2)[2] for x in (run / "events_log.txt").read_text(encoding="utf-8").splitlines()
            if " INFO " in x]


def strategy_lines(run: Path) -> list[str]:
    """The lines BarSteps and TickSteps wrote."""
    return [x for x in log_lines(run) if x.startswith(("step ", "tick "))]


def on_both(test: unittest.TestCase, g: dict, bundle: dict, make) -> list[str]:  # noqa: ANN001
    """Runs make() on the oracle and on the engine: the artifacts must be the same bytes. The strategy's lines."""
    with tempfile.TemporaryDirectory(prefix="fo-mis-") as tmp:
        folder = Path(tmp)
        pack = golden.build_pack(g, folder)
        oracle_run(copy.deepcopy(bundle), [pack], folder / "oracle", strategy=make(), report=False)
        engine.run_bundle(copy.deepcopy(bundle), [pack], folder / "engine", strategy=make(), report=False)
        assert_same_artifacts(test, folder / "oracle", folder / "engine")
        return strategy_lines(folder / "oracle")


def field(line: str, name: str) -> int:
    return int(line.split(f"{name}=")[1].split(" ")[0].rstrip(",)"))


def rupee_up(raw_ppb: int) -> int:
    return numerics.ceil_to(numerics.ceil_div(raw_ppb, 1_000_000_000), 100)


def as_product(steps, product: str):  # noqa: ANN001, ANN201
    """The same steps with every futures or options order in `product` (share orders keep theirs)."""
    def swap(orders: list[tuple]) -> list[tuple]:
        return [(i, s, q, p if i == SHARE else product) for i, s, q, p in orders]
    if isinstance(steps, dict):
        return {n: swap(orders) for n, orders in steps.items()}
    return [swap(orders) for orders in steps]


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class FlatModelTests(unittest.TestCase):
    def run_steps(self, steps: list[list[tuple]]) -> list[str]:
        g = flat_world()
        return on_both(self, g, golden.build_bundle(g), lambda: BarSteps(steps))

    def test_a_sold_mis_call_blocks_the_models_margin(self):
        """1 lot of a NIFTY call sold at ₹100 as MIS: ₹1,91,100 (12 % of 65 × 24,500), not ₹6,500 (65 × ₹100)."""
        mis = self.run_steps([[(CALL, "SELL", LOT, "MIS")]])
        self.assertIn("[COMPLETE:]", mis[-1])
        self.assertEqual(field(mis[-1], "blocked"), rupee_up(LOT * SPOT * 120_000_000))
        self.assertEqual(field(mis[-1], "blocked"), 19_110_000)
        self.assertEqual(mis, self.run_steps([[(CALL, "SELL", LOT, "NRML")]]))

    def test_a_bought_mis_future_is_accepted_with_the_models_margin(self):
        """1 lot of the NIFTY future bought at 25,000 as MIS with ₹10,00,000: ₹1,95,000 (12 % of 65 × 25,000) is
        needed, not ₹16,25,000 (the whole contract value, which rejected it R_MARGIN)."""
        mis = self.run_steps([[(FUT, "BUY", LOT, "MIS")]])
        self.assertIn("reserved=19500000 [OPEN:]", mis[1])           # the order check, before the fill
        self.assertIn("[COMPLETE:]", mis[-1])
        self.assertEqual(field(mis[-1], "blocked"), 19_500_000)
        self.assertEqual(mis, self.run_steps([[(FUT, "BUY", LOT, "NRML")]]))

    SEQUENCE = [
        [(CALL, "SELL", LOT, "MIS")],                         # a short call
        [(FUT, "BUY", LOT, "MIS"), (SHARE, "BUY", 100, "MIS")],
        [(CALL, "SELL", LOT, "MIS")],                         # increases the short
        [(WING, "BUY", 2 * LOT, "MIS")],                      # premium only
        [(CALL, "BUY", LOT, "MIS")],                          # reduces: nothing needed
        [(FUT, "SELL", 2 * LOT, "MIS")],                      # long to short in one order
        [(CALL, "SELL", 6 * LOT, "MIS")],                     # more than the money allows
        [(CALL, "BUY", 3 * LOT, "MIS")],                      # short to long in one order
        [(FUT, "BUY", LOT, "MIS"), (WING, "SELL", 2 * LOT, "MIS")],
        [(SHARE, "SELL", 100, "MIS")],
    ]

    def test_mis_and_nrml_sequences_block_the_same_at_every_step(self):
        mis = self.run_steps(self.SEQUENCE)
        self.assertEqual(mis, self.run_steps(as_product(self.SEQUENCE, "NRML")))
        self.assertEqual(sum("R_MARGIN" in x for x in mis[-1].split()), 1)       # the six-lot sale, in both
        self.assertEqual([field(x, "blocked") for x in mis[:12]],          # what each step leaves blocked
                         [0, 19_110_000, 19_110_000, 59_720_000, 59_720_000, 40_610_000, 40_610_000, 40_610_000,
                          21_500_000, 21_500_000, 2_000_000, 0])

    def test_shares_still_block_the_mis_rate(self):
        """Unchanged: 100 shares at ₹1,000 as MIS block 20 % of their value; as CNC they are paid for in full."""
        mis = self.run_steps([[(SHARE, "BUY", 100, "MIS")]])
        self.assertIn("reserved=2000000 [OPEN:]", mis[1])
        self.assertIn("blocked=2000000 available=98000000 reserved=0 [COMPLETE:]", mis[-1])
        cnc = self.run_steps([[(SHARE, "BUY", 100, "CNC")]])
        self.assertIn("blocked=0 available=90000000 reserved=0 [COMPLETE:]", cnc[-1])

    def test_an_mis_leg_and_an_nrml_leg_of_one_contract_are_one_net_leg(self):
        """One book per underlying: a call sold as NRML and bought back as MIS leaves two positions and no margin."""
        lines = self.run_steps([[(CALL, "SELL", LOT, "NRML")], [], [(CALL, "BUY", LOT, "MIS")]])
        self.assertEqual(field(lines[2], "blocked"), 19_110_000)
        self.assertIn("[COMPLETE: COMPLETE:]", lines[-1])
        self.assertEqual(field(lines[-1], "blocked"), 0)


# ── SPAN-lite and exact SPAN, on the chain worlds of the parity tests ────────────────────────────────────────

ATM_CALL, WING_CALL, CHAIN_FUT = 18, 22, 2          # the 25,000 and 25,100 calls and the future of chain_world
SEED = 4811


def traded_futures(g: dict) -> dict:
    """chain_world quotes its futures one lot deep and reports no trades in them, so an order on a future rests
    all day there. Here every tick of a future reports 7,500 more traded: orders on it fill (trade-through)."""
    futures = {i["id"] for i in g["bundle"]["instruments"] if i["kind"] == "FUT"}
    volume = dict.fromkeys(futures, 0)
    for e in g["events"]:
        if e.get("instrument") in futures and e["kind"] == "TICK_FULL":
            volume[e["instrument"]] += 7_500
            e["volume"] = volume[e["instrument"]]
    return g


def span_world(model: str, capital: int = 1_000_000_000) -> tuple[dict, dict]:
    """(world, bundle) under `model`: spanlite, or span_exact on the SPAN fixture of test_span_parity."""
    if model == "spanlite":
        g = traded_futures(spanlite_world(SEED, True))
        g["run"]["capital_paise"] = capital
        return g, spanlite_bundle(g)
    g = traded_futures(staircase_world(SEED))
    g["run"]["capital_paise"] = capital
    return g, staircase_bundle(g, model=model)


def funds_after(lines: list[str], tick: int) -> str:
    return next(x for x in lines if x.startswith(f"tick {tick} funds "))


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class SpanModelTests(unittest.TestCase):
    MODELS = ("spanlite", "span_exact")

    def run_steps(self, model: str, steps: dict[int, list[tuple]], capital: int = 1_000_000_000) -> list[str]:
        g, b = span_world(model, capital)
        return on_both(self, g, b, lambda: TickSteps(steps))

    def test_a_sold_mis_call_blocks_what_the_same_nrml_call_blocks(self):
        for model in self.MODELS:
            with self.subTest(model=model):
                steps = {5: [(ATM_CALL, "SELL", 75, "MIS")]}
                mis = self.run_steps(model, steps)
                self.assertIn("[COMPLETE:]", funds_after(mis, 8))
                self.assertGreater(field(funds_after(mis, 8), "blocked"), 10_000_000)    # a lakh or more, per lot
                self.assertEqual(mis, self.run_steps(model, as_product(steps, "NRML")))

    def test_a_bought_mis_future_is_accepted_with_the_models_margin(self):
        """₹10,00,000 does not hold a 75-lot future's whole value (about ₹18,75,000); its margin fits easily."""
        for model in self.MODELS:
            with self.subTest(model=model):
                steps = {5: [(CHAIN_FUT, "BUY", 75, "MIS")]}
                mis = self.run_steps(model, steps, capital=100_000_000)
                self.assertIn("[OPEN:]", funds_after(mis, 6))                 # accepted: before, R_MARGIN
                self.assertGreater(field(funds_after(mis, 6), "reserved"), 10_000_000)
                self.assertIn("[COMPLETE:]", funds_after(mis, 10))
                self.assertGreater(field(funds_after(mis, 10), "blocked"), 10_000_000)   # a lakh or more
                self.assertLess(field(funds_after(mis, 10), "blocked"), 30_000_000)      # far below its value
                self.assertEqual(mis, self.run_steps(model, as_product(steps, "NRML"), capital=100_000_000))

    def test_a_hedge_split_across_products_is_margined_as_the_hedge(self):
        """A call sold as NRML, then a further call of the same expiry bought as MIS: the spread's margin, which is
        less than the sold call's alone, and what the same two legs block when both are NRML."""
        for model in self.MODELS:
            with self.subTest(model=model):
                steps = {5: [(ATM_CALL, "SELL", 75, "NRML")], 9: [(WING_CALL, "BUY", 75, "MIS")]}
                split = self.run_steps(model, steps)
                self.assertIn("[COMPLETE: COMPLETE:]", funds_after(split, 16))
                self.assertLess(2 * field(funds_after(split, 16), "blocked"), field(funds_after(split, 9), "blocked"))
                self.assertEqual(split, self.run_steps(model, as_product(steps, "NRML")))


# ── An FO rate other than 1 is refused ──────────────────────────────────────────────────────────────────────

def policy_row(segment: str, pct: str, effective_from: str = "2015-01-01") -> dict:
    return {"applies": {"segment": segment}, "effective_from": effective_from, "pct": pct, "source": "Z-5",
            "verified": False}


class FoRateTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=str(support.SANDBOX_ROOT)))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def load(self, rows: list[dict]) -> dict:
        doc = {"schema": "stockify.broker_policy/1", "mis_margin_pct": rows}
        (self.dir / "broker_policy.json").write_text(json.dumps(doc), encoding="utf-8")
        return loader.load_json("broker_policy.json", self.dir)

    def test_the_loader_refuses_an_fo_rate_other_than_one(self):
        for pct in ("0.5", "0.20", "2", "0"):
            with self.subTest(pct=pct):
                with self.assertRaises(loader.ConfigError) as ctx:
                    self.load([policy_row("CM", "0.20"), policy_row("FO", pct)])
                self.assertIn(NOT_MODELLED, str(ctx.exception))
                self.assertIn("mis_margin_pct", str(ctx.exception))

    def test_the_loader_refuses_a_later_fo_row_other_than_one(self):
        with self.assertRaises(loader.ConfigError) as ctx:
            self.load([policy_row("FO", "1"), policy_row("FO", "0.5", "2027-01-01")])
        self.assertIn(NOT_MODELLED, str(ctx.exception))

    def test_the_loader_accepts_one_and_any_share_rate(self):
        doc = self.load([policy_row("CM", "0.20"), policy_row("CM", "0.25", "2027-01-01"), policy_row("FO", "1")])
        self.assertEqual(len(doc["mis_margin_pct"]), 3)
        self.assertEqual(len(self.load([policy_row("CM", "0.5"), policy_row("FO", "1.0")])["mis_margin_pct"]), 2)

    def test_the_shipped_fo_row_is_one(self):
        rows = loader.load_json("broker_policy.json")["mis_margin_pct"]
        self.assertEqual([r["pct"] for r in rows if r["applies"]["segment"] == "FO"], ["1"])

    def _bundle_error(self, runner, model: str) -> str:  # noqa: ANN001
        g = flat_world(mis_margin_fo="0.5")
        g["run"]["margin"] = {"model": model}
        with tempfile.TemporaryDirectory(prefix="fo-mis-") as tmp:
            folder = Path(tmp)
            pack = golden.build_pack(g, folder)
            b = golden.build_bundle(g)
            if model == "spanlite":
                b["margin_params"]["spanlite"] = spanlite_bundle(spanlite_world(SEED, False))[
                    "margin_params"]["spanlite"]
            with self.assertRaises(Exception) as ctx:
                runner(b, [pack], folder / "run", strategy=BarSteps([[(SHARE, "BUY", 1, "MIS")]]), report=False)
            return str(ctx.exception)

    def test_the_oracle_refuses_a_bundle_whose_fo_rate_is_not_one(self):
        for model in ("flat", "spanlite"):
            with self.subTest(model=model):
                g = flat_world(mis_margin_fo="0.5")
                g["run"]["margin"] = {"model": model}
                with tempfile.TemporaryDirectory(prefix="fo-mis-") as tmp, self.assertRaises(RunError) as ctx:
                    folder = Path(tmp)
                    oracle_run(golden.build_bundle(g), [golden.build_pack(g, folder)], folder / "run",
                               strategy=BarSteps([[(SHARE, "BUY", 1, "MIS")]]), report=False)
                self.assertIn(NOT_MODELLED, str(ctx.exception))
                self.assertIn("500000000", str(ctx.exception))

    @unittest.skipUnless(engine.available(), "stockify._engine is not built")
    def test_the_engine_refuses_it_with_the_same_words(self):
        for model in ("flat", "spanlite"):
            with self.subTest(model=model):
                on_engine = self._bundle_error(engine.run_bundle, model)
                self.assertIn(NOT_MODELLED, on_engine)
                self.assertEqual(on_engine, self._bundle_error(oracle_run, model))


# ── Random order streams: trading some contracts as MIS changes nothing ─────────────────────────────────────

SETTLED_MIS = re.compile(r"^(\S+ INFO settled \d+) MIS ", re.M)
SUBMITTED_MIS = re.compile(r"^((?:BUY|SELL) \d+) MIS ")


def without_product(path: Path) -> list[list[str]]:
    """A run's CSV without its `product` column and with the product of a submitted order (order_events' detail,
    `BUY 75 MIS MARKET`) read as NRML: positions, orders and fills are otherwise the same rows."""
    with path.open(encoding="utf-8", newline="") as f:
        rows = [[SUBMITTED_MIS.sub(r"\1 NRML ", cell) for cell in r] for r in csv.reader(f)]
    if "product" not in rows[0]:
        return rows
    at = rows[0].index("product")
    return [r[:at] + r[at + 1:] for r in rows]


def mixed_products(g: dict, seed: int) -> dict[int, str]:
    """Each future and option of the world drawn MIS or NRML (a contract keeps one product, so each position of the
    mixed run is the same position of the all-NRML run)."""
    rnd = random.Random(seed)
    return {i["id"]: rnd.choice(("MIS", "NRML")) for i in g["bundle"]["instruments"] if i["kind"] != "INDEX"}


def trader(base: type, products: dict[int, str]):  # noqa: ANN201
    class Mixed(base):
        def product(self, iid: int) -> str:
            return products.get(iid, "NRML")
    return Mixed


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class RandomStreamTests(unittest.TestCase):
    def check(self, g: dict, bundle: dict, base: type, seed: int) -> dict:
        products = mixed_products(g, seed)
        with tempfile.TemporaryDirectory(prefix="fo-mis-") as tmp:
            folder = Path(tmp)
            pack = golden.build_pack(g, folder)
            failed = {}
            for name, runner, cls in (("nrml", oracle_run, base), ("oracle", oracle_run, trader(base, products)),
                                      ("engine", engine.run_bundle, trader(base, products))):
                try:
                    runner(copy.deepcopy(bundle), [pack], folder / name, strategy=cls(), report=False)
                except Exception as e:  # noqa: BLE001 — exact SPAN stops where it cannot price: then all three must
                    failed[name] = str(e)
            if failed:
                self.assertEqual(sorted(failed), ["engine", "nrml", "oracle"], (seed, failed))
                self.assertEqual(len(set(failed.values())), 1, (seed, failed))
                return {"failed": 1}
            assert_same_artifacts(self, folder / "oracle", folder / "engine")
            for name in CSV_AND_LOG:                     # every decision, fill, charge and blocked amount
                if name.endswith(".csv"):
                    self.assertEqual(without_product(folder / "oracle" / name),
                                     without_product(folder / "nrml" / name), (seed, name))
                else:                                    # the log names the product only where a position settles
                    self.assertEqual(SETTLED_MIS.sub(r"\1 NRML ", (folder / "oracle" / name).read_text("utf-8")),
                                     (folder / "nrml" / name).read_text("utf-8"), (seed, name))
            orders = without_product(folder / "oracle" / "orders.csv")
            status = orders[0].index("status")
            with (folder / "oracle" / "orders.csv").open(encoding="utf-8", newline="") as f:
                mis = sum(r["product"] == "MIS" for r in csv.DictReader(f))
            return {"orders": len(orders) - 1, "mis": mis, "rejected": sum(r[status] == "REJECTED" for r in orders[1:]),
                    "blocked": sum("funds Funds(" in x and "blocked=0," not in x for x in log_lines(folder / "oracle"))}

    def run_seeds(self, seeds, world, bundle, base: type) -> dict:  # noqa: ANN001
        totals: dict[str, int] = {}
        for seed in seeds:
            with self.subTest(seed=seed):
                g = world(seed)
                for k, v in self.check(g, bundle(g), base, seed).items():
                    totals[k] = totals.get(k, 0) + v
        return totals

    def test_flat(self):
        def world(seed: int) -> dict:
            g = traded_futures(spanlite_world(seed, seed % 3 != 0))
            g["run"].update(margin={"model": "flat"}, capital_paise=150_000_000)   # tight: some orders are refused
            return g
        t = self.run_seeds(range(8100, 8125), world, chain_bundle, OptionTrader)
        self.assertGreater(t.get("mis", 0), 200, t)
        self.assertGreater(t.get("rejected", 0), 20, t)
        self.assertGreater(t.get("blocked", 0), 500, t)

    def test_spanlite(self):
        def world(seed: int) -> dict:
            g = traded_futures(spanlite_world(seed, seed % 3 != 0))
            g["run"]["capital_paise"] = 100_000_000
            return g
        t = self.run_seeds(range(8200, 8225), world, spanlite_bundle, OptionTrader)
        self.assertGreater(t.get("mis", 0), 200, t)
        self.assertGreater(t.get("rejected", 0), 20, t)
        self.assertGreater(t.get("blocked", 0), 500, t)

    def test_staircase_and_exact_span(self):
        for model in ("staircase", "span_exact"):
            with self.subTest(model=model):
                t = self.run_seeds(range(8300, 8312), lambda seed: traded_futures(staircase_world(seed)),
                                   lambda g, model=model: staircase_bundle(g, model=model), SpanTrader)
                self.assertGreater(t.get("mis", 0), 80, t)
                self.assertGreater(t.get("blocked", 0), 200, t)


# ── Day to day: open MIS futures and options restore with the same blocked margin ───────────────────────────

@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class CarriedStateTests(unittest.TestCase):
    """Golden worlds have no MIS cut-off (and a square-off order may not fill), so an MIS future or option can be
    open at DAY_END; the saved state carries the position with its product, and the margin is worked out again."""

    STEPS = {5: [(ATM_CALL, "SELL", 75, "MIS")], 6: [(CHAIN_FUT, "BUY", 75, "MIS")], 9: [(WING_CALL, "BUY", 75, "NRML")]}

    def outcome(self, steps: dict, folder: Path):  # noqa: ANN201
        from tests.stockify.test_engine_sod_state import Outcome
        g = traded_futures(spanlite_world(SEED, True))
        g["run"]["seed_mode"] = "per_day"
        folder.mkdir(parents=True)
        pack = golden.build_pack(g, folder)
        return Outcome(spanlite_bundle(g), pack, lambda: TickSteps(steps), folder, "spanlite")

    def test_a_state_with_open_mis_contracts_restores_with_the_same_blocked_margin(self):
        with tempfile.TemporaryDirectory(prefix="fo-mis-sod-") as tmp:
            o = self.outcome(self.STEPS, Path(tmp) / "mis")
            self.assertIsNone(o.error)
            self.assertEqual(o.oracle_states, o.engine_states)
            held = {(p["instrument_id"], p["product"]) for p in json.loads(o.oracle_states[0])["ledger"]["positions"]
                    if p["q"]}
            self.assertEqual(held, {(ATM_CALL, "MIS"), (WING_CALL, "NRML"), (CHAIN_FUT, "MIS")})
            for folder in ("oracle", "engine"):                       # whole run = day by day, on each runner
                self.assertEqual((o.folder / folder / "whole" / "events_log.txt").read_bytes(),
                                 (o.folder / folder / "parts" / "events_log.txt").read_bytes())
            self.assertIsNone(o.engine_parts_vs_whole())
            self.assertIsNone(o.oracle_vs_engine_whole())
            for i in range(o.days):
                self.assertIsNone(o.engine_vs_oracle_day(i), i)
            # the restored day blocks what the all-NRML book blocks: the model's margin of the three legs
            nrml = self.outcome(as_product(self.STEPS, "NRML"), Path(tmp) / "nrml")
            day2 = strategy_lines(o.folder / "engine" / "day1")
            self.assertGreater(field(day2[0], "blocked"), 10_000_000)
            self.assertEqual(day2, strategy_lines(nrml.folder / "engine" / "day1"))
            self.assertEqual(day2, strategy_lines(o.folder / "oracle" / "day1"))


if __name__ == "__main__":
    unittest.main()

"""B2B 5.4: what `ctx.modify` takes, the same on the oracle and on the engine.

A modify of a live order to a quantity of 0 or less used to be accepted: the order stayed live with no units, and at
the day's end the saved state held a final order of no units, which the state loader (A1 §6.4) refuses, so the
next day could not start. Now `ctx.modify` refuses it, as `ctx.place` refuses a bad argument (B2B 5.1): a ValueError
for a quantity of 0 or less, a TypeError for a `qty`, `price` or `trigger` that is not a whole int. The two runners
give the same class and the same words, and the order is left as it was. Skipped when the engine is not built.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import bundle as bundle_mod, engine
from stockify.oracle import run_bundle as oracle_run
from stockify.oracle.runner import Engine as OracleEngine, load_records
from stockify.strategy import Strategy
from stockify.tools import fuzz
from tests.stockify.test_engine_parity import assert_same_artifacts
from tests.stockify.test_engine_sod_state import _world_inputs, day_bundles, engine_run, split_pack
from tests.stockify.test_engine_strategies import _materialise, daily_world
from tests.stockify.test_sod_state import _run

WHOLE = "money is whole paise and quantity is whole units"


class _Modify(Strategy):
    """On the first bar: a LIMIT buy at the close, then `ctx.modify(oid, **changes)`; the outcome is logged."""

    def __init__(self, **changes):  # noqa: ANN003
        self.changes = changes
        self.done = False

    def on_start(self, ctx):  # noqa: ANN001
        self.iid = ctx.resolve("NSE:TEST")
        ctx.subscribe_bars(self.iid, 86400)

    def on_bar(self, ctx, bar):  # noqa: ANN001
        if self.done:
            return
        self.done = True
        oid = ctx.place(self.iid, "BUY", 1, "CNC", "LIMIT", price=bar.close)
        ctx.modify(oid, **self.changes)
        o = ctx.order(oid)
        ctx.log("info", f"order {oid}: {o.status} qty={o.qty} price={o.price}")


class _Once:
    def __init__(self, case: unittest.TestCase, seed: int = 5600):
        g, _ = daily_world(seed)
        tmp = tempfile.TemporaryDirectory(prefix="modify-args-")
        case.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.pack, self.bundle = _materialise(g, {}, self.folder)
        self.n = 0

    def run(self, impl: str, **changes) -> tuple[Path, BaseException | None]:  # noqa: ANN003
        self.n += 1
        out = self.folder / f"{impl}-{self.n}"
        go = oracle_run if impl == "oracle" else engine.run_bundle
        try:
            go(self.bundle, [self.pack], out, strategy=_Modify(**changes), report=False)
        except Exception as e:  # noqa: BLE001 — the outcome under comparison
            return out, e
        return out, None


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class ModifyArgumentTests(unittest.TestCase):
    def test_a_quantity_of_zero_or_less_is_refused_the_same_on_both_runners(self):
        once = _Once(self)
        for qty in (0, -1, -(10**9)):
            why = f"ctx.modify: qty must be positive, not {qty}"
            with self.subTest(qty=qty):
                for impl in ("oracle", "engine"):
                    run, e = once.run(impl, qty=qty)
                    self.assertIs(type(e), ValueError, (impl, e))
                    self.assertEqual(str(e), why, impl)
                    self.assertFalse((run / "orders.csv").exists(), impl)

    def test_qty_price_and_trigger_must_be_whole_ints(self):
        once = _Once(self)
        for name in ("qty", "price", "trigger"):
            for bad in (1.5, True, "1", None):
                why = f"ctx.modify: {name} must be an int, not {type(bad).__name__} ({bad!r}): {WHOLE}"
                with self.subTest(arg=name, value=repr(bad)):
                    for impl in ("oracle", "engine"):
                        _, e = once.run(impl, **{name: bad})
                        self.assertIs(type(e), TypeError, (impl, e))
                        self.assertEqual(str(e), why, impl)

    def test_the_arguments_are_checked_in_the_order_qty_price_trigger(self):
        once = _Once(self)
        for impl in ("oracle", "engine"):
            _, e = once.run(impl, trigger="3", price=2.5, qty=0)
            self.assertEqual(str(e), "ctx.modify: qty must be positive, not 0", impl)
            _, e = once.run(impl, trigger="3", price=2.5)
            self.assertTrue(str(e).startswith("ctx.modify: price must be an int, not float"), (impl, e))

    def test_valid_modifies_run_as_before_and_the_two_folders_are_the_same(self):
        once = _Once(self)
        for changes in ({}, {"qty": 3}, {"price": 7}, {"trigger": 5}, {"qty": 2, "price": 9}):
            with self.subTest(changes=changes):
                o, oe = once.run("oracle", **changes)
                e, ee = once.run("engine", **changes)
                self.assertEqual((oe, ee), (None, None))
                assert_same_artifacts(self, o, e)


class _TryZero(Strategy):
    """Places a far-off buy on the first bar, tries to modify it to 0 and to -1 (catching each refusal), and keeps
    the order live to the day's end."""

    def __init__(self, iid: int):
        self.iid = iid
        self.oid = 0
        self.seen: list[str] = []

    def on_start(self, ctx):  # noqa: ANN001
        ctx.subscribe_bars(self.iid, 60)

    def on_bar(self, ctx, bar):  # noqa: ANN001
        if self.oid:
            return
        spec = ctx.spec(self.iid)
        self.oid = ctx.place(self.iid, "BUY", spec["lot"], "CNC", "LIMIT", price=bar.close // spec["tick_paise"] * spec["tick_paise"],
                             tag="MODZERO")
        for q in (0, -1):
            try:
                ctx.modify(self.oid, qty=q)
            except ValueError as e:
                self.seen.append(str(e))
                ctx.log("info", f"refused: {e}")


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class StateAfterARefusedModifyTests(unittest.TestCase):
    def test_a_state_saved_after_the_attempt_reloads_on_both_runners(self):
        folder = Path(tempfile.mkdtemp(prefix="modify-state-", dir=str(tests.support.SANDBOX_ROOT)))
        world = fuzz.fuzz_world(4003)
        world["run"]["seed_mode"] = "per_day"
        bundle, pack = _world_inputs(world, folder)
        iid = 1
        days = day_bundles(bundle)
        b0, b1 = days[0], days[1]
        day0 = split_pack(pack, bundle["days"], folder)[0]

        s = _TryZero(iid)
        o_state = _run(b0, load_records([day0] if day0 else []), s, folder / "oracle").eod_state()
        self.assertEqual(s.seen, ["ctx.modify: qty must be positive, not 0", "ctx.modify: qty must be positive, not -1"])
        e_state = engine_run(b0, day0, _TryZero(iid), folder / "engine")
        self.assertEqual(o_state, e_state)

        st = json.loads(e_state)
        mine = [x for x in st["pending_orders"] + st["final_orders"] if x["tag"] == "MODZERO"]
        self.assertEqual(len(mine), 1)
        self.assertGreater(mine[0]["qty"], 0)       # the order kept its units

        b1["sod_state"] = st                        # the next day starts from it, on both runners
        OracleEngine(json.loads(bundle_bytes(b1)), [], Strategy())
        (folder / "d1").mkdir()
        bundle_mod.write(json.loads(bundle_bytes(b1)), folder / "d1")
        e = engine.load().Engine(str(folder / "d1" / "bundle.json"))
        e.set_strategy_py(Strategy())
        self.assertEqual(e.eod_state(), e_state)


def bundle_bytes(b: dict) -> str:
    return bundle_mod.dumps(b)

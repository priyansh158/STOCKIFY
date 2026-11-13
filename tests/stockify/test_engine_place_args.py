"""B2B 5.1 (audit M-1): what `ctx.place` takes, the same on the oracle and on the engine.

Money is whole paise and quantity is whole units. `qty`, `price` and `trigger` must be an `int` (and not a `bool`):
a float, a bool, a string, a Decimal or None stops the run with a TypeError that names the argument. Before, both
runners rounded `1.5` down to `1` and took `True` for `1`.

The same strategy object type is run on both implementations with the same bad value; the exception's type and
text must be the same, and neither run leaves artifacts. (Only the engine writes a failed run's log; it must end on
the same words.) Skipped when the engine is not built.
"""
from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine
from stockify.oracle import run_bundle as oracle_run
from stockify.strategy import Strategy
from tests.stockify.test_engine_parity import assert_same_artifacts
from tests.stockify.test_engine_strategies import _materialise, daily_world


class _Place(Strategy):
    """On the first bar: one LIMIT buy at the close with `args` over the valid defaults; its outcome is logged."""

    def __init__(self, **args):  # noqa: ANN003
        self.args = args
        self.done = False

    def on_start(self, ctx):  # noqa: ANN001
        self.iid = ctx.resolve("NSE:TEST")
        ctx.subscribe_bars(self.iid, 86400)

    def on_bar(self, ctx, bar):  # noqa: ANN001
        if self.done:
            return
        self.done = True
        a = {"qty": 1, "price": bar.close, "trigger": 0, **self.args}
        oid = ctx.place(self.iid, "BUY", a.pop("qty"), "CNC", "LIMIT", **a)
        o = ctx.order(oid)
        ctx.log("info", f"order {oid}: {o.status} {o.reason} qty={o.qty} price={o.price} trigger={o.trigger}")


class _Once:
    """A world, and one run of it per implementation."""

    def __init__(self, case: unittest.TestCase, seed: int = 5600):
        g, _ = daily_world(seed)
        tmp = tempfile.TemporaryDirectory(prefix="place-args-")
        case.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.pack, self.bundle = _materialise(g, {}, self.folder)
        self.n = 0

    def run(self, impl: str, **args) -> tuple[Path, BaseException | None]:  # noqa: ANN003
        self.n += 1
        out = self.folder / f"{impl}-{self.n}"
        go = oracle_run if impl == "oracle" else engine.run_bundle
        try:
            go(self.bundle, [self.pack], out, strategy=_Place(**args), report=False)
        except Exception as e:  # noqa: BLE001 — the outcome under comparison
            return out, e
        return out, None


def _last_line(run: Path) -> str:
    return (run / "events_log.txt").read_text(encoding="utf-8").splitlines()[-1].split(" ", 1)[1]


BAD = (1.0, 1.5, float("nan"), True, False, "1", None, Decimal("1"), [1], 2 + 0j)


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class WholeNumberTests(unittest.TestCase):
    def test_a_value_that_is_not_an_int_stops_both_runs_with_the_same_type_error(self):
        once = _Once(self)
        for name in ("qty", "price", "trigger"):
            for bad in BAD:
                why = (f"ctx.place: {name} must be an int, not {type(bad).__name__} ({bad!r}): "
                       "money is whole paise and quantity is whole units")
                with self.subTest(arg=name, value=repr(bad)):
                    seen = {}
                    for impl in ("oracle", "engine"):
                        run, e = once.run(impl, **{name: bad})
                        self.assertIs(type(e), TypeError, (impl, e))
                        self.assertEqual(str(e), why, impl)
                        self.assertFalse((run / "orders.csv").exists(), impl)        # the run stopped: no artifacts
                        self.assertFalse((run / "manifest.json").exists(), impl)
                        seen[impl] = (type(e), str(e))
                    self.assertEqual(seen["oracle"], seen["engine"])
                    self.assertEqual(_last_line(run), f"ERROR TypeError: {why}")     # the engine's log of the stop

    def test_the_first_bad_argument_is_the_one_named(self):
        once = _Once(self)
        for impl in ("oracle", "engine"):
            _, e = once.run(impl, qty=1.5, price=2.5, trigger="3")
            self.assertEqual(str(e), "ctx.place: qty must be an int, not float (1.5): "
                                     "money is whole paise and quantity is whole units", impl)
            _, e = once.run(impl, price=2.5, trigger="3")
            self.assertEqual(str(e), "ctx.place: price must be an int, not float (2.5): "
                                     "money is whole paise and quantity is whole units", impl)

    def test_ints_run_as_before_and_the_two_folders_are_the_same(self):
        class Lots(int):
            """A subclass of int is an int."""

        once = _Once(self)
        for args in ({}, {"qty": 3}, {"qty": Lots(2)}, {"qty": 0}, {"qty": -1}, {"price": 7}, {"trigger": 5}):
            with self.subTest(args=args):
                o, oe = once.run("oracle", **args)
                e, ee = once.run("engine", **args)
                self.assertEqual((oe, ee), (None, None))
                assert_same_artifacts(self, o, e)
                self.assertIn(" INFO order 1: ", (o / "events_log.txt").read_text(encoding="utf-8"))


_PROT = 'ctx.place: protection must be "AUTO" or an int from 1 to 10000 (basis points)'


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class ProtectionTests(unittest.TestCase):
    """B2B 5.2 (audit M-2): `protection` is "AUTO" or an int from 1 to 10000 bp. Before, anything else (a float,
    "auto", "10", None, 0, -5, 10**6) was quietly taken as AUTO on the engine and used raw on the oracle."""

    BAD_TYPE = (1.0, 5.5, True, False, "auto", "10", "", None, Decimal("5"), [5])
    BAD_RANGE = (0, -1, 10001, 10**6, 2**40)

    def test_a_bad_protection_stops_both_runs_with_the_same_error(self):
        once = _Once(self)
        for bad in self.BAD_TYPE + self.BAD_RANGE:
            kind = ValueError if isinstance(bad, int) and not isinstance(bad, bool) else TypeError
            why = f"{_PROT}, not {type(bad).__name__} ({bad!r})"
            with self.subTest(value=repr(bad)):
                for impl in ("oracle", "engine"):
                    run, e = once.run(impl, protection=bad)
                    self.assertIs(type(e), kind, (impl, e))
                    self.assertEqual(str(e), why, impl)
                    self.assertFalse((run / "orders.csv").exists(), impl)

    def test_auto_and_one_to_a_thousand_run_the_same_on_both(self):
        once = _Once(self)
        for prot in ("AUTO", 1, 5, 100, 1000, 5000, 10000):
            with self.subTest(protection=prot):
                o, oe = once.run("oracle", protection=prot)
                e, ee = once.run("engine", protection=prot)
                self.assertEqual((oe, ee), (None, None))
                assert_same_artifacts(self, o, e)


if __name__ == "__main__":
    unittest.main()

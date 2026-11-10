"""ST 5.1 (A1 §6.4, A4 §1.10): a start-of-day state is untrusted input, and both runners treat it the same way.

The state file sits on disk between days. So `load` on the oracle and on the C++ engine checks everything first and
only then applies it: a state is accepted whole, or it is refused with RunError and the engine is exactly as it was
(the same object then loads the good state and gives the same bytes as a fresh one, before and after running the day).
One table of hostile states (tests.stockify.sod_state_hostile) drives both runners; they must refuse the same rows
with the same words, and write the same bytes for the rows they accept. Random single-field changes of the good state
(a fixed seed) must end the same way on both.
"""
from __future__ import annotations

import copy
import json
import random
import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import bundle as bundle_mod, engine, numerics
from stockify.oracle import runner as oracle_runner, sod_state
from stockify.strategy import Strategy
from tests.stockify import sod_state_hostile as H

BUNDLE_TEXT = H.BUNDLE_PATH.read_text(encoding="utf-8")
INVALID_JSON = "sod_state: invalid JSON"      # the two JSON readers word the detail differently: only this is compared
ENGINE_BUILT = engine.available()
NEEDS_ENGINE = unittest.skipUnless(ENGINE_BUILT, "stockify._engine is not built")


class Keeper(Strategy):
    """Keeps the state it is handed, so a loaded state is written back whole."""

    def __init__(self):
        self.kept: dict = {}

    def save_state(self) -> dict:
        return self.kept

    def load_state(self, state: dict) -> None:
        self.kept = state


class OracleSide:
    name = "oracle"

    @staticmethod
    def error() -> type:
        return oracle_runner.RunError

    @staticmethod
    def new(strategy=None):  # noqa: ANN001, ANN205
        return oracle_runner.Engine(json.loads(BUNDLE_TEXT), [], strategy if strategy is not None else Keeper())

    @staticmethod
    def from_bundle(state: dict, folder: Path):  # noqa: ANN205, ARG004
        b = json.loads(BUNDLE_TEXT)
        b["sod_state"] = state
        return oracle_runner.Engine(b, [], Keeper())

    @staticmethod
    def from_bundle_text(text: str, folder: Path):  # noqa: ANN205, ARG004
        """The bundle file's text, read as the oracle's file readers read it."""
        try:
            b = sod_state.loads_bundle(text)
        except sod_state.StateError as e:
            raise oracle_runner.RunError(str(e)) from e
        return oracle_runner.Engine(b, [], Keeper())


class EngineSide:
    name = "engine"

    @staticmethod
    def error() -> type:
        return engine.load().RunError

    @staticmethod
    def new(strategy=None):  # noqa: ANN001, ANN205
        e = engine.load().Engine(str(H.BUNDLE_PATH))
        e.set_strategy_py(strategy if strategy is not None else Keeper())
        return e

    @staticmethod
    def from_bundle(state: dict, folder: Path):  # noqa: ANN205
        b = json.loads(BUNDLE_TEXT)
        b["sod_state"] = state
        bundle_mod.write(b, folder)
        e = engine.load().Engine(str(folder / "bundle.json"))
        e.set_strategy_py(Keeper())
        return e

    @staticmethod
    def from_bundle_text(text: str, folder: Path):  # noqa: ANN205
        (folder / "bundle.json").write_bytes(text.encode("utf-8"))
        e = engine.load().Engine(str(folder / "bundle.json"))
        e.set_strategy_py(Keeper())
        return e


def attempt(side, go) -> tuple[str, str]:  # noqa: ANN001
    """("ok", result) or ("refused", message). Anything but the side's RunError is not a refusal: it is raised."""
    try:
        return "ok", go()
    except side.error() as e:
        if type(e) is not side.error():
            raise AssertionError(f"{side.name}: refused with {type(e).__name__}, not RunError: {e}") from e
        return "refused", str(e)


def load_text(side, text: str, *, run: bool = False) -> tuple[str, str]:  # noqa: ANN001
    """A fresh engine loads the text: ("ok", the state it writes back [+ the state after the day]) or ("refused", …)."""
    e = side.new()

    def go() -> str:
        e.load_sod_state(text)
        out = e.eod_state()
        if run:
            e.run()
            out += "\n" + e.eod_state()
        return out
    return attempt(side, go)


def load_bundle(side, state: dict) -> tuple[str, str]:  # noqa: ANN001
    """The same state through a bundle's `sod_state` (the constructor applies it)."""
    with tempfile.TemporaryDirectory(prefix="sod-hostile-", dir=str(tests.support.SANDBOX_ROOT)) as tmp:
        return attempt(side, lambda: side.from_bundle(state, Path(tmp)).eod_state())


def bundle_text_with(state_text: str, escaped_key: bool = False) -> str:
    """The fixture bundle's file text with `state_text` as its sod_state: the text goes in as it is, so what only a
    text can hold (a key twice, a huge integer, a very deep nesting) reaches the constructor's reader."""
    b = json.loads(BUNDLE_TEXT)
    b["sod_state"] = "@@STATE@@"
    text = numerics.dumps_canonical(b).replace('"@@STATE@@"', state_text)
    if escaped_key:                       # the same key, spelled with a JSON escape (u+005f is the underscore)
        assert text.count('"sod_state":') == 1
        text = text.replace('"sod_state":', '"sod\\u005fstate":')
    return text


def load_bundle_text(side, state_text: str, escaped_key: bool = False) -> tuple[str, str]:  # noqa: ANN001
    """The same text through a bundle file's `sod_state`: ("ok", the state written back) or ("refused", …)."""
    text = bundle_text_with(state_text, escaped_key)
    with tempfile.TemporaryDirectory(prefix="sod-hostile-", dir=str(tests.support.SANDBOX_ROOT)) as tmp:
        return attempt(side, lambda: side.from_bundle_text(text, Path(tmp)).eod_state())


_reference: dict[str, str] = {}


def reference(side) -> str:  # noqa: ANN001
    """The good state loaded by a fresh engine and the day run: what a refused load must leave reachable."""
    if side.name not in _reference:
        kind, out = load_text(side, H.GOOD_TEXT, run=True)
        assert kind == "ok", out
        _reference[side.name] = out
    return _reference[side.name]


def same(a: tuple[str, str], b: tuple[str, str]) -> bool:
    if a[0] == "refused" and b[0] == "refused" and a[1].startswith(INVALID_JSON) and b[1].startswith(INVALID_JSON):
        return True
    return a == b


class RowChecks:
    """What one row must do on one side; the test classes below call it for every row."""

    def check_row(self, side, row: H.Row) -> tuple[str, str]:  # noqa: ANN001
        value = H.build(row)
        text = H.text_of(value)
        got = load_text(side, text, run=row.run)
        if row.error is None:
            self.assertEqual(got[0], "ok", got[1])
        else:
            self.assertEqual(got[0], "refused", f"{side.name} accepted it")
            self.assertIn(row.error, got[1])
            self.assertTrue(got[1].startswith("sod_state: "), got[1])
            self.assertLess(len(got[1]), 600, "a refusal is one short line")
            self.assertTrue(got[1].isprintable() and got[1].isascii(), repr(got[1]))
            self.check_untouched(side, text)
        if not row.text_only:
            state = json.loads(value) if isinstance(value, str) else value
            via_bundle = load_bundle(side, state)
            if row.error is None:
                self.assertEqual(via_bundle, ("ok", got[1].split("\n")[0]))
            else:
                self.assertEqual(via_bundle[0], "refused", f"{side.name} accepted it inside a bundle")
                self.assertIn(row.error, via_bundle[1])
        elif row.bundle:                      # a text-level refusal must hold on the constructor path too
            via_bundle = load_bundle_text(side, text, row.escaped_key)
            self.assertEqual(via_bundle[0], "refused", f"{side.name} accepted it inside a bundle file")
            self.assertIn(row.error, via_bundle[1])
            self.assertTrue(via_bundle[1].startswith("sod_state: "), via_bundle[1])
        return got

    def check_untouched(self, side, bad_text: str) -> None:  # noqa: ANN001
        """All or nothing: after a refused load, the same object loads the good state, writes the same bytes, and
        runs the day to the same end as a fresh one (so nothing was applied and nothing was queued twice)."""
        e = side.new()
        with self.assertRaises(side.error()):
            e.load_sod_state(bad_text)
        with self.assertRaises(side.error()):          # and it is refused again, the same way
            e.load_sod_state(bad_text)
        e.load_sod_state(H.GOOD_TEXT)
        out = e.eod_state()
        self.assertEqual(out, H.GOOD_TEXT)
        e.run()
        self.assertEqual(out + "\n" + e.eod_state(), reference(side))


class TableShapeTests(unittest.TestCase):
    def test_every_rule_has_a_row_and_the_named_cases_are_there(self):
        names = [r.name for r in H.ROWS]
        self.assertEqual(len(names), len(set(names)))
        # 7 (a strategy whose load_state fails), 12 (the busy guard) and 14–16 are not states: they have their own
        # tests below and in test_engine_sod_state.
        self.assertEqual({r.item for r in H.ROWS}, {1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 13})
        for name in H.HANG_ROWS + H.CRASH_ROWS:
            self.assertIn(name, names)
        self.assertEqual(H.MAX_BYTES, sod_state.MAX_BYTES)
        self.assertEqual(H.good()["trade_date"], "2026-01-12")
        self.assertEqual(json.loads(BUNDLE_TEXT)["days"][0]["date"], "2026-01-13")


class OracleHostileStateTests(RowChecks, unittest.TestCase):
    def test_every_row_on_the_oracle(self):
        for row in H.ROWS:
            with self.subTest(row=row.name):
                self.check_row(OracleSide, row)


@NEEDS_ENGINE
class EngineHostileStateTests(RowChecks, unittest.TestCase):
    def test_every_row_on_the_engine_and_the_same_as_the_oracle(self):
        for row in H.ROWS:
            with self.subTest(row=row.name):
                got = self.check_row(EngineSide, row)
                text = H.text_of(H.build(row))
                want = load_text(OracleSide, text, run=row.run)
                self.assertTrue(same(got, want), f"engine {got[1][:300]!r} vs oracle {want[1][:300]!r}")
                if row.bundle:
                    got_b = load_bundle_text(EngineSide, text, row.escaped_key)
                    want_b = load_bundle_text(OracleSide, text, row.escaped_key)
                    self.assertTrue(same(got_b, want_b), f"engine {got_b[1][:300]!r} vs oracle {want_b[1][:300]!r}")

    def test_the_size_limit_is_the_oracles(self):
        self.assertEqual(engine.load().SOD_STATE_MAX_BYTES, sod_state.MAX_BYTES)

    def test_a_text_that_is_not_unicode_is_refused(self):
        for side in (OracleSide, EngineSide):
            e = side.new()
            self.assertEqual(attempt(side, lambda e=e: e.load_sod_state('{"x":"\ud800"}')),
                             ("refused", "sod_state: the text is not valid Unicode"))


# ── Random single-field changes (a fixed seed; the property-based method with the stdlib) ────────────────────────────

SMALL = (0, 1, -1, 2, 59, 60, 61, 86400, 2**31 - 1, None, True, False, "", "x", "BUY", "SELL", "MIS", "OPEN",
         "COMPLETE", "2026-01-12", "2026-01-13", 1.5, [], {}, [1], {"a": 1}, "1", "7:60")
BIG = (2**31, 2**32 - 1, 2**32, 2**62, 2**63 - 1, 2**63, 2**64 - 1, -2**31 - 1, -2**63, 10**18, H.MIDNIGHT,
       H.MIDNIGHT - 1)


def paths(node, at: tuple = ()) -> list[tuple]:  # noqa: ANN001
    """Every place in the state: containers and leaves."""
    out = [at] if at else []
    if isinstance(node, dict):
        for k in node:
            out += paths(node[k], at + (k,))
    elif isinstance(node, list):
        for n in range(min(len(node), 3)):                 # the first rows of a long list stand for the rest
            out += paths(node[n], at + (n,))
    return out


def mutate(rng: random.Random, state: dict) -> tuple[str, bool]:
    """One change, in place. Returns what was done and whether the value put in was a small one."""
    at = rng.choice(paths(state))
    parent = state
    for k in at[:-1]:
        parent = parent[k]
    last = at[-1]
    old = parent[last]
    kind = rng.choice(("small", "small", "big", "nudge", "drop", "double", "rename"))
    where = ".".join(str(k) for k in at)
    if kind == "nudge" and isinstance(old, int) and not isinstance(old, bool):
        parent[last] = old + rng.choice((-1, 1))
        return f"{where}: {old} -> {parent[last]}", True
    if kind == "drop":
        del parent[last]
        return f"{where}: dropped", True
    if kind == "double" and isinstance(parent, list):
        parent.insert(last, copy.deepcopy(old))
        return f"{where}: doubled", True
    if kind == "rename" and isinstance(parent, dict):
        new = rng.choice(("x", "01", "999", "2:0", "2:60", "-1", " 1", "1 ", "+1", "١", "999:60", last + "0"))
        parent[new] = parent.pop(last)
        return f"{where}: key -> {new!r}", True
    if kind == "big":
        parent[last] = rng.choice(BIG)
        return f"{where}: {old!r} -> {parent[last]!r}", False
    parent[last] = copy.deepcopy(rng.choice(SMALL))
    return f"{where}: {old!r} -> {parent[last]!r}", True


def runnable(state: dict) -> bool:
    """A timer that fires every few nanoseconds is a valid state and a day that never ends: such days are not run."""
    timers = state.get("timers")
    if not isinstance(timers, list):
        return False
    for row in timers:
        if not (isinstance(row, list) and len(row) == 3 and all(type(x) is int for x in row)):
            return False
        if 0 < row[2] < 60 * 10**9:
            return False
    return True


def mutations(seed: int, count: int):  # noqa: ANN201
    rng = random.Random(seed)
    for n in range(count):
        state = H.good()
        what, small = mutate(rng, state)
        yield n, what, state, small and runnable(state)


class OracleRandomChangeTests(unittest.TestCase):
    def test_a_changed_state_loads_or_is_refused_whole(self):
        refused = accepted = 0
        for n, what, state, _run in mutations(20261109, 400):
            with self.subTest(n=n, what=what):
                text = numerics.dumps_canonical(state)
                e = OracleSide.new()
                kind, out = attempt(OracleSide, lambda e=e, text=text: e.load_sod_state(text))
                if kind == "ok":
                    accepted += 1
                    continue
                refused += 1
                self.assertTrue(out.startswith("sod_state: "), out)
                e.load_sod_state(H.GOOD_TEXT)               # the refused load left nothing behind
                self.assertEqual(e.eod_state(), H.GOOD_TEXT)
        self.assertGreater(refused, 150)
        self.assertGreater(accepted, 20)


@NEEDS_ENGINE
class EngineRandomChangeTests(unittest.TestCase):
    def test_a_changed_state_ends_the_same_way_on_both_runners(self):
        """Both refuse it with the same words, or both load it and write the same bytes; a day is then run on both
        (when the change put in no huge number: a 2**62 price overflows int64 on the engine, as it would from a pack)
        and must end in the same state."""
        ran = 0
        for n, what, state, run in mutations(20261109, 400):
            with self.subTest(n=n, what=what):
                text = numerics.dumps_canonical(state)
                oracle = load_text(OracleSide, text)
                got = load_text(EngineSide, text)
                self.assertTrue(same(got, oracle), f"engine {got[1][:300]!r} vs oracle {oracle[1][:300]!r}")
                if got[0] == "ok" and run:
                    ran += 1
                    self.assertEqual(load_text(EngineSide, text, run=True), load_text(OracleSide, text, run=True))
        self.assertGreater(ran, 20)


# ── What is not a state: the strategy's part, the guard, saving ──────────────────────────────────────────────────────

class Refuser(Keeper):
    def load_state(self, state: dict) -> None:
        raise ValueError("I cannot read this\nsecond line")


class Reentrant(Keeper):
    """Calls back into its engine from load_state and save_state (the engine is set by the test)."""

    def __init__(self, call: str):
        super().__init__()
        self.call = call
        self.engine = None
        self.seen: list[str] = []

    def _try(self) -> None:
        try:
            if self.call == "load_sod_state":
                self.engine.load_sod_state(H.GOOD_TEXT)
            else:
                getattr(self.engine, self.call)()
        except RuntimeError as e:
            self.seen.append(f"{type(e).__name__}: {e}")
        else:
            self.seen.append("allowed")

    def load_state(self, state: dict) -> None:
        self._try()
        super().load_state(state)

    def save_state(self) -> dict:
        self._try()
        return super().save_state()


class StrategyPartChecks:
    side = OracleSide

    def test_a_strategy_that_refuses_its_state_refuses_the_load(self):
        e = self.side.new(Refuser())
        kind, out = attempt(self.side, lambda: e.load_sod_state(H.GOOD_TEXT))
        self.assertEqual(kind, "refused")
        self.assertIn("strategy Refuser.load_state() raised", out)
        self.assertIn("I cannot read this", out)
        # Nothing was applied: there is still no state to write, and the day cannot have been resumed.
        self.assertEqual(attempt(self.side, e.eod_state), ("refused", "sod_state: no trading day has ended yet"))

    def test_load_and_save_refuse_calls_from_inside_a_strategy(self):
        for call in ("load_sod_state", "eod_state"):
            with self.subTest(call=call):
                s = Reentrant(call)
                e = self.side.new(s)
                s.engine = e
                e.load_sod_state(H.GOOD_TEXT)                   # → strategy.load_state → the nested call
                self.assertEqual(len(s.seen), 1, s.seen)
                self.assertIn("RunError", s.seen[0])
                self.assertIn(f"{call}: the engine is busy", s.seen[0])
                self.assertEqual(e.eod_state(), H.GOOD_TEXT)    # the outer load went through, once
                e.run()                                         # DAY_END → save_state → the nested call
                self.assertEqual(len(s.seen), 2, s.seen)
                self.assertIn(f"{call}: the engine is busy", s.seen[1])

    def test_eod_state_refuses_after_a_run_that_stopped_mid_day(self):
        class Boom(Keeper):
            def on_session_phase(self, ctx, change):  # noqa: ANN001
                raise ValueError("boom")
        e = self.side.new(Boom())
        e.load_sod_state(H.GOOD_TEXT)
        self.assertEqual(e.eod_state(), H.GOOD_TEXT)
        with self.assertRaises(Exception):                      # noqa: B017 — each runner's own wrapper of it
            e.run()
        kind, out = attempt(self.side, e.eod_state)
        self.assertEqual(kind, "refused")
        self.assertRegex(out, "events were processed after the last day ended|failed earlier")

    def test_eod_state_needs_a_finished_day(self):
        e = self.side.new()
        self.assertEqual(attempt(self.side, e.eod_state), ("refused", "sod_state: no trading day has ended yet"))
        e.load_sod_state(H.GOOD_TEXT)
        self.assertEqual(e.eod_state(), H.GOOD_TEXT)
        self.assertEqual(attempt(self.side, lambda: e.load_sod_state(H.GOOD_TEXT)),
                         ("refused", "sod_state is already loaded"))

    def test_a_state_number_outside_64_bits_cannot_be_saved(self):
        for bad in (2**64, -2**63 - 1, 10**30):
            with self.subTest(bad=bad):
                s = Keeper()
                e = self.side.new(s)
                e.load_sod_state(H.GOOD_TEXT)
                s.kept = {"a": [1, {"n": bad}]}
                kind, out = attempt(self.side, lambda e=e: (e.run(), e.eod_state())[1])
                self.assertEqual(kind, "refused")
                self.assertIn("strategy Keeper: save_state() returned state['a'][1]['n'] = an integer outside 64 bits",
                              out)

    def test_a_state_nested_past_the_limit_cannot_be_saved(self):
        for depth, ok in ((63, True), (64, False), (5000, False)):
            with self.subTest(depth=depth):
                s = Keeper()
                e = self.side.new(s)
                e.load_sod_state(H.GOOD_TEXT)
                deep: list = []
                for _ in range(depth - 1):
                    deep = [deep]
                s.kept = {"k": deep}                            # the dict, then `depth` lists
                kind, out = attempt(self.side, lambda e=e: (e.run(), e.eod_state())[1])
                if ok:
                    self.assertEqual(kind, "ok", out)
                    self.assertEqual(json.loads(out)["strategy"], s.kept)
                else:
                    self.assertEqual(kind, "refused")
                    self.assertIn("strategy Keeper: save_state() returned a state nested more than 64 levels deep",
                                  out)


class OracleStrategyPartTests(StrategyPartChecks, unittest.TestCase):
    side = OracleSide


@NEEDS_ENGINE
class EngineStrategyPartTests(StrategyPartChecks, unittest.TestCase):
    side = EngineSide

    def test_book_refuses_calls_from_inside_a_strategy(self):
        s = Reentrant("book")
        e = EngineSide.new(s)
        s.engine = e
        e.load_sod_state(H.GOOD_TEXT)
        self.assertIn("book: the engine is busy", s.seen[0])

    def test_a_failed_load_state_in_set_strategy_is_final_for_that_engine(self):
        """The state is loaded before the strategy is set (as a bundle's sod_state is): set_strategy hands it over.
        When the strategy refuses it, the engine is not left half-configured: it refuses everything from then on."""
        mod = engine.load()
        e = mod.Engine(str(H.BUNDLE_PATH))
        e.load_sod_state(H.GOOD_TEXT)
        with self.assertRaises(mod.RunError) as c:
            e.set_strategy_py(Refuser())
        self.assertIn("strategy Refuser.load_state() raised", str(c.exception))
        for go in (e.run, e.eod_state, lambda: e.set_strategy_py(Keeper()), lambda: e.load_sod_state(H.GOOD_TEXT)):
            with self.assertRaises(mod.RunError) as c:
                go()
            self.assertIn("failed earlier; start a new engine", str(c.exception))


if __name__ == "__main__":
    unittest.main()

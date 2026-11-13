"""CH 6.3: the order `note` in the C++ engine, the bridge and the carried state, byte for byte with the oracle.

Every test needs the engine (skipped when `stockify._engine` is not built). The oracle is the reference: cleaned
note, a slice child inherits its parent's, AUTO_SQOFF takes the bundle's system note, no system_notes gives "".
"""
from __future__ import annotations

import copy
import csv
import io
import json
import random
import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine, golden
from stockify.oracle import run_bundle as oracle_run
from stockify.oracle.notes import clean_note
from stockify.oracle.runner import load_records
from stockify.strategy import Strategy
from tests.stockify.test_engine_parity import _cases, _tick_world, assert_same_artifacts
from tests.stockify.test_engine_sod_state import engine_day_by_day, engine_run, split_pack
from tests.stockify.test_engine_strategies import _materialise, daily_world
from tests.stockify.test_sod_state import _run as oracle_day_run, day_bundles, run_day_by_day, slice_records

HOSTILE = ("plain", "  padded \t\n ", 'say "hi", then leave', "line1\nline2\r\nline3", "nul\x00here",
           "nel\x85 ls  ps ", "=SUM(A1)", "@x", "é ₹ 😀 ​", "x" * 500, "\ud800 lone",
           "a😀b", "", "\x7f\x1b[31mred")


def _alphabet_note(rng: random.Random) -> str:
    pool = ['"', ",", "\n", "\r", "\x00", "\x85", " ", "\ud800", "é", "😀", " ", "a", "=", "\t", "\x7f", "₹"]
    return "".join(rng.choice(pool) for _ in range(rng.randint(0, 40)))


class _Noter(Strategy):
    """On each bar: a spread of orders carrying notes; on_session_end: a pending order with a note."""

    def __init__(self, notes, big=None, sqoff=False):  # noqa: ANN001
        self.notes, self.big, self.sqoff, self.n = list(notes), big, sqoff, 0

    def on_start(self, ctx):  # noqa: ANN001
        self.iid = ctx.resolve("NSE:TEST")
        ctx.subscribe_bars(self.iid, 86400)

    def _note(self) -> str:
        n = self.notes[self.n % len(self.notes)]
        self.n += 1
        return n

    def on_bar(self, ctx, bar):  # noqa: ANN001
        i = self.iid
        ctx.place(i, "BUY", 1, "CNC", "MARKET", note=self._note())
        ctx.place(i, "BUY", 1, "CNC", "LIMIT", price=bar.close, validity="IOC", note=self._note())
        ctx.place(i, "SELL", 1, "BOGUS", "MARKET", note=self._note())            # rejected
        ctx.place(i, "BUY", 1, "CNC", "MARKET", tag="t", note=self._note())
        oid = ctx.place(i, "BUY", 1, "CNC", "LIMIT", price=max(5, bar.close // 2 // 5 * 5), note=self._note())
        ctx.modify(oid, price=max(5, bar.close // 3 // 5 * 5))
        if self.big:
            ctx.place(i, "BUY", self.big, "CNC", "MARKET", note=self._note())    # past the freeze: sliced
        if self.sqoff:
            ctx.place(i, "BUY", 1, "MIS", "MARKET", note=self._note())

    def on_session_end(self, ctx):  # noqa: ANN001
        ctx.place(self.iid, "BUY", 1, "CNC", "LIMIT", price=5, note=self._note())

    def save_state(self) -> dict:
        return {"n": self.n, "iid": self.iid}

    def load_state(self, state: dict) -> None:
        self.n, self.iid = state["n"], state["iid"]


def _rows(path: Path) -> tuple[list[str], list[list[str]]]:
    rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"), newline="")))
    return rows[0], rows[1:]


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class NoteParityTests(unittest.TestCase):
    def world(self, seed=7100, freeze=None, system_notes=True):  # noqa: ANN001, ANN202
        g, _ = daily_world(seed)
        if freeze:
            for i in g["bundle"]["instruments"]:
                for s in i["specs"]:
                    s["freeze_qty"] = freeze
        tmp = tempfile.TemporaryDirectory(prefix="engine-note-")
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        pack, b = _materialise(g, {}, folder)
        if not system_notes:
            b.pop("system_notes", None)
        return folder, pack, b

    def both(self, folder, pack, b, make):  # noqa: ANN001, ANN202
        oracle_run(b, [pack], folder / "oracle", strategy=make(), report=False)
        engine.run_bundle(b, [pack], folder / "engine", strategy=make(), report=False)
        return folder / "oracle", folder / "engine"

    def test_engine_and_oracle_write_identical_orders_csv_with_notes(self):
        folder, pack, b = self.world(freeze=3)
        o, e = self.both(folder, pack, b, lambda: _Noter(HOSTILE, big=10, sqoff=True))
        assert_same_artifacts(self, o, e)
        header, rows = _rows(e / "orders.csv")
        self.assertEqual(header[-1], "note")
        self.assertTrue(any(r[1] != "0" and r[-1] for r in rows), "a slice child carries its parent's note")
        self.assertTrue(any(r[14] == "REJECTED" and r[-1] for r in rows))

    def golden_pair(self, name, system_notes):  # noqa: ANN001, ANN202
        g = dict(_cases())[name]
        tmp = tempfile.TemporaryDirectory(prefix="engine-note-g-")
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        pack = golden.build_pack(g, folder)
        b = golden.build_bundle(g)
        if not system_notes:
            b.pop("system_notes", None)
        oracle_run(b, [pack], folder / "oracle", report=False)
        engine.run_bundle(b, [pack], folder / "engine", report=False)
        assert_same_artifacts(self, folder / "oracle", folder / "engine")
        return [r for r in _rows(folder / "engine" / "orders.csv")[1] if r[11] == "AUTO_SQOFF"]

    def test_auto_sqoff_takes_the_bundles_system_note(self):
        for name in ("G04", "G21"):
            with self.subTest(golden=name):
                sq = self.golden_pair(name, True)
                self.assertTrue(sq)
                self.assertTrue(all(r[-1] == "Closed by the broker's 15:20 auto square-off" for r in sq), sq)

    def test_a_bundle_without_system_notes_gives_an_empty_auto_sqoff_note(self):
        for name in ("G04", "G21"):
            with self.subTest(golden=name):
                sq = self.golden_pair(name, False)
                self.assertTrue(sq)
                self.assertTrue(all(r[-1] == "" for r in sq))

    def test_a_snapshot_run_agrees_too(self):
        class Ticker(Strategy):
            def __init__(self):
                self.n = 0

            def on_start(self, ctx):  # noqa: ANN001
                self.iid = ctx.resolve("NSE:AAA")
                ctx.subscribe_ticks(self.iid)

            def on_tick(self, ctx, tick):  # noqa: ANN001
                self.n += 1
                if self.n % 5 == 0 and self.n < 200:
                    ctx.place(self.iid, "BUY", 1, "CNC", "MARKET", note=HOSTILE[self.n // 5 % len(HOSTILE)])
        for seed in (3302, 3303, 3304):
            g = _tick_world(seed)
            with self.subTest(seed=seed), tempfile.TemporaryDirectory(prefix="engine-note-snap-") as tmp:
                folder = Path(tmp)
                pack = golden.build_pack(g, folder)
                b = golden.build_bundle(g)
                self.assertNotEqual(b["run"]["fill_model"]["kind"], "bar")
                oracle_run(b, [pack], folder / "oracle", strategy=Ticker(), report=False)
                engine.run_bundle(b, [pack], folder / "engine", strategy=Ticker(), report=False)
                assert_same_artifacts(self, folder / "oracle", folder / "engine")
                _, rows = _rows(folder / "engine" / "orders.csv")
                self.assertGreater(sum(1 for r in rows if r[-1]), 1)

    def test_orders_csv_has_one_row_per_order_with_hostile_notes(self):
        rng = random.Random(1630)
        notes = list(HOSTILE) + [_alphabet_note(rng) for _ in range(150)]
        folder, pack, b = self.world()
        o, e = self.both(folder, pack, b, lambda: _Noter(notes))
        assert_same_artifacts(self, o, e)
        text = (e / "orders.csv").read_bytes()
        header, rows = _rows(e / "orders.csv")
        self.assertEqual({len(r) for r in rows}, {len(header)})
        ids = [int(r[0]) for r in rows]
        self.assertEqual(ids, list(range(1, len(ids) + 1)))
        want = {clean_note(n) for n in notes}
        for r in rows:
            self.assertIn(r[-1], want)
        self.assertNotIn(b"\r", text)
        self.assertGreater(len(rows), 150)

    def test_note_with_a_nul_in_a_python_strategy_is_cleaned_by_the_bridge(self):
        folder, pack, b = self.world()
        _, e = self.both(folder, pack, b, lambda: _Noter(["a\x00b", "\ud800", "x y"]))
        notes = {r[-1] for r in _rows(e / "orders.csv")[1]}
        self.assertTrue({"a b", "�", "x y"} <= notes, notes)

    def test_a_tag_with_a_carriage_return_is_quoted_alike_and_keeps_one_row(self):
        class Tagged(Strategy):
            def on_start(self, ctx):  # noqa: ANN001
                ctx.subscribe_bars(ctx.resolve("NSE:TEST"), 86400)

            def on_bar(self, ctx, bar):  # noqa: ANN001
                ctx.place(bar.instrument, "BUY", 1, "CNC", "MARKET", tag="a\rb,c", note="n")
        folder, pack, b = self.world()
        o, e = self.both(folder, pack, b, Tagged)
        assert_same_artifacts(self, o, e)
        raw = (e / "orders.csv").read_bytes()
        self.assertIn(b'"a\rb,c"', raw)
        header, rows = _rows(e / "orders.csv")
        self.assertEqual({len(r) for r in rows}, {len(header)})

    def test_a_note_must_be_text_on_the_engine_as_on_the_oracle(self):
        class Bad(Strategy):
            def on_start(self, ctx):  # noqa: ANN001
                ctx.subscribe_bars(ctx.resolve("NSE:TEST"), 86400)

            def on_bar(self, ctx, bar):  # noqa: ANN001
                ctx.place(bar.instrument, "BUY", 1, "CNC", "MARKET", note=b"bytes")
        folder, pack, b = self.world()
        errs = []
        for go, out in ((oracle_run, "oracle"), (engine.run_bundle, "engine")):
            with self.assertRaises(TypeError) as c:
                go(b, [pack], folder / out, strategy=Bad(), report=False)
            errs.append(str(c.exception))
        self.assertEqual(errs[0], errs[1])
        self.assertIn("note must be text", errs[0])

    def test_tag_and_note_are_keyword_only_on_both(self):
        class Pos(Strategy):
            def on_start(self, ctx):  # noqa: ANN001
                ctx.subscribe_bars(ctx.resolve("NSE:TEST"), 86400)

            def on_bar(self, ctx, bar):  # noqa: ANN001
                ctx.place(bar.instrument, "BUY", 1, "CNC", "MARKET", 0, 0, "DAY", "AUTO", "tag")
        folder, pack, b = self.world()
        for go, out in ((oracle_run, "oracle"), (engine.run_bundle, "engine")):
            with self.subTest(impl=out), self.assertRaises(TypeError):
                go(b, [pack], folder / out, strategy=Pos(), report=False)


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class NoteStateTests(unittest.TestCase):
    def setUp(self):
        g, _ = daily_world(7200)
        tmp = tempfile.TemporaryDirectory(prefix="engine-note-state-")
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.pack, self.bundle = _materialise(g, {}, self.folder)
        self.records = load_records([self.pack])
        self.make = lambda: _Noter(HOSTILE)

    def test_sod_state_carries_notes_byte_for_byte(self):
        o_states = run_day_by_day(self.bundle, self.records, self.make, self.folder / "o")
        e_states = engine_day_by_day(self.bundle, self.pack, self.make, self.folder / "e")
        self.assertEqual(o_states, e_states)
        self.assertTrue(any(o["note"] for s in o_states for o in json.loads(s)["pending_orders"]))
        self.assertEqual((self.folder / "o" / "parts" / "orders.csv").read_bytes(),
                         (self.folder / "e" / "parts" / "orders.csv").read_bytes())

    def test_a_state_moves_between_the_runners_in_both_directions(self):
        days = day_bundles(self.bundle)
        parts = slice_records(self.records, self.bundle["days"])
        packs = split_pack(self.pack, self.bundle["days"], self.folder)
        for first_oracle in (True, False):
            state, out = None, []
            for i, d in enumerate(days):
                d["sod_state"] = json.loads(state) if state is not None else None
                f = self.folder / f"x{first_oracle}-{i}"
                if (i % 2 == 0) == first_oracle:
                    state = oracle_day_run(d, parts[i], self.make(), f).eod_state()
                else:
                    state = engine_run(d, packs[i], self.make(), f)
                out.append(state)
            ref = run_day_by_day(self.bundle, self.records, self.make, self.folder / f"r{first_oracle}")
            self.assertEqual(out, ref, f"first_oracle={first_oracle}")

    def _first_state_with_orders(self):  # noqa: ANN202
        states = run_day_by_day(self.bundle, self.records, self.make, self.folder / "src")
        for n, s in enumerate(states):
            st = json.loads(s)
            if st["pending_orders"]:
                return n, st
        self.fail("no state with a pending order")

    def test_pending_order_note_survives_the_day_boundary(self):
        _, st = self._first_state_with_orders()
        self.assertTrue(any(o["note"] for o in st["pending_orders"]))
        e_states = engine_day_by_day(self.bundle, self.pack, self.make, self.folder / "e2")
        self.assertTrue(any(o["note"] for s in e_states for o in json.loads(s)["pending_orders"]))

    def test_a_carried_note_with_control_characters_and_over_200_characters_is_cleaned_on_load(self):
        """CH 6.22: both runners pass a loaded note through clean_note (control characters, U+2028, the 200-character
        cap), so a hand-edited or old state cannot put a raw note into orders.csv."""
        n, st = self._first_state_with_orders()
        dirty = ["  start\x01mid\x7f end\n  ", "z" * 300, "\x00\x1f", "plain note",
                 "\u202eevil\u202c note \u2066x\u2069 \u200f\u061c"]
        want = {}
        for i, o in enumerate(st["pending_orders"]):
            o["note"] = dirty[i % len(dirty)]
            want[o["order_id"]] = clean_note(o["note"])
        self.assertTrue(any(clean_note(x) != x for x in dirty))
        days = day_bundles(self.bundle)
        parts = slice_records(self.records, self.bundle["days"])
        packs = split_pack(self.pack, self.bundle["days"], self.folder)
        d = days[n + 1]
        d["sod_state"] = st
        o_state = oracle_day_run(copy.deepcopy(d), parts[n + 1], self.make(), self.folder / "oc").eod_state()
        e_state = engine_run(copy.deepcopy(d), packs[n + 1], self.make(), self.folder / "ec")
        self.assertEqual(o_state, e_state)
        seen = 0
        for s in (o_state, e_state):
            for group in ("pending_orders", "final_orders"):
                for o in json.loads(s)[group]:
                    if o["order_id"] in want:
                        seen += 1
                        self.assertEqual(o["note"], want[o["order_id"]])
        self.assertGreater(seen, 0)

    def test_state_without_note_loads_as_empty_note_in_both_runners(self):
        n, st = self._first_state_with_orders()
        for group in ("pending_orders", "final_orders"):
            for o in st[group]:
                o.pop("note")
        days = day_bundles(self.bundle)
        parts = slice_records(self.records, self.bundle["days"])
        packs = split_pack(self.pack, self.bundle["days"], self.folder)
        d = days[n + 1]
        d["sod_state"] = st
        o_state = oracle_day_run(copy.deepcopy(d), parts[n + 1], self.make(), self.folder / "oo").eod_state()
        e_state = engine_run(copy.deepcopy(d), packs[n + 1], self.make(), self.folder / "ee")
        self.assertEqual(o_state, e_state)
        for s in (o_state, e_state):
            for group in ("pending_orders", "final_orders"):
                for o in json.loads(s)[group]:
                    self.assertIn("note", o)


if __name__ == "__main__":
    unittest.main()

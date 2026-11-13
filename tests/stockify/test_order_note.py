"""CH 6.2: every order carries the strategy's own `note`, written to orders.csv by the oracle (the engine half is C3)."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import random
import shutil
import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import bundle as bundle_mod, oracle, paths, runconfig, runs
from stockify.config import loader
from stockify.oracle import orders as O
from stockify.oracle.notes import clean_note
from stockify.oracle.runner import Engine, load_records
from stockify.strategy import Strategy
from tests.stockify.test_bundle import run
from tests.stockify.test_runs_launch import BUY, RunsTestBase, _cli

NOTE = "Bought 50: price 2,418.40 broke above 2,412.50"


class Placer(Strategy):
    """Places `plan` = [(side, qty, product, kwargs)] on the first daily bar of RELIANCE."""

    def __init__(self, plan):  # noqa: ANN001
        self.plan, self.done = plan, False

    def on_start(self, ctx):  # noqa: ANN001
        self.iid = ctx.resolve("NSE:RELIANCE")
        ctx.subscribe_bars(self.iid, 86400)

    def on_bar(self, ctx, bar):  # noqa: ANN001
        if self.done:
            return
        self.done = True
        for side, qty, product, kw in self.plan:
            ctx.place(self.iid, side, qty, product, "MARKET", **kw)


class NoteBase(RunsTestBase):
    def bundle(self, **kw):  # noqa: ANN003, ANN202
        return bundle_mod.resolve_bundle(runconfig.load(run(**kw)))

    def run_bundle(self, b, strategy, name="n"):  # noqa: ANN001, ANN202
        folder = paths.run_path(name)
        oracle.run_bundle(b, [paths.lab_dir() / p["path"] for p in b["packs"]], folder, strategy=strategy,
                          report=False)
        return folder

    def run_plan(self, plan, mutate=None, name="n"):  # noqa: ANN001, ANN202
        b = self.bundle()
        if mutate:
            mutate(b)
        return self.run_bundle(b, Placer(plan), name)

    @staticmethod
    def orders(folder: Path) -> tuple[list[str], list[dict]]:
        with (folder / "orders.csv").open(encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        return rows[0], [dict(zip(rows[0], r)) for r in rows[1:]]


class NoteInOrdersCsvTests(NoteBase):
    def test_place_keeps_a_cleaned_note_in_orders_csv(self):
        folder = self.run_plan([("BUY", 5, "CNC", {"note": "  " + NOTE + "\n"})])
        header, rows = self.orders(folder)
        self.assertEqual(header[-2:], ["reason", "note"])
        self.assertEqual([r["note"] for r in rows], [NOTE])

    def test_no_note_is_an_empty_cell_not_null(self):
        folder = self.run_plan([("BUY", 5, "CNC", {})])
        text = (folder / "orders.csv").read_text(encoding="utf-8")
        self.assertTrue(text.splitlines()[1].endswith(","))
        self.assertNotIn("None", text)
        self.assertEqual(self.orders(folder)[1][0]["note"], "")

    def test_sliced_children_inherit_the_parents_note_and_keep_their_own(self):
        def freeze(b):  # noqa: ANN001, ANN202
            for i in b["instruments"]:
                if i["id"] == self.rel:
                    for s in i["specs"]:
                        s["freeze_qty"] = 10
        folder = self.run_plan([("BUY", 25, "CNC", {"note": "parent says"}), ("BUY", 3, "CNC", {"note": "own"})],
                               freeze)
        _, rows = self.orders(folder)
        parent = [r for r in rows if r["status"] == "SLICED"]
        kids = [r for r in rows if r["parent_id"] != "0"]
        self.assertEqual(len(parent), 1)
        self.assertEqual([r["qty"] for r in kids], ["10", "10", "5"])
        self.assertEqual({r["note"] for r in kids}, {"parent says"})
        self.assertEqual(parent[0]["note"], "parent says")
        self.assertEqual([r["note"] for r in rows if r["parent_id"] == "0" and r["status"] != "SLICED"], ["own"])

    def test_a_child_with_an_empty_note_takes_its_parents(self):
        eng, _ = SystemNotesTests._engine(self)
        eng.now = eng.days[0]["groups"]["CM"][0]["start_ns"]
        o = eng.place(self.rel, "BUY", 3, "CNC", "MARKET", note="from parent")
        self.assertEqual(o.note, "from parent")
        child = eng.place(self.rel, "BUY", 2, "CNC", "MARKET", parent=o.order_id)
        self.assertEqual(child.note, "")                      # only the slice recursion passes the parent's note

    def test_rejected_order_keeps_its_note(self):
        folder = self.run_plan([("BUY", 5, "XYZ", {"note": "why I tried"})])
        _, rows = self.orders(folder)
        self.assertEqual([(r["status"], r["reason"], r["note"]) for r in rows],
                         [("REJECTED", "R_PRODUCT", "why I tried")])

    def test_every_fill_of_one_order_belongs_to_one_note(self):
        folder = self.run_plan([("BUY", 5, "CNC", {"note": "first"}), ("BUY", 6, "CNC", {"note": "second"})])
        _, rows = self.orders(folder)
        by_id = {r["order_id"]: r["note"] for r in rows}
        with (folder / "fills.csv").open(encoding="utf-8", newline="") as f:
            fills = list(csv.DictReader(f))
        self.assertTrue(fills)
        self.assertEqual({by_id[f["order_id"]] for f in fills}, {"first", "second"})
        self.assertNotIn("note", fills[0])                        # a join through order_id, not a copy

    def test_note_must_be_text(self):
        for bad in (None, 5, b"x", ["a"]):
            with self.subTest(bad=bad):
                err = []

                class Bad(Placer):
                    def on_bar(self, ctx, bar):  # noqa: ANN001
                        if not err:
                            try:
                                ctx.place(self.iid, "BUY", 1, "CNC", "MARKET", note=bad)
                            except TypeError as e:
                                err.append(str(e))
                folder = self.run_bundle(self.bundle(), Bad([]), "bad")
                self.assertEqual(err, ["ctx.place: note must be text"])
                self.assertEqual(self.orders(folder)[1], [])      # validated before anything was placed
                shutil.rmtree(folder)


class SystemNotesTests(NoteBase):
    def _engine(self, mutate=None):  # noqa: ANN001, ANN202
        b = self.bundle()
        if mutate:
            mutate(b)
        eng = Engine(b, load_records([paths.lab_dir() / p["path"] for p in b["packs"]]), Placer([]))
        return eng, b

    def _sqoff(self, mutate=None):  # noqa: ANN001, ANN202
        eng, b = self._engine(mutate)
        eng.now = b["days"][0]["groups"]["CM"][0]["start_ns"]
        eng.book.ledger.apply_fill(self.rel, "MIS", "premium", "BUY", 5, 100)
        eng._mis_sqoff(("CM", b["days"][0]["date"]))
        return eng

    def test_the_bundle_carries_the_shipped_system_notes(self):
        self.assertEqual(self.bundle()["system_notes"], loader.load_json("system_notes.json")["notes"])

    def test_auto_sqoff_order_gets_the_system_note_from_the_bundle(self):
        eng = self._sqoff()
        sq = [o for o in eng.book.orders.values() if o.tag == "AUTO_SQOFF"]
        self.assertEqual([o.note for o in sq], ["Closed by the broker's 15:20 auto square-off"])

    def test_bundle_without_system_notes_gives_empty_system_note(self):
        eng = self._sqoff(lambda b: b.pop("system_notes"))
        sq = [o for o in eng.book.orders.values() if o.tag == "AUTO_SQOFF"]
        self.assertEqual([o.note for o in sq], [""])

    def test_system_notes_loader_rejects_unknown_tag_and_uncleaned_text(self):
        good = loader.load_json("system_notes.json")
        self.assertEqual(set(good["notes"]), set(O.SYSTEM_TAGS) | {"SETTLEMENT"})
        self.assertEqual(good["notes"]["SETTLEMENT"], "Settled at expiry at {price}")
        cases = {"unknown tag": {"NOPE": "x"}, "padded": {"AUTO_SQOFF": " x"}, "newline": {"AUTO_SQOFF": "a\nb"},
                 "not text": {"AUTO_SQOFF": 5}, "too long": {"AUTO_SQOFF": "y" * 201},
                 "placeholder on a non-settlement": {"AUTO_SQOFF": "at {price}"},
                 "settlement without placeholder": {"SETTLEMENT": "Settled"},
                 "settlement with two": {"SETTLEMENT": "{price} {price}"},
                 "settlement with another brace": {"SETTLEMENT": "{price} {x}"}}
        for name, notes in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as d:
                (Path(d) / "system_notes.json").write_text(
                    json.dumps({"schema": "stockify.system_notes/1", "notes": {**good["notes"], **notes}}),
                    encoding="utf-8")
                with self.assertRaisesRegex(loader.ConfigError, "system_notes"):
                    loader.load_json("system_notes.json", Path(d))


class HostileNotesTests(NoteBase):
    ALPHABET = list("abcXYZ019 ,\"'=+-@;|\\/\t\n\r\x00\x0b\x85  …₹\U0001f600\ud800") + ["\r\n", '""']

    def test_orders_csv_has_one_row_per_order_with_hostile_notes(self):
        rng = random.Random(1620)
        notes = ["", "=1+1", "@SUM(A1)", '"', ",", "\n", "a,b\r\nc", "x" * 500, "é" * 250] + [
            "".join(rng.choice(self.ALPHABET) for _ in range(rng.randint(0, 60))) for _ in range(150)]
        folder = self.run_plan([("BUY", 1, "CNC", {"note": n}) for n in notes])
        raw = (folder / "orders.csv").read_bytes()
        with (folder / "orders.csv").open(encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(len(rows) - 1, len(notes))
        self.assertTrue(all(len(r) == len(rows[0]) for r in rows))
        self.assertEqual([r[-1] for r in rows[1:]], [clean_note(n) for n in notes])
        self.assertEqual([int(r[0]) for r in rows[1:]], list(range(1, len(notes) + 1)))
        self.assertNotIn(b"\r", raw)                              # clean_note blanked every line break


def _to_old_orders(folder: Path) -> None:
    """Rewrite a run folder's orders.csv as it was before CH 6.2 (no `note` column), and its hash."""
    path = folder / "orders.csv"
    rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"), newline="")))
    path.write_bytes(("\n".join(",".join(r[:-1]) for r in rows) + "\n").encode("utf-8"))
    det = folder / "determinism.json"
    hashes = json.loads(det.read_text(encoding="utf-8"))
    hashes["orders.csv"] = hashlib.sha256(path.read_bytes()).hexdigest()
    hashes.pop("plots.csv", None)
    det.write_text(json.dumps(hashes, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class OldRunsTests(NoteBase):
    def test_old_orders_csv_without_note_column_still_imports(self):
        rid = runs.launch(self.config(BUY, "old"), impl="oracle", report=False)
        folder = paths.run_path(rid)
        _to_old_orders(folder)
        from stockify import store
        runs.import_run(folder)
        rows = store.run_rows(rid, "orders")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["note"], "")

    def test_a_new_run_with_no_note_imports_an_empty_string(self):
        rid = runs.launch(self.config(BUY, "nonote"), impl="oracle", report=False)
        from stockify import store
        runs.import_run(paths.run_path(rid))
        self.assertEqual(store.run_rows(rid, "orders")[0]["note"], "")

    def test_a_cell_with_a_carriage_return_is_quoted(self):
        from stockify.oracle import artifacts
        self.assertEqual(artifacts._cell("a\rb"), '"a\rb"')
        self.assertEqual(artifacts._cell("a b"), "a b")

    def test_the_step_and_note_additions_compose_in_one_rerun(self):
        from tests.stockify.test_runs_rerun import to_old_margin_file
        rid = runs.launch(self.config(BUY, "olc"), impl="oracle", report=False)
        folder = paths.run_path(rid)
        to_old_margin_file(folder)
        _to_old_orders(folder)
        code, _, err = _cli("run", "--rerun", rid)
        self.assertEqual(code, 0, err)
        self.assertIn("margin_daily.csv gained the step column, D-64; orders.csv gained the note column, D-92", err)
        self.assertNotIn("not reproduced", err)

    def test_rerun_of_an_old_run_reports_the_new_column_not_a_failure(self):
        rid = runs.launch(self.config(BUY, "olr"), impl="oracle", report=False)
        _to_old_orders(paths.run_path(rid))
        code, out, err = _cli("run", "--rerun", rid)
        self.assertEqual(code, 0, err)
        new = f"{rid}-rerun"
        self.assertIn(f"reproduced (orders.csv gained the note column, D-92; plots.csv is new, D-94) ({rid} -> {new})",
                      err)
        # determinism_diff itself is not weakened: it still lists both files
        self.assertEqual(runs.determinism_diff(rid, new), ["orders.csv", "plots.csv"])
        self.assertTrue(runs.note_column_only(rid, new))

    def test_rerun_of_a_run_that_only_lacks_plots_csv_is_reproduced_with_a_new_file(self):
        rid = runs.launch(self.config(BUY, "olp"), impl="oracle", report=False)
        det = paths.run_path(rid) / "determinism.json"
        hashes = json.loads(det.read_text(encoding="utf-8"))
        self.assertIn("plots.csv", hashes)
        hashes.pop("plots.csv")                                        # a folder saved between CH 6.2 and CH 6.4
        det.write_text(json.dumps(hashes, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (paths.run_path(rid) / "plots.csv").unlink()
        code, _, err = _cli("run", "--rerun", rid)
        new = f"{rid}-rerun"
        self.assertEqual(code, 0, err)
        self.assertIn(f"reproduced (plots.csv is new, D-94) ({rid} -> {new})", err)
        self.assertNotIn("not reproduced", err)
        self.assertEqual(runs.determinism_diff(rid, new), ["plots.csv"])
        self.assertFalse(runs.note_column_only(rid, new))              # no note column was added here
        # a plots.csv the old folder lists is never excused: a changed hash is a real difference
        hashes["plots.csv"] = "0" * 64
        det.write_text(json.dumps(hashes, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.assertEqual(runs.column_changes(rid, new), [])

    def test_any_other_difference_is_not_tolerated(self):
        rid = runs.launch(self.config(BUY, "olx"), impl="oracle", report=False)
        new_id, _ = runs.rerun(rid)
        self.assertFalse(runs.note_column_only(rid, new_id))               # identical: nothing to excuse
        _to_old_orders(paths.run_path(rid))
        self.assertTrue(runs.note_column_only(rid, new_id))
        det = paths.run_path(rid) / "determinism.json"
        h = json.loads(det.read_text(encoding="utf-8"))
        h["fills.csv"] = "x"
        det.write_text(json.dumps(h), encoding="utf-8")
        self.assertFalse(runs.note_column_only(rid, new_id))
        p = paths.run_path(rid) / "orders.csv"                              # a changed value, not only the column
        _to_old_orders(paths.run_path(rid))
        text = p.read_text(encoding="utf-8").replace(",CANCELLED,", ",COMPLETE,")
        p.write_bytes(text.encode("utf-8"))
        h["fills.csv"] = json.loads((paths.run_path(new_id) / "determinism.json").read_text())["fills.csv"]
        h["orders.csv"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        det.write_text(json.dumps(h), encoding="utf-8")
        self.assertFalse(runs.note_column_only(rid, new_id))


if __name__ == "__main__":
    unittest.main()

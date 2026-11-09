"""M3.11 / A3 DT-03: `python -m stockify run --rerun <id>` from the archived bundle and packs reproduces
determinism.json (I-8)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

import stockify.store as store
from stockify import engine, paths, runs
from tests.stockify.test_runs_launch import BUY, RunsTestBase, _cli


def to_old_margin_file(folder: Path) -> None:
    """Rewrite a run folder's margin_daily.csv as it was before D-64 (no `step` column), and its hash."""
    import csv
    import hashlib
    import io
    path = folder / "margin_daily.csv"
    rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"))))
    step = rows[0].index("step")
    lines = [",".join(r[:step] + r[step + 1:]) for r in rows]
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    det = folder / "determinism.json"
    hashes = json.loads(det.read_text(encoding="utf-8"))
    hashes["margin_daily.csv"] = hashlib.sha256(path.read_bytes()).hexdigest()
    det.write_text(json.dumps(hashes, indent=2, sort_keys=True) + "\n", encoding="utf-8")


OLD_HEADER = "trade_date,underlying_id,model,scan,somc,elm,addons,total\n"
OLD_ROWS = "2026-11-09,7,staircase,100,10,5,0,105\n2026-11-10,7,staircase,200,0,0,0,200\n"


class StepColumnOnlyTests(tests.support.SandboxedDBTest):
    """runs.step_column_only on hand-made folders with margin rows."""

    NEW = ("trade_date,underlying_id,model,step,scan,somc,elm,addons,total\n"
           "2026-11-09,7,staircase,B1_span_nse_elm,100,10,5,0,105\n"
           "2026-11-10,7,staircase,B5_flat,200,0,0,0,200\n")

    def folder(self, name: str, margin: str, fills: str = "f") -> str:
        import hashlib
        d = paths.run_dir(name)
        (d / "margin_daily.csv").write_bytes(margin.encode("utf-8"))
        hashes = {"fills.csv": fills, "margin_daily.csv": hashlib.sha256(margin.encode("utf-8")).hexdigest()}
        (d / "determinism.json").write_text(json.dumps(hashes), encoding="utf-8")
        return name

    def test_only_the_step_column(self):
        self.assertTrue(runs.step_column_only(self.folder("old", OLD_HEADER + OLD_ROWS), self.folder("new", self.NEW)))

    def test_any_other_difference_is_not_tolerated(self):
        new = self.folder("new", self.NEW)
        for name, text, fills in (
                ("value", OLD_HEADER + OLD_ROWS.replace("105", "106"), "f"),
                ("row", OLD_HEADER + OLD_ROWS.split("\n")[0] + "\n", "f"),
                ("other-file", OLD_HEADER + OLD_ROWS, "g"),
                ("same-header", self.NEW.replace("105", "106"), "f")):
            with self.subTest(name):
                self.assertFalse(runs.step_column_only(self.folder(f"old-{name}", text, fills), new))

    def test_an_old_file_that_is_not_the_one_its_hash_names_is_not_tolerated(self):
        old = self.folder("old", OLD_HEADER + OLD_ROWS)
        (paths.run_path(old) / "margin_daily.csv").write_bytes((OLD_HEADER + OLD_ROWS).encode() + b"\n")
        self.assertFalse(runs.step_column_only(old, self.folder("new", self.NEW)))


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class RerunTests(RunsTestBase):
    def test_dt03_a_rerun_reproduces_determinism_json(self):
        for impl in ("engine", "oracle"):
            with self.subTest(impl=impl):
                rid = runs.launch(self.config(BUY, f"t{impl}"), impl=impl, report=False)
                new_id, same = runs.rerun(rid)
                self.assertTrue(same)
                self.assertEqual(new_id, f"{rid}-rerun")
                old, new = store.get_run(rid), store.get_run(new_id)
                self.assertEqual((new["status"], new["impl"], new["bundle_sha256"]), ("done", impl, old["bundle_sha256"]))
                self.assertEqual((paths.runs_dir() / new_id / "bundle.json").read_bytes(),
                                 (paths.runs_dir() / rid / "bundle.json").read_bytes())
                self.assertEqual(store.run_rows(new_id, "orders"), store.run_rows(rid, "orders"))
                self.assertEqual(runs.rerun(rid)[0], f"{rid}-rerun-2")

    def test_the_cli_names_the_file_that_differs(self):
        rid = runs.launch(self.config(BUY), report=False)
        code, out, err = _cli("run", "--rerun", rid)
        self.assertEqual(code, 0, err)
        self.assertEqual(Path(out.strip()), paths.runs_dir() / f"{rid}-rerun")
        self.assertIn("reproduced", err)
        det = paths.runs_dir() / rid / "determinism.json"
        hashes = json.loads(det.read_text(encoding="utf-8"))
        hashes["fills.csv"] = "0" * 64
        det.write_text(json.dumps(hashes, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        code, _, err = _cli("run", "--rerun", rid)
        self.assertEqual(code, 1)
        self.assertEqual([x for x in err.splitlines() if x.startswith("differs:")], ["differs: fills.csv"])

    def test_a_run_saved_before_the_step_column_reproduces_with_a_note(self):
        """I-3 / D-64: an old folder's margin_daily.csv has no `step` column. When that column is the only
        difference, the re-run is reported reproduced, naming the cause, and exits 0."""
        rid = runs.launch(self.config(BUY), report=False)
        to_old_margin_file(paths.runs_dir() / rid)
        code, _, err = _cli("run", "--rerun", rid)
        self.assertEqual(code, 0, err)
        self.assertIn("reproduced (margin_daily.csv gained the step column, D-64)", err)
        self.assertNotIn("differs:", err)
        det = paths.runs_dir() / rid / "determinism.json"                 # and anything else still fails
        hashes = json.loads(det.read_text(encoding="utf-8"))
        hashes["fills.csv"] = "0" * 64
        det.write_text(json.dumps(hashes, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        code, _, err = _cli("run", "--rerun", rid)
        self.assertEqual(code, 1)
        self.assertIn("not reproduced", err)
        self.assertEqual(sorted(x for x in err.splitlines() if x.startswith("differs:")),
                         ["differs: fills.csv", "differs: margin_daily.csv"])

    def test_the_archived_bundle_is_used_not_a_fresh_one(self):
        rid = runs.launch(self.config(BUY), report=False)
        from stockify.data import history
        with history._Tx() as conn:  # the bar cache changes after the run: a re-resolved bundle would differ
            conn.execute("UPDATE bars SET close_paise = close_paise + 1")
        self.assertTrue(runs.rerun(rid)[1])

    def test_a_missing_pack_or_folder_is_named(self):
        rid = runs.launch(self.config(BUY), report=False)
        bundle = json.loads((paths.runs_dir() / rid / "bundle.json").read_text(encoding="utf-8"))
        pack = paths.lab_dir() / bundle["packs"][0]["path"]
        pack.unlink()
        with self.assertRaisesRegex(runs.RunFailed, "pack file missing: .*" + pack.name):
            runs.rerun(rid)
        code, _, err = _cli("run", "--rerun", "no-such-run")
        self.assertEqual(code, 1)
        self.assertIn("bundle.json is missing", err)


if __name__ == "__main__":
    unittest.main()

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

"""M3 review fix (RD 4.1): a run's `name` (and anything else that becomes a run id) can never point a run folder
outside `runs_dir()`. Every run folder goes through `paths.run_path`, which checks the resolved parent."""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import stockify.store as store
from stockify import __main__ as cli
from stockify import paths, runconfig, runs

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = json.loads((REPO / "examples" / "runs" / "sma_reliance_daily.json").read_text(encoding="utf-8"))

BAD_NAMES = ["..\\..\\x", "../x", "..", ".", "C:\\Temp\\x", "C:x", "/abs", "a/b", "a\\b", "", " x", "x ", "a b",
             ".hidden", "-x", "x.", "x" * 65, "nul", "CON", "con.backup", "Aux", "COM1", "lpt9.x", "x\u00e9", "x:y"]
GOOD_NAMES = ["sma_reliance_daily", "t", "A-1.2_b", "x" * 64, "con2", "console", "nullish"]
BAD_FAMILIES = ["../x", "a//b", "/abs", "a/", "C:\\x", "a\\b", "a/../b", "a/./b", "a b/c", "x" * 129]
GOOD_FAMILIES = [None, "sma_cross/RELIANCE/daily", "eod_short_straddle/NIFTY/weekly", "f", "a.b/c-d/e_f"]


def _config(**kw) -> dict:
    return {**EXAMPLE, **kw}


class NameRuleTests(unittest.TestCase):
    def test_bad_names_are_refused(self):
        for name in BAD_NAMES:
            with self.subTest(name=name), self.assertRaisesRegex(runconfig.RunConfigError, "^name"):
                runconfig.load(_config(name=name))

    def test_good_names_pass(self):
        for name in GOOD_NAMES:
            with self.subTest(name=name):
                self.assertEqual(runconfig.load(_config(name=name))["name"], name)

    def test_family_ids(self):
        for fam in BAD_FAMILIES:
            with self.subTest(family=fam), self.assertRaisesRegex(runconfig.RunConfigError, "^family_id"):
                runconfig.load(_config(family_id=fam))
        for fam in GOOD_FAMILIES:
            with self.subTest(family=fam):
                self.assertEqual(runconfig.load(_config(family_id=fam))["family_id"], fam)


class RunPathTests(SandboxedDBTest):
    db_modules = (store,)

    def test_run_path_stays_in_runs_dir(self):
        self.assertEqual(paths.run_path("t-abc"), paths.runs_dir() / "t-abc")
        self.assertFalse((paths.runs_dir() / "t-abc").exists())  # run_path only names it
        for bad in ["..", "../x", "a\\..\\..\\b", "C:/x", "C:\\Temp\\x", "", "x/y", ".", "nul"]:
            with self.subTest(run_id=bad), self.assertRaises(ValueError):
                paths.run_path(bad)

    def test_queue_and_launch_refuse_a_traversal_name(self):
        outside = paths.lab_dir().parent
        before = sorted(p.name for p in outside.iterdir())
        for name in ("..\\..\\escaped", "../escaped", str(outside / "escaped")):
            with self.subTest(name=name):
                with self.assertRaises(runconfig.RunConfigError):
                    runs.queue(_config(name=name))
                with self.assertRaises(runconfig.RunConfigError):
                    runs.launch(_config(name=name))
        self.assertEqual(sorted(p.name for p in outside.iterdir()), before)
        self.assertEqual(store.list_runs(), [])

    def test_ids_from_outside_are_checked_too(self):
        """--rerun and --run-id take an id from the command line (and the API from a URL)."""
        for bad in ("..\\..\\x", "../x", "C:\\Temp\\x"):
            with self.subTest(run_id=bad):
                with self.assertRaises(runs.RunNotQueued):
                    runs.launch(run_id=bad)
                with self.assertRaises(runs.RunFailed):
                    runs.rerun(bad)
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = cli.main(["run", "--rerun", bad])
                self.assertEqual(code, 2, err.getvalue())
                self.assertIn("not a valid run id", err.getvalue())

    def test_import_refuses_a_folder_outside_runs_dir(self):
        stray = paths.lab_dir().parent / "stray-run"
        stray.mkdir()
        with self.assertRaises(ValueError):
            runs.import_run(stray)


if __name__ == "__main__":
    unittest.main()

"""stockify.paths: every store resolves inside the sandboxed state dir."""
from __future__ import annotations

import unittest
from datetime import date

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import paths


class PathsTests(unittest.TestCase):
    def test_lab_dir_is_under_the_state_dir(self):
        import stockify_paths
        self.assertEqual(paths.lab_dir(), stockify_paths.state_dir() / "lab")
        support.assert_sandboxed(paths.lab_dir())

    def test_every_store_is_sandboxed(self):
        for p in (paths.refdata_db(), paths.bars_db(), paths.lab_db(),
                  paths.ticks_landing_dir(), paths.ticks_archive_dir(),
                  paths.nse_raw_dir("fo_bhav"), paths.packs_dir(), paths.runs_dir(),
                  paths.journals_dir("s1"), paths.logs_dir()):
            with self.subTest(path=str(p)):
                support.assert_sandboxed(p)

    def test_day_files_use_the_documented_names(self):
        d = date(2026, 11, 2)
        self.assertEqual(paths.ticks_landing_file(d).name, "ticks_20261102.sqlite")
        self.assertEqual(paths.ticks_archive_dir(d).name, "date=2026-11-02")

    def test_directories_are_created_lazily(self):
        self.assertTrue(paths.run_dir("r1").is_dir())


if __name__ == "__main__":
    unittest.main()

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

    def test_research_path_refuses_unsafe_ids(self):
        for bad in ("..", "a/b", "a\\b", "CON", "nul.x", "x" * 65, "", "C:evil", ".hidden"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    paths.research_path(bad)
        ok = paths.research_path("sweep-1")
        self.assertEqual(ok.parent, paths.research_dir())
        self.assertEqual(paths.research_path("x" * 64).name, "x" * 64)
        support.assert_sandboxed(ok)

    def test_research_path_property_random_segments(self):
        import random
        rng = random.Random(11)
        safe = "abcXYZ019._-"
        junk = safe + "/\\:*? ~"
        for _ in range(500):
            text = "".join(rng.choice(junk) for _ in range(rng.randint(0, 70)))
            try:
                got = paths.research_path(text)
            except ValueError:
                continue
            self.assertEqual(got.parent.resolve(), paths.research_dir().resolve())
            self.assertEqual(got.name, text)
            self.assertTrue(set(text) <= set(safe) and 0 < len(text) <= 64)

    def test_exports_dir_is_under_the_lab(self):
        self.assertEqual(paths.exports_dir(), paths.lab_dir() / "exports")
        support.assert_sandboxed(paths.exports_dir())

    def test_export_path_names_the_variant(self):
        self.assertEqual(paths.export_path("r1", False), paths.exports_dir() / "r1.zip")
        self.assertEqual(paths.export_path("r1", True), paths.exports_dir() / "r1-with-packs.zip")
        with self.assertRaises(ValueError):
            paths.export_path("../x", False)


if __name__ == "__main__":
    unittest.main()

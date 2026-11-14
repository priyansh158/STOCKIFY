"""AM 7.x: web/src/lib/names.ts, the pure helpers for run names, statuses, engines and the forms' suggested name."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

REPO = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which("node"), "node is not installed: the web helper checks need node >= 22.18")
class NamesHelperChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run(["node", "--no-warnings", str(Path(__file__).with_name("names_check.ts"))], capture_output=True,
                             text=True, timeout=60, cwd=REPO, encoding="utf-8")
        if out.returncode != 0:
            raise AssertionError("names_check.ts failed: " + out.stderr)
        cls.r = json.loads(out.stdout)

    def test_a_chosen_name_reads_as_words_without_changing_what_is_stored(self):
        self.assertEqual(self.r["nameWords"], ["Cmp eod straddle spanlite", "Orb reliance minute", "Orb nifty", "", "",
                                               "Already Fine", "A b"])

    def test_code_words_status_and_engine_in_plain_words(self):
        self.assertEqual(self.r["wordsOf"], ["Refdata stale", "R margin", "Margin flat", "X"])
        self.assertEqual(self.r["status"], ["Finished", "Running", "Weird one"])
        self.assertEqual(self.r["impl"], ["C++ engine", "Python reference engine", None, "Other impl"])

    def test_the_forms_suggest_a_friendly_name_and_the_lab_gets_a_safe_one(self):
        self.assertEqual(self.r["month"], ["Jun 2026", "", ""])
        self.assertEqual(self.r["suggested"], ["Opening Range Breakout · RELIANCE · Jun 2026", "Buy and Hold",
                                               "X · NIFTY 50 · Jan 2024"])
        from stockify import paths
        for slug in self.r["slug"]:
            self.assertTrue(paths.safe_segment(slug, 64), slug)     # exactly the lab's rule for a run name
        self.assertEqual(self.r["slug"][0], "Opening_Range_Breakout_RELIANCE_Jun_2026")
        self.assertEqual(self.r["slug"][1], "test")
        self.assertEqual(self.r["slug"][4], "run_CON")


if __name__ == "__main__":
    unittest.main()

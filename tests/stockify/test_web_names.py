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

    def test_a_name_that_is_changed_when_saved_says_so_gently(self):
        n = self.r["notes"]
        self.assertIn('marks other than letters, digits, "." "_" and "-" become "_"', n[0])
        self.assertIn("Opening_Range_Breakout_RELIANCE", n[0])
        self.assertIsNone(n[1])                       # spaces, ".", "_" and "-" are kept: nothing to say
        self.assertIsNone(n[2])
        self.assertIn("become", n[3])                 # non-ASCII is replaced
        self.assertIn("only the first 64 characters", n[4])
        self.assertTrue("become" in n[5] and "only the first 64" in n[5])

    def test_a_spaced_hyphen_survives_the_slug_and_the_servers_label(self):
        from stockify import display
        self.assertEqual(self.r["roundTrip"], "a_-_b")
        self.assertEqual(display.name_label(self.r["roundTrip"]), "A - b")

    def test_code_words_status_and_engine_in_plain_words(self):
        self.assertEqual(self.r["wordsOf"], ["Refdata stale", "R margin", "Margin flat", "X"])
        self.assertEqual(self.r["status"], ["Finished", "Running", "Weird one"])
        self.assertEqual(self.r["impl"], ["C++ engine", "Python reference engine", None, "Other impl"])

    def test_the_forms_suggest_a_friendly_name_and_the_lab_gets_a_safe_one(self):
        self.assertEqual(self.r["month"], ["Jun 2026", "", ""])
        self.assertEqual(self.r["suggested"], ["Opening Range Breakout on RELIANCE from Jun 2026",
                                               "Buy and Hold", "X on NIFTY 50 from Jan 2024"])
        self.assertNotIn("·", "".join(self.r["suggested"]))        # no dot-joined names (Hallmark gate 63)
        from stockify import paths
        for slug in self.r["slug"]:
            self.assertTrue(paths.safe_segment(slug, 64), slug)     # exactly the lab's rule for a run name
        self.assertEqual(self.r["slug"][0], "Opening_Range_Breakout_on_RELIANCE_from_Jun_2026")
        self.assertEqual(self.r["slug"][1], "test")
        self.assertEqual(self.r["slug"][4], "run_CON")


if __name__ == "__main__":
    unittest.main()

"""DM 7.1 (D-145): the sweep form's pure helpers (web/src/lib/sweepForm.ts) give the hand-checked answers: what was typed
becomes the values to try, the tries are counted live, and the reason Run is disabled is said in words. Skipped, with a
message, when node is missing."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

REPO = Path(__file__).resolve().parents[2]
CHECK = Path(__file__).with_name("jobs_check.ts")
HOOK = Path(__file__).with_name("ts_resolve_hook.mjs").as_uri()


@unittest.skipUnless(shutil.which("node"), "node is not installed: the sweep form checks need it")
class SweepFormChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run(["node", "--import", str(HOOK), str(CHECK)], capture_output=True, text=True, encoding="utf-8",
                             timeout=60, cwd=REPO)
        assert out.returncode == 0, out.stderr
        cls.a = json.loads(out.stdout)

    def test_the_value_limit_is_the_apis(self):
        self.assertEqual(self.a["max"], 100)
        text = (REPO / "server" / "schemas.py").read_text(encoding="utf-8")
        self.assertIn("Annotated[list[Any], Field(min_length=1, max_length=100)]", text)

    def test_empty_text_keeps_the_setting(self):
        self.assertEqual(self.a["empty"], {"values": [], "problem": None})

    def test_a_list_and_a_range_give_the_values(self):
        self.assertEqual(self.a["list"], {"values": [5, 10, 15], "problem": None})
        self.assertEqual(self.a["range"], {"values": [5, 10, 15, 20, 25, 30], "problem": None})
        self.assertEqual(self.a["rangeOne"], {"values": [5], "problem": None})
        self.assertEqual(self.a["times"], {"values": ["14:30", "15:00"], "problem": None})
        self.assertEqual(self.a["symbols"]["values"], ["NSE:RELIANCE", "NSE:TCS"])

    def test_decimals_are_exact_text_never_floats(self):
        self.assertEqual(self.a["decimalRange"], {"values": ["0.05", "0.1", "0.15", "0.2"], "problem": None})
        self.assertEqual(self.a["decimalRangeExact"], ["0.1", "0.2", "0.3", "0.4", "0.5"])      # not 0.30000000000000004
        self.assertEqual(self.a["decimalList"]["values"], ["0.05", "0.1"])

    def test_a_bad_value_is_named_in_words(self):
        self.assertEqual(self.a["badInt"]["problem"], "“abc” is not a whole number.")
        self.assertEqual(self.a["badTime"]["problem"], "“25:99” is not a time like 15:10.")
        self.assertEqual(self.a["twice"]["problem"], "“5” is listed twice.")
        self.assertEqual(self.a["rangeFraction"]["problem"], "“7.5” is not a whole number.")
        for key in ("badInt", "badTime", "twice", "rangeFraction"):
            self.assertEqual(self.a[key]["values"], [])

    def test_a_bad_range_says_why(self):
        self.assertIn("numbers only", self.a["rangeOnText"]["problem"])
        self.assertIn("must not be above", self.a["rangeBackwards"]["problem"])
        self.assertIn("above zero", self.a["rangeStepZero"]["problem"])
        self.assertEqual(self.a["rangeTooMany"]["problem"], "That range has 1000 values; the most is 100.")
        self.assertEqual(self.a["listTooMany"]["problem"], "That is 101 values; the most is 100.")

    def test_bool_choices(self):
        self.assertEqual(self.a["bool"], [[], [True, False], [True], [False]])

    def test_tries_are_the_product_and_an_unchanged_setting_adds_nothing(self):
        self.assertEqual(self.a["tries"], [1, 3, 6, 1])
        self.assertEqual(self.a["words"], ["1 try", "15 tries", "12,345 tries"])

    def test_run_is_disabled_with_a_reason_in_words(self):
        self.assertEqual(self.a["planNoRun"], "Choose a finished test to start from.")
        self.assertEqual(self.a["planNothing"], "Give at least one setting more than one value to try.")
        self.assertEqual(self.a["planSingle"], "That is a single try. List two or more values for a setting.")
        self.assertEqual(self.a["planBad"]["reason"], "Fix the settings marked above first.")
        self.assertEqual(self.a["planLabelled"], ["Opening range (minutes): is not a whole number"])

    def test_the_plan_builds_the_apis_axes(self):
        ok = self.a["planOk"]
        self.assertEqual(ok["axes"], {"strategy.params.range_minutes": [5, 10, 15],
                                      "strategy.params.exit_hhmm": ["14:30", "15:00"]})
        self.assertEqual((ok["tries"], ok["reason"], ok["needsConfirm"]), (6, None, False))

    def test_a_big_sweep_needs_a_tick_and_a_too_big_one_is_refused(self):
        self.assertEqual(self.a["planBig"], [[60, True, "Tick the box to confirm that you want to run 60 tries."],
                                             [60, True, None]])
        self.assertEqual(self.a["planTooBig"], "That is 60 tries and the most is 40. Offer fewer values.")

    def test_the_walkforward_form_checks_before_sending(self):
        ok, overlap, zero, text, full, date, order = self.a["wf"]
        self.assertEqual(ok["body"], {"sweep_job_id": "sweep-1", "train_days": 120, "test_days": 30, "step_days": 30,
                                      "objective": "sharpe"})
        self.assertIsNone(ok["problem"])
        self.assertIn("overlap", overlap)
        self.assertEqual(zero, "Training days: a whole number from 1 to 10,000.")
        self.assertEqual(text, "Training days: a whole number from 1 to 10,000.")
        self.assertEqual(full, {"sweep_job_id": "sweep-1", "train_days": 60, "test_days": 20, "step_days": 20,
                                "objective": "net_pnl", "first_day": "2026-01-01", "last_day": "2026-06-30"})
        self.assertEqual(date, "First day: a date like 2026-03-31.")
        self.assertEqual(order, "The first day is after the last day.")


if __name__ == "__main__":
    unittest.main()

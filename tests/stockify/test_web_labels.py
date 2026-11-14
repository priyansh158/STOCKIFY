"""AM 7.x: the run caveat chips and their sentences (web/src/lib/labels.ts) are plain sentence-case words, never codes."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

REPO = Path(__file__).resolve().parents[2]

# every label the lab attaches to a run or a paper day (bundle.py, the oracle and the engine), with the margin and fill families
LABELS = ["fill:approximate", "fill:bar", "fill:eod_close", "fill:snapshot_quote", "fill:snapshot_ltp", "fill:some_new_model",
          "eod_approx", "margin:flat(approx)", "margin:spanlite(approx)", "margin:staircase", "margin:span_exact",
          "margin:B1_span_nse_elm", "margin:B4_spanlite_placeholder", "margin_steps:B1_span_nse_elm=3",
          "margin_steps:B5_flat=1", "margin_span:no_section", "margin_span:skipped_files", "margin_span:no_derivatives",
          "margin_elm:skipped_files", "margin_spanlite:placeholder", "margin_spanlite:none", "margin_spanlite:iv_fallback",
          "margin_spanlite:calib_2026_10", "charges:unverified_rows", "costs:scaled", "specs:equity_tick_assumed",
          "settlement_fallback", "delivery_margin_approx", "short_delivery", "data:gaps=3", "data:gaps=1", "refdata_stale",
          "sod_from:2026-10-05", "margin_span:sod_files_only", "margin_elm:sod_files_only", "session:paper-1",
          "a_label_nobody_has_seen_yet"]


@unittest.skipUnless(shutil.which("node"), "node is not installed: the web helper checks need node >= 22.18")
class LabelWordsChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run(["node", "--no-warnings", str(Path(__file__).with_name("labels_check.ts")), json.dumps(LABELS)],
                             capture_output=True, text=True, timeout=60, cwd=REPO, encoding="utf-8")
        if out.returncode != 0:
            raise AssertionError("labels_check.ts failed: " + out.stderr)
        cls.r = json.loads(out.stdout)

    def test_every_chip_is_sentence_case_plain_words(self):
        for label, got in self.r["labels"].items():
            text = got["text"]
            self.assertFalse(text[0].islower(), f"{label}: {text!r} does not start with a capital")
            self.assertNotIn("_", text, label)                                       # no snake_case
            self.assertNotRegex(text, r"[a-z]:[a-z]|=\d", label)                      # no key:value or key=value code
            self.assertNotIn("_", got["meaning"], label)

    def test_the_examples_the_owner_pointed_at(self):
        L = self.r["labels"]
        self.assertEqual(L["fill:eod_close"]["text"], "Fills at the day's close")
        self.assertEqual(L["fill:bar"]["text"], "Fills from price bars")
        self.assertEqual(L["margin:flat(approx)"]["text"], "Flat margin, approximate")
        self.assertEqual(L["charges:unverified_rows"]["text"], "Unverified charges")
        self.assertEqual(L["data:gaps=3"]["text"], "3 days with missing data")
        self.assertEqual(L["data:gaps=1"]["text"], "1 day with missing data")
        self.assertEqual(L["a_label_nobody_has_seen_yet"]["text"], "A label nobody has seen yet")
        self.assertTrue(L["charges:unverified_rows"]["warn"])
        self.assertFalse(L["fill:bar"]["warn"])

    def test_margin_steps_have_words_even_when_new(self):
        self.assertEqual(self.r["step"]["text"], "exact NSE margin")
        self.assertNotIn("_", self.r["weird"]["text"])


if __name__ == "__main__":
    unittest.main()

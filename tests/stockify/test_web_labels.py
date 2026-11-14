"""AM 7.x: the run caveat chips and their sentences (web/src/lib/labels.ts) are plain sentence-case words, never codes."""
from __future__ import annotations

import json
import re
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


def _node(labels: list[str]) -> dict:
    out = subprocess.run(["node", "--no-warnings", str(Path(__file__).with_name("labels_check.ts")), json.dumps(labels)],
                         capture_output=True, text=True, timeout=60, cwd=REPO, encoding="utf-8")
    if out.returncode != 0:
        raise AssertionError("labels_check.ts failed: " + out.stderr)
    return json.loads(out.stdout)


# The files that emit run labels, and the line shapes that emit one (a new emitter line in an old file is caught too).
EMITTERS = ("stockify/bundle.py", "stockify/oracle/margin.py", "stockify/oracle/runner.py", "stockify/fno/spanlite.py",
            "stockify/fno/staircase.py")
EMIT_LINE = re.compile(r"labels\.(?:add|append)\(|extra\.add\(|labels \|= \{|_LABEL\s*=|out \|= \{")
# label families built with an f-string: the source must still build them this way, and a sample of each is checked
FAMILIES = {"fill:": "fill:{", "margin:": "f\"margin:{", "margin_steps:": "margin_steps:{", "data:gaps=": "data:gaps=",
            "sod_from:": "sod_from:", "session:": "session:{", "margin_spanlite:": "margin_spanlite:"}
SAMPLES = ["fill:bar", "fill:eod_close", "fill:snapshot_quote", "fill:snapshot_ltp", "margin:flat(approx)",
           "margin:spanlite(approx)", "margin:staircase", "margin:span_exact", "margin:B1_span_nse_elm",
           "margin:B2_span_rule_elm", "margin:B3_spanlite_calibrated", "margin:B4_spanlite_placeholder", "margin:B5_flat",
           "margin_steps:B3_spanlite_calibrated=2", "margin_spanlite:placeholder", "margin_spanlite:none",
           "data:gaps=2", "sod_from:2026-10-05", "session:paper-1"]


@unittest.skipUnless(shutil.which("node"), "node is not installed: the web helper checks need node >= 22.18")
class EmittedLabelsHaveWords(unittest.TestCase):
    """Every label code the lab can attach to a run has its own wording in web/src/lib/labels.ts (else a new code would
    reach the page as generic words made from the code)."""

    def test_every_literal_code_in_the_emitters_has_wording(self):
        codes: set[str] = set()
        for rel in EMITTERS:
            for line in (REPO / rel).read_text(encoding="utf-8").splitlines():
                if EMIT_LINE.search(line) and not line.lstrip().startswith("#"):
                    codes |= {c for c in re.findall(r'"([a-z][a-z0-9_]*(?::[a-z0-9_()+=\-]+)?)"', line)
                              if "_" in c or ":" in c}
        codes -= {"labels", "extra", "out"}
        self.assertGreaterEqual(len(codes), 10, codes)
        got = _node(sorted(codes))["labels"]
        for code in sorted(codes):
            if code.endswith(":") or code in ("margin_spanlite", "margin_span", "margin_elm"):
                continue
            self.assertTrue(got[code]["known"], f"{code} is emitted but labels.ts has no wording for it")

    def test_the_label_families_are_still_built_as_the_samples_assume_and_have_wording(self):
        text = "".join((REPO / rel).read_text(encoding="utf-8") for rel in EMITTERS)             + (REPO / "engine/src/margin/staircase.cpp").read_text(encoding="utf-8")
        for family, shape in FAMILIES.items():
            self.assertIn(shape, text, f"no emitter builds the {family!r} labels any more: update the samples")
        got = _node(SAMPLES)["labels"]
        for code in SAMPLES:
            self.assertTrue(got[code]["known"], f"{code} has no wording in labels.ts")


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

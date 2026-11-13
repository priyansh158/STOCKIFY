"""The research page helpers (web/src/lib/researchHelpers.ts) give the hand-checked answers (M6 Task 29).
Skipped, with a message, when node is missing."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

REPO = Path(__file__).resolve().parents[2]
CHECK = Path(__file__).with_name("research_check.ts")


@unittest.skipUnless(shutil.which("node"), "node is not installed: the research helper checks need it")
class ResearchHelperChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run(["node", str(CHECK)], capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=REPO)
        assert out.returncode == 0, out.stderr
        cls.answers = json.loads(out.stdout)

    def test_heat_step_is_the_equal_width_band_clamped_to_the_range(self):
        # (value, min, max, steps) in the .ts: 0 and 10 are the ends, 5 is the middle band of five,
        # 4.99 is still in band 2, 6 is band 3, out-of-range values clamp, a flat range and NaN are band 0,
        # one step is always 0, and zero steps is 0 rather than -1.
        self.assertEqual(self.answers["heat"], [0, 4, 2, 2, 3, 0, 4, 0, 0, 0, 0])

    def test_bucket_tone_names_thin_before_sign(self):
        self.assertEqual(self.answers["tone"], ["up", "down", "flat", "thin", "thin"])

    def test_sentence_or_dash_gives_a_dash_for_nothing_and_trims_the_rest(self):
        self.assertEqual(self.answers["sentence"], ["—", "—", "—", "—", "Held up.", "A. B."])

    def test_series_palette_is_the_validated_one(self):
        # accent, info, warn passed the dataviz validator on the dark surface (chroma, deutan, normal-vision, contrast);
        # a change to the palette must re-run scripts/validate_palette.js and update this list on purpose.
        self.assertEqual(self.answers["seriesTokens"], ["accent", "info", "warn"])
        self.assertEqual(self.answers["seriesCss"], [["0", "accent"], ["1", "info"], ["2", "warn"]])

    def test_heat_caption_follows_the_scale_direction(self):
        # Larger values get a stronger accent mix, on a dark surface: so the caption must say brighter.
        mixes = self.answers["heatMixes"]
        self.assertEqual(len(mixes), 5)
        self.assertEqual(mixes, sorted(mixes))
        self.assertEqual(self.answers["scheme"], "dark")
        self.assertEqual(self.answers["more"], ["brighter", "darker"])

    def test_diverging_step_is_centred_on_zero_with_three_levels_each_side(self):
        # (value, max |value|): a tenth is the first level, a half the second, the full range the third, and the
        # sign is kept; zero, NaN and a zero range are the neutral 0; beyond the range clamps.
        self.assertEqual(self.answers["div"], [1, -1, 0, 3, -3, 2, -2, 2, 3, 0, 0])

    def test_heat_formats_carry_the_sign_when_signed_and_use_rupees_for_paise(self):
        self.assertEqual(self.answers["fmt"], ["+1.23", "−0.20", "0.00", "−0.20", "+12.34 %", "−5.00 %",
                                               "+₹4,120", "−₹31,000", "₹1,23,456", "—", "+1.5"])

    def test_heat_grid_props_adapt_the_sweep_cells(self):
        g = self.answers["grid"]
        self.assertEqual(g["x"], {"x": 0, "y": 1})                      # best {xi, yi} becomes {x, y}
        self.assertTrue(g["signed"])
        self.assertEqual((g["xs"], g["ys"]), (["5", "10"], ["20", "30"]))
        self.assertEqual(g["cells"], [[0.5, -0.2], [1.0, None]])         # cells[yi][xi] pass as they are, null stays null
        self.assertEqual((g["xLabel"], g["yLabel"]), ("fast", "slow"))
        self.assertEqual(g["f"], "+1.00")
        m = self.answers["money"]
        self.assertEqual((m["signed"], m["f"]), (False, "₹4,120"))

    def test_one_axis_sweep_is_one_row_named_by_the_objective(self):
        o = self.answers["one"]
        # the real one-axis DTO has a best {xi: k, yi: 0}: it becomes {x: k, y: 0}
        self.assertEqual((o["best"], o["cells"]), ({"x": 2, "y": 0}, [[0.5, -0.2, 1.2]]))
        self.assertEqual(o["links"], [["/tests/a", "/tests/b", "/tests/c"]])
        self.assertEqual(o["yLabel"], "Objective")
        self.assertEqual(self.answers["one"]["ys"], ["Steadiness of returns"])      # the row is named by what is scored

    def test_axis_labels_replace_the_paths_and_a_percent_grid_is_signed(self):
        self.assertEqual(self.answers["grid2"]["xLabel"], "Fast average")
        self.assertEqual(self.answers["grid2"]["yLabel"], "Slow average")
        self.assertEqual(self.answers["grid2"]["links"], [["/tests/a", "/tests/b"], ["/tests/c", None]])   # a cell links to its run
        self.assertEqual(self.answers["pc"], {"signed": True, "f": ["+12.34 %", "−5.00 %"]})

    def test_diverging_palette_words_and_css_agree(self):
        # info above zero, warn below: the validated colour-blind-safe pair (dataviz validator, deutan/protan/tritan
        # separation 22 to 25). The caption's colour words come from DIVERGING; the stylesheet must use the same tokens.
        self.assertEqual(self.answers["diverging"], {"positive": {"token": "info", "word": "blue"},
                                                     "negative": {"token": "warn", "word": "orange"}})
        css = self.answers["divCss"]
        self.assertEqual(len(css), 6)
        for sign, token in (("pos", "info"), ("neg", "warn")):
            arm = [c for c in css if c[0] == sign]
            self.assertEqual([c[1] for c in arm], [1, 2, 3])
            self.assertEqual({c[2] for c in arm}, {token})
            self.assertEqual([c[3] for c in arm], sorted(c[3] for c in arm))

    def test_missing_sweep_detail_maps_to_running_failed_missing_or_unreadable(self):
        self.assertEqual(self.answers["missing"],
                         ["missing", "running", "failed", "running", "unreadable", "missing", "missing", "failed", "running"])

    def test_a_failed_sweeps_reason_is_the_text_after_its_status(self):
        self.assertEqual(self.answers["failure"], ["the holdout is locked", None, None, None, None])


if __name__ == "__main__":
    unittest.main()

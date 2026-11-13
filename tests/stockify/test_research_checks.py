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

    def test_a_line_is_cut_at_each_break_so_no_segment_bridges_a_gap(self):
        # unsorted points sort first; a break x starts a new segment; no breaks is one segment; nothing is none.
        self.assertEqual(self.answers["segments"], [[[1, 2, 3], [5], [9]], [2], 0])

    def test_the_walkforward_curve_is_one_segment_per_tested_window_with_a_boundary_at_each_test_start(self):
        c = self.answers["curve"]
        day = 19723                                                    # 2024-01-01 as days since 1970
        self.assertEqual([p[0] - day for p in c["points"]], [0, 1, 9, 10, 11])
        self.assertEqual([p[1] for p in c["points"]], [1.01, 1.02, 1.0, 0.99, 1.03])
        self.assertEqual([b - day for b in c["breaks"]], [9])           # the untested window adds no break
        self.assertEqual([s - day for s in c["starts"]], [0, 8])        # and no boundary

    def test_heat_caption_follows_the_scale_direction(self):
        # Larger values get a stronger accent mix, on a dark surface: so the caption must say brighter.
        mixes = self.answers["heatMixes"]
        self.assertEqual(len(mixes), 5)
        self.assertEqual(mixes, sorted(mixes))
        self.assertEqual(self.answers["scheme"], "dark")
        self.assertEqual(self.answers["more"], ["brighter", "darker"])


if __name__ == "__main__":
    unittest.main()

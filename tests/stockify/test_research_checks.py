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

    def test_cost_scale_is_null_for_one_and_for_anything_unreadable(self):
        # absent, empty, "1", "1.00", null, a bad string and a non-object: no banner; 2, 1.50 and 0 (free) are shown
        self.assertEqual(self.answers["costScale"], [None, None, None, None, None, "2", "1.5", "0", None, None, None])

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

    # ---- /data page helpers (web/src/lib/data.ts), M6 Task 32 ----

    def test_every_gap_reason_in_a1_has_a_plain_word_and_an_unknown_one_reads_as_other(self):
        self.assertEqual(self.answers["a1Reasons"], ["disconnect", "process_stop", "token_expired", "stall", "not_subscribed"])
        self.assertTrue(self.answers["reasonKeysCovered"])
        self.assertEqual(self.answers["reasonWords"], [
            "the connection to Kite dropped", "the recorder was stopped", "the Kite login expired",
            "an instrument stopped sending data", "the instrument was not subscribed"])
        self.assertEqual(self.answers["reasonOther"], ["other: brand_new", "not stated", "not stated", "not stated"])
        self.assertEqual(self.answers["scope"], ["everything the recorder watches", "one instrument (id 738561)", "not stated", "other: weird"])

    def test_cell_words_say_a_word_and_a_figure_and_never_rely_on_colour(self):
        # [words, tone, symbol, note] per cell; the symbol is the non-colour signal next to the word
        self.assertEqual(self.answers["cells"], [
            ["98.4% covered", "up", "✓", None],
            ["100% covered", "up", "✓", None],
            ["97.3% covered", "warn", "~", "2 instruments recorded before mode logging; not counted"],
            ["verified", "up", "✓", None],
            ["complete", "up", "✓", None],
            ["landing, not archived yet", "warn", "~", None],
            ["downloaded, not parsed", "warn", "~", None],
            ["no recording", "down", "✕", None],
            ["no recording", "down", "✕", None],
            ["not downloaded", "down", "✕", None],
            ["download failed (http 503)", "down", "✕", None],
            ["not reported yet", "info", "?", "the quality report has not run for this day"],
            ["market closed", None, "–", None],
            ["not yet", None, "–", None],
            ["no instruments recorded in this mode", None, "–", "2 instruments recorded before mode logging; not counted"],
            ["not applicable", None, "–", None],
            ["unknown state: surprise", None, "?", None],
        ])

    def test_a_downloaded_nse_file_says_downloaded_and_other_datasets_say_complete(self):
        self.assertEqual(self.answers["nseWords"], ["downloaded", "complete", "verified", "partly there"])

    def test_durations_bytes_and_the_recorder_strip_are_in_words(self):
        self.assertEqual(self.answers["durations"], ["still open", "still open", "0 s", "12 s", "1 min", "2 min 5 s", "1 h", "1 h 2 min", "—"])
        self.assertEqual(self.answers["bytes"], ["—", "0 B", "999 B", "1.5 kB", "2.5 MB", "12.3 GB", "3 TB", "—"])
        none = {"headline": "The recorder has not reported yet", "tone": "warn", "rate": "—", "last": "—",
                "note": "Start the recorder to see live figures."}
        self.assertEqual(self.answers["recorder"], [
            none,
            {"headline": "Connected", "tone": "up", "rate": "12.3 packets a second", "last": "last packet 340 ms ago", "note": None},
            {"headline": "Connected", "tone": "up", "rate": "0 packets a second", "last": "no packet seen yet", "note": None},
            {"headline": "Not connected", "tone": "down", "rate": "0 packets a second", "last": "last packet 2 min 5 s ago", "note": None},
            {"headline": "Not reporting", "tone": "warn", "rate": "5 packets a second", "last": "no packet seen yet",
             "note": "The last report was 6 min 40 s ago, so these figures may be out of date."},
        ])

    def test_day_helpers_use_ist_and_reject_what_is_not_a_date(self):
        d = self.answers["days"]
        self.assertEqual(d["today"], ["2026-10-07", "2026-10-06"])  # 00:30 IST on the 7th, 23:59 IST on the 6th
        self.assertEqual(d["add"], ["2026-09-08", "2026-02-28", "2024-02-29", "2027-01-01"])
        self.assertEqual(d["label"], ["Wed 7 Oct", "Thu 1 Jan", "nonsense"])
        self.assertEqual(d["parse"], ["2026-10-07", None, None, None, None, None, None])
        self.assertEqual(d["range"], [60, 30, 60, 180, 60, 60, 60])
        self.assertEqual(self.answers["worst"], [1, 4, 5, 3])  # lowest coverage first, ties by id, no figure last

    # ---- /research overview helpers (web/src/lib/researchHelpers.ts), M6 Task 33 ----

    def test_family_and_job_links_encode_what_the_ids_hold(self):
        # a family id holds "/" and can hold anything else: it rides in the query, fully encoded
        self.assertEqual(self.answers["hrefs"]["family"], [
            "/research/family?id=sweep%2Fab12", "/research/family?id=a%20b%26c%3Dd%3Fe%23f",
            "/research/family?id=plain", "/research/family?id=100%25"])
        # only a sweep and a walk-forward have a page; any other kind has no link
        self.assertEqual(self.answers["hrefs"]["job"], ["/research/sweeps/sweep-1", "/research/walkforward/wf%2F1%20x", None, None])

    # ---- /research/family helpers, M6 Task 34 ----

    def test_a_family_id_survives_the_link_and_the_page_reads_it_once(self):
        # ids with "/", "&", "#", a literal "%" and "%25" all come back as written (a second decode would break "100%25")
        self.assertEqual(self.answers["familyRoundTrip"], [True] * 6)
        self.assertEqual(self.answers["familyIdParam"], [None, None, None, "a/b", "x/1", None])

    def test_trial_table_words_and_the_loose_rows_are_read_as_text(self):
        self.assertEqual(self.answers["objective"], ["₹2,500.00", "12.3%", "1.23", "—", "—"])
        self.assertEqual(self.answers["roles"], ["sensitivity check", "out-of-sample test", "re-run", "odd"])
        # the owner's reason stays a plain string (the page renders it as text, never as markup); a non-string becomes empty
        self.assertEqual(self.answers["unlocks"], [
            {"when_ns": 1_700_000_000_000_000_000, "run_id": "r1", "reason": "<b>final</b> check"},
            {"when_ns": None, "run_id": None, "reason": ""}])
        self.assertEqual(self.answers["sessions"], [
            {"session_id": "s1", "name": "n", "status": "RUNNING", "created_ns": 5},
            {"session_id": "s2", "name": None, "status": None, "created_ns": None}])

    def test_the_family_fetch_path_keeps_slashes_and_encodes_everything_else(self):
        self.assertEqual(self.answers["apiPath"], [
            "/api/research/families/sweep/j1", "/api/research/families/strat/ds-1m-RELIANCE/sharpe",
            "/api/research/families/100%2525", "/api/research/families/a%20b/c%3Fd%23e",
            "/api/research/families/a/%2E/b", "/api/research/families/x/%2E%2E/y"])

    def test_scores_are_ranked_best_first_with_ties_and_missing_scores_left_unranked(self):
        self.assertEqual(self.answers["ranks"], [1, 5, 2, 2, None, None, 4])

    def test_the_overfitting_note_is_not_said_twice_and_says_how_it_was_computed(self):
        self.assertEqual(self.answers["pboNote"], ["Left out: zz (flat)", "Based on 5 of 6 tries; left out: zz (flat)",
                                                   "Fewer than 2 tries", None, None, None])
        self.assertEqual(self.answers["pboHow"], [
            "Computed over 40 days, cut into 4 blocks. Every way of choosing half the blocks (6 in all) was set against the other half.",
            "Computed over 1 day, cut into 2 blocks. Every way of choosing half the blocks (1 in all) was set against the other half."])

    def test_a_capped_list_says_how_many_are_shown_and_stays_quiet_otherwise(self):
        self.assertEqual(self.answers["capNote"], [
            None,
            "Showing the latest 50 of 130 sweeps. Older ones are not listed here.",
            None,
            "Showing the latest 50 of 51 walk-forward runs. Older ones are not listed here.",
            None,
            None])

    def test_the_data_page_prints_the_launcher_command_with_its_backslash_and_no_stray_carriage_return(self):
        self.assertIn(".\\run.ps1 -DaemonOnly", self.answers["dataPageCommands"])
        self.assertIn("python -m stockify nse fetch --date YYYY-MM-DD", self.answers["dataPageCommands"])
        self.assertEqual(self.answers["webSourceHasStrayCR"], [False, False, False, False])


if __name__ == "__main__":
    unittest.main()

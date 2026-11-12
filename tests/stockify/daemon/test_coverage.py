"""LW 5.1 (M5.11): each session-day's coverage, the share of continuous-phase seconds with no gap that touches the
session's instruments; a day under the threshold is flagged."""
from __future__ import annotations

import unittest
from datetime import date
from fractions import Fraction

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import timeutil
from stockify.daemon import coverage as cov
from stockify.data import packs

DAY = date(2026, 11, 17)
SEC = 1_000_000_000
THRESHOLD = Fraction("0.98")


def at(hhmmss: str) -> int:
    return timeutil.ist_ns(DAY, hhmmss)


def phase(name: str, a: str, b: str) -> dict:
    return {"phase": name, "start_ns": at(a), "end_ns": at(b)}


def bundle(extra_groups: dict | None = None) -> dict:
    groups = {"FO_OPT": [phase("PRE_OPEN", "09:00:00", "09:15:00"), phase("CONTINUOUS", "09:15:00", "15:30:00")],
              "CM": [phase("CONTINUOUS", "09:15:00", "15:30:00")],
              "CM_CAS": [phase("CONTINUOUS", "09:15:00", "15:30:00")], **(extra_groups or {})}
    return {"days": [{"date": DAY.isoformat(), "groups": groups}],
            "instruments": [{"id": 1, "session_group": "FO_OPT"}, {"id": 2, "session_group": "CM"},
                            {"id": 3, "session_group": "FO_OPT"}]}


def gap(reason: str, start: str, end: str | None, iid: int = 0) -> bytes:
    out = packs.enc_gap(iid, at(start), 0, reason)
    if end is not None:
        out += packs.enc_gap(iid, at(end), 1, reason)
    return out


class CoverageTest(unittest.TestCase):
    def test_full_day_no_gaps_is_1(self):
        c = cov.coverage(bundle(), b"", {1}, THRESHOLD)
        self.assertEqual((c.fraction, c.covered_s, c.continuous_s, c.gaps, c.below_threshold),
                         (Fraction(1), 22500, 22500, [], False))

    def test_90s_disconnect_in_a_6h25m_day(self):
        c = cov.coverage(bundle(), gap("disconnect", "10:00:00", "10:01:30"), {1}, THRESHOLD)
        self.assertEqual(c.fraction, Fraction(22500 - 90, 22500))
        self.assertFalse(c.below_threshold)
        self.assertEqual((c.covered_s, c.continuous_s), (22410, 22500))
        self.assertEqual([(g["reason"], g["scope"], g["start_ns"], g["end_ns"], g["closed"]) for g in c.gaps],
                         [("disconnect", "all", at("10:00:00"), at("10:01:30"), True)])

    def test_gap_on_unrelated_instrument_does_not_count(self):
        body = gap("not_subscribed", "10:00:00", "11:00:00", iid=99)
        self.assertEqual(cov.coverage(bundle(), body, {1}, THRESHOLD).fraction, Fraction(1))
        own = gap("not_subscribed", "10:00:00", "10:30:00", iid=1)
        self.assertEqual(cov.coverage(bundle(), own, {1}, THRESHOLD).fraction, Fraction(22500 - 1800, 22500))

    def test_unclosed_token_gap_runs_to_close_and_flags(self):
        c = cov.coverage(bundle(), gap("token_expired", "14:00:00", None), {1}, THRESHOLD)
        self.assertEqual(c.fraction, Fraction(22500 - 5400, 22500))
        self.assertTrue(c.below_threshold)
        self.assertFalse(c.gaps[0]["closed"])
        self.assertEqual(c.gaps[0]["end_ns"], at("15:30:00"))

    # ── the details ─────────────────────────────────────────────────────────────────────────────────────────────────
    def test_overlapping_gaps_count_once_and_any_reason_counts(self):
        body = gap("disconnect", "10:00:00", "10:10:00") + gap("stall", "10:05:00", "10:20:00", iid=1)
        c = cov.coverage(bundle(), body, {1}, THRESHOLD)
        self.assertEqual(c.continuous_s - c.covered_s, 20 * 60)

    def test_time_outside_continuous_phases_is_not_counted(self):
        c = cov.coverage(bundle(), gap("disconnect", "09:05:00", "09:20:00"), {1}, THRESHOLD)
        self.assertEqual(c.continuous_s - c.covered_s, 5 * 60)           # only 09:15-09:20 is continuous

    def test_continuous_time_is_the_union_over_the_groups_of_the_ids(self):
        groups = {"CM": [phase("CONTINUOUS", "09:15:00", "09:25:00")],
                  "FO_OPT": [phase("CONTINUOUS", "09:20:00", "09:40:00")]}
        c = cov.coverage(bundle(groups), b"", {1, 2}, THRESHOLD)
        self.assertEqual(c.continuous_s, 25 * 60)
        self.assertEqual(cov.coverage(bundle(groups), b"", {2}, THRESHOLD).continuous_s, 10 * 60)

    def test_exactly_the_threshold_is_not_below(self):
        c = cov.coverage(bundle(), gap("disconnect", "10:00:00", "10:07:30"), {1}, Fraction(22050, 22500))
        self.assertEqual(c.fraction, Fraction(22050, 22500))
        self.assertFalse(c.below_threshold)
        self.assertTrue(cov.coverage(bundle(), gap("disconnect", "10:00:00", "10:07:31"), {1},
                                     Fraction(22050, 22500)).below_threshold)

    def test_a_repeated_start_keeps_the_first_and_an_orphan_end_is_ignored(self):
        body = (packs.enc_gap(0, at("10:00:00"), 0, "disconnect") + packs.enc_gap(0, at("10:01:00"), 0, "disconnect")
                + packs.enc_gap(0, at("10:02:00"), 1, "disconnect") + packs.enc_gap(0, at("11:00:00"), 1, "stall"))
        c = cov.coverage(bundle(), body, {1}, THRESHOLD)
        self.assertEqual(c.continuous_s - c.covered_s, 120)

    def test_unknown_ids_and_no_continuous_time_are_errors(self):
        with self.assertRaises(cov.CoverageError):
            cov.coverage(bundle(), b"", {42}, THRESHOLD)
        with self.assertRaises(cov.CoverageError):
            cov.coverage(bundle(), b"", set(), THRESHOLD)

    def test_the_stored_forms(self):
        c = cov.coverage(bundle(), gap("disconnect", "10:00:00", "10:01:30"), {1}, THRESHOLD)
        self.assertEqual(cov.as_float(c), (22500 - 90) / 22500)
        j = cov.as_json(c)
        self.assertEqual((j["fraction"], j["below_threshold"], j["covered_s"], j["continuous_s"]),
                         ("249/250", False, 22410, 22500))
        self.assertEqual(j["gaps"], c.gaps)

    def test_the_threshold_is_read_from_paper_json(self):
        self.assertEqual(cov.configured_threshold(), Fraction("0.98"))


def clocks(a: str, b: str, step_s: int = 1) -> bytes:
    """A CLOCK record every `step_s` seconds from `a` to `b` inclusive."""
    return b"".join(packs.enc_clock(t) for t in range(at(a), at(b) + 1, step_s * SEC))


CADENCE, TOLERANCE = 1 * SEC, 5 * SEC


def seen(body: bytes, ids=frozenset({1})) -> "cov.Coverage":
    return cov.coverage(bundle(), body, set(ids), THRESHOLD, cadence_ns=CADENCE, tolerance_ns=TOLERANCE)


class UnseenTimeTest(unittest.TestCase):
    """I-2: continuous time in which the session saw no record at all is uncovered, though the journal holds no GAP."""

    def test_a_full_journal_is_fully_covered(self):
        c = seen(clocks("09:00:00", "15:30:00"))
        self.assertEqual((c.fraction, c.gaps), (Fraction(1), []))

    def test_no_records_at_all_is_zero_coverage(self):
        c = seen(b"")
        self.assertEqual((c.fraction, c.covered_s, c.below_threshold), (Fraction(0), 0, True))
        self.assertEqual([g["reason"] for g in c.gaps], ["unseen"])

    def test_a_journal_that_stops_at_11_is_uncovered_to_the_close(self):
        """A process that died, a failed session, a dead engine."""
        c = seen(clocks("09:00:00", "11:00:00"))
        self.assertEqual(c.continuous_s - c.covered_s, 4 * 3600 + 30 * 60)
        self.assertTrue(c.below_threshold)

    def test_a_hole_in_the_middle_with_no_gap_record_counts(self):
        c = seen(clocks("09:00:00", "11:00:00") + clocks("11:30:00", "15:30:00"))
        self.assertEqual(c.continuous_s - c.covered_s, 30 * 60)

    def test_a_restart_after_the_close_with_no_frames_leaves_the_tail_uncovered(self):
        c = seen(clocks("09:00:00", "14:00:00") + packs.enc_gap(0, at("16:00:00"), 1, "process_stop"))
        self.assertEqual(c.continuous_s - c.covered_s, 90 * 60)

    def test_settles_refdata_and_gaps_are_not_frames(self):
        late = at("20:00:00")
        body = (clocks("09:00:00", "11:00:00") + packs.enc_settle(1, late, 0, DAY.isoformat(), 100)
                + packs.enc_refdata(0, late, "strategy_control", {"enabled": True, "reasons": []})
                + packs.enc_gap(0, late, 0, "process_stop"))
        self.assertEqual(seen(body).continuous_s - seen(body).covered_s, 4 * 3600 + 30 * 60)

    def test_the_tolerance_comes_from_the_caller(self):
        edge = clocks("09:00:00", "15:30:00", step_s=6)             # a record every 6 s: cadence 1 s + tolerance 5 s
        self.assertEqual(seen(edge).fraction, Fraction(1))
        wide = clocks("09:00:00", "15:30:00", step_s=7)
        self.assertLess(seen(wide).fraction, Fraction(1))

    def test_an_overlap_with_a_journaled_gap_counts_once(self):
        body = clocks("09:00:00", "10:00:00") + gap("disconnect", "10:00:00", "10:30:00") + clocks("10:30:00", "15:30:00")
        c = seen(body)
        self.assertEqual(c.continuous_s - c.covered_s, 30 * 60)

    def test_without_a_cadence_only_journaled_gaps_count(self):
        self.assertEqual(cov.coverage(bundle(), b"", {1}, THRESHOLD).fraction, Fraction(1))

    def test_the_bounds_are_read_from_paper_json(self):
        cadence, tolerance = cov.configured_bounds()
        self.assertEqual((cadence, tolerance), (1 * SEC, 5 * SEC))


if __name__ == "__main__":
    unittest.main()

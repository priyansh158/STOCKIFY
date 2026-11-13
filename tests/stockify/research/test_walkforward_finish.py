"""M6.5 (WF 6.5): stitching the out-of-sample pieces, in-sample vs out-of-sample, the deflated Sharpe over every
training run, and `finish`. Fixture runs have known daily returns; nothing runs an engine."""
from __future__ import annotations

import json
import math
import random
import shutil
import statistics
import unittest
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import stockify.store as store
from stockify import paths
from stockify.research import fileio, registry, stats, walkforward as wf
from tests.stockify.research.fixtures import make_run

CAP = 100_000_000
JOB = "wf-fixture-20260101T000000-abc123"
A, B = {"range_minutes": 5}, {"range_minutes": 10}


def ann_sharpe(r: list[float]) -> float | None:
    """The annualised Sharpe of plain returns with a zero risk-free rate, from the statistics module."""
    if len(r) < 2 or min(r) == max(r):
        return None
    return statistics.mean(r) / statistics.stdev(r) * math.sqrt(252)


def build_job(windows: list[dict], *, status: str = "running") -> str:
    """windows: dicts with `train` (a return list per grid point), `params` (per grid point), `chosen` (grid index
    or None), `test` (return list, None = no test run) and `test_start`."""
    family = registry.job_family_id("walkforward", JOB)
    registry.ensure_family(family, "sharpe")
    state = []
    for k, w in enumerate(windows):
        train_ids = []
        for g, rets in enumerate(w["train"]):
            rid = f"tr{k}-{g}"
            make_run(rid, family, daily_returns=rets, capital_paise=CAP, role="trial", job_id=JOB)
            train_ids.append(rid)
        chosen = w.get("chosen")
        test_id = None
        if w.get("test") is not None:
            test_id = f"te{k}"
            make_run(test_id, family, daily_returns=w["test"], capital_paise=CAP, role="oos", job_id=JOB,
                     start=w["test_start"])
        default = "done" if test_id else ("failed" if chosen is not None else "skipped")
        state.append({"index": k, "train_from": "2026-01-01", "train_to": "2026-04-30", "test_from": "2026-05-01",
                      "test_to": "2026-05-30", "train_run_ids": train_ids,
                      "chosen_params": w["params"][chosen] if chosen is not None else None,
                      "chosen_run_id": train_ids[chosen] if chosen is not None else None,
                      "test_run_id": test_id, "test_status": w.get("test_status", default), "error": None})
    grid = max(len(w["train"]) for w in windows)
    store.create_research_job({
        "job_id": JOB, "kind": "walkforward", "name": "fixture", "family_id": family, "status": status,
        "created_ns": 1, "spec_json": json.dumps({"name": "fixture", "train": 120, "test": 30, "step": 30,
                                                  "objective": "sharpe", "span": ["2026-01-01", "2026-06-30"],
                                                  "grid": grid, "windows": len(windows), "unlock_given": False}),
        "state_json": json.dumps({"windows": state}, sort_keys=True)})
    return JOB


R0 = [0.01, -0.005, 0.004, 0.002, -0.003]
R1 = [0.002, 0.003, -0.001, 0.004, 0.001]
T0 = [0.006, -0.002, 0.003, 0.001, -0.004, 0.002]
T1 = [-0.003, 0.004, 0.002, -0.001, 0.005, 0.001]


def two_windows(w0: dict | None = None, w1: dict | None = None) -> list[dict]:
    a = dict(train=[R0, R1], params=[A, B], chosen=1, test=T0, test_start="2026-05-04")
    b = dict(train=[R1, R0], params=[A, B], chosen=0, test=T1, test_start="2026-06-01")
    a.update(w0 or {})
    b.update(w1 or {})
    return [a, b]


class StitchTests(unittest.TestCase):
    def test_stitch_compounds_and_starts_at_one(self):
        dates, index = wf.stitch([(["2026-05-04", "2026-05-05", "2026-05-06"], [0.10, -0.10, 0.0])])
        self.assertEqual(dates, ["2026-05-04", "2026-05-05", "2026-05-06"])
        # 1.0 is the capital before the first day: index_0 = 1.0 * (1 + r_0)
        self.assertAlmostEqual(index[0], 1.10)
        self.assertAlmostEqual(index[1], 0.99)
        self.assertAlmostEqual(index[2], 0.99)
        self.assertEqual(wf.stitch([]), ([], []))

    def test_stitch_keeps_window_order_and_gaps(self):
        p1 = (["2026-05-04", "2026-05-05"], [0.01, 0.02])
        p2 = (["2026-06-01", "2026-06-02"], [-0.01, 0.03])      # a month later: the gap is not filled
        dates, index = wf.stitch([p1, p2])
        self.assertEqual(dates, p1[0] + p2[0])
        self.assertEqual(len(index), 4)
        self.assertAlmostEqual(index[2], 1.01 * 1.02 * 0.99)
        self.assertAlmostEqual(index[3], 1.01 * 1.02 * 0.99 * 1.03)
        with self.assertRaisesRegex(ValueError, "order"):
            wf.stitch([p2, p1])                                   # pieces out of window order are refused

    def test_stitch_refuses_unequal_lengths_and_non_finite_returns(self):
        with self.assertRaises(ValueError):
            wf.stitch([(["2026-05-04"], [0.1, 0.2])])
        with self.assertRaises(ValueError):
            wf.stitch([(["2026-05-04"], [float("nan")])])

    def test_stitch_properties_over_seeded_cases(self):
        rng = random.Random(21)
        for _ in range(300):
            pieces, day = [], 0
            for _ in range(rng.randint(0, 5)):
                n = rng.randint(0, 6)
                day += rng.randint(1, 20)
                pieces.append(([f"2027-{1 + (day + i) // 28:02d}-{1 + (day + i) % 28:02d}" for i in range(n)],
                               [rng.uniform(-0.05, 0.05) for _ in range(n)]))
                day += n
            dates, index = wf.stitch(pieces)
            flat = [r for _, rs in pieces for r in rs]
            self.assertEqual((len(dates), len(index)), (len(flat), len(flat)))
            self.assertEqual(dates, sorted(dates))
            prev = 1.0
            for i, r in enumerate(flat):                          # the defining recurrence, day by day
                self.assertAlmostEqual(index[i] / prev, 1 + r, places=9)
                prev = index[i]
            self.assertAlmostEqual(prev, math.prod(1 + r for r in flat), places=9)   # capital is reinvested
            if len(pieces) >= 2:                                  # no state between windows: pieces compose
                k = rng.randint(1, len(pieces) - 1)
                d1, i1 = wf.stitch(pieces[:k])
                d2, i2 = wf.stitch(pieces[k:])
                last = i1[-1] if i1 else 1.0
                self.assertEqual(dates, d1 + d2)
                for a, b in zip(index, i1 + [last * x for x in i2]):
                    self.assertAlmostEqual(a, b, places=9)


class ChartTests(unittest.TestCase):
    A = (["2026-05-04", "2026-05-05", "2026-05-06"], [1.01, 1.02, 1.01])
    B = (["2026-06-01", "2026-06-02"], [1.03, 1.05])             # a month later: the gap must stay a gap

    def test_each_test_piece_is_its_own_segment_and_nothing_bridges_the_gap(self):
        fig = wf._figure([self.A, self.B], ["2026-05-01", "2026-05-30"])
        try:
            ax = fig.axes[0]
            segs = [ln for ln in ax.get_lines() if ln.get_label() == "out-of-sample"]
            self.assertEqual(len(segs), 2)
            for ln, (dates, idx) in zip(segs, (self.A, self.B)):
                self.assertEqual(list(ln.get_ydata()), idx)
                self.assertEqual([x.isoformat() for x in ln.get_xdata()], dates)
            last_a, first_b = max(segs[0].get_xdata()), min(segs[1].get_xdata())
            self.assertGreater((first_b - last_a).days, 20)               # the days between are on the axis, unplotted
            for ln in segs:                                        # no line holds points from both windows
                xs = list(ln.get_xdata())
                self.assertFalse(min(xs) <= last_a and max(xs) >= first_b)
            bounds = [ln for ln in ax.get_lines() if ln.get_label() == "test start"]
            self.assertEqual(len(bounds), 2)
            self.assertEqual(ax.get_ylabel(), "growth of 1 (capital = 1.0)")
            self.assertEqual(len({ln.get_color() for ln in segs}), 1)
        finally:
            import matplotlib.pyplot as plt
            plt.close(fig)


class ChartNoteTests(unittest.TestCase):
    def test_a_note_is_written_on_the_figure_and_the_y_range_keeps_one_inside_a_floor(self):
        import matplotlib.pyplot as plt
        fig = wf._figure([ChartTests.A], ["2026-05-01"], note="3 out-of-sample days only")
        try:
            ax = fig.axes[0]
            self.assertIn("3 out-of-sample days only", [t.get_text() for t in ax.texts])
            lo, hi = ax.get_ylim()
            self.assertLessEqual(lo, 0.95)
            self.assertGreaterEqual(hi, 1.05)
        finally:
            plt.close(fig)
        fig = wf._figure([ChartTests.A], ["2026-05-01"])
        try:
            self.assertEqual(len(fig.axes[0].texts), 0)
        finally:
            plt.close(fig)


class FinishTests(SandboxedDBTest):
    db_modules = (store,)

    def setUp(self):
        super().setUp()
        p = mock.patch.object(stats, "risk_free_rows", return_value=[])     # a zero risk-free rate: hand-checkable
        p.start()
        self.addCleanup(p.stop)

    def test_summary_on_a_constructed_job(self):
        build_job(two_windows())
        fin = wf.finish(JOB)
        s = fin.summary
        is_sr = ann_sharpe(R1)             # the chosen training run is R1 in both windows
        oos = ann_sharpe(T0 + T1)
        self.assertEqual((s.windows, s.oos_days), (2, 12))
        self.assertAlmostEqual(s.is_sharpe, is_sr, places=3)
        self.assertAlmostEqual(s.oos_sharpe, oos, places=3)
        self.assertAlmostEqual(s.ratio, oos / is_sr, places=3)
        self.assertEqual((len(fin.stitched_dates), len(fin.stitched_index)), (12, 12))
        self.assertEqual(fin.stitched_dates, sorted(fin.stitched_dates))
        self.assertAlmostEqual(fin.stitched_index[-1], math.prod(1 + r for r in T0 + T1), places=3)
        self.assertEqual(len(fin.windows), 2)
        w0 = fin.windows[0]
        self.assertEqual((w0["chosen_params"], w0["test_status"]), (B, "done"))
        self.assertAlmostEqual(w0["train"]["sharpe"], ann_sharpe(R1), places=3)
        self.assertAlmostEqual(w0["test"]["sharpe"], ann_sharpe(T0), places=3)
        self.assertEqual(w0["test"]["days"], 6)
        self.assertIsInstance(w0["test"]["net_pnl_paise"], int)

    def test_is_sharpe_is_the_mean_over_windows(self):
        build_job(two_windows(w1=dict(train=[R1, R0], chosen=1)))      # window 1 now chose R0
        s = wf.finish(JOB).summary
        self.assertAlmostEqual(s.is_sharpe, (ann_sharpe(R1) + ann_sharpe(R0)) / 2, places=3)

    def test_ratio_is_none_when_in_sample_is_not_positive_or_undefined(self):
        losing = [-0.01, 0.002, -0.004, 0.001, -0.003]
        build_job(two_windows(w0=dict(train=[losing, losing], chosen=0), w1=dict(train=[losing, losing], chosen=0)))
        s = wf.finish(JOB).summary
        self.assertLess(s.is_sharpe, 0)
        self.assertIsNotNone(s.oos_sharpe)
        self.assertIsNone(s.ratio)

    def test_ratio_is_none_when_in_sample_is_undefined(self):
        flat = [0.0] * 5
        build_job(two_windows(w0=dict(train=[flat, flat], chosen=0), w1=dict(train=[flat, flat], chosen=0)))
        s = wf.finish(JOB).summary
        self.assertIsNone(s.is_sharpe)
        self.assertIsNone(s.ratio)
        self.assertIsNotNone(s.oos_sharpe)

    def test_modal_share_hand_checked(self):
        three = [dict(train=[R0, R1], params=[A, B], chosen=c, test=t, test_start=s)
                 for c, t, s in ((0, T0, "2026-05-04"), (0, T1, "2026-06-01"), (1, T0, "2026-07-06"))]
        build_job(three)
        self.assertAlmostEqual(wf.finish(JOB).summary.chosen_modal_share, 2 / 3)

    def test_dsr_uses_every_training_run_as_a_trial(self):
        build_job(two_windows())
        s = wf.finish(JOB).summary
        self.assertEqual(s.n_trials, 2 * 2)                                   # windows x grid
        per_period = [statistics.mean(r) / statistics.stdev(r) for r in (R0, R1, R1, R0)]
        self.assertAlmostEqual(s.var_sharpe, statistics.variance(per_period), places=6)
        want = stats.deflated_sharpe(T0 + T1, per_period, 4)
        self.assertAlmostEqual(s.dsr.dsr, want.dsr, places=3)
        self.assertEqual(s.dsr.n_trials, 4)
        self.assertGreater(s.dsr.sr0, 0)

    def test_finish_works_from_the_database_alone(self):
        build_job(two_windows())
        before = wf.finish(JOB)
        shutil.rmtree(paths.research_path(JOB))                   # the artefacts gone
        shutil.rmtree(paths.runs_dir(), ignore_errors=True)       # and every run folder
        store.close_connection() if hasattr(store, "close_connection") else None
        again = wf.finish(JOB)
        self.assertEqual(again.stitched_index, before.stitched_index)
        self.assertEqual(again.summary, before.summary)
        self.assertTrue((paths.research_path(JOB) / "walkforward.json").is_file())

    def test_flat_oos_series_has_no_sharpe_and_no_nan(self):
        flat = [0.0] * 6
        build_job(two_windows(w0=dict(test=flat), w1=dict(test=flat)))
        fin = wf.finish(JOB)
        s = fin.summary
        self.assertIsNone(s.oos_sharpe)
        self.assertIsNone(s.ratio)
        self.assertIsNone(s.dsr)
        self.assertEqual(set(fin.stitched_index), {1.0})
        text = (paths.research_path(JOB) / "walkforward.json").read_text(encoding="utf-8")
        json.loads(text, parse_constant=lambda c: self.fail(f"non-finite constant {c} in walkforward.json"))

    def test_walkforward_json_has_no_nan(self):
        build_job(two_windows())
        fin = wf.finish(JOB)
        folder = paths.research_path(JOB)
        doc = json.loads((folder / "walkforward.json").read_text(encoding="utf-8"),
                         parse_constant=lambda c: self.fail(f"non-finite constant {c}"))
        self.assertEqual(set(doc), {"schema", "job_id", "name", "objective", "train_days", "test_days", "step_days",
                                    "span", "summary", "windows", "dates", "index"})
        self.assertEqual(doc["dates"], fin.stitched_dates)
        self.assertEqual(doc["index"], fin.stitched_index)
        self.assertEqual(doc["summary"]["windows"], 2)
        self.assertEqual(doc["windows"][0]["chosen_params"], B)
        self.assertEqual(doc["summary"]["dsr"]["n_trials"], 4)
        self.assertGreater((folder / "walkforward.png").stat().st_size, 0)
        self.assertFalse((folder / "report.md").exists())          # Task 22's wf_report owns it

    def test_window_with_a_failed_test_run_is_listed_and_left_out_of_the_stitch(self):
        build_job(two_windows(w0=dict(test=None, test_status="failed")))
        fin = wf.finish(JOB)
        self.assertEqual(len(fin.windows), 2)
        self.assertEqual(fin.windows[0]["test_status"], "failed")
        self.assertIsNone(fin.windows[0]["test"])
        self.assertEqual((fin.summary.windows, fin.summary.oos_days, len(fin.stitched_dates)), (2, 6, 6))
        self.assertAlmostEqual(fin.summary.oos_sharpe, ann_sharpe(T1), places=3)
        self.assertAlmostEqual(fin.summary.is_sharpe, ann_sharpe(R1), places=3)   # only the stitched window counts

    def test_a_skipped_window_has_no_chosen_run_and_no_share_weight(self):
        build_job(two_windows(w1=dict(chosen=None, test=None, test_status="skipped")))
        fin = wf.finish(JOB)
        self.assertEqual(fin.windows[1]["test_status"], "skipped")
        self.assertIsNone(fin.windows[1]["train"])
        self.assertEqual(fin.summary.chosen_modal_share, 1.0)

    def test_job_is_marked_done_only_after_the_artefact_exists(self):
        build_job(two_windows())
        seen = {}
        real = store.update_research_job

        def spy(job_id, **fields):
            if fields.get("status") == "done":
                seen["file"] = (paths.research_path(job_id) / "walkforward.json").is_file()
            return real(job_id, **fields)
        with mock.patch.object(store, "update_research_job", side_effect=spy):
            wf.finish(JOB)
        self.assertIs(seen["file"], True)
        job = store.get_research_job(JOB)
        self.assertEqual((job["status"], job["error"]), ("done", None))
        self.assertIsNotNone(job["finished_ns"])
        self.assertIn("windows", json.loads(job["state_json"]))          # the state stays for a re-finish

    def test_the_png_is_annotated_below_the_minimum_days_and_plain_above_it(self):
        notes = []
        real = wf._figure

        def spy(segments, starts, note=None):
            notes.append(note)
            return real(segments, starts, note)
        build_job(two_windows())                                                    # 12 out-of-sample days
        with mock.patch.object(wf, "_figure", spy):
            wf.finish(JOB)
        self.assertEqual(notes, ["12 out-of-sample days only"])
        notes.clear()
        store.update_research_job(JOB, status="running")
        with mock.patch.object(wf, "_figure", spy), mock.patch.object(wf, "min_oos_days", return_value=12):
            wf.finish(JOB)
        self.assertEqual(notes, [None])

    def test_finish_on_a_done_job_rewrites_identically(self):
        build_job(two_windows())
        first = wf.finish(JOB)
        path = paths.research_path(JOB) / "walkforward.json"
        text, finished = path.read_text(encoding="utf-8"), store.get_research_job(JOB)["finished_ns"]
        second = wf.finish(JOB)
        self.assertEqual(second, first)
        self.assertEqual(path.read_text(encoding="utf-8"), text)
        job = store.get_research_job(JOB)
        self.assertEqual((job["status"], job["finished_ns"]), ("done", finished))

    def test_a_write_failure_marks_the_job_failed_and_not_done(self):
        build_job(two_windows())
        with mock.patch.object(fileio, "atomic_write_text", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                wf.finish(JOB)
        job = store.get_research_job(JOB)
        self.assertEqual(job["status"], "failed")
        self.assertIn("disk full", job["error"])
        self.assertTrue(job["finished_ns"])

    def test_a_failure_does_not_demote_a_job_that_is_already_done(self):
        build_job(two_windows())
        wf.finish(JOB)
        with mock.patch.object(fileio, "atomic_write_text", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                wf.finish(JOB)
        self.assertEqual(store.get_research_job(JOB)["status"], "done")

    def test_missing_and_foreign_jobs_are_refused(self):
        with self.assertRaisesRegex(wf.WalkforwardError, "no such"):
            wf.finish("wf-nothing")
        store.create_research_job({"job_id": "sw-1", "kind": "sweep", "status": "running", "created_ns": 1})
        with self.assertRaisesRegex(wf.WalkforwardError, "not a walk-forward"):
            wf.finish("sw-1")

    def test_a_failed_job_with_a_pending_window_is_refused_naming_the_window(self):
        build_job(two_windows(w1=dict(test=None, test_status="pending")), status="failed")
        with self.assertRaisesRegex(wf.WalkforwardError, r"window 1 .*run_windows"):
            wf.finish(JOB)
        self.assertEqual(store.get_research_job(JOB)["status"], "failed")
        self.assertFalse((paths.research_path(JOB) / "walkforward.json").exists())

    def test_a_failed_job_whose_windows_all_have_results_is_finished_again(self):
        build_job(two_windows())
        with mock.patch.object(fileio, "atomic_write_text", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                wf.finish(JOB)
        self.assertEqual(store.get_research_job(JOB)["status"], "failed")
        fin = wf.finish(JOB)                                      # the failure came from finish: retry works
        job = store.get_research_job(JOB)
        self.assertEqual((job["status"], job["error"]), ("done", None))
        self.assertEqual(fin.summary.oos_days, 12)
        self.assertTrue((paths.research_path(JOB) / "walkforward.json").is_file())

    def test_two_finishes_give_byte_equal_files(self):
        build_job(two_windows())
        folder = paths.research_path(JOB)
        wf.finish(JOB)
        first = [(folder / n).read_bytes() for n in ("walkforward.json", "walkforward.png")]
        wf.finish(JOB)
        self.assertEqual([(folder / n).read_bytes() for n in ("walkforward.json", "walkforward.png")], first)

    def test_n_trials_falls_back_to_the_training_runs_found_when_the_spec_lacks_the_grid(self):
        build_job(two_windows())
        spec = json.loads(store.get_research_job(JOB)["spec_json"])
        del spec["windows"], spec["grid"]
        store.update_research_job(JOB, spec_json=json.dumps(spec))
        self.assertEqual(wf.finish(JOB).summary.n_trials, 4)

    def test_is_windows_counts_the_windows_that_entered_is_sharpe(self):
        build_job(two_windows(w0=dict(test=None, test_status="failed")))
        fin = wf.finish(JOB)
        self.assertEqual((fin.summary.windows, fin.summary.is_windows), (2, 1))
        doc = json.loads((paths.research_path(JOB) / "walkforward.json").read_text(encoding="utf-8"))
        self.assertEqual((doc["summary"]["windows"], doc["summary"]["is_windows"]), (2, 1))

    def test_no_window_with_a_test_run_gives_an_empty_stitch_and_no_nan(self):
        build_job(two_windows(w0=dict(test=None, test_status="failed"), w1=dict(test=None, test_status="failed")))
        fin = wf.finish(JOB)
        self.assertEqual((fin.stitched_dates, fin.stitched_index), ([], []))
        s = fin.summary
        self.assertEqual((s.oos_days, s.oos_sharpe, s.ratio, s.dsr, s.is_sharpe), (0, None, None, None, None))
        folder = paths.research_path(JOB)
        self.assertFalse((folder / "walkforward.png").exists())
        json.loads((folder / "walkforward.json").read_text(encoding="utf-8"),
                   parse_constant=lambda c: self.fail(c))


if __name__ == "__main__":
    unittest.main()

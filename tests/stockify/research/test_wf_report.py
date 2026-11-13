"""M6.5 (WF 6.6): the walk-forward report.md, its one writer, and the `walkforward` command end to end. Fixture runs
have known returns; the command's pool is a stub that writes finished runs (nothing runs an engine)."""
from __future__ import annotations

import contextlib
import copy
import io
import itertools
import json
import random
import re
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import stockify.store as store
from stockify import __main__ as cli, paths, runconfig
from stockify.commands import stockify_command
from stockify.research import holdout, pbo, plain, registry, stats, walkforward as wf, wf_report
from stockify.research.pool import PoolOutcome
from tests.stockify.research.fixtures import make_run
from tests.stockify.research.test_walkforward_finish import JOB, R0, R1, build_job, two_windows

ROOT = Path(__file__).resolve().parents[3]
GREEK = re.compile("[Ͱ-Ͽ]")
A, B = {"width": 5}, {"width": 10}


def plain_params_windows(**kw) -> list[dict]:
    """The two-window fixture with parameter names that are one plain word."""
    ws = two_windows(**kw)
    for w in ws:
        w["params"] = [A, B]
    return ws


class ReportBase(SandboxedDBTest):
    db_modules = (store,)
    min_oos = 2                  # the fixtures have 12 out-of-sample days: the shipped 60 is tested in MinOosDaysTests

    def setUp(self):
        super().setUp()
        p = mock.patch.object(stats, "risk_free_rows", return_value=[])
        p.start()
        self.addCleanup(p.stop)
        if self.min_oos is not None:
            q = mock.patch.object(wf_report, "min_oos_days", return_value=self.min_oos)
            q.start()
            self.addCleanup(q.stop)

    def finished(self, windows=None) -> str:
        build_job(windows or plain_params_windows())
        wf.finish(JOB)
        return JOB

    def report_text(self, **kw) -> str:
        return wf_report.render(self.finished(), **kw).read_text(encoding="utf-8")


def write_sensitivity(run_id: str, verdict: str = "fragile") -> None:
    make_run(run_id, None, with_folder=True)
    rows = [
        {"key": "baseline", "axis": "baseline", "label": "As run", "run_id": run_id, "status": "baseline",
         "net_pnl_paise": 500000, "sharpe": 1.5, "delta_net_paise": 0, "error": None},
        {"key": "costs_x2", "axis": "costs", "label": "All costs ×2", "run_id": "s1", "status": "done",
         "net_pnl_paise": -120000, "sharpe": -0.4, "delta_net_paise": -620000, "error": None},
        {"key": "slip_plus1", "axis": "slippage", "label": "1 extra tick", "run_id": "s2", "status": "done",
         "net_pnl_paise": 300000, "sharpe": 0.9, "delta_net_paise": -200000, "error": None}]
    doc = {"schema": "stockify.sensitivity/1", "run_id": run_id, "job_id": "sens-job", "verdict": verdict,
           "fragile": verdict == "fragile", "reasons": ["x"], "baseline_net_paise": 500000,
           "fragile_cost_scale": "2", "fragile_extra_ticks": 1, "rows": rows}
    (paths.run_path(run_id) / "sensitivity.json").write_text(json.dumps(doc), encoding="utf-8")


class RenderTests(ReportBase):
    def test_report_md_has_the_two_sharpes_the_ratio_and_the_dsr_sentence(self):
        job = self.finished()
        text = wf_report.render(job).read_text(encoding="utf-8")
        s = wf.finish(job).summary
        self.assertEqual(wf_report.report_path(job), paths.research_path(job) / "report.md")
        self.assertIn(f"{s.is_sharpe:.2f}", text)
        self.assertIn(f"{s.oos_sharpe:.2f}", text)
        self.assertIn(plain.wf_ratio_sentence(s), text)                    # the same wording the page shows
        self.assertIn(plain.wf_dsr_sentence(s.n_trials, s.dsr.dsr), text)
        self.assertIn("over 4 tries", text)                                # N = 2 windows x 2 grid points
        self.assertIn("N = windows x grid = 4", text)
        self.assertIn("never used to choose", text)
        self.assertIn("cautious", text)
        self.assertNotIn("best of 4 tries", text)                          # the stitched days are not "best of N"
        self.assertIn(f"{s.oos_days} out-of-sample days", text)
        self.assertIn("calendar days", text)
        self.assertIn("the same settings won", text)                       # the stability line
        self.assertIn("](walkforward.png)", text)
        doc = json.loads((paths.research_path(job) / "walkforward.json").read_text(encoding="utf-8"))
        for w in doc["windows"]:
            self.assertIn(w["test_from"], text)                            # each window's dates are in the table
        self.assertIn("width = 10", text)                                  # the per-window chosen settings

    def test_the_curve_axis_title_names_the_dashed_line_as_the_starting_capital(self):
        # one wording for every growth-of-1 chart (the compare page uses the same): the start is the dashed line at 1.0
        # and the first point is the end of the first day
        job = self.finished()
        doc = json.loads((paths.research_path(job) / "walkforward.json").read_text(encoding="utf-8"))
        self.assertEqual(wf_report.notes(doc)["y_label"], "growth of 1 (dashed line = starting capital)")

    def test_report_says_how_the_two_sharpes_are_measured_next_to_the_ratio(self):
        text = self.report_text()
        note = ("The in-sample figure is the average of each window's training Sharpe; the out-of-sample figure is "
                "the Sharpe of all the tested days joined together, so the ratio is a rough guide, not like for like.")
        self.assertIn(note, text)
        ratio_at, note_at = text.index("Out-of-sample against in-sample:"), text.index(note)
        self.assertLess(abs(note_at - ratio_at), 400)                      # in the same paragraph block

    def test_report_without_sensitivity_says_how_to_run_it(self):
        text = self.report_text()
        self.assertIn(f"not run yet: `{stockify_command("sensitivity --run <id>")}`", text)
        text = wf_report.render(JOB, sensitivity_run="no-such-run").read_text(encoding="utf-8")
        self.assertIn(f"not run yet: `{stockify_command("sensitivity --run no-such-run")}`", text)

    def test_report_with_sensitivity_has_the_table(self):
        write_sensitivity("base1")
        text = self.report_text(sensitivity_run="base1")
        self.assertIn("All costs ×2", text)
        self.assertIn("-₹1,200", text)
        self.assertIn("| 1 extra tick |", text)
        self.assertNotIn("not run yet", text)
        self.assertIn("Breaks if all costs double", text)                  # the fragile sentence, as the page says it
        self.assertIn("base1", text)

    def test_a_damaged_sensitivity_file_reads_as_not_run_yet(self):
        make_run("base2", None, with_folder=True)
        (paths.run_path("base2") / "sensitivity.json").write_text("{not json", encoding="utf-8")
        self.assertIn("not run yet", self.report_text(sensitivity_run="base2"))

    def test_an_unsafe_sensitivity_run_id_is_refused(self):
        self.finished()
        for bad in ("../x", "a/b", "", "C:\\x"):
            with self.assertRaises(wf_report.WfReportError, msg=bad):
                wf_report.render(JOB, sensitivity_run=bad)

    def test_report_is_plain_words_only(self):
        write_sensitivity("base3")
        text = self.report_text(sensitivity_run="base3")
        self.assertIsNone(GREEK.search(text))
        words = set(re.findall(r"[A-Za-z_]+", re.sub(r"`[^`]*`", "", text).lower()))
        for raw in ("done", "failed", "pending", "skipped", "dsr", "psr", "pbo", "nan", "none", "null", "inf",
                    "ddof", "kurtosis", "logit", "test_status", "n_trials"):
            self.assertNotIn(raw, words, raw)
        self.assertEqual([w for w in words if "_" in w], [])                # no code names
        self.assertNotIn("$", text)

    def test_the_result_of_each_window_is_said_in_words(self):
        ws = plain_params_windows(w1=dict(test=None, chosen=0))               # chosen, but its test run did not finish
        ws.append(dict(train=[R0, R1], params=[A, B], chosen=None, test=None))   # nothing usable to pick
        build_job(ws)
        wf.finish(JOB)
        text = wf_report.render(JOB).read_text(encoding="utf-8")
        self.assertIn("test run did not finish", text)
        self.assertIn("no training run gave a usable result", text)
        self.assertIn("| tested |", text)

    def test_holdout_unlocks_of_the_family_are_counted(self):
        self.finished()
        self.assertIn("never looked at", wf_report.render(JOB).read_text(encoding="utf-8"))
        fam = registry.job_family_id("walkforward", JOB)
        store.log_holdout_unlock("tr0-0", fam, "first look on purpose", job_id="j1")
        store.log_holdout_unlock("tr0-1", fam, "second look on purpose", job_id="j2")
        self.assertIn("looked at 2 times", wf_report.render(JOB).read_text(encoding="utf-8"))

    def test_the_overfitting_chance_is_absent_with_a_plain_note_for_a_walk_forward_family(self):
        text = self.report_text()
        self.assertIn("Chance of overfitting", text)
        self.assertIn("the windows cover different days, so the tries cannot be compared day by day", text)

    def test_the_overfitting_sentence_is_shown_when_there_is_one(self):
        res = pbo.PboResult(pbo=0.25, n_trials=4, n_days=100, slices=16, combinations=100, rows_dropped=0,
                            median_logit=0.1, prob_loss=0.2)
        self.finished()
        with mock.patch.object(pbo, "family_pbo", return_value=pbo.PboOutcome(res, None, 4, 4, [], [])):
            text = wf_report.render(JOB).read_text(encoding="utf-8")
        self.assertIn(plain.pbo_sentence(0.25, 4, 4), text)
        self.assertNotIn("cannot be compared", text)

    def test_few_days_carry_a_caution(self):
        self.assertIn("less than a year of trading", self.report_text())            # 12 out-of-sample days

    def test_a_job_with_no_out_of_sample_days_still_has_a_report(self):
        build_job(plain_params_windows(w0=dict(test=None, chosen=0), w1=dict(test=None, chosen=0)))
        wf.finish(JOB)
        text = wf_report.render(JOB).read_text(encoding="utf-8")
        self.assertIn("no out-of-sample days", text)
        self.assertNotIn("](walkforward.png)", text)                                  # no chart was drawn

    def test_render_is_deterministic(self):
        job = self.finished()
        first = wf_report.render(job).read_bytes()
        self.assertEqual(wf_report.render(job).read_bytes(), first)

    def test_render_is_the_only_writer_of_report_md(self):
        self.finished()
        folder = paths.research_path(JOB)
        self.assertFalse((folder / "report.md").exists())                             # finish leaves no report.md
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["walkforward.json", "walkforward.png"])
        self.assertEqual(wf_report.render(JOB), folder / "report.md")
        package = Path(wf.__file__).parent                                            # and no other module names it
        others = [p.name for p in package.glob("*.py") if p.name != "wf_report.py"
                  and "report.md" in re.sub(r'""".*?"""', "", p.read_text(encoding="utf-8"), flags=re.S)
                  and re.search(r"write_text|open\(|atomic_write", p.read_text(encoding="utf-8"))
                  and re.search(r"[\"']report\.md[\"']", p.read_text(encoding="utf-8"))]
        self.assertEqual(others, [])

    def test_a_failed_write_keeps_the_old_report(self):
        job = self.finished()
        old = wf_report.render(job).read_bytes()
        with mock.patch("stockify.research.fileio.atomic_write_text", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                wf_report.render(job)
        self.assertEqual(wf_report.report_path(job).read_bytes(), old)


def long_windows(days: int = 40) -> list[dict]:
    """The two-window fixture with `days` out-of-sample days in each window (80 in all by default)."""
    rng = random.Random(7)
    t0 = [0.002 + rng.uniform(-0.01, 0.01) for _ in range(days)]
    t1 = [0.001 + rng.uniform(-0.01, 0.01) for _ in range(days)]
    return plain_params_windows(w0=dict(test=t0, test_start="2026-05-04"), w1=dict(test=t1, test_start="2026-07-01"))


class MinOosDaysTests(ReportBase):
    """D-102: below research.json walk_forward.min_oos_days the Sharpe figures are not shown."""
    min_oos = None               # the shipped value, 60

    def test_the_shipped_minimum_is_read_from_the_config(self):
        self.assertEqual(wf_report.min_oos_days(), 60)

    def test_few_days_show_the_constant_in_place_of_every_sharpe_figure(self):
        text = self.report_text()                                                       # 12 out-of-sample days
        s = wf.finish(JOB).summary
        want = plain.NOT_ENOUGH_OOS.format(n=12, m=60)
        self.assertGreaterEqual(text.count(want), 4)                                    # row, ratio, dsr, windows note
        rows = [ln for ln in text.splitlines() if re.match(r"\| [12] \| 2026", ln)]       # the window table's rows
        self.assertEqual(len(rows), 2)
        for ln in rows:                                                                  # a short word, not the sentence
            self.assertIn(f"| {plain.NOT_ENOUGH_OOS_SHORT} |", ln)
            self.assertNotIn("not enough", ln)
        self.assertEqual(plain.NOT_ENOUGH_OOS_SHORT, "too few days")
        above = text[:text.index("| Window |")]
        self.assertIn(want, above.splitlines()[-3] + above.splitlines()[-2])             # said in full just above it
        for figure in (s.is_sharpe, s.oos_sharpe):
            self.assertNotIn(f"{figure:.2f}", text)
        self.assertNotIn("% chance the out-of-sample edge", text)
        self.assertNotIn("kept ", text)
        for w in wf_report.load(JOB)[1]["windows"]:                                      # per-window Sharpes
            for part in ("train", "test"):
                self.assertNotIn(f"| {w[part]['sharpe']:.2f} |", text)
        self.assertIn("less than a year of trading", text)                               # the caution stays
        self.assertRegex(text, r"\| (\+|-)?₹")                                          # raw P&L stays
        self.assertIn("](walkforward.png)", text)                                        # and the curve

    def test_the_console_summary_and_the_api_detail_hide_the_figures_too(self):
        job = self.finished()
        text = wf_report.summary_text(job)
        self.assertIn(plain.NOT_ENOUGH_OOS.format(n=12, m=60), text)
        d = wf_report.detail(job)
        s = d["summary"]
        self.assertEqual((s["oos_days"], s["oos_days_needed"]), (12, 60))
        self.assertTrue(all(s[k] is None for k in ("is_sharpe", "oos_sharpe", "ratio", "dsr")))
        for w in d["windows"]:
            self.assertIsNone(w["train"]["sharpe"])
            self.assertIsNone(w["test"]["sharpe"])
            self.assertIsInstance(w["test"]["net_pnl_paise"], int)                       # raw P&L stays
        self.assertEqual(len(d["stitched_index"]), 12)
        self.assertIn(plain.NOT_ENOUGH_OOS.format(n=12, m=60), d["plain"]["ratio"])
        self.assertIn(plain.NOT_ENOUGH_OOS.format(n=12, m=60), d["plain"]["dsr"])

    def test_enough_days_show_the_figures(self):
        job = self.finished(long_windows())                                              # 80 days >= 60
        text = wf_report.render(job).read_text(encoding="utf-8")
        s = wf.finish(job).summary
        self.assertEqual(s.oos_days, 80)
        self.assertNotIn("not enough out-of-sample days", text)
        self.assertIn(f"{s.oos_sharpe:.2f}", text)
        self.assertIn("less than a year of trading", text)                               # 60 <= T < 252: still cautioned
        self.assertIn("% chance the out-of-sample edge beats luck", text)
        d = wf_report.detail(job)
        self.assertEqual(d["summary"]["oos_sharpe"], s.oos_sharpe)
        self.assertEqual(d["summary"]["oos_days_needed"], 60)

    def test_the_boundary_is_the_minimum_itself(self):
        job = self.finished(long_windows(30))                                            # exactly 60
        self.assertEqual(wf.finish(job).summary.oos_days, 60)
        self.assertNotIn("not enough out-of-sample days", wf_report.render(job).read_text(encoding="utf-8"))

    def test_one_day_below_the_minimum_is_not_enough(self):
        job = self.finished(long_windows(29))                                            # 58
        self.assertIn("(58 of 60 needed)", wf_report.render(job).read_text(encoding="utf-8"))


class RefusalTests(ReportBase):
    def test_only_a_done_job_has_a_report(self):
        build_job(plain_params_windows())                                              # running: finish has not run
        with self.assertRaises(wf_report.WfReportError) as cm:
            wf_report.render(JOB)
        self.assertEqual(cm.exception.kind, "not_done")
        self.assertIn("not finished", str(cm.exception))
        store.update_research_job(JOB, status="failed", error="boom at C:\\secret\\path")
        with self.assertRaises(wf_report.WfReportError) as cm:
            wf_report.render(JOB)
        self.assertEqual(cm.exception.kind, "not_done")
        self.assertNotIn("secret", str(cm.exception))                                  # a stored error is not echoed

    def test_unknown_and_other_kinds_of_job_are_refused(self):
        for bad in ("wf-nothing-here", "../escape"):
            with self.assertRaises(wf_report.WfReportError) as cm:
                wf_report.render(bad)
            self.assertEqual(cm.exception.kind, "unknown")
        store.create_research_job({"job_id": "sw-1", "kind": "sweep", "name": "s", "status": "done", "created_ns": 1})
        with self.assertRaises(wf_report.WfReportError) as cm:
            wf_report.render("sw-1")
        self.assertEqual(cm.exception.kind, "unknown")

    def test_no_message_echoes_the_job_or_run_id_the_caller_gave(self):
        self.finished()
        store.create_research_job({"job_id": "sw-1", "kind": "sweep", "name": "s", "status": "done", "created_ns": 1})
        for bad in ("wf-nothing-here", "../escape", "sw-1"):
            with self.assertRaises(wf_report.WfReportError) as cm:
                wf_report.render(bad)
            self.assertNotIn(bad.strip("."), str(cm.exception), bad)
        (paths.research_path(JOB) / "walkforward.json").unlink()
        with self.assertRaises(wf_report.WfReportError) as cm:
            wf_report.render(JOB)
        self.assertNotIn(JOB, str(cm.exception))
        with self.assertRaises(wf_report.WfReportError) as cm:
            wf_report.render(JOB, sensitivity_run="../bad-run-id")
        self.assertNotIn("bad-run-id", str(cm.exception))

    def test_a_done_job_without_its_file_is_refused_not_guessed(self):
        self.finished()
        (paths.research_path(JOB) / "walkforward.json").unlink()
        with self.assertRaises(wf_report.WfReportError) as cm:
            wf_report.render(JOB)
        self.assertEqual(cm.exception.kind, "missing")
        (paths.research_path(JOB) / "walkforward.json").write_text("[1]", encoding="utf-8")
        with self.assertRaises(wf_report.WfReportError) as cm:
            wf_report.render(JOB)
        self.assertEqual(cm.exception.kind, "missing")


# ---- the command, end to end, over a stub pool ------------------------------------------------------------------

AXIS = "strategy.params.range_minutes"
BASE = json.loads((ROOT / "examples" / "runs" / "orb_reliance_minute.json").read_text(encoding="utf-8"))


def _weekday_start(iso: str) -> str:
    d = date.fromisoformat(iso)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


class StubPool:
    """A finished run per job, with seeded daily returns that depend on the grid point and the window's range."""

    _instances = itertools.count()

    def __init__(self):
        self.n = 0
        self.prefix = f"stub{next(self._instances)}x"                      # run ids stay unique across instances
        self.calls: list[tuple[int, int | None]] = []

    def __call__(self, jobs, *, workers=None):
        self.calls.append((len(jobs), workers))
        out = []
        for j in jobs:
            self.n += 1
            spec = j.config["data"]["packs"][0]["spec"]
            width = j.config["strategy"]["params"]["range_minutes"]
            rng = random.Random(f"{width}-{spec['from']}")
            rets = [0.0004 * width / 5 + rng.uniform(-0.006, 0.006) for _ in range(5 if j.role == "oos" else 8)]
            rid = f"{self.prefix}{self.n:03d}"
            make_run(rid, j.config.get("family_id"), daily_returns=rets, capital_paise=10_000_000, role=j.role,
                     job_id=j.job_id, name=j.config["name"], start=_weekday_start(spec["from"]))
            out.append(PoolOutcome(rid, "done", None))
        return out


class CliTests(ReportBase):
    def setUp(self):
        super().setUp()
        base = copy.deepcopy(BASE)
        base["data"]["packs"][0]["spec"].update({"from": "2026-01-01", "to": "2026-03-31"})
        self.dir = Path(self._cache_dir)
        self.sweep = self.dir / "sweep.yaml"
        self.sweep.write_text(
            "schema: stockify.sweep/1\nname: wfcli\nbase: " + json.dumps(runconfig.validate(base)) +
            f"\nobjective: sharpe\naxes:\n  {AXIS}: [5, 10]\n  strategy.params.qty: [10, 20]\n"
            "search: {kind: grid}\n", encoding="utf-8")

    def run_cli(self, *argv, pool=None):
        out, err = io.StringIO(), io.StringIO()
        pool = pool or StubPool()
        with mock.patch("stockify.research.pool.run_all", pool), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = cli.main(["walkforward", *argv])
        return code, out.getvalue(), err.getvalue(), pool

    def test_cli_walkforward_end_to_end_on_the_fixture(self):
        code, out, err, pool = self.run_cli("--sweep", str(self.sweep), "--train", "30d", "--test", "10d",
                                            "--step", "10d", "--objective", "sharpe")
        self.assertEqual(code, 0, err)
        jobs = store.list_research_jobs(kind="walkforward")
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job["status"], "done")
        folder = paths.research_path(job["job_id"])
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["report.md", "walkforward.json", "walkforward.png"])
        spec = json.loads(job["spec_json"])
        self.assertEqual((spec["windows"], spec["grid"]), (6, 4))                 # 6 windows x a 2 x 2 grid
        self.assertEqual([c[0] for c in pool.calls], [24, 6])                      # one training call, one test call
        self.assertIn(str(folder), out)                                            # the folder is printed
        self.assertIn("in-sample", out)                                            # and the summary
        self.assertIn(job["job_id"], (folder / "report.md").read_text(encoding="utf-8"))

    def test_defaults_come_from_research_json(self):
        with mock.patch.object(wf, "run_windows", side_effect=wf.WalkforwardError("stop here")) as m:
            code, _, err, _ = self.run_cli("--sweep", str(self.sweep))
        self.assertEqual(code, 2)
        self.assertIn("stop here", err)
        kw = m.call_args.kwargs
        self.assertEqual((kw["train"], kw["test"], kw["step"], kw["objective"]), (120, 30, 30, "sharpe"))

    def test_bad_arguments_exit_2_before_any_work(self):
        pool = StubPool()
        sweep = str(self.sweep)
        for argv in (["--sweep", str(self.dir / "missing.yaml")],
                     ["--sweep", sweep, "--train", "4 months"],
                     ["--sweep", sweep, "--test", "0d"],
                     ["--sweep", sweep, "--step", "5d", "--test", "30d"],             # step below test
                     ["--sweep", sweep, "--from", "2026-13-01"],
                     ["--sweep", sweep, "--workers", "0"],
                     ["--sweep", sweep, "--sensitivity-run", "../x"],
                     ["--sweep", sweep, "--train", "900d"]):                          # no window fits
            code, out, err, _ = self.run_cli(*argv, pool=pool)
            self.assertEqual(code, 2, (argv, err))
            self.assertTrue(err.strip(), argv)
        self.assertEqual(pool.calls, [])
        self.assertEqual(store.list_research_jobs(kind="walkforward"), [])             # no job row from a refusal

    def test_a_locked_holdout_exits_2_and_says_how_to_unlock(self):
        locked = holdout.HoldoutLocked("the holdout is locked; pass --unlock-holdout")
        with mock.patch.object(holdout, "enforce", side_effect=locked):
            code, _, err, pool = self.run_cli("--sweep", str(self.sweep), "--train", "30d", "--test", "10d",
                                              "--step", "10d")
        self.assertEqual(code, 2)
        self.assertIn("unlock", err)
        self.assertEqual(pool.calls, [])

    def test_workers_reach_every_pool_call(self):
        code, _, err, pool = self.run_cli("--sweep", str(self.sweep), "--train", "30d", "--test", "10d",
                                          "--step", "10d", "--workers", "3")
        self.assertEqual(code, 0, err)
        self.assertEqual([w for _, w in pool.calls], [3, 3])
        code, _, err, pool = self.run_cli("--sweep", str(self.sweep), "--train", "30d", "--test", "10d",
                                          "--step", "10d")
        self.assertEqual([w for _, w in pool.calls], [None, None])           # the sweep file names none

    def test_a_pool_that_returns_too_few_outcomes_is_a_failure_not_a_usage_error(self):
        for phase in (0, 1):                                                   # the training call, then the test call
            class Short(StubPool):
                def __call__(self, jobs, *, workers=None):
                    out = super().__call__(jobs, workers=workers)
                    return out[:-1] if len(self.calls) == phase + 1 else out
            code, _, err, _ = self.run_cli("--sweep", str(self.sweep), "--train", "30d", "--test", "10d",
                                           "--step", "10d", pool=Short())
            self.assertEqual(code, 1, (phase, err))
            self.assertIn("outcomes", err)
            job = store.list_research_jobs(kind="walkforward")[0]                # newest first
            self.assertEqual(job["status"], "failed")
            store.update_research_job(job["job_id"], created_ns=1)               # so the next loop's job sorts first

    def test_the_sensitivity_run_goes_into_the_report(self):
        write_sensitivity("senbase")
        code, _, err, _ = self.run_cli("--sweep", str(self.sweep), "--train", "30d", "--test", "10d", "--step", "10d",
                                       "--sensitivity-run", "senbase")
        self.assertEqual(code, 0, err)
        job = store.list_research_jobs(kind="walkforward")[0]
        self.assertIn("All costs ×2", (paths.research_path(job["job_id"]) / "report.md").read_text(encoding="utf-8"))

    def test_exit_1_when_a_window_could_not_be_tested(self):
        class Failing(StubPool):
            def __call__(self, jobs, *, workers=None):
                out = super().__call__(jobs, workers=workers)
                if jobs and jobs[0].role == "oos":
                    out[0] = PoolOutcome(None, "failed", "the run broke")
                return out
        code, out, err, _ = self.run_cli("--sweep", str(self.sweep), "--train", "30d", "--test", "10d",
                                         "--step", "10d", pool=Failing())
        self.assertEqual(code, 1, err)
        self.assertIn("test run did not finish", (paths.research_path(store.list_research_jobs(kind="walkforward")[0]["job_id"])
                                          / "report.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

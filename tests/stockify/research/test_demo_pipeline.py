"""M6.12 (WF 6.7): the `research report` command, and the whole demo pipeline (sweep, walk-forward, sensitivity,
report) on the tiny oracle fixture through the real pool. The only stub is the pool's `impl`: the children run the
oracle, not the C++ engine, so no build is needed."""
from __future__ import annotations

import contextlib
import io
import time
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import stockify.store as store
import stockify_paths
from stockify import __main__ as cli, paths, runconfig
from stockify.research import holdout, pool, sensitivity, sweep, walkforward as wf, wf_report
from tests.stockify.research.test_walkforward_finish import JOB, build_job
from tests.stockify.research.test_wf_report import ReportBase, plain_params_windows, write_sensitivity
from tests.stockify.test_bundle import BundleTest, run as run_config
from tests.stockify.test_runs_launch import BUY


def research_report(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["research", "report", *argv])
    return code, out.getvalue(), err.getvalue()


class ResearchReportCommandTests(ReportBase):
    def test_research_report_command_rerenders_with_the_sensitivity_table(self):
        job = self.finished()
        write_sensitivity("senbase")
        folder = paths.research_path(job)
        self.assertFalse((folder / "report.md").exists())
        code, out, err = research_report("--walkforward", job)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.strip(), str(folder / "report.md"))
        self.assertIn("not run yet", (folder / "report.md").read_text(encoding="utf-8"))
        code, out, err = research_report("--walkforward", job, "--sensitivity-run", "senbase")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("All costs x2", (folder / "report.md").read_text(encoding="utf-8"))

    def test_research_report_unknown_job_exits_2(self):
        for bad in ("wf-nothing-here", "../escape"):
            code, out, err = research_report("--walkforward", bad)
            self.assertEqual((code, out), (2, ""), bad)
            self.assertTrue(err.strip(), bad)

    def test_a_job_that_is_not_done_exits_2_and_names_its_status(self):
        build_job(plain_params_windows())                                    # running: finish has not run
        code, out, err = research_report("--walkforward", JOB)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("running", err)
        self.assertFalse((paths.research_path(JOB) / "report.md").exists())

    def test_an_unsafe_sensitivity_run_id_exits_2(self):
        job = self.finished()
        code, _, err = research_report("--walkforward", job, "--sensitivity-run", "../x")
        self.assertEqual(code, 2)
        self.assertIn("run id", err)

    def test_a_finished_job_without_its_file_is_an_internal_failure_exit_1(self):
        job = self.finished()
        (paths.research_path(job) / "walkforward.json").unlink()
        code, _, err = research_report("--walkforward", job)
        self.assertEqual(code, 1)
        self.assertIn("walkforward.json", err)

    def test_help_says_the_sensitivity_run_is_given_explicitly(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["research", "report", "--help"])
        text = " ".join(out.getvalue().split())
        self.assertIn("--sensitivity-run", text)
        self.assertIn("not stored on the walk-forward job", text)


def oracle_pool(jobs, *, workers=None):
    """The real pool, real child processes, the oracle in place of the C++ engine."""
    return pool.run_all([replace(j, impl="oracle") for j in jobs], workers=workers)


class DemoPipelineTests(BundleTest):
    """The fixture bars cover 2025-12-22 to 2025-12-31, before the shipped holdout (research.json holdout_from)."""

    def setUp(self):
        super().setUp()
        support.reset_sqlite_singleton(store)
        self.addCleanup(support.reset_sqlite_singleton, store)

    def test_demo_pipeline_on_the_fixture(self):
        t0 = time.monotonic()
        base = runconfig.validate(run_config(name="demo", strategy=BUY))
        hold = holdout.holdout_from()                       # the fixture's days must be before it, so no unlock is needed
        self.assertTrue(hold is None or date(2025, 12, 31) < hold)
        spec = sweep.SweepSpec(name="demo", base=base, axes={"capital_paise": [10_000_000, 20_000_000],
                                                             "seed": [0, 1]},
                               search={"kind": "grid"}, objective="net_pnl", workers=2, source=None)
        # 1. sweep
        swept = sweep.run_sweep(spec, pool_run=oracle_pool)
        self.assertEqual([r["status"] for r in swept.rows], ["done"] * 4, [r["error"] for r in swept.rows])
        self.assertIsNotNone(swept.best_run_id)
        # 2. walk-forward, then finish
        res = wf.run_windows(spec, train=3, test=3, step=3, objective="net_pnl", pool_run=oracle_pool, workers=2)
        fin = wf.finish(res.job_id)
        self.assertGreaterEqual(len(fin.windows), 2)
        self.assertEqual(store.get_research_job(res.job_id)["status"], "done")
        # 3. sensitivity on the sweep's best run
        sens = sensitivity.run_suite(swept.best_run_id, workers=2, pool_run=oracle_pool)
        self.assertEqual(sens.run_id, swept.best_run_id)
        self.assertTrue((paths.run_path(swept.best_run_id) / "sensitivity.json").is_file())
        # 4. report, through the command
        code, out, err = research_report("--walkforward", res.job_id, "--sensitivity-run", swept.best_run_id)
        self.assertEqual((code, err), (0, ""))
        report = paths.research_path(res.job_id) / "report.md"
        self.assertEqual(Path(out.strip()), report)
        text = report.read_text(encoding="utf-8")
        self.assertIn("after allowing for luck", text)
        self.assertIn("Out-of-sample against in-sample:", text)
        self.assertIn("Costs and fills changed one at a time", text)
        self.assertIn("| Variant | Status | Net P&L | Change | Sharpe |", text)
        self.assertIn("All costs ×2", text)
        self.assertEqual(report.read_text(encoding="utf-8"), wf_report.build(*wf_report.load(res.job_id),
                                                                              swept.best_run_id))
        # no holdout was reached, so nothing was unlocked
        self.assertEqual(store.holdout_unlock_rows(), [])
        # everything written lies under the sandbox
        state = stockify_paths.state_dir().resolve()
        written = [p for root in (paths.exports_dir(), paths.research_dir(), paths.runs_dir()) for p in root.rglob("*")]
        self.assertTrue(written)
        for p in written:
            self.assertTrue(p.resolve().is_relative_to(state), p)
        print(f"demo pipeline wall time: {time.monotonic() - t0:.1f}s")


if __name__ == "__main__":
    unittest.main()

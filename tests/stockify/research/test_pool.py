"""M6.2 (PM 6.2): the bounded pool of `python -m stockify run` subprocesses, with a fake popen and one real pair."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports

import stockify.store as store
from stockify import __main__ as cli
from stockify import paths, research
from stockify.research import pool
from stockify.research.pool import PoolJob, PoolOutcome
from tests.stockify.test_bundle import BundleTest, run as run_config
from tests.stockify.test_runs_launch import BUY


class FakePopen:
    """Stands in for subprocess.Popen: a child that finishes after `script[job_id]["polls"]` calls of poll()."""
    live = 0
    peak = 0
    calls: list = []
    script: dict = {}
    instances: list = []

    def __init__(self, args, *, stdout=None, stderr=None, env=None, **kw):
        self.args, self.child_env, self.kw = list(args), env, kw
        self.stdout, self.stderr = stdout, stderr
        self.job_id = self.args[self.args.index("--job-id") + 1]
        self.plan = {"polls": 1, "code": 0, "out": "", "err": "", **FakePopen.script.get(self.job_id, {})}
        self.returncode = None
        self.killed = self.waited = False
        self.config_text = Path(self.args[self.args.index("--config") + 1]).read_text(encoding="utf-8")
        FakePopen.live += 1
        FakePopen.peak = max(FakePopen.peak, FakePopen.live)
        FakePopen.calls.append(self.job_id)
        FakePopen.instances.append(self)

    def _finish(self, code):
        self.returncode = code
        FakePopen.live -= 1
        self.stdout.write(self.plan["out"])
        self.stderr.write(self.plan["err"])
        self.stdout.flush()
        self.stderr.flush()

    def poll(self):
        if self.returncode is None:
            self.plan["polls"] -= 1
            if self.plan["polls"] <= 0:
                self._finish(self.plan["code"])
        return self.returncode

    def kill(self):
        if self.returncode is None:
            self.killed = True
            self._finish(-9)

    def wait(self, timeout=None):
        self.waited = True
        return self.returncode


class PoolTestBase(unittest.TestCase):
    def setUp(self):
        FakePopen.live = FakePopen.peak = 0
        FakePopen.calls, FakePopen.script, FakePopen.instances = [], {}, []
        p = mock.patch.object(pool, "_POLL_SECONDS", 0)
        p.start()
        self.addCleanup(p.stop)

    def jobs(self, n, **kw):
        return [PoolJob({"name": f"c{i}"}, job_id=f"j{i}", **kw) for i in range(n)]


class PoolBehaviourTests(PoolTestBase):
    def test_at_most_workers_run_at_once(self):
        for i in range(7):
            FakePopen.script[f"j{i}"] = {"polls": 3}
        out = pool.run_all(self.jobs(7), workers=3, popen=FakePopen)
        self.assertEqual(len(out), 7)
        self.assertEqual(FakePopen.peak, 3)

    def test_results_are_in_input_order_even_when_jobs_finish_out_of_order(self):
        FakePopen.script = {"j0": {"polls": 9, "out": "C:/lab/runs/r0\n"}, "j1": {"polls": 1, "out": "C:/lab/runs/r1\n"},
                            "j2": {"polls": 4, "out": "C:/lab/runs/r2\n"}}
        out = pool.run_all(self.jobs(3), workers=3, popen=FakePopen)
        self.assertEqual([o.run_id for o in out], ["r0", "r1", "r2"])
        self.assertEqual([o.status for o in out], ["done"] * 3)

    def test_failed_job_is_reported_not_raised(self):
        FakePopen.script = {"j1": {"code": 1, "err": "summary\nrunner exited 3: boom\n"}, "j0": {"out": "x/r0\n"}}
        out = pool.run_all(self.jobs(2), workers=2, popen=FakePopen)
        self.assertEqual(out[0], PoolOutcome("r0", "done", None))
        self.assertEqual(out[1], PoolOutcome(None, "failed", "runner exited 3: boom"))

    def test_refused_config_exit_2_has_the_message(self):
        msg = "holdout is locked: say why"
        FakePopen.script = {"j0": {"code": 2, "err": msg + "\n"}}
        (o,) = pool.run_all(self.jobs(1), popen=FakePopen)
        self.assertEqual(o, PoolOutcome(None, "failed", msg))

    def test_a_crash_or_a_kill_code_is_failed_with_the_last_stderr_line(self):
        FakePopen.script = {"j0": {"code": -9, "err": "Traceback\nMemoryError\n"}, "j1": {"code": 7, "err": ""}}
        a, b = pool.run_all(self.jobs(2), popen=FakePopen)
        self.assertEqual((a.status, a.error), ("failed", "MemoryError"))
        self.assertEqual((b.status, b.error), ("failed", "exit code 7"))

    def test_a_child_that_cannot_start_is_failed_not_raised(self):
        def boom(*a, **k):
            raise OSError("no such interpreter")
        (o,) = pool.run_all(self.jobs(1), popen=boom)
        self.assertEqual((o.status, o.run_id), ("failed", None))
        self.assertIn("no such interpreter", o.error)

    def test_command_line_and_files_not_pipes(self):
        pool.run_all([PoolJob({"name": "c"}, role="sensitivity", job_id="jx", impl="oracle")], popen=FakePopen)
        (p,) = FakePopen.instances
        self.assertEqual(p.args[:5], [sys.executable, "-m", "stockify", "run", "--config"])
        self.assertEqual(p.args[6:], ["--role", "sensitivity", "--job-id", "jx", "--impl", "oracle", "--no-report"])
        self.assertEqual(json.loads(p.config_text), {"name": "c"})
        self.assertTrue(hasattr(p.stdout, "write") and hasattr(p.stderr, "write"))   # files, never subprocess.PIPE

    def test_no_work_no_children(self):
        self.assertEqual(pool.run_all([], popen=FakePopen), [])

    def test_temp_files_are_removed(self):
        before = set(paths.research_dir().iterdir())
        pool.run_all(self.jobs(3), popen=FakePopen)
        self.assertEqual(set(paths.research_dir().iterdir()), before)
        mine = paths.lab_dir() / "mine"
        mine.mkdir()
        pool.run_all(self.jobs(2), popen=FakePopen, tmp_dir=mine)
        self.assertEqual(list(mine.iterdir()), [])

    def test_temp_files_are_removed_after_an_interrupt(self):
        before = set(paths.research_dir().iterdir())
        FakePopen.script = {"j0": {"polls": 99}}
        with mock.patch.object(pool, "_sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                pool.run_all(self.jobs(1), popen=FakePopen)
        self.assertEqual(set(paths.research_dir().iterdir()), before)

    def test_children_inherit_the_sandbox_environment(self):
        pool.run_all(self.jobs(1), popen=FakePopen)
        self.assertEqual(FakePopen.instances[0].child_env["LOCALAPPDATA"], os.environ["LOCALAPPDATA"])

    def test_keyboard_interrupt_kills_children(self):
        for i in range(4):
            FakePopen.script[f"j{i}"] = {"polls": 99}
        with mock.patch.object(pool, "_sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                pool.run_all(self.jobs(4), workers=2, popen=FakePopen)
        self.assertEqual(len(FakePopen.instances), 2)                  # the other two never started
        self.assertTrue(all(p.killed and p.waited for p in FakePopen.instances))
        self.assertEqual(FakePopen.live, 0)

    def test_run_all_uses_default_workers_when_none_given(self):
        FakePopen.script = {f"j{i}": {"polls": 3} for i in range(5)}
        with mock.patch.object(pool, "default_workers", return_value=2) as dw:
            pool.run_all(self.jobs(5), popen=FakePopen)
        dw.assert_called_once_with(None)
        self.assertEqual(FakePopen.peak, 2)


class DefaultWorkersTests(unittest.TestCase):
    def test_default_workers_is_cpu_minus_one_and_at_least_one(self):
        for cpus, want in ((1, 1), (2, 1), (16, 15), (None, 1), (0, 1)):
            with self.subTest(cpus=cpus), mock.patch.object(os, "cpu_count", return_value=cpus):
                self.assertEqual(pool.default_workers(), want)

    def test_requested_is_a_ceiling_never_below_one(self):
        with mock.patch.object(os, "cpu_count", return_value=16):
            self.assertEqual(pool.default_workers(4), 4)
            self.assertEqual(pool.default_workers(99), 15)
            self.assertEqual(pool.default_workers(0), 15)      # 0 / None = no request
            self.assertEqual(pool.default_workers(-3), 1)

    def test_sweep_max_workers_setting_caps_it(self):
        base = research.settings()
        for cap, req, want in ((2, None, 2), (2, 8, 2), (20, None, 15), (None, None, 15)):
            cfg = {**base, "sweep": {**base["sweep"], "max_workers": cap}}
            with self.subTest(cap=cap, req=req), mock.patch.object(os, "cpu_count", return_value=16), \
                    mock.patch.object(research, "settings", return_value=cfg):
                self.assertEqual(pool.default_workers(req), want)


class CliFlagTests(unittest.TestCase):
    def setUp(self):
        quiet = contextlib.ExitStack()
        quiet.enter_context(contextlib.redirect_stdout(io.StringIO()))
        quiet.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.addCleanup(quiet.close)

    def test_run_passes_role_and_job_id_to_launch(self):
        with mock.patch("stockify.runs.launch", return_value="r1") as launch, \
                mock.patch.object(store, "get_run", return_value={"folder": "f"}):
            code = cli.main(["run", "--config", "x.json", "--role", "sensitivity", "--job-id", "j9", "--no-report"])
        self.assertEqual(code, 0)
        self.assertEqual((launch.call_args.kwargs["role"], launch.call_args.kwargs["job_id"]), ("sensitivity", "j9"))

    def test_role_defaults_to_trial_and_a_bad_role_is_refused(self):
        with mock.patch("stockify.runs.launch", return_value="r1") as launch, \
                mock.patch.object(store, "get_run", return_value={"folder": "f"}):
            cli.main(["run", "--config", "x.json"])
        self.assertEqual((launch.call_args.kwargs["role"], launch.call_args.kwargs["job_id"]), ("trial", None))
        with self.assertRaises(SystemExit):
            cli.main(["run", "--config", "x.json", "--role", "nope"])


class RealProcessTests(BundleTest):
    """Real children. Their data ends in 2025, before the shipped holdout (2026-08-01); the sandboxed
    LOCALAPPDATA is inherited, so they read this test's bars and write this test's lab."""

    def setUp(self):
        super().setUp()
        support.reset_sqlite_singleton(store)
        self.addCleanup(support.reset_sqlite_singleton, store)

    def test_two_real_subprocesses_run_a_tiny_config(self):
        jobs = [PoolJob(run_config(name=n, strategy=BUY), role="sensitivity", job_id="job-" + n, impl="oracle")
                for n in ("pa", "pb")]
        out = pool.run_all(jobs, workers=2)
        self.assertEqual([o.status for o in out], ["done", "done"], out)
        self.assertEqual(len({o.run_id for o in out}), 2)
        for o, n in zip(out, ("pa", "pb")):
            row = store.get_run(o.run_id)
            self.assertEqual((row["role"], row["job_id"], row["status"]), ("sensitivity", "job-" + n, "done"))

    def test_a_refused_config_comes_back_failed_with_its_message(self):
        bad = run_config(name="bad", strategy=BUY, capital_paise=0)
        (o,) = pool.run_all([PoolJob(bad, job_id="jb", impl="oracle")], workers=1)
        self.assertEqual(o.status, "failed")
        self.assertTrue(o.error.startswith("capital_paise"), o.error)


if __name__ == "__main__":
    unittest.main()

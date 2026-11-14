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
from stockify import paths, research, runs
from stockify.runconfig import RunConfigError
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


def rp(name: str) -> str:
    """What a finished child prints on stdout: its run folder, a direct child of the runs dir."""
    return f"{paths.runs_dir() / name}\n"


class PoolTestBase(unittest.TestCase):
    def setUp(self):
        FakePopen.live = FakePopen.peak = 0
        FakePopen.calls, FakePopen.script, FakePopen.instances = [], {}, []
        p = mock.patch.object(pool, "_POLL_SECONDS", 0)
        p.start()
        self.addCleanup(p.stop)

    def jobs(self, n, **kw):
        return [PoolJob({"name": f"c{i}"}, job_id=f"j{i}", **kw) for i in range(n)]

class PoolCancelTests(PoolTestBase):
    """DM 7.4 (I2): a job started from the web can be stopped: the live children are killed, no pending one starts."""

    def test_a_set_event_kills_the_live_children_starts_no_more_and_raises_cancelled(self):
        import threading
        ev = threading.Event()
        FakePopen.script = {"j0": {"polls": 99}, "j1": {"polls": 99}, "j2": {"polls": 99}}
        token = pool.set_cancel(ev)
        self.addCleanup(pool.reset_cancel, token)
        with mock.patch.object(pool, "_sleep", side_effect=ev.set), self.assertRaises(pool.Cancelled):
            pool.run_all(self.jobs(3), workers=1, popen=FakePopen)
        self.assertEqual(FakePopen.calls, ["j0"])                 # j1 and j2 never started
        self.assertTrue(FakePopen.instances[0].killed)

    def test_without_an_event_nothing_changes(self):
        FakePopen.script = {"j0": {"out": rp("r0")}}
        (o,) = pool.run_all(self.jobs(1), popen=FakePopen)
        self.assertEqual(o.status, "done")


class PoolBehaviourTests(PoolTestBase):
    def test_at_most_workers_run_at_once(self):
        for i in range(7):
            FakePopen.script[f"j{i}"] = {"polls": 3}
        out = pool.run_all(self.jobs(7), workers=3, popen=FakePopen)
        self.assertEqual(len(out), 7)
        self.assertEqual(FakePopen.peak, 3)

    def test_results_are_in_input_order_even_when_jobs_finish_out_of_order(self):
        FakePopen.script = {"j0": {"polls": 9, "out": rp("r0")}, "j1": {"polls": 1, "out": rp("r1")},
                            "j2": {"polls": 4, "out": rp("r2")}}
        out = pool.run_all(self.jobs(3), workers=3, popen=FakePopen)
        self.assertEqual([o.run_id for o in out], ["r0", "r1", "r2"])
        self.assertEqual([o.status for o in out], ["done"] * 3)

    def test_failed_job_is_reported_not_raised(self):
        FakePopen.script = {"j1": {"code": 1, "err": "summary\nrunner exited 3: boom\n"}, "j0": {"out": rp("r0")}}
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

    def test_kill_uses_taskkill_on_the_process_tree_when_there_is_a_pid(self):
        FakePopen.script = {"j0": {"polls": 99}}
        FakePopen.pid = 4321
        self.addCleanup(lambda: delattr(FakePopen, "pid"))
        with mock.patch.object(pool, "_sleep", side_effect=KeyboardInterrupt), \
                mock.patch.object(pool.os, "name", "nt"), mock.patch.object(pool.subprocess, "run") as run:
            with self.assertRaises(KeyboardInterrupt):
                pool.run_all(self.jobs(1), popen=FakePopen)
        self.assertEqual(run.call_args.args[0], ["taskkill", "/PID", "4321", "/T", "/F"])
        self.assertTrue(FakePopen.instances[0].killed)

    def test_the_run_id_is_the_last_reported_folder_not_the_first_line(self):
        noise = "UserWarning: something\nprinted by strategy code\n"
        FakePopen.script = {"j0": {"out": noise + "C:/elsewhere/r9\n" + rp("real")},
                            "j1": {"out": "just a warning\n"}, "j2": {"out": rp("a") + "trailing text\n"}}
        a, b, c = pool.run_all(self.jobs(3), popen=FakePopen)
        self.assertEqual(a, PoolOutcome("real", "done", None))
        self.assertEqual(b, PoolOutcome(None, "failed", "the run did not report its folder"))
        self.assertEqual(c, PoolOutcome("a", "done", None))             # last line that is a runs folder

    def test_a_path_outside_the_runs_folder_is_not_a_run(self):
        FakePopen.script = {"j0": {"out": str(paths.lab_dir() / "other" / "r0") + "\n"}}
        (o,) = pool.run_all(self.jobs(1), popen=FakePopen)
        self.assertEqual((o.run_id, o.status), (None, "failed"))

    def test_one_kill_that_raises_does_not_leave_the_rest_running(self):
        for i in range(3):
            FakePopen.script[f"j{i}"] = {"polls": 99}
        real_kill = FakePopen.kill

        def flaky(self):
            if self.job_id == "j0":
                raise RuntimeError("access denied")
            real_kill(self)
        with mock.patch.object(FakePopen, "kill", flaky), mock.patch.object(pool, "_sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                pool.run_all(self.jobs(3), workers=3, popen=FakePopen)
        self.assertEqual([p.killed for p in FakePopen.instances], [False, True, True])

    def test_child_env_is_the_parents_plus_utf8_io_and_nothing_else(self):
        pool.run_all(self.jobs(1), popen=FakePopen)
        self.assertEqual(FakePopen.instances[0].child_env, {**os.environ, "PYTHONIOENCODING": "utf-8"})

    def test_non_ascii_output_survives(self):
        FakePopen.script = {"j0": {"code": 1, "err": "bad: \u20b9 caf\u00e9\n"}}
        (o,) = pool.run_all(self.jobs(1), popen=FakePopen)
        self.assertEqual(o.error, "bad: \u20b9 caf\u00e9")

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


class WallTimeLimitTests(BundleTest):
    """XI 6.45: a child that runs past research.json `pool.max_run_hours` is killed, with its tree, and failed."""

    def setUp(self):
        super().setUp()
        support.reset_sqlite_singleton(store)
        self.addCleanup(support.reset_sqlite_singleton, store)
        FakePopen.live = FakePopen.peak = 0
        FakePopen.calls, FakePopen.script, FakePopen.instances = [], {}, []
        p = mock.patch.object(pool, "_POLL_SECONDS", 0)
        p.start()
        self.addCleanup(p.stop)

    def fake_clock(self, step):
        ticks = iter(range(0, 10 ** 9, step))
        p = mock.patch.object(pool, "_now", lambda: next(ticks))
        p.start()
        self.addCleanup(p.stop)

    def test_a_child_past_the_limit_is_killed_and_failed_with_a_plain_reason_and_the_others_go_on(self):
        FakePopen.script = {"j0": {"polls": 10 ** 9}, "j1": {"polls": 1, "out": rp("r1")}}
        self.fake_clock(1000)
        with mock.patch.object(pool, "_max_run_seconds", return_value=5):
            a, b = pool.run_all([PoolJob({"name": "c0"}, job_id="j0"), PoolJob({"name": "c1"}, job_id="j1")],
                                workers=2, popen=FakePopen)
        self.assertEqual((a.status, a.run_id), ("failed", None))
        self.assertRegex(a.error, r"^stopped after .* hours? \(the pool\.max_run_hours limit in research\.json\)$")
        self.assertTrue(FakePopen.instances[0].killed)
        self.assertEqual(b, PoolOutcome("r1", "done", None))

    def test_the_limit_comes_from_the_setting_in_hours(self):
        cfg = {**research.settings(), "pool": {"max_run_hours": 3}}
        with mock.patch.object(research, "settings", return_value=cfg):
            self.assertEqual(pool._max_run_seconds(), 3 * 3600)
        self.assertEqual(pool._max_run_seconds(), 6 * 3600)

    def test_a_child_inside_the_limit_is_left_alone(self):
        FakePopen.script = {"j0": {"polls": 3, "out": rp("r0")}}
        self.fake_clock(1)
        with mock.patch.object(pool, "_max_run_seconds", return_value=10_000):
            (o,) = pool.run_all([PoolJob({"name": "c0"}, job_id="j0")], popen=FakePopen)
        self.assertEqual(o.status, "done")
        self.assertFalse(FakePopen.instances[0].killed)

    def test_the_runs_row_the_child_had_started_is_marked_failed_too(self):
        store.create_run({"run_id": "stuck", "name": "c0", "status": "running", "job_id": "j0",
                          "started_ns": 1, "role": "trial"})
        (paths.run_dir("stuck") / "runner.pid").write_text("999999 123\n", encoding="utf-8")
        store.create_run({"run_id": "other", "name": "c9", "status": "running", "started_ns": 1})
        (paths.run_dir("other") / "runner.pid").write_text("888888 123\n", encoding="utf-8")
        FakePopen.script = {"j0": {"polls": 10 ** 9}}

        class WithPid(FakePopen):
            pid = 999999
        self.fake_clock(1000)
        with mock.patch.object(pool, "_max_run_seconds", return_value=5), \
                mock.patch.object(pool, "_kill", side_effect=lambda proc: proc.kill()):
            pool.run_all([PoolJob({"name": "c0"}, job_id="j0")], popen=WithPid)
        row = store.get_run("stuck")
        self.assertEqual(row["status"], "failed")
        self.assertRegex(row["error"], r"^stopped after .* hours? \(the pool\.max_run_hours limit")
        self.assertEqual(store.get_run("other")["status"], "running")        # another process's run is untouched

    def test_a_real_sleeping_child_and_its_tree_are_killed_at_a_tiny_limit(self):
        import subprocess
        import time
        children = []

        def sleeper(cmd, *, stdout, stderr, env):
            code = ("import subprocess, sys, time; "
                    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); time.sleep(120)")
            proc = subprocess.Popen([sys.executable, "-c", code], stdout=stdout, stderr=stderr, env=env)
            children.append(proc)
            return proc

        started = time.monotonic()
        with mock.patch.object(pool, "_max_run_seconds", return_value=1.5), \
                mock.patch.object(pool, "_POLL_SECONDS", 0.05):
            (o,) = pool.run_all([PoolJob({"name": "c0"}, job_id="j0")], popen=sleeper)
        self.assertLess(time.monotonic() - started, 60)
        self.assertEqual(o.status, "failed")
        self.assertTrue(o.error.startswith("stopped after"), o.error)
        self.assertIsNotNone(children[0].poll())                    # the child is gone


class VariantNeedsARealJobTests(BundleTest):
    """A role other than trial takes a run out of N, so only a real research job may give one (queue and launch)."""

    def setUp(self):
        super().setUp()
        support.reset_sqlite_singleton(store)
        self.addCleanup(support.reset_sqlite_singleton, store)

    FAM = "fam/x/1"

    def cfg(self, name="t", family=FAM):
        return run_config(name=name, strategy=BUY, family_id=family)

    def job(self, job_id="real", kind="sensitivity", status="running", family=FAM):
        store.create_research_job({"job_id": job_id, "kind": kind, "status": status, "family_id": family,
                                   "created_ns": 1})

    def test_a_variant_role_without_a_real_job_is_refused_everywhere(self):
        for role in ("sensitivity", "oos", "rerun"):
            for job in (None, "", "nope"):
                with self.subTest(role=role, job=job):
                    with self.assertRaisesRegex(RunConfigError, "role .* (needs a job_id|no such research job)"):
                        runs.queue(self.cfg(), role=role, job_id=job)
                    with self.assertRaisesRegex(RunConfigError, "role .* (needs a job_id|no such research job)"):
                        runs.launch(self.cfg(), role=role, job_id=job, impl="oracle", report=False)
        self.assertEqual(store.list_runs(), [])

    def test_cli_refuses_it_with_exit_2_and_a_plain_message(self):
        path = paths.lab_dir() / "v.run.json"
        path.write_text(json.dumps(self.cfg()), encoding="utf-8")
        for argv in (["--role", "sensitivity"], ["--role", "oos", "--job-id", "ghost"]):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(["run", "--config", str(path), "--impl", "oracle", "--no-report", *argv])
            self.assertEqual(code, 2)
            self.assertRegex(err.getvalue(), "^role '(sensitivity|oos)'")
        self.assertEqual(store.list_runs(), [])

    def test_a_real_research_job_may_mark_its_runs(self):
        self.job("real")
        self.job("wf", kind="walkforward")
        rid = runs.launch(self.cfg(), role="sensitivity", job_id="real", impl="oracle", report=False)
        self.assertEqual((store.get_run(rid)["role"], store.get_run(rid)["job_id"]), ("sensitivity", "real"))
        self.assertTrue(runs.queue(self.cfg("t2"), role="oos", job_id="wf"))

    def test_a_job_of_the_wrong_kind_is_refused(self):
        self.job("sj", kind="sensitivity")
        self.job("wj", kind="walkforward")
        self.job("sw", kind="sweep")
        for role, job in (("oos", "sj"), ("sensitivity", "wj"), ("sensitivity", "sw"), ("oos", "sw")):
            with self.subTest(role=role, job=job):
                with self.assertRaisesRegex(RunConfigError, "role .* belongs to a .* job"):
                    runs.queue(self.cfg(), role=role, job_id=job)
                with self.assertRaisesRegex(RunConfigError, "role .* belongs to a .* job"):
                    runs.launch(self.cfg(), role=role, job_id=job, impl="oracle", report=False)
        self.assertEqual(store.list_runs(), [])

    def test_a_job_that_is_not_running_is_refused(self):
        for status in ("done", "failed", "cancelled"):
            self.job("j-" + status, status=status)
            with self.subTest(status=status):
                with self.assertRaisesRegex(RunConfigError, "is %s, not running" % status) as cm:
                    runs.queue(self.cfg(), role="sensitivity", job_id="j-" + status)
                self.assertNotIn("j-" + status, str(cm.exception))
                with self.assertRaisesRegex(RunConfigError, "is %s, not running" % status):
                    runs.launch(self.cfg(), role="sensitivity", job_id="j-" + status, impl="oracle", report=False)
        self.assertEqual(store.list_runs(), [])

    def test_a_run_of_another_family_is_refused_and_leaves_no_folder(self):
        self.job("real")
        for fam in ("other/fam/1", None):          # an explicit other family, and the default one
            with self.subTest(family=fam):
                cfg = self.cfg(family=fam) if fam else run_config(name="t", strategy=BUY)
                with self.assertRaisesRegex(RunConfigError, "not the family of the research job") as cm:
                    runs.queue(cfg, role="sensitivity", job_id="real")
                self.assertNotIn("other/fam", str(cm.exception))
                self.assertNotIn("real", str(cm.exception))
                with self.assertRaisesRegex(RunConfigError, "not the family of the research job"):
                    runs.launch(cfg, role="sensitivity", job_id="real", impl="oracle", report=False)
        self.assertEqual(store.list_runs(), [])
        self.assertEqual([f["family_id"] for f in store.list_families()], [])    # a refusal leaves no family row

    def test_no_refusal_echoes_the_typed_job_id(self):
        self.job("sj", kind="sensitivity")
        for role, job in (("sensitivity", "typed-ghost-id"), ("oos", "sj")):
            with self.assertRaises(RunConfigError) as cm:
                runs.queue(self.cfg(), role=role, job_id=job)
            self.assertNotIn(job, str(cm.exception))

    def test_trial_needs_no_job_and_rerun_still_works(self):
        rid = runs.launch(self.cfg(), impl="oracle", report=False)           # trial, no job
        self.assertEqual(store.get_run(rid)["role"], "trial")
        new_id, same = runs.rerun(rid)                                       # rerun writes its own row, exempt
        self.assertEqual(store.get_run(new_id)["role"], "rerun")
        self.assertTrue(same)


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
        for bad in ("nope", "rerun"):                      # "rerun" is only ever set by runs.rerun
            with self.subTest(role=bad), self.assertRaises(SystemExit):
                cli.main(["run", "--config", "x.json", "--role", bad])


class RealProcessTests(BundleTest):
    """Real children. Their data ends in 2025, before the shipped holdout (2026-08-01); the sandboxed
    LOCALAPPDATA is inherited, so they read this test's bars and write this test's lab."""

    def setUp(self):
        super().setUp()
        support.reset_sqlite_singleton(store)
        self.addCleanup(support.reset_sqlite_singleton, store)

    def test_two_real_subprocesses_run_a_tiny_config(self):
        for n in ("pa", "pb"):
            store.create_research_job({"job_id": "job-" + n, "kind": "sensitivity", "status": "running",
                                       "family_id": "fam/" + n + "/1", "created_ns": 1})
        jobs = [PoolJob(run_config(name=n, strategy=BUY, family_id="fam/" + n + "/1"), role="sensitivity", job_id="job-" + n, impl="oracle")
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


class CancelMarksRunRows(WallTimeLimitTests.__bases__[0]):
    def setUp(self):
        super().setUp()
        support.reset_sqlite_singleton(store)
        self.addCleanup(support.reset_sqlite_singleton, store)
        FakePopen.live = FakePopen.peak = 0
        FakePopen.calls, FakePopen.script, FakePopen.instances = [], {}, []
        p = mock.patch.object(pool, "_POLL_SECONDS", 0)
        p.start()
        self.addCleanup(p.stop)

    def test_a_killed_childs_run_row_is_cancelled_not_failed(self):
        import threading
        ev = threading.Event()
        store.create_run({"run_id": "kid", "name": "c0", "status": "running", "job_id": "j0", "started_ns": 1,
                          "role": "trial"})
        (paths.run_dir("kid") / "runner.pid").write_text("999999 123\n", encoding="utf-8")
        FakePopen.script = {"j0": {"polls": 10 ** 9}}

        class WithPid(FakePopen):
            pid = 999999
        token = pool.set_cancel(ev)
        self.addCleanup(pool.reset_cancel, token)
        with mock.patch.object(pool, "_sleep", side_effect=ev.set), \
                mock.patch.object(pool, "_kill", side_effect=lambda proc: proc.kill()), self.assertRaises(pool.Cancelled):
            pool.run_all([PoolJob({"name": "c0"}, job_id="j0")], popen=WithPid)
        row = store.get_run("kid")
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["error"], "cancelled by the user")

"""M3 review fixes for the strategy-test queue (server/services/test_queue.py), RD 4.2 onwards.

The fakes stand in for `python -m stockify run` subprocesses: each "runner" marks its row running (as the real
one does first), then exits with a chosen code, optionally after finishing its row as the real runner would."""
from __future__ import annotations

import threading

import tests.support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import stockify.store as store
from server.services.test_queue import TestQueue

_REAL_UPDATE = store.update_run  # the fakes write through this, so a patched store only hits the queue


class ExitingProc:
    def __init__(self, rc: int):
        self.rc = rc
        self.go = threading.Event()
        self.killed = False

    def wait(self) -> int:
        self.go.wait(30)
        return self.rc

    def kill(self) -> None:
        self.killed = True
        self.go.set()

    def poll(self) -> int | None:
        return self.rc if self.go.is_set() else None


class Launcher:
    """Each run gets a process with the given exit code; `finish` says what the runner writes before exiting."""

    def __init__(self, rc: int = 0, finish: str | None = None):
        self.rc, self.finish = rc, finish
        self.procs: dict[str, ExitingProc] = {}

    def __call__(self, run_id: str) -> ExitingProc:
        _REAL_UPDATE(run_id, status="running", only_if_status="queued")
        p = ExitingProc(self.rc)
        if self.finish:
            _REAL_UPDATE(run_id, status=self.finish, only_if_status="running")
        self.procs[run_id] = p
        p.go.set()
        return p


class ExitCodeTests(SandboxedDBTest):
    """RD 4.2: a runner that dies before writing its final status must not leave the row `running` for ever."""

    db_modules = (store,)

    def _run(self, launcher: Launcher, run_id: str = "r1") -> dict:
        store.create_run({"run_id": run_id, "status": "queued", "name": "n"})
        q = TestQueue(launcher=launcher, cap=1)
        q.submit(run_id)
        q.join(10)
        return store.get_run(run_id)

    def test_a_crashed_runner_fails_its_row(self):
        row = self._run(Launcher(rc=3))
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], "runner exited 3; see runner.log")
        self.assertIsNotNone(row["finished_ns"])

    def test_a_runner_that_exits_0_without_finishing_also_fails_it(self):
        row = self._run(Launcher(rc=0))
        self.assertEqual((row["status"], row["error"]), ("failed", "runner exited 0; see runner.log"))

    def test_a_finished_row_is_left_alone(self):
        for final, rc in (("done", 0), ("failed", 1), ("cancelled", 0)):
            with self.subTest(final=final):
                row = self._run(Launcher(rc=rc, finish=final), run_id=f"r-{final}")
                self.assertEqual(row["status"], final)
                self.assertIsNone(row["error"])

    def test_the_queue_keeps_going_after_a_crash(self):
        launcher = Launcher(rc=5)
        for i in range(3):
            store.create_run({"run_id": f"q{i}", "status": "queued", "name": "n"})
        q = TestQueue(launcher=launcher, cap=1)
        for i in range(3):
            q.submit(f"q{i}")
        q.join(10)
        self.assertEqual([store.get_run(f"q{i}")["status"] for i in range(3)], ["failed"] * 3)
        self.assertEqual((q.waiting(), q.running()), ([], []))


class RecoverTests(SandboxedDBTest):
    """RD 4.3: an API restart fails only the runs whose runner is gone; live runners are adopted and watched."""

    db_modules = (store,)

    def _sleeper(self):  # noqa: ANN202
        import subprocess
        import sys
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(lambda: (p.kill(), p.wait()))
        return p

    def _running_row(self, run_id: str, pidfile: str | None) -> None:
        from stockify import paths
        folder = paths.run_dir(run_id)
        if pidfile is not None:
            (folder / "runner.pid").write_text(pidfile, encoding="utf-8")
        store.create_run({"run_id": run_id, "status": "running", "name": "n", "folder": str(folder)})

    def test_a_live_runner_is_adopted_not_failed(self):
        from server.services.test_queue import INTERRUPTED
        from stockify import proc as P
        child = self._sleeper()
        self._running_row("live", f"{child.pid} {P.creation_time(child.pid)}")
        q = TestQueue(launcher=Launcher(), cap=4)
        requeued, failed = q.recover()
        self.assertEqual(failed, [])
        self.assertEqual(q.running(), ["live"])
        self.assertEqual(store.get_run("live")["status"], "running")
        child.kill()
        child.wait()
        q.join(15)
        row = store.get_run("live")
        self.assertEqual(row["status"], "failed")
        self.assertNotEqual(row["error"], INTERRUPTED)
        self.assertIn("ended without finishing", row["error"])

    def test_dead_reused_or_unknown_runners_are_failed(self):
        import os
        import subprocess
        import sys
        from server.services.test_queue import INTERRUPTED
        from stockify import proc as P
        done = subprocess.Popen([sys.executable, "-c", "pass"])
        created = P.creation_time(done.pid)
        done.wait()
        self._running_row("dead", f"{done.pid} {created}")
        self._running_row("reused", f"{os.getpid()} 12345")       # our pid, but not our creation time
        self._running_row("nofile", None)
        self._running_row("garbled", "not a pid")
        q = TestQueue(launcher=Launcher(), cap=4)
        _, failed = q.recover()
        self.assertEqual(sorted(failed), ["dead", "garbled", "nofile", "reused"])
        self.assertTrue(all(store.get_run(r)["error"] == INTERRUPTED for r in failed))
        self.assertEqual(q.running(), [])

    def test_cancel_kills_an_adopted_runner(self):
        from stockify import proc as P
        child = self._sleeper()
        self._running_row("live", f"{child.pid} {P.creation_time(child.pid)}")
        q = TestQueue(launcher=Launcher(), cap=4)
        q.recover()
        self.assertEqual(q.cancel("live"), "running")
        child.wait(10)                                             # killed by the queue
        q.join(15)
        self.assertEqual(store.get_run("live")["status"], "cancelled")


class PidFileTests(SandboxedDBTest):
    db_modules = (store,)

    def test_process_identity(self):
        import os
        from stockify import proc as P
        me = P.creation_time(os.getpid())
        self.assertTrue(P.is_alive(os.getpid(), me))
        self.assertFalse(P.is_alive(os.getpid(), me + 1))
        self.assertFalse(P.is_alive(2**31 - 7, me))


if __name__ == "__main__":
    import unittest
    unittest.main()


from tests.stockify.test_runs_launch import BUY, RunsTestBase  # noqa: E402


class RunnerClaimTests(RunsTestBase):
    def test_launch_records_its_process(self):
        import os
        from stockify import engine, paths, proc, runs
        if not engine.available():
            self.skipTest("stockify._engine is not built")
        rid = runs.queue(self.config(BUY))
        runs.launch(run_id=rid, report=False)
        pid, created = proc.read_pidfile(paths.run_path(rid))
        self.assertEqual(pid, os.getpid())
        self.assertTrue(proc.is_alive(pid, created))


class FinishRunTests(SandboxedDBTest):
    """RD 4.4: `done` and the child rows land together, or not at all."""

    db_modules = (store,)
    ROWS = {"orders": [{"order_id": 1, "side": "BUY"}], "fills": [], "equity": [], "charges": []}

    def test_done_and_rows_together(self):
        store.create_run({"run_id": "a", "status": "running"})
        self.assertTrue(store.finish_run("a", {"status": "done", "finished_ns": 7, "metrics_json": "{}"}, **self.ROWS))
        row = store.get_run("a")
        self.assertEqual((row["status"], row["finished_ns"], row["metrics_json"]), ("done", 7, "{}"))
        self.assertEqual(store.run_rows("a", "orders"), [{"order_id": 1, "side": "BUY"}])

    def test_a_failure_in_the_rows_leaves_the_run_running(self):
        store.create_run({"run_id": "a", "status": "running"})
        bad = dict(self.ROWS, orders=[{"order_id": 1}, {"no_id": 2}])
        with self.assertRaises(KeyError):
            store.finish_run("a", {"status": "done", "finished_ns": 7}, **bad)
        self.assertEqual(store.get_run("a")["status"], "running")
        self.assertEqual(store.run_rows("a", "orders"), [])

    def test_a_cancelled_run_gets_neither(self):
        store.create_run({"run_id": "a", "status": "cancelled"})
        self.assertFalse(store.finish_run("a", {"status": "done", "finished_ns": 7}, **self.ROWS))
        self.assertEqual(store.get_run("a")["status"], "cancelled")
        self.assertEqual(store.run_rows("a", "orders"), [])


class LockedDatabaseTests(SandboxedDBTest):
    """RD 4.5: a 'database is locked' error in one status write never strands the queue or loses a run."""

    db_modules = (store,)

    def _locked(self, times: int):  # noqa: ANN202
        """Patch store.update_run to raise 'database is locked' for the next `times` calls."""
        import sqlite3
        from unittest import mock
        real = store.update_run
        left = {"n": times}

        def flaky(*a, **kw):  # noqa: ANN002, ANN003, ANN202
            if left["n"] > 0:
                left["n"] -= 1
                raise sqlite3.OperationalError("database is locked")
            return real(*a, **kw)
        return mock.patch.object(store, "update_run", flaky)

    def test_a_locked_write_after_a_crash_is_retried(self):
        store.create_run({"run_id": "r1", "status": "queued", "name": "n"})
        q = TestQueue(launcher=Launcher(rc=3), cap=1)
        with self._locked(2):                        # the watcher's failed-write hits the lock twice
            q.submit("r1")
            q.join(15)
        self.assertEqual(store.get_run("r1")["status"], "failed")

    def test_a_launch_failure_with_a_locked_db_keeps_the_run_and_the_queue(self):
        for i in range(2):
            store.create_run({"run_id": f"q{i}", "status": "queued", "name": "n"})
        calls = []

        def launcher(run_id):  # noqa: ANN001, ANN202
            calls.append(run_id)
            if run_id == "q0":
                raise OSError("could not start")
            return Launcher(rc=0, finish="done")(run_id)
        q = TestQueue(launcher=launcher, cap=1)
        with self._locked(10**6):                    # every status write the queue makes fails meanwhile
            q.submit("q0")                           # must not raise into the API
            q.submit("q1")
            self.assertIn("q1", calls)               # the queue went on past q0
            self.assertIn("q0", q.waiting())         # q0 is not lost: still waiting
            self.assertEqual(store.get_run("q0")["status"], "queued")
        q.join(15)
        self.assertEqual(store.get_run("q1")["status"], "done")
        self.assertEqual(store.get_run("q0")["status"], "failed")     # recorded once the store answers again
        self.assertIn("could not start the runner", store.get_run("q0")["error"])

    def test_cancel_keeps_a_queued_run_when_the_write_fails(self):
        store.create_run({"run_id": "c", "status": "queued", "name": "n"})
        q = TestQueue(launcher=Launcher(), cap=0)    # cap 0: nothing starts, so 'c' stays waiting
        q.submit("c")
        import sqlite3
        with self._locked(10), self.assertRaises(sqlite3.OperationalError):
            q.cancel("c")
        self.assertEqual(q.waiting(), ["c"])                          # still there to cancel again
        self.assertEqual(q.cancel("c"), "queued")


class ApiBlockingTests(SandboxedDBTest):
    """RD 4.6: the tests API never blocks the event loop with SQLite or file reads, and log tails read the end."""

    db_modules = (store,)

    def test_handlers_are_plain_functions(self):
        import inspect
        from server.routers import tests as router
        endpoints = [r.endpoint for r in router.router.routes]
        self.assertGreater(len(endpoints), 8)
        self.assertEqual([e.__name__ for e in endpoints if inspect.iscoroutinefunction(e)], [])

    def test_tail_matches_the_whole_file_reading(self):
        from unittest import mock
        from server.routers.tests import _tail
        from stockify import paths
        f = paths.lab_dir() / "log.txt"
        cases = ["", "one", "one\n", "a\nb\nc", "a\nb\nc\n", "\n\n", "x\n" * 5000 + "last",
                 "é and ₹\nsecond ₹ line\n" * 3000]
        for text in cases:
            f.write_bytes(text.encode("utf-8"))
            whole = text.splitlines()
            with self.subTest(size=len(text)), mock.patch("pathlib.Path.read_text", side_effect=AssertionError):
                for limit in (0, 1, 3, 40, 10_000):
                    self.assertEqual(_tail(f, offset=None, limit=limit), (whole[-limit:] if limit else [], len(whole)))
                for offset, limit in ((0, 2), (1, 3), (4990, 50), (10**6, 5)):
                    self.assertEqual(_tail(f, offset=offset, limit=limit),
                                     (whole[offset:offset + limit], len(whole)))
        self.assertEqual(_tail(None, offset=None, limit=5), ([], 0))

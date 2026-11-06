"""M3 review fixes for the strategy-test queue (server/services/test_queue.py), RD 4.2 onwards.

The fakes stand in for `python -m stockify run` subprocesses: each "runner" marks its row running (as the real
one does first), then exits with a chosen code, optionally after finishing its row as the real runner would."""
from __future__ import annotations

import threading

import tests.support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import stockify.store as store
from server.services.test_queue import TestQueue


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
        store.update_run(run_id, status="running", only_if_status="queued")
        p = ExitingProc(self.rc)
        if self.finish:
            store.update_run(run_id, status=self.finish, only_if_status="running")
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


if __name__ == "__main__":
    import unittest
    unittest.main()

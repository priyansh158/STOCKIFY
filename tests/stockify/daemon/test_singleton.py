"""GK 5.1 (carried): one daemon per lab. The lock is an OS-level byte lock held for the process lifetime, so a holder
that dies, however it dies, never blocks the next daemon."""
from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock
from contextlib import redirect_stderr
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import __main__ as cli
from stockify import paths
from stockify.daemon import exitcodes, singleton

ROOT = Path(__file__).resolve().parents[3]


class SingletonTest(unittest.TestCase):
    def test_a_second_daemon_fails_fast_with_a_clear_message(self):
        with singleton.acquire():
            with self.assertRaises(singleton.AlreadyRunning) as ctx:
                singleton.acquire()
        message = str(ctx.exception)
        self.assertIn("another STOCKIFY daemon", message)
        self.assertIn(str(os.getpid()), message)                      # it names the holder
        self.assertIn(str(paths.daemon_lock_file()), message)

    def test_releasing_lets_the_next_daemon_in(self):
        first = singleton.acquire()
        first.release()
        first.release()                                               # twice is harmless
        with singleton.acquire():
            pass

    def test_a_killed_holder_never_blocks_the_next_daemon(self):
        code = ("import sys; from pathlib import Path; from stockify.daemon import singleton; "
                "import os; singleton.acquire(Path(sys.argv[1])); print('held', os.getpid(), flush=True); sys.stdin.read()")
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        proc = subprocess.Popen([sys.executable, "-c", code, str(paths.daemon_lock_file())], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, cwd=str(ROOT), env=env, text=True)
        try:
            held, pid = proc.stdout.readline().split()               # the real pid: the venv's python.exe is a launcher
            self.assertEqual(held, "held")
            with self.assertRaises(singleton.AlreadyRunning) as ctx:
                singleton.acquire()
            self.assertIn(pid, str(ctx.exception))
        finally:
            os.kill(int(pid), signal.SIGTERM)                         # TerminateProcess: no release, no atexit
            proc.kill()
            proc.wait(10)
        deadline = time.monotonic() + 10
        while True:                                                   # the OS drops the lock as the process dies
            try:
                singleton.acquire().release()
                break
            except singleton.AlreadyRunning:
                self.assertLess(time.monotonic(), deadline, "the dead holder's lock was never dropped")
                time.sleep(0.05)

    def test_the_cli_refuses_a_second_daemon_with_its_own_exit_code(self):
        self.assertNotIn(exitcodes.ALREADY_RUNNING, (exitcodes.OK, exitcodes.SLEEP, exitcodes.TOKEN_EXPIRED,
                                                     exitcodes.RECONNECT_EXHAUSTED))
        with singleton.acquire():
            for argv in (["daemon", "--recorder-only"], ["daemon"], ["daemon", "--sessions", "none"]):
                err = io.StringIO()
                with redirect_stderr(err):
                    self.assertEqual(cli.main(argv), exitcodes.ALREADY_RUNNING, argv)
                self.assertIn("another STOCKIFY daemon", err.getvalue())

    def test_an_os_error_is_a_clear_message_and_its_own_exit_code(self):
        self.assertNotIn(exitcodes.LOCK_FAILED, (exitcodes.ALREADY_RUNNING, exitcodes.ENGINE_STUCK))
        with mock.patch.object(os, "open", side_effect=PermissionError(13, "Access is denied")):
            with self.assertRaises(singleton.LockUnavailable) as ctx:
                singleton.acquire()
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(cli.main(["daemon", "--recorder-only"]), exitcodes.LOCK_FAILED)
        self.assertIn("cannot take the daemon lock", str(ctx.exception))
        self.assertIn("Access is denied", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_the_launcher_knows_every_daemon_code(self):
        text = (ROOT / "scripts" / "run_stockify_daemon.ps1").read_text(encoding="utf-8")
        self.assertIn(f"$ExitEngineStuck = {exitcodes.ENGINE_STUCK}", text)
        self.assertIn(f"$ExitLockFailed = {exitcodes.LOCK_FAILED}", text)
        self.assertIn("-eq $ExitLockFailed", text)                  # a lock that cannot be taken is not fixed by a restart

    def test_the_launcher_stops_on_that_code_instead_of_restarting(self):
        text = (ROOT / "scripts" / "run_stockify_daemon.ps1").read_text(encoding="utf-8")
        self.assertIn(f"$ExitAlreadyRunning = {exitcodes.ALREADY_RUNNING}", text)
        self.assertIn("-eq $ExitAlreadyRunning", text)


if __name__ == "__main__":
    unittest.main()

"""The strategy-test queue (M3.14): queued runs start as `python -m stockify run` subprocesses (I-9).

The API never runs the engine itself. At most `max(1, os.cpu_count() - 1)` runs go at once; the rest wait in
arrival order. The subprocess owns the run's status from `running` on (stockify.runs.launch); the queue only
writes `cancelled` (with `only_if_status`, so it never overwrites a finished run) and, on API start, `failed`
for runs a previous API process left `running`.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Callable, Protocol

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
INTERRUPTED = "interrupted: the API restarted"


class Process(Protocol):
    def wait(self) -> int: ...
    def kill(self) -> None: ...
    def poll(self) -> int | None: ...


Launcher = Callable[[str], Process]


def default_cap() -> int:
    return max(1, (os.cpu_count() or 2) - 1)


def subprocess_launcher(run_id: str) -> Process:
    """`python -m stockify run --config <runs>/<id>/request.json --run-id <id>`, output to runner.log."""
    from stockify import paths
    folder = paths.run_path(run_id)
    out = (folder / "runner.log").open("ab")
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "stockify", "run", "--config", str(folder / "request.json"), "--run-id", run_id],
            cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    finally:
        out.close()  # the child holds its own handle


class TestQueue:
    def __init__(self, launcher: Launcher = subprocess_launcher, cap: int | None = None):
        self.launcher = launcher
        self.cap = cap if cap is not None else default_cap()
        self._lock = threading.Lock()
        self._waiting: deque[str] = deque()
        self._running: dict[str, Process] = {}
        self._cancelled: set[str] = set()
        self._watchers: list[threading.Thread] = []

    # ── state ────────────────────────────────────────────────────────────────
    def waiting(self) -> list[str]:
        with self._lock:
            return list(self._waiting)

    def running(self) -> list[str]:
        with self._lock:
            return list(self._running)

    # ── work ─────────────────────────────────────────────────────────────────
    def submit(self, run_id: str) -> None:
        with self._lock:
            if run_id in self._waiting or run_id in self._running:
                return
            self._waiting.append(run_id)
        self._pump()

    def _pump(self) -> None:
        while True:
            with self._lock:
                if not self._waiting or len(self._running) >= self.cap:
                    return
                run_id = self._waiting.popleft()
                try:
                    proc = self.launcher(run_id)
                except Exception as e:  # noqa: BLE001 — a launch failure fails that run, not the queue
                    log.exception("could not start run %s", run_id)
                    from stockify import store, timeutil
                    store.update_run(run_id, status="failed", finished_ns=timeutil.now_ns(),
                                     error=f"{type(e).__name__}: could not start the runner: {e}",
                                     only_if_status="queued")
                    continue
                self._running[run_id] = proc
            t = threading.Thread(target=self._watch, args=(run_id, proc), name=f"test-run-{run_id}", daemon=True)
            self._watchers.append(t)
            t.start()

    def _watch(self, run_id: str, proc: Process) -> None:
        try:
            rc = proc.wait()
            # A runner writes its own final status. One that died first (a crash, a kill, a bad environment)
            # leaves the row queued or running for ever, so fail it here; a finished row is left as it is.
            try:
                from stockify import store, timeutil
                store.update_run(run_id, status="failed", finished_ns=timeutil.now_ns(),
                                 error=f"runner exited {rc}; see runner.log", only_if_status=("queued", "running"))
            except Exception:  # noqa: BLE001 — one failed update must not stop the queue
                log.exception("could not record the exit of run %s", run_id)
        finally:
            with self._lock:
                self._running.pop(run_id, None)
            self._pump()

    def cancel(self, run_id: str) -> str | None:
        """'queued' or 'running' when this call cancelled the run, None when it was neither (the caller answers
        409). A run that finishes while being killed keeps its final status."""
        from stockify import store, timeutil
        with self._lock:
            if run_id in self._waiting:
                self._waiting.remove(run_id)
                was = "queued"
                proc = None
            elif run_id in self._running:
                was = "running"
                proc = self._running[run_id]
            else:
                was = None
                proc = None
        if was == "queued":
            ok = store.update_run(run_id, status="cancelled", finished_ns=timeutil.now_ns(), only_if_status="queued")
            return "queued" if ok else None
        if was == "running" and proc is not None:
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
            # the runner may have set running just now, or still be queued in the table
            ok = store.update_run(run_id, status="cancelled", finished_ns=timeutil.now_ns(),
                                  error="cancelled by the user", only_if_status=("queued", "running"))
            return "running" if ok else None
        # not ours: a queued row from before a restart that has not been re-submitted yet
        ok = store.update_run(run_id, status="cancelled", finished_ns=timeutil.now_ns(), only_if_status="queued")
        return "queued" if ok else None

    def recover(self) -> tuple[list[str], list[str]]:
        """On API start: re-enqueue `queued` rows (oldest first) and fail `running` ones left by a dead API.
        Returns (requeued, failed)."""
        from stockify import store, timeutil
        failed = []
        for row in store.list_runs(status="running", limit=10_000):
            if store.update_run(row["run_id"], status="failed", finished_ns=timeutil.now_ns(), error=INTERRUPTED,
                                only_if_status="running"):
                failed.append(row["run_id"])
        queued = [r["run_id"] for r in reversed(store.list_runs(status="queued", limit=10_000))]
        for run_id in queued:
            self.submit(run_id)
        return queued, failed

    def join(self, timeout: float = 30.0) -> None:
        """Tests: wait until nothing is waiting or running (runs started while waiting included)."""
        import time
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                watchers = list(self._watchers)
                idle = not self._waiting and not self._running
            if idle and all(not t.is_alive() for t in watchers):
                return
            for t in watchers:
                t.join(max(0.0, min(0.2, end - time.monotonic())))


_queue: TestQueue | None = None


def get_queue() -> TestQueue:
    global _queue
    if _queue is None:
        _queue = TestQueue()
    return _queue


def set_queue(q: TestQueue | None) -> None:
    """Tests: install a queue with a fake launcher (or None to reset)."""
    global _queue
    _queue = q

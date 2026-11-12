"""A bounded pool of `python -m stockify run` subprocesses (PM 6.2), shared by sweeps, walk-forward and sensitivity.

Each job's config goes to a temp JSON in a throw-away folder under research_dir(); each child runs in its own
process (a crash or a kill takes one run, not the pool). Children's stdout and stderr go to files in that folder,
never to pipes: polling a child whose pipe is full deadlocks. The parent's environment is passed through unchanged
and is written nowhere. Outcomes come back in input order; a failed job is reported, not raised.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

_POLL_SECONDS = 0.2


def _sleep() -> None:
    time.sleep(_POLL_SECONDS)


@dataclass(frozen=True)
class PoolJob:
    config: dict
    role: str = "trial"
    job_id: str | None = None
    impl: str = "engine"


@dataclass(frozen=True)
class PoolOutcome:
    run_id: str | None
    status: str          # "done" | "failed"
    error: str | None


def default_workers(requested: int | None = None) -> int:
    """min(requested, cpu_count - 1, settings sweep.max_workers), never below 1. No request (None or 0) and a null
    setting mean no cap from that side; an unknown cpu count counts as 1."""
    from stockify import research
    limits = [max(1, (os.cpu_count() or 1) - 1)]
    if requested:
        limits.append(requested)
    cap = research.settings()["sweep"]["max_workers"]
    if cap is not None:
        limits.append(cap)
    return max(1, min(limits))


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _reported_run_id(out: str) -> str | None:
    """The run id from a child's stdout: the LAST non-empty line that is a direct child of the runs folder with a
    valid run id. Earlier lines (a library warning, a print in strategy code) are never taken for it."""
    from stockify import paths
    try:
        base = os.path.normcase(str(paths.runs_dir().resolve()))
    except OSError:
        return None
    for line in reversed([ln.strip() for ln in out.splitlines() if ln.strip()]):
        try:
            folder = Path(line)
            if os.path.normcase(str(folder.resolve().parent)) == base and paths.safe_segment(folder.name, 128):
                return folder.name
        except (OSError, ValueError):
            continue
    return None


def _kill(proc) -> None:  # noqa: ANN001
    """Kill a child and its children (a run may start stocksim_run), then reap it. No new process group: kill()
    is TerminateProcess, which needs none, and without one Ctrl-C reaches the children as well."""
    pid = getattr(proc, "pid", None)
    if os.name == "nt" and isinstance(pid, int):
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


class _Live:
    def __init__(self, index: int, proc, out, err, out_path: Path, err_path: Path) -> None:  # noqa: ANN001
        self.index, self.proc, self.out, self.err, self.out_path, self.err_path = index, proc, out, err, out_path, err_path

    def close(self) -> None:
        for f in (self.out, self.err):
            try:
                f.close()
            except OSError:
                pass


def _outcome(live: _Live, code: int) -> PoolOutcome:
    live.close()
    out = live.out_path.read_text(encoding="utf-8", errors="replace")
    err = live.err_path.read_text(encoding="utf-8", errors="replace")
    if code == 0:
        run_id = _reported_run_id(out)
        if run_id:
            return PoolOutcome(run_id, "done", None)
        return PoolOutcome(None, "failed", "the run did not report its folder")
    return PoolOutcome(None, "failed", _last_line(err) or f"exit code {code}")


def run_all(jobs: list[PoolJob], *, workers: int | None = None, popen=subprocess.Popen,  # noqa: ANN001
            tmp_dir: Path | None = None) -> list[PoolOutcome]:
    """Run every job as `python -m stockify run`, at most `workers` at once (default_workers(workers)); outcomes in
    input order. Exit 0 is done; 1 (run failed), 2 (config refused, a locked holdout included) and anything else
    (a crash, a negative code) are failed with stderr's last line as the error. KeyboardInterrupt kills every live
    child, waits for it, removes the temp files and re-raises."""
    from stockify import paths
    if not jobs:
        return []
    limit = default_workers(workers)
    own_dir = tmp_dir is None
    if own_dir:
        tmp = Path(tempfile.mkdtemp(prefix="pool-", dir=paths.research_dir()))
    else:
        tmp = Path(tmp_dir)
        tmp.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []
    results: list[PoolOutcome | None] = [None] * len(jobs)
    live: list[_Live] = []
    try:
        pending = list(range(len(jobs)))
        while pending or live:
            while pending and len(live) < limit:
                i = pending.pop(0)
                job = jobs[i]
                cfg_path, out_path, err_path = (tmp / f"{i}.{ext}" for ext in ("run.json", "out.txt", "err.txt"))
                made += [cfg_path, out_path, err_path]
                cfg_path.write_text(json.dumps(job.config), encoding="utf-8")
                cmd = [sys.executable, "-m", "stockify", "run", "--config", str(cfg_path), "--role", job.role]
                if job.job_id is not None:
                    cmd += ["--job-id", job.job_id]
                cmd += ["--impl", job.impl, "--no-report"]
                out = open(out_path, "w", encoding="utf-8")
                try:
                    err = open(err_path, "w", encoding="utf-8")
                except OSError:
                    out.close()
                    raise
                # The one key added to the parent's environment: the child writes its stdout and stderr as UTF-8, so
                # a non-ASCII lab path survives the trip through the files (which are read back as UTF-8).
                child_env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
                try:
                    proc = popen(cmd, stdout=out, stderr=err, env=child_env)
                except OSError as e:
                    out.close()
                    err.close()
                    results[i] = PoolOutcome(None, "failed", f"could not start the run: {e}")
                    continue
                live.append(_Live(i, proc, out, err, out_path, err_path))
            still = []
            for item in live:
                code = item.proc.poll()
                if code is None:
                    still.append(item)
                else:
                    results[item.index] = _outcome(item, code)
            live = still
            if live or pending:
                _sleep()
        return results  # type: ignore[return-value]
    except BaseException:
        for item in live:
            try:                           # one child that will not die must not leave the others running
                _kill(item.proc)
            except Exception:  # noqa: BLE001
                pass
            item.close()
        raise
    finally:
        for f in made:
            f.unlink(missing_ok=True)
        if own_dir:
            shutil.rmtree(tmp, ignore_errors=True)

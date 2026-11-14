"""Jobs the web app starts for the owner (D-145): a parameter sweep, a walk-forward, a sensitivity check, an NSE
download and a data-quality report.

Each runs in a thread of the API process and calls the same Python functions the command line calls (run_sweep,
run_windows + finish + render, run_suite, nse_files.backfill, quality.report): no shell, no command string. The heavy
part of a research job is the run pool's own child processes (stockify.research.pool), so the thread only waits.

Bounds: at most `web_jobs.max_research_jobs` research jobs (sweep, walk-forward and sensitivity share that limit) and
`web_jobs.max_data_jobs` of each data kind run at once (research.json); another request is refused (409), never
queued silently. A second job of the same kind and target is refused with its own reason.

A research job's row lives in lab.sqlite like one the command line makes, so it shows in the jobs list and survives a
page reload. The API records itself as the job's owner (`runner.pid` in the job's research folder, stockify.proc);
`recover()` on the next API start marks a `running` job whose owner is gone `interrupted`. Data jobs are kept in
memory only: after a restart nothing claims to be running, and a download is resumable.
"""
from __future__ import annotations

import logging
import secrets
import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

log = logging.getLogger(__name__)

RESEARCH_KINDS = ("sweep", "walkforward", "sensitivity")
DATA_KINDS = ("backfill", "report")
KINDS = RESEARCH_KINDS + DATA_KINDS
INTERRUPTED = "interrupted: the API restarted"
_ID_WAIT_S = 30.0                      # how long a request waits for a research job to get its id
_KEEP_FINISHED = 50                    # finished jobs kept in memory for the list


class PlainFailure(Exception):
    """A job that failed with a sentence already fit to show; the sentence is the message."""


class JobRefused(Exception):
    """A request the API answers with `status` and a plain `message` (422 not allowed, 404 unknown, 409 busy)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


@dataclass
class WebJob:
    ref: str
    kind: str
    target: str                         # what makes two jobs "the same": a run id, a sweep id, a date range
    status: str = "running"             # running | done | failed
    started_ns: int = 0
    finished_ns: int | None = None
    research_job_id: str | None = None
    error: str | None = None            # a plain reason; never exception text
    result: str | None = None           # a plain sentence about what it did
    done: int = 0                       # data jobs count their own steps; research jobs are counted from the runs
    total: int | None = None
    _id_ready: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def job_id(self) -> str:
        return self.research_job_id or self.ref


def _group(kind: str) -> str:
    return "research" if kind in RESEARCH_KINDS else kind


def _limit(kind: str) -> int:
    from stockify import research
    cfg = research.settings()["web_jobs"]
    return cfg["max_research_jobs"] if kind in RESEARCH_KINDS else cfg["max_data_jobs"]


class WebJobs:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, WebJob] = {}
        self._threads: list[threading.Thread] = []

    # ── reading ──────────────────────────────────────────────────────────────
    def get(self, job_id: str) -> WebJob | None:
        with self._lock:
            for j in self._jobs.values():
                if job_id in (j.ref, j.research_job_id):
                    return j
        return None

    def all(self) -> list[WebJob]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: -j.started_ns)

    def active(self, kind: str | None = None) -> list[WebJob]:
        return [j for j in self.all() if j.status == "running" and (kind is None or j.kind == kind)]

    # ── starting ─────────────────────────────────────────────────────────────
    def start(self, kind: str, target: str, work: Callable[[WebJob], None], *, total: int | None = None,
              wait_for_id: bool = False) -> WebJob:
        """Refuse (JobRefused 409) when the same job is already running or the limit for its group is reached;
        else run `work(job)` in a thread. For a research job, wait until its row exists and has an id."""
        from stockify import timeutil
        if kind not in KINDS:
            raise ValueError(f"unknown job kind {kind!r}")
        limit = _limit(kind)
        with self._lock:
            running = [j for j in self._jobs.values() if j.status == "running" and _group(j.kind) == _group(kind)]
            if any(j.kind == kind and j.target == target for j in running):
                raise JobRefused(409, _SAME[kind])
            if len(running) >= limit:
                raise JobRefused(409, _BUSY[_group(kind)])
            job = WebJob(ref=f"wj-{secrets.token_hex(5)}", kind=kind, target=target, started_ns=timeutil.now_ns(),
                         total=total)
            self._jobs[job.ref] = job
            self._trim()
        t = threading.Thread(target=self._run, args=(job, work), name=f"web-job-{job.ref}", daemon=True)
        with self._lock:
            self._threads.append(t)
        t.start()
        if wait_for_id:
            job._id_ready.wait(_ID_WAIT_S)
        return job

    def _trim(self) -> None:
        done = [j for j in self._jobs.values() if j.status != "running"]
        for j in sorted(done, key=lambda j: j.started_ns)[:-_KEEP_FINISHED]:
            self._jobs.pop(j.ref, None)

    def _run(self, job: WebJob, work: Callable[[WebJob], None]) -> None:
        from stockify import timeutil
        try:
            work(job)
            status, error = "done", None
        except Exception as e:  # noqa: BLE001 — a job's failure is its status, never a crashed thread
            log.exception("web job %s (%s) failed", job.ref, job.kind)
            status, error = "failed", reason_of(e)
        finally:
            job._id_ready.set()
        with self._lock:
            job.status, job.error, job.finished_ns = status, error, timeutil.now_ns()

    def set_research_job(self, job: WebJob, job_id: str) -> None:
        """Called by the research function once its row exists: record this process as the owner, then publish the id."""
        try:
            from stockify import paths, proc
            folder = paths.research_path(job_id)
            folder.mkdir(parents=True, exist_ok=True)
            proc.write_pidfile(folder)
        except (OSError, ValueError):
            log.exception("could not record the owner of research job %s", job_id)
        job.research_job_id = job_id
        job._id_ready.set()

    def join(self, timeout: float = 30.0) -> None:
        """Tests: wait for every job thread."""
        import time
        end = time.monotonic() + timeout
        for t in list(self._threads):
            t.join(max(0.0, end - time.monotonic()))


_SAME = {
    "sweep": "A sweep of this test is already running. Its progress is on this page.",
    "walkforward": "A walk-forward run of this sweep is already running. Its progress is on this page.",
    "sensitivity": "The cost check of this test is already running. Its progress is on this page.",
    "backfill": "A download of these days is already running. Its progress is on this page.",
    "report": "The quality report of this day is already being made.",
}
_BUSY = {
    "research": "Another sweep, walk-forward or cost check is still running. Wait for it to finish; its progress is "
                "on the Research page.",
    "backfill": "Another download from NSE is still running. Wait for it to finish.",
    "report": "Another quality report is still being made. Wait for it to finish.",
}


def reason_of(e: BaseException) -> str:
    """A plain reason for a job that failed: the fixed table of stockify.research.plain (by error class), fixed
    sentences for the data jobs, else the generic line. Exception text (a path, a value) is logged, never shown."""
    from stockify.data import nse_files, quality
    from stockify.research import walkforward
    if isinstance(e, PlainFailure):
        return str(e)
    if isinstance(e, quality.QualityError):
        return str(e)                                      # "no recording for 2026-11-04": a fixed sentence
    if isinstance(e, nse_files.NseError):
        return "NSE did not give the files (the site was unreachable or refused the request); try again later"
    return walkforward.plain_failure(e)


# ── the one manager ──────────────────────────────────────────────────────────

_manager: WebJobs | None = None


def get_jobs() -> WebJobs:
    global _manager
    if _manager is None:
        _manager = WebJobs()
    return _manager


def set_jobs(m: WebJobs | None) -> None:
    """Tests: install a fresh manager (or None to reset)."""
    global _manager
    _manager = m


# ── progress, read from the runs the job's children made ─────────────────────

_FINISHED_RUN = ("done", "failed", "cancelled")


def research_progress(job_row: dict) -> tuple[int | None, int | None]:
    """(runs finished, runs planned) of a research job, from its spec and the runs table. None when unknown."""
    import json
    from stockify import store
    try:
        spec = json.loads(job_row.get("spec_json") or "{}")
    except ValueError:
        return None, None
    kind = job_row.get("kind")
    if kind == "sweep":
        total = spec.get("trials")
    elif kind == "walkforward":
        total = spec["windows"] * spec["grid"] + spec["windows"] if "windows" in spec and "grid" in spec else None
    elif kind == "sensitivity":
        total = len(spec["variants"]) if isinstance(spec.get("variants"), list) else None
    else:
        return None, None
    counts = store.run_status_counts(job_row["job_id"])
    done = sum(n for s, n in counts.items() if s in _FINISHED_RUN)
    if isinstance(total, int) and not isinstance(total, bool):
        if job_row.get("status") == "done":
            done = total
        return min(done, total), total
    return done, None


def recover() -> list[str]:
    """On API start: a research job still `running` whose owner process (the API that started it) is gone is marked
    `interrupted`. A job with no owner file was started from the command line, and is left alone."""
    from stockify import paths, proc, store, timeutil
    marked = []
    for row in store.list_research_jobs(None, 0, 1000):
        if row.get("status") != "running":
            continue
        try:
            who = proc.read_pidfile(paths.research_path(row["job_id"]))
        except ValueError:
            who = None
        if who is None or proc.is_alive(*who):
            continue
        if store.update_research_job(row["job_id"], status="interrupted", finished_ns=timeutil.now_ns(),
                                     error=INTERRUPTED):
            marked.append(row["job_id"])
    return marked


# ── words ────────────────────────────────────────────────────────────────────

_OUTCOME_WORDS = {"ok": "downloaded", "skipped": "already there", "missing": "not published by NSE", "error": "failed"}


def backfill_sentence(totals: dict[str, Any]) -> str:
    """What a finished download did, from nse_files.backfill's outcome counts."""
    parts = [f"{n} {_OUTCOME_WORDS.get(k, k)}" for k, n in sorted(totals.items(), key=lambda kv: kv[0]) if n]
    return ("Finished: " + ", ".join(parts) + ".") if parts else "Finished: there were no trading days to fetch."


def count_noun(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def progress_words(kind: str, status: str, done: int | None, total: int | None, result: str | None,
                   error: str | None) -> str:
    """One plain sentence for the page: what the job is doing, did, or why it stopped."""
    if status == "failed":
        return f"Failed: {error}" if error else "Failed."
    if status == "interrupted":
        return "Stopped before it finished, because STOCKIFY was closed. Start it again."
    if status == "done":
        return result or {"sweep": "Finished.", "walkforward": "Finished.", "sensitivity": "Finished.",
                          "backfill": "Finished.", "report": "The quality report is ready."}[kind]
    if total is not None and done is not None:
        unit = {"sweep": ("try", "tries"), "walkforward": ("run", "runs"), "sensitivity": ("check", "checks"),
                "backfill": ("file", "files")}.get(kind)
        if unit and total > 0:
            verb = "Downloading" if kind == "backfill" else "Running"
            return f"{verb}: {done} of {total} {unit[1]} finished…"
    return {"report": "Making the report…"}.get(kind, "Running…")


def today_ist() -> date:
    from stockify import timeutil
    return timeutil.ist_today()

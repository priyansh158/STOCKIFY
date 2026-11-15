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
DATA_KINDS = ("backfill", "report", "geo")
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
    status: str = "running"             # running | done | failed | cancelled
    started_ns: int = 0
    finished_ns: int | None = None
    research_job_id: str | None = None
    error: str | None = None            # a plain reason; never exception text
    result: str | None = None           # a plain sentence about what it did
    done: int = 0                       # data jobs count their own steps; research jobs are counted from the runs
    total: int | None = None
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)     # the Stop button sets it
    _id_ready: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def stopping(self) -> bool:
        return self.status == "running" and self.cancel.is_set()

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
            if kind in RESEARCH_KINDS and outside_running({j.research_job_id for j in running}) + len(running) >= limit:
                raise JobRefused(409, _OUTSIDE)
            job = WebJob(ref=f"wj-{secrets.token_hex(5)}", kind=kind, target=target, started_ns=timeutil.now_ns(),
                         total=total)
            self._jobs[job.ref] = job
            self._trim()
        t = threading.Thread(target=self._run, args=(job, work), name=f"web-job-{job.ref}", daemon=True)
        with self._lock:
            self._threads = [x for x in self._threads if x.is_alive()] + [t]       # finished threads are not kept
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
        from stockify.research import pool
        token = pool.set_cancel(job.cancel)          # run_all checks it between polls and stops the live children
        try:
            work(job)
            status, error = "done", None
        except pool.Cancelled:
            status, error = "cancelled", "Stopped by you before it finished. The tries that had finished are kept."
            self._mark_cancelled(job)
        except Exception as e:  # noqa: BLE001 — a job's failure is its status, never a crashed thread
            log.exception("web job %s (%s) failed", job.ref, job.kind)
            status, error = "failed", reason_of(e)
        finally:
            pool.reset_cancel(token)
        # The status is written BEFORE the id is published, so a request that wakes on the id never reads a stale
        # "running" for a job that already ended (a start that failed answers 422, not 202).
        with self._lock:
            job.status, job.error, job.finished_ns = status, error, timeutil.now_ns()
        job._id_ready.set()

    @staticmethod
    def _mark_cancelled(job: WebJob) -> None:
        """The research function marked its row failed on the way out; the owner stopped it, so say that."""
        if job.research_job_id is None:
            return
        try:
            from stockify import store, timeutil
            store.update_research_job(job.research_job_id, status="cancelled", finished_ns=timeutil.now_ns(),
                                      error="cancelled by the user")
        except Exception:  # noqa: BLE001
            log.exception("could not mark research job %s cancelled", job.research_job_id)

    def cancel(self, job_id: str) -> WebJob:
        """Ask a running job to stop (it ends within a moment; live children are killed, finished tries are kept)."""
        job = self.get(job_id)
        if job is None:
            raise JobRefused(404, "This job is not running in this app.")
        if job.kind == "report":
            raise JobRefused(409, "A quality report takes a few seconds and cannot be stopped.")
        if job.kind == "geo":
            raise JobRefused(409, "A news check takes under a minute and cannot be stopped.")
        if job.status != "running":
            raise JobRefused(409, "This job has already ended.")
        job.cancel.set()
        return job

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
    "geo": "A news check is already running. Its progress is on this page.",
}
_OUTSIDE = ("A sweep, walk-forward or cost check started outside the app (from a terminal) is still running. Wait for "
            "it to finish, or stop it where it was started.")
_BUSY = {
    "research": "Another sweep, walk-forward or cost check is still running. Wait for it to finish; its progress is "
                "on the Research page.",
    "backfill": "Another download from NSE is still running. Wait for it to finish.",
    "report": "Another quality report is still being made. Wait for it to finish.",
    "geo": "A news check is already running. Wait for it to finish.",
}


def outside_running(own_ids: set[str | None]) -> int:
    """Research jobs the store says are running that this app did not start (the command line's). A row left
    `running` by a crashed terminal would block for ever, so only a job created within `pool.max_run_hours` counts:
    no run of the pool lives longer, and a job older than that with no news is taken as dead (D-145)."""
    from stockify import research, store, timeutil
    horizon = timeutil.now_ns() - int(research.settings()["pool"]["max_run_hours"]) * 3600 * 10**9
    return sum(1 for r in store.list_research_jobs(None, 0, 200)
               if r.get("status") == "running" and r.get("kind") in RESEARCH_KINDS and r["job_id"] not in own_ids
               and (r.get("created_ns") or 0) >= horizon)


def reason_of(e: BaseException) -> str:
    """A plain reason for a job that failed: the fixed table of stockify.research.plain (by error class), fixed
    sentences for the data jobs, else the generic line. Exception text (a path, a value) is logged, never shown."""
    from stockify.data import nse_files, quality
    from stockify.research import walkforward
    if isinstance(e, PlainFailure):
        return str(e)
    if isinstance(e, quality.QualityError):
        return str(e)                                      # "no recording for 2026-11-04": a fixed sentence
    if isinstance(e, nse_files.UnverifiedSource):
        return "STOCKIFY does not read NSE's files for these days yet (their format is not verified); choose later days"
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
                   error: str | None, stopping: bool = False) -> str:
    """One plain sentence for the page: what the job is doing, did, or why it stopped."""
    if status == "failed":
        return f"Failed: {error}" if error else "Failed."
    if status == "cancelled":
        return error or "Stopped by you before it finished."
    if status == "running" and stopping:
        return "Stopping…"
    if status == "interrupted":
        return "Stopped before it finished, because STOCKIFY was closed. Start it again."
    if status == "done":
        return result or {"sweep": "Finished.", "walkforward": "Finished.", "sensitivity": "Finished.",
                          "backfill": "Finished.", "report": "The quality report is ready.",
                          "geo": "The news check is finished."}[kind]
    if total is not None and done is not None:
        unit = {"sweep": ("try", "tries"), "walkforward": ("run", "runs"), "sensitivity": ("check", "checks"),
                "backfill": ("file", "files")}.get(kind)
        if unit and total > 0:
            verb = "Downloading" if kind == "backfill" else "Running"
            return f"{verb}: {done} of {total} {unit[1]} finished…"
    return {"report": "Making the report…", "geo": "Checking the news…"}.get(kind, "Running…")


def today_ist() -> date:
    from stockify import timeutil
    return timeutil.ist_today()


# ── the news check (D-150) ───────────────────────────────────────────────────

_NO_KEY = "no Gemini API key"


def geo_failure(stats: dict[str, Any]) -> str | None:
    """A plain reason when a cycle read no news at all (it could not even start), else None."""
    if not stats.get("fetched") and stats.get("errors"):
        return ("The news check could not read Google News (the network may be down, or Google refused the "
                "request). Nothing was checked; try again in a few minutes.")
    return None


def geo_sentence(stats: dict[str, Any]) -> str:
    """What a finished news check did, in plain words, from the worker's stats."""
    fetched, alerts, suspects = int(stats.get("fetched") or 0), int(stats.get("alerts") or 0), int(stats.get("suspects") or 0)
    errors = [str(e) for e in stats.get("errors") or []]
    story = count_noun(fetched, "story", "stories")
    head = (f"{count_noun(alerts, 'new alert', 'new alerts')} ({story} checked)." if alerts
            else f"{story} checked, no new alerts.")
    notes: list[str] = []
    if alerts:
        emailed = int(stats.get("emailed") or 0)
        notes.append(f"{emailed} emailed." if emailed else "None was emailed.")
    if any(_NO_KEY in e for e in errors):
        notes.append(f"{count_noun(suspects, 'story', 'stories')} could not be assessed because no Gemini key is set in "
                     "the settings file; they will be tried again once it is.")
    elif suspects and not stats.get("classified") and errors:
        notes.append("Gemini did not assess the stories that looked important (its quota may be used up, or the "
                     "network failed); they will be tried again at the next check.")
    if any("email not configured" in e for e in errors):
        notes.append("Email is not set up in the settings file, so no email was sent.")
    elif any(e.startswith("email:") for e in errors):
        notes.append("An alert email could not be sent.")
    return " ".join([head, *notes])

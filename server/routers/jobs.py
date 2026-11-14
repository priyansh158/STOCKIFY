"""Jobs the owner starts from a page (D-145): a parameter sweep, a walk-forward, a cost check, an NSE download, a
data-quality report. Each POST validates with the validators the command line uses, starts a background job through
server.services.web_jobs and answers 202 with the job; the page polls `GET /api/jobs/{id}` for progress.

Safe by construction: nothing here takes a path, a command or a config from the client. A sweep's base is a finished
run chosen by its id; its axes may only be that strategy's own parameters (validated against the catalogue's schema);
the sweep config is built here from those validated fields. The holdout is never unlocked from the web (422 with the
reason). No shell and no command string: the jobs call Python functions. Same protection as every other POST: the
`WebOriginOnly` middleware (server/local_only.py), loopback hosts only; bodies are bounded models.
"""
from __future__ import annotations

import copy
import json
import math
import re
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException

from server.schemas import (BackfillRequestDTO, QualityReportRequestDTO, ResearchLimitsDTO, SweepRequestDTO,
                            WalkforwardRequestDTO, WebJobDTO)
from server.services import web_jobs
from server.services.web_jobs import JobRefused, WebJob

router = APIRouter(tags=["jobs"])

_PARAM_PREFIX = "strategy.params."
MIN_TRIES = 2          # a sweep compares tries; web/src/lib/sweepForm.ts `MIN_TRIES` says the same (tested)
_ISO_DAY = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_NAME_OK = re.compile(r"[^A-Za-z0-9._-]+")


def _refuse(e: JobRefused) -> HTTPException:
    return HTTPException(status_code=e.status, detail=e.message)


def _422(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=message)


def _day(text: str | None, label: str) -> date:
    if text is None or not _ISO_DAY.fullmatch(text):
        raise _422(f"{label} must be a date like 2026-11-05")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise _422(f"{label} is not a real date") from None


# ── words ────────────────────────────────────────────────────────────────────

def _holdout_reason() -> str:
    from stockify.research import holdout
    hold = holdout.holdout_from()
    since = f" (from {hold.isoformat()})" if hold else ""
    return (f"This test's days reach into the days held back for the final check{since}. A job started here never "
            "touches them. Choose a test that ends before then.")


def _locked(cfg: dict) -> None:
    """422 when the config's days reach the holdout; the web has no way to unlock it."""
    from stockify import runconfig
    from stockify.research import holdout
    try:
        holdout.enforce({**cfg, "unlock_holdout": None})
    except holdout.HoldoutLocked:
        raise _422(_holdout_reason()) from None
    except runconfig.RunConfigError as e:
        raise _422(str(e)) from None


# ── reading a job ────────────────────────────────────────────────────────────

def _dto_of_web(j: WebJob) -> WebJobDTO:
    from stockify import store
    done, total, status = j.done, j.total, j.status
    stopping = j.stopping
    if j.research_job_id:
        row = store.get_research_job(j.research_job_id)
        if row is not None:
            done, total = web_jobs.research_progress(row)
            if status == "running" and row["status"] == "interrupted":
                status = "interrupted"
    if status == "done" and total is not None:
        done = total
    return WebJobDTO(job_id=j.job_id, kind=j.kind, status=status, target=j.target, done=done, total=total,
                     plain=web_jobs.progress_words(j.kind, status, done, total, j.result, j.error, stopping), error=j.error,
                     stopping=stopping)


def _dto_of_row(row: dict) -> WebJobDTO:
    """A research job this process did not start in this run (the command line, or before a restart)."""
    from stockify.research import walkforward
    kind = row["kind"]
    status = row["status"] if row["status"] in ("running", "done", "failed", "interrupted", "cancelled") else "failed"
    done, total = web_jobs.research_progress(row)
    error = walkforward.plain_error(row) if status == "failed" else None
    try:
        target = (json.loads(row.get("spec_json") or "{}").get("run_id") if kind == "sensitivity" else None) or row["job_id"]
    except ValueError:
        target = row["job_id"]
    return WebJobDTO(job_id=row["job_id"], kind=kind, status=status, target=str(target), done=done, total=total,
                     plain=web_jobs.progress_words(kind, status, done, total, None, error), error=error)


@router.get("/api/jobs", response_model=list[WebJobDTO])
def list_jobs() -> list[WebJobDTO]:
    """The jobs started from the web since the API started, newest first (running ones first), so a reloaded page
    can find its job again."""
    jobs = web_jobs.get_jobs().all()
    jobs.sort(key=lambda j: (j.status != "running", -j.started_ns))
    return [_dto_of_web(j) for j in jobs]


@router.get("/api/jobs/{job_id}", response_model=WebJobDTO)
def get_job(job_id: str) -> WebJobDTO:
    from stockify import store
    job = web_jobs.get_jobs().get(job_id)
    if job is not None:
        return _dto_of_web(job)
    row = store.get_research_job(job_id) if 0 < len(job_id) <= 64 else None
    if row is not None and row.get("kind") in web_jobs.RESEARCH_KINDS:
        return _dto_of_row(row)
    raise HTTPException(status_code=404, detail="no such job")


@router.post("/api/jobs/{job_id}/cancel", response_model=WebJobDTO)
@router.post("/api/research/jobs/{job_id}/cancel", response_model=WebJobDTO)
def cancel_job(job_id: str) -> WebJobDTO:
    """Stop a job started from the app: its live children are killed and no new try starts; the tries that had
    finished are kept and the job ends `cancelled` (D-145). 404 when this app is not running it, 409 when it ended,
    is a report (a few seconds) or was started outside the app (stop it where it was started)."""
    from stockify import store
    try:
        job = web_jobs.get_jobs().cancel(job_id)
    except JobRefused as e:
        if e.status == 404 and 0 < len(job_id) <= 64:
            row = store.get_research_job(job_id)
            if row is not None and row.get("status") == "running":
                raise HTTPException(status_code=409, detail="This job was started outside the app; stop it where it was started.") from None
        raise _refuse(e) from None
    return _dto_of_web(job)


@router.get("/api/research/limits", response_model=ResearchLimitsDTO)
def research_limits() -> ResearchLimitsDTO:
    from stockify import research
    from stockify.research import holdout, sweep
    wf = research.settings()["walk_forward"]
    hold = holdout.holdout_from()
    return ResearchLimitsDTO(max_trials=sweep.max_trials(), confirm_above=sweep.CONFIRM_ABOVE,
                             train_days=wf["train_days"], test_days=wf["test_days"], step_days=wf["step_days"],
                             wf_objective=wf["objective"], holdout_from=hold.isoformat() if hold else None)


# ── a sweep ──────────────────────────────────────────────────────────────────

def _finished_run(run_id: str) -> dict:
    from stockify import store
    if not 0 < len(run_id) <= 128:
        raise HTTPException(status_code=404, detail="no such test")
    row = store.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such test")
    if row.get("status") != "done":
        raise _422(f"This test is {row.get('status')}, not finished. Choose a finished test.")
    return row


def _run_config(row: dict) -> dict:
    try:
        cfg = json.loads(row.get("config_json") or "")
    except ValueError:
        cfg = None
    if not isinstance(cfg, dict):
        raise _422("This test's settings cannot be read, so it cannot be the start of a sweep.")
    return cfg


def _axis_values(label: str, param: dict, values: list[Any]) -> list[Any]:
    """Each value, checked the way the strategy would read it (the catalogue's own check); decimals stay text."""
    from stockify.strategies import _catalogue
    out: list[Any] = []
    for v in values:
        if isinstance(v, float):
            if not math.isfinite(v):
                raise _422(f"{label}: {v!r} is not a number")
            v = str(v)                                   # a decimal is carried as text (the engine converts it exactly)
        if v is not None and not isinstance(v, (int, str, bool)):
            raise _422(f"{label}: each value must be a number, text or true/false")
        problems = _catalogue.validate_params([param], {param["name"]: v})
        if problems:
            raise _422(f"{label}: {v!r} {problems[0].split(': ', 1)[-1]}")
        out.append(v)
    if len({json.dumps(v, sort_keys=True) for v in out}) != len(out):
        raise _422(f"{label}: the same value is listed twice")
    return out


def _sweep_name(requested: str | None, base_name: str | None, run_id: str) -> str:
    from stockify import paths
    from stockify.research import sweep
    if requested is not None and requested.strip():
        name = requested.strip()
        if not paths.safe_segment(name, sweep.NAME_MAX):
            raise _422(f"The name may use letters, digits, dots, dashes and underscores only (up to {sweep.NAME_MAX} "
                       "characters, no spaces).")
        return name
    stem = _NAME_OK.sub("-", f"{base_name or run_id}-sweep").strip("-.")[: sweep.NAME_MAX].strip("-.")
    return stem if paths.safe_segment(stem, sweep.NAME_MAX) else "sweep"


@router.post("/api/research/sweeps", response_model=WebJobDTO, status_code=202)
def start_sweep(req: SweepRequestDTO) -> WebJobDTO:
    from stockify import runconfig
    from stockify.research import plain, registry, sweep
    from stockify.strategies import _catalogue
    row = _finished_run(req.base_run_id)
    base = _run_config(row)
    base.pop("unlock_holdout", None)
    base.pop("family_id", None)
    strategy = base.get("strategy") if isinstance(base.get("strategy"), dict) else {}
    schema = _catalogue.schema_for(strategy.get("kind"), strategy.get("ref"))
    if schema is None:
        raise _422("This test's strategy is not one of the built-in strategies, so its settings cannot be swept here.")
    by_name = {p["name"]: p for p in schema}
    axes: dict[str, list] = {}
    for path, values in req.axes.items():
        name = path[len(_PARAM_PREFIX):] if path.startswith(_PARAM_PREFIX) else None
        if name not in by_name:
            raise _422("Only the strategy's own settings can be swept.")
        param = by_name[name]
        axes[path] = _axis_values(param.get("label") or name, param, values)
    tries = math.prod(len(v) for v in axes.values())
    cap = sweep.max_trials()
    if tries > cap:
        raise _422(f"That is {tries:,} tries, and the most one sweep may have is {cap:,}. Offer fewer values.")
    if tries < MIN_TRIES:
        raise _422(f"That is a single try. List at least {MIN_TRIES} values for one setting, so there is something to compare.")
    if tries > sweep.CONFIRM_ABOVE and not req.confirm:
        raise _422(f"That is {tries:,} tries, which will take a while. Tick the box to confirm that you want to run them.")
    _locked(base)
    name = _sweep_name(req.name, row.get("name"), req.base_run_id)
    try:
        spec = sweep.build_spec(name, base, axes, objective=req.objective)
    except (sweep.SweepSpecError, runconfig.RunConfigError, ValueError) as e:
        raise _422(plain.plain_reason(f"{type(e).__name__}: {e}") or "The sweep's settings were not accepted.") from None
    jobs = web_jobs.get_jobs()

    def work(job: WebJob) -> None:
        sweep.run_sweep(spec, on_job=lambda job_id: jobs.set_research_job(job, job_id))

    try:
        job = jobs.start("sweep", req.base_run_id, work, total=tries, wait_for_id=True)
    except JobRefused as e:
        raise _refuse(e) from None
    return _after_start(job)


def _after_start(job: WebJob) -> WebJobDTO:
    """202 for a job that is running or done; a job that failed before it had an id says why (422)."""
    if job.status == "failed" and job.research_job_id is None:
        raise _422(job.error or "The job could not start.")
    return _dto_of_web(job)


# ── a walk-forward ───────────────────────────────────────────────────────────

def _stored_spec(sweep_job_id: str):  # noqa: ANN202
    from stockify import store
    from stockify.research import sweep
    if not 0 < len(sweep_job_id) <= 64:
        raise HTTPException(status_code=404, detail="no such sweep")
    row = store.get_research_job(sweep_job_id)
    if row is None or row.get("kind") != "sweep":
        raise HTTPException(status_code=404, detail="no such sweep")
    if row.get("status") != "done":
        raise _422("This sweep has not finished. Walk-forward needs a finished sweep.")
    try:
        d = json.loads(row.get("spec_json") or "")
        return sweep.build_spec(d["name"], copy.deepcopy(d["base"]), d["axes"], objective=d["objective"],
                                search=d.get("search"), workers=d.get("workers"))
    except (ValueError, KeyError, TypeError, sweep.SweepSpecError):
        raise _422("This sweep's stored settings cannot be read, so it cannot be walked forward.") from None


@router.post("/api/research/walkforwards", response_model=WebJobDTO, status_code=202)
def start_walkforward(req: WalkforwardRequestDTO) -> WebJobDTO:
    from stockify import research, runconfig
    from stockify.research import pool, sweep, walkforward, wf_report
    spec = _stored_spec(req.sweep_job_id)
    cfg = research.settings()["walk_forward"]
    train = req.train_days or cfg["train_days"]
    test = req.test_days or cfg["test_days"]
    step = req.step_days or cfg["step_days"]
    objective = req.objective or cfg["objective"]
    start = _day(req.first_day, "The first day") if req.first_day else None
    end = _day(req.last_day, "The last day") if req.last_day else None
    if step < test:
        raise _422(f"The step ({step} days) is shorter than the test period ({test} days), so the test periods "
                   "would overlap. Make the step at least as long as the test period.")
    try:
        wins, grid, first, last = walkforward.plan(spec, train=train, test=test, step=step, objective=objective,
                                                   start=start, end=end)
    except (walkforward.WalkforwardError, ValueError) as e:
        raise _422(_wf_words(e)) from None
    try:
        _locked(walkforward.window_config(spec.base, first, last))
    except runconfig.RunConfigError as e:
        raise _422(str(e)) from None
    jobs = web_jobs.get_jobs()

    def work(job: WebJob) -> None:
        res = walkforward.run_windows(spec, train=train, test=test, step=step, objective=objective, start=first,
                                      end=last, pool_run=pool.run_all,
                                      on_job=lambda job_id: jobs.set_research_job(job, job_id))
        walkforward.finish(res.job_id)                   # the only writer of `done` for a walk-forward job
        wf_report.render(res.job_id)

    try:
        job = jobs.start("walkforward", req.sweep_job_id, work, total=len(wins) * grid + len(wins), wait_for_id=True)
    except JobRefused as e:
        raise _refuse(e) from None
    return _after_start(job)


def _wf_words(e: Exception) -> str:
    """A plain sentence for a refused walk-forward plan (the messages name settings, not paths)."""
    text = str(e)
    if text.startswith("no window fits"):
        return ("No window fits: the training and test periods together are longer than the sweep's days. Shorten "
                "them, or give a longer span.")
    if "sweep.max_trials" in text:
        return ("That would be more training runs than one job may make. Shorten the span, lengthen the windows or "
                "use a smaller sweep.")
    if "needs pack specs" in text or "no end date" in text:
        return "The sweep's days cannot be worked out. Give a first and last day."
    return "The windows could not be worked out from these settings."


# ── a cost check ─────────────────────────────────────────────────────────────

@router.post("/api/tests/{run_id}/sensitivity", response_model=WebJobDTO, status_code=202)
def start_sensitivity(run_id: str) -> WebJobDTO:
    from stockify.research import sensitivity
    row = _finished_run(run_id)
    _locked(_run_config(row))
    jobs = web_jobs.get_jobs()

    def work(job: WebJob) -> None:
        sensitivity.run_suite(run_id, on_job=lambda job_id: jobs.set_research_job(job, job_id))

    try:
        job = jobs.start("sensitivity", run_id, work, wait_for_id=True)
    except JobRefused as e:
        raise _refuse(e) from None
    return _after_start(job)


# ── data: download and report ────────────────────────────────────────────────

BACKFILL_DATASETS = ("fo", "cm", "idx")           # the command's own default (`nse backfill --datasets`)


@router.post("/api/data/backfill", response_model=WebJobDTO, status_code=202)
def start_backfill(req: BackfillRequestDTO) -> WebJobDTO:
    from stockify import research
    from stockify.data import calendar, nse_files
    from stockify.research import pool
    first, last = _day(req.first_day, "The first day"), _day(req.last_day, "The last day")
    if first > last:
        raise _422("The first day is after the last day.")
    if last > web_jobs.today_ist():
        raise _422("NSE has not published files for days that have not happened yet. Choose a last day up to today.")
    max_days = int(research.settings()["coverage"]["max_days"])
    if (last - first).days + 1 > max_days:
        raise _422(f"At most {max_days} days at a time.")
    days = calendar.trading_days("FO", first, last)
    total = len(days) * len(BACKFILL_DATASETS)
    jobs = web_jobs.get_jobs()

    def work(job: WebJob) -> None:
        def step(_res) -> None:  # noqa: ANN001
            job.done += 1
            if job.cancel.is_set():
                raise pool.Cancelled("stopped by the owner")
        totals = nse_files.backfill(first, last, BACKFILL_DATASETS, progress=step)
        sentence = web_jobs.backfill_sentence(totals)
        if totals.get("error"):
            raise web_jobs.PlainFailure(f"{sentence} Run it again to retry the failed ones; days already fetched are skipped.")
        job.result = sentence

    try:
        job = jobs.start("backfill", f"{first.isoformat()}..{last.isoformat()}", work, total=total)
    except JobRefused as e:
        raise _refuse(e) from None
    return _dto_of_web(job)


@router.post("/api/data/report", response_model=WebJobDTO, status_code=202)
def start_quality_report(req: QualityReportRequestDTO) -> WebJobDTO:
    from stockify.data import quality
    day = _day(req.day, "The day")
    jobs = web_jobs.get_jobs()

    def work(job: WebJob) -> None:
        quality.report(day)
        job.result = f"The quality report for {day.isoformat()} is ready."

    try:
        job = jobs.start("report", day.isoformat(), work)
    except JobRefused as e:
        raise _refuse(e) from None
    return _dto_of_web(job)

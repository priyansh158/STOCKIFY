"""Geo disruption alerts from the always-on monitor.

GET  /api/alerts
GET  /api/alerts/{id}
POST /api/alerts/{id}/ack
GET  /api/alerts/monitor   when the last check ran, and whether one is running now
POST /api/alerts/check     run one monitor cycle now (202 + job; progress at GET /api/jobs/{id})
"""
from __future__ import annotations

import logging
import traceback

from fastapi import APIRouter, HTTPException, Query

import geo_alerts
from server.schemas import GeoAlertDTO, GeoMonitorStatusDTO, WebJobDTO
from server.services import web_jobs
from server.services.web_jobs import JobRefused, WebJob

log = logging.getLogger("stockify.alerts")

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


def _to_dto(a: geo_alerts.GeoAlert) -> GeoAlertDTO:
    return GeoAlertDTO(
        id=a.id,
        created_at=a.created_at,
        severity=a.severity,
        disruption_score=a.disruption_score,
        scope=a.scope or None,
        title=a.title,
        source=a.source or None,
        url=a.url or None,
        published_at=a.published_at or None,
        guid=a.guid or None,
        why_disruptive=a.why_disruptive or None,
        investor_action=a.investor_action or None,
        confidence=a.confidence,
        affected_tickers=a.affected_tickers or [],
        emailed_at=a.emailed_at,
        acknowledged_at=a.acknowledged_at,
    )


@router.get("", response_model=list[GeoAlertDTO])
async def list_alerts(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    min_severity: str | None = Query(
        None, description="LOW|MEDIUM|HIGH|CRITICAL — inclusive floor"
    ),
) -> list[GeoAlertDTO]:
    geo_alerts.init_db()
    rows = geo_alerts.list_alerts(
        limit=limit, offset=offset, min_severity=min_severity
    )
    return [_to_dto(r) for r in rows]


@router.get("/monitor", response_model=GeoMonitorStatusDTO)
async def monitor_status() -> GeoMonitorStatusDTO:
    """Read from the store and the cycle lock only: whether the scheduled task is installed is not looked up here
    (that needs a PowerShell call), so the page says only what is known: the last finished check, and whether one
    is running."""
    from geo_monitor import cycle_guard
    geo_alerts.init_db()
    row = geo_alerts.last_cycle()
    plain = None
    if row:
        alerts = int(row["alerts"])
        plain = (f"{web_jobs.count_noun(int(row['fetched']), 'story', 'stories')} checked, "
                 + (web_jobs.count_noun(alerts, "new alert", "new alerts") if alerts else "no new alerts") + ".")
    return GeoMonitorStatusDTO(
        last_check_at=row["finished_at"] if row else None,
        last_by=row["source"] if row and row["source"] in ("background", "page") else None,
        last_plain=plain,
        checking=cycle_guard.is_busy(),
    )


@router.post("/check", response_model=WebJobDTO, status_code=202)
def start_check() -> WebJobDTO:
    """Run ONE real monitor cycle, exactly as the scheduled monitor does (it may store and email alerts), in a
    background job. 409 when a check is already running here or the scheduled monitor is in the middle of one.
    Nothing is taken from the client: no body, no query."""
    from geo_monitor import cycle_guard, worker
    from server.routers.jobs import _dto_of_web
    if cycle_guard.is_busy():
        raise HTTPException(status_code=409, detail=_BUSY_ELSEWHERE)

    def work(job: WebJob) -> None:
        try:
            stats = worker.run_cycle(source="page")
        except cycle_guard.CycleBusy:
            raise web_jobs.PlainFailure(_BUSY_ELSEWHERE) from None
        except Exception:  # noqa: BLE001 — the job's failure is a plain sentence; the trace goes to the log, scrubbed
            log.error("news check failed: %s", worker.scrub(traceback.format_exc()).rstrip())
            raise web_jobs.PlainFailure("The news check stopped unexpectedly. Try again; the details are in the "
                                        "app's log.") from None
        reason = web_jobs.geo_failure(stats)
        if reason:
            raise web_jobs.PlainFailure(reason)
        job.result = web_jobs.geo_sentence(stats)

    try:
        job = web_jobs.get_jobs().start("geo", "news", work)
    except JobRefused as e:
        raise HTTPException(status_code=e.status, detail=e.message) from None
    return _dto_of_web(job)


_BUSY_ELSEWHERE = ("The background news monitor is checking right now. Wait a minute, then check again; "
                   "its result will show here.")


@router.get("/{alert_id}", response_model=GeoAlertDTO)
async def get_alert(alert_id: int) -> GeoAlertDTO:
    geo_alerts.init_db()
    row = geo_alerts.get_alert(alert_id)
    if not row:
        raise HTTPException(status_code=404, detail="Alert not found")
    return _to_dto(row)


@router.post("/{alert_id}/ack", response_model=GeoAlertDTO)
async def ack_alert(alert_id: int) -> GeoAlertDTO:
    geo_alerts.init_db()
    if not geo_alerts.acknowledge(alert_id):
        raise HTTPException(status_code=404, detail="Alert not found")
    row = geo_alerts.get_alert(alert_id)
    if not row:
        raise HTTPException(status_code=404, detail="Alert not found")
    return _to_dto(row)

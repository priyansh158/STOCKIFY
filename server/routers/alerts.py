"""Geo disruption alerts from the always-on monitor.

GET  /api/alerts
GET  /api/alerts/{id}
POST /api/alerts/{id}/ack
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

import geo_alerts
from server.schemas import GeoAlertDTO

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

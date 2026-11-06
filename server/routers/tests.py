"""Strategy tests (M3.14, A2 §6): the catalogue, launching runs, and reading their results.

Runs execute as subprocesses through server.services.test_queue (I-9); this router only validates, queues and
reads lab.sqlite and the run folders. Money is integer paise; times are ns plus an ISO IST string.
Fixed paths (`/api/tests/compare`, …) are declared before `/api/tests/{run_id}` so they are never taken as ids.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from server.schemas import (StrategyDTO, TestChargeDTO, TestCreatedDTO, TestEquityDTO, TestFillDTO, TestLogsDTO,
                            TestRunDetailDTO, TestRunDTO)

router = APIRouter(tags=["tests"])

STATUSES = ("queued", "running", "done", "failed", "cancelled")
CHARGE_PARTS = ("brokerage", "stt", "exchange", "sebi", "stamp", "gst", "dp", "other")


def _ist(ns: int | None) -> str | None:
    if ns is None:
        return None
    from stockify import timeutil
    return timeutil.ns_to_ist(ns).isoformat()


def _json(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def _dates(cfg: dict) -> tuple[str | None, str | None]:
    specs = [p["spec"] for p in cfg.get("data", {}).get("packs", []) if isinstance(p, dict) and "spec" in p]
    froms = [s["from"] for s in specs if s.get("from")]
    tos = [s["to"] for s in specs if s.get("to")]
    return (min(froms) if froms else None, max(tos) if tos else None)


def run_dto(row: dict) -> TestRunDTO:
    cfg = _json(row.get("config_json"), {})
    metrics = _json(row.get("metrics_json"), {})
    start, end = _dates(cfg)
    strategy = cfg.get("strategy") or {}
    return TestRunDTO(
        run_id=row["run_id"], name=row.get("name"), family_id=row.get("family_id"), status=row["status"],
        impl=row.get("impl"), strategy_ref=strategy.get("ref"), start_date=start, end_date=end,
        price_basis=cfg.get("price_basis"), capital_paise=cfg.get("capital_paise"),
        created_ns=row.get("created_ns"), created_ist=_ist(row.get("created_ns")),
        finished_ns=row.get("finished_ns"), finished_ist=_ist(row.get("finished_ns")),
        nav_start_paise=metrics.get("nav_start_paise"), nav_end_paise=metrics.get("nav_end_paise"),
        max_drawdown=metrics.get("max_drawdown"), trades=metrics.get("trades"),
        costs_total_paise=metrics.get("costs_total_paise"), labels=_json(row.get("labels_json"), []),
        error=row.get("error"),
    )


def _row_or_404(run_id: str) -> dict:
    from stockify import store
    row = store.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no test run {run_id!r}")
    return row


def _folder(row: dict) -> Path | None:
    from stockify import paths
    if row.get("folder"):
        return Path(row["folder"])
    p = paths.runs_dir() / row["run_id"]
    return p if p.exists() else None


def _tail(path: Path | None, *, offset: int | None, limit: int) -> tuple[list[str], int]:
    if path is None or not path.exists():
        return [], 0
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if offset is None:
        return lines[-limit:] if limit else [], len(lines)
    return lines[offset:offset + limit], len(lines)


# ── catalogue ────────────────────────────────────────────────────────────────

@router.get("/api/strategies", response_model=list[StrategyDTO])
async def strategies() -> list[StrategyDTO]:
    from stockify.strategies import _catalogue
    return [StrategyDTO(**s) for s in _catalogue.catalogue()]


# ── fixed /api/tests paths (before /{run_id}) ────────────────────────────────

@router.get("/api/tests", response_model=list[TestRunDTO])
async def list_tests(status: str | None = Query(None), family: str | None = Query(None),
                     offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=500)) -> list[TestRunDTO]:
    from stockify import store
    if status is not None and status not in STATUSES:
        raise HTTPException(status_code=422, detail=[f"status must be one of {', '.join(STATUSES)}"])
    return [run_dto(r) for r in store.list_runs(status=status, family_id=family, offset=offset, limit=limit)]


@router.post("/api/tests", response_model=TestCreatedDTO, status_code=201)
async def create_test(config: dict = Body(...)) -> TestCreatedDTO:
    from stockify import runconfig, runs
    from stockify.strategies import _catalogue
    from server.services.test_queue import get_queue
    try:
        cfg = runconfig.load(config)
    except runconfig.RunConfigError as e:
        raise HTTPException(status_code=422, detail=[str(e)])
    s = cfg["strategy"]
    if s["kind"] not in ("python", "cpp"):
        raise HTTPException(status_code=422, detail=[f"strategy.kind: {s['kind']!r} cannot be started from the API"])
    schema = _catalogue.schema_for(s["kind"], s["ref"])
    if schema is None:
        raise HTTPException(status_code=422, detail=[f"strategy.ref: {s['ref']!r} is not in the catalogue"])
    problems = _catalogue.validate_params(schema, s.get("params", {}))
    if problems:
        raise HTTPException(status_code=422, detail=[f"strategy.params.{p}" for p in problems])
    run_id = runs.queue(config)
    get_queue().submit(run_id)
    return TestCreatedDTO(run_id=run_id)


@router.get("/api/tests/compare")
async def compare_tests() -> None:
    """Reserved for M6 (A2 §6): declared here so `compare` is never read as a run id."""
    raise HTTPException(status_code=501, detail="comparing tests arrives in M6")


# ── one run ──────────────────────────────────────────────────────────────────

@router.get("/api/tests/{run_id}", response_model=TestRunDetailDTO)
async def get_test(run_id: str) -> TestRunDetailDTO:
    row = _row_or_404(run_id)
    folder = _folder(row)
    manifest = None
    if folder is not None and (folder / "manifest.json").exists():
        manifest = _json((folder / "manifest.json").read_text(encoding="utf-8"), None)
    return TestRunDetailDTO(run=run_dto(row), config=_json(row.get("config_json"), {}),
                            metrics=_json(row.get("metrics_json"), {}), manifest=manifest)


@router.get("/api/tests/{run_id}/orders", response_model=list[dict[str, Any]])
async def test_orders(run_id: str, offset: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=5000)) -> list[dict]:
    from stockify import store
    _row_or_404(run_id)
    return store.run_rows(run_id, "orders", offset=offset, limit=limit)


@router.get("/api/tests/{run_id}/fills", response_model=list[TestFillDTO])
async def test_fills(run_id: str, offset: int = Query(0, ge=0),
                     limit: int = Query(500, ge=1, le=5000)) -> list[TestFillDTO]:
    from stockify import store
    _row_or_404(run_id)
    return [TestFillDTO(fill_id=r["fill_id"], order_id=r["order_id"], t_ns=r["t_ns"], t_ist=_ist(r["t_ns"]) or "",
                        instrument_id=r["instrument_id"], side=r["side"], qty=r["qty"], price_paise=r["price_paise"])
            for r in store.run_rows(run_id, "fills", offset=offset, limit=limit)]


@router.get("/api/tests/{run_id}/equity", response_model=list[TestEquityDTO])
async def test_equity(run_id: str, offset: int = Query(0, ge=0),
                      limit: int = Query(5000, ge=1, le=20000)) -> list[TestEquityDTO]:
    from stockify import store
    _row_or_404(run_id)
    return [TestEquityDTO(trade_date=r["trade_date"], nav_paise=r["nav_paise"], cash_paise=r["cash_paise"],
                          margin_paise=r.get("margin_paise"))
            for r in store.run_rows(run_id, "equity", offset=offset, limit=limit)]


@router.get("/api/tests/{run_id}/charges", response_model=list[TestChargeDTO])
async def test_charges(run_id: str, offset: int = Query(0, ge=0),
                       limit: int = Query(5000, ge=1, le=20000)) -> list[TestChargeDTO]:
    from stockify import store
    _row_or_404(run_id)
    out = []
    for r in store.run_rows(run_id, "charges", offset=offset, limit=limit):
        parts = _json(r.get("components_json"), {})
        out.append(TestChargeDTO(row_no=r["row_no"], kind=r["kind"], order_id=r.get("order_id"),
                                 trade_date=r["trade_date"],
                                 components={k: int(parts[k]) for k in CHARGE_PARTS if parts.get(k) is not None},
                                 total_paise=r["total_paise"]))
    return out


@router.get("/api/tests/{run_id}/logs", response_model=TestLogsDTO)
async def test_logs(run_id: str, tail: int = Query(40, ge=0, le=5000), offset: int | None = Query(None, ge=0),
                    limit: int = Query(200, ge=1, le=5000)) -> TestLogsDTO:
    """The end of events_log.txt (or a page of it with `offset`/`limit`), plus the end of runner.log."""
    row = _row_or_404(run_id)
    folder = _folder(row)
    events, total = _tail(folder / "events_log.txt" if folder else None, offset=offset,
                          limit=limit if offset is not None else tail)
    runner, _ = _tail(folder / "runner.log" if folder else None, offset=None, limit=tail)
    return TestLogsDTO(events_log=events, runner_log=runner,
                       offset=offset if offset is not None else max(0, total - len(events)), total_lines=total)


@router.post("/api/tests/{run_id}/cancel", response_model=TestRunDTO)
async def cancel_test(run_id: str) -> TestRunDTO:
    from stockify import store
    from server.services.test_queue import get_queue
    row = _row_or_404(run_id)
    if row["status"] not in ("queued", "running") or get_queue().cancel(run_id) is None:
        latest = store.get_run(run_id) or row
        raise HTTPException(status_code=409, detail=f"test run {run_id} is {latest['status']}; only waiting or "
                                                    "running tests can be cancelled")
    return run_dto(store.get_run(run_id) or row)

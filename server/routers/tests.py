"""Strategy tests (M3.14, A2 §6): the catalogue, launching runs, and reading their results.

Runs execute as subprocesses through server.services.test_queue (I-9); this router only validates, queues and
reads lab.sqlite and the run folders. Money is integer paise; times are ns plus an ISO IST string.
Fixed paths (`/api/tests/compare`, …) are declared before `/api/tests/{run_id}` so they are never taken as ids.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Response

from server.routers.research import run_research_dto
from server.schemas import (ComparisonDTO, RunResearchDTO, StrategyDTO, TestChargeDTO, TestCreatedDTO, TestEquityDTO, TestFillDTO, TestLogsDTO,
                            TestGreekDTO, TestMarginDTO, TestRunDTO, TestRunDetailDTO, TestSettlementDTO)

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
    """The run's folder, always derived from its checked id (never the stored path, which a row written before
    the run-id check could have pointed anywhere)."""
    from stockify import paths
    try:
        p = paths.run_path(row["run_id"])
    except ValueError:
        return None
    return p if p.exists() else None


def _folder_totals(folder: Path | None) -> tuple[int | None, dict[str, int]]:
    """The whole run's fill count and paise per charge component, from the run folder's CSVs (the /fills and
    /charges lists are paged, so the UI must not count or add them up itself)."""
    import csv
    fills = folder / "fills.csv" if folder else None
    charges = folder / "charges.csv" if folder else None
    count = None
    if fills is not None and fills.exists():
        with fills.open(encoding="utf-8", newline="") as f:
            count = sum(1 for _ in csv.DictReader(f))
    totals: dict[str, int] = {}
    if charges is not None and charges.exists():
        with charges.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                for k in CHARGE_PARTS:
                    if r.get(k):
                        totals[k] = totals.get(k, 0) + int(r[k])
    return count, {k: totals[k] for k in CHARGE_PARTS if k in totals}


def _symbols(row: dict) -> dict[int, str]:
    """instrument id → tradingsymbol, from the run's archived bundle.json (empty when it is not there)."""
    folder = _folder(row)
    bundle = folder / "bundle.json" if folder else None
    if bundle is None or not bundle.exists():
        return {}
    rows = _json(bundle.read_text(encoding="utf-8"), {}).get("instruments", [])
    return {r["id"]: r["tradingsymbol"] for r in rows if isinstance(r, dict) and "id" in r and r.get("tradingsymbol")}


_CHUNK = 1 << 20


def _count_lines(path: Path) -> int:
    """Lines as str.splitlines() counts them for a "\\n"-separated file, streaming (never the whole file)."""
    n, last = 0, b""
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            n += chunk.count(b"\n")
            last = chunk[-1:]
    return n + (1 if last not in (b"", b"\n") else 0)


def _last_lines(path: Path, limit: int) -> list[str]:
    """The last `limit` lines, read backwards in blocks from the end of the file."""
    with path.open("rb") as f:
        f.seek(0, 2)
        end = pos = f.tell()
        data = b""
        while pos > 0 and data.count(b"\n") <= limit:
            step = min(_CHUNK, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
        lines = data.decode("utf-8", errors="replace").splitlines()
        if pos > 0:
            lines = lines[1:]            # the first line read may be cut off at the block boundary
        return lines[-limit:] if end else []


def _tail(path: Path | None, *, offset: int | None, limit: int) -> tuple[list[str], int]:
    """(lines, total line count) of a log: its last `limit` lines, or `limit` lines from `offset`. Streams the
    file (a run's log can be large), never reading it whole into memory."""
    if path is None or not path.exists():
        return [], 0
    total = _count_lines(path)
    if offset is None:
        return (_last_lines(path, limit) if limit else []), total
    out: list[str] = []
    if offset < total:
        with path.open("rb") as f:
            for i, line in enumerate(f):
                if i >= offset + limit:
                    break
                if i >= offset:
                    out.append(line.decode("utf-8", errors="replace").rstrip("\r\n"))
    return out, total


# ── catalogue ────────────────────────────────────────────────────────────────

@router.get("/api/strategies", response_model=list[StrategyDTO])
def strategies() -> list[StrategyDTO]:
    from stockify.strategies import _catalogue
    return [StrategyDTO(**s) for s in _catalogue.catalogue()]


# ── fixed /api/tests paths (before /{run_id}) ────────────────────────────────

@router.get("/api/tests", response_model=list[TestRunDTO])
def list_tests(status: str | None = Query(None), family: str | None = Query(None),
                     offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=500)) -> list[TestRunDTO]:
    from stockify import store
    if status is not None and status not in STATUSES:
        raise HTTPException(status_code=422, detail=[f"status must be one of {', '.join(STATUSES)}"])
    return [run_dto(r) for r in store.list_runs(status=status, family_id=family, offset=offset, limit=limit)]


@router.post("/api/tests", response_model=TestCreatedDTO, status_code=201)
def create_test(config: dict = Body(...)) -> TestCreatedDTO:
    from stockify import runconfig, runs
    from stockify.strategies import _catalogue
    from server.services.test_queue import get_queue
    try:
        cfg = runconfig.load(config)
    except runconfig.RunConfigError as e:
        raise HTTPException(status_code=422, detail=e.problems)     # one line per problem (REG 5.3)
    s = cfg["strategy"]
    if s["kind"] not in ("python", "cpp"):
        raise HTTPException(status_code=422, detail=[f"strategy.kind: {s['kind']!r} cannot be started from the API"])
    schema = _catalogue.schema_for(s["kind"], s["ref"])
    if schema is None:
        raise HTTPException(status_code=422, detail=[f"strategy.ref: {s['ref']!r} is not in the catalogue"])
    problems = _catalogue.validate_params(schema, s.get("params", {}))
    if problems:
        raise HTTPException(status_code=422, detail=[f"strategy.params.{p}" for p in problems])
    try:
        run_id = runs.queue(config)
    except runconfig.RunConfigError as e:                           # the locked holdout (M6.6): 422, no traceback
        raise HTTPException(status_code=422, detail=e.problems)
    get_queue().submit(run_id)
    return TestCreatedDTO(run_id=run_id)


_MAX_ID = 128  # paths.run_path's own bound


@router.get("/api/tests/compare", response_model=ComparisonDTO)
def compare_tests(ids: list[str] = Query(default=[], max_length=24)) -> ComparisonDTO:
    """2 to 6 finished runs side by side (DMF 6.1). `ids` repeats (`?ids=a&ids=b`); a comma list is split too.
    Declared before `/api/tests/{run_id}` so `compare` is never read as a run id."""
    from stockify.reporting import compare as rc
    from server.routers.research import _clean
    wanted = [p.strip() for v in ids for p in v.split(",") if p.strip()]
    if not rc.MIN_RUNS <= len(wanted) <= rc.MAX_RUNS or len(set(wanted)) != len(wanted):
        raise HTTPException(status_code=422, detail="compare 2 to 6 runs")
    if any(len(w) > _MAX_ID for w in wanted):
        raise HTTPException(status_code=422, detail=f"a run id is at most {_MAX_ID} characters")
    try:
        c = rc.compare_runs(wanted)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"no test run {str(e.args[0])!r}") from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return ComparisonDTO(**_clean(c))


# ── one run ──────────────────────────────────────────────────────────────────

@router.get("/api/tests/{run_id}", response_model=TestRunDetailDTO)
def get_test(run_id: str) -> TestRunDetailDTO:
    row = _row_or_404(run_id)
    folder = _folder(row)
    manifest = None
    if folder is not None and (folder / "manifest.json").exists():
        manifest = _json((folder / "manifest.json").read_text(encoding="utf-8"), None)
    fill_count, cost_totals = _folder_totals(folder)
    return TestRunDetailDTO(run=run_dto(row), config=_json(row.get("config_json"), {}),
                            metrics=_json(row.get("metrics_json"), {}), manifest=manifest,
                            fill_count=fill_count, cost_totals=cost_totals)


@router.get("/api/tests/{run_id}/export")
def export_test(run_id: str, with_packs: bool = Query(False)) -> Response:
    """The run as a reproducible zip (DMF 6.2). Loopback only like the whole API. NOTE: this GET writes the derived zip
    into exports/ (a deterministic overwrite). The file name is the checked run id; the refusal text never echoes a match."""
    from stockify.reporting import export
    try:
        path = export.build(run_id, with_packs=with_packs)
    except export.ExportError as e:
        raise HTTPException(status_code=404 if e.code in ("bad_id", "not_found") else 409, detail=str(e)) from None
    return Response(content=path.read_bytes(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{path.name}"'})


@router.get("/api/tests/{run_id}/research", response_model=RunResearchDTO)
def test_research(run_id: str) -> RunResearchDTO:
    """The run's deflated Sharpe and bootstrap intervals (M6.4, M6.8). Derived from the database; the slow
    bootstrap is cached in the run folder's research.json, so this GET may write that derived file."""
    _row_or_404(run_id)
    return run_research_dto(run_id)


@router.get("/api/tests/{run_id}/orders", response_model=list[dict[str, Any]])
def test_orders(run_id: str, offset: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=5000)) -> list[dict]:
    from stockify import store
    _row_or_404(run_id)
    return store.run_rows(run_id, "orders", offset=offset, limit=limit)


@router.get("/api/tests/{run_id}/fills", response_model=list[TestFillDTO])
def test_fills(run_id: str, offset: int = Query(0, ge=0),
                     limit: int = Query(500, ge=1, le=5000)) -> list[TestFillDTO]:
    from stockify import store
    symbols = _symbols(_row_or_404(run_id))
    return [TestFillDTO(fill_id=r["fill_id"], order_id=r["order_id"], t_ns=r["t_ns"], t_ist=_ist(r["t_ns"]) or "",
                        instrument_id=r["instrument_id"], tradingsymbol=symbols.get(r["instrument_id"]),
                        side=r["side"], qty=r["qty"], price_paise=r["price_paise"])
            for r in store.run_rows(run_id, "fills", offset=offset, limit=limit)]


@router.get("/api/tests/{run_id}/equity", response_model=list[TestEquityDTO])
def test_equity(run_id: str, offset: int = Query(0, ge=0),
                      limit: int = Query(5000, ge=1, le=20000)) -> list[TestEquityDTO]:
    from stockify import store
    _row_or_404(run_id)
    return [TestEquityDTO(trade_date=r["trade_date"], nav_paise=r["nav_paise"], cash_paise=r["cash_paise"],
                          margin_paise=r.get("margin_paise"))
            for r in store.run_rows(run_id, "equity", offset=offset, limit=limit)]


@router.get("/api/tests/{run_id}/charges", response_model=list[TestChargeDTO])
def test_charges(run_id: str, offset: int = Query(0, ge=0),
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


# Expiry settlements and deliveries, as the engines log them (identical lines on both, A4 §7):
#   "<t_ns> INFO settled <instrument> <product> at FSP <price> (<source>)"
#   "<t_ns> INFO delivered <stock> CNC <BUY|SELL> <units> at <price> (<source>)"
_SETTLED = re.compile(r"^(\d+) INFO settled (\d+) (\w+) at FSP (-?\d+) \((\w+)\)$")
_DELIVERED = re.compile(r"^(\d+) INFO delivered (\d+) (\w+) (BUY|SELL) (\d+) at (-?\d+) \((\w+)\)$")


@router.get("/api/tests/{run_id}/margin", response_model=list[TestMarginDTO])
def test_margin(run_id: str) -> list[TestMarginDTO]:
    """margin_daily.csv: one row per day and underlying (SPAN-lite runs; a flat-margin run writes none)."""
    from stockify import runs
    row = _row_or_404(run_id)
    folder = _folder(row)
    if folder is None or not (folder / "margin_daily.csv").exists():
        return []
    symbols = _symbols(row)
    return [TestMarginDTO(trade_date=r["trade_date"], underlying_id=r["underlying_id"],
                          underlying=symbols.get(r["underlying_id"]), model=r["model"] or "", step=r.get("step") or "", scan_paise=r["scan"] or 0,
                          somc_paise=r["somc"] or 0, elm_paise=r["elm"] or 0, addons_paise=r["addons"] or 0,
                          total_paise=r["total"] or 0)
            for r in runs.read_csv(folder / "margin_daily.csv")]


@router.get("/api/tests/{run_id}/settlements", response_model=list[TestSettlementDTO])
def test_settlements(run_id: str) -> list[TestSettlementDTO]:
    """Every expiry settlement in cash and every physical delivery of the run, in time order, with the settlement
    charges of each cash-settled contract. `source` is "settle" (NSE's settlement price) or "fallback" (estimated,
    A4 §7.2)."""
    from stockify import runs
    row = _row_or_404(run_id)
    folder = _folder(row)
    if folder is None or not (folder / "events_log.txt").exists():
        return []
    symbols = _symbols(row)
    charges: dict[tuple[int, str], int] = {}
    if (folder / "charges.csv").exists():
        for c in runs.read_csv(folder / "charges.csv"):
            if c["kind"] == "settlement":
                key = (c["instrument_id"], c["trade_date"])
                charges[key] = charges.get(key, 0) + (c["total"] or 0)
    out = []
    with (folder / "events_log.txt").open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if m := _SETTLED.match(line):
                t, iid, product, price, source = int(m[1]), int(m[2]), m[3], int(m[4]), m[5]
                day = _ist_date(t)
                out.append(TestSettlementDTO(t_ns=t, t_ist=_ist(t) or "", trade_date=day, kind="cash", instrument_id=iid,
                                             tradingsymbol=symbols.get(iid), product=product, price_paise=price,
                                             source=source, charges_paise=charges.get((iid, day))))
            elif m := _DELIVERED.match(line):
                t, iid = int(m[1]), int(m[2])
                out.append(TestSettlementDTO(t_ns=t, t_ist=_ist(t) or "", trade_date=_ist_date(t), kind="delivery",
                                             instrument_id=iid, tradingsymbol=symbols.get(iid), product=m[3],
                                             side=m[4], qty=int(m[5]), price_paise=int(m[6]), source=m[7]))
    return out


@router.get("/api/tests/{run_id}/greeks", response_model=list[TestGreekDTO])
def test_greeks(run_id: str) -> list[TestGreekDTO]:
    """The Greeks of every open F&O position at each day end (M4.14). From positions_eod.csv: the option's mark and
    its underlying's mark at day end (D-55), taken as the forward (as the chain view does without a future or a
    synthetic forward); strike, expiry and last trading moment from bundle.json; time from that day's end; the
    run's dated risk-free rate. Black-76 (stockify.fno.black76). No IV → the reason, no numbers."""
    from stockify import runs
    from stockify.fno import black76 as B
    row = _row_or_404(run_id)
    folder = _folder(row)
    if folder is None or not (folder / "positions_eod.csv").exists() or not (folder / "bundle.json").exists():
        return []
    bundle = _json((folder / "bundle.json").read_text(encoding="utf-8"), {})
    inst = {r["id"]: r for r in bundle.get("instruments", []) if isinstance(r, dict) and "id" in r}
    days = {d["date"]: d for d in bundle.get("days", []) if isinstance(d, dict)}
    rates = bundle.get("pricing", {}).get("risk_free", [])
    out = []
    for p in runs.read_csv(folder / "positions_eod.csv"):
        r = inst.get(int(p["instrument_id"]))
        if r is None or r.get("kind") not in ("FUT", "CE", "PE") or not p["qty"]:
            continue
        qty = int(p["qty"])
        under = p.get("underlying_mark_paise")
        under = int(under) if under not in (None, "") else None
        item = TestGreekDTO(trade_date=p["trade_date"], instrument_id=r["id"], tradingsymbol=r.get("tradingsymbol"),
                            underlying=r.get("underlying"), kind=r["kind"], strike_paise=r.get("strike_paise"), expiry=r.get("expiry"), qty=qty,
                            mark_paise=int(p["mark_paise"]), underlying_paise=under)
        if r["kind"] == "FUT":
            item.delta_units = float(qty)
            out.append(item)
            continue
        day = days.get(p["trade_date"])
        end, last = (day or {}).get("day_end_ns"), r.get("last_trading_ns")
        if last is None and day is not None:
            last = _expiry_close_estimate(r, day, p["trade_date"])
            item.expiry_close_estimated = last is not None
        if under is None or not under > 0:
            item.iv_reason = "NO_UNDERLYING_PRICE"
        elif end is None or last is None or B.year_fraction(end, last) <= 0:
            item.iv_reason = "EXPIRED"
        elif not int(p["mark_paise"]) > 0:
            item.iv_reason = "NO_PRICE"
        else:
            t = B.year_fraction(end, last)
            rate = B.rate_on(rates, p["trade_date"])
            is_call, f, k = r["kind"] == "CE", under / 100.0, r["strike_paise"] / 100.0
            sigma, why = B.implied_vol(is_call, int(p["mark_paise"]) / 100.0, f, k, t, rate)
            if why is not None:
                item.iv_reason = why
            else:
                g = B.greeks(is_call, f, k, t, rate, sigma)
                item.iv = sigma
                item.delta_units = g.delta * qty
                item.gamma_units_per_rupee = g.gamma * qty
                item.vega_rupees_per_point = g.vega * qty / 100
                item.theta_rupees_per_day = g.theta * qty / 365
        out.append(item)
    return out


def _expiry_close_estimate(inst: dict, day: dict, trade_date: str) -> int | None:
    """For run folders made before D-61, whose bundles have no last_trading_ns for an expiry after the run's last
    day (newer bundles always carry it). Estimate it as the
    expiry date at the IST time of day this trade date's group closes (IST has no daylight saving, so that is the
    close plus whole days). Comes from the bundle's own sessions, not a hard-coded time."""
    from datetime import date
    from stockify.bundle import group_close_ns
    expiry = inst.get("actual_expiry") or inst.get("expiry")
    group = inst.get("session_group")
    if not expiry or group not in day.get("groups", {}):
        return None
    close = group_close_ns(day, group)
    if close is None:
        return None
    return close + (date.fromisoformat(expiry) - date.fromisoformat(trade_date)).days * 86_400 * 10**9


def _ist_date(t_ns: int) -> str:
    from stockify import timeutil
    return timeutil.ns_to_ist(t_ns).date().isoformat()


@router.get("/api/tests/{run_id}/logs", response_model=TestLogsDTO)
def test_logs(run_id: str, tail: int = Query(40, ge=0, le=5000), offset: int | None = Query(None, ge=0),
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
def cancel_test(run_id: str) -> TestRunDTO:
    from stockify import store
    from server.services.test_queue import get_queue
    row = _row_or_404(run_id)
    queue = get_queue()
    if row["status"] not in ("queued", "running") or queue.cancel(run_id) is None:
        latest = store.get_run(run_id) or row
        if latest["status"] == "running" and run_id not in queue.running():
            raise HTTPException(status_code=409, detail=(
                f"test run {run_id} is running but was not started by this API (for example by "
                "`python -m stockify run` in a terminal); stop it where it was started"))
        raise HTTPException(status_code=409, detail=f"test run {run_id} is {latest['status']}; only waiting or "
                                                    "running tests can be cancelled")
    return run_dto(store.get_run(run_id) or row)

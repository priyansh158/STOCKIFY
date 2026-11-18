"""FS 9: /api/fundamentals, the fundamentals screener. Ideas to research, never orders.

* `GET /api/fundamentals`: the results coming up (today through 28 days), then the newest saved list, best first
  (`position` orders it; the page does not print it), with how many universe stocks are not on it.
* `GET /api/fundamentals/test-record`: the walk-forward summary of the screen's test record, or `available: false`.
* `GET /api/fundamentals/{symbol}`: one stock's figures against its peers' median (table and quarter-by-quarter
  series), the plain reasons, and the filings each figure came from. A stock of the universe that is not on the list
  (no usable results, a held filing, no price) still gets its filings and the reason; a symbol outside the universe
  is a 404.

Everything is read from fundamentals.sqlite and refdata: nothing here calls NSE. No score, rank, place or method
text reaches a response (owner, 2026-11-17): the list's order is the judgement.
"""
from __future__ import annotations

import logging
import re
from datetime import date

from fastapi import APIRouter, HTTPException

from server.schemas import (
    CandidateDetailDTO,
    CandidateDTO,
    DsrDTO,
    FilingDTO,
    FundamentalsListDTO,
    FundamentalsTestRecordDTO,
    RatioDTO,
    ResultsDateDTO,
    SeriesDTO,
    WalkforwardSummaryDTO,
)
from stockify import timeutil
from stockify.fundamentals import calendar, filings, ratios, screen, series, store

log = logging.getLogger("stockify.fundamentals")

router = APIRouter(prefix="/api/fundamentals", tags=["fundamentals"])

NOTES = ["Covers today's listed companies only.", "Sectors are today's NSE industry groups."]
NOT_IN_UNIVERSE = "not one of the stocks the screener covers"
NOT_YET = "not on the list yet: the screen runs after the close"
STILL_RUNNING = "The test is still running."
NO_RECORD = "The test has not been run yet."
_SYMBOL = re.compile(r"^[A-Za-z0-9&_.\-]{1,40}$")

RATIO_LABELS = {"pe": ("P/E", "times"), "pb": ("P/B", "times"), "roe": ("Return on equity", "fraction"),
                "debt_equity": ("Debt to equity", "times"), "op_margin": ("Operating margin", "fraction"),
                "profit_growth": ("Profit growth on a year earlier", "fraction"),
                "nii_growth": ("Net interest income growth on a year earlier", "fraction")}
RATIO_KEYS = {False: ("pe", "pb", "roe", "debt_equity", "op_margin", "profit_growth"),
              True: ("pe", "pb", "roe", "profit_growth", "nii_growth")}


def now_ns() -> int:
    return timeutil.now_ns()


def _today() -> date:
    return timeutil.ns_to_ist(now_ns()).date()


def _universe() -> dict[str, dict]:
    """The newest Nifty 500 list read, every stock (liquid or not), by symbol."""
    as_of = store.latest_as_of()
    return {} if as_of is None else {r["symbol"]: r for r in store.universe_on(as_of)}


def _company(uni: dict[str, dict], symbol: str) -> str:
    return (uni.get(symbol) or {}).get("company") or symbol


def _record() -> tuple[str | None, str | None]:
    """(job id, benchmark label) of the newest stored test record, or (None, None)."""
    record = store.latest_test_record() or {}
    job_id, benchmark = record.get("job_id"), record.get("benchmark")
    return (str(job_id) if job_id else None), (str(benchmark) if benchmark else None)


# ── GET /api/fundamentals ────────────────────────────────────────────────────

@router.get("", response_model=FundamentalsListDTO)
def fundamentals_list() -> FundamentalsListDTO:
    day, rows = screen.latest()
    uni = _universe()
    listed = {r["symbol"] for r in rows}
    items = [CandidateDTO(position=n, symbol=r["symbol"], company=_company(uni, r["symbol"]), sector=r["sector"],
                          short_reason=r["short_reason"]) for n, r in enumerate(rows, start=1)]
    upcoming = [ResultsDateDTO(symbol=m["symbol"], company=_company(uni, m["symbol"]), date=m["meeting_date"],
                               period_hint=m["period_hint"]) for m in calendar.upcoming(_today())]
    held = sum(1 for s, u in uni.items() if u["liquid"] and s not in listed)
    return FundamentalsListDTO(as_of=day, upcoming=upcoming, items=items, held_count=held,
                               test_record_id=_record()[0], notes=list(NOTES))


# ── GET /api/fundamentals/test-record (before /{symbol}, which would take it) ─

@router.get("/test-record", response_model=FundamentalsTestRecordDTO)
def fundamentals_test_record() -> FundamentalsTestRecordDTO:
    """The walk-forward summary of the newest stored test record. A job that is not finished, failed or whose file
    is gone is `available: false` with a plain note; a stored error text is never echoed."""
    from stockify.research import wf_report
    job_id, benchmark = _record()
    if job_id is None:
        return FundamentalsTestRecordDTO(available=False, message=NO_RECORD, notes=list(NOTES))
    try:
        d = wf_report.detail(job_id)
    except wf_report.WfReportError as e:
        note = STILL_RUNNING if e.kind == "not_done" else "The test record could not be read."
        log.info("fundamentals: test record %s not shown (%s)", job_id, e.kind)
        return FundamentalsTestRecordDTO(available=False, message=note, job_id=job_id, notes=list(NOTES))
    from server.routers.research import _clean
    s = _clean(d["summary"])
    s["dsr"] = DsrDTO(**s["dsr"]) if s.get("dsr") is not None else None
    plain = {str(k): str(v) for k, v in (d.get("plain") or {}).items() if isinstance(v, str)}
    return FundamentalsTestRecordDTO(available=True, job_id=job_id, summary=WalkforwardSummaryDTO(**s), plain=plain,
                                     benchmark=benchmark, notes=list(NOTES))


# ── GET /api/fundamentals/{symbol} ───────────────────────────────────────────

def _ratio_rows(values: dict, medians: dict, financial: bool) -> list[RatioDTO]:
    return [RatioDTO(key=k, label=RATIO_LABELS[k][0], value=values.get(k), sector_median=medians.get(k),
                     unit=RATIO_LABELS[k][1]) for k in RATIO_KEYS[financial]]


def _filing(f: dict) -> FilingDTO:
    when = None
    if f["broadcast_ns"] is not None:
        when = timeutil.ns_to_ist(int(f["broadcast_ns"])).strftime("%Y-%m-%d %H:%M")
    url = filings._xbrl_url(f["xbrl_url"])[0] if f["xbrl_url"] else None      # only an NSE archive https link
    return FilingDTO(period_end=f["period_end"], period_kind=f["period_kind"], basis=f["basis"], broadcast_ist=when,
                     xbrl_url=url, status=f["status"], reason=f["reason"])


def _peers(row: dict, rows: list[dict]) -> list[tuple[str, bool]]:
    """The stock's peer group as the screen formed it: its industry's listed stocks, or every listed stock."""
    by_industry = row["sector"] is not None and row["peer_group"] == f"Industry: {row['sector']}"
    group = [r for r in rows if r["sector"] == row["sector"]] if by_industry else rows
    return [(r["symbol"], r["sector"] == ratios.FINANCIAL_SECTOR) for r in group]


@router.get("/{symbol}", response_model=CandidateDetailDTO)
def fundamentals_detail(symbol: str) -> CandidateDetailDTO:
    if not _SYMBOL.match(symbol):
        raise HTTPException(status_code=404, detail=NOT_IN_UNIVERSE)
    sym = symbol.upper()
    uni = _universe()
    u = uni.get(sym)
    if u is None or not u["liquid"]:
        raise HTTPException(status_code=404, detail=NOT_IN_UNIVERSE)
    day_iso, rows = screen.latest()
    day = date.fromisoformat(day_iso) if day_iso else _today()
    row = next((r for r in rows if r["symbol"] == sym), None)
    sector = row["sector"] if row is not None else (u["list_industry"] or None)
    financial = sector == ratios.FINANCIAL_SECTOR
    if row is not None:
        reason, peer_group, reasons = None, row["peer_group"], list(row["reasons"])
        table = _ratio_rows(row["ratios"], row["sector_medians"], financial)
        peers = _peers(row, rows)
    else:
        d = ratios.detail(sym, day, sector=sector)
        reason = screen.left_out_reason(d, day) or NOT_YET
        peer_group, reasons = None, [f"Not on the list: {reason}."]
        table = _ratio_rows(d["ratios"], {}, financial)
        peers = []
    points = series.peer_series(sym, day, financial=financial, peers=peers)
    shown = sorted(filings.for_symbol(sym), key=lambda f: (f["period_end"], f["broadcast_ns"] or 0), reverse=True)
    return CandidateDetailDTO(symbol=sym, company=_company(uni, sym), sector=sector, peer_group=peer_group,
                              left_out_reason=reason, reasons=reasons, ratios=table,
                              series=[SeriesDTO(**s) for s in points], filings=[_filing(f) for f in shown],
                              as_of=day_iso)

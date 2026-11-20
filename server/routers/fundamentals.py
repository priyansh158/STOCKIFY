"""FS 9: /api/fundamentals, the fundamentals screener. Ideas to research, never orders.

* `GET /api/fundamentals`: the results coming up (today through 28 days), then the newest saved list, best first
  (`position` orders it; the page does not print it): the stocks without a red-flag warning, then the first
  `top_kept` (fundamentals.json) of those (rulings R15, R15a). `held_count` is how many stocks of the universe it was computed from
  the screen could not judge, `not_listed_count` how many it judged but the page does not list. Names and that count use the list's own universe (screen.universe_of), not a newer one.
* `GET /api/fundamentals/test-record`: the walk-forward summary of the screen's test record, or `available: false`.
* `GET /api/fundamentals/{symbol}`: one stock's figures against its peers' median (table and quarter-by-quarter
  series), the plain reasons, and the filings each figure came from. A listed stock's series and peer medians are
  the ones series.precompute kept when the list was saved, so a click makes no point-in-time reads per peer. A stock
  of the newest universe that is not on the list (no usable results, a held filing, no price) gets its filings, its
  own series without medians and `left_out_reason`; any other symbol is a 404 (a stock on the list never is).

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
    FilingDTO,
    FundamentalsListDTO,
    FundamentalsTestRecordDTO,
    PriceContextDTO,
    RatioDTO,
    ResultsDateDTO,
    SeriesDTO,
    WebJobDTO,
)
from server.services import charts
from stockify import timeutil
from stockify.config import loader
from stockify.data import instruments
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


def _universe(as_of: str | None) -> dict[str, dict]:
    """The Nifty 500 list read on `as_of`, every stock (liquid or not), by symbol."""
    return {} if as_of is None else {r["symbol"]: r for r in store.universe_on(as_of)}


def _universes(day: str | None) -> tuple[dict[str, dict], dict[str, dict]]:
    """(the universe the list of `day` was computed from, the newest universe). They differ after a universe
    refresh before the next screen: the list is named and counted against its own, a stock not yet screened is
    found in the newest."""
    newest = store.latest_as_of()
    used = (screen.universe_of(day) if day else None) or newest
    latest = _universe(newest)
    return (latest if used == newest else _universe(used)), latest


def _company(symbol: str, *unis: dict[str, dict]) -> str:
    return next((u[symbol]["company"] for u in unis if u.get(symbol) and u[symbol]["company"]), symbol)


def _record() -> tuple[str | None, str | None]:
    """(job id, benchmark label) of the newest stored test record, or (None, None)."""
    record = store.latest_test_record() or {}
    job_id, benchmark = record.get("job_id"), record.get("benchmark")
    return (str(job_id) if job_id else None), (str(benchmark) if benchmark else None)


# ── GET /api/fundamentals ────────────────────────────────────────────────────

def _top_kept() -> int:
    from stockify.fundamentals import history
    return history.top_kept()


def _shown(rows: list[dict]) -> list[dict]:
    """Rulings R15 / R15a: the placed stocks without a red-flag warning (loss-making, negative equity), then the
    best `top_kept` of those, so the page shows up to `top_kept` clean names. The rest stay on the saved list, so
    their details page still shows their figures and the warning."""
    return [r for r in rows if not r.get("red_flags")][:_top_kept()]


@router.get("", response_model=FundamentalsListDTO)
def fundamentals_list() -> FundamentalsListDTO:
    day, rows = screen.latest()
    used, latest = _universes(day)
    listed = {r["symbol"] for r in rows}
    shown = _shown(rows)
    items = [CandidateDTO(position=n, symbol=r["symbol"], company=_company(r["symbol"], used, latest),
                          sector=r["sector"], short_reason=r["short_reason"]) for n, r in enumerate(shown, start=1)]
    upcoming = [ResultsDateDTO(symbol=m["symbol"], company=_company(m["symbol"], latest, used), date=m["meeting_date"],
                               period_hint=m["period_hint"]) for m in calendar.upcoming(_today())
                if any(u.get(m["symbol"], {}).get("liquid") for u in (used, latest))]   # CW 3: not the watchlist's
    held = sum(1 for s, u in used.items() if u["liquid"] and s not in listed)
    return FundamentalsListDTO(as_of=day, upcoming=upcoming, items=items, held_count=held,
                               not_listed_count=len(rows) - len(shown),
                               test_record_id=_record()[0], notes=list(NOTES))


# ── POST /api/fundamentals/backfill: the first download, as a web job ───────

IN_SESSION = ("The recorder's session is in progress. The download reads NSE for a few hours, so it waits for the "
              "close: start it after the session ends.")
RUN_BUSY = ("Another fundamentals run is in progress (the evening update or an earlier download). Start this when "
            "it has finished.")
_STOPPED = {
    "blocked": "NSE refused the requests. Nothing is lost: start it again later and it carries on where it stopped.",
    "no universe": "The list of companies could not be read from NSE yet (it needs about three months of NSE's "
                   "daily price files). Try again later.",
    "no progress": "NSE's archive kept failing on the same files. The rest are kept: start it again later.",
}


def _files_kept() -> int:
    """Filing files saved so far, over every run of the download."""
    from stockify.fundamentals import filings
    return len(filings.by_status("downloaded")) + len(filings.by_status("parsed"))


def paused_sentence(n: int) -> str:
    return (f"Paused for the market session: {n:,} filing {'file' if n == 1 else 'files'} saved so far. Start it "
            "again after the close and it carries on where it stopped.")


_PROGRESS = re.compile(r"^\[(\d+)/(\d+)\] ")


def _in_session() -> bool:
    from stockify.daemon import scheduler
    return scheduler.session_in_progress(now_ns())


def backfill_sentence(res: dict) -> str:
    files = (res.get("download") or {}).get("downloaded", 0)
    listed, saved = res.get("listed", 0), res.get("saved")
    tail = f" The list has {saved} stocks." if saved is not None else ""
    return (f"Finished: the filings of {listed} {'company' if listed == 1 else 'companies'} read and {files} "
            f"result {'file' if files == 1 else 'files'} downloaded.{tail}")


@router.post("/backfill", response_model=WebJobDTO, status_code=202)
def start_backfill() -> WebJobDTO:
    """`stockify fundamentals backfill` as a web job: every universe company's results filings since filings_from,
    their XBRL figures and today's list, about one NSE request a second (resumable). 409 during the recorder's
    session, while another fundamentals run holds the run lock, or while this download runs. Nothing is taken from
    the client. It asks the session again before each company and file batch, and the Stop button ends it there."""
    from server.routers.jobs import _dto_of_web
    from server.services import web_jobs
    from server.services.web_jobs import JobRefused, WebJob
    from stockify.fundamentals import job as fjob
    from stockify.research import pool
    if _in_session():
        raise HTTPException(status_code=409, detail=IN_SESSION)
    try:
        with fjob.hold():                       # the daemon's job or a command holds it: say so now, not later
            pass
    except fjob.Busy:
        raise HTTPException(status_code=409, detail=RUN_BUSY) from None

    def work(j: WebJob) -> None:
        def progress(line: str) -> None:
            m = _PROGRESS.match(line)
            if m:
                j.done, j.total = int(m.group(1)), int(m.group(2))
            log.info("fundamentals download: %s", line)
        try:
            res = fjob.backfill(progress=progress, in_session=lambda: j.cancel.is_set() or _in_session())
        except fjob.Busy:
            raise web_jobs.PlainFailure(RUN_BUSY) from None
        if j.cancel.is_set():
            raise pool.Cancelled()
        if res.get("stopped") == "session":     # planned and resumable: a pause, not a failure
            raise web_jobs.PlannedPause(paused_sentence(_files_kept()))
        if res.get("stopped") is not None:
            raise web_jobs.PlainFailure(_STOPPED.get(res["stopped"], "The download stopped before it finished. "
                                                     "Start it again; it carries on where it stopped."))
        j.result = backfill_sentence(res)

    try:
        job = web_jobs.get_jobs().start("fundamentals", "backfill", work)
    except JobRefused as e:
        raise HTTPException(status_code=e.status, detail=e.message) from None
    return _dto_of_web(job)


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
    from server.routers.research import walkforward_summary_dto
    plain = {str(k): str(v) for k, v in (d.get("plain") or {}).items() if isinstance(v, str)}
    return FundamentalsTestRecordDTO(available=True, job_id=job_id, summary=walkforward_summary_dto(d["summary"]),
                                     plain=plain,
                                     benchmark=benchmark, notes=list(NOTES))


# ── GET /api/fundamentals/{symbol} ───────────────────────────────────────────

def _ratio_rows(values: dict, medians: dict, financial: bool) -> list[RatioDTO]:
    return [RatioDTO(key=k, label=RATIO_LABELS[k][0], value=values.get(k), sector_median=medians.get(k),
                     unit=RATIO_LABELS[k][1]) for k in RATIO_KEYS[financial]]


def _filing(f: dict) -> FilingDTO:
    when = None
    if f["broadcast_ns"] is not None:
        when = timeutil.ns_to_ist(int(f["broadcast_ns"])).strftime("%Y-%m-%d %H:%M")
    url = filings.nse_xbrl_url(f["xbrl_url"])[0] if f["xbrl_url"] else None      # only an NSE archive https link
    return FilingDTO(period_end=f["period_end"], period_kind=f["period_kind"], basis=f["basis"], broadcast_ist=when,
                     xbrl_url=url, status=f["status"], reason=f["reason"])


def _price(sym: str) -> PriceContextDTO | None:
    """The stock's last close and 52-week range from its daily candles (the watchlist's rule); None without candles.
    It never fails the page: a read that goes wrong is no price context."""
    try:
        iid = instruments.find("NSE", sym)
        rows = charts.stock_rows(iid) if iid is not None else []
        if not rows:
            return None
        last, rng = charts.stock_range(rows, _today(), loader.load_json("watchlist.json"))
    except Exception as e:  # noqa: BLE001
        log.info("fundamentals: no price context for %s (%s)", sym, type(e).__name__)
        return None
    d: dict = {}
    if last:
        d.update(last_close_paise=last[0], last_close_date=last[1])
    if rng:
        d.update(high_paise=rng["high_paise"], high_date=rng["high_date"], low_paise=rng["low_paise"],
                 low_date=rng["low_date"])
    return PriceContextDTO(**d)


@router.get("/{symbol}", response_model=CandidateDetailDTO)
def fundamentals_detail(symbol: str) -> CandidateDetailDTO:
    if not _SYMBOL.match(symbol):
        raise HTTPException(status_code=404, detail=NOT_IN_UNIVERSE)
    sym = symbol.upper()
    day_iso, rows = screen.latest()
    used, latest = _universes(day_iso)
    row = next((r for r in rows if r["symbol"] == sym), None)
    u = latest.get(sym)
    if row is None and (u is None or not u["liquid"]):          # a stock on the list is always shown
        raise HTTPException(status_code=404, detail=NOT_IN_UNIVERSE)
    day = date.fromisoformat(day_iso) if day_iso else _today()
    sector = row["sector"] if row is not None else (u["list_industry"] or None)
    financial = sector == ratios.FINANCIAL_SECTOR
    points = None
    if row is not None:
        reason, peer_group, reasons = None, row["peer_group"], list(row["reasons"])
        table = _ratio_rows(row["ratios"], row["sector_medians"], financial)
        points = series.saved(day_iso, sym)                     # kept when the list was saved: no reads per peer
    else:
        d = ratios.detail(sym, day, sector=sector)
        reason = screen.left_out_reason(d, day, sym) or NOT_YET
        peer_group, reasons = None, []                          # the reason is said once, in left_out_reason
        table = _ratio_rows(d["ratios"], {}, financial)
    if points is None:                                          # not on the list: its own figures, no medians
        points = series.peer_series(sym, day, financial=financial, peers=[])
    shown = sorted(filings.for_symbol(sym), key=lambda f: (f["period_end"], f["broadcast_ns"] or 0), reverse=True)
    return CandidateDetailDTO(symbol=sym, company=_company(sym, used, latest), sector=sector, peer_group=peer_group,
                              left_out_reason=reason, listed=any(r["symbol"] == sym for r in _shown(rows)),
                              reasons=reasons, ratios=table,
                              series=[SeriesDTO(**s) for s in points], filings=[_filing(f) for f in shown],
                              as_of=day_iso, price=_price(sym))

"""HD 9.2, 12.2: /api/desk, the holdings desk. A verdict chip per Kite holding, one holding's view, and its chart.

* `GET /api/desk`: a chip per holding the desk looks at (its verdict and the close it is from), and the others with
  the reason (BSE only, not an NSE stock, not graded yet).
* `GET /api/desk/{symbol}`: the verdict with its reasons, the agreeing and disagreeing rules, each rule's signal and
  record on the stock.
* `GET /api/desk/{symbol}/chart`: the daily candles as the RunChartDTO the price chart reads; no markers; one line
  per voting rule that plots an exit level.

The desk updates once a day, after the session (owner, 2026-11-17): every answer is the nightly job's stored view of
the latest close, read from desk.sqlite and the bar cache. Nothing here calls Kite except the holdings list
(`portfolio_sync`), which says which holdings to show. Kite logged out: a 200 with the last stored views, never a 500.

The owner's average cost, P&L and any profit figure never appear here (owner, 2026-11-17).
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import date

from fastapi import APIRouter, HTTPException

import portfolio_sync
from server.schemas import DeskChipDTO, DeskListDTO, DeskSignalDTO, DeskSkipDTO, DeskViewDTO, RunChartDTO
from stockify import timeutil
from stockify.data import instruments
from stockify.desk import nightly, replay, store

log = logging.getLogger("stockify.desk")

router = APIRouter(prefix="/api/desk", tags=["desk"])

NOT_GRADED = "not graded yet: the desk grades each holding after the close"
CHART_DAYS = 500                                # about two years of daily candles
_SYMBOL = re.compile(r"^[A-Za-z0-9&_.\-]{1,40}$")

_lock = threading.Lock()
_charts: dict[int, tuple[tuple, RunChartDTO]] = {}     # iid -> (key, chart)


def reset() -> None:
    with _lock:
        _charts.clear()


def now_ns() -> int:
    return timeutil.now_ns()


def ist_day(t_ns: int) -> date:
    return timeutil.ns_to_ist(t_ns).date()


# ── which holding, which view ────────────────────────────────────────────────

def _snapshot():  # noqa: ANN202
    try:
        return portfolio_sync.cached_snapshot(60)
    except Exception as e:  # noqa: BLE001 — Kite trouble is "logged out" to the page
        log.info("desk: no holdings from Kite (%s)", type(e).__name__)
        return portfolio_sync.PortfolioSnapshot(holdings=[], fetched_at=0, error=type(e).__name__)


def _symbol(h, iid: int) -> str:  # noqa: ANN001
    """The NSE symbol the desk files the holding under (a BSE holding's own symbol may differ)."""
    return (instruments.get(iid) or {}).get("tradingsymbol") or (h.tradingsymbol if h is not None else "")


def _find(symbol: str, t_ns: int):  # noqa: ANN202
    """(holding or None, instrument_id, logged_in) for a symbol in the URL; 404 when the desk has nothing on it."""
    if not _SYMBOL.match(symbol):
        raise HTTPException(status_code=404, detail="no such holding on the desk")
    sym = symbol.upper()
    snap = _snapshot()
    day = ist_day(t_ns)
    if not snap.error:
        for h in snap.holdings:
            if h.tradingsymbol.upper() == sym:
                iid, reason = nightly.resolve(h, day)
                if reason is None and store.latest_view(iid) is not None:
                    return h, iid, True
                raise HTTPException(status_code=404, detail=reason or NOT_GRADED)
        raise HTTPException(status_code=404, detail="not one of your holdings")
    iid = instruments.find("NSE", sym)                 # logged out: the last stored view, if there is one
    if iid is None or store.latest_view(iid) is None:
        raise HTTPException(status_code=404, detail="no such holding on the desk")
    return None, iid, False


# ── GET /api/desk ────────────────────────────────────────────────────────────

@router.get("", response_model=DeskListDTO)
def desk_list() -> DeskListDTO:
    t_ns = now_ns()
    snap = _snapshot()
    if snap.error:
        items = [DeskChipDTO(symbol=r["symbol"], exchange="NSE", instrument_id=r["instrument_id"],
                             verdict=r["verdict"], as_of_ns=int(r["made_ns"]), close_date=r["view"].get("close_date"))
                 for r in store.latest_views()]
        plain = ("Your Kite login has expired, so these are the last closing views of the stocks the desk has graded; "
                 "log in again to see your current holdings." if getattr(snap, "expired", False) else
                 "Kite is not logged in, so these are the last closing views of the stocks the desk has graded; "
                 "log in to see your current holdings.")
        return DeskListDTO(items=sorted(items, key=lambda c: c.symbol), skipped=[], plain=plain)
    day = ist_day(t_ns)
    items, skipped = [], []
    for h in snap.holdings:
        iid, reason = nightly.resolve(h, day)
        if reason is not None:
            skipped.append(DeskSkipDTO(symbol=h.tradingsymbol, reason=reason))
            continue
        row = store.latest_view(iid)
        if row is None:
            skipped.append(DeskSkipDTO(symbol=h.tradingsymbol, reason=NOT_GRADED))
            continue
        items.append(DeskChipDTO(symbol=_symbol(h, iid), exchange="NSE", instrument_id=iid, verdict=row["verdict"],
                                 as_of_ns=int(row["made_ns"]), close_date=row["view"].get("close_date"),
                                 holding_symbol=h.tradingsymbol, holding_exchange=h.exchange))
    return DeskListDTO(items=items, skipped=skipped)


# ── GET /api/desk/{symbol} ───────────────────────────────────────────────────

def _signals(view: dict, rows: list[dict]) -> list[DeskSignalDTO]:
    by_ref = {r["ref"]: r for r in rows}
    out = []
    for s in view.get("signals") or []:
        ev = by_ref.get(s["ref"], {})
        out.append(DeskSignalDTO(
            display_name=s["display_name"], in_position=bool(s["in_position"]), buying_today=bool(s["buying_today"]),
            exit_level_paise=s.get("exit_level_paise"), grade=s.get("grade") or ev.get("grade") or "none",
            trades=ev.get("trades"), windows_won=ev.get("windows_won"), windows_total=ev.get("windows_total"),
            cagr=ev.get("cagr"), buy_hold_cagr=ev.get("buy_hold_cagr"), note=replay.plain_note(s.get("note") or "")))
    return out


def _day_text(t_ns: int) -> str:
    d = timeutil.ns_to_ist(t_ns)
    return f"{d.day} {d.strftime('%b')} {d.year}"


def _plain(view: dict, made_ns: int, logged_in: bool) -> str:
    close = view.get("close_date")
    when = _day_text(timeutil.ist_ns(date.fromisoformat(close), "12:00") if close else made_ns)
    if not logged_in:
        return f"Kite is not logged in, so this is the last closing view the desk stored, from the close on {when}."
    return "The desk updates once a day, after the session, from that day's closing prices."


@router.get("/{symbol}", response_model=DeskViewDTO)
def desk_view(symbol: str) -> DeskViewDTO:
    t_ns = now_ns()
    h, iid, logged_in = _find(symbol, t_ns)
    sym = _symbol(h, iid) or symbol.upper()
    row = store.latest_view(iid)
    view, made = row["view"], int(row["made_ns"])
    rows = store.latest_evidence(iid)
    return DeskViewDTO(
        symbol=sym, instrument_id=iid, verdict=view["verdict"], exit_share=float(view["exit_share"]),
        nearest_exit_paise=view.get("nearest_exit_paise"), price_paise=int(view["close_paise"]),
        counts=view.get("counts") or "", reasons=list(view.get("reasons") or []), agree=list(view.get("agree") or []),
        disagree=list(view.get("disagree") or []), signals=_signals(view, rows),
        evidence_from=min((r["from_date"] for r in rows), default=None),
        evidence_to=view.get("evidence_to") or max((r["to_date"] for r in rows), default=None),
        as_of_ns=made, close_date=view.get("close_date"), plain=_plain(view, made, logged_in))


# ── GET /api/desk/{symbol}/chart ─────────────────────────────────────────────

def _model(iid: int, sym: str, interval_s: int, basis: str, bars: list, plots: list[dict]) -> dict:
    return {"instruments": [{"id": iid, "symbol": sym, "kind": "equity"}], "instrument_id": iid, "basis": basis,
            "interval_s": interval_s,
            "bars": [{"start_ns": b.start_ns, "open": b.open, "high": b.high, "low": b.low, "close": b.close,
                      "volume": b.volume} for b in bars],
            "markers": [], "omitted_markers": 0, "plots": plots, "truncated": False, "next_from_ns": None,
            "data_state": "ok", "notice": None, "pnl_complete": True, "skipped_settlements": 0,
            "idle_settlements": 0}


def _dto(model: dict, labels: dict[str, str]) -> RunChartDTO:
    from server.routers.tests import _chart_dto
    dto = _chart_dto(model)
    plots = [p.model_copy(update={"label": labels.get(p.name, p.label)}) for p in dto.plots]
    return dto.model_copy(update={"plots": plots})


def _plot_name(ref: str) -> str:
    return ref.rpartition(".")[2].partition(":")[0]


def _day_chart(iid: int, sym: str) -> RunChartDTO:
    """The stored daily candles (the last CHART_DAYS) with each voting rule's exit level, day by day. Each rule is
    replayed with `replay.window` bars of warm-up before the first candle shown, so its line says what a replay over
    the whole history says. Cached until the bar cache's candles change."""
    bars = replay.daily_bars(iid)
    # the first close too: a split or bonus refetch rewrites the older (adjusted) candles
    key = (len(bars), bars[-1].start_ns, bars[-1].close, bars[0].close) if bars else (0,)
    with _lock:
        hit = _charts.get(iid)
    if hit is not None and hit[0] == key:
        return hit[1]
    shown = bars[-CHART_DAYS:]
    first = shown[0].start_ns if shown else None
    plots, labels = [], {}
    sym_param = f"NSE:{sym}"
    for ref, extra in replay.VOTERS:
        params = {"symbol": sym_param, **extra}
        warm = replay.window(ref, params)
        part = bars[-(CHART_DAYS + warm):]
        steps = replay.walk(ref, params, part, warm=max(0, len(part) - len(shown)))
        pts = [[b.start_ns, s.exit_level_paise] for b, s in zip(part[len(part) - len(steps):], steps)
               if s.exit_level_paise is not None and b.start_ns >= first]
        if pts:
            name = _plot_name(ref)
            plots.append({"name": name, "kind": "price", "instrument_id": iid, "points": pts})
            labels[name] = f"{steps[-1].display_name} exit level"
    dto = _dto(_model(iid, sym, replay.DAY_S, "adjusted", shown, plots), labels)
    with _lock:
        _charts[iid] = (key, dto)
    return dto


@router.get("/{symbol}/chart", response_model=RunChartDTO)
def desk_chart(symbol: str) -> RunChartDTO:
    h, iid, _logged_in = _find(symbol, now_ns())
    return _day_chart(iid, _symbol(h, iid) or symbol.upper())

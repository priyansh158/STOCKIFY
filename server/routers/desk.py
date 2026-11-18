"""HD 9.2: /api/desk, the holdings desk. A verdict chip per Kite holding, one holding's view, and its chart.

* `GET /api/desk`: a chip per holding the desk looks at (its verdict, when, live or not), and the others with the
  reason (BSE only, not an NSE stock, not graded yet).
* `GET /api/desk/{symbol}`: the verdict with its reasons, the agreeing and disagreeing rules, each rule's signal and
  record on the stock, the option chain gauge and ideas (F&O stocks), the lot.
* `GET /api/desk/{symbol}/chart?interval=day|minute`: the RunChartDTO the price chart reads; no markers; one line
  per voting rule that plots an exit level.

During the session the view is recomputed from the live price (`desk_poller.live_view`, cached 30 s); otherwise,
or when the live price cannot be read, it is the nightly job's stored closing view with `live` False. Kite logged
out, a refused quote, a missing snapshot: a 200 with the last stored view and snapshot and their times, never a 500.

The owner's average cost, P&L and any profit figure never appear here (owner, 2026-11-17); the only quantity used
is the holding's share count, for the "at least one lot" check of the option ideas.
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

import kite_client
import portfolio_sync
from server.schemas import (DeskChipDTO, DeskGaugeDTO, DeskIdeaDTO, DeskIdeaLegDTO, DeskListDTO, DeskSignalDTO,
                            DeskSkipDTO, DeskViewDTO, RunChartDTO)
from server.services import desk_poller as P
from stockify import timeutil
from stockify.daemon import scheduler
from stockify.data import calendar, history, instruments
from stockify.desk import chain, ideas, nightly, replay, store, thesis

log = logging.getLogger("stockify.desk")

router = APIRouter(prefix="/api/desk", tags=["desk"])

NS = timeutil.NS_PER_S
NOT_GRADED = "not graded yet: the desk grades each holding after the close"
NO_OPTIONS = "No options are listed on NSE for this stock."
CHART_DAYS = 500                                # about two years of daily candles
MINUTE_REFRESH_S = 60
_SYMBOL = re.compile(r"^[A-Za-z0-9&_.\-]{1,40}$")

_lock = threading.Lock()
_charts: dict[tuple[int, str], tuple[tuple, RunChartDTO]] = {}     # (iid, interval) -> (key, chart)
_minutes: dict[tuple[int, str], tuple[int, list[dict]]] = {}        # (iid, IST date) -> (fetched_ns, candles)
_dtos: dict[int, tuple[tuple, int, DeskViewDTO]] = {}               # iid -> (key, made_ns, view)


def reset() -> None:
    with _lock:
        _charts.clear()
        _minutes.clear()
        _dtos.clear()


# ── which holding, which view ────────────────────────────────────────────────

def _snapshot():  # noqa: ANN202
    try:
        return portfolio_sync.cached_snapshot(60)
    except Exception as e:  # noqa: BLE001 — Kite trouble is "logged out" to the page
        log.info("desk: no holdings from Kite (%s)", type(e).__name__)
        return portfolio_sync.PortfolioSnapshot(holdings=[], fetched_at=0, error=type(e).__name__)


def _logged_in() -> bool:
    try:
        return bool(kite_client.is_authenticated())
    except Exception:  # noqa: BLE001
        return False


def _current(iid: int, symbol: str, token, t_ns: int,  # noqa: ANN001
             max_age_s: int = P.LIVE_CACHE_S) -> tuple[dict, int, bool] | None:
    """(view json, as_of_ns, live): the live recompute in the session, else the stored closing view."""
    live = P.live_view(iid, symbol, token, t_ns, max_age_s=max_age_s)
    if live is not None:
        return live["view"], live["as_of_ns"], True
    row = store.latest_view(iid)
    if row is None:
        return None
    return row["view"], int(row["made_ns"]), False


def _find(symbol: str, t_ns: int):  # noqa: ANN202
    """(holding or None, instrument_id, logged_in) for a symbol in the URL; 404 when the desk has nothing on it."""
    if not _SYMBOL.match(symbol):
        raise HTTPException(status_code=404, detail="no such holding on the desk")
    sym = symbol.upper()
    snap = _snapshot()
    day = P.ist_day(t_ns)
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
    t_ns = P.now_ns()
    snap = _snapshot()
    if snap.error:
        items = [DeskChipDTO(symbol=r["symbol"], exchange="NSE", instrument_id=r["instrument_id"],
                             verdict=r["verdict"], as_of_ns=int(r["made_ns"]), live=False)
                 for r in store.latest_views()]
        return DeskListDTO(items=sorted(items, key=lambda c: c.symbol), skipped=[],
                           plain="Kite is not logged in, so these are the last closing views of the stocks the desk "
                                 "has graded; log in to see your current holdings.")
    day = P.ist_day(t_ns)
    items, skipped = [], []
    for h in snap.holdings:
        iid, reason = nightly.resolve(h, day)
        if reason is not None:
            skipped.append(DeskSkipDTO(symbol=h.tradingsymbol, reason=reason))
            continue
        sym = P._symbol(h, iid)
        cur = _current(iid, sym, h.instrument_token, t_ns, max_age_s=2 * P.INTERVAL_S)
        if cur is None:
            skipped.append(DeskSkipDTO(symbol=h.tradingsymbol, reason=NOT_GRADED))
            continue
        view, as_of, live = cur
        items.append(DeskChipDTO(symbol=sym, exchange="NSE", instrument_id=iid, verdict=view["verdict"],
                                 as_of_ns=as_of, live=live))
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


def _verdict(view: dict) -> thesis.Verdict:
    return thesis.Verdict(view["verdict"], float(view["exit_share"]), view.get("nearest_exit_paise"),
                          list(view.get("reasons") or []), list(view.get("agree") or []),
                          list(view.get("disagree") or []), view.get("counts") or "")


def _margin_client():  # noqa: ANN202
    if not _logged_in():
        return None
    try:
        from stockify import kite_readonly
        return kite_readonly.client()
    except Exception:  # noqa: BLE001 — no margins, the ideas say so
        return None


def _chain_part(h, iid: int, sym: str, view: dict, t_ns: int, logged_in: bool):  # noqa: ANN001, ANN202
    """(fno, lot, gauge dto, idea dtos, extra plain sentence)."""
    day = P.ist_day(t_ns)
    if not chain.is_fno(sym, day):
        return False, None, None, [], NO_OPTIONS
    lot = chain.lot_size(sym, day)
    row = store.latest_chain(iid)
    if row is None:
        return True, lot, None, [], "No option chain has been read for this stock yet; it is read during the session."
    snap, taken = row["snapshot"], int(row["taken_ns"])
    spot = int(view.get("close_paise") or snap.get("spot_paise") or 0)
    g = chain.gauge(snap, spot, P.iv_history(sym, day))
    chain_live = P.in_session(t_ns) and 0 <= t_ns - taken <= P.CHAIN_FRESH_NS
    gauge = DeskGaugeDTO(expiry=g["expiry"], pcr_oi=g["pcr_oi"], call_walls=list(g["call_walls"]),
                         put_walls=list(g["put_walls"]), max_pain_paise=g["max_pain_paise"], atm_iv=g["atm_iv"],
                         iv_percentile=g["iv_percentile"], sentences=list(g["sentences"]), taken_ns=taken,
                         live=chain_live)
    if h is None or not lot:
        return True, lot, gauge, [], ("Option ideas need your holdings from Kite." if h is None else "")
    # free shares for the lot check: Kite's quantity; no pledged count exists, so none is assumed pledged.
    free_qty = int(h.quantity or 0)
    out = ideas.ideas(_verdict(view), snap, g, spot, lot, free_qty, margin_client=_margin_client(),
                      atr_paise=float(view.get("atr_paise") or 0) or None)
    dtos = [DeskIdeaDTO(name=i.name, legs=[DeskIdeaLegDTO(**leg) for leg in i.legs], premium_paise=i.premium_paise,
                        max_loss_paise=i.max_loss_paise, max_gain_paise=i.max_gain_paise,
                        breakeven_paise=list(i.breakeven_paise), margin_paise=i.margin_paise, margin_ok=i.margin_ok,
                        why=i.why, caution=i.caution, possible=i.possible, not_possible_reason=i.not_possible_reason)
            for i in out]
    note = "" if chain_live else "The option figures are from the last chain read, at the time shown."
    return True, lot, gauge, dtos, note


def _day_text(t_ns: int) -> str:
    d = timeutil.ns_to_ist(t_ns)
    return f"{d.day} {d.strftime('%b')} {d.year}"


def _plain(view: dict, as_of: int, live: bool, logged_in: bool, t_ns: int, extra: str) -> str:
    close = view.get("close_date")
    when = _day_text(timeutil.ist_ns(date.fromisoformat(close), "12:00") if close else as_of)
    if live:
        d = timeutil.ns_to_ist(as_of)
        head = f"Live: worked out from the price at {d.strftime('%H:%M')} IST, with today's candle so far."
    elif not logged_in:
        head = f"Kite is not logged in, so this is the closing view of {when}."
    elif P.in_session(t_ns):
        head = f"The live price could not be read, so this is the closing view of {when}."
    else:
        head = f"The closing view of {when}; during the session it follows the live price."
    return " ".join(x for x in (head, extra) if x)


@router.get("/{symbol}", response_model=DeskViewDTO)
def desk_view(symbol: str) -> DeskViewDTO:
    t_ns = P.now_ns()
    h, iid, logged_in = _find(symbol, t_ns)
    sym = P._symbol(h, iid) if h is not None else (instruments.get(iid) or {}).get("tradingsymbol", symbol.upper())
    cur = _current(iid, sym, h.instrument_token if h is not None else None, t_ns) if logged_in else None
    if cur is None:
        row = store.latest_view(iid)
        cur = (row["view"], int(row["made_ns"]), False)
    view, as_of, live = cur
    chain_row = store.latest_chain(iid)
    key = (as_of, live, logged_in, chain_row["taken_ns"] if chain_row else None, int(h.quantity) if h else None)
    with _lock:
        hit = _dtos.get(iid)
    if hit is not None and hit[0] == key and 0 <= t_ns - hit[1] < P.LIVE_CACHE_S * NS:
        return hit[2]
    rows = store.latest_evidence(iid)
    fno, lot, gauge, idea_dtos, extra = _chain_part(h, iid, sym, view, t_ns, logged_in)
    if not logged_in and fno:
        extra = " ".join(x for x in (extra, "Option ideas need your holdings from Kite.") if x and x not in extra)
    dto = DeskViewDTO(
        symbol=sym, instrument_id=iid, verdict=view["verdict"], exit_share=float(view["exit_share"]),
        nearest_exit_paise=view.get("nearest_exit_paise"), price_paise=int(view["close_paise"]),
        counts=view.get("counts") or "", reasons=list(view.get("reasons") or []), agree=list(view.get("agree") or []),
        disagree=list(view.get("disagree") or []), signals=_signals(view, rows),
        evidence_from=min((r["from_date"] for r in rows), default=None),
        evidence_to=view.get("evidence_to") or max((r["to_date"] for r in rows), default=None),
        gauge=gauge, ideas=idea_dtos, fno=fno, lot=lot, as_of_ns=as_of, live=live,
        plain=_plain(view, as_of, live, logged_in, t_ns, extra))
    with _lock:
        _dtos[iid] = (key, t_ns, dto)
    return dto


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


def _day_chart(iid: int, sym: str, h, t_ns: int) -> RunChartDTO:  # noqa: ANN001
    bars = replay.daily_bars(iid)
    price = P.live_price(iid, sym, h.instrument_token if h else None, t_ns, allow_call=False) \
        if (h is not None and P.in_session(t_ns)) else None
    if bars and price is not None and price["t_ns"] >= bars[-1].start_ns:
        bars = replay.with_live_candle(bars, price["ltp"], price["t_ns"], price["open"], price["high"], price["low"])
    key = (len(bars), bars[-1].start_ns if bars else None, bars[-1].close if bars else None,
           bars[-1].high if bars else None, bars[-1].low if bars else None)
    with _lock:
        hit = _charts.get((iid, "day"))
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
        _charts[(iid, "day")] = (key, dto)
    return dto


def _minute_day(t_ns: int) -> date:
    """Today once the session has opened, else the last trading day before it."""
    d = P.ist_day(t_ns)
    if calendar.is_trading_day(scheduler.SEGMENT, d) and t_ns >= timeutil.ist_ns(d, "09:15"):
        return d
    return calendar.previous_trading_day(scheduler.SEGMENT, d)


def _minute_candles(iid: int, token, day, t_ns: int) -> list[dict]:  # noqa: ANN001
    """The day's minute candles from Kite `historical_data` (one call per holding, again after MINUTE_REFRESH_S in
    the session), kept in memory, never written to the bar cache (a partial day must not look complete there).
    Logged out or refused: what was read last, else the bar cache's candles for the day."""
    k = (iid, day.isoformat())
    with _lock:
        hit = _minutes.get(k)
    stale = hit is None or (P.in_session(t_ns) and t_ns - hit[0] >= MINUTE_REFRESH_S * NS)
    if stale and token and _logged_in():
        try:
            from stockify import kite_readonly
            lo, hi = timeutil.ist_ns(day, "09:00"), min(t_ns, timeutil.ist_ns(day, "16:00"))
            rows = history._call(kite_readonly.client(), int(token), lo, hi, "minute", continuous=False,
                                 stats=history.FetchStats(iid, "minute"), bkt=history.bucket())
            candles = [{"start_ns": history._ts_ns(r["date"]), "open": history._paise(r["open"]),
                        "high": history._paise(r["high"]), "low": history._paise(r["low"]),
                        "close": history._paise(r["close"]), "volume": int(r.get("volume") or 0)} for r in rows]
            candles.sort(key=lambda c: c["start_ns"])
            with _lock:
                _minutes[k] = hit = (t_ns, candles)
        except Exception as e:  # noqa: BLE001 — the chart shows what it has
            log.info("desk: no minute candles from Kite (%s)", type(e).__name__)
    if hit is not None:
        return hit[1]
    lo = timeutil.ist_ns(day, "00:00")
    return [{"start_ns": int(r["ts_ns"]), "open": int(r["open_paise"]), "high": int(r["high_paise"]),
             "low": int(r["low_paise"]), "close": int(r["close_paise"]), "volume": int(r["volume"] or 0)}
            for r in history.bars(iid, "minute", from_ns=lo, to_ns=lo + 86_400 * NS - 1)]


class _B:
    __slots__ = ("start_ns", "open", "high", "low", "close", "volume")

    def __init__(self, c: dict) -> None:
        for k in self.__slots__:
            setattr(self, k, c[k])


def _minute_chart(iid: int, sym: str, h, t_ns: int, logged_in: bool) -> RunChartDTO:  # noqa: ANN001
    token = (h.instrument_token if h is not None else None) or instruments.token_at(iid, P.ist_day(t_ns))
    day = _minute_day(t_ns)
    bars = [_B(c) for c in _minute_candles(iid, token, day, t_ns)]
    cur = _current(iid, sym, h.instrument_token if h is not None else None, t_ns) if logged_in else None
    view = cur[0] if cur else (store.latest_view(iid) or {}).get("view") or {}
    plots, labels = [], {}
    if bars:
        ends = [bars[0].start_ns, bars[-1].start_ns]
        for s in view.get("signals") or []:
            if s.get("exit_level_paise") is None:
                continue
            name = _plot_name(s["ref"])
            plots.append({"name": name, "kind": "price", "instrument_id": iid,
                          "points": [[t, int(s["exit_level_paise"])] for t in sorted(set(ends))]})
            labels[name] = f"{s['display_name']} exit level"
    return _dto(_model(iid, sym, 60, "raw", bars, plots), labels)


@router.get("/{symbol}/chart", response_model=RunChartDTO)
def desk_chart(symbol: str, interval: Literal["day", "minute"] = Query("day")) -> RunChartDTO:
    t_ns = P.now_ns()
    h, iid, logged_in = _find(symbol, t_ns)
    sym = P._symbol(h, iid) if h is not None else (instruments.get(iid) or {}).get("tradingsymbol", symbol.upper())
    if interval == "minute":
        return _minute_chart(iid, sym, h, t_ns, logged_in)
    return _day_chart(iid, sym, h, t_ns)

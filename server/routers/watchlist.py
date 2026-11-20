"""CW 2: /api/watchlist, the stocks the owner watches.

* `GET /api/watchlist`: the list in the order added, each stock with its last close and its 52-week high and low, read
  from the bar cache (the nightly daily candles). Nothing here calls Kite: live prices come from the ticks socket on the
  page, and the page falls back to the last close here.
* `POST /api/watchlist` {symbol}: 404 when NSE has no such symbol, 422 when it is not an NSE share (an index, a bond),
  409 when it is already on the list or the list is full.
* `DELETE /api/watchlist/{symbol}`: 204, or 404 when it is not on the list.
* `GET /api/watchlist/alerts?days=30`: the alerts the recorder raised, newest first.

The writes are refused for any Origin but the web app's by `server/local_only.py`, like every write.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Query, Response

from server.schemas import WatchAddIn, WatchAlertDTO, WatchlistDTO, WatchRowDTO
from stockify import timeutil
from stockify.config import loader
from stockify.data import history, instruments
from stockify.watchlist import ranges, store

log = logging.getLogger("stockify.watchlist")

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

NO_CANDLES = "Daily prices arrive after tonight's fetch"
SHORT_HISTORY = "Less than a year of daily prices, so no 52-week range yet"
NO_RECENT = "No daily prices in the last year"
_SYMBOL = re.compile(r"^[A-Za-z0-9&_. \-]{1,40}$")
_DAY_NS = 86_400 * 10**9


def now_ns() -> int:
    return timeutil.now_ns()


def _row(r: dict, today, cfg: dict) -> WatchRowDTO:
    iid = r["instrument_id"]
    bars = history.bars(iid, "day")
    # The range is judged as of today, like the live evaluator's (365 days before today), so intraday the page agrees
    # with the alerts. Once the evening fetch has put today's candle in, it is folded in too (as of tomorrow).
    last = ranges.last_close(bars, today)
    day = today + timedelta(days=1) if last and last[1] == today.isoformat() else today
    rng = ranges.prior_range(bars, day, cfg["lookback_days"], cfg["min_history_days"])
    dto = {"symbol": r["symbol"], "instrument_id": iid, "added_ns": r["added_ns"],
           "token": instruments.token_at(iid, today)}
    if last:
        dto.update(last_close_paise=last[0], last_close_date=last[1])
    if rng:
        dto.update(high_paise=rng["high_paise"], high_date=rng["high_date"], low_paise=rng["low_paise"],
                   low_date=rng["low_date"])
    else:
        stale = last is not None and last[1] < (today - timedelta(days=cfg["lookback_days"])).isoformat()
        dto["note"] = NO_CANDLES if not bars else NO_RECENT if stale else SHORT_HISTORY
    return WatchRowDTO(**dto)


def _list() -> WatchlistDTO:
    cfg = loader.load_json("watchlist.json")
    today = timeutil.ns_to_ist(now_ns()).date()
    return WatchlistDTO(items=[_row(r, today, cfg) for r in store.rows()], max_stocks=cfg["max_stocks"])


@router.get("", response_model=WatchlistDTO)
def get_watchlist() -> WatchlistDTO:
    return _list()


@router.post("", response_model=WatchRowDTO, status_code=201)
def add_stock(body: WatchAddIn) -> WatchRowDTO:
    try:
        row = store.add(body.symbol, now_ns=now_ns())
    except store.NotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except store.NotAStock as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except (store.AlreadyWatched, store.ListFull) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    cfg = loader.load_json("watchlist.json")
    return _row(row, timeutil.ns_to_ist(now_ns()).date(), cfg)


@router.get("/alerts", response_model=list[WatchAlertDTO])
def get_alerts(days: int = Query(30, ge=1, le=366)) -> list[WatchAlertDTO]:
    rows = store.alerts(now_ns() - days * _DAY_NS)
    return [WatchAlertDTO(**{k: r[k] for k in WatchAlertDTO.model_fields}) for r in rows]


@router.delete("/{symbol}", status_code=204)
def remove_stock(symbol: str) -> Response:
    if not _SYMBOL.match(symbol) or not store.remove(symbol):
        raise HTTPException(status_code=404, detail="that stock is not on the watchlist")
    return Response(status_code=204)

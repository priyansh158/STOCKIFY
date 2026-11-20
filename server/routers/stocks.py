"""CW 4: /api/stocks, a listed stock's price chart.

* `GET /api/stocks/{symbol}/chart`: about a year of the bar cache's daily candles as the RunChartDTO the price chart reads,
  with constant "52-week high" and "52-week low" lines (the watchlist's range, by the same rule). A stock with no
  daily candles yet is a 200 with `data_state` "no_data" and a plain notice. A symbol NSE does not list is a 404.

Nothing here calls Kite or NSE: the candles are the nightly fetch's. The chart is cached on the candles' key, as the
desk's is, so a refetch (a split, a bonus, a new day) builds a fresh one.
"""
from __future__ import annotations

import re
from datetime import timedelta

from fastapi import APIRouter, HTTPException

from server.schemas import RunChartDTO
from server.services import charts
from stockify import timeutil
from stockify.config import loader
from stockify.data import instruments

router = APIRouter(prefix="/api/stocks", tags=["stocks"])

NO_DATA = "No daily prices yet: they are fetched each evening for the screener's stocks and your watchlist."
SHORT_HISTORY = "Less than a year of daily prices, so no 52-week lines yet"
DAY_S = 86_400
_CACHE = "stock"
_SYMBOL = re.compile(r"^[A-Za-z0-9&_.\-]{1,40}$")


def now_ns() -> int:
    return timeutil.now_ns()


def reset() -> None:
    charts.reset(_CACHE)


def _build(iid: int, sym: str, candles: list, rows: list[dict], today, cfg: dict) -> RunChartDTO:
    if not candles:
        return charts.dto(charts.model(iid, sym, DAY_S, "adjusted", [], [], data_state="no_data", notice=NO_DATA), {})
    first = timeutil.ist_ns(today - timedelta(days=cfg["lookback_days"]), "00:00")
    shown = [c for c in candles if c.start_ns >= first] or candles[-1:]
    _last, rng = charts.stock_range(rows, today, cfg)
    plots, labels = [], {}
    if rng:
        for name, label, key in (("high52", "52-week high", "high_paise"), ("low52", "52-week low", "low_paise")):
            plots.append({"name": name, "kind": "price", "instrument_id": iid,
                          "points": [[c.start_ns, rng[key]] for c in shown]})
            labels[name] = label
    m = charts.model(iid, sym, DAY_S, "adjusted", shown, plots, notice=None if rng else SHORT_HISTORY)
    return charts.dto(m, labels)


@router.get("/{symbol}/chart", response_model=RunChartDTO)
def stock_chart(symbol: str) -> RunChartDTO:
    iid = instruments.find("NSE", symbol) if _SYMBOL.match(symbol) else None
    if iid is None:
        raise HTTPException(status_code=404, detail="no such NSE stock")
    sym = symbol.upper()
    today = charts.today_ist(now_ns())
    rows = charts.stock_rows(iid)
    candles = charts.daily(rows)
    key = charts.bars_key(candles) + (today.isoformat(),)       # the range moves with the day as well as the candles
    cfg = loader.load_json("watchlist.json")
    return charts.cached(_CACHE, iid, key, lambda: _build(iid, sym, candles, rows, today, cfg))

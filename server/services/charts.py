"""CW 4: the daily price chart builder the desk and the stock chart share.

`model` makes the chart model of a stock's daily candles (the shape `server.routers.tests._chart_dto` reads), `dto`
turns it into the RunChartDTO with each plot's plain label, and `cached` keeps one finished chart per (namespace,
instrument) until the bar cache's candles change: `bars_key` is the key, so a split or bonus refetch (which rewrites
the older candles) or one new daily candle builds a fresh chart. Nothing here reads the lab or calls Kite.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import date
from typing import NamedTuple

from server.schemas import RunChartDTO
from stockify import timeutil
from stockify.data import history
from stockify.watchlist import ranges

_lock = threading.Lock()
_cache: dict[tuple[str, int], tuple[tuple, RunChartDTO]] = {}     # (namespace, iid) -> (key, chart)


def reset(namespace: str | None = None) -> None:
    """Forget the cached charts of one namespace, or all of them."""
    with _lock:
        for k in [k for k in _cache if namespace is None or k[0] == namespace]:
            del _cache[k]


def bars_key(bars: list) -> tuple:
    """The cache key of daily bars: how many, the last candle's day and close, and the first close (a split or bonus
    refetch rewrites the older, adjusted candles)."""
    return (len(bars), bars[-1].start_ns, bars[-1].close, bars[0].close) if bars else (0,)


def cached(namespace: str, iid: int, key: tuple, build: Callable[[], RunChartDTO]) -> RunChartDTO:
    """The chart built for `key`, built again by `build()` only when the key has changed."""
    with _lock:
        hit = _cache.get((namespace, iid))
    if hit is not None and hit[0] == key:
        return hit[1]
    dto = build()
    with _lock:
        _cache[(namespace, iid)] = (key, dto)
    return dto


def model(iid: int, sym: str, interval_s: int, basis: str, bars: list, plots: list[dict], *,
          data_state: str = "ok", notice: str | None = None) -> dict:
    return {"instruments": [{"id": iid, "symbol": sym, "kind": "equity"}], "instrument_id": iid, "basis": basis,
            "interval_s": interval_s,
            "bars": [{"start_ns": b.start_ns, "open": b.open, "high": b.high, "low": b.low, "close": b.close,
                      "volume": b.volume} for b in bars],
            "markers": [], "omitted_markers": 0, "plots": plots, "truncated": False, "next_from_ns": None,
            "data_state": data_state, "notice": notice, "pnl_complete": True, "skipped_settlements": 0,
            "idle_settlements": 0}


def dto(m: dict, labels: dict[str, str]) -> RunChartDTO:
    from server.routers.tests import _chart_dto
    out = _chart_dto(m)
    plots = [p.model_copy(update={"label": labels.get(p.name, p.label)}) for p in out.plots]
    return out.model_copy(update={"plots": plots})


# ── a stock's own chart: the bar cache's daily candles and its 52-week range ─────────────────────────────

class Candle(NamedTuple):
    """One stored daily candle, in paise, as `model` reads a bar."""
    start_ns: int
    open: int
    high: int
    low: int
    close: int
    volume: int


def daily(rows: list[dict]) -> list[Candle]:
    return [Candle(int(r["ts_ns"]), int(r["open_paise"]), int(r["high_paise"]), int(r["low_paise"]),
                   int(r["close_paise"]), int(r["volume"] or 0)) for r in rows]


def stock_range(rows: list[dict], today: date, cfg: dict) -> tuple[tuple[int, str] | None, dict | None]:
    """(last close, 52-week range) of a stock from `history.bars` rows, by the watchlist's rule (ranges.stock_range)."""
    return ranges.stock_range(rows, today, cfg["lookback_days"], cfg["min_history_days"])


def stock_rows(iid: int) -> list[dict]:
    return history.bars(iid, "day")


def today_ist(now_ns: int) -> date:
    return timeutil.ns_to_ist(now_ns).date()

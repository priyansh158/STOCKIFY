"""CW 2: the 52-week range of a stock from its daily candles. Pure: it takes the bars and reads nothing.

The bars are the rows `stockify.data.history.bars(iid, "day")` returns (adjusted basis): dicts with `ts_ns` (the IST
midnight that starts the day), `high_paise`, `low_paise` and `close_paise`. The API, the live evaluator (phase 3) and
the price context (phase 4) all read the range through `prior_range`, so they agree.
"""
from __future__ import annotations

from datetime import date, timedelta

from stockify import timeutil


def _day(bar: dict) -> date:
    return timeutil.ns_to_ist(int(bar["ts_ns"])).date()


def prior_range(bars: list[dict], day: date, lookback_days: int, min_history_days: int) -> dict | None:
    """The range a new high or low on `day` is judged against: the highest high and the lowest low of the bars dated
    from `day - lookback_days` (inclusive) up to the day before `day`, with the dates they were made (the latest date
    when two bars tie) and the last close before `day`.

    Returns {high_paise, high_date, low_paise, low_date, last_close_paise, last_close_date} (dates as ISO text), or None
    when the history is too short to judge: no bars in the window, or the first bar is newer than
    `day - min_history_days` (a stock listed too recently has no real 52-week range yet)."""
    dated = sorted(((_day(b), b) for b in bars), key=lambda p: p[0])
    if not dated or dated[0][0] > day - timedelta(days=min_history_days):
        return None
    start = day - timedelta(days=lookback_days)
    window = [(d, b) for d, b in dated if start <= d < day]
    if not window:
        return None
    high_d, high_b = max(window, key=lambda p: (int(p[1]["high_paise"]), p[0]))
    low_d, low_b = max(window, key=lambda p: (-int(p[1]["low_paise"]), p[0]))
    last_d, last_b = window[-1]
    return {"high_paise": int(high_b["high_paise"]), "high_date": high_d.isoformat(),
            "low_paise": int(low_b["low_paise"]), "low_date": low_d.isoformat(),
            "last_close_paise": int(last_b["close_paise"]), "last_close_date": last_d.isoformat()}


def stock_range(bars: list[dict], today: date, lookback_days: int, min_history_days: int) -> tuple[tuple[int, str] | None, dict | None]:
    """(last close, 52-week range) of a stock as of `today`, the one rule the watchlist page, the stock chart and the
    price context share: the range is judged as of today (like the live evaluator's, `lookback_days` before today), and
    once today's own candle is in, it is folded in too (judged as of tomorrow). Either part is None when it is unknown."""
    last = last_close(bars, today)
    day = today + timedelta(days=1) if last and last[1] == today.isoformat() else today
    return last, prior_range(bars, day, lookback_days, min_history_days)


def last_close(bars: list[dict], day: date) -> tuple[int, str] | None:
    """(close in paise, ISO date) of the latest bar dated on or before `day`, or None."""
    best = None
    for b in bars:
        d = _day(b)
        if d <= day and (best is None or d > best[0]):
            best = (d, int(b["close_paise"]))
    return None if best is None else (best[1], best[0].isoformat())

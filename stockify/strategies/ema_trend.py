"""EMA Trend (HD 2.1): holds the stock while the fast EMA is above the slow EMA; the exit level is the slow EMA."""
from __future__ import annotations

from stockify.strategies._swing import SwingLong, ema_series, int_param, swing_params
from stockify.strategy import Bar, fmt_price


class EmaTrend(SwingLong):
    DISPLAY_NAME = "EMA Trend"
    DESCRIPTION = "Holds the stock while the fast exponential average is above the slow one."
    PARAMS = swing_params(
        int_param("fast", "Fast average (bars)", 20, "Bars in the fast average."),
        int_param("slow", "Slow average (bars)", 50, "Bars in the slow average."))
    LOOKBACK = 250

    def decide(self, bars: list[Bar]) -> tuple[bool, float | None, str]:
        closes = [b.close for b in bars]
        fast, slow = ema_series(closes, int(self.params.get("fast", 20))), ema_series(closes, int(self.params.get("slow", 50)))
        if fast[-1] != fast[-1] or slow[-1] != slow[-1]:
            return False, None, "Not enough history for the averages yet."
        up = fast[-1] > slow[-1]
        return up, slow[-1], f"the fast average {fmt_price(int(fast[-1]))} is {'above' if up else 'not above'} the slow average {fmt_price(int(slow[-1]))}"

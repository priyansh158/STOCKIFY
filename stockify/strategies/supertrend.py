"""Supertrend (HD 2.1): holds the stock while the Supertrend is up; the exit level is the Supertrend line."""
from __future__ import annotations

from stockify.strategies._swing import SwingLong, decimal_param, int_param, supertrend, swing_params
from stockify.strategy import Bar, fmt_price


class Supertrend(SwingLong):
    DISPLAY_NAME = "Supertrend"
    DESCRIPTION = "Holds the stock while the Supertrend line sits below the price and sells when the price closes under it."
    PARAMS = swing_params(
        int_param("n", "Range length (bars)", 10, "Bars in the average true range."),
        decimal_param("mult", "Range multiple", "3.0", "How many ranges the line sits from the middle of each bar."))
    LOOKBACK = 250

    def decide(self, bars: list[Bar]) -> tuple[bool, float | None, str]:
        line, up = supertrend([b.high for b in bars], [b.low for b in bars], [b.close for b in bars],
                              int(self.params.get("n", 10)), float(str(self.params.get("mult", "3.0"))))
        if line[-1] != line[-1]:
            return False, None, "Not enough history for the Supertrend yet."
        where = "below" if up[-1] else "above"
        return up[-1], line[-1], f"the Supertrend is {'up' if up[-1] else 'down'}, its line {fmt_price(int(line[-1]))} {where} the price"

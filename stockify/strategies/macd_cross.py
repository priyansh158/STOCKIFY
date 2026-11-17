"""MACD Crossover (HD 2.1): holds the stock while the MACD line is above its signal line."""
from __future__ import annotations

from stockify.strategies._swing import SwingLong, int_param, macd, swing_params
from stockify.strategy import Bar, fmt_price


class MacdCross(SwingLong):
    DISPLAY_NAME = "MACD Crossover"
    DESCRIPTION = "Holds the stock while the MACD line is above its signal line."
    PARAMS = swing_params(
        int_param("fast", "Fast average (bars)", 12, "Bars in the fast average."),
        int_param("slow", "Slow average (bars)", 26, "Bars in the slow average."),
        int_param("signal", "Signal average (bars)", 9, "Bars in the average of the MACD line."))
    LOOKBACK = 250

    def decide(self, bars: list[Bar]) -> tuple[bool, float | None, str]:
        line, sig = macd([b.close for b in bars], int(self.params.get("fast", 12)), int(self.params.get("slow", 26)), int(self.params.get("signal", 9)))
        if sig[-1] != sig[-1]:
            return False, None, "Not enough history for the MACD yet."
        gap = line[-1] - sig[-1]
        side = "above" if gap > 0 else "below"
        return gap > 0, None, f"the MACD line is {fmt_price(int(abs(gap)))} {side} its signal line"

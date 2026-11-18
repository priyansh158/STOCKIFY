"""Chandelier Exit (HD 2.1): buys a close above the previous `entry_n` highs, sells a close below the chandelier line.

Held or not is rebuilt by scanning the bars passed in. The exit level is the chandelier line.
"""
from __future__ import annotations

from stockify.strategies._swing import SwingLong, chandelier, decimal_param, donchian, int_param, swing_params
from stockify.strategy import Bar, fmt_price


class ChandelierExit(SwingLong):
    DISPLAY_NAME = "Chandelier Exit"
    DESCRIPTION = "Buys when the price closes above its recent high and sells when it closes below a line hung under the highest high."
    PARAMS = swing_params(
        int_param("n", "Line length (bars)", 22, "Bars for the highest high and the average true range."),
        decimal_param("mult", "Range multiple", "3.0", "How many ranges the line hangs under the highest high."),
        int_param("entry_n", "Entry high (bars)", 20, "Buy on a close above the highest high of this many earlier bars."))
    LOOKBACK = 250

    def decide(self, bars: list[Bar]) -> tuple[bool, float | None, str]:
        closes, highs, lows = [b.close for b in bars], [b.high for b in bars], [b.low for b in bars]
        hh, _ = donchian(highs, lows, int(self.params.get("entry_n", 20)))
        line = chandelier(highs, lows, closes, int(self.params.get("n", 22)), float(str(self.params.get("mult", "3.0"))))
        long = False
        for i, c in enumerate(closes):
            if not long and hh[i] == hh[i] and line[i] == line[i] and c > hh[i]:
                long = True
            elif long and line[i] == line[i] and c < line[i]:
                long = False
        if line[-1] != line[-1] or hh[-1] != hh[-1]:
            return False, None, "Not enough history for the chandelier line yet."
        if long:
            note = f"the close {fmt_price(closes[-1])} is above the chandelier line {fmt_price(int(line[-1]))}"
        else:
            note = f"the close {fmt_price(closes[-1])} has not broken above the recent high {fmt_price(int(hh[-1]))}, or fell under the chandelier line"
        return long, line[-1], note

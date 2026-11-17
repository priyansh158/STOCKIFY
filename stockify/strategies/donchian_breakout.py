"""Donchian Breakout (HD 2.1): buys a close above the previous `entry_n` highs, sells a close below the previous `exit_n` lows.

Held or not is rebuilt by scanning the bars passed in. The exit level is the `exit_n` low.
"""
from __future__ import annotations

from stockify.strategies._swing import SwingLong, donchian, int_param, swing_params
from stockify.strategy import Bar, fmt_price


class DonchianBreakout(SwingLong):
    DISPLAY_NAME = "Donchian Breakout"
    DESCRIPTION = "Buys when the price closes above its recent high and sells when it closes below its recent low."
    PARAMS = swing_params(
        int_param("entry_n", "Entry high (bars)", 20, "Buy on a close above the highest high of this many earlier bars."),
        int_param("exit_n", "Exit low (bars)", 10, "Sell on a close below the lowest low of this many earlier bars."))
    LOOKBACK = 250

    def decide(self, bars: list[Bar]) -> tuple[bool, float | None, str]:
        closes, highs, lows = [b.close for b in bars], [b.high for b in bars], [b.low for b in bars]
        hh, _ = donchian(highs, lows, int(self.params.get("entry_n", 20)))
        _, ll = donchian(highs, lows, int(self.params.get("exit_n", 10)))
        long = False
        for i, c in enumerate(closes):
            if not long and hh[i] == hh[i] and c > hh[i]:
                long = True
            elif long and ll[i] == ll[i] and c < ll[i]:
                long = False
        if hh[-1] != hh[-1]:
            return False, None, "Not enough history for the breakout levels yet."
        level = ll[-1] if ll[-1] == ll[-1] else None
        if long:
            note = f"the close {fmt_price(closes[-1])} broke above the recent high and has not closed below the exit low"
        else:
            note = f"the close {fmt_price(closes[-1])} is not above the recent high {fmt_price(int(hh[-1]))}, or closed below the exit low"
        return long, level, note

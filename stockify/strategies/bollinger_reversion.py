"""Bollinger Reversion (HD 2.1): buys a close below the lower band and sells once the close is back at the middle.

Held or not is rebuilt by scanning the bars passed in. The exit level is the middle band.
"""
from __future__ import annotations

from stockify.strategies._swing import SwingLong, bollinger, decimal_param, int_param, swing_params
from stockify.strategy import Bar, fmt_price


class BollingerReversion(SwingLong):
    DISPLAY_NAME = "Bollinger Reversion"
    DESCRIPTION = "Buys when the price closes below its lower Bollinger band and sells when it is back at the middle band."
    PARAMS = swing_params(
        int_param("n", "Average length (bars)", 20, "Bars in the middle band."),
        decimal_param("k", "Band width (deviations)", "2.0", "How many standard deviations the outer bands sit from the middle."))
    LOOKBACK = 250

    def decide(self, bars: list[Bar]) -> tuple[bool, float | None, str]:
        closes = [b.close for b in bars]
        lower, mid, _ = bollinger(closes, int(self.params.get("n", 20)), float(str(self.params.get("k", "2.0"))))
        long = False
        for i, c in enumerate(closes):
            if mid[i] != mid[i]:
                continue
            if not long and c < lower[i]:
                long = True
            elif long and c >= mid[i]:
                long = False
        if mid[-1] != mid[-1]:
            return False, None, "Not enough history for the bands yet."
        if long:
            note = f"the close {fmt_price(closes[-1])} is under the middle band {fmt_price(int(mid[-1]))} after dipping below the lower band"
        else:
            note = f"the close {fmt_price(closes[-1])} is not below the lower band {fmt_price(int(lower[-1]))}, or is back at the middle"
        return long, mid[-1], note

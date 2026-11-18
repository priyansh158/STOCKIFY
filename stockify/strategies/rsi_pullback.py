"""RSI Pullback (HD 2.1): buys when the RSI climbs back above `buy_below`, sells when it falls back below `sell_above`.

The state (held or not) is rebuilt from the bars passed in, never kept on the object, so a replay of the same bars
always gives the same answer. The exit level is the price at which the next close would take the RSI to `sell_above`.
"""
from __future__ import annotations

import math

from stockify.strategies._swing import SwingLong, int_param, swing_params
from stockify.strategy import Bar, fmt_price


def rsi_state(closes: list[int], n: int) -> tuple[list[float], float, float]:
    """(RSI per bar, Wilder average gain, average loss on the last bar); NaN until n changes exist."""
    out = [math.nan] * len(closes)
    if n < 1 or len(closes) < n + 1:
        return out, math.nan, math.nan
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    ag = sum(x for x in changes[:n] if x > 0) / n
    al = sum(-x for x in changes[:n] if x < 0) / n
    out[n] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    for i in range(n + 1, len(closes)):
        x = changes[i - 1]
        ag = (ag * (n - 1) + (x if x > 0 else 0)) / n
        al = (al * (n - 1) + (-x if x < 0 else 0)) / n
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return out, ag, al


def price_for_rsi(last: int, ag: float, al: float, n: int, target: float) -> float | None:
    """The next close at which the RSI equals `target`, or None when no price does."""
    if not 0 < target < 100 or ag != ag or al != al or ag <= 0 and al <= 0:
        return None
    rs = target / (100.0 - target)
    up = (rs * al - ag) * (n - 1)                      # a rise of `up` paise lifts the RSI to the target
    if up > 0:
        price = last + up
    else:
        down = (ag / rs - al) * (n - 1)                # otherwise a fall of `down` paise lets it sink to the target
        price = last - down
    return price if price > 0 else None


class RsiPullback(SwingLong):
    DISPLAY_NAME = "RSI Pullback"
    DESCRIPTION = "Buys after the RSI climbs back above 30 and sells when it falls back below 70."
    PARAMS = swing_params(
        int_param("rsi_n", "RSI length (bars)", 14, "Bars in the RSI."),
        int_param("buy_below", "Buy after RSI climbs above", 30, "Buy when the RSI climbs back up through this level."),
        int_param("sell_above", "Sell when RSI falls below", 70, "Sell when the RSI falls back down through this level."))
    LOOKBACK = 250

    def decide(self, bars: list[Bar]) -> tuple[bool, float | None, str]:
        n, buy, sell = int(self.params.get("rsi_n", 14)), int(self.params.get("buy_below", 30)), int(self.params.get("sell_above", 70))
        closes = [b.close for b in bars]
        rsi, ag, al = rsi_state(closes, n)
        long = False
        for i in range(n + 1, len(rsi)):
            if not long and rsi[i - 1] < buy <= rsi[i]:
                long = True
            elif long and rsi[i - 1] >= sell > rsi[i]:
                long = False
        if rsi[-1] != rsi[-1]:
            return False, None, "Not enough history for the RSI yet."
        level = price_for_rsi(closes[-1], ag, al, n, float(sell))
        if long:
            note = f"the RSI is {rsi[-1]:.0f}, back above {buy}"
            if level is not None:
                note += f"; it would fall below {sell} at about {fmt_price(int(level))}"
        else:
            note = f"the RSI is {rsi[-1]:.0f}, no recovery above {buy} to hold, or it fell back below {sell}"
        return long, level, note

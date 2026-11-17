"""Swing indicators and the long-only base class the swing strategies share (HD 1.1).

Leading underscore: not a catalogue strategy. The functions are pure: plain lists of ints (paise) in, lists of
floats out, aligned 1:1 with the input, `math.nan` during warm-up. `SwingLong` is a long-only CNC helper: a
subclass declares `LOOKBACK` and implements `decide(bars)`; the base class sizes and places the orders and plots
the exit level (the holdings desk reads the `exit_level` plot).
"""
from __future__ import annotations

import math

from stockify.strategy import Bar, Strategy, fmt_qty

NAN = math.nan


def ema_series(values: list[int], n: int) -> list[float]:
    """Exponential average, alpha 2/(n+1), seeded with the simple average of the first n values."""
    out = [NAN] * len(values)
    if n < 1 or len(values) < n:
        return out
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    alpha = 2.0 / (n + 1)
    for i in range(n, len(values)):
        prev = prev + alpha * (values[i] - prev)
        out[i] = prev
    return out


def macd(closes: list[int], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[list[float], list[float]]:
    """(MACD line, signal line): fast EMA minus slow EMA, and an EMA of that line."""
    f, s = ema_series(closes, fast), ema_series(closes, slow)
    line = [a - b if a == a and b == b else NAN for a, b in zip(f, s)]
    sig = [NAN] * len(closes)
    first = next((i for i, v in enumerate(line) if v == v), None)
    if first is not None:
        tail = ema_series(line[first:], signal)
        sig[first:] = tail
    return line, sig


def bollinger(closes: list[int], n: int = 20, k: float = 2.0) -> tuple[list[float], list[float], list[float]]:
    """(lower, middle, upper): the n-bar average and k population standard deviations either side."""
    lower, mid, upper = [NAN] * len(closes), [NAN] * len(closes), [NAN] * len(closes)
    for i in range(n - 1, len(closes)):
        window = closes[i - n + 1:i + 1]
        m = sum(window) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in window) / n)
        lower[i], mid[i], upper[i] = m - k * sd, m, m + k * sd
    return lower, mid, upper


def donchian(highs: list[int], lows: list[int], n: int) -> tuple[list[float], list[float]]:
    """(highest high, lowest low) of the previous n bars, the current bar excluded."""
    hh, ll = [NAN] * len(highs), [NAN] * len(lows)
    for i in range(n, len(highs)):
        hh[i] = float(max(highs[i - n:i]))
        ll[i] = float(min(lows[i - n:i]))
    return hh, ll


def atr_series(highs: list[int], lows: list[int], closes: list[int], n: int) -> list[float]:
    """Wilder's average true range: the first value is the mean of the first n true ranges."""
    size = len(closes)
    out = [NAN] * size
    if n < 1 or size < n:
        return out
    tr = [float(highs[0] - lows[0])]
    for i in range(1, size):
        tr.append(float(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))))
    prev = sum(tr[:n]) / n
    out[n - 1] = prev
    for i in range(n, size):
        prev = (prev * (n - 1) + tr[i]) / n
        out[i] = prev
    return out


def supertrend(highs: list[int], lows: list[int], closes: list[int], n: int = 10,
               mult: float = 3.0) -> tuple[list[float], list[bool]]:
    """(line, is_up): the lower band while the trend is up, the upper band while it is down. Starts up."""
    size = len(closes)
    atr = atr_series(highs, lows, closes, n)
    line, up = [NAN] * size, [False] * size
    trend_up, fub, flb = True, NAN, NAN
    for i in range(size):
        if atr[i] != atr[i]:
            continue
        mid = (highs[i] + lows[i]) / 2
        ub, lb = mid + mult * atr[i], mid - mult * atr[i]
        if fub != fub:                                   # first valid bar
            fub, flb = ub, lb
        else:
            pfub, pflb, pc = fub, flb, closes[i - 1]
            fub = ub if ub < pfub or pc > pfub else pfub
            flb = lb if lb > pflb or pc < pflb else pflb
            if trend_up and closes[i] < pflb:
                trend_up = False
            elif not trend_up and closes[i] > pfub:
                trend_up = True
        up[i] = trend_up
        line[i] = flb if trend_up else fub
    return line, up


def chandelier(highs: list[int], lows: list[int], closes: list[int], n: int = 22, mult: float = 3.0) -> list[float]:
    """Highest high of the last n bars (current included) minus mult times ATR(n)."""
    atr = atr_series(highs, lows, closes, n)
    out = [NAN] * len(closes)
    for i in range(n - 1, len(closes)):
        if atr[i] == atr[i]:
            out[i] = max(highs[i - n + 1:i + 1]) - mult * atr[i]
    return out


class SwingLong(Strategy):
    """Long-only CNC helper. Subclasses declare `LOOKBACK` and implement `decide(bars)`, which returns
    (want_long, exit_level in paise or None, a plain note)."""

    LOOKBACK = 100

    def decide(self, bars: list[Bar]) -> tuple[bool, float | None, str]:
        raise NotImplementedError

    def on_start(self, ctx):  # noqa: ANN001
        self.symbol = ctx.params["symbol"]
        self.iid = ctx.resolve(self.symbol)
        self.isec = int(ctx.params.get("interval_s", 86400))
        ctx.subscribe_bars(self.iid, self.isec)

    def on_bar(self, ctx, bar):  # noqa: ANN001
        bars = ctx.bars(self.iid, self.isec, self.LOOKBACK)
        if not bars:
            return
        want_long, exit_level, note = self.decide(bars)
        if exit_level is not None and exit_level == exit_level:
            ctx.plot("exit_level", int(exit_level), kind="price", instrument=self.iid)
        pos = ctx.position(self.iid, "CNC")
        held = pos.qty if pos else 0
        if want_long and held <= 0:
            close = bar.close
            qty = (ctx.funds().available * 95 // 100) // (close * 103 // 100) if close > 0 else 0
            if qty > 0:
                ctx.place(self.iid, "BUY", qty, "CNC", "MARKET", tag="entry",
                          note=f"Bought {fmt_qty(qty)} {self.symbol}: {note}")
        elif not want_long and held > 0:
            ctx.place(self.iid, "SELL", held, "CNC", "MARKET", tag="exit",
                      note=f"Sold {fmt_qty(held)} {self.symbol}: {note}")

"""SMA crossover (M2.4): long when the fast SMA is above the slow one.

Params: `symbol`, `fast`, `slow`, `interval_s` (86400 daily or 60 for minute bars),
`long_short` (false: flat when fast < slow; true: short instead, which needs a
derivative traded NRML), `product` (CNC for equities, NRML for futures), `lots`
(derivatives: lots per position).
"""
from __future__ import annotations

from stockify.strategy import Strategy, fmt_price, fmt_qty


class SmaCross(Strategy):
    # The parameter schema for the catalogue and the /tests form (data only; on_start reads ctx.params).
    PARAMS = [
        {'name': 'symbol', 'label': 'Stock or future', 'type': 'symbol', 'default': None, 'required': True, 'help': 'what to trade, e.g. NSE:RELIANCE'},
        {'name': 'fast', 'label': 'Fast average (bars)', 'type': 'int', 'default': None, 'required': True, 'help': 'bars in the fast moving average'},
        {'name': 'slow', 'label': 'Slow average (bars)', 'type': 'int', 'default': None, 'required': True, 'help': 'bars in the slow moving average'},
        {'name': 'interval_s', 'label': 'Bar length (seconds)', 'type': 'int', 'default': 86400, 'required': False, 'help': 'bar length in seconds: 86400 daily, 60 minute'},
        {'name': 'long_short', 'label': 'Also go short', 'type': 'bool', 'default': False, 'required': False, 'help': 'go short when fast < slow (needs a future traded NRML)'},
        {'name': 'product', 'label': 'Product (CNC or NRML)', 'type': 'str', 'default': 'CNC', 'required': False, 'help': 'CNC for stocks, NRML for futures'},
        {'name': 'lots', 'label': 'Lots per position', 'type': 'int', 'default': 1, 'required': False, 'help': 'futures only: lots per position'},
    ]

    def on_start(self, ctx):  # noqa: ANN001
        p = ctx.params
        self.symbol = p["symbol"]
        self.iid = ctx.resolve(self.symbol)
        self.isec = int(p.get("interval_s", 86400))
        self.fast, self.slow = int(p["fast"]), int(p["slow"])
        self.long_short = bool(p.get("long_short", False))
        self.product = p.get("product", "CNC")
        if self.long_short and self.product == "CNC":
            raise ValueError("long_short needs a derivative traded NRML (no CNC short selling)")
        ctx.subscribe_bars(self.iid, self.isec)

    def _units(self, ctx, close: int) -> int:  # noqa: ANN001
        if self.product == "CNC":
            return (ctx.funds().available * 95 // 100) // (close * 103 // 100)
        return int(ctx.params.get("lots", 1)) * ctx.spec(self.iid)["lot"]

    def on_bar(self, ctx, bar):  # noqa: ANN001
        fast = ctx.sma(self.iid, self.isec, self.fast)
        slow = ctx.sma(self.iid, self.isec, self.slow)
        if fast != fast or slow != slow:                        # NaN during warm-up
            return
        ctx.plot("fast", int(fast), instrument=self.iid)       # the averages are positive paise: int() truncates
        ctx.plot("slow", int(slow), instrument=self.iid)
        pos = ctx.position(self.iid, self.product)
        held = pos.qty if pos else 0
        if fast > slow:
            want = self._units(ctx, bar.close) if held <= 0 else held
        elif self.long_short:
            want = -self._units(ctx, bar.close) if held >= 0 else held
        else:
            want = 0
        delta = want - held
        if delta == 0:
            return
        qty = fmt_qty(abs(delta))
        move = "rose above" if fast > slow else "fell below" if fast < slow else "is level with"
        why = (f"the {self.fast}-bar average {fmt_price(int(fast))} {move} "
               f"the {self.slow}-bar average {fmt_price(int(slow))}")
        if delta > 0:
            lead = f"Bought {qty} {self.symbol}" if held >= 0 else f"Flipped to long {qty} {self.symbol}"
            ctx.place(self.iid, "BUY", delta, self.product, "MARKET", tag="entry" if held >= 0 else "flip",
                      note=f"{lead}: {why}")
        else:
            lead = (f"Sold {qty} {self.symbol}" if want >= 0 else f"Flipped to short {qty} {self.symbol}"
                    if held > 0 else f"Sold short {qty} {self.symbol}")
            ctx.place(self.iid, "SELL", -delta, self.product, "MARKET", tag="exit" if want >= 0 else "flip",
                      note=f"{lead}: {why}")

"""SMA crossover (M2.4): long when the fast SMA is above the slow one.

Params: `symbol`, `fast`, `slow`, `interval_s` (86400 daily or 60 for minute bars),
`long_short` (false: flat when fast < slow; true: short instead, which needs a
derivative traded NRML), `product` (CNC for equities, NRML for futures), `lots`
(derivatives: lots per position).
"""
from __future__ import annotations

from stockify.strategy import Strategy


class SmaCross(Strategy):
    # The parameter schema for the catalogue and the /tests form (data only; on_start reads ctx.params).
    PARAMS = [
        {'name': 'symbol', 'type': 'symbol', 'default': None, 'required': True, 'help': 'what to trade, e.g. NSE:RELIANCE'},
        {'name': 'fast', 'type': 'int', 'default': None, 'required': True, 'help': 'bars in the fast moving average'},
        {'name': 'slow', 'type': 'int', 'default': None, 'required': True, 'help': 'bars in the slow moving average'},
        {'name': 'interval_s', 'type': 'int', 'default': 86400, 'required': False, 'help': 'bar length in seconds: 86400 daily, 60 minute'},
        {'name': 'long_short', 'type': 'bool', 'default': False, 'required': False, 'help': 'go short when fast < slow (needs a future traded NRML)'},
        {'name': 'product', 'type': 'str', 'default': 'CNC', 'required': False, 'help': 'CNC for stocks, NRML for futures'},
        {'name': 'lots', 'type': 'int', 'default': 1, 'required': False, 'help': 'futures only: lots per position'},
    ]

    def on_start(self, ctx):  # noqa: ANN001
        p = ctx.params
        self.iid = ctx.resolve(p["symbol"])
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
        pos = ctx.position(self.iid, self.product)
        held = pos.qty if pos else 0
        if fast > slow:
            want = self._units(ctx, bar.close) if held <= 0 else held
        elif self.long_short:
            want = -self._units(ctx, bar.close) if held >= 0 else held
        else:
            want = 0
        delta = want - held
        if delta > 0:
            ctx.place(self.iid, "BUY", delta, self.product, "MARKET", tag="entry" if held >= 0 else "flip")
        elif delta < 0:
            ctx.place(self.iid, "SELL", -delta, self.product, "MARKET", tag="exit" if want >= 0 else "flip")

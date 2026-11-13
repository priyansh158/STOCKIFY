"""Buy and hold: the benchmark for every equity test (M2.4).

Params: `symbol` ("NSE:RELIANCE"), `interval_s` (default 86400), `invest` (fraction of
available funds, default "0.95" as a decimal string).
"""
from __future__ import annotations

from decimal import Decimal

from stockify.strategy import Strategy


class BuyAndHold(Strategy):
    def on_start(self, ctx):  # noqa: ANN001
        self.iid = ctx.resolve(ctx.params["symbol"])
        self.isec = int(ctx.params.get("interval_s", 86400))
        self.share = Decimal(str(ctx.params.get("invest", "0.95")))
        self.done = False
        ctx.subscribe_bars(self.iid, self.isec)

    def on_bar(self, ctx, bar):  # noqa: ANN001
        if self.done:
            return
        budget = int(Decimal(ctx.funds().available) * self.share)
        qty = budget // (bar.close * 103 // 100)               # headroom for the 2 % protection band
        if qty > 0:
            ctx.place(self.iid, "BUY", qty, "CNC", "MARKET", tag="entry")
            self.done = True

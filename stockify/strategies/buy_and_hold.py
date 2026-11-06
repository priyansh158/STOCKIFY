"""Buy and hold: the benchmark for every equity test (M2.4).

Params: `symbol` ("NSE:RELIANCE"), `interval_s` (default 86400), `invest` (fraction of
available funds, default "0.95" as a decimal string).
"""
from __future__ import annotations

from decimal import Decimal

from stockify.strategy import Strategy


class BuyAndHold(Strategy):
    # The parameter schema for the catalogue and the /tests form (data only; on_start reads ctx.params).
    PARAMS = [
        {'name': 'symbol', 'type': 'symbol', 'default': None, 'required': True, 'help': 'the stock to buy, e.g. NSE:RELIANCE'},
        {'name': 'interval_s', 'type': 'int', 'default': 86400, 'required': False, 'help': 'bar length in seconds: 86400 daily, 60 minute'},
        {'name': 'invest', 'type': 'decimal', 'default': '0.95', 'required': False, 'help': 'share of available cash to invest, e.g. 0.95'},
    ]

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

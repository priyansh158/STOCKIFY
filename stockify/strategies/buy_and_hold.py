"""Buy and hold: the benchmark for every equity test (M2.4).

Params: `symbol` ("NSE:RELIANCE"), `interval_s` (default 86400), `invest` (fraction of
available funds, default "0.95" as a decimal string).
"""
from __future__ import annotations

from decimal import Decimal

from stockify.strategy import Strategy, fmt_qty


class BuyAndHold(Strategy):
    DISPLAY_NAME = "Buy and Hold"   # what the UI shows (stockify.display); the catalogue test fails without one
    DESCRIPTION = 'Buys once with most of the cash on the first day and holds. The benchmark to compare every other strategy against.'   # one plain sentence for the New test form (the module docstring is for developers)
    # The parameter schema for the catalogue and the /tests form (data only; on_start reads ctx.params).
    PARAMS = [
        {'name': 'symbol', 'label': 'Stock', 'type': 'symbol', 'default': None, 'required': True, 'help': 'The stock to buy, e.g. NSE:RELIANCE.'},
        {'name': 'interval_s', 'label': 'Bar length', 'type': 'int', 'default': 86400, 'required': False, 'help': 'How long each price bar covers.', 'choices': [{'value': 60, 'label': '1 minute'}, {'value': 180, 'label': '3 minutes'}, {'value': 300, 'label': '5 minutes'}, {'value': 600, 'label': '10 minutes'}, {'value': 900, 'label': '15 minutes'}, {'value': 1800, 'label': '30 minutes'}, {'value': 3600, 'label': '1 hour'}, {'value': 86400, 'label': '1 day'}]},
        {'name': 'invest', 'label': 'Share of cash to invest (%)', 'type': 'decimal', 'default': '0.95', 'required': False, 'help': 'The share of available cash to invest, e.g. 95.', 'percent': True},
    ]

    def on_start(self, ctx):  # noqa: ANN001
        self.symbol = ctx.params["symbol"]
        self.iid = ctx.resolve(self.symbol)
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
            ctx.place(self.iid, "BUY", qty, "CNC", "MARKET", tag="entry",
                      note=f"Bought {fmt_qty(qty)} {self.symbol} on the first bar and will hold")
            self.done = True

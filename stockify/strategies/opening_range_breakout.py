"""Opening-range breakout on minute bars, MIS (M2.4).

The range is the high and low of the first `range_minutes` minute bars after
09:15. A later bar closing above the high buys (below the low sells short, if
`short` is true) with a protected market order and a stop at the other side of
the range. The position is closed at `exit_hhmm` (default 15:10), or by the MIS
square-off at the cut-off. At most one trade per day.

Params: `symbol`, `range_minutes` (15), `qty` (units), `short` (false), `exit_hhmm` ("15:10").
"""
from __future__ import annotations

from stockify import timeutil
from stockify.strategy import Strategy


class OpeningRangeBreakout(Strategy):
    # The parameter schema for the catalogue and the /tests form (data only; on_start reads ctx.params).
    PARAMS = [
        {'name': 'symbol', 'label': 'Stock', 'type': 'symbol', 'default': None, 'required': True, 'help': 'the stock to trade intraday, e.g. NSE:RELIANCE'},
        {'name': 'qty', 'label': 'Shares per trade', 'type': 'int', 'default': None, 'required': True, 'help': 'shares per trade'},
        {'name': 'range_minutes', 'label': 'Opening range (minutes)', 'type': 'int', 'default': 15, 'required': False, 'help': 'minutes after 09:15 that set the range'},
        {'name': 'short', 'label': 'Also sell short', 'type': 'bool', 'default': False, 'required': False, 'help': 'also sell short when price breaks below the range'},
        {'name': 'exit_hhmm', 'label': 'Exit time (IST)', 'type': 'hhmm', 'default': '15:10', 'required': False, 'help': 'time to close the position, IST'},
    ]

    def on_start(self, ctx):  # noqa: ANN001
        p = ctx.params
        self.iid = ctx.resolve(p["symbol"])
        self.minutes = int(p.get("range_minutes", 15))
        self.qty = int(p["qty"])
        self.short = bool(p.get("short", False))
        self.exit_hhmm = p.get("exit_hhmm", "15:10")
        self.day = None
        ctx.subscribe_bars(self.iid, 60)

    def _new_day(self, ctx, bar) -> None:  # noqa: ANN001
        self.day = ctx.today()
        self.open_ns = timeutil.ist_ns(self.day, "09:15")
        self.hi = self.lo = None
        self.traded = False
        self.stop_id = None
        exit_ns = timeutil.ist_ns(self.day, self.exit_hhmm)
        if exit_ns > ctx.now():
            ctx.schedule_at(exit_ns)

    def on_bar(self, ctx, bar):  # noqa: ANN001
        if ctx.today() != self.day:
            self._new_day(ctx, bar)
        if bar.start_ns < self.open_ns:
            return
        if bar.start_ns < self.open_ns + self.minutes * 60 * 1_000_000_000:
            self.hi = bar.high if self.hi is None else max(self.hi, bar.high)
            self.lo = bar.low if self.lo is None else min(self.lo, bar.low)
            return
        if self.traded or self.hi is None:
            return
        tick = ctx.spec(self.iid)["tick_paise"]
        if bar.close > self.hi:
            ctx.place(self.iid, "BUY", self.qty, "MIS", "MARKET", tag="orb_long")
            self.stop_id = ctx.place(self.iid, "SELL", self.qty, "MIS", "SL-M", trigger=self.lo // tick * tick,
                                     tag="orb_stop")
            self.traded = True
        elif self.short and bar.close < self.lo:
            ctx.place(self.iid, "SELL", self.qty, "MIS", "MARKET", tag="orb_short")
            self.stop_id = ctx.place(self.iid, "BUY", self.qty, "MIS", "SL-M", trigger=-(-self.hi // tick) * tick,
                                     tag="orb_stop")
            self.traded = True

    def on_timer(self, ctx, timer_id):  # noqa: ANN001
        if self.stop_id is not None:
            ctx.cancel(self.stop_id)
        pos = ctx.position(self.iid, "MIS")
        if pos and pos.qty:
            side = "SELL" if pos.qty > 0 else "BUY"
            ctx.place(self.iid, side, abs(pos.qty), "MIS", "MARKET", tag="orb_exit")

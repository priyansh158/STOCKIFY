"""Opening-range breakout on minute bars, MIS (M2.4).

The range is the high and low of the first `range_minutes` minute bars after
09:15. A later bar closing above the high buys (below the low sells short, if
`short` is true) with a protected market order; each fill of that order gets a stop (SL-M) of exactly its
quantity at the other side of the range, so a rejected or cancelled entry has no stop. The position is closed at
`exit_hhmm` (default 15:10), or by the MIS square-off at the cut-off. At most one trade per day.

Params: `symbol`, `range_minutes` (15), `qty` (units), `short` (false), `exit_hhmm` ("15:10").
"""
from __future__ import annotations

from stockify import timeutil
from stockify.strategy import Strategy, encode_state, fmt_price, fmt_qty


class OpeningRangeBreakout(Strategy):
    closed = False                      # the day's position is closed (or the time exit passed): no more range lines
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
        self.symbol = p["symbol"]
        self.iid = ctx.resolve(self.symbol)
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
        self.closed = False
        self.entry_id = None
        self.stop_side = None
        self.stop_trigger = 0
        self.stop_ids = []
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
        if self.hi is None or self.closed:
            return
        ctx.plot("range_high", self.hi, instrument=self.iid)     # paise: the range as the bars after it see it
        ctx.plot("range_low", self.lo, instrument=self.iid)
        if self.traded:
            return
        tick = ctx.spec(self.iid)["tick_paise"]
        if bar.close > self.hi:
            self.entry_id = ctx.place(
                self.iid, "BUY", self.qty, "MIS", "MARKET", tag="orb_long",
                note=f"Bought {fmt_qty(self.qty)} {self.symbol}: price {fmt_price(bar.close)} broke above the "
                     f"{self.minutes}-minute opening range high of {fmt_price(self.hi)}")
            self.stop_side, self.stop_trigger = "SELL", self.lo // tick * tick
            self.traded = True
        elif self.short and bar.close < self.lo:
            self.entry_id = ctx.place(
                self.iid, "SELL", self.qty, "MIS", "MARKET", tag="orb_short",
                note=f"Sold short {fmt_qty(self.qty)} {self.symbol}: price {fmt_price(bar.close)} broke below the "
                     f"{self.minutes}-minute opening range low of {fmt_price(self.lo)}")
            self.stop_side, self.stop_trigger = "BUY", -(-self.hi // tick) * tick
            self.traded = True

    def on_fill(self, ctx, fill):  # noqa: ANN001
        # The stop exists only for what the entry has filled: one SL-M of exactly each fill's quantity. A rejected or
        # cancelled entry fills nothing, so it never gets a stop (and cannot open a reverse position).
        if self.entry_id is not None and fill.order_id == self.entry_id:
            qty = fmt_qty(fill.qty)
            trigger = fmt_price(self.stop_trigger)
            note = (f"Stop-loss for the {qty} bought: sells if price falls to {trigger}" if self.stop_side == "SELL"
                    else f"Stop-loss for the {qty} sold short: buys back if price rises to {trigger}")
            self.stop_ids.append(ctx.place(self.iid, self.stop_side, fill.qty, "MIS", "SL-M",
                                           trigger=self.stop_trigger, tag="orb_stop", note=note))
        elif fill.order_id in self.stop_ids:
            pos = ctx.position(self.iid, "MIS")                  # a stop filled: the day is closed once flat
            self.closed = not (pos and pos.qty)

    # A paper session's day-to-day state (A2 §4.1): the default, with the date `day` as ISO text.
    def save_state(self) -> dict:
        return encode_state(self, {**self.state_attrs(), "day": self.day.isoformat() if self.day else None})

    def load_state(self, state: dict) -> None:
        from datetime import date
        super().load_state(state)
        self.day = date.fromisoformat(self.day) if self.day else None

    def on_timer(self, ctx, timer_id):  # noqa: ANN001
        self.closed = True                                      # the time exit ends the day's lines
        for stop_id in self.stop_ids:
            ctx.cancel(stop_id)
        pos = ctx.position(self.iid, "MIS")
        if pos and pos.qty:
            side = "SELL" if pos.qty > 0 else "BUY"
            ctx.place(self.iid, side, abs(pos.qty), "MIS", "MARKET", tag="orb_exit",
                      note=f"Closed {fmt_qty(abs(pos.qty))} {self.symbol} at the {self.exit_hhmm} time exit")

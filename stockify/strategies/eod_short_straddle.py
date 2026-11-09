"""End-of-day NIFTY short straddle on NSE bhavcopy data (M2.4; label `eod_approx`).

Run with a `bhav_eod` pack (the NIFTY 50 index plus a NIFTY option chain around
the money) and the `eod_close` fill model (A4 §4.9): a decision taken on day d's
published file fills at day d+1's close, moved adversely by `half_spread_ticks`.

Each evening, just after the day's file is published:
* flat → sell `lots` lots each of the ATM call and put of the nearest expiry at
  least `min_days` calendar days away (ATM = strike nearest the index close);
* short → buy both back if their combined close has risen above
  `stop_multiple` × the combined premium received (a stop on daily closes);
  otherwise hold to expiry, where they settle in cash at the FSP.

Orders are protected market orders with a wide band (`protection_bp`, default
5000 = 50 %): option closes move far more than 2 % from day to day, and the
fill is the next close anyway.

Params: `underlying` ("NIFTY"), `index` ("NSE:NIFTY 50"), `lots` (1),
`stop_multiple` ("1.5"), `min_days` (1), `protection_bp` (5000).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from stockify import timeutil
from stockify.strategy import Strategy, encode_state


class EodShortStraddle(Strategy):
    # The parameter schema for the catalogue and the /tests form (data only; on_start reads ctx.params).
    PARAMS = [
        {'name': 'underlying', 'label': 'Index for the options', 'type': 'underlying', 'default': 'NIFTY', 'required': False, 'help': 'the index whose options are sold'},
        {'name': 'index', 'label': 'Index symbol', 'type': 'symbol', 'default': 'NSE:NIFTY 50', 'required': False, 'help': 'the index itself, for the at-the-money strike'},
        {'name': 'lots', 'label': 'Lots per option', 'type': 'int', 'default': 1, 'required': False, 'help': 'lots of each option sold'},
        {'name': 'stop_multiple', 'label': 'Stop at × premium', 'type': 'decimal', 'default': '1.5', 'required': False, 'help': 'buy back when the options cost this many times the premium'},
        {'name': 'min_days', 'label': 'Skip expiries closer than (days)', 'type': 'int', 'default': 1, 'required': False, 'help': 'skip expiries fewer than this many days away'},
        {'name': 'protection_bp', 'label': 'Price band (basis points)', 'type': 'int', 'default': 5000, 'required': False, 'help': 'price band for the market orders, in basis points (5000 = 50 %)'},
    ]

    def on_start(self, ctx):  # noqa: ANN001
        p = ctx.params
        self.und = p.get("underlying", "NIFTY")
        self.index = ctx.resolve(p.get("index", "NSE:NIFTY 50"))
        self.lots = int(p.get("lots", 1))
        self.stop = Decimal(str(p.get("stop_multiple", "1.5")))
        self.min_days = int(p.get("min_days", 1))
        self.bp = int(p.get("protection_bp", 5000))
        self.legs: list[int] = []
        self.credit = 0
        self.pending_day = None
        ctx.subscribe_bars(self.index, 86400)        # option closes are read with ctx.bars()

    def on_bar(self, ctx, bar):  # noqa: ANN001
        if self.pending_day != ctx.today():
            self.pending_day = ctx.today()
            ctx.schedule_at(ctx.now() + 1)                     # after every bar published at this moment

    # A paper session's day-to-day state (A2 §4.1): the default, with the date `pending_day` as ISO text.
    def save_state(self) -> dict:
        d = self.pending_day
        return encode_state(self, {**self.state_attrs(), "pending_day": d.isoformat() if d else None})

    def load_state(self, state: dict) -> None:
        super().load_state(state)
        self.pending_day = date.fromisoformat(self.pending_day) if self.pending_day else None

    def _close(self, ctx, iid: int) -> int | None:  # noqa: ANN001
        bars = ctx.bars(iid, 86400, 1)
        if not bars or timeutil.ns_to_ist(bars[-1].start_ns).date() != ctx.today():
            return None                                       # no close published for it today
        return bars[-1].close

    def on_timer(self, ctx, timer_id):  # noqa: ANN001
        held = [p for p in ctx.positions() if p.instrument in self.legs]
        if held:
            closes = [self._close(ctx, p.instrument) for p in held]
            if None in closes:
                return
            value = 0
            for c, p in zip(closes, held):
                value += c * abs(p.qty)
            if Decimal(value) > self.stop * self.credit:
                for p in held:
                    ctx.place(p.instrument, "BUY", abs(p.qty), "NRML", "MARKET", protection=self.bp, tag="stop")
                ctx.log("INFO", f"stop: legs worth {value} vs credit {self.credit}")
                self.legs = []
            return
        self.legs = []
        spot = self._close(ctx, self.index)
        if spot is None:
            return
        today = ctx.today()
        expiries = [e for e in ctx.expiries(self.und, "OPT") if (date.fromisoformat(e) - today).days >= self.min_days]
        if not expiries:
            return
        chain = ctx.chain(self.und, expiries[0])
        strikes = sorted({r["strike_paise"] for r in chain})
        if not strikes:
            return
        atm = min(strikes, key=lambda k: (abs(k - spot), k))
        legs = [r["instrument_id"] for r in chain if r["strike_paise"] == atm]
        if len(legs) != 2 or any(self._close(ctx, i) is None for i in legs):
            return
        credit = 0
        for iid in legs:
            qty = self.lots * ctx.spec(iid)["lot"]
            ctx.place(iid, "SELL", qty, "NRML", "MARKET", protection=self.bp, tag="entry")
            credit += self._close(ctx, iid) * qty
        self.legs, self.credit = legs, credit
        ctx.log("INFO", f"sell straddle {expiries[0]} strike {atm} spot {spot}")

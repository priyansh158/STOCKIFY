"""Shared mechanics of the intraday option-selling reference strategies (M4.13): short_straddle_stop and iron_fly.

Each trading day, from `entry_hhmm` IST: pick the nearest expiry at least `min_days` away, read the chain view
(ctx.chain, M4.4) and take its ATM strike (nearest the forward). At the first moment every leg has a fresh
two-sided quote (checked at the entry time, then on each tick), buy the wings first, if any (`wing_strikes`
strikes either side), then sell the ATM call and put. If that moment does not come before `exit_hhmm`, there is
no trade that day. When a short leg's sell completes, place a stop-loss: a
BUY SL-M with its trigger at `stop_pct` above that leg's average fill price, rounded up to the tick. At
`exit_hhmm`: cancel any stop still working and close every leg still open, with market orders.

Nothing is traded on data that is not there (I-7): no entry while any leg lacks a fresh two-sided quote or the
chain has no forward. Orders are NRML, market orders carry the `protection_bp` band. The fill model (quote-based,
`snapshot_quote`) and the margin model are the run's choice, not the strategy's.

The subclasses read their own params in on_start (one module each, so the catalogue's schema check sees exactly
what each reads) and call `begin(ctx)`.
"""
from __future__ import annotations

from datetime import date
from decimal import ROUND_CEILING, Decimal

from stockify import timeutil
from stockify.strategy import Strategy, encode_state

DAY_NS = 86_400 * 1_000_000_000


class OptionLegs(Strategy):
    underlying: str = "NIFTY"
    entry_hhmm: str = "09:30"
    exit_hhmm: str = "15:15"
    stop_pct: Decimal = Decimal("0.3")
    lots: int = 1
    min_days: int = 0
    protection_bp: int = 5000
    wing_strikes: int = 0                       # 0: a straddle; n: an iron fly with wings n strikes out

    # ── setup ────────────────────────────────────────────────────────────────
    def begin(self, ctx) -> None:  # noqa: ANN001
        if self.stop_pct <= 0:
            raise ValueError("stop_pct must be above 0")
        if self.stop_pct > 1:                   # the same bound as the params schema, for a subclass built by hand
            raise ValueError(f'stop_pct {self.stop_pct} is above 1: it is a fraction ("0.3" = 30 %)')
        rows = [r for r in ctx._rows() if r.get("underlying") == self.underlying or r["id"] in self._index_ids(ctx)]
        for r in rows:
            ctx.subscribe_ticks(r["id"])
        self.body: list[int] = []               # the legs sold today
        self.wings: list[int] = []              # the legs bought today
        self.entries: dict[int, int] = {}       # sell order id → instrument
        self.stops: dict[int, int] = {}         # instrument → stop order id
        self.armed = False                      # between the entry time and an entry (or the exit time)
        self.waiting_for = ""                   # why the armed entry has not happened yet
        self.entry_timer = self._daily(ctx, self.entry_hhmm)
        self.exit_timer = self._daily(ctx, self.exit_hhmm)

    # A paper session's day-to-day state (A2 §4.1): the default, with the int-keyed dicts `entries` (sell order id
    # → instrument) and `stops` (instrument → stop order id) as [key, value] pairs in insertion order.
    def save_state(self) -> dict:
        return encode_state(self, {**self.state_attrs(), "entries": [[k, v] for k, v in self.entries.items()],
                                   "stops": [[k, v] for k, v in self.stops.items()]})

    def load_state(self, state: dict) -> None:
        super().load_state(state)
        self.entries = {k: v for k, v in self.entries}
        self.stops = {k: v for k, v in self.stops}

    def _index_ids(self, ctx) -> set[int]:  # noqa: ANN001
        return {r["underlying_id"] for r in ctx._rows()
                if r.get("underlying") == self.underlying and r.get("underlying_id") is not None}

    @staticmethod
    def _daily(ctx, hhmm: str) -> int:  # noqa: ANN001
        first = timeutil.ist_ns(ctx.today(), hhmm)
        while first <= ctx.now():
            first += DAY_NS
        return ctx.schedule_every(DAY_NS, first)

    # ── the day ──────────────────────────────────────────────────────────────
    def on_timer(self, ctx, timer_id):  # noqa: ANN001
        if timer_id == self.entry_timer:
            self.armed, self.waiting_for = True, ""
            self._try_enter(ctx)
        elif timer_id == self.exit_timer:
            if self.armed:
                ctx.log("info", f"skip: {self.waiting_for or 'no entry'} until the exit time")
                self.armed = False
            self._exit(ctx)

    def on_tick(self, ctx, tick):  # noqa: ANN001
        if self.armed:
            self._try_enter(ctx)

    def _try_enter(self, ctx) -> None:  # noqa: ANN001
        why = self._enter(ctx)
        if why is None:
            self.armed = False
        elif why != self.waiting_for:
            if not self.waiting_for:
                ctx.log("info", f"waiting: {why}")
            self.waiting_for = why

    def _expiry(self, ctx) -> str | None:  # noqa: ANN001
        today = ctx.today()
        for e in ctx.expiries(self.underlying, "OPT"):
            if (date.fromisoformat(e) - today).days >= self.min_days:
                return e
        return None

    def _enter(self, ctx) -> str | None:  # noqa: ANN001
        """Enter now if possible. None when done for today (entered, or nothing to do); else why not yet."""
        if any(self._open(ctx, iid) for iid in self.body + self.wings):
            ctx.log("info", "skip: earlier legs are still open")
            return None
        expiry = self._expiry(ctx)
        if expiry is None:
            ctx.log("info", "skip: no expiry")
            return None
        view = ctx.chain(self.underlying, expiry)
        atm = view.atm_strike_paise
        if atm is None:
            return f"no forward for {self.underlying} {expiry}"
        strikes = sorted({r["strike_paise"] for r in view})
        i = strikes.index(atm)
        by = {(r["strike_paise"], r["kind"]): r for r in view}
        legs = [("SELL", by.get((atm, "CE"))), ("SELL", by.get((atm, "PE")))]
        if self.wing_strikes:
            if i + self.wing_strikes >= len(strikes) or i - self.wing_strikes < 0:
                return f"no strikes {self.wing_strikes} away from {atm}"
            legs = [("BUY", by.get((strikes[i + self.wing_strikes], "CE"))),
                    ("BUY", by.get((strikes[i - self.wing_strikes], "PE")))] + legs
        missing = [side for side, r in legs if r is None or r["bid_paise"] is None or r["ask_paise"] is None
                   or r["stale"]]
        if missing:
            return "a leg has no fresh two-sided quote"
        self.body, self.wings, self.entries, self.stops = [], [], {}, {}
        for side, r in legs:                                # wings first, so the short legs are covered
            iid = r["instrument_id"]
            qty = self.lots * ctx.spec(iid)["lot"]
            oid = ctx.place(iid, side, qty, "NRML", "MARKET", protection=self.protection_bp,
                            tag="wing" if side == "BUY" else "entry")
            (self.wings if side == "BUY" else self.body).append(iid)
            if side == "SELL":
                self.entries[oid] = iid
        ctx.log("info", f"enter {expiry} atm {atm} forward {view.forward_paise} ({view.forward_source})")
        return None

    def on_order_update(self, ctx, order):  # noqa: ANN001
        iid = self.entries.get(order.order_id)
        if iid is None or order.status not in ("COMPLETE", "CANCELLED", "REJECTED") or order.filled_qty == 0:
            return
        if iid in self.stops:
            return
        tick = ctx.spec(iid)["tick_paise"]
        raw = Decimal(order.avg_price) * (1 + self.stop_pct)
        trigger = int((raw / tick).to_integral_value(rounding=ROUND_CEILING)) * tick
        self.stops[iid] = ctx.place(iid, "BUY", order.filled_qty, "NRML", "SL-M", trigger=trigger,
                                    protection=self.protection_bp, tag="stop")

    def _open(self, ctx, iid: int) -> int:  # noqa: ANN001
        pos = ctx.position(iid, "NRML")
        return pos.qty if pos else 0

    def _exit(self, ctx) -> None:  # noqa: ANN001
        for oid in self.stops.values():
            ctx.cancel(oid)
        for iid in self.body + self.wings:
            qty = self._open(ctx, iid)
            if qty:
                ctx.place(iid, "BUY" if qty < 0 else "SELL", abs(qty), "NRML", "MARKET",
                          protection=self.protection_bp, tag="time_exit")

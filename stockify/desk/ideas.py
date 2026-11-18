"""HD 7.1: option ideas for one F&O holding, priced from the live chain (`chain.snapshot` / `chain.gauge`).

A pure function apart from the optional margin client. Ideas, in this order:

* hedges, always: protective put (the put at the strike nearest the verdict's nearest exit level, else one strike
  below the strike nearest the price), collar (that put plus a sold call at the nearest call wall);
* covered call (a sold call at the nearest call wall), only when the free shares make at least one lot;
* when the rules turn down (verdict Exit or Trim): bear put spread (buy the put nearest the price, sell the put at
  the nearest put wall below it) and long put;
* when they say stay in (verdict Hold with exit share at most 0.4, so at least 60% of the weight says In): bull
  call spread (buy the call nearest the price, sell the call at the nearest call wall above it) and long call.

Without a wall, the next listed strike on that side stands in. The near expiry is used, unless it has fewer than
MIN_DAYS_LEFT trading days left (counting the trade date and the expiry day), then the next one.

Buys are priced at the ask, sells at the bid; a leg without that quote makes the idea not possible. Hedges and the
covered call use floor(free shares / lot) lots, the directional ideas 1 lot. Every figure is in paise for the
stated lots; the shares in a hedge are valued at today's price, never at what the owner paid. `max_gain_paise`
None means no upper limit. Margin is Kite's basket margin (span + exposure + additional + option premium) for the
idea's option legs alone, compared with the equity segment's net available cash; both are None without a client
or when Kite does not answer.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from stockify.data import calendar
from stockify.desk.thesis import Verdict
from stockify.strategy import fmt_price, fmt_qty

EXCHANGE = "NFO"
MIN_DAYS_LEFT = 5
HIGH_IV = 70.0
LOW_IV = 30.0
CALL_SHARE = 0.4            # "Hold" with exit share at most this (at least 60% of the weight says In)
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass(frozen=True)
class Idea:
    name: str                      # "Protective put", "Collar", "Covered call", "Bull call spread", "Long call", ...
    legs: list[dict]               # {side, tradingsymbol, lots, price_paise}
    premium_paise: int             # + paid / − received, for the stated lots
    max_loss_paise: int | None     # None: no limit
    max_gain_paise: int | None     # None: no limit
    breakeven_paise: list[int]
    margin_paise: int | None
    margin_ok: bool | None
    why: str
    caution: str
    possible: bool
    not_possible_reason: str


def _rupees(paise: int) -> str:
    return "₹" + fmt_price(int(paise))


def _day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


def _days_left(trade_date: str, expiry: str) -> int:
    return len(calendar.trading_days("FO", date.fromisoformat(trade_date), date.fromisoformat(expiry)))


def _pick_expiry(snap: dict) -> tuple[str | None, str]:
    exps = snap.get("expiries") or []
    if not exps:
        return None, ""
    near = exps[0]
    td = snap.get("trade_date")
    if td and len(exps) > 1:
        left = _days_left(td, near)
        if left < MIN_DAYS_LEFT:
            return exps[1], (f" The near expiry ({_day(near)}) has only {left} trading days left, so this uses the "
                             f"next expiry, {_day(exps[1])}.")
    return near, ""


def _nearest(strikes: list[int], target: int) -> int | None:
    return min(strikes, key=lambda s: (abs(s - target), s)) if strikes else None


class _Leg:
    __slots__ = ("side", "kind", "strike", "row")

    def __init__(self, side: str, kind: str, strike: int | None, rows: dict):
        self.side, self.kind, self.strike = side, kind, strike
        self.row = rows.get((strike, kind)) if strike is not None else None

    @property
    def price(self) -> int | None:
        if self.row is None:
            return None
        return self.row.get("ask_paise") if self.side == "BUY" else self.row.get("bid_paise")

    def missing(self) -> str:
        if self.strike is None:
            return "The chain has no strike on that side of the price"
        return f"No live quotes for {_rupees(self.strike)} {self.kind}"

    def as_dict(self, lots: int) -> dict:
        return {"side": self.side, "tradingsymbol": self.row.get("tradingsymbol") if self.row else None,
                "lots": lots, "price_paise": self.price}


def _iv_caution(legs: list[_Leg], pct: float | None) -> str:
    if pct is None:
        return ""
    if pct >= HIGH_IV and any(l.side == "BUY" for l in legs):
        return (f" Options are priced high right now (implied volatility is at or above {pct:.0f}% of the past "
                "year's days), so buying one costs more than usual.")
    if pct <= LOW_IV and any(l.side == "SELL" for l in legs):
        return (f" Options are priced low right now (implied volatility is at or above only {pct:.0f}% of "
                "the past year's days), so selling one brings in less than usual.")
    return ""


def _cash_paise(client) -> int | None:  # noqa: ANN001
    """The equity segment's net available cash from Kite `margins()`, in paise; None when it cannot be read."""
    try:
        eq = (client.margins() or {}).get("equity") or {}
        value = eq.get("net")
        if value is None:
            value = (eq.get("available") or {}).get("live_balance")
        if value is None or isinstance(value, bool):
            return None
        return int((Decimal(str(value)) * 100).to_integral_value())
    except Exception:  # noqa: BLE001 — no login, the network: no cash figure
        return None


def _margin_paise(client, legs: list[_Leg], units: int) -> int | None:  # noqa: ANN001
    from stockify.daemon.margin_poller import kite_parts
    orders = [{"exchange": EXCHANGE, "tradingsymbol": l.row["tradingsymbol"],
               "transaction_type": l.side, "variety": "regular", "product": "NRML",
               "order_type": "MARKET", "quantity": units, "price": 0} for l in legs]
    try:
        parts = kite_parts(client.basket_order_margins(orders, consider_positions=False))
    except Exception:  # noqa: BLE001 — Kite did not answer, or answered without a margin block
        return None
    return sum(parts.values())


def ideas(verdict: Verdict, snap: dict, gauge: dict, spot_paise: int, lot: int, free_qty: int, *,
          margin_client=None) -> list[Idea]:  # noqa: ANN001
    """The option ideas for one holding (see the module docstring). [] when the chain lists no expiry or the lot
    is unknown."""
    expiry, roll_note = _pick_expiry(snap)
    if expiry is None or not lot or lot <= 0:
        return []
    rows = {(r["strike_paise"], r["kind"]): r for r in snap.get("rows") or [] if r.get("expiry") == expiry}
    strikes = sorted({k for k, _kind in rows})
    if not strikes:
        return []
    spot = int(spot_paise)
    atm = _nearest(strikes, spot)
    below_atm = [s for s in strikes if s < atm]
    above_atm = [s for s in strikes if s > atm]
    call_walls = list(gauge.get("call_walls") or [])
    put_walls = list(gauge.get("put_walls") or [])
    pct = gauge.get("iv_percentile")
    held_lots = max(0, int(free_qty)) // lot
    exp_txt = f"the {_day(expiry)} expiry"
    lot_txt = f"1 lot is {fmt_qty(lot)} shares"

    # strikes
    if verdict.nearest_exit_paise is not None:
        put_k = _nearest(strikes, verdict.nearest_exit_paise)
        put_src = f"the strike nearest the nearest exit level ({_rupees(verdict.nearest_exit_paise)})"
    else:
        put_k = below_atm[-1] if below_atm else atm
        put_src = "one strike below the price"
    wall_up = [w for w in call_walls if w > spot]
    if wall_up:
        call_k, call_src = _nearest(strikes, min(wall_up)), "where the most calls are open above the price"
    else:
        up = [s for s in strikes if s > spot]
        call_k, call_src = (up[0] if up else None), "the first strike above the price"

    out: list[Idea] = []

    def build(name: str, legs: list[_Leg], lots: int, figures, why: str, caution: str,  # noqa: ANN001
              shares_needed: bool) -> None:
        units = lots * lot
        reason = ""
        if shares_needed and held_lots == 0:
            reason = f"You hold fewer shares than one lot ({fmt_qty(lot)})"
        else:
            gap = next((l for l in legs if l.price is None), None)
            if gap is not None:
                reason = gap.missing()
        caution = caution + _iv_caution(legs, pct)
        why = why + roll_note
        if reason:
            out.append(Idea(name, [l.as_dict(lots) for l in legs], 0, None, None, [], None, None, why, caution,
                            False, reason))
            return
        prem = sum((l.price if l.side == "BUY" else -l.price) for l in legs)   # per share
        loss, gain, be = figures(prem)
        margin = ok = None
        if margin_client is not None:
            margin = _margin_paise(margin_client, legs, units)
            if margin is not None:
                cash = _cash_paise(margin_client)
                ok = None if cash is None else margin <= cash
        out.append(Idea(name, [l.as_dict(lots) for l in legs], prem * units,
                        None if loss is None else loss * units, None if gain is None else gain * units,
                        be, margin, ok, why, caution, True, ""))

    hl = max(held_lots, 1)          # lots shown on a hedge the owner cannot do yet

    # Protective put: the shares valued at today's price, plus the put.
    pp = _Leg("BUY", "PE", put_k, rows)
    build("Protective put", [pp], hl,
          lambda p: (max(0, spot - put_k + p), None, [spot + p]),
          f"Buying the {_rupees(put_k)} put, {put_src}, sets a floor under your shares until {exp_txt}; "
          f"{lot_txt}.",
          "The put costs money and loses value every day; if the stock does not fall below the strike it "
          "expires worth nothing.", True)

    # Collar: the same put, paid for in part by selling a call.
    cl = [_Leg("BUY", "PE", put_k, rows), _Leg("SELL", "CE", call_k, rows)]
    build("Collar", cl, hl,
          lambda p: (max(0, spot - put_k + p), call_k - spot - p, [spot + p]),
          f"Buying the {_rupees(put_k)} put and selling a call {call_src}"
          f"{'' if call_k is None else ' (' + _rupees(call_k) + ')'} sets a floor under your shares and pays for "
          f"some or all of it by giving up the rise above the call's strike, until {exp_txt}; {lot_txt}.",
          "If the stock rises above the call's strike your shares can be called away at that price, and you "
          "miss any rise beyond it.", True)

    # Covered call: shares plus a sold call.
    cc = _Leg("SELL", "CE", call_k, rows)
    build("Covered call", [cc], hl,
          lambda p: (spot + p, call_k - spot - p, [spot + p]),
          f"Selling a call {call_src}"
          f"{'' if call_k is None else ' (' + _rupees(call_k) + ')'} against your shares brings in its price "
          f"now, until {exp_txt}; {lot_txt}.",
          "If the stock rises above the strike your shares can be called away at that price; the call's price "
          "does little to cushion a fall.", True)

    down = verdict.label in ("Exit", "Trim")
    up_ok = verdict.label == "Hold" and verdict.exit_share <= CALL_SHARE
    if down:
        walls = [w for w in put_walls if w < atm]
        if walls:
            low_k, low_src = _nearest(below_atm, max(walls)), "where the most puts are open below it"
        else:
            low_k, low_src = (below_atm[-1] if below_atm else None), "the next strike below it"
        legs = [_Leg("BUY", "PE", atm, rows), _Leg("SELL", "PE", low_k, rows)]
        build("Bear put spread", legs, 1,
              lambda p: (p, atm - low_k - p, [atm - p]),
              f"The tested rules say to be out of this stock. Buying the {_rupees(atm)} put and selling one "
              f"{low_src}{'' if low_k is None else ' (' + _rupees(low_k) + ')'} gains if the stock falls, for less "
              f"than a put alone, until {exp_txt}; {lot_txt}.",
              "The most it can make is capped at the lower strike, and you lose what you paid if the stock does "
              "not fall.", False)
        build("Long put", [_Leg("BUY", "PE", atm, rows)], 1,
              lambda p: (p, atm - p, [atm - p]),
              f"The tested rules say to be out of this stock. Buying the {_rupees(atm)} put gains if the stock "
              f"falls below the breakeven by {exp_txt}; {lot_txt}.",
              "You lose the whole price of the put if the stock is not below the breakeven at expiry.", False)
    elif up_ok:
        walls = [w for w in call_walls if w > atm]
        if walls:
            hi_k, hi_src = _nearest(above_atm, min(walls)), "where the most calls are open above it"
        else:
            hi_k, hi_src = (above_atm[0] if above_atm else None), "the next strike above it"
        legs = [_Leg("BUY", "CE", atm, rows), _Leg("SELL", "CE", hi_k, rows)]
        build("Bull call spread", legs, 1,
              lambda p: (p, hi_k - atm - p, [atm + p]),
              f"Most of the tested rules say to stay in this stock. Buying the {_rupees(atm)} call and selling "
              f"one {hi_src}{'' if hi_k is None else ' (' + _rupees(hi_k) + ')'} gains if the stock rises, for "
              f"less than a call alone, until {exp_txt}; {lot_txt}.",
              "The most it can make is capped at the higher strike, and you lose what you paid if the stock does "
              "not rise.", False)
        build("Long call", [_Leg("BUY", "CE", atm, rows)], 1,
              lambda p: (p, None, [atm + p]),
              f"Most of the tested rules say to stay in this stock. Buying the {_rupees(atm)} call gains if the "
              f"stock rises above the breakeven by {exp_txt}; {lot_txt}.",
              "You lose the whole price of the call if the stock is not above the breakeven at expiry.", False)
    return out


__all__ = ["Idea", "ideas"]

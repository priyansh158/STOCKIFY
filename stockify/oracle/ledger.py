"""Cash, positions and NAV (A4 §6). Integers only.

Positions are keyed by `(instrument_id, product)`. Each keeps its open
quantity `q` (units, ≥ 0), direction `d` (+1 long, −1 short), the exact cost
`c` of the open quantity (Σ price × units) and the realised P&L. Reducing a
position by `k` units removes `floor_div(c·k, q)` of the cost, so the cost is
used up exactly when the position closes and realised P&L is exact (§6.1).

Two cash styles:

* **premium** (CNC equities, options): the traded value moves at each fill;
  NAV counts the position at its mark (long +, short −).
* **variation** (futures, MIS equities): nothing moves at the fill. Cash moves
  by `pnl(m) − vm` when the position is marked (SEG_CLOSE, daily settlement
  price, final settlement) and when it goes flat, where
  `pnl(x) = realised + d·(q·x − c)` and `vm` is what was already moved. A fill
  never changes `pnl` at its own price, so this is exact. NAV adds the
  unrealised part since the last mark: `pnl(mark) − vm`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from stockify import numerics


@dataclass
class Position:
    instrument_id: int
    product: str
    style: str                 # premium | variation
    q: int = 0
    d: int = 1
    c: int = 0
    realised: int = 0
    vm: int = 0                # variation already moved to cash
    last_mark: int | None = None
    mark_source: str = ""

    @property
    def qty(self) -> int:
        return self.d * self.q

    @property
    def avg_price(self) -> int:
        return numerics.floor_div(self.c, self.q) if self.q else 0

    def pnl(self, price: int) -> int:
        return numerics.traded_pnl(self.realised, self.d, self.q * price, self.c)

    def copy(self) -> "Position":
        return Position(**self.__dict__)


@dataclass
class Ledger:
    cash: int
    capital: int
    positions: dict = field(default_factory=dict)          # (iid, product) -> Position
    charges_total: int = 0
    unavailable: dict = field(default_factory=dict)        # trade_date -> CNC sale proceeds not yet usable
    reserved: int = 0

    def copy(self) -> "Ledger":
        return Ledger(self.cash, self.capital, {k: p.copy() for k, p in self.positions.items()},
                      self.charges_total, dict(self.unavailable), self.reserved)

    def position(self, iid: int, product: str) -> Position | None:
        p = self.positions.get((iid, product))
        return p if p is not None and p.q else None

    def get_or_open(self, iid: int, product: str, style: str) -> Position:
        key = (iid, product)
        if key not in self.positions:
            self.positions[key] = Position(iid, product, style)
        return self.positions[key]

    def apply_fill(self, iid: int, product: str, style: str, side: str, qty: int, price: int,
                   cnc_credit_ppb: int | None = None, trade_date: str | None = None) -> int:
        """Book one fill; returns the realised P&L it produced."""
        p = self.get_or_open(iid, product, style)
        sign = 1 if side == "BUY" else -1
        realised_before = p.realised
        remaining = qty
        if p.q and p.d != sign:                        # reducing (and maybe flipping)
            k = min(remaining, p.q)
            removed = p.c if k == p.q else numerics.floor_div(p.c * k, p.q)
            p.realised = numerics.traded_pnl(p.realised, p.d, k * price, removed)
            p.c -= removed
            p.q -= k
            remaining -= k
        if remaining:
            if not p.q:
                p.d, p.c = sign, 0
            p.q += remaining
            p.c += remaining * price
        if style == "premium":
            self.cash -= sign * qty * price
            if side == "SELL" and product == "CNC" and cnc_credit_ppb is not None and trade_date:
                held_back = qty * price - numerics.floor_div(qty * price * cnc_credit_ppb, 1_000_000_000)
                self.unavailable[trade_date] = self.unavailable.get(trade_date, 0) + held_back
        elif not p.q:                                  # variation position closed: settle what is left
            self.settle_variation(p, price, "fill")
        return p.realised - realised_before

    def settle_variation(self, p: Position, price: int, source: str) -> int:
        """Move `pnl(price) − vm` to cash (a mark or the final settlement)."""
        delta = p.pnl(price) - p.vm
        self.cash += delta
        p.vm += delta
        p.last_mark, p.mark_source = price, source
        return delta

    def debit(self, amount: int) -> None:
        self.cash -= amount
        self.charges_total += amount

    def release_day(self, trade_date: str) -> None:
        """CNC sale proceeds held back on earlier days become usable."""
        for d in [d for d in self.unavailable if d < trade_date]:
            del self.unavailable[d]

    def nav(self, mark) -> int:  # noqa: ANN001
        """mark(iid) -> price or None (then the position's last mark, else its average)."""
        total = self.cash
        for p in self.positions.values():
            if not p.q:
                continue
            m = mark(p.instrument_id)
            if m is None:
                m = p.last_mark if p.last_mark is not None else p.avg_price
            if p.style == "premium":
                total += p.qty * m
            else:
                total += p.pnl(m) - p.vm
        return total

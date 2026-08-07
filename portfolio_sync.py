"""
Read-only portfolio sync for STOCKIFY.

Phase 4 of the Kite Roadmap.

Wraps kite.holdings() and kite.positions() with a 60-second cache so the
"My Portfolio" page and the OVERVIEW caption don't slam the API on every
Streamlit rerun.

Critical framing rule (load-bearing):
    The LLM prompts MUST NOT learn that the user owns these positions.
    All position info passed to the model is anonymized — framed as
    "Observed position for context" with no owner attribution. This
    prevents the well-known LLM confirmation-bias failure mode where
    knowing the user holds X biases the analysis toward "this is great,
    you made a good call."
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

import kite_client


@dataclass
class Holding:
    tradingsymbol: str
    exchange: str          # "NSE" | "BSE"
    quantity: int
    average_price: float
    last_price: float
    pnl: float             # current unrealised P&L in ₹
    day_change_pct: float
    instrument_token: int
    isin: str = ""

    @property
    def invested(self) -> float:
        return float(self.quantity) * float(self.average_price)

    @property
    def current_value(self) -> float:
        return float(self.quantity) * float(self.last_price)

    @property
    def pnl_pct(self) -> float:
        inv = self.invested
        return (self.pnl / inv * 100.0) if inv else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["invested"] = self.invested
        d["current_value"] = self.current_value
        d["pnl_pct"] = self.pnl_pct
        return d


@dataclass
class PortfolioSnapshot:
    holdings: list[Holding] = field(default_factory=list)
    positions_net: list[dict] = field(default_factory=list)
    fetched_at: int = 0
    error: Optional[str] = None

    @property
    def total_invested(self) -> float:
        return sum(h.invested for h in self.holdings)

    @property
    def total_current(self) -> float:
        return sum(h.current_value for h in self.holdings)

    @property
    def total_pnl(self) -> float:
        return sum(h.pnl for h in self.holdings)

    @property
    def total_pnl_pct(self) -> float:
        inv = self.total_invested
        return (self.total_pnl / inv * 100.0) if inv else 0.0


_cache: Optional[PortfolioSnapshot] = None


def fetch_snapshot() -> PortfolioSnapshot:
    """Always fetch fresh; populates _cache. Returns a snapshot, with
    snapshot.error set when the underlying calls failed."""
    global _cache
    snap = PortfolioSnapshot(fetched_at=int(time.time()))
    if not kite_client.is_authenticated():
        snap.error = "Kite not authenticated"
        _cache = snap
        return snap
    try:
        raw_h = kite_client.holdings()
        for r in raw_h or []:
            try:
                snap.holdings.append(Holding(
                    tradingsymbol = (r.get("tradingsymbol") or "").upper(),
                    exchange      = (r.get("exchange") or "").upper(),
                    quantity      = int(r.get("quantity") or 0),
                    average_price = float(r.get("average_price") or 0),
                    last_price    = float(r.get("last_price") or 0),
                    pnl           = float(r.get("pnl") or 0),
                    day_change_pct = float(r.get("day_change_percentage") or 0),
                    instrument_token = int(r.get("instrument_token") or 0),
                    isin          = (r.get("isin") or ""),
                ))
            except Exception:
                continue
        try:
            pos = kite_client.positions()
            snap.positions_net = (pos or {}).get("net", []) or []
        except Exception:
            snap.positions_net = []
    except Exception as e:
        snap.error = str(e)
    _cache = snap
    return snap


def cached_snapshot(max_age_seconds: int = 60) -> PortfolioSnapshot:
    """Return _cache if still fresh, else re-fetch.

    Always returns a PortfolioSnapshot (possibly empty) — never None.
    """
    global _cache
    if _cache is not None and (int(time.time()) - _cache.fetched_at) < max_age_seconds:
        return _cache
    return fetch_snapshot()


def holding_for(tradingsymbol: str, exchange: str = "NSE") -> Optional[Holding]:
    """Look up a single holding by tradingsymbol in the cached snapshot."""
    sym = (tradingsymbol or "").upper()
    if not sym:
        return None
    snap = cached_snapshot()
    for h in snap.holdings:
        if h.tradingsymbol == sym:
            return h
    return None


# ── Anonymous-position blurb for LLM prompt injection ────────────────────────
#
# The framing rule (see module docstring): the LLM never learns that the user
# owns the position. We pass it as anonymized market context.

def anonymous_position_blurb(h: Holding) -> str:
    """Render an anonymized position-context block for inclusion in a prompt.

    Format is intentional — no "you", "your", "user", "I" pronouns; just
    "Observed position" framing so the model treats it as market data.
    """
    if not h or h.quantity <= 0:
        return ""
    pnl_sign = "+" if h.pnl_pct >= 0 else ""
    return (
        f"## OBSERVED POSITION FOR CONTEXT\n"
        f"(Anonymized context for analysis — do NOT assume the analysis recipient owns this.)\n"
        f"- Quantity: {h.quantity:,} shares\n"
        f"- Average entry price: ₹{h.average_price:,.2f}\n"
        f"- Current price: ₹{h.last_price:,.2f}\n"
        f"- Notional P&L: {pnl_sign}{h.pnl_pct:.2f}%  (₹{h.pnl:,.2f} on ₹{h.invested:,.2f} cost basis)\n"
    )


def ui_caption_for(h: Holding) -> str:
    """Short caption for the OVERVIEW header. UI-only; never sent to the LLM.

    Returns "" when the holding is missing or zero-quantity.
    """
    if not h or h.quantity <= 0:
        return ""
    sign = "+" if h.pnl_pct >= 0 else ""
    return (f"You hold {h.quantity:,} sh @ ₹{h.average_price:,.2f} avg · "
            f"{sign}{h.pnl_pct:.2f}% · "
            f"₹{h.invested:,.0f} cost → ₹{h.current_value:,.0f} now")


def invalidate_cache() -> None:
    """Force the next cached_snapshot() to refetch."""
    global _cache
    _cache = None

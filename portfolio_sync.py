"""
Read-only portfolio sync for STOCKIFY.

Phase 4 of the Kite Roadmap.

Wraps kite.holdings() and kite.positions() with a 60-second cache so the
portfolio page does not hit the API on every request.

A refused Kite login is never an empty portfolio: the snapshot carries `expired=True` and the plain EXPIRED sentence,
and the cache is keyed by the stored login, so a new login fetches again at once.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

import kite_client

log = logging.getLogger("stockify.portfolio_sync")

EXPIRED = "Your Kite login has expired or was ended by Zerodha. Log in again on this page."
UNAVAILABLE = "Kite did not send your holdings just now, so they cannot be shown. Reload the page in a minute."

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
    expired: bool = False      # Kite refused the stored login: log in again (error is EXPIRED)
    token_fp: str = ""         # which stored login made this snapshot (a hash, never the token)

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
    snap = PortfolioSnapshot(fetched_at=int(time.time()), token_fp=kite_client.token_fingerprint())
    if not kite_client.is_authenticated():
        snap.error = "Kite not authenticated"
        _cache = snap
        return snap
    try:
        raw_h = kite_client.holdings(strict=True)
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
    except Exception as e:  # noqa: BLE001 - only the class name is logged: SDK text can carry request details
        snap.holdings = []
        if kite_client.is_token_rejection(e):
            snap.error, snap.expired = EXPIRED, True
            log.info("portfolio: Kite refused the stored login (%s)", type(e).__name__)
        else:
            snap.error = UNAVAILABLE
            log.warning("portfolio: holdings fetch failed (%s)", type(e).__name__)
    _cache = snap
    return snap


def cached_snapshot(max_age_seconds: int = 60) -> PortfolioSnapshot:
    """Return _cache if still fresh and made by the same stored login, else re-fetch.

    Always returns a PortfolioSnapshot (possibly empty) — never None.
    """
    global _cache
    cached = _cache
    if (cached is not None and (int(time.time()) - cached.fetched_at) < max_age_seconds
            and cached.token_fp == kite_client.token_fingerprint()):
        return cached
    return fetch_snapshot()


def invalidate_cache() -> None:
    """Force the next cached_snapshot() to refetch."""
    global _cache
    _cache = None

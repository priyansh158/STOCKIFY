"""Pydantic response models — mirror existing Python dataclasses so the
frontend has a single source of truth.

When the underlying dataclass changes shape, update both here AND in
`web/lib/types.ts` (the TS mirror).
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ── Search ───────────────────────────────────────────────────────────────────

class SearchHit(BaseModel):
    """One row in the search-suggestions dropdown."""
    ticker: str = Field(..., description="Canonical bare NSE symbol, e.g. 'RELIANCE'")
    name: str
    exchange: str = Field("NSE", description="'NSE' or 'BSE'")
    sector: str | None = None
    instrument_type: str = Field("EQ", description="EQ / ETF / INDEX etc.")
    source: Literal["kite", "yfinance"] = "kite"
    instrument_token: int | None = Field(
        default=None,
        description="Kite instrument_token for live-tick subscription; None when source=yfinance.",
    )


# ── Analysis pipeline ────────────────────────────────────────────────────────

class StageStartedResponse(BaseModel):
    analysis_id: str
    ticker: str
    company_name: str
    stages: list[str]


class SSEDelta(BaseModel):
    type: Literal["delta"]
    text: str


class SSEDone(BaseModel):
    type: Literal["done"]
    result: dict[str, Any]


class SSEError(BaseModel):
    type: Literal["error"]
    message: str
    raw: str | None = None


# ── History ──────────────────────────────────────────────────────────────────

class HistoryListEntry(BaseModel):
    id: int
    ticker: str
    company_name: str
    sector: str | None = None
    verdict: str
    verdict_reason: str | None = None
    bottom_line: str | None = None
    created_at: int  # unix seconds


class HistoryRecord(HistoryListEntry):
    profile: dict[str, Any]
    financials: dict[str, Any]
    geo: dict[str, Any]
    technical: dict[str, Any]
    summary: dict[str, Any]
    macro_snapshot: dict[str, Any] | None = None
    position_context: dict[str, Any] | None = None


# ── Portfolio ────────────────────────────────────────────────────────────────

class HoldingDTO(BaseModel):
    symbol: str
    exchange: str
    quantity: int
    average_price: float
    last_price: float
    pnl: float
    pnl_pct: float
    instrument_token: int = 0


class PortfolioSnapshotDTO(BaseModel):
    holdings: list[HoldingDTO]
    total_value: float
    total_pnl: float
    fetched_at: int
    stale: bool = False
    error: str | None = None


# ── Kite ─────────────────────────────────────────────────────────────────────

class KiteStatusResponse(BaseModel):
    configured: bool
    authenticated: bool
    error: str | None = None


# ── Geo alerts ───────────────────────────────────────────────────────────────

class GeoAlertDTO(BaseModel):
    id: int
    created_at: int
    severity: str
    disruption_score: int
    scope: str | None = None
    title: str
    source: str | None = None
    url: str | None = None
    published_at: str | None = None
    guid: str | None = None
    why_disruptive: str | None = None
    investor_action: str | None = None
    confidence: float | None = None
    affected_tickers: list[str] = Field(default_factory=list)
    emailed_at: int | None = None
    acknowledged_at: int | None = None


# ── Ticks (WS payloads) ──────────────────────────────────────────────────────

class TickEvent(BaseModel):
    type: Literal["tick"]
    token: int
    ltp: float
    change_pct: float | None = None
    ts: int  # unix seconds

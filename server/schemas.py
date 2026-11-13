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


# ── Data layer (M1) ──────────────────────────────────────────────────────────

class RecorderStatusDTO(BaseModel):
    """Latest recorder health row (lab.sqlite recorder_status, written every 5 s)."""
    wall_ns: int
    connected: bool
    subs_full: int
    subs_quote: int
    packets_per_s: float
    queue_depth: int
    writer_lag_ms: float | None = None
    last_packet_age_ms: dict[str, int | None] = Field(default_factory=dict)
    bytes_today: int
    age_s: float = Field(..., description="seconds since this row was written")


class RecorderStatusResponse(BaseModel):
    status: RecorderStatusDTO | None


class InstrumentQualityDTO(BaseModel):
    """data_quality metrics of one instrument for one day (M1.9)."""
    instrument_id: int
    symbol: str | None = None
    metrics: dict[str, float]


class DataQualityResponse(BaseModel):
    date: str
    day: dict[str, float] = Field(default_factory=dict, description="day-level metrics (instrument_id 0)")
    instruments: list[InstrumentQualityDTO] = Field(default_factory=list)


# ── Ticks (WS payloads) ──────────────────────────────────────────────────────

class TickEvent(BaseModel):
    type: Literal["tick"]
    token: int
    ltp: float
    change_pct: float | None = None
    ts: int  # unix seconds

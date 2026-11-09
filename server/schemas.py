"""Pydantic response models — mirror existing Python dataclasses so the
frontend has a single source of truth.

When the underlying dataclass changes shape, update both here AND in
`web/src/lib/types.ts` (the TS mirror). blockwatch checks that the two
files list the same field names in the same order.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# <block name="dto-fields" same-as="web/src/lib/types.ts:dto-fields" same-as-mode="sequence" same-as-pattern='^\s*(?P<value>[a-z_0-9]+): '>
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


# ── Strategy tests (M3.14) ───────────────────────────────────────────────────
# Money is integer paise, times are UTC epoch ns with an ISO IST string beside them (A2 §6).

class StrategyParamDTO(BaseModel):
    """One parameter of a strategy (stockify.strategies._catalogue)."""
    name: str
    label: str = ""
    type: Literal["int", "bool", "str", "decimal", "symbol", "hhmm", "underlying"]
    default: Any = None
    required: bool = False
    help: str = ""


class StrategyDTO(BaseModel):
    kind: Literal["python", "cpp"]
    ref: str = Field(..., description="'module:Class' for python, the registered name for cpp")
    name: str
    doc: str = ""
    params: list[StrategyParamDTO] = Field(default_factory=list)


class TestRunDTO(BaseModel):
    """One run as a list card shows it (lab.sqlite runs row plus its headline metrics)."""
    run_id: str
    name: str | None = None
    family_id: str | None = None
    status: Literal["queued", "running", "done", "failed", "cancelled"]
    impl: str | None = None
    strategy_ref: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    price_basis: str | None = None
    capital_paise: int | None = None
    created_ns: int | None = None
    created_ist: str | None = None
    finished_ns: int | None = None
    finished_ist: str | None = None
    nav_start_paise: int | None = None
    nav_end_paise: int | None = None
    max_drawdown: float | None = Field(None, description="largest fall from a high, as a fraction (0.12 = 12 %)")
    trades: int | None = None
    costs_total_paise: int | None = None
    labels: list[str] = Field(default_factory=list)
    error: str | None = None


class TestRunDetailDTO(BaseModel):
    run: TestRunDTO
    config: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    manifest: dict[str, Any] | None = None
    fill_count: int | None = Field(None, description="all fills of the run (the /fills list is paged)")
    cost_totals: dict[str, int] = Field(default_factory=dict, description="paise per charge component, whole run")


class TestFillDTO(BaseModel):
    fill_id: int
    order_id: int
    t_ns: int
    t_ist: str
    instrument_id: int
    tradingsymbol: str | None = None
    side: str
    qty: int
    price_paise: int


class TestEquityDTO(BaseModel):
    trade_date: str
    nav_paise: int
    cash_paise: int
    margin_paise: int | None = None


class TestChargeDTO(BaseModel):
    row_no: int
    kind: str
    order_id: int | None = None
    trade_date: str
    components: dict[str, int] = Field(default_factory=dict)
    total_paise: int


class TestMarginDTO(BaseModel):
    """One underlying's margin on one day (margin_daily.csv, M4): SPAN-lite parts in paise; empty for flat runs."""
    trade_date: str
    underlying_id: int
    underlying: str | None = None
    model: str
    step: str = ""
    scan_paise: int
    somc_paise: int
    elm_paise: int
    addons_paise: int
    total_paise: int


class TestSettlementDTO(BaseModel):
    """An expiry settlement (cash, at the final settlement price) or a physical delivery, from the run's log."""
    t_ns: int
    t_ist: str
    trade_date: str
    kind: Literal["cash", "delivery"]
    instrument_id: int
    tradingsymbol: str | None = None
    product: str
    side: str | None = None
    qty: int | None = None
    price_paise: int
    source: str
    charges_paise: int | None = None


class TestGreekDTO(BaseModel):
    """One open F&O position at a day end with its Greeks (M4.14), derived from positions_eod.csv: the option's mark,
    its underlying's mark (D-55, used as the forward) and Black-76. Position figures: delta in underlying units, gamma
    in units per rupee, vega in rupees per volatility point, theta in rupees per day. When no IV can be found,
    `iv_reason` says why and the Greeks are empty. A future has delta = its quantity."""
    trade_date: str
    instrument_id: int
    tradingsymbol: str | None = None
    underlying: str | None = None
    kind: str
    strike_paise: int | None = None
    expiry: str | None = None
    qty: int
    mark_paise: int
    underlying_paise: int | None = None
    iv: float | None = None
    iv_reason: str | None = None
    delta_units: float | None = None
    gamma_units_per_rupee: float | None = None
    vega_rupees_per_point: float | None = None
    theta_rupees_per_day: float | None = None
    expiry_close_estimated: bool = False   # the expiry is after the run's last day: its close time is estimated


class TestLogsDTO(BaseModel):
    events_log: list[str] = Field(default_factory=list)
    runner_log: list[str] = Field(default_factory=list)
    offset: int = 0
    total_lines: int = 0


class TestCreatedDTO(BaseModel):
    run_id: str


# ── Ticks (WS payloads) ──────────────────────────────────────────────────────

class TickEvent(BaseModel):
    type: Literal["tick"]
    token: int
    ltp: float
    change_pct: float | None = None
    ts: int  # unix seconds
# </block>

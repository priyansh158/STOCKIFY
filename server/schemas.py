"""Pydantic response models — mirror existing Python dataclasses so the
frontend has a single source of truth.

When the underlying dataclass changes shape, update both here AND in
`web/src/lib/types.ts` (the TS mirror). blockwatch checks that the two
files list the same field names in the same order.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

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
    daemon_waiting_for_login: bool = False
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


class GeoMonitorStatusDTO(BaseModel):
    """The news monitor as the /alerts page shows it (D-150): when the last check finished (epoch seconds; None when
    none is recorded), who ran it ("background" = the scheduled monitor, "page" = the button), what it found in
    words, and whether a check is running right now (in the app or in the scheduled monitor)."""
    last_check_at: int | None = None
    last_by: Literal["background", "page"] | None = None
    last_plain: str | None = None
    checking: bool = False


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
    stale: bool = Field(False, description="the row is older than the recorder's report interval allows (stockify.daemon.recorder_health); connected is then false")
    market_open: bool = Field(False, description="now is inside market hours on a trading day (the recorder reports only then)")


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
    errors: list[str] = Field(default_factory=list, description="plain sentences naming a store that could not be read")
    error_stores: list[str] = Field(default_factory=list, description="store keys the errors name: refdata, bars, lab")


# ── Strategy tests (M3.14) ───────────────────────────────────────────────────
# Money is integer paise, times are UTC epoch ns with an ISO IST string beside them (A2 §6).

class ParamChoiceDTO(BaseModel):
    """One allowed value of a strategy parameter, with its label for a person (the value stays what is stored)."""
    value: Any
    label: str


class StrategyParamDTO(BaseModel):
    """One parameter of a strategy (stockify.strategies._catalogue)."""
    name: str
    label: str = ""
    type: Literal["int", "bool", "str", "decimal", "symbol", "hhmm", "underlying", "ranking"]
    default: Any = None
    required: bool = False
    help: str = ""
    choices: list[ParamChoiceDTO] = Field(default_factory=list, description="when not empty, the only values allowed; the form shows a list of their labels")
    percent: bool = Field(False, description="a decimal fraction the form shows as a percentage (0.95 as 95); stored unchanged")


class StrategyDTO(BaseModel):
    kind: Literal["python", "cpp"]
    ref: str = Field(..., description="'module:Class' for python, the registered name for cpp")
    name: str
    display_name: str = Field("", description="the strategy's name for a person ('Opening Range Breakout'); never a code")
    doc: str = ""
    params: list[StrategyParamDTO] = Field(default_factory=list)


class TestRunDTO(BaseModel):
    """One run as a list card shows it (lab.sqlite runs row plus its headline metrics)."""
    run_id: str
    name: str | None = None
    name_label: str = Field("", description="the name as words (stockify.display.name_label); the page shows this")
    family_id: str | None = None
    status: Literal["queued", "running", "done", "failed", "cancelled"]
    impl: str | None = None
    strategy_ref: str | None = None
    strategy_name: str | None = Field(None, description="the strategy's display name (the page shows this, never the ref)")
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
    manifest_labels: dict[str, str] = Field(default_factory=dict, description="each manifest key in plain words")
    fill_count: int | None = Field(None, description="all fills of the run (the /fills list is paged)")
    cost_totals: dict[str, int] = Field(default_factory=dict, description="paise per charge component, whole run")
    withheld: dict[str, str] = Field(default_factory=dict,
                                     description="metrics key -> plain reason the figure is not shown (null in metrics)")


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
    note: str = Field("", description="the strategy's own note on the order this fill belongs to ('' when it gave none)")
    tag: str = Field("", description="the strategy's tag on that order, as stored (a code label)")
    tag_label: str = Field("", description="the tag in plain words ('Long entry'); the page shows this, with `tag` as its hover")
    evidence: str = Field("", description="how the engine priced the fill (A1 fills.csv: eod_close, bar_open, ltp, ...)")
    at_close: bool = Field(False, description="True when the fill is the day's close (evidence eod_close): its time is "
                                              "when the close is published, not when anything traded")


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


class SessionDTO(BaseModel):
    """A paper session as a card. `today_pnl_paise` is the latest recorded day's NAV less the day before's (the
    capital for a first day); None until that day has an equity row. `coverage_today` is 0..1."""
    session_id: str
    name: str
    name_label: str = ""
    status: str
    pause_reasons: list[str] = Field(default_factory=list)
    strategy_name: str | None = Field(None, description="the strategy's display name")
    today_pnl_paise: int | None = None
    open_positions: int | None = None
    coverage_today: float | None = None
    parity_last: str | None = None
    labels: list[str] = Field(default_factory=list)
    created_ist: str


class SessionStatsDTO(BaseModel):
    days: int = 0
    pnl_paise: int = 0
    win_days: int = 0
    loss_days: int = 0
    best_day_paise: int | None = None
    worst_day_paise: int | None = None
    max_drawdown_paise: int = 0


class SessionDetailDTO(BaseModel):
    """The statistics are given twice (I-14): over every day, and over the days whose coverage reached the
    threshold. A day whose coverage is unknown is never counted as full."""
    session: SessionDTO
    config: dict[str, Any] = Field(default_factory=dict)
    trade_date: str | None = None
    stats_all: SessionStatsDTO
    stats_full_coverage: SessionStatsDTO
    coverage_threshold: str
    has_coverage_holes: bool = False


class SessionCreatedDTO(BaseModel):
    session_id: str


class SessionCommandDTO(BaseModel):
    cmd_id: int


class CommandStatusDTO(BaseModel):
    """A queued owner command. A pending command has no expiry: it is applied when the daemon next runs."""
    cmd_id: int
    session_id: str | None = None
    command: str
    status: Literal["pending", "applied", "failed"]
    created_ns: int
    created_ist: str
    applied_ns: int | None = None
    applied_ist: str | None = None
    error: str | None = None


class SessionPositionDTO(BaseModel):
    trade_date: str
    instrument_id: int
    instrument_token: int | None = None
    tradingsymbol: str | None = None
    product: str | None = None
    qty: int
    avg_price_paise: int | None = None
    mark_paise: int | None = None
    unrealised_paise: int | None = None
    source: Literal["eod", "fills"]
    names_note: str | None = None


class SessionTimelineDTO(BaseModel):
    engine_ns: int | None = None
    wall_ns: int
    wall_ist: str
    kind: str
    detail: Any = None


class SessionMarginDTO(BaseModel):
    wall_ns: int
    wall_ist: str
    trade_date: str | None = None
    step: str | None = None
    kite_total_paise: int | None = None
    ours_total_paise: int | None = None
    gap_paise: int | None = None
    gap_ppm: int | None = None
    error: str | None = None


class SessionOrderDTO(BaseModel):
    """One paper order as the engine stored it (A1 6.3 orders.csv). Money in paise; an unset number is null."""
    order_id: int
    parent_id: int | None = None
    instrument_id: int | None = None
    side: str | None = None
    product: str | None = None
    type: str | None = None
    validity: str | None = None
    qty: int | None = None
    price_paise: int | None = None
    trigger_paise: int | None = None
    tag: str | None = None
    t_submit_ns: int | None = None
    t_activate_ns: int | None = None
    status: str | None = None
    filled_qty: int | None = None
    avg_price_paise: int | None = None
    reason: str | None = None


class SessionFillDTO(BaseModel):
    """One paper fill as the engine stored it (A1 6.3 fills.csv); `t_ns` is UTC epoch nanoseconds."""
    fill_id: int
    order_id: int | None = None
    t_ns: int | None = None
    instrument_id: int | None = None
    side: str | None = None
    qty: int | None = None
    price_paise: int | None = None
    evidence: str | None = None


class TradeLoginDTO(BaseModel):
    """The owner's Kite login window for the next session, on the owner's own clock."""
    waiting_for_login: bool
    day: str
    window_local: list[str]
    last_usable_local: str
    market_open_local: str


# ── Ticks (WS payloads) ──────────────────────────────────────────────────────

class TickEvent(BaseModel):
    type: Literal["tick"]
    token: int
    ltp: float
    change_pct: float | None = None
    ts: int  # unix seconds

# ── Research (M6.4 deflated Sharpe, M6.8 bootstrap; WF 6.1) ──────────────────

class IntervalDTO(BaseModel):
    """A point estimate with its bootstrap interval; any part is None when it could not be computed."""
    point: float | None = None
    lo: float | None = None
    hi: float | None = None


class DsrDTO(BaseModel):
    """The deflated Sharpe of a try (per-day Sharpe; the page annualises). `dsr` and `psr` are probabilities."""
    sr: float | None = None
    t: int
    skew: float | None = None
    kurt: float | None = None
    n_trials: int
    var_sharpe: float | None = None
    sr0: float | None = None
    dsr: float | None = None
    psr: float | None = None


class BootstrapDTO(BaseModel):
    """Stationary block bootstrap intervals at level `ci`; the mean return is a per-day fraction, the drawdown a
    fraction of the peak."""
    n_days: int
    mean_block: float | None = None
    resamples: int
    ci: float | None = None
    enough_days: bool
    sharpe: IntervalDTO
    mean_daily_return: IntervalDTO
    max_drawdown: IntervalDTO


class RegimeBucketDTO(BaseModel):
    """One group of days: P&L in paise (`mean_pnl_paise` rounded half away from zero), `thin` when it has too few
    days to read much into."""
    label: str
    days: int
    pnl_paise: int
    mean_pnl_paise: int | None = None
    up_days: int
    up_share: float | None = None
    thin: bool
    plain: str | None = None


class RegimeReportDTO(BaseModel):
    month: list[RegimeBucketDTO]
    weekday: list[RegimeBucketDTO]
    expiry: list[RegimeBucketDTO]
    vix: list[RegimeBucketDTO]
    gap: list[RegimeBucketDTO]
    vix_source: dict[str, str]
    notes: list[str]


class RunResearchDTO(BaseModel):
    run_id: str
    family_id: str | None = None
    role: str
    objective: str
    n_trials: int
    rank: int | None = None
    is_best: bool
    picked_run_id: str | None = None
    picked_name: str | None = None
    dsr: DsrDTO | None = None
    bootstrap: BootstrapDTO | None = None
    regimes: RegimeReportDTO | None = None
    regimes_error: str | None = None
    holdout_unlocks: int
    plain: dict[str, str]


class SensitivityRowDTO(BaseModel):
    key: str
    axis: str
    label: str
    run_id: str | None = None
    status: str
    net_pnl_paise: int | None = None
    sharpe: float | None = None
    delta_net_paise: int | None = None
    error: str | None = None


class SensitivityDTO(BaseModel):
    run_id: str
    job_id: str
    rows: list[SensitivityRowDTO]
    baseline_net_paise: int
    fragile: bool
    verdict: str
    reasons: list[str]
    plain: str


class TrialRowDTO(BaseModel):
    run_id: str
    name: str | None = None
    name_label: str = ""
    role: str
    status: str
    objective_value: float | None = None
    sharpe_per_period: float | None = None
    net_pnl_paise: int | None = None
    days: int
    job_id: str | None = None


class PboDTO(BaseModel):
    pbo: float
    n_trials: int
    n_days: int
    slices: int
    combinations: int
    rows_dropped: int
    median_logit: float | None = None
    prob_loss: float


class FamilySummaryDTO(BaseModel):
    family_id: str
    title: str = ""                 # what the page calls the family: its description, else its id read as words
    description: str | None = None
    named: bool
    objective: str
    n_trials: int
    n_other_runs: int
    n_sessions: int
    best_run_id: str | None = None
    best_dsr: float | None = None
    unlocks: int
    n_finished: int = 0
    best_plain: str = ""


class FamilyDetailDTO(BaseModel):
    family_id: str
    title: str = ""
    description: str | None = None
    objective: str
    named: bool
    n_trials: int
    var_sharpe: float | None = None
    best_run_id: str | None = None
    best: DsrDTO | None = None
    trials: list[TrialRowDTO]
    others: list[TrialRowDTO]
    sessions: list[dict[str, Any]]
    holdout_unlocks: list[dict[str, Any]]
    plain: dict[str, str]
    pbo: PboDTO | None = None
    pbo_note: str | None = None
    pbo_dropped: list[str] = []
    pbo_flat: list[str] = []


# ── Parameter sweeps (WF 6.3) ────────────────────────────────────────────────

class ResearchJobDTO(BaseModel):
    job_id: str
    kind: str
    name: str | None = None
    name_label: str = ""
    family_id: str | None = None
    status: str
    created_ns: int
    finished_ns: int | None = None
    error: str | None = None
    plain: str = ""


class ResearchCountsDTO(BaseModel):
    """How many families, sweeps and walk-forward runs exist: the totals behind the overview's capped lists."""
    families: int
    sweeps: int
    walkforwards: int


class SweepAxisDTO(BaseModel):
    path: str
    label: str                      # a plain name from the strategy's params schema, or the path when none is known
    labels: list[str]
    values: list[Any]


class SweepRowDTO(BaseModel):
    index: int
    params: dict[str, Any]
    run_id: str | None = None
    status: str
    objective_value: float | None = None
    sharpe: float | None = None
    net_pnl_paise: int | None = None
    trades: int | None = None
    max_drawdown: float | None = None
    error: str | None = None        # a short plain clause, or None when the stored error looks like a raw log line


class SweepBestCellDTO(BaseModel):
    xi: int
    yi: int
    run_id: str
    value: float


# Plain data; the page feeds HeatGrid through an adapter (format kind -> formatter, best {xi, yi} -> {x, y}).
class SweepHeatmapDTO(BaseModel):
    x: str
    y: str | None = None
    xs: list[str]
    ys: list[str]
    x_values: list[Any]
    y_values: list[Any]
    cells: list[list[float | None]]
    run_ids: list[list[str | None]]
    best: SweepBestCellDTO | None = None
    signed: bool
    format: str
    hidden_axes: list[str]


class SweepDTO(BaseModel):
    job_id: str
    name: str
    name_label: str = ""
    family_id: str
    objective: str
    objective_label: str            # the one name for the objective, as the plain sentence uses it
    axes: list[SweepAxisDTO]
    rows: list[SweepRowDTO]
    best_run_id: str | None = None
    cells: SweepHeatmapDTO
    plain: str


class CompareMetricDTO(BaseModel):
    key: str
    label: str
    values: list[float | int | None]
    kind: str
    plain: str
    reasons: list[str | None] = Field(default_factory=list, description="per run: why the value is withheld, else null")


class CompareDiffDTO(BaseModel):
    field: str                      # the stored key: a hover for the plain `label`
    values: list[str | None]        # as stored
    differs: bool
    label: str = ""                 # the field in plain words
    shown: list[str | None] = Field(default_factory=list, description="each value in plain words; the page shows these")


class CompareSeriesDTO(BaseModel):
    run_id: str
    dates: list[str]
    values: list[float | None]


class ComparisonDTO(BaseModel):
    run_ids: list[str]
    names: list[str]
    name_labels: list[str] = Field(default_factory=list, description="each name as words, same order as `names`")
    metrics: list[CompareMetricDTO]
    equity: list[CompareSeriesDTO]
    drawdown: list[CompareSeriesDTO]
    labels: dict[str, list[bool]]
    differing_labels: list[str]
    manifest_diff: list[CompareDiffDTO]
    notes: list[str]


class CoverageCellDTO(BaseModel):
    state: str
    value: float | None = None
    detail: str | None = None


class CoverageDayDTO(BaseModel):
    date: str
    trading: bool
    cells: dict[str, CoverageCellDTO]


class BarRangeDTO(BaseModel):
    instrument_id: int
    symbol: str | None = None
    interval: str
    from_ns: int
    to_ns: int
    from_ist: str
    to_ist: str
    days: int
    rows: int


class StorageDTO(BaseModel):
    landing_bytes: int
    archive_bytes: int
    refdata_bytes: int
    lab_bytes: int
    nse_bytes: int
    free_bytes: int
    total_bytes: int
    warnings: list[str]


class CoverageMatrixDTO(BaseModel):
    from_date: str
    to_date: str
    datasets: list[dict[str, str]]
    days: list[CoverageDayDTO]
    bars: list[BarRangeDTO]
    storage: StorageDTO
    recorder: RecorderStatusDTO | None = None
    errors: list[str] = []
    error_stores: list[str] = []
    earliest_date: str | None = None


class GapDTO(BaseModel):
    start_ns: int
    end_ns: int | None = None
    start_ist: str
    duration_s: float | None = None
    reason: str | None = None
    scope: str | None = None
    frames: int | None = None


class GapPageDTO(BaseModel):
    total: int
    offset: int
    limit: int
    gaps: list[GapDTO]


# ---- walk-forward report (M6.5, WF 6.6; Task 22) ----

class WalkforwardSummaryDTO(BaseModel):
    """Sharpe figures are annualised; `ratio` is out-of-sample over in-sample, absent unless in-sample is positive.
    With fewer than `oos_days_needed` out-of-sample days (research.json walk_forward.min_oos_days) the Sharpe figures,
    the ratio and `dsr` are None, here and in every window."""
    windows: int
    is_windows: int
    oos_days: int
    oos_days_needed: int
    is_sharpe: float | None = None
    oos_sharpe: float | None = None
    ratio: float | None = None
    chosen_modal_share: float | None = None
    n_trials: int
    var_sharpe: float | None = None
    dsr: DsrDTO | None = None


class WalkforwardWindowDTO(BaseModel):
    """One window. `train` and `test` hold run_id, from, to, days, net_pnl_paise (int paise) and sharpe, or are
    absent when that part did not run. Calendar-day dates, inclusive."""
    index: int
    train_from: str
    train_to: str
    test_from: str
    test_to: str
    chosen_params: dict[str, Any] | None = None
    chosen_text: str = ""           # the chosen settings in plain words
    chosen_run_id: str | None = None
    test_run_id: str | None = None
    test_status: str
    result: str = ""
    no_sharpe: str = ""
    error: str | None = None
    train: dict[str, Any] | None = None
    test: dict[str, Any] | None = None


class WalkforwardDTO(BaseModel):
    job_id: str
    name: str
    name_label: str = ""
    family_id: str
    objective: str
    train: int
    test: int
    step: int
    summary: WalkforwardSummaryDTO
    windows: list[WalkforwardWindowDTO]
    stitched_dates: list[str]
    stitched_index: list[float]
    plain: dict[str, str]


# ── Run chart (CH 6.11) ──────────────────────────────────────────────────────

class ChartInstrumentDTO(BaseModel):
    id: int
    symbol: str
    kind: Literal["equity", "future", "option", "index"] | None = None


class ChartBarDTO(BaseModel):
    t: int
    d: str
    open: str
    high: str
    low: str
    close: str
    volume: int


class ChartChunkDTO(BaseModel):
    qty: int
    open_price_paise: int
    pnl_paise: int


class ChartMarkerDTO(BaseModel):
    id: str
    kind: Literal["fill", "settlement"]
    t: int
    bar_t: int
    bar_d: str
    side: Literal["BUY", "SELL"]
    qty: int
    price: str
    price_paise: int
    fsp_paise: int | None = None
    order_id: int | None = None
    fill_id: int | None = None
    note: str
    tag: str
    tag_label: str = ""
    tag_is_code: bool
    charges_paise: int | None = None
    trade_pnl_paise: int | None = None
    chunks: list[ChartChunkDTO]


class ChartPointDTO(BaseModel):
    t: int
    value: str


class ChartPlotDTO(BaseModel):
    name: str
    label: str = ""                 # the line's name in plain words, for a legend
    kind: Literal["price", "level"]
    instrument_id: int
    points: list[ChartPointDTO]


class RunChartDTO(BaseModel):
    instruments: list[ChartInstrumentDTO]
    instrument_id: int | None = None
    basis: str | None = None
    interval_s: int | None = None
    bars: list[ChartBarDTO]
    markers: list[ChartMarkerDTO]
    plots: list[ChartPlotDTO]
    truncated: bool
    next_from_ns: int | None = None
    data_state: Literal["ok", "pruned", "no_pack", "pack unreadable"]
    omitted_markers: int
    notice: str | None = None
    pnl_complete: bool
    skipped_settlements: int
    idle_settlements: int
    plain: str | None = None


# ── Walk-forward chart (CH 6.12) ─────────────────────────────────────────────

class WalkforwardChartWindowDTO(BaseModel):
    """One window's test range: UTC seconds `from_t` (inclusive) to `to_t` (exclusive), the IST dates `from_d` and
    `to_d` (inclusive), and the test run drawn there (None when it has no chart: see `status`). `result` and `no_sharpe`
    are the walk-forward report's own words for the window; `excluded` is the reason a window the summary leaves out
    (no stitched test) is not drawn, else None. `session_open_s`: seconds after IST midnight at which the cash (CM)
    session opens on the window's first day (sessions.json, dated), where the page puts the shading point of a day with
    no bars; None when the config has none."""
    index: int
    from_t: int
    to_t: int
    from_d: str
    to_d: str
    run_id: str | None = None
    status: Literal["done", "failed", "skipped", "pending", "missing", "no_trades", "unknown"]
    result: str = ""
    no_sharpe: str = ""
    excluded: str | None = None
    session_open_s: int | None = None


class WalkforwardChartDTO(RunChartDTO):
    windows: list[WalkforwardChartWindowDTO]


class SessionChartDTO(RunChartDTO):
    trade_date: str
    days: list[str]
    paper: bool
    live: bool

# ── Jobs the web app starts (D-145): a sweep, a walk-forward, a cost check, an NSE download, a quality report ──

class WebJobDTO(BaseModel):
    """A job's state. `job_id` is the research job's id once it has one (the sweep's own page is named by it), else a
    temporary id; `target` is what it works on (a run id, a sweep id, a date range, a date). `done` and `total` count
    runs, or files for a download (None when not known); `plain` is the one sentence the page shows; `error` is a plain
    reason, never exception text."""
    job_id: str
    kind: Literal["sweep", "walkforward", "sensitivity", "backfill", "report", "geo"]
    status: Literal["running", "done", "failed", "interrupted", "cancelled"]
    target: str
    done: int | None = None
    total: int | None = None
    plain: str
    error: str | None = None
    stopping: bool = False


class SweepRequestDTO(BaseModel):
    """Start a sweep from a finished test: `axes` maps `strategy.params.<name>` to the values to try. Decimals are
    strings ('0.05'); a list may hold at most 100 values and there may be at most 8 axes."""
    base_run_id: str = Field(..., max_length=128)
    name: str | None = Field(None, max_length=40)
    axes: dict[str, Annotated[list[Any], Field(min_length=1, max_length=100)]] = Field(..., min_length=1, max_length=8)
    objective: Literal["sharpe", "sortino", "calmar", "cagr", "net_pnl"] = "sharpe"
    confirm: bool = Field(False, description="required to run more tries than the limit that asks first")


class WalkforwardRequestDTO(BaseModel):
    """Walk a finished sweep forward. Unset sizes and objective come from research.json; the span defaults to the
    sweep's own dates."""
    sweep_job_id: str = Field(..., max_length=64)
    train_days: int | None = Field(None, ge=1, le=10000)
    test_days: int | None = Field(None, ge=1, le=10000)
    step_days: int | None = Field(None, ge=1, le=10000)
    objective: Literal["sharpe", "sortino", "calmar", "cagr", "net_pnl"] | None = None
    first_day: str | None = Field(None, max_length=10)
    last_day: str | None = Field(None, max_length=10)


class BackfillRequestDTO(BaseModel):
    first_day: str = Field(..., max_length=10, description="IST date YYYY-MM-DD")
    last_day: str = Field(..., max_length=10, description="IST date YYYY-MM-DD")


class QualityReportRequestDTO(BaseModel):
    day: str = Field(..., max_length=10, description="IST date YYYY-MM-DD")


class ResearchLimitsDTO(BaseModel):
    """What the sweep and walk-forward forms need from research.json: the most tries one sweep may have, the count
    above which the form asks for a tick, the walk-forward defaults, and the first day that is held back."""
    max_trials: int
    confirm_above: int
    train_days: int
    test_days: int
    step_days: int
    wf_objective: str
    holdout_from: str | None = None

# ── Holdings desk (HD 9, 12): verdict chips, one holding's view, as of the latest close. Never the owner's cost or P&L. ──

class DeskChipDTO(BaseModel):
    symbol: str
    exchange: str
    instrument_id: int
    verdict: str
    as_of_ns: int                           # when the nightly job stored the view, UTC epoch ns
    close_date: str | None = None           # the IST date of the close the view is from
    holding_symbol: str | None = None       # the Kite holding's own tradingsymbol (a BSE holding's may differ from NSE's)
    holding_exchange: str | None = None     # the Kite holding's exchange; None for a stored view shown while logged out


class DeskSkipDTO(BaseModel):
    symbol: str
    reason: str


class DeskListDTO(BaseModel):
    items: list[DeskChipDTO]
    skipped: list[DeskSkipDTO]
    plain: str | None = None


class DeskSignalDTO(BaseModel):
    display_name: str
    in_position: bool
    buying_today: bool
    exit_level_paise: int | None = None
    grade: Literal["strong", "some", "weak", "none"]
    trades: int | None = None
    windows_won: int | None = None
    windows_total: int | None = None
    cagr: float | None = None
    buy_hold_cagr: float | None = None
    note: str


class DeskViewDTO(BaseModel):
    symbol: str
    instrument_id: int
    verdict: str
    exit_share: float
    nearest_exit_paise: int | None = None
    price_paise: int
    counts: str
    reasons: list[str]
    agree: list[str]
    disagree: list[str]
    signals: list[DeskSignalDTO]
    evidence_from: str | None = None
    evidence_to: str | None = None
    as_of_ns: int
    close_date: str | None = None
    plain: str

# ── Fundamentals screener (FS 9): the ordered list, one stock's details, the test record. No score, rank or place
#    field anywhere: the list's order is the judgement (owner, 2026-11-17). ──

class ResultsDateDTO(BaseModel):
    symbol: str
    company: str
    date: str                               # IST date of the board meeting, YYYY-MM-DD
    period_hint: str                        # "Jul–Sep quarter": the period the results most likely cover


class CandidateDTO(BaseModel):
    position: int                           # 1 = first; orders the list, never printed
    symbol: str
    company: str
    sector: str | None = None
    short_reason: str


class FundamentalsListDTO(BaseModel):
    as_of: str | None = None                # IST date the list was computed for; None before the first run
    upcoming: list[ResultsDateDTO]
    items: list[CandidateDTO]
    held_count: int                         # universe stocks not on the list (no usable results, no price, ...)
    test_record_id: str | None = None
    notes: list[str]


class RatioDTO(BaseModel):
    key: str
    label: str
    value: float | None = None
    sector_median: float | None = None
    unit: Literal["times", "fraction"]      # fraction: 0.153 is 15.3 %


class SeriesPointDTO(BaseModel):
    period_end: str
    value: int | float | None = None        # int paise for unit "paise"
    sector_median: int | float | None = None


class SeriesDTO(BaseModel):
    key: str
    label: str
    unit: Literal["paise", "times", "fraction"]
    points: list[SeriesPointDTO]            # oldest quarter first


class FilingDTO(BaseModel):
    period_end: str
    period_kind: str                        # Q | H | A
    basis: str                              # consolidated | standalone
    broadcast_ist: str | None = None        # "2025-11-09 20:00", IST
    xbrl_url: str | None = None             # an NSE https link, else None
    status: str                             # listed | downloaded | parsed | held
    reason: str | None = None


class CandidateDetailDTO(BaseModel):
    symbol: str
    company: str
    sector: str | None = None
    peer_group: str | None = None           # "Industry: Capital Goods"; None when the stock is not on the list
    left_out_reason: str | None = None      # why the stock is not on the list; None when it is
    reasons: list[str]
    ratios: list[RatioDTO]
    series: list[SeriesDTO]
    filings: list[FilingDTO]                # newest period first
    as_of: str | None = None


class FundamentalsTestRecordDTO(BaseModel):
    available: bool
    message: str | None = None              # why it is not available, in plain words
    job_id: str | None = None
    summary: WalkforwardSummaryDTO | None = None
    plain: dict[str, str] | None = None     # the walk-forward report's plain lines
    benchmark: str | None = None
    notes: list[str]
# </block>

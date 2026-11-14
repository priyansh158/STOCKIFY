/**
 * TypeScript mirror of `server/schemas.py`. Keep these in sync — when a Pydantic
 * model on the backend changes shape, update the matching interface here.
 *
 * Single source of truth: the FastAPI OpenAPI doc at /api/docs and
 * /openapi.json. Future: codegen these from openapi.json instead of hand-typing.
 * blockwatch checks that both files list the same field names in the same order.
 */

// <block name="dto-fields" same-as="server/schemas.py:dto-fields" same-as-mode="sequence" same-as-pattern='^\s*(?P<value>[a-z_0-9]+)\??: '>
// ── Search ───────────────────────────────────────────────────────────────────

export interface SearchHit {
  ticker: string;
  name: string;
  exchange: "NSE" | "BSE";
  sector?: string | null;
  instrument_type: string;
  source: "kite" | "yfinance";
  instrument_token?: number | null;
}

// ── Portfolio ────────────────────────────────────────────────────────────────

export interface Holding {
  symbol: string;
  exchange: string;
  quantity: number;
  average_price: number;
  last_price: number;
  pnl: number;
  pnl_pct: number;
  instrument_token?: number;
}

export interface PortfolioSnapshot {
  holdings: Holding[];
  total_value: number;
  total_pnl: number;
  fetched_at: number;
  stale?: boolean;
  error?: string | null;
}

// ── Kite ─────────────────────────────────────────────────────────────────────

export interface KiteStatus {
  configured: boolean;
  authenticated: boolean;
  daemon_waiting_for_login: boolean;
  error?: string | null;
}

// ── Geo alerts ───────────────────────────────────────────────────────────────

export interface GeoAlert {
  id: number;
  created_at: number;
  severity: string;
  disruption_score: number;
  scope?: string | null;
  title: string;
  source?: string | null;
  url?: string | null;
  published_at?: string | null;
  guid?: string | null;
  why_disruptive?: string | null;
  investor_action?: string | null;
  confidence?: number | null;
  affected_tickers: string[];
  emailed_at?: number | null;
  acknowledged_at?: number | null;
}

// ── Data layer (M1) ──────────────────────────────────────────────────────────

export interface RecorderStatus {
  wall_ns: number;
  connected: boolean;
  subs_full: number;
  subs_quote: number;
  packets_per_s: number;
  queue_depth: number;
  writer_lag_ms?: number | null;
  last_packet_age_ms: Record<string, number | null>;
  bytes_today: number;
  age_s: number;
  stale: boolean;
  market_open: boolean;
}

export interface RecorderStatusResponse {
  status: RecorderStatus | null;
}

export interface InstrumentQuality {
  instrument_id: number;
  symbol?: string | null;
  metrics: Record<string, number>;
}

export interface DataQualityResponse {
  date: string;
  day: Record<string, number>;
  instruments: InstrumentQuality[];
  errors: string[];
  error_stores: string[];
}

// ── Strategy tests (M3.14) ───────────────────────────────────────────────────
// Money is integer paise, times are UTC epoch ns with an ISO IST string beside them.

export type ParamType = "int" | "bool" | "str" | "decimal" | "symbol" | "hhmm" | "underlying";

export interface StrategyParam {
  name: string;
  label: string;
  type: ParamType;
  default?: unknown;
  required: boolean;
  help: string;
}

export interface Strategy {
  kind: "python" | "cpp";
  ref: string;
  name: string;
  display_name: string;
  doc: string;
  params: StrategyParam[];
}

export type TestStatus = "queued" | "running" | "done" | "failed" | "cancelled";

export interface TestRun {
  run_id: string;
  name?: string | null;
  name_label: string;
  family_id?: string | null;
  status: TestStatus;
  impl?: string | null;
  strategy_ref?: string | null;
  strategy_name?: string | null;
  start_date?: string | null;
  end_date?: string | null;
  price_basis?: string | null;
  capital_paise?: number | null;
  created_ns?: number | null;
  created_ist?: string | null;
  finished_ns?: number | null;
  finished_ist?: string | null;
  nav_start_paise?: number | null;
  nav_end_paise?: number | null;
  max_drawdown?: number | null;
  trades?: number | null;
  costs_total_paise?: number | null;
  labels: string[];
  error?: string | null;
}

export interface TestRunDetail {
  run: TestRun;
  config: Record<string, unknown>;
  metrics: Record<string, unknown>;
  manifest?: Record<string, unknown> | null;
  manifest_labels: Record<string, string>;
  fill_count?: number | null;
  cost_totals: Record<string, number>;
  /** metrics key -> plain reason the figure is not shown (its value in `metrics` is null) */
  withheld?: Record<string, string>;
}

export interface TestFill {
  fill_id: number;
  order_id: number;
  t_ns: number;
  t_ist: string;
  instrument_id: number;
  tradingsymbol?: string | null;
  side: string;
  qty: number;
  price_paise: number;
  note: string;
  tag: string;
  tag_label: string;
  evidence: string;
  at_close: boolean;
}

export interface TestEquity {
  trade_date: string;
  nav_paise: number;
  cash_paise: number;
  margin_paise?: number | null;
}

export interface TestCharge {
  row_no: number;
  kind: string;
  order_id?: number | null;
  trade_date: string;
  components: Record<string, number>;
  total_paise: number;
}

export interface TestMargin {
  trade_date: string;
  underlying_id: number;
  underlying?: string | null;
  model: string;
  step?: string;
  scan_paise: number;
  somc_paise: number;
  elm_paise: number;
  addons_paise: number;
  total_paise: number;
}

export interface TestSettlement {
  t_ns: number;
  t_ist: string;
  trade_date: string;
  kind: "cash" | "delivery";
  instrument_id: number;
  tradingsymbol?: string | null;
  product: string;
  side?: string | null;
  qty?: number | null;
  price_paise: number;
  source: string;
  charges_paise?: number | null;
}

export interface TestGreek {
  trade_date: string;
  instrument_id: number;
  tradingsymbol?: string | null;
  underlying?: string | null;
  kind: string;
  strike_paise?: number | null;
  expiry?: string | null;
  qty: number;
  mark_paise: number;
  underlying_paise?: number | null;
  iv?: number | null;
  iv_reason?: string | null;
  delta_units?: number | null;
  gamma_units_per_rupee?: number | null;
  vega_rupees_per_point?: number | null;
  theta_rupees_per_day?: number | null;
  expiry_close_estimated: boolean;
}

export interface TestLogs {
  events_log: string[];
  runner_log: string[];
  offset: number;
  total_lines: number;
}

export interface TestCreated {
  run_id: string;
}

export interface SessionDTO {
  session_id: string;
  name: string;
  name_label: string;
  status: string;
  pause_reasons: string[];
  strategy_name?: string | null;
  today_pnl_paise?: number | null;
  open_positions?: number | null;
  coverage_today?: number | null;
  parity_last?: string | null;
  labels: string[];
  created_ist: string;
}

export interface SessionStatsDTO {
  days: number;
  pnl_paise: number;
  win_days: number;
  loss_days: number;
  best_day_paise?: number | null;
  worst_day_paise?: number | null;
  max_drawdown_paise: number;
}

export interface SessionDetailDTO {
  session: SessionDTO;
  config: Record<string, unknown>;
  trade_date?: string | null;
  stats_all: SessionStatsDTO;
  stats_full_coverage: SessionStatsDTO;
  coverage_threshold: string;
  has_coverage_holes: boolean;
}

export interface SessionCreatedDTO {
  session_id: string;
}

export interface SessionCommandDTO {
  cmd_id: number;
}

export interface CommandStatusDTO {
  cmd_id: number;
  session_id?: string | null;
  command: string;
  status: "pending" | "applied" | "failed";
  created_ns: number;
  created_ist: string;
  applied_ns?: number | null;
  applied_ist?: string | null;
  error?: string | null;
}

export interface SessionPositionDTO {
  trade_date: string;
  instrument_id: number;
  instrument_token?: number | null;
  tradingsymbol?: string | null;
  product?: string | null;
  qty: number;
  avg_price_paise?: number | null;
  mark_paise?: number | null;
  unrealised_paise?: number | null;
  source: "eod" | "fills";
  names_note?: string | null;
}

export interface SessionTimelineDTO {
  engine_ns?: number | null;
  wall_ns: number;
  wall_ist: string;
  kind: string;
  detail?: unknown;
}

export interface SessionMarginDTO {
  wall_ns: number;
  wall_ist: string;
  trade_date?: string | null;
  step?: string | null;
  kite_total_paise?: number | null;
  ours_total_paise?: number | null;
  gap_paise?: number | null;
  gap_ppm?: number | null;
  error?: string | null;
}

export interface SessionOrderDTO {
  order_id: number;
  parent_id?: number | null;
  instrument_id?: number | null;
  side?: string | null;
  product?: string | null;
  type?: string | null;
  validity?: string | null;
  qty?: number | null;
  price_paise?: number | null;
  trigger_paise?: number | null;
  tag?: string | null;
  t_submit_ns?: number | null;
  t_activate_ns?: number | null;
  status?: string | null;
  filled_qty?: number | null;
  avg_price_paise?: number | null;
  reason?: string | null;
}

export interface SessionFillDTO {
  fill_id: number;
  order_id?: number | null;
  t_ns?: number | null;
  instrument_id?: number | null;
  side?: string | null;
  qty?: number | null;
  price_paise?: number | null;
  evidence?: string | null;
}

export interface TradeLoginDTO {
  waiting_for_login: boolean;
  day: string;
  window_local: string[];
  last_usable_local: string;
  market_open_local: string;
}

// ── Live ticks (WS) ──────────────────────────────────────────────────────────

export interface TickEvent {
  type: "tick";
  token: number;
  ltp: number;
  change_pct?: number;
  ts: number;
}

// ── Research (M6.4 deflated Sharpe, M6.8 bootstrap; WF 6.1) ──────────────────

export interface IntervalDTO {
  point: number | null;
  lo: number | null;
  hi: number | null;
}

export interface DsrDTO {
  sr: number | null;
  t: number;
  skew: number | null;
  kurt: number | null;
  n_trials: number;
  var_sharpe: number | null;
  sr0: number | null;
  dsr: number | null;
  psr: number | null;
}

export interface BootstrapDTO {
  n_days: number;
  mean_block: number | null;
  resamples: number;
  ci: number | null;
  enough_days: boolean;
  sharpe: IntervalDTO;
  mean_daily_return: IntervalDTO;
  max_drawdown: IntervalDTO;
}

export interface RegimeBucketDTO {
  label: string;
  days: number;
  pnl_paise: number;
  mean_pnl_paise: number | null;
  up_days: number;
  up_share: number | null;
  thin: boolean;
  plain?: string | null;
}

export interface RegimeReportDTO {
  month: RegimeBucketDTO[];
  weekday: RegimeBucketDTO[];
  expiry: RegimeBucketDTO[];
  vix: RegimeBucketDTO[];
  gap: RegimeBucketDTO[];
  vix_source: Record<string, string>;
  notes: string[];
}

export interface RunResearchDTO {
  run_id: string;
  family_id: string | null;
  role: string;
  objective: string;
  n_trials: number;
  rank: number | null;
  is_best: boolean;
  picked_run_id: string | null;
  picked_name: string | null;
  dsr: DsrDTO | null;
  bootstrap: BootstrapDTO | null;
  regimes: RegimeReportDTO | null;
  regimes_error: string | null;
  holdout_unlocks: number;
  plain: Record<string, string>;
}

export interface SensitivityRowDTO {
  key: string;
  axis: string;
  label: string;
  run_id: string | null;
  status: string;
  net_pnl_paise: number | null;
  sharpe: number | null;
  delta_net_paise: number | null;
  error: string | null;
}

export interface SensitivityDTO {
  run_id: string;
  job_id: string;
  rows: SensitivityRowDTO[];
  baseline_net_paise: number;
  fragile: boolean;
  verdict: string;
  reasons: string[];
  plain: string;
}

export interface TrialRowDTO {
  run_id: string;
  name: string | null;
  name_label: string;
  role: string;
  status: string;
  objective_value: number | null;
  sharpe_per_period: number | null;
  net_pnl_paise: number | null;
  days: number;
  job_id: string | null;
}

export interface PboDTO {
  pbo: number;
  n_trials: number;
  n_days: number;
  slices: number;
  combinations: number;
  rows_dropped: number;
  median_logit: number | null;
  prob_loss: number;
}

export interface FamilySummaryDTO {
  family_id: string;
  title: string;
  description: string | null;
  named: boolean;
  objective: string;
  n_trials: number;
  n_other_runs: number;
  n_sessions: number;
  best_run_id: string | null;
  best_dsr: number | null;
  unlocks: number;
  n_finished: number;
  best_plain: string;
}

export interface FamilyDetailDTO {
  family_id: string;
  title: string;
  description: string | null;
  objective: string;
  named: boolean;
  n_trials: number;
  var_sharpe: number | null;
  best_run_id: string | null;
  best: DsrDTO | null;
  trials: TrialRowDTO[];
  others: TrialRowDTO[];
  sessions: Record<string, unknown>[];
  holdout_unlocks: Record<string, unknown>[];
  plain: Record<string, string>;
  pbo: PboDTO | null;
  pbo_note: string | null;
  pbo_dropped: string[];
  pbo_flat: string[];
}

// ── Parameter sweeps (WF 6.3) ────────────────────────────────────────────────

export interface ResearchJobDTO {
  job_id: string;
  kind: string;
  name: string | null;
  name_label: string;
  family_id: string | null;
  status: string;
  created_ns: number;
  finished_ns: number | null;
  error: string | null;
  plain: string;
}

export interface ResearchCountsDTO {
  families: number;
  sweeps: number;
  walkforwards: number;
}

export interface SweepAxisDTO {
  path: string;
  label: string;
  labels: string[];
  values: (string | number | boolean | null)[];
}

export interface SweepRowDTO {
  index: number;
  params: Record<string, string | number | boolean | null>;
  run_id: string | null;
  status: string;
  objective_value: number | null;
  sharpe: number | null;
  net_pnl_paise: number | null;
  trades: number | null;
  max_drawdown: number | null;
  error: string | null;
}

export interface SweepBestCellDTO {
  xi: number;
  yi: number;
  run_id: string;
  value: number;
}

/** Plain data for the heatmap; the page feeds HeatGrid through an adapter: xs, ys and cells (cells[yi][xi], null for a
 * missing or failed trial) pass as they are, `format` (a kind) becomes a formatter, `best` {xi, yi} becomes {x, y}. */
export interface SweepHeatmapDTO {
  x: string;
  y: string | null;
  xs: string[];
  ys: string[];
  x_values: (string | number | boolean | null)[];
  y_values: (string | number | boolean | null)[];
  cells: (number | null)[][];
  run_ids: (string | null)[][];
  best: SweepBestCellDTO | null;
  signed: boolean;
  format: string;
  hidden_axes: string[];
}

export interface SweepDTO {
  job_id: string;
  name: string;
  name_label: string;
  family_id: string;
  objective: string;
  objective_label: string;
  axes: SweepAxisDTO[];
  rows: SweepRowDTO[];
  best_run_id: string | null;
  cells: SweepHeatmapDTO;
  plain: string;
}

export interface CompareMetricDTO {
  key: string;
  label: string;
  values: (number | null)[];
  kind: string;
  plain: string;
  reasons?: (string | null)[];
}

export interface CompareDiffDTO {
  field: string;
  values: (string | null)[];
  differs: boolean;
  label: string;
  shown: (string | null)[];
}

export interface CompareSeriesDTO {
  run_id: string;
  dates: string[];
  values: (number | null)[];
}

export interface ComparisonDTO {
  run_ids: string[];
  names: string[];
  name_labels: string[];
  metrics: CompareMetricDTO[];
  equity: CompareSeriesDTO[];
  drawdown: CompareSeriesDTO[];
  labels: Record<string, boolean[]>;
  differing_labels: string[];
  manifest_diff: CompareDiffDTO[];
  notes: string[];
}


export interface CoverageCellDTO {
  state: string;
  value?: number | null;
  detail?: string | null;
}

export interface CoverageDayDTO {
  date: string;
  trading: boolean;
  cells: Record<string, CoverageCellDTO>;
}

export interface BarRangeDTO {
  instrument_id: number;
  symbol?: string | null;
  interval: string;
  from_ns: number;
  to_ns: number;
  from_ist: string;
  to_ist: string;
  days: number;
  rows: number;
}

export interface StorageDTO {
  landing_bytes: number;
  archive_bytes: number;
  refdata_bytes: number;
  lab_bytes: number;
  nse_bytes: number;
  free_bytes: number;
  total_bytes: number;
  warnings: string[];
}

export interface CoverageMatrixDTO {
  from_date: string;
  to_date: string;
  datasets: Record<string, string>[];
  days: CoverageDayDTO[];
  bars: BarRangeDTO[];
  storage: StorageDTO;
  recorder?: RecorderStatus | null;
  errors: string[];
  error_stores: string[];
  earliest_date?: string | null;
}

export interface GapDTO {
  start_ns: number;
  end_ns?: number | null;
  start_ist: string;
  duration_s?: number | null;
  reason?: string | null;
  scope?: string | null;
  frames?: number | null;
}

export interface GapPageDTO {
  total: number;
  offset: number;
  limit: number;
  gaps: GapDTO[];
}

// ---- walk-forward report (M6.5, WF 6.6; Task 22) ----

export interface WalkforwardSummaryDTO {
  windows: number;
  is_windows: number;
  oos_days: number;
  oos_days_needed: number;
  is_sharpe?: number | null;
  oos_sharpe?: number | null;
  ratio?: number | null;
  chosen_modal_share?: number | null;
  n_trials: number;
  var_sharpe?: number | null;
  dsr?: DsrDTO | null;
}

export interface WalkforwardWindowDTO {
  index: number;
  train_from: string;
  train_to: string;
  test_from: string;
  test_to: string;
  chosen_params?: Record<string, unknown> | null;
  chosen_text: string;
  chosen_run_id?: string | null;
  test_run_id?: string | null;
  test_status: string;
  result: string;
  no_sharpe: string;
  error?: string | null;
  train?: Record<string, unknown> | null;
  test?: Record<string, unknown> | null;
}

export interface WalkforwardDTO {
  job_id: string;
  name: string;
  name_label: string;
  family_id: string;
  objective: string;
  train: number;
  test: number;
  step: number;
  summary: WalkforwardSummaryDTO;
  windows: WalkforwardWindowDTO[];
  stitched_dates: string[];
  stitched_index: number[];
  plain: Record<string, string>;
}


// ── Run chart (CH 6.11): times are UTC seconds, prices rupee decimal strings, paise fields integers ─────────

export interface ChartInstrumentDTO {
  id: number;
  symbol: string;
  kind: "equity" | "future" | "option" | "index" | null;
}

export interface ChartBarDTO {
  t: number;
  d: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: number;
}

export interface ChartChunkDTO {
  qty: number;
  open_price_paise: number;
  pnl_paise: number;
}

export interface ChartMarkerDTO {
  id: string;
  kind: "fill" | "settlement";
  t: number;
  bar_t: number;
  bar_d: string;
  side: "BUY" | "SELL";
  qty: number;
  price: string;
  price_paise: number;
  fsp_paise: number | null;
  order_id: number | null;
  fill_id: number | null;
  note: string;
  tag: string;
  tag_label: string;
  tag_is_code: boolean;
  charges_paise: number | null;
  trade_pnl_paise: number | null;
  chunks: ChartChunkDTO[];
}

export interface ChartPointDTO {
  t: number;
  value: string;
}

export interface ChartPlotDTO {
  name: string;
  label: string;
  kind: "price" | "level";
  instrument_id: number;
  points: ChartPointDTO[];
}

export interface RunChartDTO {
  instruments: ChartInstrumentDTO[];
  instrument_id: number | null;
  basis: string | null;
  interval_s: number | null;
  bars: ChartBarDTO[];
  markers: ChartMarkerDTO[];
  plots: ChartPlotDTO[];
  truncated: boolean;
  next_from_ns: number | null;
  data_state: "ok" | "pruned" | "no_pack" | "pack unreadable";
  omitted_markers: number;
  notice: string | null;
  pnl_complete: boolean;
  skipped_settlements: number;
  idle_settlements: number;
  plain: string | null;
}

export interface WalkforwardChartWindowDTO {
  index: number;
  from_t: number;
  to_t: number;
  from_d: string;
  to_d: string;
  run_id: string | null;
  status: "done" | "failed" | "skipped" | "pending" | "missing" | "no_trades" | "unknown";
  result: string;
  no_sharpe: string;
  excluded: string | null;
  session_open_s: number | null;
}

export interface WalkforwardChartDTO extends RunChartDTO {
  windows: WalkforwardChartWindowDTO[];
}

export interface SessionChartDTO extends RunChartDTO {
  trade_date: string;
  days: string[];
  paper: boolean;
  live: boolean;
}
// </block>

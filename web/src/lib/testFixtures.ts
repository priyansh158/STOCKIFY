/** Typed sample data for the strategy-test pages (NEXT_PUBLIC_STOCKIFY_FIXTURES=1). Not real results. */
import type {
  Strategy,
  TestCharge,
  TestEquity,
  TestFill,
  TestGreek,
  TestLogs,
  TestMargin,
  TestRun,
  TestRunDetail,
  TestSettlement,
} from "./types";

export const strategies: Strategy[] = [
  {
    kind: "python",
    ref: "stockify.strategies.sma_cross:SmaCross",
    name: "sma_cross",
    doc: "SMA crossover (M2.4): long when the fast SMA is above the slow one.",
    params: [
      { name: "symbol", label: "Stock", type: "symbol", default: null, required: true, help: "what to trade, e.g. NSE:RELIANCE" },
      { name: "fast", label: "Fast average (bars)", type: "int", default: null, required: true, help: "bars in the fast moving average" },
      { name: "slow", label: "Slow average (bars)", type: "int", default: null, required: true, help: "bars in the slow moving average" },
      { name: "interval_s", label: "Bar length (seconds)", type: "int", default: 86400, required: false, help: "bar length in seconds: 86400 daily, 60 minute" },
      { name: "long_short", label: "Also go short", type: "bool", default: false, required: false, help: "go short when fast < slow (needs a future traded NRML)" },
      { name: "product", label: "Product (CNC or NRML)", type: "str", default: "CNC", required: false, help: "CNC for stocks, NRML for futures" },
      { name: "lots", label: "Lots per position", type: "int", default: 1, required: false, help: "futures only: lots per position" },
    ],
  },
  {
    kind: "python",
    ref: "stockify.strategies.opening_range_breakout:OpeningRangeBreakout",
    name: "opening_range_breakout",
    doc: "Opening-range breakout on minute bars, MIS (M2.4).",
    params: [
      { name: "symbol", label: "Stock", type: "symbol", default: null, required: true, help: "the stock to trade intraday, e.g. NSE:RELIANCE" },
      { name: "qty", label: "Shares per trade", type: "int", default: null, required: true, help: "shares per trade" },
      { name: "range_minutes", label: "Opening range (minutes)", type: "int", default: 15, required: false, help: "minutes after 09:15 that set the range" },
      { name: "short", label: "Also sell short", type: "bool", default: false, required: false, help: "also sell short when price breaks below the range" },
      { name: "exit_hhmm", label: "Exit time (IST)", type: "hhmm", default: "15:10", required: false, help: "time to close the position, IST" },
    ],
  },
  {
    kind: "python",
    ref: "stockify.strategies.eod_short_straddle:EodShortStraddle",
    name: "eod_short_straddle",
    doc: "End-of-day NIFTY short straddle on NSE bhavcopy data (M2.4; label `eod_approx`).",
    params: [
      { name: "underlying", label: "Index for the options", type: "underlying", default: "NIFTY", required: false, help: "the index whose options are sold" },
      { name: "lots", label: "Lots per position", type: "int", default: 1, required: false, help: "lots of each option sold" },
      { name: "stop_multiple", label: "Stop at × premium", type: "decimal", default: "1.5", required: false, help: "buy back when the options cost this many times the premium" },
    ],
  },
];

const base: TestRun = {
  run_id: "sma_reliance_daily-20261106T051500-3fa9c1",
  name: "sma_reliance_daily",
  family_id: "sma_cross/RELIANCE/daily",
  status: "done",
  impl: "engine",
  strategy_ref: "stockify.strategies.sma_cross:SmaCross",
  start_date: "2022-01-03",
  end_date: "2026-09-30",
  price_basis: "adjusted",
  capital_paise: 100_000_000,
  created_ns: 1_793_934_900_000_000_000,
  created_ist: "2026-11-06T10:45:00+05:30",
  finished_ns: 1_793_934_912_000_000_000,
  finished_ist: "2026-11-06T10:45:12+05:30",
  nav_start_paise: 100_000_000,
  nav_end_paise: 131_472_550,
  max_drawdown: 0.1834,
  trades: 14,
  costs_total_paise: 1_284_311,
  labels: ["fill:bar", "margin:flat(approx)", "charges:unverified_rows"],
  error: null,
};

export const runs: TestRun[] = [
  base,
  { ...base, run_id: "orb_infy-20261106T060000-a1b2c3", name: "orb_infy", status: "running",
    strategy_ref: "stockify.strategies.opening_range_breakout:OpeningRangeBreakout", start_date: "2026-08-03",
    end_date: "2026-10-30", price_basis: "raw", nav_end_paise: null, max_drawdown: null, trades: null,
    costs_total_paise: null, finished_ns: null, finished_ist: null, labels: [] },
  { ...base, run_id: "eod_straddle_nifty-20261106T061500-0d0e0f", name: "eod_straddle_nifty", status: "queued",
    strategy_ref: "stockify.strategies.eod_short_straddle:EodShortStraddle", price_basis: "raw",
    nav_end_paise: null, max_drawdown: null, trades: null, costs_total_paise: null, finished_ns: null,
    finished_ist: null, labels: [] },
  { ...base, run_id: "sma_tcs_daily-20261106T043000-777777", name: "sma_tcs_daily", status: "failed",
    nav_end_paise: null, max_drawdown: null, trades: null, costs_total_paise: null,
    labels: ["fill:bar", "margin:flat(approx)"],
    error: "ZeroDivisionError: integer division or modulo by zero" },
  { ...base, run_id: "eod_straddle_banknifty-20261105T190000-abcdef", name: "eod_straddle_banknifty",
    strategy_ref: "stockify.strategies.eod_short_straddle:EodShortStraddle", price_basis: "raw",
    nav_end_paise: 96_120_000, max_drawdown: 0.0712, trades: 31, costs_total_paise: 3_420_118,
    labels: ["fill:eod_close", "margin:flat(approx)", "eod_approx", "settlement_fallback"] },
  { ...base, run_id: "iron_fly_nifty-20261108T040000-a1b2c3", name: "iron_fly_nifty",
    strategy_ref: "stockify.strategies.iron_fly:IronFly", price_basis: "raw",
    nav_end_paise: 101_840_000, max_drawdown: 0.0231, trades: 12, costs_total_paise: 412_660,
    labels: ["fill:snapshot_quote", "margin:staircase", "margin:B1_span_nse_elm", "margin:B4_spanlite_placeholder",
             "margin_steps:B1_span_nse_elm=1", "margin_steps:B4_spanlite_placeholder=1", "margin_spanlite:placeholder",
             "settlement_fallback", "delivery_margin_approx", "short_delivery"] },
];

const FLY = "iron_fly_nifty-20261108T040000-a1b2c3";

/** Margin by day for the sample iron fly (the margin staircase); other sample runs use the flat model and have none. */
export function margin(id: string): TestMargin[] {
  if (id !== FLY) return [];
  return [
    { trade_date: "2026-11-03", underlying_id: 256265, underlying: "NIFTY 50", model: "staircase", step: "B1_span_nse_elm", scan_paise: 9_412_300,
      somc_paise: 0, elm_paise: 3_120_000, addons_paise: 0, total_paise: 12_532_300 },
    { trade_date: "2026-11-04", underlying_id: 256265, underlying: "NIFTY 50", model: "staircase", step: "B4_spanlite_placeholder", scan_paise: 8_905_100,
      somc_paise: 0, elm_paise: 3_098_000, addons_paise: 3_098_000, total_paise: 15_101_100 },
  ];
}

/** Settlements for the sample iron fly: two legs at an estimated price, one stock delivery. */
export function settlements(id: string): TestSettlement[] {
  if (id !== FLY) return [];
  const t = 1_794_045_000_000_000_000;
  return [
    { t_ns: t, t_ist: "2026-11-04T15:40:00+05:30", trade_date: "2026-11-04", kind: "cash", instrument_id: 668,
      tradingsymbol: "NIFTY26N0424000CE", product: "NRML", side: null, qty: null, price_paise: 2_398_550,
      source: "fallback", charges_paise: 2_360 },
    { t_ns: t, t_ist: "2026-11-04T15:40:00+05:30", trade_date: "2026-11-04", kind: "cash", instrument_id: 669,
      tradingsymbol: "NIFTY26N0424000PE", product: "NRML", side: null, qty: null, price_paise: 2_398_550,
      source: "fallback", charges_paise: 8_861 },
    { t_ns: t, t_ist: "2026-11-04T15:40:00+05:30", trade_date: "2026-11-04", kind: "delivery", instrument_id: 1,
      tradingsymbol: "RELIANCE", product: "CNC", side: "BUY", qty: 500, price_paise: 141_230, source: "settle",
      charges_paise: null },
  ];
}

/** Greeks at the two day ends of the sample iron fly: four legs, one of them with no implied volatility. */
export function greeks(id: string): TestGreek[] {
  if (id !== FLY) return [];
  const leg = (d: string, iid: number, sym: string, kind: string, strike: number, qty: number, mark: number,
               iv: number | null, delta: number | null, gamma: number | null, vega: number | null,
               theta: number | null, reason: string | null = null): TestGreek => ({
    trade_date: d, instrument_id: iid, tradingsymbol: sym, underlying: "NIFTY", kind, strike_paise: strike,
    expiry: "2026-11-04", qty, mark_paise: mark, underlying_paise: 2_401_250, iv, iv_reason: reason,
    delta_units: delta, gamma_units_per_rupee: gamma, vega_rupees_per_point: vega, theta_rupees_per_day: theta,
    expiry_close_estimated: false,
  });
  return [
    leg("2026-11-03", 668, "NIFTY26N0424000CE", "CE", 2_400_000, -75, 9_215, 0.1342, -40.1, -0.0412, -611.2, 3_904.5),
    leg("2026-11-03", 669, "NIFTY26N0424000PE", "PE", 2_400_000, -75, 8_840, 0.1355, 34.7, -0.0409, -612.0, 3_861.0),
    leg("2026-11-03", 670, "NIFTY26N0424200CE", "CE", 2_420_000, 75, 2_310, 0.1288, 15.2, 0.0302, 455.7, -2_610.3),
    leg("2026-11-03", 671, "NIFTY26N0423800PE", "PE", 2_380_000, 75, 5, null, null, null, null, null, "BELOW_TICK"),
  ];
}

export const equity: TestEquity[] = Array.from({ length: 60 }, (_, i) => {
  const wave = Math.sin(i / 6) * 4_000_000 + i * 520_000 - (i > 30 && i < 40 ? (i - 30) * 900_000 : 0);
  const d = new Date(Date.UTC(2026, 7, 3 + i));
  return {
    trade_date: d.toISOString().slice(0, 10),
    nav_paise: Math.round(100_000_000 + wave),
    cash_paise: 2_000_000,
    margin_paise: 0,
  };
});

export const fills: TestFill[] = [
  { fill_id: 1, order_id: 1, t_ns: 1_662_000_000_000_000_000, t_ist: "2022-03-14T15:30:00+05:30", instrument_id: 738561, tradingsymbol: "RELIANCE",
    side: "BUY", qty: 410, price_paise: 239_105 },
  { fill_id: 2, order_id: 2, t_ns: 1_668_000_000_000_000_000, t_ist: "2022-06-20T15:30:00+05:30", instrument_id: 738561, tradingsymbol: "RELIANCE",
    side: "SELL", qty: 410, price_paise: 251_870 },
];

export const charges: TestCharge[] = [
  { row_no: 1, kind: "order", order_id: 1, trade_date: "2022-03-14",
    components: { brokerage: 0, stt: 98_033, exchange: 3_111, sebi: 98, stamp: 14_700, gst: 578, dp: 0, other: 0 },
    total_paise: 116_520 },
  { row_no: 2, kind: "dp", order_id: 2, trade_date: "2022-06-21",
    components: { brokerage: 0, stt: 0, exchange: 0, sebi: 0, stamp: 0, gst: 2_664, dp: 1_480, other: 0 },
    total_paise: 1_575 },
];

export const logs: TestLogs = {
  events_log: [
    "1662000000000000000 ERROR the strategy raised an exception; the run stops",
    "1662000000000000000 ERROR Traceback (most recent call last):",
    "1662000000000000000 ERROR ZeroDivisionError: integer division or modulo by zero",
  ],
  runner_log: ["resolving bundle", "running on the engine"],
  offset: 0,
  total_lines: 3,
};

export function detail(id: string): TestRunDetail {
  const run = runs.find((r) => r.run_id === id) ?? base;
  return {
    run,
    config: {
      schema: "stockify.run/1",
      name: run.name,
      strategy: { kind: "python", ref: run.strategy_ref, params: { symbol: "NSE:RELIANCE", fast: 20, slow: 50 } },
      fill_model: { kind: "bar" },
      price_basis: run.price_basis,
      capital_paise: run.capital_paise,
      seed: 42,
    },
    metrics: run.status === "done"
      ? { nav_start_paise: run.nav_start_paise, nav_end_paise: run.nav_end_paise, cagr: 0.0592, max_drawdown: run.max_drawdown,
          trades: run.trades, win_rate: 0.5714, costs_total_paise: run.costs_total_paise, days: 1180 }
      : {},
    manifest: run.status === "done"
      ? { engine_build_id: "2c5ed577b5ab", bundle_sha256: "9f2c…", packs: ["41ab…"], duration_ms: 812 }
      : null,
    fill_count: run.status === "done" ? fills.length : null,
    cost_totals: run.status === "done" ? { stt: 98_033, exchange: 3_111, sebi: 98, stamp: 14_700, gst: 3_242, dp: 1_480 } : {},
  };
}

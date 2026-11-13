/**
 * Pure helpers for the run chart (CH 6.14): no DOM, no charting-library import, so they run under node in
 * tests/stockify/test_chart_checks.py. Times: the API sends UTC seconds, the chart wants "IST wall-clock as if UTC".
 * Text: every string returned here is plain text; the note is passed through verbatim and the page must render
 * it as text (React escapes it), never as markup.
 */
import { rupees } from "./format";

// ---------------------------------------------------------------------------------------------------------
// Chart types. They follow the DTO shapes in task C11 (RunChartDTO, MarkerDTO). C11 owns web/src/lib/types.ts
// and its blockwatch tie to server/schemas.py: its entries replace these at integration.
// Guessed where C11's brief is silent: `side` is the oracle's "BUY" | "SELL" (matched case-insensitively here),
// `qty` is a number, `volume` is a number, and `chunks` has the C10 model's keys.
// ---------------------------------------------------------------------------------------------------------
export type ChartBar = {
  t: number; // UTC seconds
  d: string; // IST date, YYYY-MM-DD (daily bars use it)
  open: string; // rupee decimal strings
  high: string;
  low: string;
  close: string;
  volume: number;
};

export type ChartMarkerChunk = { qty: number; open_price_paise: number; pnl_paise: number };

export type ChartMarker = {
  kind: "fill" | "settlement";
  t: number;
  bar_t: number;
  side: string;
  qty: number;
  price: string;
  price_paise: number;
  /** A settlement's exchange settlement price (the underlying's, for an option); null or absent otherwise. */
  fsp_paise?: number | null;
  order_id: number | null;
  fill_id: number | null;
  note: string;
  tag: string;
  tag_is_code: boolean;
  charges_paise: number | null;
  trade_pnl_paise: number | null;
  chunks: ChartMarkerChunk[];
};

/** Colour values (resolved from the CSS tokens by the caller; the library cannot read `var(...)`). */
export type MarkerTokens = { up: string; down: string; warn: string };

export type ChartTime = number | string;
export type CandlePoint = { time: ChartTime; open: number; high: number; low: number; close: number };
export type VolumePoint = { time: ChartTime; value: number };
export type MarkerPoint = {
  time: ChartTime;
  position: "belowBar" | "aboveBar" | "inBar";
  shape: "arrowUp" | "arrowDown" | "square";
  color: string;
  text: string;
  id: string;
};
export type CardModel = {
  title: string;
  /** The note verbatim, or the "no note" sentence. Plain prose either way, never code. */
  reason: string;
  /** `code` is true when the value is a code label (a strategy tag) the UI shows in the code style. */
  lines: { label: string; value: string; code: boolean }[];
  pnl: { label: string; value: string; tone: "up" | "down" | "flat" } | null;
  /** One row per chunk of an earlier position this fill closed, with that chunk's profit or loss. */
  chunks: { label: string; value: string; tone: "up" | "down" | "flat" }[];
};

/** India Standard Time is UTC+5:30. */
export const istShift = 19800;

const isBuy = (m: ChartMarker): boolean => m.side.toUpperCase() === "BUY";

function timeOf(t: number, d: string, daily: boolean): ChartTime {
  return daily ? d : t + istShift;
}

export function toCandle(bar: ChartBar, daily: boolean): CandlePoint {
  return {
    time: timeOf(bar.t, bar.d, daily),
    open: Number(bar.open),
    high: Number(bar.high),
    low: Number(bar.low),
    close: Number(bar.close),
  };
}

export function toVolume(bar: ChartBar, daily: boolean): VolumePoint {
  return { time: timeOf(bar.t, bar.d, daily), value: bar.volume };
}

/**
 * The on-chart marker: side and quantity only (B 50, S 50, X 50); the note lives on the card. `index` is the marker's
 * position in the list being drawn; it keeps settlement ids unique when two settle at the same instant and quantity.
 */
export function toMarker(m: ChartMarker, daily: boolean, tokens: MarkerTokens, index = 0): MarkerPoint {
  const base = {
    time: daily ? istDate(m.bar_t) : m.bar_t + istShift,
    id: m.fill_id !== null ? `f${m.fill_id}` : `x${m.t}-${m.qty}-${index}`,
  };
  if (m.kind === "settlement") {
    return { ...base, position: "inBar", shape: "square", color: tokens.warn, text: `X ${m.qty}` };
  }
  return isBuy(m)
    ? { ...base, position: "belowBar", shape: "arrowUp", color: tokens.up, text: `B ${m.qty}` }
    : { ...base, position: "aboveBar", shape: "arrowDown", color: tokens.down, text: `S ${m.qty}` };
}

/** The IST calendar date (YYYY-MM-DD) of a UTC-seconds instant. */
function istDate(t: number): string {
  return new Date((t + istShift) * 1000).toISOString().slice(0, 10);
}

function signedRupees(paise: number): string {
  return paise > 0 ? `+${rupees(paise)}` : rupees(paise);
}

/** An option's symbol ends in a strike then CE or PE; anything else settled here is a future. */
export function settlementKindOf(symbol: string): "option" | "future" {
  return /\d(CE|PE)$/.test(symbol) ? "option" : "future";
}

/** What the card beside a marker shows. All values are plain text. */
export function cardModel(m: ChartMarker, contract?: "option" | "future"): CardModel {
  const verb = m.kind === "settlement" ? "Settled" : isBuy(m) ? "Bought" : "Sold";
  const lines: CardModel["lines"] = [{ label: "Quantity", value: String(m.qty), code: false }];
  if (m.kind === "settlement" && contract === "option") {
    // The runner closes an option at its value at expiry; the exchange's settlement price is the underlying's.
    lines.push({ label: "Value at expiry", value: rupees(m.price_paise), code: false });
    if (m.fsp_paise != null) lines.push({ label: "Settlement price", value: rupees(m.fsp_paise), code: false });
  } else {
    lines.push({ label: m.kind === "settlement" ? "Settlement price" : "Price", value: rupees(m.price_paise), code: false });
  }
  if (m.charges_paise !== null) lines.push({ label: "Costs", value: rupees(m.charges_paise), code: false });
  if (m.tag) lines.push({ label: "Tag", value: m.tag, code: m.tag_is_code });

  const noNote = m.note === "";
  let pnl: CardModel["pnl"] = null;
  if (m.trade_pnl_paise !== null) {
    const p = m.trade_pnl_paise;
    pnl =
      p > 0
        ? { label: "Profit on the trade", value: signedRupees(p), tone: "up" }
        : p < 0
          ? { label: "Loss on the trade", value: signedRupees(p), tone: "down" }
          : { label: "Result of the trade", value: rupees(0), tone: "flat" };
  }
  const chunks: CardModel["chunks"] = m.chunks.map((c) => ({
    label: `Closed ${c.qty}, opened at ${rupees(c.open_price_paise)}`,
    value: signedRupees(c.pnl_paise),
    tone: c.pnl_paise > 0 ? "up" : c.pnl_paise < 0 ? "down" : "flat",
  }));
  return {
    chunks,
    title: `${verb} ${m.qty} at ${rupees(m.price_paise)}`,
    reason: noNote ? `${verb} ${m.qty} (no note from the strategy)` : m.note,
    lines,
    pnl,
  };
}

/** Joins a fetched page into the bars already held: unique by `t` (the held bar wins), sorted by `t`. */
export function mergePages(have: ChartBar[], page: ChartBar[]): ChartBar[] {
  const byT = new Map<number, ChartBar>();
  for (const b of page) byT.set(b.t, b);
  for (const b of have) byT.set(b.t, b);
  return [...byT.values()].sort((a, b) => a.t - b.t);
}

/**
 * The `from_ns`/`to_ns` query (UTC epoch nanoseconds) for `bars` bars either side of an instant given in UTC seconds.
 * Whole seconds times 1e9 are exact in a double up to about 2.3e18, so nothing is rounded.
 */
export function rangeAround(tSec: number, interval_s: number, bars: number): { from_ns: number; to_ns: number } {
  const t = Math.floor(tSec);
  const span = Math.ceil(bars * interval_s);
  return { from_ns: Math.max(0, t - span) * 1e9, to_ns: (t + span) * 1e9 };
}

// ---------------------------------------------------------------------------------------------------------
// The component's pure parts (CH 6.15).
// Guessed where C11's brief is silent (C11 replaces these types at integration): `data_state` is "ok" or a word
// for missing prices ("pruned" for a pruned run, "no_pack" for a session day without a recording); `plain` is a
// short plain-words line about the chart, shown as is. Session and walk-forward fields are optional.
// ---------------------------------------------------------------------------------------------------------
export type ChartPlotPoint = { t: number; value: string };
export type ChartPlot = { name: string; kind: "price" | "level"; instrument_id: number | null; points: ChartPlotPoint[] };
export type ChartWindow = { from_ns: number; to_ns: number; label: string };

export type ChartModel = {
  instruments: { id: number; symbol: string }[];
  instrument_id: number | null;
  basis: string;
  interval_s: number;
  bars: ChartBar[];
  markers: ChartMarker[];
  plots: ChartPlot[];
  truncated: boolean;
  next_from_ns: number | null;
  data_state: string;
  omitted_markers: number;
  plain?: string;
  /** A plain sentence to show above the chart (a different interval, an unpriced settlement, delivery); null when none. */
  notice?: string | null;
  /** False when some profit is not on the markers (delivery, an unpriced settlement). */
  pnl_complete?: boolean;
  // sessions (C13)
  trade_date?: string;
  days?: string[];
  paper?: boolean;
  live?: boolean;
  // walk-forward (C12)
  windows?: { index: number; from_ns: number; to_ns: number; run_id: string | null; status: string }[];
};

export type ChartViewState = "loading" | "error" | "pruned" | "empty" | "full";

/** null: not loaded yet. "error": the load failed. A model: no price data, no trades, or both drawn. */
export function chartViewState(model: ChartModel | null | "error"): ChartViewState {
  if (model === null) return "loading";
  if (model === "error") return "error";
  if (model.data_state === "pruned" || model.data_state === "no_pack") return "pruned";
  return model.markers.length === 0 ? "empty" : "full";
}

/** A daily chart uses dates, not instants. */
export const isDaily = (interval_s: number): boolean => interval_s >= 86400;

function istStamp(t: number, daily: boolean): string {
  const iso = new Date((t + istShift) * 1000).toISOString();
  return daily ? iso.slice(0, 10) : `${iso.slice(0, 10)} ${iso.slice(11, 16)}`;
}

export type MarkerRow = { id: string; when: string; title: string; reason: string };

/** One row per marker, the same ids and order as the chart's markers, for the keyboard-reachable list. */
export function markerRows(markers: ChartMarker[], daily: boolean, tokens: MarkerTokens): MarkerRow[] {
  return markers.map((m, i) => {
    const card = cardModel(m);
    return { id: toMarker(m, daily, tokens, i).id, when: istStamp(m.t, daily), title: card.title, reason: card.reason };
  });
}

/** A plot point on the chart's time rule; the value arrives as a decimal string. */
export function toPlotPoint(p: ChartPlotPoint, daily: boolean): { time: ChartTime; value: number } {
  return { time: daily ? istDate(p.t) : p.t + istShift, value: Number(p.value) };
}

export type EdgeRequest = { side: "before" | "after"; from_ns: number; to_ns: number };

/**
 * Which neighbouring ranges to fetch when the visible range (UTC seconds) reaches an edge of the bars held:
 * within three bars of it. `count` bars are asked for beyond the edge.
 */
export function edgeRequests(
  visible: { from: number; to: number },
  bars: ChartBar[],
  interval_s: number,
  count: number,
): EdgeRequest[] {
  if (bars.length === 0) return [];
  const first = bars[0].t;
  const last = bars[bars.length - 1].t;
  const near = 3 * interval_s;
  const span = Math.ceil(count * interval_s);
  const out: EdgeRequest[] = [];
  if (visible.from <= first + near) out.push({ side: "before", from_ns: Math.max(0, first - span) * 1e9, to_ns: first * 1e9 });
  if (visible.to >= last - near) out.push({ side: "after", from_ns: last * 1e9, to_ns: (last + span) * 1e9 });
  return out;
}

/** Bars inside a test window, with the window's index: the walk-forward shading. */
export function shadeWindows(
  bars: ChartBar[],
  windows: ChartWindow[],
  daily: boolean,
): { time: ChartTime; index: number }[] {
  const out: { time: ChartTime; index: number }[] = [];
  for (const b of bars) {
    const ns = b.t * 1e9;
    const index = windows.findIndex((w) => ns >= w.from_ns && ns < w.to_ns);
    if (index >= 0) out.push({ time: timeOf(b.t, b.d, daily), index });
  }
  return out;
}

export type ChartOpts = { instrument?: number; from_ns?: number; to_ns?: number; trade_date?: string };

/** The query string for a chart request: only what is set (an instrument of 0 is set). */
export function chartQuery(o: ChartOpts): string {
  const q: string[] = [];
  if (o.instrument !== undefined) q.push(`instrument=${o.instrument}`);
  if (o.from_ns !== undefined) q.push(`from_ns=${o.from_ns}`);
  if (o.to_ns !== undefined) q.push(`to_ns=${o.to_ns}`);
  if (o.trade_date !== undefined) q.push(`trade_date=${encodeURIComponent(o.trade_date)}`);
  return q.length ? `?${q.join("&")}` : "";
}

// ---------------------------------------------------------------------------------------------------------
// Fix round 1 (C15 review): the canvas summary, the axis of a chart with no price data, the hover tip.
// ---------------------------------------------------------------------------------------------------------
export function intervalWords(interval_s: number): string {
  if (isDaily(interval_s)) return "daily";
  if (interval_s === 60) return "one-minute";
  return interval_s % 60 === 0 ? `${interval_s / 60}-minute` : `${interval_s}-second`;
}

/** The text alternative of the canvas: what is drawn, and where the keyboard route to the trades is. */
export function chartSummary(model: ChartModel, symbol: string): string {
  const head = symbol ? `Price chart of ${symbol}` : "Price chart";
  const n = model.bars.length;
  const m = model.markers.length;
  const trades = m === 0 ? "" : `${m} trade${m === 1 ? "" : "s"}`;
  if (n === 0) return `${head} with no price data${m ? "; the trades are listed below." : "."}`;
  const bars = `${n} ${intervalWords(model.interval_s)} bar${n === 1 ? "" : "s"}`;
  return m === 0 ? `${head}, ${bars}, no trades.` : `${head}, ${bars}, ${trades}; the trades are listed below.`;
}

const AXIS_FILL_MAX = 5000;

/**
 * Chart times for the empty axis of a chart with no price data: every marker's bar, and (intraday, when not too many)
 * every bar slot between the first and the last, so the markers sit at their real spacing.
 */
export function axisTimes(markers: ChartMarker[], interval_s: number, daily: boolean): ChartTime[] {
  const ts = [...new Set(markers.map((m) => m.bar_t))].sort((a, b) => a - b);
  if (ts.length === 0) return [];
  if (daily) return [...new Set(ts.map(istDate))];
  const first = ts[0];
  const last = ts[ts.length - 1];
  if (interval_s <= 0 || (last - first) / interval_s > AXIS_FILL_MAX) return ts.map((t) => t + istShift);
  const out: number[] = [];
  for (let t = first; t <= last; t += interval_s) out.push(t + istShift);
  return out;
}

/** What the hover tip shows for the markers on the bar under the crosshair: the card's title and reason, as text. */
export function tipModel(ms: ChartMarker[], contract?: "option" | "future"): { title: string; reason: string; more: number } | null {
  if (ms.length === 0) return null;
  const c = cardModel(ms[0], contract);
  return { title: c.title, reason: c.reason, more: ms.length - 1 };
}

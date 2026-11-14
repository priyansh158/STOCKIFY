/**
 * Pure helpers for the run chart (CH 6.14): no DOM, no charting-library import, so they run under node in
 * tests/stockify/test_chart_checks.py. Times: the API sends UTC seconds, the chart wants "IST wall-clock as if UTC".
 * Text: every string returned here is plain text; the note is passed through verbatim and the page must render
 * it as text (React escapes it), never as markup.
 */
import { rupees } from "./format";
import type {
  ChartBarDTO,
  ChartInstrumentDTO,
  ChartChunkDTO,
  ChartMarkerDTO,
  RunChartDTO,
  TestFill,
  WalkforwardChartWindowDTO,
} from "./types";

// The wire shapes are C11's DTOs in ./types (times UTC seconds, prices rupee strings, paise as integers).
export type ChartInstrumentKind = ChartInstrumentDTO["kind"];
export type ChartBar = ChartBarDTO;
export type ChartMarkerChunk = ChartChunkDTO;
export type ChartMarker = ChartMarkerDTO;

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
  /** `code` is true when the value should be shown in the code style (none now: tags arrive in plain words). `hint` is the
   * stored word behind a plain value, for a hover. */
  lines: { label: string; value: string; code: boolean; hint?: string }[];
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

/** The on-chart marker: side and quantity only (B 50, S 50, X 50); the note lives on the card. The id is the API's, opaque. */
export function toMarker(m: ChartMarker, daily: boolean, tokens: MarkerTokens): MarkerPoint {
  const base = { time: daily ? m.bar_d : m.bar_t + istShift, id: m.id };
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

/** What a settlement's price is called depends on the instrument's kind from the API; other kinds have no special label. */
export function contractOf(kind: ChartInstrumentKind | undefined): "option" | "future" | undefined {
  return kind === "option" || kind === "future" ? kind : undefined;
}

/** The note verbatim, or the "no note" sentence: the one wording the card, the hover tip and the run page's Why column share. */
export function reasonOf(m: ChartMarker): string {
  if (m.note !== "") return m.note;
  const verb = m.kind === "settlement" ? "Settled" : isBuy(m) ? "Bought" : "Sold";
  return `${verb} ${m.qty} (no note from the strategy)`;
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
  // The strategy's tag, in the server's plain words ("Long entry"); the stored tag is the hover.
  if (m.tag) lines.push({ label: "Kind", value: m.tag_label || m.tag, code: false, hint: m.tag });

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
    reason: reasonOf(m),
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
export type ChartPlot = RunChartDTO["plots"][number];
export type ChartWindow = {
  from_ns: number;
  to_ns: number;
  label: string;
  /** True for a window with no bars to shade (excluded, no trades, failed): it is shaded by day instead. */
  gap?: boolean;
  /** Seconds after IST midnight at which the session opens on this window's first day (the server's dated config). */
  open_s?: number | null;
};

/** A run's chart as the API sends it, plus the session and walk-forward fields (C12, C13) that a run does not have. */
export type ChartModel = RunChartDTO & {
  /** Present on a live session. */
  live?: boolean;
  trade_date?: string;
  days?: string[];
  paper?: boolean;
  windows?: WalkforwardChartWindowDTO[];
};

export type ChartViewState = "loading" | "error" | "pruned" | "empty" | "full";

/** null: not loaded yet. "error": the load failed. A model: no price data, no trades, or both drawn. */
export function chartViewState(model: ChartModel | null | "error"): ChartViewState {
  if (model === null) return "loading";
  if (model === "error") return "error";
  if (model.data_state !== "ok") return "pruned"; // pruned, no_pack and pack unreadable are plain states, not "no trades"
  return model.markers.length === 0 ? "empty" : "full";
}

/**
 * What a paper session's chart area says when the server has no chart for it yet (its chart answers 404 until the session
 * has recorded a day). A session that is created or armed has simply not started, which is not an error; any other state
 * returns null, and the chart keeps its generic "not found" message.
 */
export function sessionNotStartedNote(status: string | null | undefined): string | null {
  return status === "CREATED" || status === "ARMED" ? "This session has not started yet; prices appear once it is running." : null;
}

/** The interval the API leaves null when there are no bars (no recording); a minute, the runner's default. */
export const DEFAULT_INTERVAL_S = 60;

/** A daily chart uses dates, not instants. */
export const isDaily = (interval_s: number): boolean => interval_s >= 86400;

function istStamp(t: number, daily: boolean): string {
  const iso = new Date((t + istShift) * 1000).toISOString();
  return daily ? iso.slice(0, 10) : `${iso.slice(0, 10)} ${iso.slice(11, 16)}`;
}

export type MarkerRow = { id: string; when: string; title: string; reason: string };

/** One row per marker, the same ids and order as the chart's markers, for the keyboard-reachable list. */
export function markerRows(markers: ChartMarker[], daily: boolean): MarkerRow[] {
  return markers.map((m) => {
    const card = cardModel(m);
    return { id: m.id, when: istStamp(m.t, daily), title: card.title, reason: card.reason };
  });
}

/** A plot point on the chart's time rule; the value arrives as a decimal string. */
export function toPlotPoint(p: ChartPlot["points"][number], daily: boolean): { time: ChartTime; value: number } {
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

export type ChartOpts = { instrument?: number; from_ns?: number; to_ns?: number; interval_s?: number; trade_date?: string; signal?: AbortSignal };

/** The query string for a chart request: only what is set (an instrument of 0 is set). */
export function chartQuery(o: ChartOpts): string {
  const q: string[] = [];
  if (o.instrument !== undefined) q.push(`instrument=${o.instrument}`);
  if (o.from_ns !== undefined) q.push(`from_ns=${o.from_ns}`);
  if (o.to_ns !== undefined) q.push(`to_ns=${o.to_ns}`);
  if (o.interval_s !== undefined) q.push(`interval_s=${o.interval_s}`);
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
  const bars = `${n} ${intervalWords(model.interval_s ?? DEFAULT_INTERVAL_S)} bar${n === 1 ? "" : "s"}`;
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

// ---------------------------------------------------------------------------------------------------------
// The run page's Fills table and the chart tell one story (CH 6.16).
// ---------------------------------------------------------------------------------------------------------
export type JoinedFill = {
  fill: TestFill;
  /** The chart marker for this fill, for the row click and the highlight; null when the chart has none (yet). */
  markerId: string | null;
  /** The strategy's note, or the "no note" sentence with its tag in plain words beside it (`raw` is the stored tag, for a
   * hover). From the fill's own order, so it never depends on what the chart has loaded. */
  why: { text: string; tag: string | null; raw: string | null };
};

/** The reason for a fill: the note of its order, else the same "no note" sentence the chart card uses. */
export function whyOf(fill: TestFill): { text: string; tag: string | null; raw: string | null } {
  if (fill.note !== "") return { text: fill.note, tag: null, raw: null };
  const verb = fill.side.toUpperCase() === "BUY" ? "Bought" : "Sold";
  return {
    text: `${verb} ${fill.qty} (no note from the strategy)`,
    tag: fill.tag !== "" ? fill.tag_label || fill.tag : null,
    raw: fill.tag !== "" ? fill.tag : null,
  };
}

/** One row per fill, in the fills' order; the chart marker with the same `fill_id` (if the chart has loaded it) is
 * only for the row click and the highlight. */
export function joinFillsAndMarkers(fills: TestFill[], markers: ChartMarker[]): JoinedFill[] {
  const byFill = new Map<number, ChartMarker>();
  for (const m of markers) if (m.fill_id !== null) byFill.set(m.fill_id, m);
  return fills.map((fill) => {
    const m = byFill.get(fill.fill_id);
    return { fill, markerId: m ? m.id : null, why: whyOf(fill) };
  });
}

/** One row of a sweep, as far as picking a trial needs it. */
export type TrialRow = { index: number; run_id: string | null; status: string; params: Record<string, string | number | boolean | null> };

/**
 * Which trial the sweep page charts. `param` is the page's `?trial=`: missing or empty gives the best trial, a finished
 * trial's run id gives that trial, and anything else (an unknown id, a trial that did not finish) is "unknown", which the page
 * shows as an empty state. "none" means there is no best trial to fall back on.
 */
export function resolveTrial<R extends TrialRow>(
  param: string | undefined,
  rows: R[],
  bestRunId: string | null,
): { kind: "best" | "picked" | "unknown" | "none"; row: R | null } {
  const finished = (r: R) => r.status === "done" && r.run_id !== null;
  if (!param) {
    const best = bestRunId === null ? undefined : rows.find((r) => finished(r) && r.run_id === bestRunId);
    return best ? { kind: "best", row: best } : { kind: "none", row: null };
  }
  const row = rows.find((r) => finished(r) && r.run_id === param);
  return row ? { kind: "picked", row } : { kind: "unknown", row: null };
}

/** A trial's settings in words, "Fast average 5, Slow average 20" (none, true and false are written out). */
export function trialWords(axes: { path: string; label: string }[], params: TrialRow["params"]): string {
  return axes
    .map((a) => {
      const v = params[a.path];
      return `${a.label} ${v === null || v === undefined ? "none" : typeof v === "boolean" ? (v ? "true" : "false") : String(v)}`;
    })
    .join(", ");
}

/** The link that picks a trial: the sweep page with its axes kept, `?trial=`, and the anchor of the chart under the grid. */
export function trialHref(sweepId: string, x: string, y: string | null, runId: string): string {
  const q = [`x=${encodeURIComponent(x)}`, ...(y === null ? [] : [`y=${encodeURIComponent(y)}`]), `trial=${encodeURIComponent(runId)}`];
  return `/research/sweeps/${encodeURIComponent(sweepId)}?${q.join("&")}#trial-chart`;
}

// ---------------------------------------------------------------------------------------------------------
// The walk-forward page (CH 6.18): the out-of-sample windows as bands on the chart.
// ---------------------------------------------------------------------------------------------------------
const NO_CHART_WHY: Record<string, string> = {
  failed: "this window failed",
  skipped: "this window was skipped",
  pending: "this window has not run yet",
  missing: "the files of its run are gone",
};

/** The band's words, plain: drawn windows say "out of sample"; the others say why nothing is drawn. The server's `excluded` text is used as it came. */
export function windowLabel(w: WalkforwardChartWindowDTO): string {
  const head = `Window ${w.index + 1} (out of sample)`;
  if (w.excluded) return w.excluded.startsWith("not charted") ? `${head}, ${w.excluded}` : `${head}, excluded: ${w.excluded}`;
  if (w.status === "no_trades") return `${head}, no trades in this window`;
  if (w.status === "done") return head;
  return `${head}, no chart: ${NO_CHART_WHY[w.status] ?? "its state is not known"}`;
}

/** The server's windows as bands (UTC ns bounds, the test days as the server clipped them), in the server's order. */
export function windowBands(windows: WalkforwardChartWindowDTO[]): ChartWindow[] {
  return windows.map((w) => ({
    from_ns: w.from_t * 1e9,
    to_ns: w.to_t * 1e9,
    label: windowLabel(w),
    gap: w.excluded != null || w.status !== "done",
    open_s: w.session_open_s,
  }));
}

const GAP_DAYS_MAX = 400;

/**
 * The shading for the histogram under the candles: every bar inside a window gets that window's position; a gap window has
 * no bars, so it gets one point on each weekday of its days (at the window's `open_s` seconds after IST midnight, the session open the server
 * reads from its dated config for that day, or the date on a daily chart; with no open known, the day's start). Sorted, one per time.
 * A holiday inside a gap window still gets its point, which is why the shading is a band and never a claim about trading days.
 */
export function shadePoints(
  bars: ChartBar[],
  windows: ChartWindow[],
  daily: boolean,
): { time: ChartTime; index: number; gap: boolean }[] {
  const byTime = new Map<ChartTime, { time: ChartTime; index: number; gap: boolean }>();
  for (const b of bars) {
    const ns = b.t * 1e9;
    const index = windows.findIndex((w) => ns >= w.from_ns && ns < w.to_ns);
    if (index >= 0) byTime.set(timeOf(b.t, b.d, daily), { time: timeOf(b.t, b.d, daily), index, gap: windows[index].gap === true });
  }
  windows.forEach((w, index) => {
    if (!w.gap) return;
    const end = w.to_ns / 1e9;
    for (let t = w.from_ns / 1e9, n = 0; t < end && n < GAP_DAYS_MAX; t += 86400, n++) {
      const weekday = new Date((t + istShift) * 1000).getUTCDay();
      if (weekday === 0 || weekday === 6) continue;
      const time = daily ? istDate(t) : t + (w.open_s ?? 0) + istShift;
      if (!byTime.has(time)) byTime.set(time, { time, index, gap: true });
    }
  });
  return [...byTime.values()].sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
}

/** Bars of empty space allowed past the first and last bar (the chart's `rightOffset` is the same). */
export const VIEW_MARGIN = 4;

export type FirstView = { kind: "fit" } | { kind: "range"; from: number; to: number };

/**
 * How a chart first looks: everything fitted, or, for a long chart with trades, `near` bars round the first trade (`at` is the
 * index of the first bar at or after it, -1 when there is none, which shows the end). The chart re-applies the same view
 * when its box gets its width late, until the person pans or zooms.
 */
export function initialView(barCount: number, at: number, markerCount: number, near: number): FirstView {
  if (barCount <= near || markerCount === 0) return { kind: "fit" };
  const i = at < 0 ? barCount - 1 : at;
  // A window of `near` bars round the trade, slid back inside the data (with a small margin at each end) so that it never
  // reaches past the first or last bar: a window that overshoots would leave part of the chart empty.
  let from = i - near / 2;
  if (from < -VIEW_MARGIN) from = -VIEW_MARGIN;
  let to = from + near;
  const last = barCount - 1 + VIEW_MARGIN;
  if (to > last) {
    to = last;
    from = Math.max(-VIEW_MARGIN, to - near);
  }
  return { kind: "range", from, to };
}

/** Poll delay in ms for a live chart: only while the day is open and the tab is visible; null means do not poll. */
export function pollDelay(live: boolean, hidden: boolean, ms: number): number | null {
  return live && !hidden && ms > 0 ? ms : null;
}

/** The wait before the next poll: the base after a success, doubled for each failure in a row, capped at 30 s (never below the base). */
export function nextDelay(base: number, failures: number, cap = 30_000): number {
  if (!(base > 0)) return 0;
  if (!(failures > 0)) return base;
  return Math.max(base, Math.min(cap, base * 2 ** failures));
}

/** The plain sentence for a chart with no price data (state "pruned"). */
export function prunedWords(dataState: ChartModel["data_state"], paper: boolean, live = false): string {
  // a session still open (the server's `live` flag) has no recording yet because prices have not arrived, not because none was made
  if (live && dataState === "no_pack") return "No prices have arrived yet. The chart appears here when they do; the trades are listed below.";
  if (dataState === "no_pack") {
    return paper
      ? "No price data was recorded for this day. The trades are listed below."
      : "There is no price recording for this day. The trades are listed below.";
  }
  if (dataState === "pack unreadable") return "The price recording for this day cannot be read right now. Reload to try again; the trades are listed below.";
  return "The price data for this run is no longer on disk. The trades are listed below.";
}

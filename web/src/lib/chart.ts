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
  reason: string;
  reasonIsCode: boolean;
  lines: { label: string; value: string }[];
  pnl: { label: string; value: string; tone: "up" | "down" | "flat" } | null;
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

/** The on-chart marker: side and quantity only (B 50, S 50, X 50); the note lives on the card. */
export function toMarker(m: ChartMarker, daily: boolean, tokens: MarkerTokens): MarkerPoint {
  const base = {
    time: daily ? istDate(m.bar_t) : m.bar_t + istShift,
    id: m.fill_id !== null ? `f${m.fill_id}` : `x${m.t}-${m.qty}`,
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

/** What the card beside a marker shows. All values are plain text. */
export function cardModel(m: ChartMarker): CardModel {
  const verb = m.kind === "settlement" ? "Settled" : isBuy(m) ? "Bought" : "Sold";
  const lines: { label: string; value: string }[] = [
    { label: "Quantity", value: String(m.qty) },
    { label: "Price", value: rupees(m.price_paise) },
  ];
  if (m.charges_paise !== null) lines.push({ label: "Costs", value: rupees(m.charges_paise) });
  if (m.tag) lines.push({ label: "Tag", value: m.tag });

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
  return {
    title: `${verb} ${m.qty} at ${rupees(m.price_paise)}`,
    reason: noNote ? `${verb} ${m.qty} (no note from the strategy)` : m.note,
    reasonIsCode: noNote,
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
 * The `from_ns`/`to_ns` query for `bars` bars either side of an instant. A double holds integers exactly only up
 * to 2^53 (about 9.0e15), so epoch nanoseconds (about 1.8e18) are rounded to the nearest 256 ns: far finer than
 * any bar, and the server only uses the range to pick whole bars.
 */
export function rangeAround(tNs: number, interval_s: number, bars: number): { from_ns: number; to_ns: number } {
  const span = bars * interval_s * 1e9;
  return { from_ns: Math.max(0, tNs - span), to_ns: tNs + span };
}

/**
 * The fundamentals screener (FS 10): calls for /api/fundamentals and the words its pages use. The list is ideas to
 * research, never orders. Its order is the judgement: no score, rank number or account of how the list is ordered
 * reaches the page (owner, 2026-11-17), so `position` is used only to keep the API's order.
 */
import { api, ApiError } from "./api";
import { dayWords } from "./format";
import { pathSegment } from "./pathSeg";
import type { CandidateDetailDTO, FilingDTO, FundamentalsListDTO, SeriesDTO } from "./types";

/** What a call can give: the answer, a 404 with the API's reason (details only), or the API is not answering. */
export type Loaded<T> = { kind: "ok"; value: T } | { kind: "missing"; reason: string } | { kind: "error"; message: string };

async function load<T>(path: string): Promise<Loaded<T>> {
  try {
    return { kind: "ok", value: await api<T>(path) };
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return { kind: "missing", reason: e.message };
    return { kind: "error", message: e instanceof ApiError ? e.message : "the API did not answer" };
  }
}

/** GET /api/fundamentals: the results coming up and the newest list, best first. */
export async function getFundamentals(): Promise<Loaded<FundamentalsListDTO>> {
  const r = await load<FundamentalsListDTO>("/api/fundamentals");
  if (r.kind === "ok") r.value.items = [...r.value.items].sort((a, b) => a.position - b.position);
  return r;
}

/** GET /api/fundamentals/{symbol}; a symbol the screener does not cover is `missing` with the API's reason. */
export async function getFundamentalsDetail(symbol: string): Promise<Loaded<CandidateDetailDTO>> {
  let seg: string;
  try {
    seg = pathSegment(symbol);
  } catch {
    return { kind: "missing", reason: "not one of the stocks the screener covers" };
  }
  return load<CandidateDetailDTO>(`/api/fundamentals/${seg}`);
}

export function detailHref(symbol: string): string {
  return `/research/fundamentals/${encodeURIComponent(symbol)}`;
}

/** The plain meaning of each figure, one line each (the spec: plain names with one-line meanings, no jargon alone). */
export const MEANINGS: Record<string, string> = {
  pe: "Price to earnings: the share price over the last four quarters' profit per share. Lower is cheaper.",
  pb: "Price to book: the share price over the shareholders' equity per share. Lower is cheaper.",
  roe: "Return on equity: the last four quarters' profit over the shareholders' equity. Higher is better.",
  debt_equity: "Debt to equity: what the company has borrowed over its shareholders' equity. Lower is safer.",
  op_margin: "Operating margin: what is left of revenue after operating costs. Higher is better.",
  profit_growth: "Profit growth: the newest quarter's profit against the same quarter a year earlier.",
  nii_growth: "Net interest income growth: a lender's interest earned less interest paid, against a year earlier.",
  revenue: "Revenue from operations in the quarter.",
  net_profit: "Net profit in the quarter.",
  nii: "Net interest income in the quarter: interest earned less interest paid.",
};

/** The full name of a figure where the API's label is an abbreviation. */
export function figureName(key: string, label: string): string {
  if (key === "pe") return "Price to earnings (P/E)";
  if (key === "pb") return "Price to book (P/B)";
  return label;
}

const CRORE = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
const CRORE_SMALL = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 1 });

const MINUS = "−";   // a true minus sign, as the research figures use (researchHelpers objectiveFormat)

/** A figure in its unit: paise as ₹ crore, "times" as 18.4×, a fraction as 12.3 % (the app's `pct` spacing). A negative
 * figure leads with a true minus, before the ₹: "−₹1,234 cr". Missing is a dash. */
export function figureText(unit: SeriesDTO["unit"], v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  const sign = v < 0 ? MINUS : "";
  const a = Math.abs(v);
  if (unit === "paise") {
    const cr = a / 1e9;                          // 1 crore = ₹1,00,00,000 = 1e9 paise
    return `${sign}₹${(cr < 100 ? CRORE_SMALL : CRORE).format(cr)} cr`;
  }
  if (unit === "fraction") return `${sign}${(a * 100).toFixed(1)} %`;
  return `${sign}${a.toFixed(a < 10 ? 2 : 1)}×`;
}

/** A quarter's period end ("2025-09-30") as a number that counts quarters, so a missing quarter leaves a gap. */
export function quarterNumber(iso: string): number {
  const y = Number(iso.slice(0, 4));
  const m = Number(iso.slice(5, 7));
  return y * 4 + Math.floor((m - 1) / 3);
}

const MONTHS = ["Mar", "Jun", "Sep", "Dec"];

/** The quarter number back as the month it ended: "Sep 2025". */
export function quarterWords(n: number): string {
  const y = Math.floor(n / 4);
  return `${MONTHS[n - y * 4]} ${y}`;
}

/** One figure's series as chart points: the stock's and its peers' median. The step is the series' own smallest gap
 * (a quarter, or a half-year for balance-sheet figures), and the line breaks wherever a step is missing. */
export function chartLines(s: SeriesDTO): { own: [number, number][]; median: [number, number][]; ownBreaks: number[]; medianBreaks: number[] } {
  const pts = s.points.map((p) => ({ q: quarterNumber(p.period_end), v: p.value, m: p.sector_median }));
  const line = (get: (p: (typeof pts)[number]) => number | null | undefined) => {
    const out: [number, number][] = [];
    for (const p of pts) {
      const v = get(p);
      if (v !== null && v !== undefined && Number.isFinite(v)) out.push([p.q, v]);
    }
    out.sort((x, y) => x[0] - y[0]);
    const gaps = out.slice(1).map((p, i) => p[0] - out[i][0]);
    const step = gaps.length ? Math.min(...gaps) : 1;
    const breaks = out.slice(1).filter((p, i) => p[0] - out[i][0] > step).map((p) => p[0]);
    return { out, breaks };
  };
  const own = line((p) => p.v);
  const median = line((p) => p.m);
  return { own: own.out, median: median.out, ownBreaks: own.breaks, medianBreaks: median.breaks };
}

/** Who a stock is compared with, in words: "Capital Goods stocks on the list", or every stock when its industry has too few. */
export function peerWords(peerGroup: string | null | undefined): string | null {
  if (!peerGroup) return null;
  const m = /^Industry: (.+)$/.exec(peerGroup);
  if (m) return `Compared with the other ${m[1]} stocks on the list.`;
  const all = /^All stocks \((.+)\)$/.exec(peerGroup);
  if (all) return `Compared with every stock on the list (${all[1]}).`;
  return `Compared with: ${peerGroup}.`;
}

/** The name of the median line: the industry's, or every listed stock's when the industry had too few. */
export function medianLabel(peerGroup: string | null | undefined): string {
  return peerGroup && !peerGroup.startsWith("Industry:") ? "Median of all stocks" : "Industry median";
}

const KINDS: Record<string, string> = { Q: "Quarter", H: "Half year", A: "Year" };

export function periodKindWords(kind: string): string {
  return KINDS[kind] ?? kind;
}

export function basisWords(basis: string): string {
  return basis === "consolidated" ? "Consolidated" : basis === "standalone" ? "Standalone" : basis;
}

/** A filing's state as a word and its text class: read, held back (needs a look), or not read yet. */
export function filingState(f: FilingDTO): { text: string; cls?: string } {
  switch (f.status) {
    case "parsed":
      return { text: "Read" };
    case "held":
      return { text: "Held back", cls: "stk-warn" };
    case "downloaded":
      return { text: "Downloaded, not read yet", cls: "stk-muted" };
    case "listed":
      return { text: "Not downloaded yet", cls: "stk-muted" };
    default:
      return { text: f.status, cls: "stk-muted" };
  }
}

/** NSE's broadcast time as the API gives it ("2025-11-09 20:00", IST) in the app's date words. */
export function broadcastWords(s: string | null | undefined): string {
  if (!s) return "—";
  const m = /^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})/.exec(s);
  return m ? `${dayWords(m[1])}, ${m[2]} IST` : s;
}

/** A clause from the API as a sentence: capitalised, with a full stop. */
export function sentence(s: string): string {
  const t = s.trim();
  if (!t) return t;
  const up = t[0].toUpperCase() + t.slice(1);
  return /[.!?]$/.test(up) ? up : `${up}.`;
}

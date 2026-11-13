/**
 * Pure helpers of the /data coverage page: cell words, gap reasons, durations, sizes, the recorder strip's sentences and the
 * IST day arithmetic. No imports of the fetch wrapper, so Node can run them directly in tests/stockify/research_check.ts.
 * Sentences the API already carries (a cell's detail, the storage warnings, the store errors) are shown as they come;
 * these helpers only shorten a cell for its chip and name the codes the API gives as codes.
 */
import type { CoverageCellDTO, InstrumentQuality, RecorderStatus } from "./types";

export type CellTone = "up" | "down" | "warn" | "info";

/** The tone of a cell state: colour is the second signal, `cellSymbol` and the words are the first. Closed and not applicable have none. */
export function cellTone(state: string): CellTone | null {
  switch (state) {
    case "ok": return "up";
    case "partial": return "warn";
    case "missing": return "down";
    case "not_reported": return "info";
    default: return null;
  }
}

/** A text symbol per state, shown beside the words so no state depends on colour. Decorative to a screen reader (the words follow). */
export function cellSymbol(state: string): string {
  switch (state) {
    case "ok": return "✓";
    case "partial": return "~";
    case "missing": return "✕";
    case "closed":
    case "not_applicable": return "–";
    default: return "?";
  }
}

const PARTIAL_WORDS: Record<string, string> = {
  landing: "landing, not archived yet",
  archived: "archived, not verified yet",
};

function percentWords(v: number): string {
  return `${Number(v.toFixed(1))}% covered`;
}

/** The chip's words and the small line under it (the part of the API's detail the words do not already say). */
export function cellParts(cell: CoverageCellDTO, datasetKey?: string): { words: string; note: string | null } {
  const detail = cell.detail?.trim() || null;
  const hasValue = typeof cell.value === "number" && Number.isFinite(cell.value);
  switch (cell.state) {
    case "ok":
    case "partial":
      if (hasValue) return { words: percentWords(cell.value as number), note: detail };
      if (detail) return { words: PARTIAL_WORDS[detail] ?? detail, note: null };
      if (cell.state === "ok") return { words: datasetKey?.startsWith("nse_") ? "downloaded" : "complete", note: null };
      return { words: "partly there", note: null };
    case "missing":
      if (!detail || detail === "no recording for this day") return { words: "no recording", note: null };
      return { words: detail, note: null };
    case "not_reported":
      return { words: "not reported yet", note: detail };
    case "closed":
      return { words: "market closed", note: null };
    case "not_applicable": {
      if (!detail) return { words: "not applicable", note: null };
      const i = detail.indexOf("; ");
      return i < 0 ? { words: detail, note: null } : { words: detail.slice(0, i), note: detail.slice(i + 2) };
    }
    default:
      return { words: `unknown state: ${cell.state}`, note: null };
  }
}

/** "98.4% covered", "no recording", "not reported yet", "market closed", or the API's own short detail. A file from NSE that is fine reads "downloaded". */
export function cellWords(cell: CoverageCellDTO, datasetKey?: string): string {
  return cellParts(cell, datasetKey).words;
}

/** The line under the chip, when the API had more to say (such as instruments recorded before mode logging). */
export function cellNote(cell: CoverageCellDTO, datasetKey?: string): string | null {
  return cellParts(cell, datasetKey).note;
}

/**
 * Gap reasons in plain words. The keys are the reasons of A1's `gap-reasons` block plus the recorder-only `queue_full`;
 * tests/stockify/research_check.ts reads that block and fails when a reason has no word here.
 */
export const GAP_REASON_WORDS: Record<string, string> = {
  disconnect: "the connection to Kite dropped",
  process_stop: "the recorder was stopped",
  token_expired: "the Kite login expired",
  stall: "an instrument stopped sending data",
  not_subscribed: "the instrument was not subscribed",
  queue_full: "the recorder's queue was full",
};

export function gapReasonWords(reason: string | null | undefined): string {
  const r = reason?.trim();
  if (!r) return "not stated";
  return Object.hasOwn(GAP_REASON_WORDS, r) ? GAP_REASON_WORDS[r] : `other: ${r}`;
}

/** A gap's scope: "all" or "instrument:<id>" in the recorder's tables. */
export function gapScopeWords(scope: string | null | undefined): string {
  const s = scope?.trim();
  if (!s) return "not stated";
  if (s === "all") return "everything the recorder watches";
  const m = /^instrument:(\d+)$/.exec(s);
  return m ? `one instrument (id ${m[1]})` : `other: ${s}`;
}

/** A length of time in seconds: "12 s", "2 min 5 s", "1 h 2 min"; null is a gap that never closed. */
export function durationWords(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "still open";
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const parts: string[] = [];
  if (h) parts.push(`${h} h`);
  if (m) parts.push(`${m} min`);
  if (s && !h) parts.push(`${s} s`);
  return parts.length ? parts.join(" ") : "0 s";
}

/** A size in bytes, decimal units like the server's disk warnings (1 GB = 10^9 bytes). */
export function bytesWords(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n) || n < 0) return "—";
  if (n < 1000) return `${Math.round(n)} B`;
  const units = ["kB", "MB", "GB", "TB"];
  let v = n;
  let u = -1;
  while (v >= 1000 && u < units.length - 1) {
    v /= 1000;
    u += 1;
  }
  return `${Number(v.toFixed(1))} ${units[u]}`;
}

const STALE_REPORT_S = 60;

/** The recorder strip in words: the state, the packet rate, the age of the freshest packet, and a note when the report is old or missing. */
export function recorderWords(
  s: Pick<RecorderStatus, "connected" | "packets_per_s" | "last_packet_age_ms" | "age_s"> | null | undefined,
): { headline: string; tone: "up" | "down" | "warn"; rate: string; last: string; note: string | null } {
  if (!s) {
    return {
      headline: "The recorder has not reported yet", tone: "warn", rate: "—", last: "—",
      note: "Start the recorder to see live figures.",
    };
  }
  const ages = Object.values(s.last_packet_age_ms ?? {}).filter((a): a is number => typeof a === "number" && Number.isFinite(a));
  const freshest = ages.length ? Math.min(...ages) : null;
  const last = freshest === null
    ? "no packet seen yet"
    : `last packet ${freshest < 1000 ? `${Math.round(freshest)} ms` : durationWords(freshest / 1000)} ago`;
  const rate = `${Number((Number.isFinite(s.packets_per_s) ? s.packets_per_s : 0).toFixed(1))} packets a second`;
  if (s.age_s > STALE_REPORT_S) {
    return {
      headline: "Not reporting", tone: "warn", rate, last,
      note: `The last report was ${durationWords(s.age_s)} ago, so these figures may be out of date.`,
    };
  }
  return { headline: s.connected ? "Connected" : "Not connected", tone: s.connected ? "up" : "down", rate, last, note: null };
}

/** Today's date in India (YYYY-MM-DD), whatever the viewer's own time zone. */
export function istToday(now: Date): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata", year: "numeric", month: "2-digit", day: "2-digit" }).format(now);
}

/** An ISO day shifted by whole days (calendar arithmetic, no time zone involved). */
export function addDays(iso: string, n: number): string {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + n)).toISOString().slice(0, 10);
}

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2026-10-07" as "Wed 7 Oct"; anything that is not a day comes back unchanged. */
export function dayLabel(iso: string): string {
  const ok = parseDay(iso);
  if (!ok) return iso;
  const [y, m, d] = ok.split("-").map(Number);
  const wd = new Date(Date.UTC(y, m - 1, d)).getUTCDay();
  return `${WEEKDAYS[wd]} ${d} ${MONTHS[m - 1]}`;
}

/** The text when it is a real YYYY-MM-DD day, else null (the API refuses anything looser). */
export function parseDay(text: string | null | undefined): string | null {
  if (!text || !/^\d{4}-\d{2}-\d{2}$/.test(text)) return null;
  const [y, m, d] = text.split("-").map(Number);
  const back = new Date(Date.UTC(y, m - 1, d));
  return back.toISOString().slice(0, 10) === text && y >= 1000 ? text : null;
}

export const RANGE_CHOICES = [30, 60, 180] as const;

/** The range from the `days` search parameter: one of 30, 60 or 180, else 60. */
export function rangeDays(param: string | null | undefined): number {
  const n = param ? Number(param) : Number.NaN;
  return (RANGE_CHOICES as readonly number[]).includes(n) ? n : 60;
}

/**
 * The IST day a coverage window ends on: the `to` param when it is a real day that is not after today, else today. The window
 * is `days` long and ends there ("Earlier" and "Later" move it by whole windows, within the API's max_days).
 */
export function windowEnd(param: string | null | undefined, today: string): string {
  const d = parseDay(param);
  return d !== null && d <= today ? d : today;
}

/** The window of `days` days ending at `end` (both ends inclusive), and the end of the window before it. */
export function windowBounds(days: number, end: string): { from: string; to: string; earlier: string | null } {
  const earlier = addDays(end, -days);
  return { from: addDays(end, -(days - 1)), to: end, earlier: parseDay(earlier) };
}

/** The end of the window after the one ending at `end`, stopping at today; null when `end` is today (nothing later). */
export function laterEnd(end: string, days: number, today: string): string | null {
  if (end >= today) return null;
  const next = addDays(end, days);
  return next > today ? today : next;
}

/** The instruments with the lowest coverage first (ties by id, no figure last). */
export function worstCoverage(items: InstrumentQuality[], n = 10): InstrumentQuality[] {
  const cov = (i: InstrumentQuality) => {
    const v = i.metrics.coverage_pct;
    return typeof v === "number" && Number.isFinite(v) ? v : Number.POSITIVE_INFINITY;
  };
  return [...items].sort((a, b) => cov(a) - cov(b) || a.instrument_id - b.instrument_id).slice(0, n);
}

/** A stored quality report that came back 200 but with `errors`: the store it could not read. The sentences are the API's own,
 * shown as they come; `hidden` names what the page then leaves out ("" when the unreadable store is not behind this list).
 * Null for a clean report. The list of lowest-coverage instruments sits on the recorded-day store, so it is hidden
 * unless the only store named is another one. */
export function qualityNotice(report: { errors?: string[]; error_stores?: string[] }): { sentences: string[]; hidden: string } | null {
  const sentences = report.errors ?? [];
  if (sentences.length === 0) return null;
  const stores = report.error_stores ?? [];
  const behindList = stores.length === 0 || stores.includes("refdata");
  return {
    sentences,
    hidden: behindList ? "the list of lowest-coverage instruments is hidden, because a store that cannot be read looks the same as an empty one." : "",
  };
}

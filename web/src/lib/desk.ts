/**
 * The holdings desk (HD 10): calls for /api/desk and the words the desk pages use. The desk suggests; it never
 * places an order, and it never shows the owner's average cost, profit or the ₹ outcome of a level (owner,
 * 2026-11-17).
 */
import { api, ApiError } from "./api";
import type { ChartModel } from "./chart";
import { pathSegment } from "./pathSeg";
import type { DeskGrade, DeskListDTO, DeskSignalDTO, DeskViewDTO } from "./types";

/** How often a live view and its chart are asked for again during the session. */
export const DESK_POLL_MS = 60_000;

/** The sentence the API adds to a view's `plain` for a stock without options; the option section says it instead. */
export const NO_OPTIONS = "No options are listed on NSE for this stock.";

export type DeskInterval = "day" | "minute";

/** GET /api/desk: a verdict per holding the desk grades, and the others with the reason. */
export function getDeskList(init?: RequestInit): Promise<DeskListDTO> {
  return api<DeskListDTO>("/api/desk", init);
}

/** What a holding page can be: the view, or why there is none (404: the API's reason; unreachable: the API is down). */
export type DeskViewResult =
  | { kind: "view"; view: DeskViewDTO }
  | { kind: "missing"; reason: string }
  | { kind: "error"; message: string };

/** GET /api/desk/{symbol}. A 404 carries the reason (not one of your holdings, BSE only, not graded yet). */
export async function getDeskView(symbol: string, signal?: AbortSignal): Promise<DeskViewResult> {
  try {
    return { kind: "view", view: await api<DeskViewDTO>(`/api/desk/${pathSegment(symbol)}`, { signal }) };
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return { kind: "missing", reason: e.message };
    return { kind: "error", message: e instanceof Error ? e.message : String(e) };
  }
}

/** GET /api/desk/{symbol}/chart?interval=day|minute, the run chart's shape; a 404 gives null. */
export async function getDeskChart(symbol: string, interval: DeskInterval, signal?: AbortSignal): Promise<ChartModel | null> {
  try {
    return await api<ChartModel>(`/api/desk/${pathSegment(symbol)}/chart?interval=${interval}`, { signal });
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
}

export type VerdictTone = "down" | "warn" | "up" | "muted";

/** Exit is the loss colour, Trim and Tighten stop need you (warn), Hold is the gain colour, no reliable signal is muted. */
export function verdictTone(verdict: string): VerdictTone {
  switch (verdict) {
    case "Exit":
      return "down";
    case "Trim":
    case "Tighten stop":
      return "warn";
    case "Hold":
      return "up";
    default:
      return "muted";
  }
}

/** The text class for a verdict word set without a frame (in a table, in running text). */
export function verdictClass(verdict: string): string {
  const t = verdictTone(verdict);
  return t === "down" ? "stk-down" : t === "warn" ? "stk-warn" : t === "up" ? "stk-up" : "stk-muted";
}

/** A skip reason from the API ("BSE only", "not graded yet: ...") as a sentence-case phrase. */
export function reasonWords(reason: string): string {
  const r = reason.trim();
  return r ? r[0].toUpperCase() + r.slice(1) : r;
}

/** UTC epoch ns as "17 Nov 2026, 11:02 IST". */
export function nsWhen(ns: number): string {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata", day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(new Date(ns / 1e6));
  const p = (t: string) => parts.find((x) => x.type === t)?.value ?? "";
  return `${p("day")} ${p("month")} ${p("year")}, ${p("hour")}:${p("minute")} IST`;
}

/** UTC epoch ns as "11:02 IST". */
export function nsTime(ns: number): string {
  const parts = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false })
    .formatToParts(new Date(ns / 1e6));
  const p = (t: string) => parts.find((x) => x.type === t)?.value ?? "";
  return `${p("hour")}:${p("minute")} IST`;
}

/** What a rule says now, in words. A rule that is out but would buy today counts as in (its rule is buying here). */
export function signalWords(s: DeskSignalDTO): { text: string; out: boolean } {
  if (s.in_position) return { text: "Stay in", out: false };
  if (s.buying_today) return { text: "Buying here", out: false };
  return { text: "Be out", out: true };
}

export const GRADE_WORDS: Record<DeskGrade, string> = {
  strong: "Strong",
  some: "Some",
  weak: "Weak",
  none: "None",
};

/** A grade's text class: a record that carries no weight (none) is muted; the others read in ink. */
export function gradeClass(g: DeskGrade): string | undefined {
  return g === "none" ? "stk-muted" : undefined;
}

/** The API's sentence for an F&O stock whose chain has not been read yet; the option section says it on its own. */
export const NO_CHAIN_YET = "No option chain has been read for this stock yet; it is read during the session.";

/** A view's `plain`, without the sentences the option section says on its own (no options, no chain read yet). */
export function plainWithoutOptions(plain: string): string {
  return plain.replace(NO_OPTIONS, "").replace(NO_CHAIN_YET, "").replace(/\s{2,}/g, " ").trim();
}

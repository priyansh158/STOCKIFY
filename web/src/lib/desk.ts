/**
 * The holdings desk (HD 10, 12): calls for /api/desk and the words the desk pages use. The desk suggests; it never
 * places an order, and it never shows the owner's average cost, profit or the ₹ outcome of a level (owner,
 * 2026-11-17). It updates once a day, after the session, so nothing here asks again on a timer.
 */
import { api, ApiError } from "./api";
import type { ChartModel } from "./chart";
import { dayWords } from "./format";
import { pathSegment } from "./pathSeg";
import type { DeskGrade, DeskSignalDTO, DeskViewDTO } from "./types";

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

/** GET /api/desk/{symbol}/chart: the daily candles in the run chart's shape; a 404 gives null. */
export async function getDeskChart(symbol: string, signal?: AbortSignal): Promise<ChartModel | null> {
  try {
    return await api<ChartModel>(`/api/desk/${pathSegment(symbol)}/chart`, { signal });
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

/** "As of the close on 16 Nov 2026": the close a stored view is from (its IST date), else when it was stored. */
export function asOfClose(closeDate: string | null | undefined, asOfNs: number): string {
  return closeDate ? `As of the close on ${dayWords(closeDate)}` : `As of ${nsWhen(asOfNs)}`;
}

/** What a rule says now, in words. A rule that is out but would buy today counts as staying in. */
export function signalWords(s: DeskSignalDTO): { text: string; out: boolean } {
  if (s.in_position) return { text: "Stay in", out: false };
  if (s.buying_today) return { text: "Would buy today", out: false };
  return { text: "Get out", out: true };
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

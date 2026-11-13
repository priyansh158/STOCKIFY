/** Pure helpers for the research pages. No imports of the fetch wrapper, so Node can run them directly in the checks. */
import type { RegimeBucketDTO } from "./types";

/** Which of `steps` equal bands of [min, max] a value falls in (0 to steps-1). Out of range clamps; a flat range or NaN is 0. */
export function heatColourStep(value: number, min: number, max: number, steps: number): number {
  if (!(steps > 1) || !Number.isFinite(value) || !(max > min)) return 0;
  const band = Math.floor(((value - min) / (max - min)) * steps);
  return Math.min(steps - 1, Math.max(0, band));
}

export type BucketTone = "up" | "down" | "flat" | "thin";

/** A regime bucket's tone: too few days to read is "thin" whatever its sign; otherwise the sign of its pnl. */
export function bucketTone(bucket: Pick<RegimeBucketDTO, "thin" | "pnl_paise">): BucketTone {
  if (bucket.thin) return "thin";
  if (bucket.pnl_paise > 0) return "up";
  if (bucket.pnl_paise < 0) return "down";
  return "flat";
}

/** A plain-language sentence from the API, or an em dash when there is none. */
export function sentenceOrDash(s: string | null | undefined): string {
  const t = s?.trim();
  return t ? t : "—";
}

/**
 * The colour tokens of a line chart series, in order. This is the palette the dataviz validator passed on the dark
 * surface (chroma, deutan and normal-vision separation, contrast); a fourth series reuses a colour and differs by dash and
 * marker. globals.css (.stk-lc-line[data-c]) must list the same tokens, which tests/stockify/research_check.ts checks.
 */
export const SERIES_TOKENS = ["accent", "info", "warn"] as const;

/** Which way a heat grid shade runs, for its caption: on a dark surface a larger value is brighter, on a light one darker. */
export function heatMoreWord(scheme: "dark" | "light"): "brighter" | "darker" {
  return scheme === "dark" ? "brighter" : "darker";
}

/**
 * The run's cost multiplier as text when it is not 1 (config `costs.scale`, a decimal string), else null. Anything
 * unreadable gives null (no banner); 0 (free trading) is shown, since that is the most flattering result of all.
 */
export function costScale(config: Record<string, unknown> | null | undefined): string | null {
  const costs = config?.costs;
  const raw = costs && typeof costs === "object" ? (costs as Record<string, unknown>).scale : undefined;
  if (raw === undefined || raw === null || (typeof raw === "string" && raw.trim() === "")) return null;
  const n = Number(raw);
  if (!Number.isFinite(n) || n < 0 || n === 1) return null;
  return String(n);
}

/** The overview's link to a trial family. Family ids hold "/", so the id rides fully encoded in the query. */
export function familyHref(id: string): string {
  return `/research/family?id=${encodeURIComponent(id)}`;
}

/** The page of a sweep or walk-forward job; null for any other kind of job (it has no page). */
export function jobHref(kind: string, id: string): string | null {
  if (kind === "sweep") return `/research/sweeps/${encodeURIComponent(id)}`;
  if (kind === "walkforward") return `/research/walkforward/${encodeURIComponent(id)}`;
  return null;
}

/**
 * The line under a list that was cut at its page size: how many are shown of how many exist, and which end is shown.
 * Null when everything is shown or the total is not known (a missing total is never guessed).
 */
export function listCapNote(shown: number, total: number | null, what: string, order: "first" | "latest"): string | null {
  if (total === null || !(total > shown)) return null;
  return `Showing the ${order} ${shown.toLocaleString("en-IN")} of ${total.toLocaleString("en-IN")} ${what}. ${
    order === "first" ? "The rest are not listed here." : "Older ones are not listed here."
  }`;
}

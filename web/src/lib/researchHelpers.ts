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

/** Whole days since 1970-01-01 of an ISO date (YYYY-MM-DD), so dates can sit on a numeric chart axis. */
export function dayNumber(iso: string): number {
  return Math.round(Date.parse(`${iso}T00:00:00Z`) / 86_400_000);
}

/** One line segment per run of points: `breaks` lists the x values where a new segment starts, so no line is drawn across the gap. */
export function splitSegments(points: [number, number][], breaks: number[] = []): [number, number][][] {
  const sorted = [...points].sort((a, b) => a[0] - b[0]);
  const starts = new Set(breaks);
  const out: [number, number][][] = [];
  for (const p of sorted) {
    if (out.length === 0 || starts.has(p[0])) out.push([p]);
    else out[out.length - 1].push(p);
  }
  return out;
}

export interface WfCurve {
  points: [number, number][];
  /** x of the first point of every tested window after the first: where a new segment begins. */
  breaks: number[];
  /** x of each tested window's first test day (the dotted boundary lines, as in walkforward.png). */
  starts: number[];
}

/** The stitched out-of-sample curve as chart points, cut into one segment per tested window by that window's own test
 * dates, with its test starts. A window with no test result has no segment and no boundary. */
export function wfCurve(
  dates: string[],
  index: number[],
  windows: { test_from: string; test?: { from?: unknown; to?: unknown } | null }[],
): WfCurve {
  const n = Math.min(dates.length, index.length);
  const points: [number, number][] = [];
  for (let i = 0; i < n; i++) points.push([dayNumber(dates[i]), index[i]]);
  const breaks: number[] = [];
  const starts: number[] = [];
  let first = true;
  for (const w of windows) {
    const t = w.test;
    if (!t || typeof t.from !== "string" || typeof t.to !== "string") continue;
    starts.push(dayNumber(w.test_from));
    const i = dates.findIndex((d, k) => k < n && d >= (t.from as string) && d <= (t.to as string));
    if (i >= 0) {
      if (!first) breaks.push(points[i][0]);
      first = false;
    }
  }
  return { points, breaks, starts };
}

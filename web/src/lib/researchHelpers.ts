/** Pure helpers for the research pages. No imports of the fetch wrapper, so Node can run them directly in the checks. */
import type { RegimeBucketDTO, SweepHeatmapDTO } from "./types";

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
 * The two arms of a scale centred on zero: a value above zero is `info` (blue), below zero is `warn` (orange). The pair passed
 * the dataviz validator on the dark surface for colour-blind separation (deutan and protan 22, tritan 25). The caption's colour
 * words come from here; globals.css (.stk-heat-cell[data-sign]) must use the same tokens, which research_check.ts checks.
 */
export const DIVERGING = {
  positive: { token: "info", word: "blue" },
  negative: { token: "warn", word: "orange" },
} as const;

/** Levels of strength on each side of zero. */
export const DIVERGING_LEVELS = 3;

/**
 * Where a value sits on a scale centred on zero: -levels to levels, 0 for zero, NaN or a zero range. The size is how far
 * from zero it is as a share of `maxAbs` (the largest absolute value in the grid); the sign is kept. Beyond the range clamps.
 */
export function divergingStep(value: number, maxAbs: number, levels: number = DIVERGING_LEVELS): number {
  if (!Number.isFinite(value) || !(maxAbs > 0) || value === 0) return 0;
  const level = 1 + Math.min(levels - 1, Math.floor((Math.abs(value) / maxAbs) * levels));
  return value < 0 ? -level : level;
}

const MINUS = "−";
const INR_WHOLE = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });

/**
 * A number formatter for a sweep objective's format kind (ratio, percent as a fraction, paise as rupees). When `signed`, a
 * positive value carries "+" and a negative one a true minus sign, so the sign is read in the text as well as the colour.
 * An unknown kind is a plain number; NaN is a dash.
 */
export function heatFormat(kind: string, signed: boolean): (n: number) => string {
  return (n) => {
    if (!Number.isFinite(n)) return "—";
    const sign = n < 0 ? MINUS : signed && n > 0 ? "+" : "";
    const a = Math.abs(n);
    if (kind === "ratio") return `${sign}${a.toFixed(2)}`;
    if (kind === "percent") return `${sign}${(a * 100).toFixed(2)} %`;
    if (kind === "paise") return `${sign}₹${INR_WHOLE.format(Math.round(a) / 100)}`;
    return `${sign}${Number(a.toPrecision(6))}`;
  };
}

/** What the page hands HeatGrid: the sweep's cells as they are, `format` as a function, `best` {xi, yi} as {x, y}. */
export function heatGridProps(d: SweepHeatmapDTO) {
  return {
    xs: d.xs,
    ys: d.ys,
    cells: d.cells,
    format: heatFormat(d.format, d.signed),
    best: d.best ? { x: d.best.xi, y: d.best.yi } : null,
    signed: d.signed,
    xLabel: d.x,
    yLabel: d.y ?? "Objective",
  };
}

export type SweepGone = "running" | "failed" | "missing" | "unreadable";

/** Which plain state a sweep's 404 detail from the server stands for (see sweep_detail in server/routers/research.py). */
export function sweepMissingKind(detail: string): SweepGone {
  const m = /^sweep not finished yet \(status: ([a-z_]+)\)$/.exec(detail.trim());
  if (m) return m[1] === "failed" ? "failed" : "running";
  if (detail.trim() === "the sweep has no readable stored result") return "unreadable";
  return "missing";
}

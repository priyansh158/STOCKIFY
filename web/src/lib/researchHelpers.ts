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
 * The colour tokens of a line chart series, in order. On the dark surface this set passes the dataviz validator's
 * colour-blind, normal-vision and contrast checks and fails only the lightness band (informational). A fourth to sixth series reuses
 * a colour, so each line is also named at its end, carries markers and has a dash of its own. globals.css (.stk-lc-line[data-c]) must list the same tokens, which tests/stockify/research_check.ts checks.
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

/**
 * The family id out of the page's `?id=`. Next hands the query over already decoded once (familyHref encoded it once), so
 * nothing is decoded again here: a second decode would turn an id like "100%25" into "100%". Repeated parameters take the
 * first; a missing or blank one is null.
 */
export function familyIdParam(raw: string | string[] | undefined): string | null {
  const v = Array.isArray(raw) ? raw[0] : raw;
  return typeof v === "string" && v.trim() !== "" ? v : null;
}

/** What a trial table's "score" column holds, in the family's own objective: paise as rupees, growth as a percent, the rest as a ratio. */
export function objectiveText(value: number | null | undefined, objective: string): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  if (objective === "net_pnl") return new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" }).format(value / 100);
  if (objective === "cagr") return `${(value * 100).toFixed(1)}%`;
  return value.toFixed(2);
}

const ROLE_WORDS: Record<string, string> = {
  sensitivity: "sensitivity check",
  oos: "out-of-sample test",
  rerun: "re-run",
};

/** A run's role in words; a role nobody has named is shown as it is stored. */
export function roleWords(role: string): string {
  return ROLE_WORDS[role] ?? role;
}

export interface UnlockRow {
  when_ns: number | null;
  run_id: string | null;
  reason: string;
}

export interface SessionRow {
  session_id: string;
  name: string | null;
  status: string | null;
  created_ns: number | null;
}

const text = (v: unknown): string | null => (typeof v === "string" ? v : null);
const ns = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

/** The unlock log rows as the page reads them (the API sends loose objects). The reason is owner text and stays a string. */
export function unlockRows(raw: Record<string, unknown>[] | null | undefined): UnlockRow[] {
  return (raw ?? []).map((r) => ({ when_ns: ns(r.unlock_ns), run_id: text(r.run_id), reason: text(r.reason) ?? "" }));
}

/** The family's paper sessions as the page reads them; a row without an id is dropped. */
export function sessionRows(raw: Record<string, unknown>[] | null | undefined): SessionRow[] {
  return (raw ?? []).flatMap((r) => {
    const id = text(r.session_id);
    return id ? [{ session_id: id, name: text(r.name), status: text(r.status), created_ns: ns(r.created_ns) }] : [];
  });
}

/**
 * The API path of one family. The id keeps its "/" (the route is a path), every segment is encoded, and a segment that is
 * "." or ".." is written out as %2E so that fetch cannot fold it into a different family's path.
 */
export function familyApiPath(id: string): string {
  const seg = (s: string) => (s === "." ? "%2E" : s === ".." ? "%2E%2E" : encodeURIComponent(s));
  return `/api/research/families/${id.split("/").map(seg).join("/")}`;
}

/** 1 = best. Equal scores share a rank; a missing or non-finite score has none. */
export function scoreRanks(values: (number | null)[]): (number | null)[] {
  const ok = values.filter((v): v is number => typeof v === "number" && Number.isFinite(v));
  return values.map((v) => (typeof v === "number" && Number.isFinite(v) ? 1 + ok.filter((o) => o > v).length : null));
}

/**
 * The note under the overfitting sentence. When the sentence already says "based on N of M tries" only the list of tries
 * left out is kept ("Left out: ..."); any other note is shown whole. Null when there is nothing to add.
 */
export function pboNoteLine(note: string | null | undefined, sentenceShown: boolean): string | null {
  const t = note?.trim();
  if (!t) return null;
  const cap = (x: string) => `${x[0].toUpperCase()}${x.slice(1)}`;
  const at = t.indexOf("left out:");
  if (sentenceShown) return t.startsWith("based on") ? (at >= 0 ? cap(t.slice(at)) : null) : null;
  return cap(t);
}

/** How the overfitting estimate was computed, from the counts the API sends (days, blocks, ways of choosing half). */
export function pboHowText(p: { n_days: number; slices: number; combinations: number }): string {
  const n = (x: number) => x.toLocaleString("en-IN");
  return `Computed over ${n(p.n_days)} ${p.n_days === 1 ? "day" : "days"}, cut into ${n(p.slices)} blocks. Every way of choosing half the blocks (${n(
    p.combinations,
  )} in all) was set against the other half.`;
}

/** The compare page takes two to six runs (the API's own limit, GET /api/tests/compare). */
export const COMPARE_MIN = 2;
export const COMPARE_MAX = 6;

/** `/tests/compare?ids=a&ids=b…`: repeated `ids`, each encoded, cut at six. No ids gives the bare path. */
export function compareHref(ids: string[]): string {
  const q = ids.slice(0, COMPARE_MAX).map((i) => `ids=${encodeURIComponent(i)}`).join("&");
  return q ? `/tests/compare?${q}` : "/tests/compare";
}

/** The `ids` of a query string (one value or a repeated one): trimmed, no blanks, no repeats, in order. Commas are not split: an id never has one. */
export function compareIds(raw: string | string[] | undefined): string[] {
  const list = raw === undefined ? [] : Array.isArray(raw) ? raw : [raw];
  const out: string[] = [];
  for (const v of list) {
    const t = v.trim();
    if (t !== "" && !out.includes(t)) out.push(t);
  }
  return out;
}

/** One distinct display name per run: the run's name, with its id added when the name repeats, and the id when there is no name. */
export function runLabels(names: string[], ids: string[]): string[] {
  return ids.map((id, i) => {
    const name = (names[i] ?? "").trim();
    if (name === "") return id;
    return names.filter((n) => (n ?? "").trim() === name).length > 1 ? `${name} (${id})` : name;
  });
}

/** True when the runs do not all agree (a missing value counts as a value, unless every run lacks it). */
export function valuesDiffer(values: readonly (string | number | boolean | null)[]): boolean {
  return new Set(values).size > 1;
}

/**
 * Where to draw each line's end label (a y in the chart, same order as `ys`): each label sits at its line's end height, and
 * labels too close are pushed apart by `gap`, so six lines that finish together all stay readable. Stays inside [lo, hi].
 */
export function spreadLabels(ys: number[], gap: number, lo: number, hi: number): number[] {
  const order = ys.map((y, i) => i).sort((a, b) => ys[a] - ys[b] || a - b);
  const pos = new Array<number>(ys.length);
  let prev = -Infinity;
  for (const i of order) {
    prev = Math.max(ys[i], prev + gap, lo);
    pos[i] = prev;
  }
  // anything past the top edge moves down, keeping the gap, from the top label down
  let top = hi;
  for (let k = order.length - 1; k >= 0; k--) {
    const i = order[k];
    pos[i] = Math.max(lo, Math.min(pos[i], top));
    top = pos[i] - gap;
  }
  return pos;
}

/** A line's end label: its name cut to `max` characters with an ellipsis (the legend and the table keep the whole name). */
export function endLabelText(label: string, max = 16): string {
  const t = label.trim();
  return t.length <= max ? t : `${t.slice(0, max - 1)}…`;
}

/** Indices of the points that carry a marker along a line: about `target` of them, evenly spaced, offset by the series so
 * two lines' markers do not sit on each other, and always the last point (where the end label is). */
export function markerIndexes(n: number, seriesIndex: number, target = 6): number[] {
  if (n <= 0) return [];
  const step = Math.max(1, Math.ceil(n / target));
  const out: number[] = [];
  for (let i = seriesIndex % step; i < n; i += step) out.push(i);
  if (out[out.length - 1] !== n - 1) out.push(n - 1);
  return out;
}

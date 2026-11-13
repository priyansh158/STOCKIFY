// Runs the pure helpers of web/src/lib/researchHelpers.ts on fixed inputs and prints one JSON object. Node >= 22.18
// runs this file directly (type stripping); called by tests/stockify/test_research_checks.py, which holds the
// hand-checked answers. The helpers live apart from research.ts because that file imports the fetch wrapper.
import { readFileSync } from "node:fs";
import { SERIES_TOKENS, bucketTone, costScale, heatColourStep, heatMoreWord, sentenceOrDash } from "../../web/src/lib/researchHelpers.ts";

import {
  GAP_REASON_WORDS, addDays, bytesWords, cellNote, cellSymbol, cellTone, cellWords, dayLabel, durationWords, gapReasonWords,
  gapScopeWords, istToday, parseDay, rangeDays, recorderWords, worstCoverage,
} from "../../web/src/lib/data.ts";

// The reasons the recorder can write, read from the gap-reasons block of A1 (so a new reason there fails the check below).
const a1 = readFileSync(new URL("../../docs/plan/A1_schemas.md", import.meta.url), "utf-8");
const a1Reasons = [...(/<block name="gap-reasons"[^>]*>([\s\S]*?)<\/block>/.exec(a1)?.[1] ?? "").matchAll(/\d ([a-z_]+)/g)].map((m) => m[1]);

const css = readFileSync(new URL("../../web/src/app/globals.css", import.meta.url), "utf-8");
const colourRules = [...css.matchAll(/\.stk-lc-line\[data-c="(\d)"\]\s*\{\s*stroke:\s*var\(--color-([a-z0-9-]+)\)/g)].map((m) => [m[1], m[2]]);
const heatMixes = [...css.matchAll(/\.stk-heat-cell\[data-step="(\d)"\]\s*\{\s*background:\s*color-mix\(in oklab, var\(--color-accent\) (\d+)%/g)].map((m) => Number(m[2]));

const bucket = (pnl: number, thin = false) => ({
  label: "x", days: 1, pnl_paise: pnl, mean_pnl_paise: pnl, up_days: 0, up_share: null, thin,
});

const out = {
  heat: [
    [0, 0, 10, 5], [10, 0, 10, 5], [5, 0, 10, 5], [4.99, 0, 10, 5], [6, 0, 10, 5],
    [-3, 0, 10, 5], [99, 0, 10, 5], [5, 5, 5, 5], [Number.NaN, 0, 10, 5], [3, 0, 10, 1], [3, 0, 10, 0],
  ].map(([v, lo, hi, n]) => heatColourStep(v, lo, hi, n)),
  tone: [bucket(500), bucket(-500), bucket(0), bucket(500, true), bucket(-500, true)].map(bucketTone),
  seriesTokens: [...SERIES_TOKENS],
  seriesCss: colourRules,
  heatMixes,
  scheme: /:root\s*\{[^}]*color-scheme:\s*(\w+)/.exec(css)?.[1] ?? null,
  more: [heatMoreWord("dark"), heatMoreWord("light")],
  costScale: [undefined, {}, { costs: {} }, { costs: { scale: "1" } }, { costs: { scale: "1.00" } }, { costs: { scale: "2" } },
    { costs: { scale: "1.50" } }, { costs: { scale: "0" } }, { costs: { scale: "x" } }, { costs: { scale: null } },
    { costs: "no" }].map((c) => costScale(c as Record<string, unknown> | undefined)),
  a1Reasons,
  reasonWords: a1Reasons.map((r) => gapReasonWords(r)),
  reasonKeysCovered: a1Reasons.every((r) => r in GAP_REASON_WORDS),
  reasonOther: [gapReasonWords("brand_new"), gapReasonWords(""), gapReasonWords(null), gapReasonWords(undefined)],
  scope: ["all", "instrument:738561", null, "weird"].map((x) => gapScopeWords(x)),
  cells: [
    { state: "ok", value: 98.4 }, { state: "ok", value: 100 }, { state: "partial", value: 97.25, detail: "2 instruments recorded before mode logging; not counted" },
    { state: "ok", detail: "verified" }, { state: "ok" }, { state: "partial", detail: "landing" }, { state: "partial", detail: "downloaded, not parsed" },
    { state: "missing", detail: "no recording for this day" }, { state: "missing" }, { state: "missing", detail: "not downloaded" },
    { state: "missing", detail: "download failed (http 503)" }, { state: "not_reported", detail: "the quality report has not run for this day" },
    { state: "closed" }, { state: "not_applicable", detail: "not yet" },
    { state: "not_applicable", detail: "no instruments recorded in this mode; 2 instruments recorded before mode logging; not counted" },
    { state: "not_applicable" }, { state: "surprise" },
  ].map((c) => [cellWords(c), cellTone(c.state), cellSymbol(c.state), cellNote(c)]),
  nseWords: [cellWords({ state: "ok" }, "nse_fo"), cellWords({ state: "ok" }, "archive"), cellWords({ state: "ok", detail: "verified" }, "nse_cm"), cellWords({ state: "partial" }, "nse_idx")],
  durations: [null, undefined, 0, 12, 59.6, 125, 3600, 3725, Number.NaN].map((x) => durationWords(x)),
  bytes: [null, 0, 999, 1500, 2_500_000, 12_345_678_901, 3_000_000_000_000, Number.NaN].map((x) => bytesWords(x)),
  recorder: [
    null,
    { connected: true, packets_per_s: 12.34, last_packet_age_ms: { full: 340, quote: 2100 }, age_s: 3 },
    { connected: true, packets_per_s: 0, last_packet_age_ms: {}, age_s: 3 },
    { connected: false, packets_per_s: 0, last_packet_age_ms: { full: 125_000 }, age_s: 4 },
    { connected: true, packets_per_s: 5, last_packet_age_ms: { full: null }, age_s: 400 },
  ].map((r) => recorderWords(r as never)),
  days: {
    today: [istToday(new Date("2026-10-06T19:00:00Z")), istToday(new Date("2026-10-06T18:29:00Z"))],
    add: [addDays("2026-10-07", -29), addDays("2026-03-01", -1), addDays("2024-02-28", 1), addDays("2026-12-31", 1)],
    label: [dayLabel("2026-10-07"), dayLabel("2026-01-01"), dayLabel("nonsense")],
    parse: ["2026-10-07", "2026-02-30", "20261007", "2026-10-7", "", null, undefined].map((x) => parseDay(x)),
    range: [undefined, "30", "60", "180", "90", "abc", "30.5"].map((x) => rangeDays(x)),
  },
  worst: worstCoverage([
    { instrument_id: 3, metrics: { coverage_pct: 99 } }, { instrument_id: 1, metrics: { coverage_pct: 40 } },
    { instrument_id: 2, metrics: {} }, { instrument_id: 4, metrics: { coverage_pct: 40 } }, { instrument_id: 5, metrics: { coverage_pct: 70 } },
  ], 4).map((i) => i.instrument_id),
  sentence: [null, undefined, "", "   ", "  Held up.  ", "A. B."].map(sentenceOrDash),
};
process.stdout.write(JSON.stringify(out));

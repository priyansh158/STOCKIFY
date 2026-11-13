// Runs the pure helpers of web/src/lib/researchHelpers.ts on fixed inputs and prints one JSON object. Node >= 22.18
// runs this file directly (type stripping); called by tests/stockify/test_research_checks.py, which holds the
// hand-checked answers. The helpers live apart from research.ts because that file imports the fetch wrapper.
import { readFileSync } from "node:fs";
import { pathSegment } from "../../web/src/lib/pathSeg.ts";
import { COMPARE_MAX, COMPARE_MIN, DIVERGING, SERIES_TOKENS, bucketTone, compareHref, compareIds, costScale, divergingStep, endLabelText, familyApiPath, familyHref, familyIdParam, heatColourStep, heatFormat, heatGridProps, heatMoreWord, jobHref, listCapNote, markerIndexes, objectiveText, pboHowText, pboNoteLine, roleWords, runLabels, scoreRanks, sentenceOrDash, sessionRows, splitSegments, spreadLabels, sweepFailureReason, sweepMissingKind, unlockRows, valuesDiffer, wfCurve } from "../../web/src/lib/researchHelpers.ts";

import {
  GAP_REASON_WORDS, addDays, bytesWords, cellNote, cellSymbol, cellTone, cellWords, dayLabel, durationWords, gapReasonWords,
  gapScopeWords, istToday, laterEnd, parseDay, qualityNotice, rangeDays, recorderWords, windowBounds, windowEnd, worstCoverage,
} from "../../web/src/lib/data.ts";

// The reasons the recorder can write, read from the gap-reasons block of A1 (so a new reason there fails the check below).
const a1 = readFileSync(new URL("../../docs/plan/A1_schemas.md", import.meta.url), "utf-8");
const a1Reasons = [...(/<block name="gap-reasons"[^>]*>([\s\S]*?)<\/block>/.exec(a1)?.[1] ?? "").matchAll(/\d ([a-z_]+)/g)].map((m) => m[1]);

const css = readFileSync(new URL("../../web/src/app/globals.css", import.meta.url), "utf-8");
const colourRules = [...css.matchAll(/\.stk-lc-line\[data-c="(\d)"\]\s*\{\s*stroke:\s*var\(--color-([a-z0-9-]+)\)/g)].map((m) => [m[1], m[2]]);
const heatMixes = [...css.matchAll(/\.stk-heat-cell\[data-step="(\d)"\]\s*\{\s*background:\s*color-mix\(in oklab, var\(--color-accent\) (\d+)%/g)].map((m) => Number(m[2]));

const divCss = [...css.matchAll(/\.stk-heat-cell\[data-sign="(pos|neg)"\]\[data-step="(\d)"\]\s*\{\s*background:[^;]*?var\(--color-([a-z]+)\) (\d+)%/g)].map((m) => [m[1], Number(m[2]), m[3], Number(m[4])]);

const dto = (over: object) => ({
  x: "fast", y: "slow", xs: ["5", "10"], ys: ["20", "30"], x_values: [5, 10], y_values: [20, 30],
  cells: [[0.5, -0.2], [1.0, null]], run_ids: [["a", "b"], ["c", null]], best: { xi: 0, yi: 1, run_id: "c", value: 1.0 },
  signed: true, format: "ratio", hidden_axes: [], ...over,
});
const gp = heatGridProps(dto({}) as never);
const gl = heatGridProps(dto({}) as never, { labels: { fast: "Fast average", slow: "Slow average" } });
const one = heatGridProps(dto({
  y: null, ys: ["sharpe"], y_values: [null], xs: ["5", "10", "15"], cells: [[0.5, -0.2, 1.2]], run_ids: [["a", "b", "c"]],
  best: { xi: 2, yi: 0, run_id: "c", value: 1.2 },
}) as never, { scored: "Steadiness of returns" });
const pc = heatGridProps(dto({ format: "percent", signed: true }) as never);
const money = heatGridProps(dto({ format: "paise", signed: false }) as never);

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
  segments: [
    splitSegments([[3, 1.3], [1, 1.1], [2, 1.2], [5, 1.5], [9, 0.9]], [5, 9]).map((s) => s.map((p) => p[0])),
    splitSegments([[1, 1], [2, 1]]).map((s) => s.length),
    splitSegments([]).length,
  ],
  curve: wfCurve(
    ["2024-01-01", "2024-01-02", "2024-01-10", "2024-01-11", "2024-01-12"], [1.01, 1.02, 1.0, 0.99, 1.03],
    [
      { test_from: "2024-01-01", test: { from: "2024-01-01", to: "2024-01-02" } },
      { test_from: "2024-01-05", test: null },
      { test_from: "2024-01-09", test: { from: "2024-01-10", to: "2024-01-12" } },
    ],
  ),
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
    // REQ-113: the window the page shows ends at `to` (an IST day, never after today), `days` long; Earlier and Later step by a window.
    windowEnd: [undefined, "2026-06-30", "2026-11-13", "2027-01-01", "2026-02-30", "x"].map((x) => windowEnd(x, "2026-11-13")),
    windows: [30, 60, 180].map((n) => {
      const a = windowBounds(n, "2026-11-13");
      const b = windowBounds(n, a.earlier!);
      return { a, b, later: [laterEnd(a.to, n, "2026-11-13"), laterEnd(b.to, n, "2026-11-13"), laterEnd("2026-11-10", n, "2026-11-13")] };
    }),  },
  worst: worstCoverage([
    { instrument_id: 3, metrics: { coverage_pct: 99 } }, { instrument_id: 1, metrics: { coverage_pct: 40 } },
    { instrument_id: 2, metrics: {} }, { instrument_id: 4, metrics: { coverage_pct: 40 } }, { instrument_id: 5, metrics: { coverage_pct: 70 } },
  ], 4).map((i) => i.instrument_id),
  qualityNotice: [
    qualityNotice({ errors: [], error_stores: [] }),
    qualityNotice({ errors: ["Couldn't read the recorded-day and download store."], error_stores: ["refdata"] }),
    qualityNotice({ errors: ["Couldn't read the recorder status."], error_stores: ["lab"] }),
    qualityNotice({ errors: ["Couldn't read the bar store."] }),
  ],
  hrefs: {
    family: ["sweep/ab12", "a b&c=d?e#f", "plain", "100%"].map(familyHref),
    job: [["sweep", "sweep-1"], ["walkforward", "wf/1 x"], ["sensitivity", "s1"], ["", "x"]].map(([k, i]) => jobHref(k, i)),
  },
  capNote: [[50, 50, "families", "first"], [50, 130, "sweeps", "latest"], [3, 3, "sweeps", "latest"], [50, 51, "walk-forward runs", "latest"], [0, 0, "sweeps", "latest"], [50, null, "sweeps", "latest"]]
    .map(([n, t, w, o]) => listCapNote(n as number, t as number | null, w as string, o as "first" | "latest")),
  // the commands the data page prints, read from its source so a corrupted backslash shows up here
  dataPageCommands: [...readFileSync(new URL("../../web/src/app/data/page.tsx", import.meta.url), "utf-8").matchAll(/<code>([^<]*)<\/code>/g)].map((m) => m[1]),
  webSourceHasStrayCR: ["web/src/app/data/page.tsx", "web/src/app/research/page.tsx", "web/src/app/research/family/page.tsx", "web/src/lib/researchHelpers.ts"]
    .map((f) => /\r(?!\n)/.test(readFileSync(new URL(`../../${f}`, import.meta.url), "utf-8"))),
  // the family page's id: encoded once into the link, read once by the page (URLSearchParams decodes as Next does)
  familyRoundTrip: ["sweep/ab12/sharpe", "a b&c=d?e#f", "100%", "100%25", "strat/ds-1m-RELIANCE/sharpe", "ü/x+y"]
    .map((id) => familyIdParam(new URLSearchParams(familyHref(id).split("?")[1]).get("id") ?? undefined) === id),
  familyIdParam: [undefined, "", "   ", "a/b", ["x/1", "y"], []].map((x) => familyIdParam(x as string | string[] | undefined)),
  objective: [[250000, "net_pnl"], [0.1234, "cagr"], [1.2345, "sharpe"], [null, "sharpe"], [Number.NaN, "calmar"]]
    .map(([v, o]) => objectiveText(v as number | null, o as string)),
  roles: ["sensitivity", "oos", "rerun", "odd"].map(roleWords),
  unlocks: unlockRows([{ unlock_ns: 1_700_000_000_000_000_000, run_id: "r1", reason: "<b>final</b> check" }, { reason: 5 }]),
  sessions: sessionRows([{ session_id: "s1", name: "n", status: "RUNNING", created_ns: 5 }, { name: "no id" }, { session_id: "s2" }]),
  apiPath: ["sweep/j1", "strat/ds-1m-RELIANCE/sharpe", "100%25", "a b/c?d#e", "a/./b", "x/../y"].map(familyApiPath),
  // L1: what the real URL parser makes of each path (the fetch URL). A path that folds loses a slash; a refused id is null.
  dotIds: ["a/./b", "x/../y", "..", ".", "%2E%2E", "a/%2e/b", "a/%2E%2E/b", "ok/id", "a..b/c.d", "100%25", "%E0%A4%A"].map((id) => {
    const p = familyApiPath(id);
    if (p === null) return { id, refused: true };
    const u = new URL(p, "http://127.0.0.1:8001");
    return { id, refused: false, path: p, slashes: [p.split("/").length, u.pathname.split("/").length], under: u.pathname.startsWith("/api/research/families/") };
  }),
  pathSegs: [".", "..", "%2E%2E", "%2e", "a b", "a.b", "100%25"].map((s) => { try { return pathSegment(s); } catch (e) { return `refused:${(e as Error).name}`; } }),
  ranks: scoreRanks([7.4, -2.7, 2.2, 2.2, null, Number.NaN, 2.1]),
  pboNote: [
    ["based on 5 of 6 tries; left out: zz (flat)", true], ["based on 5 of 6 tries; left out: zz (flat)", false],
    ["fewer than 2 tries", false], ["fewer than 2 tries", true], [null, true], ["  ", false],
  ].map(([n, h]) => pboNoteLine(n as string | null, h as boolean)),
  pboHow: [pboHowText({ n_days: 40, slices: 4, combinations: 6 }), pboHowText({ n_days: 1, slices: 2, combinations: 1 })],
  compare: {
    limits: [COMPARE_MIN, COMPARE_MAX],
    href: [
      compareHref(["a", "b"]),
      compareHref(["a", "b", "c", "d", "e", "f", "g"]),
      compareHref(["a b", "x/y&z"]),
      compareHref([]),
    ],
    ids: [
      compareIds("a"), compareIds(["a", "b"]), compareIds(undefined), compareIds(["a", " ", "a", "b "]),
      compareIds(["a,b", "c"]),
    ],
    labels: [
      runLabels(["x", "y"], ["r1", "r2"]), runLabels(["x", "x", "y"], ["r1", "r2", "r3"]), runLabels(["", "y"], ["r1", "r2"]),
    ],
    differ: [valuesDiffer([1, 1]), valuesDiffer([1, 2]), valuesDiffer([null, null]), valuesDiffer([1, null]), valuesDiffer(["a", "a", "b"]), valuesDiffer([])],
  },
  endLabels: {
    // six series ending at nearly the same height: every one still gets its own text, kept apart by at least the gap
    spread: spreadLabels([100, 102, 101, 300, 299, 50], 14, 0, 400),
    pushedUp: spreadLabels([390, 395, 399], 14, 0, 400),
    none: spreadLabels([], 14, 0, 400),
    text: [endLabelText("orb30 (orb30b)"), endLabelText("a".repeat(20)), endLabelText("")],
    marks: [markerIndexes(20, 0), markerIndexes(20, 1), markerIndexes(3, 0), markerIndexes(0, 0), markerIndexes(1, 2)],
  },
  div: [[0.1, 1], [-0.1, 1], [0, 1], [1, 1], [-1, 1], [0.5, 1], [-0.34, 1], [0.34, 1], [5, 1], [3, 0], [Number.NaN, 1]]
    .map(([v, m]) => divergingStep(v, m)),
  diverging: DIVERGING,
  divCss,
  fmt: [
    heatFormat("ratio", true)(1.234), heatFormat("ratio", true)(-0.2), heatFormat("ratio", true)(0), heatFormat("ratio", false)(-0.2),
    heatFormat("percent", true)(0.1234), heatFormat("percent", true)(-0.05), heatFormat("paise", true)(412000), heatFormat("paise", true)(-3100000),
    heatFormat("paise", false)(12345600), heatFormat("ratio", true)(Number.NaN), heatFormat("other", true)(1.5),
  ],
  grid: { x: gp.best, signed: gp.signed, xs: gp.xs, ys: gp.ys, cells: gp.cells, xLabel: gp.xLabel, yLabel: gp.yLabel, f: gp.format(1) },
  one: { ys: one.ys, best: one.best, cells: one.cells, yLabel: one.yLabel, links: one.links },
  grid2: { xLabel: gl.xLabel, yLabel: gl.yLabel, links: gl.links },
  pc: { signed: pc.signed, f: [pc.format(0.1234), pc.format(-0.05)] },
  money: { signed: money.signed, f: money.format(412000) },
  missing: ["no such sweep", "sweep not finished yet (status: running)", "sweep not finished yet (status: failed)",
    "sweep not finished yet (status: queued)", "the sweep has no readable stored result", "", "something else",
    "sweep not finished yet (status: failed): the holdout is locked", "sweep not finished yet (status: running): x"].map(sweepMissingKind),
  failure: ["sweep not finished yet (status: failed): the holdout is locked", "sweep not finished yet (status: failed)",
    "sweep not finished yet (status: failed): ", "no such sweep", "sweep not finished yet (status: running): why"].map(sweepFailureReason),
  sentence: [null, undefined, "", "   ", "  Held up.  ", "A. B."].map(sentenceOrDash),
};
process.stdout.write(JSON.stringify(out));

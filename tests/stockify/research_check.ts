// Runs the pure helpers of web/src/lib/researchHelpers.ts on fixed inputs and prints one JSON object. Node >= 22.18
// runs this file directly (type stripping); called by tests/stockify/test_research_checks.py, which holds the
// hand-checked answers. The helpers live apart from research.ts because that file imports the fetch wrapper.
import { readFileSync } from "node:fs";
import { COMPARE_MAX, COMPARE_MIN, SERIES_TOKENS, bucketTone, compareHref, compareIds, endLabelText, markerIndexes, runLabels, spreadLabels, valuesDiffer, heatColourStep, heatMoreWord, sentenceOrDash, splitSegments, wfCurve } from "../../web/src/lib/researchHelpers.ts";

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
  sentence: [null, undefined, "", "   ", "  Held up.  ", "A. B."].map(sentenceOrDash),
};
process.stdout.write(JSON.stringify(out));

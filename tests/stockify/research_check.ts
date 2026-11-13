// Runs the pure helpers of web/src/lib/researchHelpers.ts on fixed inputs and prints one JSON object. Node >= 22.18
// runs this file directly (type stripping); called by tests/stockify/test_research_checks.py, which holds the
// hand-checked answers. The helpers live apart from research.ts because that file imports the fetch wrapper.
import { readFileSync } from "node:fs";
import { SERIES_TOKENS, bucketTone, costScale, heatColourStep, heatMoreWord, sentenceOrDash } from "../../web/src/lib/researchHelpers.ts";

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
  sentence: [null, undefined, "", "   ", "  Held up.  ", "A. B."].map(sentenceOrDash),
};
process.stdout.write(JSON.stringify(out));

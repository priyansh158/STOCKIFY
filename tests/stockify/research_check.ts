// Runs the pure helpers of web/src/lib/researchHelpers.ts on fixed inputs and prints one JSON object. Node >= 22.18
// runs this file directly (type stripping); called by tests/stockify/test_research_checks.py, which holds the
// hand-checked answers. The helpers live apart from research.ts because that file imports the fetch wrapper.
import { readFileSync } from "node:fs";
import {
  DIVERGING, SERIES_TOKENS, bucketTone, divergingStep, heatColourStep, heatFormat, heatGridProps, heatMoreWord, sentenceOrDash,
  sweepFailureReason, sweepMissingKind,
} from "../../web/src/lib/researchHelpers.ts";

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

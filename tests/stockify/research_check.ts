// Runs the pure helpers of web/src/lib/researchHelpers.ts on fixed inputs and prints one JSON object. Node >= 22.18
// runs this file directly (type stripping); called by tests/stockify/test_research_checks.py, which holds the
// hand-checked answers. The helpers live apart from research.ts because that file imports the fetch wrapper.
import { bucketTone, heatColourStep, sentenceOrDash } from "../../web/src/lib/researchHelpers.ts";

const bucket = (pnl: number, thin = false) => ({
  label: "x", days: 1, pnl_paise: pnl, mean_pnl_paise: pnl, up_days: 0, up_share: null, thin,
});

const out = {
  heat: [
    [0, 0, 10, 5], [10, 0, 10, 5], [5, 0, 10, 5], [4.99, 0, 10, 5], [6, 0, 10, 5],
    [-3, 0, 10, 5], [99, 0, 10, 5], [5, 5, 5, 5], [Number.NaN, 0, 10, 5], [3, 0, 10, 1], [3, 0, 10, 0],
  ].map(([v, lo, hi, n]) => heatColourStep(v, lo, hi, n)),
  tone: [bucket(500), bucket(-500), bucket(0), bucket(500, true), bucket(-500, true)].map(bucketTone),
  sentence: [null, undefined, "", "   ", "  Held up.  ", "A. B."].map(sentenceOrDash),
};
process.stdout.write(JSON.stringify(out));

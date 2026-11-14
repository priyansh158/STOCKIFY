// Runs web/src/lib/runView.ts and chart.ts's whyOf on hand cases and prints one JSON object. Node >= 22.18 runs this
// directly (type stripping); called by tests/stockify/test_api_run_page.py.
// chart.ts imports "./format" without an extension (the bundler's way), so a resolve hook adds ".ts" here.
import { registerHooks } from "node:module";
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith("./") && !/\.\w+$/.test(specifier)) return nextResolve(specifier + ".ts", context);
    return nextResolve(specifier, context);
  },
});
const root = resolve(import.meta.dirname, "..", "..", "web", "src", "lib");
const v = await import(pathToFileURL(resolve(root, "runView.ts")).href);
const chart = await import(pathToFileURL(resolve(root, "chart.ts")).href);

const fill = (o: object) => ({ fill_id: 1, order_id: 1, t_ns: 1, t_ist: "2026-10-05T19:00:00+05:30", instrument_id: 1, side: "SELL", qty: 65,
  price_paise: 100, note: "", tag: "", evidence: "", at_close: false, ...o });
const out: Record<string, unknown> = {};
out.close = v.fillWhen(fill({ at_close: true }));
// the same stamp on a fill that is not a close model reads as a time, whatever the clock says
out.intraday = v.fillWhen(fill({ at_close: false }));
out.closeOther = v.fillWhen(fill({ at_close: true, t_ist: "2026-10-12T20:30:00+05:30" }));
out.pruned = [chart.prunedWords("no_pack", true, true), chart.prunedWords("no_pack", true, false), chart.prunedWords("pruned", true, true)];
out.hints = [v.chartHint(false, null), v.chartHint(true, null), v.chartHint(true, "f1"), v.chartHint(false, "f1")];
out.whyNote = chart.whyOf(fill({ note: "Range broke up", tag: "orb" }));
out.whyNone = chart.whyOf(fill({ tag: "entry" }));
out.whyBare = chart.whyOf(fill({ side: "BUY", qty: 3 }));
process.stdout.write(JSON.stringify(out));

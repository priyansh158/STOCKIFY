// Runs web/src/lib/chart.ts on hand cases and prints one JSON object of the answers, keyed by case name.
// Node >= 22.18 runs this file directly (type stripping); called by tests/stockify/test_chart_checks.py.
// chart.ts imports "./format" without an extension (the bundler's way), so a resolve hook adds ".ts" here.
import { registerHooks } from "node:module";
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith("./") && !/\.\w+$/.test(specifier)) {
      return nextResolve(specifier + ".ts", context);
    }
    return nextResolve(specifier, context);
  },
});

const root = resolve(import.meta.dirname, "..", "..", "web", "src", "lib");
const chart = await import(pathToFileURL(resolve(root, "chart.ts")).href);

const TOKENS = { up: "var-up", down: "var-down", warn: "var-warn" };
const bar = (t: number, d: string, close = "2418.40") => ({
  t, d, open: "2400.00", high: "2420.50", low: "2399.95", close, volume: 1200,
});
const fill = (over: Record<string, unknown>) => ({
  kind: "fill", t: 1_762_913_100, bar_t: 1_762_913_100, side: "BUY", qty: 50, price: "2418.40", price_paise: 241840,
  order_id: 7, fill_id: 11, note: "", tag: "", tag_is_code: false, charges_paise: null, trade_pnl_paise: null,
  chunks: [], ...over,
});

const out: Record<string, unknown> = {};
out.istShift = chart.istShift;
out.candleIntraday = chart.toCandle(bar(1_762_913_100, "2025-11-12"), false);
out.candleDaily = chart.toCandle(bar(1_762_913_100, "2025-11-12"), true);
out.volumeIntraday = chart.toVolume(bar(1_762_913_100, "2025-11-12"), false);
out.volumeDaily = chart.toVolume(bar(1_762_913_100, "2025-11-12"), true);
out.markerBuy = chart.toMarker(fill({}), false, TOKENS);
out.markerSell = chart.toMarker(fill({ side: "SELL", qty: 50, fill_id: 12 }), false, TOKENS);
out.markerDaily = chart.toMarker(fill({}), true, TOKENS);
out.markerSettlement = chart.toMarker(
  fill({ kind: "settlement", side: "SELL", fill_id: null, order_id: null, note: "Settled at expiry at ₹112.00" }),
  false, TOKENS);
out.markerWithNote = chart.toMarker(fill({ note: "Range broke up <b>now</b>" }), false, TOKENS);

const NOTE = "<img src=x onerror=alert(1)>";
out.cardNote = chart.cardModel(fill({ note: "Opening range broke up", charges_paise: 2145, trade_pnl_paise: 125000 }));
out.cardNoNote = chart.cardModel(fill({ side: "SELL", note: "", tag: "orb_exit", tag_is_code: true }));
out.cardOpening = chart.cardModel(fill({ note: "Open long", trade_pnl_paise: null }));
out.cardLoss = chart.cardModel(fill({ side: "SELL", note: "Stop hit", trade_pnl_paise: -53050 }));
out.cardFlat = chart.cardModel(fill({ side: "SELL", note: "Flat exit", trade_pnl_paise: 0 }));
out.cardHtml = chart.cardModel(fill({ note: NOTE, tag: "x", charges_paise: 100, trade_pnl_paise: -100 }));
out.markerHtml = chart.toMarker(fill({ note: NOTE }), false, TOKENS);

out.merged = chart.mergePages(
  [bar(300, "d"), bar(100, "d"), bar(200, "d")],
  [bar(300, "d", "1.00"), bar(400, "d"), bar(50, "d")],
).map((b: { t: number; close: string }) => [b.t, b.close]);
out.mergedEmpty = chart.mergePages([], [bar(5, "d")]).length;
out.rangeAround = chart.rangeAround(1_762_913_100_000_000_000, 60, 100);
out.rangeNearZero = chart.rangeAround(1_000_000_000, 60, 100);

process.stdout.write(JSON.stringify(out));

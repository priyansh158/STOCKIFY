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
out.rangeAround = chart.rangeAround(1_762_913_100, 60, 100);
out.rangeNearZero = chart.rangeAround(1_000, 60, 100);

// Two settlements at the same instant and quantity (two legs of one expiry) must not share an id.
const sx = fill({ kind: "settlement", side: "SELL", fill_id: null, order_id: null, qty: 75 });
out.settlementIds = [chart.toMarker(sx, false, TOKENS, 0).id, chart.toMarker(sx, false, TOKENS, 1).id];

// Seeded generated properties (a fixed-seed PRNG, no package). Each list holds the violations found; empty is a pass.
function rng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const rand = rng(20261112);
const int = (lo: number, hi: number) => lo + Math.floor(rand() * (hi - lo + 1));
const CHARS = ["a", "Z", "7", " ", "<", ">", "&", '"', "'", "/", "B", "S", "X", "₹", "\n"];
const text = () => Array.from({ length: int(0, 12) }, () => CHARS[int(0, CHARS.length - 1)]).join("");
const ts = (n: number) => {
  const set = new Set<number>();
  while (set.size < n) set.add(int(0, 40) * 60);
  return [...set];
};

const merge: string[] = [];
for (let i = 0; i < 200; i++) {
  const have = ts(int(0, 12)).map((t) => bar(t, "d", "have"));
  const page = ts(int(0, 12)).map((t) => bar(t, "d", "page"));
  const m = chart.mergePages(have, page) as { t: number; close: string }[];
  const heldT = new Set(have.map((b) => b.t));
  const want = new Set([...have, ...page].map((b) => b.t));
  const key = JSON.stringify(m);
  if (new Set(m.map((b) => b.t)).size !== m.length) merge.push(`${i}: duplicate t`);
  if (m.some((b, k) => k > 0 && m[k - 1].t >= b.t)) merge.push(`${i}: not sorted`);
  if (m.length !== want.size) merge.push(`${i}: not the union`);
  if (m.some((b) => heldT.has(b.t) && b.close !== "have")) merge.push(`${i}: the page replaced a held bar`);
  if (JSON.stringify(chart.mergePages(m, page)) !== key) merge.push(`${i}: not idempotent with the page`);
  if (JSON.stringify(chart.mergePages(m, [])) !== key) merge.push(`${i}: not idempotent with nothing`);
}
out.propMerge = merge;

const tone: string[] = [];
for (let i = 0; i < 300; i++) {
  const p = rand() < 0.2 ? 0 : int(-2_000_000, 2_000_000);
  const c = chart.cardModel(fill({ note: text(), side: rand() < 0.5 ? "BUY" : "SELL", trade_pnl_paise: p }));
  const want = p > 0 ? "up" : p < 0 ? "down" : "flat";
  if (!c.pnl || c.pnl.tone !== want) tone.push(`${p}: ${JSON.stringify(c.pnl)}`);
  if (chart.cardModel(fill({ trade_pnl_paise: null })).pnl !== null) tone.push("null pnl is not null");
}
out.propTone = tone;

const noteText: string[] = [];
for (let i = 0; i < 300; i++) {
  const note = text();
  const kind = rand() < 0.3 ? "settlement" : "fill";
  const side = rand() < 0.5 ? "BUY" : "SELL";
  const m = chart.toMarker(fill({ note, kind, qty: int(1, 100000), side }), rand() < 0.5, TOKENS, i);
  if (!/^[BSX] \d+$/.test(m.text)) noteText.push(`${JSON.stringify(note)} -> ${m.text}`);
  if ((kind === "settlement") !== (m.shape === "square")) noteText.push(`${kind} has shape ${m.shape}`);
}
out.propMarkerText = noteText;

process.stdout.write(JSON.stringify(out));

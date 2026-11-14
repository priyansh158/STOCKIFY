// Runs web/src/lib/names.ts on hand cases and prints one JSON object. Node >= 22.18 runs this directly (type stripping);
// called by tests/stockify/test_web_names.py.
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";

const n = await import(pathToFileURL(resolve(import.meta.dirname, "..", "..", "web", "src", "lib", "names.ts")).href);
const out: Record<string, unknown> = {};
out.notes = ["Opening Range Breakout · RELIANCE", "plain name_1.v2-x", "  ", "é", "a".repeat(70), "é".repeat(70)].map((x) => n.slugNote(x));
out.roundTrip = n.runNameSlug("a - b");
out.wordsOf = ["refdata_stale", "R_MARGIN", "margin:flat", "x"].map((x) => n.wordsOf(x));
out.status = ["done", "running", "weird_one"].map((x) => n.statusWords(x));
out.impl = ["engine", "oracle", null, "other_impl"].map((x) => n.implWords(x));
out.month = [n.monthYear("2026-06-01"), n.monthYear("2026-13-01"), n.monthYear("nope")];
out.suggested = [n.suggestedName("Opening Range Breakout", "NSE:RELIANCE, NSE:INFY", "2026-06-01"), n.suggestedName("Buy and Hold", "", ""), n.suggestedName("X", "NSE:NIFTY 50", "2024-01-01")];
out.slug = [n.runNameSlug(n.suggestedName("Opening Range Breakout", "NSE:RELIANCE", "2026-06-01")), n.runNameSlug("  ··  "), n.runNameSlug("a".repeat(80) + "_x"), n.runNameSlug("../evil"), n.runNameSlug("CON")];
process.stdout.write(JSON.stringify(out));

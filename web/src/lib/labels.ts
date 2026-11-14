/**
 * What each run label says, in plain words (labels from bundle.py and the engine, A4 I-14), and the margin-step names.
 * Pure (checked from Python by tests/stockify/labels_check.ts). The chips, the compare table and the run page all read these.
 */
import { upperFirst, wordsOf } from "./names";

/** The margin staircase steps (A1 6.3 margin_daily.csv `step`), in plain words. An unknown code is shown as-is. */
const MARGIN_STEPS: Record<string, { text: string; detail: string }> = {
  B1_span_nse_elm: { text: "exact NSE margin",
    detail: "NSE's own SPAN risk file and its extreme-loss margin file were both used, so this day's margin is the exchange's figure." },
  B2_span_rule_elm: { text: "NSE margin, exposure from rules",
    detail: "NSE's SPAN risk file was used, but the extreme-loss (exposure) part came from the published rules, not NSE's file for that day." },
  B3_spanlite_calibrated: { text: "estimate (calibrated)",
    detail: "No NSE risk file for the day, so a SPAN-like estimate was used, with settings calibrated against Kite's margin figures." },
  B4_spanlite_placeholder: { text: "estimate (not calibrated)",
    detail: "No NSE risk file for the day, so a SPAN-like estimate was used with placeholder settings; the amount is rough." },
  B5_flat: { text: "simple estimate",
    detail: "A fixed percentage of position value, not a SPAN calculation." },
};

/** The step's plain-words name (and a longer sentence for a tooltip); an unrecognised code comes back as words. */
export function marginStep(step: string | null | undefined): { text: string; detail: string } | null {
  if (!step) return null;
  return MARGIN_STEPS[step] ?? { text: wordsOf(step), detail: wordsOf(step) };
}

/** Plain words for the margin-staircase labels, or null when the label is not one of them. */
function marginLabel(label: string): { text: string; detail: string; warn: boolean } | null {
  if (label === "margin_span:no_section") {
    return { text: "no NSE margin data in this test", warn: true,
      detail: "The test's data had no exchange margin section at all, so every day used an estimate." };
  }
  if (label === "margin_span:skipped_files") {
    return { text: "some NSE margin files skipped", warn: true,
      detail: "At least one NSE margin file could not be used and was skipped; those days fell back to a lower step." };
  }
  if (label === "margin_elm:skipped_files") {
    return { text: "an NSE ELM file skipped", warn: true,
      detail: "At least one day's NSE exposure-margin file could not be read and was skipped; those days used the dated exposure rule (step B2) instead." };
  }
  if (label === "margin_span:no_derivatives") {
    return { text: "no futures or options", warn: false,
      detail: "The test traded no futures or options, so no NSE margin file was needed or read." };
  }
  if (label === "margin:staircase") {
    return { text: "margin staircase", warn: false,
      detail: "Margin was priced by the most exact method available each day: NSE's own SPAN file first, stepping down to an estimate only where the file could not price a position. The margin-step labels say which methods were used." };
  }
  if (label === "margin:span_exact") {
    return { text: "exact NSE SPAN margin", warn: false,
      detail: "Margin came only from NSE's SPAN file; the test would have stopped had any position been missing from it." };
  }
  if (label.startsWith("margin_steps:")) {
    const m = /^margin_steps:([^=]+)=(\d+)$/.exec(label);
    if (!m) return { text: label, detail: label, warn: false };
    const s = marginStep(m[1])!;
    const n = Number(m[2]);
    return { text: `${s.text}: ${n} ${n === 1 ? "day" : "days"}`, warn: m[1] !== "B1_span_nse_elm" && m[1] !== "B2_span_rule_elm",
             detail: `${n} ${n === 1 ? "run day was" : "run days were"} priced by this margin step. ${s.detail}` };
  }
  if (/^margin:B\d/.test(label)) {
    const step = label.slice("margin:".length);
    const s = marginStep(step)!;
    return { text: `margin: ${s.text}`, detail: `Margin was priced this way on at least one day. ${s.detail}`,
             warn: step !== "B1_span_nse_elm" && step !== "B2_span_rule_elm" };
  }
  return null;
}

/** How a fill model's name reads in a caveat ("fills at the day's close"); any other name is shown as words. */
const FILL_WORDS: Record<string, string> = {
  bar: "fills from price bars",
  eod_close: "fills at the day's close",
  snapshot_quote: "fills from live quotes",
  snapshot_ltp: "fills from the last traded price",
  approximate: "approximate fills",
};

/** What each run label says, in plain sentence-case words (labels from bundle.py / the engine, A4 I-14). */
export function labelText(label: string): { text: string; warn: boolean } {
  const r = labelTextRaw(label);
  return { text: upperFirst(r.text), warn: r.warn };
}

/** Labels of data gaps and of paper sessions (not part of the margin or fill families above). */
function otherLabel(label: string): { text: string; warn: boolean; detail: string } | null {
  const gaps = /^data:gaps=(\d+)$/.exec(label);
  if (gaps) {
    const n = Number(gaps[1]);
    return { text: `${n.toLocaleString("en-IN")} ${n === 1 ? "day" : "days"} with missing data`, warn: true,
             detail: `${n.toLocaleString("en-IN")} ${n === 1 ? "day was" : "days were"} missing from the recorded data, so the test did not see them.` };
  }
  if (label === "refdata_stale") {
    return { text: "Instrument list out of date", warn: true, detail: "The list of instruments was older than it should be when this was set up, so a contract may be missing or renamed." };
  }
  if (label.startsWith("sod_from:")) {
    return { text: "Started from a saved end-of-day state", warn: false, detail: "The day began from the positions and cash saved at the end of an earlier day." };
  }
  if (label === "margin_span:sod_files_only" || label === "margin_elm:sod_files_only") {
    return { text: "Margin from files published before the open", warn: false, detail: "Only the margin files NSE publishes before the market opens were used for this day." };
  }
  if (label.startsWith("session:")) {
    return { text: "Paper session", warn: false, detail: "This day was run by a paper session." };
  }
  return null;
}

/** True when the label has its own wording here; false when it would only get the generic words-from-the-code fallback. */
export function labelKnown(label: string): boolean {
  return labelTextRaw(label).known;
}

function labelTextRaw(label: string): { text: string; warn: boolean; known: boolean } {
  const fixed: Record<string, { text: string; warn: boolean }> = {
    "fill:approximate": { text: FILL_WORDS.approximate, warn: true },
    eod_approx: { text: "fills at the day's close, approximate", warn: true },
    "margin:flat(approx)": { text: "flat margin, approximate", warn: true },
    "charges:unverified_rows": { text: "unverified charges", warn: true },
    "costs:scaled": { text: "costs scaled", warn: true },
    "specs:equity_tick_assumed": { text: "tick size assumed", warn: true },
    settlement_fallback: { text: "settlement price estimated", warn: true },
    "margin:spanlite(approx)": { text: "SPAN-like margin, approximate", warn: true },
    "margin_spanlite:iv_fallback": { text: "margin used an assumed volatility", warn: true },
    delivery_margin_approx: { text: "delivery margin approximate", warn: true },
    short_delivery: { text: "short stock delivered", warn: true },
  };
  if (fixed[label]) return { ...fixed[label], known: true };
  const ml = marginLabel(label);
  if (ml) return { text: ml.text, warn: ml.warn, known: true };
  if (label.startsWith("margin_spanlite:")) {
    const calib = label.slice("margin_spanlite:".length);
    if (calib === "none") return { text: "no SPAN-like settings needed", warn: false, known: true };
    return { text: calib.includes("placeholder") ? "margin settings not yet calibrated" : `margin settings: ${wordsOf(calib).toLowerCase()}`,
             warn: calib.includes("placeholder"), known: true };
  }
  if (label.startsWith("fill:")) {
    const model = label.slice(5);
    return { text: FILL_WORDS[model] ?? `fills: ${wordsOf(model).toLowerCase()}`, warn: false, known: model in FILL_WORDS };
  }
  const other = otherLabel(label);
  if (other) return { text: other.text, warn: other.warn, known: true };
  return { text: wordsOf(label), warn: false, known: false };
}

const FILL_MEANING: Record<string, string> = {
  bar: "at prices taken from the price bars",
  eod_close: "at the day's closing price from NSE's end-of-day files",
  snapshot_quote: "from the recorded quotes (best bid and offer)",
  snapshot_ltp: "at the last traded price in the recorded ticks",
};

/** One plain sentence per run label, for the detail page (what the label says about the results). */
export function labelMeaning(label: string): string {
  const fixed: Record<string, string> = {
    "fill:approximate": "Fills are estimated from the last traded price only, so they may be better than a real order would get.",
    eod_approx: "Orders fill at the next day's closing price from NSE's end-of-day files, so intraday moves are not seen.",
    "margin:flat(approx)": "Margin is a fixed percentage of each position's value, not the exchange's SPAN calculation.",
    "margin:spanlite(approx)": "Margin is a SPAN-like estimate (price and volatility scenarios plus extreme-loss margin), not the exchange's own file.",
    "margin_spanlite:iv_fallback": "For at least one option, the margin used an assumed volatility because no implied volatility could be read from the quotes.",
    "charges:unverified_rows": "Some of the charge rates used have not been checked against the broker's latest published rates.",
    "costs:scaled": "Every brokerage, tax and fee was multiplied by a set factor for a what-if test, so these results do not show what the real charges would cost.",
    "specs:equity_tick_assumed": "A stock's tick size was not recorded for these dates, so a 1 paisa tick was assumed.",
    settlement_fallback: "NSE had not published a final settlement price for an expiry, so the price was estimated from the last market data.",
    delivery_margin_approx: "The extra margin brokers block before stock F&O delivery was applied approximately.",
    short_delivery: "A short stock F&O position ended in delivering shares that were not held; a real account would face an auction penalty.",
  };
  if (fixed[label]) return fixed[label];
  const ml = marginLabel(label);
  if (ml) return ml.detail;
  if (label === "margin_spanlite:none") {
    return "The run had no futures or options, so the SPAN-like margin's settings were never needed.";
  }
  if (label.startsWith("margin_spanlite:")) {
    return label.includes("placeholder")
      ? "The SPAN-like margin's risk settings are placeholders until they are calibrated against Kite, so margin amounts are rough."
      : `The SPAN-like margin used the "${wordsOf(label.slice("margin_spanlite:".length)).toLowerCase()}" calibration.`;
  }
  if (label.startsWith("fill:")) {
    const model = label.slice(5);
    return `Orders were filled ${FILL_MEANING[model] ?? `by the ${wordsOf(model).toLowerCase()} method`}.`;
  }
  return otherLabel(label)?.detail ?? `${wordsOf(label)}.`;
}

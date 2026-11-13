/** Small presentational pieces shared by /tests and /tests/[id]. */
import { Chip } from "@/components/ui";
import { strategyName } from "@/lib/runView";
import type { TestRun, TestStatus } from "@/lib/types";

const STATUS: Record<TestStatus, { text: string; tone?: "up" | "down" | "warn" | "info" }> = {
  queued: { text: "Waiting" },
  running: { text: "Running", tone: "info" },
  done: { text: "Finished", tone: "up" },
  failed: { text: "Failed", tone: "down" },
  cancelled: { text: "Cancelled", tone: "warn" },
};

export function StatusChip({ status }: { status: TestStatus }) {
  const s = STATUS[status];
  return <Chip tone={s.tone}>{s.text}</Chip>;
}

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

/** The step's plain-words name (and a longer sentence for a tooltip); an unrecognised code comes back unchanged. */
export function marginStep(step: string | null | undefined): { text: string; detail: string } | null {
  if (!step) return null;
  return MARGIN_STEPS[step] ?? { text: step, detail: step };
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

/** What each run label means, in plain words (labels from bundle.py / the engine, A4 I-14). */
export function labelText(label: string): { text: string; warn: boolean } {
  const fixed: Record<string, { text: string; warn: boolean }> = {
    "fill:approximate": { text: "approximate fills", warn: true },
    eod_approx: { text: "end-of-day fills, approximate", warn: true },
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
  if (fixed[label]) return fixed[label];
  const ml = marginLabel(label);
  if (ml) return { text: ml.text, warn: ml.warn };
  if (label.startsWith("margin_spanlite:")) {
    const calib = label.slice("margin_spanlite:".length);
    if (calib === "none") return { text: "no SPAN-like settings needed", warn: false };
    return { text: calib.includes("placeholder") ? "margin settings not yet calibrated" : `margin settings ${calib}`,
             warn: calib.includes("placeholder") };
  }
  if (label.startsWith("fill:")) return { text: `fills from ${label.slice(5).replace("_", " ")}`, warn: false };
  return { text: label, warn: false };
}

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
      : `The SPAN-like margin used calibration ${label.slice("margin_spanlite:".length)}.`;
  }
  if (label.startsWith("fill:")) return `Orders filled by the ${label.slice(5).replace(/_/g, " ")} model.`;
  return label;
}

export function LabelChips({ run }: { run: Pick<TestRun, "labels" | "price_basis"> }) {
  const items = run.labels.map(labelText);
  if (run.price_basis === "adjusted") items.push({ text: "adjusted prices", warn: true });
  if (items.length === 0) return null;
  return (
    <div className="stk-chips">
      {items.map((l) => (
        <Chip key={l.text} tone={l.warn ? "warn" : undefined}>
          {l.text}
        </Chip>
      ))}
    </div>
  );
}

/** "sma_cross · 2024-01-01 to 2026-09-30", leaving out whatever the run does not say. */
export function runSummary(run: Pick<TestRun, "strategy_ref" | "start_date" | "end_date">, from?: string | null,
                           to?: string | null): string {
  const a = run.start_date ?? from;
  const b = run.end_date ?? to;
  const parts = [run.strategy_ref ? strategyName(run.strategy_ref) : null, a && b ? `${a} to ${b}` : null];
  return parts.filter(Boolean).join(", ");
}

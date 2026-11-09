/** Small presentational pieces shared by /tests and /tests/[id]. */
import type { ReactNode } from "react";
import type { TestRun, TestStatus } from "@/lib/types";

const STATUS: Record<TestStatus, { text: string; fg: string; border: string; bg: string }> = {
  queued: { text: "Waiting", fg: "var(--color-fg-muted)", border: "rgba(0,255,65,0.25)", bg: "rgba(255,255,255,0.03)" },
  running: { text: "Running", fg: "var(--color-blue)", border: "var(--color-blue)", bg: "rgba(79,176,255,0.08)" },
  done: { text: "Finished", fg: "var(--color-green)", border: "var(--color-green)", bg: "rgba(0,255,65,0.06)" },
  failed: { text: "Failed", fg: "var(--color-red-soft)", border: "var(--color-red)", bg: "rgba(255,68,68,0.10)" },
  cancelled: { text: "Cancelled", fg: "var(--color-amber)", border: "var(--color-amber)", bg: "rgba(255,170,64,0.08)" },
};

export function StatusChip({ status }: { status: TestStatus }) {
  const s = STATUS[status];
  return (
    <span
      className="stk-mono-tight inline-flex items-center rounded px-2 py-0.5 text-xs font-bold"
      style={{ color: s.fg, border: `1px solid ${s.border}`, background: s.bg }}
    >
      {s.text}
    </span>
  );
}

/** What each run label means, in plain words (labels from bundle.py / the engine, A4 I-14). */
function labelText(label: string): { text: string; warn: boolean } {
  const fixed: Record<string, { text: string; warn: boolean }> = {
    "fill:approximate": { text: "approximate fills", warn: true },
    eod_approx: { text: "end-of-day fills, approximate", warn: true },
    "margin:flat(approx)": { text: "flat margin, approximate", warn: true },
    "charges:unverified_rows": { text: "unverified charges", warn: true },
    "specs:equity_tick_assumed": { text: "tick size assumed", warn: true },
    settlement_fallback: { text: "settlement price estimated", warn: true },
    "margin:spanlite(approx)": { text: "SPAN-like margin, approximate", warn: true },
    "margin_spanlite:iv_fallback": { text: "margin used an assumed volatility", warn: true },
    delivery_margin_approx: { text: "delivery margin approximate", warn: true },
    short_delivery: { text: "short stock delivered", warn: true },
  };
  if (fixed[label]) return fixed[label];
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
    "specs:equity_tick_assumed": "A stock's tick size was not recorded for these dates, so a 1 paisa tick was assumed.",
    settlement_fallback: "NSE had not published a final settlement price for an expiry, so the price was estimated from the last market data.",
    delivery_margin_approx: "The extra margin brokers block before stock F&O delivery was applied approximately.",
    short_delivery: "A short stock F&O position ended in delivering shares that were not held; a real account would face an auction penalty.",
  };
  if (fixed[label]) return fixed[label];
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
    <div className="flex flex-wrap gap-1.5">
      {items.map((l) => (
        <span
          key={l.text}
          className="rounded px-2 py-0.5 text-xs"
          style={{
            color: l.warn ? "var(--color-amber)" : "var(--color-fg-muted)",
            border: `1px solid ${l.warn ? "rgba(255,170,64,0.45)" : "rgba(0,255,65,0.20)"}`,
          }}
        >
          {l.text}
        </span>
      ))}
    </div>
  );
}

export function Card({ children, tone }: { children: ReactNode; tone?: "red" }) {
  return (
    <section
      className="rounded-lg border px-4 py-3"
      style={{
        borderColor: tone === "red" ? "var(--color-red)" : "rgba(0,255,65,0.20)",
        background: tone === "red" ? "rgba(255,68,68,0.06)" : "rgba(0,255,65,0.03)",
      }}
    >
      {children}
    </section>
  );
}

export function Stat({ label, value, tone }: { label: string; value: string; tone?: "up" | "down" }) {
  const color = tone === "up" ? "var(--color-green)" : tone === "down" ? "var(--color-red-soft)" : "var(--color-fg)";
  return (
    <div>
      <div className="text-xs" style={{ color: "var(--color-fg-muted)" }}>{label}</div>
      <div className="stk-mono text-base font-semibold" style={{ color }}>{value}</div>
    </div>
  );
}

export const buttonStyle = {
  borderColor: "rgba(0,255,65,0.35)",
  color: "var(--color-green-soft)",
  background: "rgba(0,255,65,0.06)",
} as const;

/** "sma_cross · 2024-01-01 to 2026-09-30", leaving out whatever the run does not say. */
export function runSummary(run: Pick<TestRun, "strategy_ref" | "start_date" | "end_date">, from?: string | null,
                           to?: string | null): string {
  const a = run.start_date ?? from;
  const b = run.end_date ?? to;
  const parts = [run.strategy_ref ? strategyName(run.strategy_ref) : null, a && b ? `${a} to ${b}` : null];
  return parts.filter(Boolean).join(" · ");
}

/** The strategy's short name from its ref ("pkg.sma_cross:SmaCross" → "sma_cross"). */
export function strategyName(ref?: string | null): string {
  if (!ref) return "—";
  const mod = ref.split(":")[0];
  return mod.includes(".") ? mod.split(".").pop() ?? ref : ref;
}

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
  };
  if (fixed[label]) return fixed[label];
  if (label.startsWith("fill:")) return { text: `fills from ${label.slice(5).replace("_", " ")}`, warn: false };
  return { text: label, warn: false };
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

/** The strategy's short name from its ref ("pkg.sma_cross:SmaCross" → "sma_cross"). */
export function strategyName(ref?: string | null): string {
  if (!ref) return "—";
  const mod = ref.split(":")[0];
  return mod.includes(".") ? mod.split(".").pop() ?? ref : ref;
}

/** Small presentational pieces for the /trade pages. Colours come from the tokens in globals.css (classes, never literals). */
import type { ReactNode } from "react";

export function Panel({ children, tone }: { children: ReactNode; tone?: "red" | "amber" }) {
  return (
    <section className="stk-panel" data-tone={tone}>
      {children}
    </section>
  );
}

export function Stat({ label, value, tone }: { label: string; value: string; tone?: "up" | "down" }) {
  const color = tone === "up" ? "text-green" : tone === "down" ? "text-red-soft" : "text-fg";
  return (
    <div>
      <div className="stk-label">{label}</div>
      <div className={`stk-mono text-base font-semibold tabular-nums ${color}`}>{value}</div>
    </div>
  );
}

/** A grey block in the shape of what is loading. */
export function Skeleton({ className = "h-24" }: { className?: string }) {
  return <div className={`stk-skeleton ${className}`} aria-hidden />;
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <Panel>
      <p className="py-4 text-center text-sm text-fg-muted">{children}</p>
    </Panel>
  );
}

/** A scrollable table frame: wide on a desktop, scrolls sideways rather than squeezing in a narrow window. */
export function Table({ caption, head, children }: { caption: string; head: { label: string; num?: boolean }[]; children: ReactNode }) {
  return (
    <div className="stk-table-wrap">
      <table className="stk-table">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr>
            {head.map((h) => (
              <th key={h.label} scope="col" className={h.num ? "num" : undefined}>
                {h.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

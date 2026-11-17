/** Shared building blocks of the app's one system (see web/design.md). Colours and sizes live in globals.css as tokens. */
import type { ReactNode } from "react";

export type Tone = "up" | "down";

/** A bordered surface. `tone` is for a notice (red: something failed, amber: needs you, info: for your information). */
export function Panel({ children, tone }: { children: ReactNode; tone?: "red" | "amber" | "info" }) {
  return (
    <section className="stk-panel" data-tone={tone}>
      {children}
    </section>
  );
}

/** The page title block: the h1, one sentence under it and the page's actions on the right. */
export function PageHead({ title, lede, actions }: { title: ReactNode; lede?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="stk-head">
      <div>
        <h1>{title}</h1>
        {lede && <p className="stk-lede">{lede}</p>}
      </div>
      {actions && <div className="stk-head-actions">{actions}</div>}
    </div>
  );
}

/** A figure with its label. Put several inside a StatGrid. `trend` is a small history drawn after the figure (a `Spark`). */
export function Stat({ label, value, tone, size, plain, trend }: { label: string; value: string; tone?: Tone; size?: "s"; plain?: boolean; trend?: ReactNode }) {
  // a value with no digit in it ("too short to say", "Not run yet") is words: the body face, not the figure face
  const words = plain || !/\d/.test(value);
  return (
    <div className="stk-stat" data-size={size} data-plain={words ? "" : undefined}>
      <dt>{label}</dt>
      <dd className={tone === "up" ? "stk-up" : tone === "down" ? "stk-down" : undefined}>
        {trend ? <span>{value}</span> : value}
        {trend}
      </dd>
    </div>
  );
}

/** A small history line beside a money figure, drawn only from a real series (never placeholder data). The dashed level and the
 * dot mark where it started; the line ends at the figure it sits beside. `label` says the same in words for screen readers. */
export function Spark({ values, start, label }: { values: number[]; start?: number | null; label: string }) {
  if (values.length < 2) return null;
  const W = 96;
  const H = 28;
  const from = start ?? values[0];
  const lo = Math.min(...values, from);
  const hi = Math.max(...values, from);
  const span = hi - lo || 1;
  const x = (i: number) => (i / (values.length - 1)) * (W - 4) + 2;
  const y = (v: number) => H - 3 - ((v - lo) / span) * (H - 6);
  const line = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  return (
    <svg className="stk-spark" viewBox={`0 0 ${W} ${H}`} width={W} height={H} role="img" aria-label={label}>
      <line x1={0} x2={W} y1={y(from)} y2={y(from)} className="stk-spark-start" />
      <path d={line} className="stk-spark-line" />
      <circle cx={x(0)} cy={y(values[0])} r={2.5} className="stk-spark-dot" />
    </svg>
  );
}

export function StatGrid({ children, cols }: { children: ReactNode; cols?: 2 }) {
  return (
    <dl className="stk-stat-grid" data-cols={cols}>
      {children}
    </dl>
  );
}

/** The loading placeholder for a region: announced as one status, the blocks themselves are hidden from screen readers. */
export function Loading({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="stk-stack-s" role="status" aria-busy="true" aria-label={label}>
      {children}
    </div>
  );
}

/** A grey block in the shape of what is loading. `size` is one of s, m, l or title. */
export function Skeleton({ size = "m" }: { size?: "s" | "m" | "l" | "title" }) {
  return <div className={`stk-skeleton stk-h-${size}`} aria-hidden />;
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <p className="stk-empty">{children}</p>
  );
}

/** A table frame: wide on a desktop, scrolls sideways in a narrow window rather than squeezing. `tall` is for a long ledger:
 * the frame gets its own height and vertical scroll, and the column heads stay in view while the rows move under them. */
export function Table({
  caption,
  head,
  tall,
  children,
}: {
  caption: string;
  head: { label: string; sub?: string; num?: boolean }[];
  tall?: boolean;
  children: ReactNode;
}) {
  return (
    <div className="stk-table-wrap" data-tall={tall ? "" : undefined} tabIndex={0} role="region" aria-label={caption}>
      <table className="stk-table">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr>
            {head.map((h, i) => (
              <th key={`${i}-${h.label}`} scope="col" className={h.num ? "num" : undefined}>
                {h.label}
                {h.sub && <div className="stk-label">{h.sub}</div>}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

/** A labelled form control. The control goes inside as the child. */
export function Field({ label, help, children }: { label: string; help?: string; children: ReactNode }) {
  return (
    <label className="stk-fieldwrap">
      <span>{label}</span>
      {children}
      {help && <span className="stk-label">{help}</span>}
    </label>
  );
}

export function Chip({ children, tone }: { children: ReactNode; tone?: "up" | "down" | "warn" | "info" }) {
  return (
    <span className="stk-chip" data-tone={tone}>
      {children}
    </span>
  );
}

/** The class for a gain (green), a loss (red) or neither. */
export function toneClass(t: Tone | undefined): string | undefined {
  return t === "up" ? "stk-up" : t === "down" ? "stk-down" : undefined;
}

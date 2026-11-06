import { rupeesWhole } from "@/lib/format";
import type { TestEquity } from "@/lib/types";

/** End-of-day money and the fall from its running high, as two stacked SVG panels that scale to any width. */
export default function EquityChart({ rows, start }: { rows: TestEquity[]; start?: number | null }) {
  if (rows.length < 2) {
    return <p className="text-sm" style={{ color: "var(--color-fg-muted)" }}>Not enough days to draw.</p>;
  }
  const W = 600;
  const H = 180;
  const DH = 70;
  const navs = rows.map((r) => r.nav_paise);
  const lo = Math.min(...navs, start ?? Infinity);
  const hi = Math.max(...navs, start ?? -Infinity);
  const span = hi - lo || 1;
  const x = (i: number) => (i / (rows.length - 1)) * W;
  const y = (v: number) => H - ((v - lo) / span) * (H - 8) - 4;
  const line = navs.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");

  const peaks = navs.reduce<number[]>((acc, v, i) => [...acc, i ? Math.max(acc[i - 1], v) : v], []);
  const dd = navs.map((v, i) => (peaks[i] > 0 ? v / peaks[i] - 1 : 0));
  const worst = Math.min(...dd, -0.0001);
  const dy = (f: number) => (f / worst) * (DH - 4);
  const area = `M0,0 ${dd.map((f, i) => `L${x(i).toFixed(1)},${dy(f).toFixed(1)}`).join(" ")} L${W},0 Z`;

  return (
    <figure className="flex flex-col gap-2">
      <svg viewBox={`0 0 ${W} ${H}`} className="h-44 w-full" preserveAspectRatio="none" role="img"
        aria-label={`Money from ${rupeesWhole(navs[0])} to ${rupeesWhole(navs[navs.length - 1])}`}>
        {start ? (
          <line x1={0} x2={W} y1={y(start)} y2={y(start)} stroke="rgba(224,255,224,0.25)" strokeDasharray="4 4"
            vectorEffect="non-scaling-stroke" />
        ) : null}
        <path d={line} fill="none" stroke="var(--color-green)" strokeWidth={2} vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="flex justify-between text-xs" style={{ color: "var(--color-fg-muted)" }}>
        <span>{rows[0].trade_date}</span>
        <span>{rows[rows.length - 1].trade_date}</span>
      </div>
      <div className="text-xs" style={{ color: "var(--color-fg-muted)" }}>
        lowest {rupeesWhole(lo)} · highest {rupeesWhole(hi)}
      </div>
      <svg viewBox={`0 0 ${W} ${DH}`} className="h-16 w-full" preserveAspectRatio="none" role="img"
        aria-label={`Worst fall from a high: ${(worst * -100).toFixed(1)} %`}>
        <path d={area} fill="rgba(255,68,68,0.35)" stroke="var(--color-red-soft)" strokeWidth={1}
          vectorEffect="non-scaling-stroke" />
      </svg>
      <figcaption className="text-xs" style={{ color: "var(--color-fg-muted)" }}>
        Top: money at each day&apos;s close (dashed: what you started with). Bottom: how far it was below its
        best so far; deepest {(worst * -100).toFixed(1)} %.
      </figcaption>
    </figure>
  );
}

"use client";

/**
 * Research charts, hand-written SVG and tables, coloured only through the tokens in globals.css (.stk-lc, .stk-iv,
 * .stk-heat). Each carries an aria-label that summarises it and a visible table of the same numbers. Desktop sizes:
 * the SVGs keep their aspect ratio and scale with the column, so the text in them is never stretched.
 */
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  DIVERGING, SERIES_TOKENS, divergingStep, heatColourStep, heatMoreWord, markerIndexes, splitSegments,
} from "@/lib/researchHelpers";

const MAX_SERIES = 6;
const HEAT_STEPS = 5;
/** globals.css sets `color-scheme: dark` and shades heat cells from faint to strong accent (checked in research_check.ts). */
const HEAT_SCHEME = "dark" as const;
const HEAT_MORE = heatMoreWord(HEAT_SCHEME);
const COLOURS = SERIES_TOKENS.length;

type Fmt = (n: number) => string;

const upperFirst = (w: string) => w[0].toUpperCase() + w.slice(1);

/** Round tick values spanning [lo, hi]: about `count` of them on a 1, 2 or 5 step. */
function niceTicks(lo: number, hi: number, count = 5): number[] {
  if (!(hi > lo)) return [lo];
  const raw = (hi - lo) / Math.max(1, count - 1);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = ([1, 2, 5, 10].find((m) => m * mag >= raw) ?? 10) * mag;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(Number(v.toPrecision(12)));
  return out;
}

export interface LineSeries {
  label: string;
  points: [number, number][];
  /** x values where a new segment starts: no line is drawn from the point before to that one (a gap in time stays a gap). */
  breaks?: number[];
}

/** The chart is drawn in the pixels it is shown in (the viewBox follows the real width), so its text keeps its token size. */
const W0 = 720;
const H = 280;
const PAD0 = { l: 76, r: 16, t: 12, b: 28 };
/** Room to the right of the plot for the name at the end of each line, when there is more than one line. */

/** Up to six series over a shared x. Series are told apart by colour, dash and marker shape, in a fixed order (the fourth to sixth reuse the three colours), and a legend always names them. */
export function LineChart({
  series,
  yFormat,
  xFormat = (n) => String(n),
  baseline,
  baselineLabel,
  xLabel = "Point",
  label,
  marks,
  marksLabel,
  yLabel,
  describe,
}: {
  series: LineSeries[];
  yFormat: Fmt;
  xFormat?: Fmt;
  /** A reference level drawn as a dashed line, such as the starting capital. */
  baseline?: number;
  /** What the dashed baseline is, such as "starting capital". */
  baselineLabel?: string;
  /** What x is (the first column of the table), such as "Date". */
  xLabel?: string;
  /** What the chart shows, for its accessible name and table caption. */
  label?: string;
  /** x values for dotted vertical boundary lines (such as where each test window starts); `marksLabel` says what they are. */
  marks?: number[];
  marksLabel?: string;
  /** What y measures, shown above the plot (the axis title), such as "growth of 1 (capital = 1.0)". */
  yLabel?: string;
  /** One more sentence for the chart's accessible name, such as that the gaps between windows are not drawn. */
  describe?: string;
}) {
  const drawn = series.filter((s) => s.points.length > 0);
  const shown = drawn.slice(0, MAX_SERIES);
  const hidden = drawn.length - shown.length;
  const [cursor, setCursor] = useState<number | null>(null);
  const [keyboard, setKeyboard] = useState(false);
  const [W, setW] = useState(W0);
  const svgRef = useRef<SVGSVGElement | null>(null);
  useEffect(() => {
    const el = svgRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((entries) => {
      const w = Math.round(entries[0]?.contentRect.width ?? 0);
      if (w >= 320) setW(w);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [shown.length]);
  // the legend above names every series (with its colour, dash and marker), so the lines carry no cut-off end labels
  const PAD = PAD0;
  if (shown.length === 0 || shown.every((s) => s.points.length < 2)) {
    return <p className="stk-label">Not enough data to draw this chart: it needs at least two points.</p>;
  }

  const xs = [...new Set(drawn.flatMap((s) => s.points.map((p) => p[0])))].sort((a, b) => a - b);
  const ys = shown.flatMap((s) => s.points.map((p) => p[1]));
  if (baseline !== undefined) ys.push(baseline);
  const xLo = Math.min(xs[0], ...(marks ?? []));
  const xHi = Math.max(xs[xs.length - 1], ...(marks ?? []));
  let yLo = Math.min(...ys);
  let yHi = Math.max(...ys);
  if (yHi === yLo) { yLo -= 1; yHi += 1; }
  const yPad = (yHi - yLo) * 0.06;
  yLo -= yPad;
  yHi += yPad;
  const px = (x: number) => PAD.l + (xHi === xLo ? 0 : ((x - xLo) / (xHi - xLo)) * (W - PAD.l - PAD.r));
  const py = (y: number) => PAD.t + (1 - (y - yLo) / (yHi - yLo)) * (H - PAD.t - PAD.b);
  const yTicks = niceTicks(yLo, yHi, 5);
  // as many date labels as fit without touching (about 90 px each), never fewer than two
  const maxTicks = Math.max(2, Math.min(6, Math.floor((W - PAD.l - PAD.r) / 90)));
  const xTicks = xs.length <= maxTicks ? xs : niceTicks(xLo, xHi, maxTicks);
  const path = (seg: [number, number][]) =>
    seg.map((p, i) => `${i ? "L" : "M"}${px(p[0]).toFixed(1)},${py(p[1]).toFixed(1)}`).join(" ");

  const at = cursor === null ? null : (xs[Math.min(cursor, xs.length - 1)] ?? null);
  const valueAt = (s: LineSeries, x: number) => s.points.find((p) => p[0] === x)?.[1];
  const summary = `${label ?? "Line chart"}: ${shown.map((s) => {
    const sorted = [...s.points].sort((a, b) => a[0] - b[0]);
    return `${s.label} from ${yFormat(sorted[0][1])} to ${yFormat(sorted[sorted.length - 1][1])}`;
  }).join("; ")}.${describe ? ` ${describe}.` : ""} Left and right arrow keys read the values.`;

  const move = (to: number) => setCursor(Math.max(0, Math.min(xs.length - 1, to)));
  const onKey = (e: React.KeyboardEvent) => {
    setKeyboard(true);
    if (e.key === "ArrowRight") { e.preventDefault(); move((cursor ?? -1) + 1); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); move((cursor ?? xs.length) - 1); }
    else if (e.key === "Home") { e.preventDefault(); move(0); }
    else if (e.key === "End") { e.preventDefault(); move(xs.length - 1); }
    else if (e.key === "Escape") setCursor(null);
  };
  const onPointer = (e: React.PointerEvent<SVGSVGElement>) => {
    setKeyboard(false);
    const box = e.currentTarget.getBoundingClientRect();
    const frac = (((e.clientX - box.left) / box.width) * W - PAD.l) / (W - PAD.l - PAD.r);
    const x = xLo + frac * (xHi - xLo);
    let best = 0;
    xs.forEach((v, i) => { if (Math.abs(v - x) < Math.abs(xs[best] - x)) best = i; });
    setCursor(best);
  };

  return (
    <figure className="stk-lc">
      {shown.length > 1 && (
        <ul className="stk-lc-legend" aria-label="Series">
          {shown.map((s, i) => (
            <li key={s.label}>
              <svg width="28" height="10" aria-hidden="true">
                <line x1="0" x2="28" y1="5" y2="5" className="stk-lc-line" data-s={i} data-c={i % COLOURS} />
                <Marker x={14} y={5} i={i} />
              </svg>
              {s.label}
            </li>
          ))}
        </ul>
      )}
      {yLabel && <p className="stk-fine stk-lc-ytitle">{yLabel}</p>}
      <svg
        ref={svgRef} viewBox={`0 0 ${W} ${H}`} role="group" aria-roledescription="chart" aria-label={summary} tabIndex={0}
        onKeyDown={onKey} onPointerMove={onPointer} onPointerLeave={() => setCursor(null)} onBlur={() => setCursor(null)}
      >
        {yTicks.map((t) => (
          <g key={t}>
            <line x1={PAD.l} x2={W - PAD.r} y1={py(t)} y2={py(t)} className="stk-lc-grid" />
            <text x={PAD.l - 8} y={py(t)} className="stk-lc-tick" textAnchor="end" dominantBaseline="middle">{yFormat(t)}</text>
          </g>
        ))}
        {xTicks.map((t) => (
          <text key={t} x={px(t)} y={H - 8} className="stk-lc-tick" textAnchor="middle">{xFormat(t)}</text>
        ))}
        {baseline !== undefined && (
          <line x1={PAD.l} x2={W - PAD.r} y1={py(baseline)} y2={py(baseline)} className="stk-lc-base" />
        )}
        {(marks ?? []).map((m) => (
          <line key={m} x1={px(m)} x2={px(m)} y1={PAD.t} y2={H - PAD.b} className="stk-lc-mark" />
        ))}
        {shown.map((s, i) => splitSegments(s.points, s.breaks).map((seg) => (
          seg.length === 1
            ? <Marker key={`${s.label}-${seg[0][0]}`} x={px(seg[0][0])} y={py(seg[0][1])} i={i} />
            : <path key={`${s.label}-${seg[0][0]}`} d={path(seg)} className="stk-lc-line" data-s={i} data-c={i % COLOURS} />
        )))}
        {shown.map((s, i) => {
          const sorted = [...s.points].sort((a, b) => a[0] - b[0]);
          return markerIndexes(sorted.length, i).map((k) => (
            <Marker key={`${s.label}-m${k}`} x={px(sorted[k][0])} y={py(sorted[k][1])} i={i} />
          ));
        })}
        {at !== null && (
          <g>
            <line x1={px(at)} x2={px(at)} y1={PAD.t} y2={H - PAD.b} className="stk-lc-cursor" />
            {shown.map((s, i) => {
              const v = valueAt(s, at);
              return v === undefined ? null : <Marker key={s.label} x={px(at)} y={py(v)} i={i} />;
            })}
          </g>
        )}
      </svg>
      <p className="stk-lc-read" aria-live={keyboard ? "polite" : "off"}>
        {at === null
          ? "Point at the chart, or focus it and use the arrow keys, to read the values."
          : `${xFormat(at)}: ${shown.map((s) => { const v = valueAt(s, at); return `${s.label} ${v === undefined ? "—" : yFormat(v)}`; }).join(", ")}`}
      </p>
      {baseline !== undefined && <p className="stk-fine">Dashed line: {baselineLabel ? `${baselineLabel} ` : ""}{yFormat(baseline)}.</p>}
      {marks !== undefined && marks.length > 0 && marksLabel && <p className="stk-fine">Dotted lines: {marksLabel}.</p>}
      {hidden > 0 && <p className="stk-fine">Showing the first {MAX_SERIES} series; all {drawn.length} are in the table.</p>}
      <details className="stk-lc-table">
        <summary>Show the values as a table</summary>
        <div className="stk-table-wrap">
          <table className="stk-table">
            <caption>{label ?? drawn[0].label}</caption>
            <thead>
              <tr>
                <th scope="col">{xLabel}</th>
                {drawn.map((s) => <th key={s.label} scope="col" className="num">{s.label}</th>)}
              </tr>
            </thead>
            <tbody>
              {xs.map((x) => (
                <tr key={x}>
                  <th scope="row">{xFormat(x)}</th>
                  {drawn.map((s) => {
                    const v = valueAt(s, x);
                    return <td key={s.label} className="num">{v === undefined ? "—" : yFormat(v)}</td>;
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}

/** A series marker: a circle for the three colour-distinct series, then a square, a diamond and a triangle, so a reused colour still differs. */
function Marker({ x, y, i }: { x: number; y: number; i: number }) {
  const c = i % COLOURS;
  if (i < COLOURS) return <circle cx={x} cy={y} r={4} className="stk-lc-dot" data-c={c} />;
  if (i === COLOURS) return <rect x={x - 4} y={y - 4} width={8} height={8} className="stk-lc-dot" data-c={c} />;
  if (i === COLOURS + 1) return <polygon points={`${x},${y - 5.5} ${x + 5.5},${y} ${x},${y + 5.5} ${x - 5.5},${y}`} className="stk-lc-dot" data-c={c} />;
  return <polygon points={`${x},${y - 5} ${x + 5},${y + 4} ${x - 5},${y + 4}`} className="stk-lc-dot" data-c={c} />;
}

const IW = 320;  // shown at most 20rem wide (globals.css), so the zero label keeps about its token size
const IH = 64;

/** One estimate with its interval: a bar from `lo` to `hi` and a marker at `point`. Any missing number gives words, not a bar. */
export function IntervalBar({
  label,
  point,
  lo,
  hi,
  format,
}: {
  label: string;
  point: number | null;
  lo: number | null;
  hi: number | null;
  format: Fmt;
}) {
  const known = point !== null && lo !== null && hi !== null;
  const summary = known
    ? `${label}: ${format(point)}, likely between ${format(lo)} and ${format(hi)}`
    : `${label}: not enough days to estimate`;
  return (
    <figure className="stk-iv">
      <figcaption className="stk-iv-name">{label}</figcaption>
      {known ? (
        <IntervalSvg point={point} lo={lo} hi={hi} format={format} />
      ) : (
        <p className="stk-label">Not enough days to estimate.</p>
      )}
      <table className="stk-iv-table">
        <caption className="sr-only">{summary}</caption>
        <thead>
          <tr><th scope="col">Low</th><th scope="col">Estimate</th><th scope="col">High</th></tr>
        </thead>
        <tbody>
          <tr>
            <td>{lo === null ? "—" : format(lo)}</td>
            <td>{point === null ? "—" : format(point)}</td>
            <td>{hi === null ? "—" : format(hi)}</td>
          </tr>
        </tbody>
      </table>
    </figure>
  );
}

function IntervalSvg({ point, lo, hi, format }: { point: number; lo: number; hi: number; format: Fmt }) {
  const a = Math.min(lo, hi, point);
  const b = Math.max(lo, hi, point);
  const pad = (b - a || Math.abs(a) || 1) * 0.15;
  const dLo = a - pad;
  const dHi = b + pad;
  const x = (v: number) => 16 + ((v - dLo) / (dHi - dLo)) * (IW - 32);
  const zero = dLo < 0 && dHi > 0;
  return (
    <svg viewBox={`0 0 ${IW} ${IH}`} aria-hidden="true">
      {zero && (
        <g>
          <line x1={x(0)} x2={x(0)} y1={8} y2={36} className="stk-iv-zero" />
          <text x={x(0)} y={IH - 6} className="stk-lc-tick" textAnchor="middle">{format(0)}</text>
        </g>
      )}
      <line x1={x(lo)} x2={x(hi)} y1={22} y2={22} className="stk-iv-span" />
      <line x1={x(lo)} x2={x(lo)} y1={14} y2={30} className="stk-iv-cap" />
      <line x1={x(hi)} x2={x(hi)} y1={14} y2={30} className="stk-iv-cap" />
      <circle cx={x(point)} cy={22} r={6} className="stk-iv-point" />
    </svg>
  );
}

/**
 * A grid of numbers shaded by size, rows `ys` by columns `xs`; `cells[row][column]`, null for a run that did not finish.
 * Every cell prints its value, so shade is never the only signal, and `best` (column x, row y) is outlined and named.
 * `signed` switches to a scale centred on zero: blue above, orange below, stronger further from zero; the values print
 * their sign and the cells below zero are striped, so a negative reads differently by more than colour.
 */
export function HeatGrid({
  xs,
  ys,
  cells,
  format,
  best,
  selected,
  signed,
  links,
  label,
  xLabel,
  yLabel,
}: {
  xs: string[];
  ys: string[];
  cells: (number | null)[][];
  format: Fmt;
  best?: { x: number; y: number } | null;
  /** The cell whose run is shown elsewhere on the page: marked by a bar under the value and `aria-current`. */
  selected?: { x: number; y: number } | null;
  signed?: boolean;
  /** `links[row][column]`: where a cell's value leads (its run), or null for none. */
  links?: (string | null)[][];
  label?: string;
  xLabel?: string;
  yLabel?: string;
}) {
  const flat = cells.flat().filter((v): v is number => v !== null && Number.isFinite(v));
  if (flat.length === 0) return <p className="stk-label">No finished runs to show.</p>;
  const lo = Math.min(...flat);
  const hi = Math.max(...flat);
  const maxAbs = Math.max(Math.abs(lo), Math.abs(hi));
  const bestVal = best ? cells[best.y]?.[best.x] : null;
  const summary = `${label ?? "Grid"}: ${ys.length} rows by ${xs.length} columns, from ${format(lo)} to ${format(hi)}` +
    (best && bestVal != null ? `. Best: ${ys[best.y]}, ${xs[best.x]}, ${format(bestVal)}` : "");
  return (
    <figure className="stk-heat">
      <div className="stk-table-wrap" tabIndex={0} role="region" aria-label={`${label ?? "Grid"}, scrolls sideways when it is wider than the page`}>
        <table className="stk-table" aria-label={summary}>
          <caption>
            {label}{yLabel || xLabel ? ` (rows: ${yLabel ?? "row"}; columns: ${xLabel ?? "column"})` : ""}
          </caption>
          <thead>
            <tr>
              <th scope="col"><span className="sr-only">{yLabel ?? "Row"}</span></th>
              {xs.map((x) => <th key={x} scope="col" className="num">{x}</th>)}
            </tr>
          </thead>
          <tbody>
            {ys.map((y, yi) => (
              <tr key={y}>
                <th scope="row">{y}</th>
                {xs.map((x, xi) => {
                  const v = cells[yi]?.[xi] ?? null;
                  const isBest = best?.x === xi && best?.y === yi;
                  const isSel = selected?.x === xi && selected?.y === yi;
                  const ok = v !== null && Number.isFinite(v);
                  const d = signed && ok ? divergingStep(v, maxAbs) : 0;
                  return (
                    <td
                      key={x} className="num stk-heat-cell"
                      data-sign={signed && ok ? (d > 0 ? "pos" : d < 0 ? "neg" : "zero") : undefined}
                      data-step={!ok ? undefined : signed ? Math.abs(d) : heatColourStep(v, lo, hi, HEAT_STEPS)}
                      data-best={isBest ? "true" : undefined}
                      data-selected={isSel ? "true" : undefined}
                    >
                      {v !== null && links?.[yi]?.[xi] ? (
                        <Link href={links[yi][xi] as string} aria-current={isSel ? "true" : undefined}>
                          {format(v)}
                          <span className="sr-only"> (show the chart for {y}, {x})</span>
                        </Link>
                      ) : v === null ? "—" : format(v)}
                      {isBest && <span className="sr-only"> (best)</span>}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <figcaption className="stk-fine">
        {signed
          ? `${[
              hi > 0 && `${upperFirst(DIVERGING.positive.word)} cells are above zero`,
              lo < 0 && `${hi > 0 ? "" : "Cells are below zero: "}${DIVERGING.negative.word} striped cells${hi > 0 ? " are below zero" : ""}`,
            ].filter(Boolean).join(" and ")}. ${lo < 0 ? "A minus sign marks every value below zero, so you do not need the colours to tell them apart. " : ""}The stronger the shade, the further from zero. Values run from ${format(lo)} to ${format(hi)}.`
          : `${upperFirst(HEAT_MORE)} cells are larger values, from ${format(lo)} to ${format(hi)}.`}
        {best ? " The outlined cell is the best." : ""}
      </figcaption>
    </figure>
  );
}

"use client";

/**
 * One small chart per figure: the stock's quarters against its peers' median for the same quarter. A client
 * component only because the chart takes formatting functions, which a server component cannot hand over.
 */
import { LineChart, type LineSeries } from "@/components/charts";
import { chartLines, figureName, figureText, MEANINGS, quarterWords } from "@/lib/fundamentals";
import type { SeriesDTO } from "@/lib/types";

export default function FigureCharts({ symbol, series, medianName }: { symbol: string; series: SeriesDTO[]; medianName: string }) {
  return (
    <div className="stk-fund-charts">
      {series.map((s) => {
        const l = chartLines(s);
        const lines: LineSeries[] = [{ label: symbol, points: l.own, breaks: l.ownBreaks }];
        const hasMedian = l.median.length > 0;
        if (hasMedian) lines.push({ label: medianName, points: l.median, breaks: l.medianBreaks });
        const name = figureName(s.key, s.label);
        return (
          <section key={s.key} className="stk-fund-chart" aria-label={name}>
            <h3>{name}</h3>
            {MEANINGS[s.key] && <p className="stk-fine">{MEANINGS[s.key]}</p>}
            <LineChart
              series={lines}
              yFormat={(n) => figureText(s.unit, n)}
              xFormat={quarterWords}
              xLabel="Quarter ended"
              label={`${name}, ${symbol} against the ${medianName.toLowerCase()}, by quarter`}
            />
            {!hasMedian && <p className="stk-fine">No peer median to draw: the stock is not compared with others yet.</p>}
          </section>
        );
      })}
    </div>
  );
}

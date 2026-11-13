"use client";

import { LineChart, type LineSeries } from "@/components/charts";
import { pct } from "@/lib/format";

const DAY = new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
const dayText = (n: number) => DAY.format(new Date(n * 86_400_000));

/** The two charts of the compare page. A client component only because the chart takes formatting functions, which a
 * server component cannot hand over; every series, label and sentence comes from the page. */
export default function CompareCharts({ equity, drawdown }: { equity: LineSeries[]; drawdown: LineSeries[] }) {
  return (
    <div className="stk-cmp-charts">
      <section className="stk-section" aria-labelledby="cmp-equity">
        <h2 id="cmp-equity">Growth of 1</h2>
        <LineChart
          series={equity}
          yFormat={(n) => n.toFixed(3)}
          xFormat={dayText}
          xLabel="Date"
          baseline={1}
          baselineLabel="starting capital"
          label="Growth of 1 for each run"
          yLabel="growth of 1 (dashed line = starting capital)"
        />
      </section>
      <section className="stk-section" aria-labelledby="cmp-dd">
        <h2 id="cmp-dd">Fall from the highest point so far</h2>
        <LineChart
          series={drawdown}
          yFormat={(n) => pct(n)}
          xFormat={dayText}
          xLabel="Date"
          baseline={0}
          baselineLabel="a new high"
          label="Fall from the highest point so far, for each run"
          yLabel="how far below its own highest value each run was (0 % means at a high)"
        />
      </section>
    </div>
  );
}

"use client";

import { LineChart } from "@/components/charts";
import type { WfCurve } from "@/lib/research";

const DAY = new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });

/** The stitched out-of-sample curve. A client component only because the chart takes formatting functions, which a
 * server component cannot hand over; the numbers and every sentence come from the page (its server text). */
export default function Curve({ curve, yLabel, gaps }: { curve: WfCurve; yLabel: string; gaps: string }) {
  return (
    <LineChart
      series={[{ label: "Out of sample", points: curve.points, breaks: curve.breaks }]}
      yFormat={(n) => n.toFixed(3)}
      xFormat={(n) => DAY.format(new Date(n * 86_400_000))}
      xLabel="Date"
      baseline={1}
      baselineLabel="starting level"
      marks={curve.starts}
      marksLabel="the first day of each test window"
      label="Out-of-sample equity, growth of 1"
      yLabel={yLabel}
      describe={gaps}
    />
  );
}

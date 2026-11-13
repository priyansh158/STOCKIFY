"use client";

import { HeatGrid } from "@/components/charts";
import { heatGridProps } from "@/lib/researchHelpers";
import type { SweepHeatmapDTO } from "@/lib/types";

/** The sweep's heatmap. A client component because HeatGrid takes a formatter function, which a server page cannot pass.
 * A one-axis sweep has no second setting, so its single row is named by what is scored. */
export default function SweepGrid({ cells, scored, caption }: { cells: SweepHeatmapDTO; scored: string; caption: string }) {
  const p = heatGridProps(cells);
  return <HeatGrid {...p} ys={cells.y === null ? [scored] : p.ys} label={caption} />;
}

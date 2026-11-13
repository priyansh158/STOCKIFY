"use client";

import { HeatGrid } from "@/components/charts";
import { heatGridProps } from "@/lib/researchHelpers";
import type { SweepHeatmapDTO } from "@/lib/types";

/** The sweep's heatmap. A client component because HeatGrid takes a formatter function, which a server page cannot pass.
 * A one-axis sweep has no second setting, so its single row is named by what is scored. */
export default function SweepGrid({
  cells,
  labels,
  scored,
  caption,
}: {
  cells: SweepHeatmapDTO;
  labels: Record<string, string>;
  scored: string;
  caption: string;
}) {
  return <HeatGrid {...heatGridProps(cells, { labels, scored })} label={caption} />;
}

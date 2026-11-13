"use client";

import type { KeyboardEvent } from "react";
import { HeatGrid } from "@/components/charts";
import { trialHref } from "@/lib/chart";
import { heatGridProps } from "@/lib/researchHelpers";
import type { SweepHeatmapDTO } from "@/lib/types";

/** The sweep's heatmap. A client component because HeatGrid takes a formatter function, which a server page cannot pass.
 * A one-axis sweep has no second setting, so its single row is named by what is scored.
 * A cell is a link to `?trial=<run id>`: Enter follows it, and so does Space (a link takes only Enter by itself). */
export default function SweepGrid({
  sweepId,
  cells,
  labels,
  scored,
  caption,
  pickedRunId,
}: {
  sweepId: string;
  cells: SweepHeatmapDTO;
  labels: Record<string, string>;
  scored: string;
  caption: string;
  /** The run whose chart is shown under the grid. */
  pickedRunId: string | null;
}) {
  const props = heatGridProps(cells, { labels, scored });
  const links = cells.run_ids.map((row) => row.map((id) => (id ? trialHref(sweepId, cells.x, cells.y, id) : null)));
  let selected: { x: number; y: number } | null = null;
  if (pickedRunId !== null) {
    cells.run_ids.forEach((row, yi) =>
      row.forEach((id, xi) => {
        if (id === pickedRunId) selected = { x: xi, y: yi };
      }),
    );
  }
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const t = e.target as HTMLElement;
    if (e.key === " " && t instanceof HTMLAnchorElement) {
      e.preventDefault(); // Space would scroll the page
      t.click();
    }
  };
  return (
    <div onKeyDown={onKeyDown}>
      <HeatGrid {...props} links={links} selected={selected} label={caption} />
    </div>
  );
}

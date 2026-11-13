"use client";

import { useCallback, useMemo, useState } from "react";
import type { ComponentProps } from "react";
import { PriceChartLazy } from "@/components/PriceChartLazy";
import { windowBands } from "@/lib/chart";
import { getWalkforwardChart } from "@/lib/chart_api";
import type { WalkforwardChartWindowDTO } from "@/lib/types";

/**
 * The walk-forward job's out-of-sample test windows on the price: bars and trades from the test runs only, each window a
 * band, a window with nothing to draw (excluded, no trades, failed) shaded in the warning tint and named in the list under
 * the chart. A client island because the chart is: the windows arrive with the chart's first answer.
 */
export default function WfChart({ jobId }: { jobId: string }) {
  const [wins, setWins] = useState<WalkforwardChartWindowDTO[] | null>(null);
  const [openS, setOpenS] = useState<number | null>(null);

  const load = useCallback<NonNullable<ComponentProps<typeof PriceChartLazy>["load"]>>(
    async (range, opts) => {
      const model = await getWalkforwardChart(jobId, { ...opts, ...range });
      if (model?.windows) setWins(model.windows);
      if (model?.session_open_s !== undefined) setOpenS(model.session_open_s);
      return model;
    },
    [jobId],
  );
  const bands = useMemo(() => (wins ? windowBands(wins) : undefined), [wins]);

  return (
    <div className="stk-stack-s">
      <PriceChartLazy load={load} title="Price in the test windows" headingLevel={3} windows={bands} sessionOpenS={openS} />
      {bands && bands.length > 0 && (
        <ul className="stk-pricechart-legend" aria-label="The test windows on the chart">
          {bands.map((b) => (
            <li key={b.label} data-gap={b.gap ? "true" : undefined}>
              {b.label}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

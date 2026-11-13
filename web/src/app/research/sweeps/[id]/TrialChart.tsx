"use client";

import { useCallback } from "react";
import { PriceChartLazy } from "@/components/PriceChartLazy";
import { getRunChart } from "@/lib/chart_api";

type Load = NonNullable<React.ComponentProps<typeof PriceChartLazy>["load"]>;

/** The chart of one trial's run: the run endpoint, as the run page uses it. The trades list stays (this page has no Fills table).
 * The page keys it by run id, so choosing another trial starts a fresh chart. */
export default function TrialChart({ runId, title }: { runId: string; title: string }) {
  const load = useCallback<Load>((range, opts) => getRunChart(runId, { ...opts, ...range }), [runId]);
  return <PriceChartLazy load={load} title={title} headingLevel={2} />;
}

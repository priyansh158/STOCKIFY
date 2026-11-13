"use client";

import { useCallback, useEffect, useRef } from "react";
import { PriceChartLazy } from "@/components/PriceChartLazy";
import { getRunChart } from "@/lib/chart_api";

type Load = NonNullable<React.ComponentProps<typeof PriceChartLazy>["load"]>;

/** The chart of one trial's run: the run endpoint, as the run page uses it. The trades list stays (this page has no Fills table).
 * The page keys it by run id, so choosing another trial starts a fresh chart. When the trial was picked (`?trial=` is set) focus
 * moves to the chart's box, which is named after the trial, and a polite live region says which chart is showing. */
export default function TrialChart({ runId, title, pick }: { runId: string; title: string; pick: boolean }) {
  const box = useRef<HTMLDivElement>(null);
  const load = useCallback<Load>((range, opts) => getRunChart(runId, { ...opts, ...range }), [runId]);
  useEffect(() => {
    if (pick) box.current?.focus();
  }, [pick]);
  return (
    <div ref={box} tabIndex={-1} className="stk-trial-box" aria-label={`Chart of ${title.toLowerCase()}`}>
      <p className="sr-only" role="status" aria-live="polite">
        Showing the chart of {title.toLowerCase()}
      </p>
      <PriceChartLazy load={load} title={title} headingLevel={2} />
    </div>
  );
}

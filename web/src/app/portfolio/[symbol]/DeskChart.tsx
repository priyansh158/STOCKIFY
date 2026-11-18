"use client";

/**
 * The holding's daily candles with each rule's exit level drawn on them, up to the latest close the desk has (it
 * updates once a day, after the session). The desk endpoint sends the whole chart at once, so a request for a
 * neighbouring page gets no more bars and the chart stops asking.
 *
 * The desk's exit-level points sit at each bar's start, which is the candle's own time, so they are drawn on the
 * candle they belong to (a run chart's points sit at the bar's end instead).
 */
import { useCallback, useRef } from "react";
import type { ChartModel } from "@/lib/chart";
import { getDeskChart } from "@/lib/desk";
import { PriceChartLazy } from "@/components/PriceChartLazy";

export default function DeskChart({ symbol }: { symbol: string }) {
  const last = useRef<ChartModel | null>(null);

  const load = useCallback<React.ComponentProps<typeof PriceChartLazy>["load"]>(
    async (range, opts) => {
      if (range && last.current) return { ...last.current, bars: [], markers: [] };
      const model = await getDeskChart(symbol, opts?.signal);
      last.current = model;
      return model;
    },
    [symbol],
  );

  return (
    <div className="stk-stack-s">
      <h2>Price and exit levels</h2>
      <PriceChartLazy load={load} title="Daily candles" headingLevel={3} hideTradesList noTrades />
    </div>
  );
}

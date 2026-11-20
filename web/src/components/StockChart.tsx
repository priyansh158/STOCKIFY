"use client";

/**
 * A stock's daily candles for about a year with its 52-week high and low drawn as lines, from the nightly candles. The
 * endpoint sends the whole chart at once, so a request for a neighbouring page gets no more bars and the chart stops asking.
 * Used by the fundamentals Details page and the watchlist rows.
 */
import { useCallback, useRef } from "react";
import type { ChartModel } from "@/lib/chart";
import { getStockChart } from "@/lib/stocks";
import { PriceChartLazy } from "./PriceChartLazy";

export default function StockChart({ symbol, headingLevel = 3 }: { symbol: string; headingLevel?: 2 | 3 }) {
  const last = useRef<ChartModel | null>(null);

  const load = useCallback<React.ComponentProps<typeof PriceChartLazy>["load"]>(
    async (range, opts) => {
      if (range && last.current) return { ...last.current, bars: [], markers: [] };
      const model = await getStockChart(symbol, opts?.signal);
      last.current = model;
      return model;
    },
    [symbol],
  );

  return <PriceChartLazy load={load} title="Daily candles" headingLevel={headingLevel} hideTradesList noTrades />;
}

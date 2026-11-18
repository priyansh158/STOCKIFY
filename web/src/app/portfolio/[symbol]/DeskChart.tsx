"use client";

/**
 * The holding's candles with each rule's exit level drawn on them: daily, or the latest session's one-minute candles. During the
 * session (the view is live) the chart asks again every minute. The desk endpoint sends the whole chart at once, so a
 * request for a neighbouring page gets no more bars and the chart stops asking.
 *
 * The desk's exit-level points sit at each bar's start, which is the candle's own time, so they are drawn on the
 * candle they belong to (a run chart's points sit at the bar's end instead).
 */
import { useCallback, useRef, useState } from "react";
import type { ChartModel } from "@/lib/chart";
import { DESK_POLL_MS, getDeskChart, type DeskInterval } from "@/lib/desk";
import { PriceChartLazy } from "@/components/PriceChartLazy";

const CHOICES: { value: DeskInterval; label: string }[] = [
  { value: "day", label: "Daily" },
  { value: "minute", label: "Latest session, by the minute" },
];

export default function DeskChart({ symbol, live }: { symbol: string; live: boolean }) {
  const [interval, setChoice] = useState<DeskInterval>("day");
  const last = useRef<ChartModel | null>(null);

  const load = useCallback<React.ComponentProps<typeof PriceChartLazy>["load"]>(
    async (range, opts) => {
      if (range && last.current) return { ...last.current, bars: [], markers: [] };
      const model = await getDeskChart(symbol, interval, opts?.signal);
      last.current = model;
      return model;
    },
    [symbol, interval],
  );

  return (
    <div className="stk-stack-s">
      <div className="stk-row-between">
        <h2>Price and exit levels</h2>
        <div className="stk-row" role="group" aria-label="Candles">
        {CHOICES.map((c) => (
          <button
            key={c.value}
            type="button"
            className="stk-btn"
            data-tone="quiet"
            aria-pressed={interval === c.value}
            aria-current={interval === c.value ? "true" : undefined}
            onClick={() => setChoice(c.value)}
          >
            {c.label}
          </button>
        ))}
        </div>
      </div>
      <PriceChartLazy
        key={interval}
        load={load}
        title={interval === "day" ? "Daily candles" : "One-minute candles of the latest session"}
        headingLevel={3}
        hideTradesList
        noTrades
        pollMs={live ? DESK_POLL_MS : undefined}
      />
    </div>
  );
}

"use client";

/**
 * The only way pages load the chart: the charting library touches `document` and canvas, so it is fetched in the
 * browser only (next/dynamic with ssr: false, which is allowed in a Client Component).
 */
import dynamic from "next/dynamic";
import { Loading, Skeleton } from "./ui";

export const PriceChartLazy = dynamic(() => import("./PriceChart").then((m) => m.PriceChart), {
  ssr: false,
  loading: () => (
    <Loading label="Loading the chart">
      <Skeleton size="l" />
    </Loading>
  ),
});

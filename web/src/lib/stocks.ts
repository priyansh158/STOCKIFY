/** CW 4: the chart of one stock's daily candles (GET /api/stocks/{symbol}/chart). A 404 (no such NSE stock) gives null. */
import { api, ApiError } from "./api";
import type { ChartModel } from "./chart";
import { pathSegment } from "./pathSeg";

export async function getStockChart(symbol: string, signal?: AbortSignal): Promise<ChartModel | null> {
  try {
    return await api<ChartModel>(`/api/stocks/${pathSegment(symbol)}/chart`, { signal });
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
}

/**
 * The watchlist (CW 2): calls for /api/watchlist and the words for a failed call. The small sums are in
 * watchlistMath.ts (no imports, so a test can run them).
 */
import { api, ApiError } from "./api";
import type { WatchAddIn, WatchRowDTO, WatchlistDTO } from "./types";

export { aboveLow, belowHigh, ltpPaise } from "./watchlistMath";

export function getWatchlist(signal?: AbortSignal): Promise<WatchlistDTO> {
  return api<WatchlistDTO>("/api/watchlist", { signal });
}

export function addWatched(symbol: string): Promise<WatchRowDTO> {
  const body: WatchAddIn = { symbol };
  return api<WatchRowDTO>("/api/watchlist", { method: "POST", body: JSON.stringify(body) });
}

export function removeWatched(symbol: string): Promise<void> {
  return api<void>(`/api/watchlist/${encodeURIComponent(symbol)}`, { method: "DELETE" });
}

/** Why a call failed, in words for the page. The API's own sentence when it sent one; a validation error (its detail is
 *  a list, not a sentence) and anything else get a plain line. */
export function problemWords(e: unknown, doing: string): string {
  if (e instanceof ApiError) {
    if (e.status === 404 || e.status === 409 || (e.status === 422 && !e.message.includes("[object"))) return e.message;
    if (e.status === 422) return "That is not a symbol the watchlist can use.";
    return `Could not ${doing}: ${e.message}`;
  }
  return `Could not ${doing}. Check that the STOCKIFY API is running, then try again.`;
}

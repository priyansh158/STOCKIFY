/**
 * Calls for the price chart (CH 6.15): a run, a walk-forward job and a paper session each have a chart endpoint of the
 * same shape (task C11 for runs, C12 for walk-forward, C13 for sessions). A 404 gives null, so the page can say
 * "no chart" without treating it as a failure. Every other error is thrown as an ApiError.
 */
import { api, ApiError } from "./api";
import { chartQuery, type ChartModel, type ChartOpts } from "./chart";

async function get(path: string, opts: ChartOpts): Promise<ChartModel | null> {
  try {
    return await api<ChartModel>(path + chartQuery(opts), { signal: opts.signal });
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
}

/** GET /api/tests/{run_id}/chart?instrument=&from_ns=&to_ns= */
export function getRunChart(runId: string, opts: ChartOpts = {}): Promise<ChartModel | null> {
  return get(`/api/tests/${encodeURIComponent(runId)}/chart`, { ...opts, trade_date: undefined });
}

/** GET /api/research/walkforward/{job_id}/chart?instrument=&from_ns=&to_ns= */
export function getWalkforwardChart(jobId: string, opts: ChartOpts = {}): Promise<ChartModel | null> {
  return get(`/api/research/walkforward/${encodeURIComponent(jobId)}/chart`, { ...opts, trade_date: undefined });
}

/** GET /api/trade/sessions/{session_id}/chart?instrument=&trade_date= (a session shows one day at a time). */
export function getSessionChart(sessionId: string, opts: ChartOpts = {}): Promise<ChartModel | null> {
  return get(`/api/trade/sessions/${encodeURIComponent(sessionId)}/chart`, { ...opts, from_ns: undefined, to_ns: undefined });
}

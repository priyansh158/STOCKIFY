/**
 * Calls for the research and data pages (/api/tests/{id}/research, /api/research/families, /api/data/coverage…),
 * and the pure helpers those pages share (re-exported from researchHelpers.ts so Node can check them alone).
 * Server components may call these; the base URL is handled by `api`.
 */
import { api, ApiError } from "./api";
import type {
  ComparisonDTO,
  CoverageMatrixDTO,
  FamilyDetailDTO,
  FamilySummaryDTO,
  GapPageDTO,
  RunResearchDTO,
  SensitivityDTO,
  WalkforwardDTO,
} from "./types";

export { bucketTone, dayNumber, heatColourStep, sentenceOrDash, splitSegments, wfCurve } from "./researchHelpers";
export type { BucketTone, WfCurve } from "./researchHelpers";

const enc = encodeURIComponent;

/** null when the API says 404 (nothing there yet); any other failure still throws. */
async function orNull<T>(call: Promise<T>): Promise<T | null> {
  try {
    return await call;
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
}

/** Deflated Sharpe, bootstrap and regime breakdown of one run. */
export function getRunResearch(id: string): Promise<RunResearchDTO> {
  return api<RunResearchDTO>(`/api/tests/${enc(id)}/research`);
}

/** The stored sensitivity suite of a run, or null when it has not been run. */
export function getSensitivity(id: string): Promise<SensitivityDTO | null> {
  return orNull(api<SensitivityDTO>(`/api/tests/${enc(id)}/sensitivity`));
}

export function getFamilies(offset = 0, limit = 50): Promise<FamilySummaryDTO[]> {
  return api<FamilySummaryDTO[]>(`/api/research/families?offset=${offset}&limit=${limit}`);
}

/** One trial family, or null when there is no such family. */
export function getFamily(id: string): Promise<FamilyDetailDTO | null> {
  return orNull(api<FamilyDetailDTO>(`/api/research/families/${id.split("/").map(enc).join("/")}`));
}

/** 2 to 6 finished runs side by side. */
export function getComparison(ids: string[]): Promise<ComparisonDTO> {
  return api<ComparisonDTO>(`/api/tests/compare?${ids.map((i) => `ids=${enc(i)}`).join("&")}`);
}

/** The data coverage matrix; both dates are IST days (YYYY-MM-DD) and optional. */
export function getCoverage(from?: string, to?: string): Promise<CoverageMatrixDTO> {
  const q = new URLSearchParams();
  if (from) q.set("from", from);
  if (to) q.set("to", to);
  const s = q.toString();
  return api<CoverageMatrixDTO>(`/api/data/coverage${s ? `?${s}` : ""}`);
}

/** One page of a recorded day's gaps, or null when nothing was recorded that day. */
export function getGaps(date: string, offset = 0, limit = 200): Promise<GapPageDTO | null> {
  return orNull(api<GapPageDTO>(`/api/data/gaps?date=${enc(date)}&offset=${offset}&limit=${limit}`));
}

/** A walk-forward job: finished with its report, not finished (still going or failed, with a plain reason when there is one), or gone. */
export type WalkforwardLoad =
  | { kind: "done"; report: WalkforwardDTO }
  | { kind: "not_done"; status: string; reason: string | null }
  | { kind: "unreadable" }
  | { kind: "unknown" };

/** Never throws for a job's own state: only a failure to reach the API does. */
export async function getWalkforward(id: string): Promise<WalkforwardLoad> {
  try {
    return { kind: "done", report: await api<WalkforwardDTO>(`/api/research/walkforward/${enc(id)}`) };
  } catch (e) {
    if (!(e instanceof ApiError)) throw e;
    if (e.status === 404) return { kind: "unknown" };
    if (e.status === 409) {
      const b = e.body as { status?: unknown; reason?: unknown } | null;
      if (b && typeof b === "object" && typeof b.status === "string" && b.status !== "") {
        return { kind: "not_done", status: b.status, reason: typeof b.reason === "string" ? b.reason : null };
      }
      return { kind: "unreadable" };
    }
    throw e;
  }
}

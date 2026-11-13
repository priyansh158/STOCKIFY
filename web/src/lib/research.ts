/**
 * Calls for the research and data pages (/api/tests/{id}/research, /api/research/families, /api/data/coverage…),
 * and the pure helpers those pages share (re-exported from researchHelpers.ts so Node can check them alone).
 * Server components may call these; the base URL is handled by `api`.
 */
import { api, ApiError } from "./api";
import type {
  ComparisonDTO,
  CoverageMatrixDTO,
  DataQualityResponse,
  FamilyDetailDTO,
  FamilySummaryDTO,
  GapPageDTO,
  ResearchCountsDTO,
  ResearchJobDTO,
  RunResearchDTO,
  SensitivityDTO,
} from "./types";

export { bucketTone, costScale, familyHref, familyIdParam, heatColourStep, jobHref, listCapNote, objectiveText, roleWords, sentenceOrDash, sessionRows, unlockRows } from "./researchHelpers";
export type { BucketTone, SessionRow, UnlockRow } from "./researchHelpers";

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

/** How many families, sweeps and walk-forward runs exist in all (the lists above are capped). */
export function getResearchCounts(): Promise<ResearchCountsDTO> {
  return api<ResearchCountsDTO>("/api/research/counts");
}

/** Sweep and walk-forward jobs, newest first (sensitivity jobs belong to a run and are left out). */
export async function getJobs(limit = 50): Promise<{ sweeps: ResearchJobDTO[]; walkforwards: ResearchJobDTO[] }> {
  const [sweeps, walkforwards] = await Promise.all([
    api<ResearchJobDTO[]>(`/api/research/jobs?kind=sweep&limit=${limit}`),
    api<ResearchJobDTO[]>(`/api/research/jobs?kind=walkforward&limit=${limit}`),
  ]);
  return { sweeps, walkforwards };
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

/** A day's stored quality report, or the API's own sentence about why there is none (404: it has not been run). */
export async function getQuality(date: string): Promise<{ report: DataQualityResponse | null; reason: string | null }> {
  try {
    return { report: await api<DataQualityResponse>(`/api/data/quality?date=${enc(date)}`), reason: null };
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return { report: null, reason: e.message };
    throw e;
  }
}

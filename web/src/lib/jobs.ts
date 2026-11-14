/**
 * Calls for the jobs the owner starts from a page (D-145): a sweep, a walk-forward, a cost check, an NSE download, a
 * quality report. Every start answers 202 with the job; `useJob` then polls its progress until it ends, and a page
 * that is reloaded finds a running job again through `findRunning`.
 */
"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import { pathSegment } from "./pathSeg";
import type {
  BackfillRequestDTO,
  QualityReportRequestDTO,
  ResearchLimitsDTO,
  SweepRequestDTO,
  WalkforwardRequestDTO,
  WebJobDTO,
  WebJobKind,
} from "./types";

const POST = (body?: unknown): RequestInit => ({ method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const startSweep = (req: SweepRequestDTO) => api<WebJobDTO>("/api/research/sweeps", POST(req));
export const startWalkforward = (req: WalkforwardRequestDTO) => api<WebJobDTO>("/api/research/walkforwards", POST(req));
export const startSensitivity = (runId: string) => api<WebJobDTO>(`/api/tests/${pathSegment(runId)}/sensitivity`, POST());
export const startBackfill = (req: BackfillRequestDTO) => api<WebJobDTO>("/api/data/backfill", POST(req));
export const startQualityReport = (req: QualityReportRequestDTO) => api<WebJobDTO>("/api/data/report", POST(req));
export const getJob = (id: string) => api<WebJobDTO>(`/api/jobs/${pathSegment(id)}`);
export const getResearchLimits = () => api<ResearchLimitsDTO>("/api/research/limits");

/** The words for a failed start: the API's plain sentence (a 422 or 409), else a generic line. */
export function startFailure(e: unknown): string {
  if (e instanceof ApiError) {
    const body = e.body as { detail?: unknown } | null | undefined;
    const d = body && typeof body === "object" ? body.detail : undefined;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) return d.map((x) => (typeof x === "string" ? x : (x as { msg?: string }).msg ?? "")).filter(Boolean).join(" ") || e.message;
    return e.message;
  }
  return "The app did not answer. Check that STOCKIFY is running, then try again.";
}

/** The running job of this kind and target (any target when null) the API already has (a page that was reloaded or left and opened again). */
export async function findRunning(kind: WebJobKind, target: string | null): Promise<WebJobDTO | null> {
  try {
    const all = await api<WebJobDTO[]>("/api/jobs");
    return all.find((j) => j.kind === kind && (target === null || j.target === target) && j.status === "running") ?? null;
  } catch {
    return null;
  }
}

const POLL_MS = 1500;

/**
 * Follow one job: `job` is the latest state, `begin` starts following a job just started, `error` is a plain line
 * when the progress could not be read (the job itself may still be running). Stops when the job leaves `running`.
 */
export function useJob(kind: WebJobKind, target: string | null, onEnd?: (job: WebJobDTO) => void) {
  const [job, setJob] = useState<WebJobDTO | null>(null);
  const [error, setError] = useState<string | null>(null);
  const endRef = useRef(onEnd);
  useEffect(() => {
    endRef.current = onEnd;
  });

  const follow = useCallback((j: WebJobDTO) => {
    setJob(j);
    setError(null);
  }, []);

  useEffect(() => {
    let stop = false;
    findRunning(kind, target).then((j) => !stop && j && follow(j));
    return () => {
      stop = true;
    };
  }, [kind, target, follow]);

  const id = job && job.status === "running" ? job.job_id : null;
  useEffect(() => {
    if (!id) return;
    let stop = false;
    const tick = async () => {
      try {
        const next = await getJob(id);
        if (stop) return;
        setJob(next);
        setError(null);
        if (next.status !== "running") endRef.current?.(next);
      } catch (e) {
        if (!stop) setError(e instanceof ApiError && e.status === 404 ? "The app no longer knows this job." : "Could not read the progress just now; trying again.");
      }
    };
    const timer = setInterval(tick, POLL_MS);
    return () => {
      stop = true;
      clearInterval(timer);
    };
  }, [id]);

  const clear = useCallback(() => {
    setJob(null);
    setError(null);
  }, []);

  return { job, error, begin: follow, clear, running: job?.status === "running" };
}

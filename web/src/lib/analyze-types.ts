/** Shared types for the analyze page client. */
import type { StageKey } from "@/lib/types";

export type StageStatus = "pending" | "loading" | "ready" | "error";

export interface StageState {
  status: StageStatus;
  /** Raw streaming text accumulated so far. Cleared once `result` arrives. */
  streamText: string;
  result: Record<string, unknown> | null;
  error: string | null;
}

export type StageMap = Record<StageKey, StageState>;

export const STAGES_ORDERED: StageKey[] = [
  "overview",
  "financials",
  "geo",
  "technical",
  "summary",
];

export const STAGE_LABELS: Record<StageKey, string> = {
  overview:   "OVERVIEW",
  financials: "FINANCIALS",
  geo:        "GEO",
  technical:  "TECH",
  summary:    "SUMMARY",
};

export const STAGE_LONG: Record<StageKey, string> = {
  overview:   "Overview",
  financials: "Financials",
  geo:        "Geopolitical risk",
  technical:  "Technical analysis",
  summary:    "Summary",
};

export function emptyStageMap(): StageMap {
  return Object.fromEntries(
    STAGES_ORDERED.map((k) => [
      k,
      { status: "pending", streamText: "", result: null, error: null } as StageState,
    ]),
  ) as StageMap;
}

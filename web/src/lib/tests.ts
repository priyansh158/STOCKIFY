/**
 * Calls for the strategy-test pages (/api/strategies, /api/tests…). With NEXT_PUBLIC_STOCKIFY_FIXTURES=1 they
 * answer from typed fixtures instead, so the pages can be built and checked without the API.
 */
import { api } from "./api";
import * as F from "./testFixtures";
import type {
  Strategy,
  TestCharge,
  TestCreated,
  TestEquity,
  TestFill,
  TestLogs,
  TestRun,
  TestRunDetail,
} from "./types";

const FIXTURES = process.env.NEXT_PUBLIC_STOCKIFY_FIXTURES === "1";

export function listStrategies(): Promise<Strategy[]> {
  return FIXTURES ? Promise.resolve(F.strategies) : api<Strategy[]>("/api/strategies");
}

export function listTests(limit = 50): Promise<TestRun[]> {
  return FIXTURES ? Promise.resolve(F.runs) : api<TestRun[]>(`/api/tests?limit=${limit}`);
}

export function getTest(id: string): Promise<TestRunDetail> {
  if (FIXTURES) return Promise.resolve(F.detail(id));
  return api<TestRunDetail>(`/api/tests/${encodeURIComponent(id)}`);
}

export function getFills(id: string, offset = 0, limit = 200): Promise<TestFill[]> {
  if (FIXTURES) return Promise.resolve(F.fills.slice(offset, offset + limit));
  return api<TestFill[]>(`/api/tests/${encodeURIComponent(id)}/fills?offset=${offset}&limit=${limit}`);
}

export function getEquity(id: string): Promise<TestEquity[]> {
  return FIXTURES ? Promise.resolve(F.equity) : api<TestEquity[]>(`/api/tests/${encodeURIComponent(id)}/equity`);
}

export function getCharges(id: string): Promise<TestCharge[]> {
  return FIXTURES ? Promise.resolve(F.charges) : api<TestCharge[]>(`/api/tests/${encodeURIComponent(id)}/charges`);
}

export function getLogs(id: string, limit = 40): Promise<TestLogs> {
  if (FIXTURES) return Promise.resolve(F.logs);
  return api<TestLogs>(`/api/tests/${encodeURIComponent(id)}/logs?tail=${limit}`);
}

export function createTest(config: Record<string, unknown>): Promise<TestCreated> {
  if (FIXTURES) return Promise.resolve({ run_id: F.runs[1].run_id });
  return api<TestCreated>("/api/tests", { method: "POST", body: JSON.stringify(config) });
}

export function cancelTest(id: string): Promise<TestRun> {
  if (FIXTURES) return Promise.resolve({ ...F.runs[1], status: "cancelled" });
  return api<TestRun>(`/api/tests/${encodeURIComponent(id)}/cancel`, { method: "POST" });
}

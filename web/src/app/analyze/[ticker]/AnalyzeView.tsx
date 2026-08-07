"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import { openAnalysisStream } from "@/lib/sse";
import { useTicks } from "@/lib/useTicks";
import type { StageKey } from "@/lib/types";
import {
  STAGES_ORDERED,
  STAGE_LABELS,
  STAGE_LONG,
  emptyStageMap,
  type StageMap,
  type StageState,
} from "@/lib/analyze-types";
import { StageTabs } from "@/components/StageTabs";
import { StagePane } from "@/components/StagePane";
import {
  OverviewView,
  FinancialsView,
  GeoView,
  TechnicalView,
  SummaryView,
} from "@/components/stage-views";

interface AnalyzeStartResponse {
  analysis_id: string;
  ticker: string;
  company_name: string;
  sector: string;
  stages: StageKey[];
  instrument_token: number;
}

export function AnalyzeView({ ticker }: { ticker: string }) {
  const [analysisId, setAnalysisId] = useState<string | null>(null);
  const [companyName, setCompanyName] = useState<string>(ticker);
  const [instrumentToken, setInstrumentToken] = useState<number>(0);
  const [bootError, setBootError] = useState<string | null>(null);
  const [stages, setStages] = useState<StageMap>(emptyStageMap());
  const [active, setActive] = useState<StageKey>("overview");

  // Live-tick subscription for the LTP chip in the header. Empty token list
  // when we don't have a Kite token (yfinance fallback) — useTicks then no-ops.
  const tickTokens = useMemo(
    () => (instrumentToken ? [instrumentToken] : []),
    [instrumentToken],
  );
  const ticks = useTicks(tickTokens);
  const liveTick = instrumentToken
    ? ticks?.[String(instrumentToken)] ?? null
    : null;

  // Track the last stage the user manually opened. We auto-advance to a stage
  // once it goes ready, but only if the user hasn't already nav'd elsewhere.
  const userPickedRef = useRef<StageKey | null>(null);

  // Cancel handles for the currently-running SSE per stage.
  const cancelRef = useRef<Record<string, (() => void) | null>>({});

  // ── Bootstrap: POST /api/analyze/{ticker} on mount ─────────────────────────
  useEffect(() => {
    let alive = true;
    setBootError(null);
    api<AnalyzeStartResponse>(`/api/analyze/${encodeURIComponent(ticker)}`, {
      method: "POST",
    })
      .then((res) => {
        if (!alive) return;
        setAnalysisId(res.analysis_id);
        setCompanyName(res.company_name || ticker);
        setInstrumentToken(res.instrument_token || 0);
      })
      .catch((e) => {
        if (!alive) return;
        setBootError(String(e));
      });
    return () => { alive = false; };
  }, [ticker]);

  // ── Stage runner ─────────────────────────────────────────────────────────
  // Runs a single stage's SSE stream. Returns a Promise that resolves on
  // terminal `done` or `error` so callers can chain stages in order.
  const runStage = useCallback(
    (id: string, stageKey: StageKey): Promise<"done" | "error"> => {
      return new Promise((resolve) => {
        // Reset the stage state to a fresh loading
        setStages((prev) => ({
          ...prev,
          [stageKey]: {
            status: "loading",
            streamText: "",
            result: null,
            error: null,
          } as StageState,
        }));

        const url = `/api/analyze/${encodeURIComponent(id)}/stage/${stageKey}/stream`;
        const cancel = openAnalysisStream({
          url,
          onEvent: (ev) => {
            if (ev.type === "delta") {
              setStages((prev) => ({
                ...prev,
                [stageKey]: { ...prev[stageKey], streamText: ev.text },
              }));
            } else if (ev.type === "done") {
              setStages((prev) => ({
                ...prev,
                [stageKey]: {
                  status: "ready",
                  streamText: "",
                  result: ev.result,
                  error: null,
                },
              }));
              cancelRef.current[stageKey] = null;
              resolve("done");
            } else if (ev.type === "error") {
              setStages((prev) => ({
                ...prev,
                [stageKey]: {
                  status: "error",
                  streamText: "",
                  result: null,
                  error: ev.message,
                },
              }));
              cancelRef.current[stageKey] = null;
              resolve("error");
            }
          },
          onClose: (reason) => {
            if (reason !== "complete") {
              cancelRef.current[stageKey] = null;
              // If the stream got cut without a terminal event, surface it.
              setStages((prev) => {
                if (prev[stageKey].status === "ready" || prev[stageKey].status === "error") {
                  return prev;
                }
                return {
                  ...prev,
                  [stageKey]: {
                    status: "error",
                    streamText: "",
                    result: null,
                    error: `Stream closed (${reason}) before a result arrived.`,
                  },
                };
              });
              resolve("error");
            }
          },
        });
        cancelRef.current[stageKey] = cancel;
      });
    },
    [],
  );

  // ── Sequential pipeline: run each stage in order, stop on first error ─────
  useEffect(() => {
    if (!analysisId) return;
    let cancelled = false;
    (async () => {
      for (const key of STAGES_ORDERED) {
        if (cancelled) return;
        const result = await runStage(analysisId, key);
        if (result === "error") return; // Pause chain; user can retry.
        // After a stage goes ready, auto-advance to it if the user hasn't
        // manually navigated elsewhere yet.
        if (userPickedRef.current === null) {
          setActive(key);
        }
      }
    })();
    return () => {
      cancelled = true;
      // Cancel any in-flight stream so we don't double-fire on remounts.
      Object.values(cancelRef.current).forEach((c) => c?.());
      cancelRef.current = {};
    };
  }, [analysisId, runStage]);

  // ── Manual stage selection ────────────────────────────────────────────────
  const onTabSelect = useCallback((k: StageKey) => {
    userPickedRef.current = k;
    setActive(k);
  }, []);

  // ── Retry a single stage ──────────────────────────────────────────────────
  const onRetry = useCallback(
    async (k: StageKey) => {
      if (!analysisId) return;
      userPickedRef.current = k;
      setActive(k);
      const result = await runStage(analysisId, k);
      // If retry succeeded and the rest of the chain is still pending, kick
      // off the remainder so the user doesn't have to retry each one.
      if (result === "done") {
        const idx = STAGES_ORDERED.indexOf(k);
        for (const next of STAGES_ORDERED.slice(idx + 1)) {
          // Skip any already-ready stage (idempotency: retry should only
          // resume forward progress, not re-do completed work).
          const cur = stages[next];
          if (cur?.status === "ready") continue;
          const r = await runStage(analysisId, next);
          if (r === "error") return;
        }
      }
    },
    [analysisId, runStage, stages],
  );

  // ── Render ────────────────────────────────────────────────────────────────
  if (bootError) {
    return (
      <div className="mx-auto max-w-2xl px-4 py-12">
        <Link
          href="/"
          className="stk-mono-tight"
          style={{ color: "var(--color-fg-muted)" }}
        >
          ← Search
        </Link>
        <h1
          className="stk-mono mt-4 text-xl"
          style={{ color: "var(--color-red-soft)" }}
        >
          × Couldn&apos;t start analysis
        </h1>
        <pre
          className="mt-3 whitespace-pre-wrap rounded-md border-l-4 p-4 text-sm"
          style={{
            borderLeftColor: "var(--color-red)",
            background: "rgba(255,68,68,0.06)",
            color: "#ffdada",
          }}
        >
          {bootError}
        </pre>
      </div>
    );
  }

  if (!analysisId) {
    return (
      <div className="mx-auto max-w-2xl px-4 py-12">
        <div
          className="stk-mono-tight"
          style={{ color: "var(--color-green)" }}
        >
          <span className="inline-block animate-pulse">●</span>&nbsp; STOCKIFYING …
        </div>
      </div>
    );
  }

  const activeStage = stages[active];

  return (
    <div className="mx-auto max-w-3xl px-3 sm:px-5 py-5 sm:py-8">
      <div className="flex items-center justify-between mb-4 gap-3">
        <Link
          href="/"
          className="stk-mono-tight shrink-0"
          style={{ color: "var(--color-fg-muted)" }}
        >
          ← Search
        </Link>
        <div className="flex items-baseline gap-3 min-w-0">
          {liveTick && (
            <span
              className="stk-mono text-sm"
              style={{
                color:
                  (liveTick.change_pct ?? 0) >= 0
                    ? "var(--color-green)"
                    : "var(--color-red-soft)",
              }}
              aria-live="polite"
            >
              ₹{liveTick.ltp.toFixed(2)}
              {liveTick.change_pct !== null &&
                liveTick.change_pct !== undefined && (
                  <span className="ml-1 text-xs">
                    {(liveTick.change_pct ?? 0) >= 0 ? "▲" : "▼"}
                    {Math.abs(liveTick.change_pct ?? 0).toFixed(2)}%
                  </span>
                )}
            </span>
          )}
          <div
            className="stk-mono-tight"
            style={{ color: "var(--color-fg-muted)" }}
          >
            {ticker} &middot; NSE
          </div>
        </div>
      </div>

      <StageTabs stages={stages} active={active} onSelect={onTabSelect} />

      <div className="mt-6">
        <StagePane
          stageKey={active}
          state={activeStage}
          onRetry={() => onRetry(active)}
          renderReady={(result) => {
            switch (active) {
              case "overview":
                return <OverviewView result={result} ticker={ticker} />;
              case "financials":
                return <FinancialsView result={result} company={companyName} />;
              case "geo":
                return <GeoView result={result} company={companyName} />;
              case "technical":
                return <TechnicalView result={result} company={companyName} />;
              case "summary":
                return <SummaryView result={result} company={companyName} />;
              default:
                return null;
            }
          }}
        />
      </div>

      <div className="mt-10">
        <PipelineProgress stages={stages} />
      </div>
    </div>
  );
}

function PipelineProgress({ stages }: { stages: StageMap }) {
  return (
    <div
      className="stk-mono-tight flex flex-wrap gap-2"
      style={{ color: "var(--color-fg-muted)" }}
    >
      {STAGES_ORDERED.map((k) => {
        const s = stages[k];
        const color =
          s.status === "ready"   ? "var(--color-green)" :
          s.status === "loading" ? "var(--color-amber)" :
          s.status === "error"   ? "var(--color-red-soft)" :
          "var(--color-fg-muted)";
        return (
          <span key={k} style={{ color }}>
            {STAGE_LABELS[k]}: {s.status}
          </span>
        );
      })}
    </div>
  );
  // `STAGE_LONG` is imported but unused here — referenced for tooling.
  void STAGE_LONG;
}

"use client";

import { useEffect, useRef } from "react";
import type { StageKey } from "@/lib/types";
import { STAGE_LONG, type StageState } from "@/lib/analyze-types";

interface Props {
  stageKey: StageKey;
  state: StageState;
  onRetry: () => void;
}

/**
 * Container for one stage's render. Handles four sub-states:
 *   pending  → "queued" placeholder
 *   loading  → live streaming text in a monospace box
 *   ready    → delegates to a children renderer (passed by parent)
 *   error    → friendly error + Retry button
 *
 * The structured ready-state renderer is passed by the parent page so each
 * stage can format its own dict shape. This component only owns the framing.
 */
export function StagePane({
  stageKey,
  state,
  onRetry,
  renderReady,
}: Props & { renderReady: (result: Record<string, unknown>) => React.ReactNode }) {
  const streamRef = useRef<HTMLPreElement | null>(null);

  // Auto-scroll the streaming box as text arrives.
  useEffect(() => {
    if (state.status === "loading" && streamRef.current) {
      streamRef.current.scrollTop = streamRef.current.scrollHeight;
    }
  }, [state.status, state.streamText]);

  if (state.status === "pending") {
    return (
      <div
        className="stk-mono-tight rounded-lg border px-6 py-10 text-center"
        style={{
          borderColor: "rgba(0,255,65,0.20)",
          color: "var(--color-fg-muted)",
        }}
      >
        {STAGE_LONG[stageKey]} — queued.
      </div>
    );
  }

  if (state.status === "loading") {
    return (
      <div className="flex flex-col gap-3">
        <div
          className="stk-mono-tight"
          style={{ color: "var(--color-green)" }}
        >
          <span className="inline-block animate-pulse">●</span>
          &nbsp;&nbsp; streaming {STAGE_LONG[stageKey].toLowerCase()}…
        </div>
        <pre
          ref={streamRef}
          className="max-h-[60vh] overflow-auto rounded-lg border p-4 text-xs whitespace-pre-wrap break-words"
          style={{
            borderColor: "rgba(0,255,65,0.35)",
            background: "rgba(0,0,0,0.55)",
            color: "var(--color-green-soft)",
            fontFamily: "var(--font-mono)",
            lineHeight: 1.55,
            boxShadow: "0 0 16px rgba(0,255,65,0.10)",
          }}
        >
          {state.streamText || "(waiting for first token…)"}
        </pre>
      </div>
    );
  }

  if (state.status === "error") {
    return (
      <div className="flex flex-col gap-4">
        <div
          className="stk-mono-tight"
          style={{ color: "var(--color-red-soft)" }}
        >
          × {STAGE_LONG[stageKey]} failed
        </div>
        <div
          className="rounded-md border-l-4 p-4"
          style={{
            borderLeftColor: "var(--color-red)",
            background: "rgba(255,68,68,0.06)",
            color: "#ffdada",
            lineHeight: 1.55,
          }}
        >
          {state.error ?? "Stage failed without a specific error message."}
        </div>
        <div className="flex gap-3">
          <button
            type="button"
            onClick={onRetry}
            className="stk-mono-tight rounded-md border px-4 py-2 text-sm"
            style={{
              borderColor: "var(--color-green)",
              background: "rgba(0,255,65,0.10)",
              color: "var(--color-green)",
            }}
          >
            ↻ Retry {STAGE_LONG[stageKey]}
          </button>
        </div>
      </div>
    );
  }

  // ready
  if (state.result) return <>{renderReady(state.result)}</>;
  return null;
}

"use client";

import type { StageKey } from "@/lib/types";
import {
  STAGES_ORDERED,
  STAGE_LABELS,
  type StageMap,
} from "@/lib/analyze-types";

const glyph: Record<string, string> = {
  pending: "○",
  loading: "◐",
  ready: "●",
  error: "×",
};

export function StageTabs({
  stages,
  active,
  onSelect,
}: {
  stages: StageMap;
  active: StageKey;
  onSelect: (key: StageKey) => void;
}) {
  return (
    <div className="flex gap-2 overflow-x-auto pb-2">
      {STAGES_ORDERED.map((key) => {
        const s = stages[key];
        const isActive = key === active;
        // Clickable when ready OR error — error tabs need to be visitable so
        // the user can read the message + retry. Loading/pending are locked.
        const clickable = !isActive && (s.status === "ready" || s.status === "error");
        const label = isActive
          ? `▸ ${STAGE_LABELS[key]}`
          : `${glyph[s.status]}  ${STAGE_LABELS[key]}`;
        return (
          <button
            key={key}
            type="button"
            disabled={!clickable}
            onClick={() => clickable && onSelect(key)}
            className="stk-mono-tight rounded-md border px-3 py-2 text-sm shrink-0 transition-colors disabled:cursor-not-allowed disabled:opacity-60"
            style={{
              borderColor: isActive
                ? "var(--color-green)"
                : "rgba(0,255,65,0.25)",
              background: isActive
                ? "rgba(0,255,65,0.12)"
                : s.status === "error"
                ? "rgba(255,68,68,0.08)"
                : "transparent",
              color: isActive
                ? "var(--color-green)"
                : s.status === "error"
                ? "var(--color-red-soft)"
                : "var(--color-fg)",
            }}
          >
            {label}
          </button>
        );
      })}
    </div>
  );
}

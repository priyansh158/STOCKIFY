import Link from "next/link";
import { api } from "@/lib/api";
import type { HistoryListEntry } from "@/lib/types";

export const dynamic = "force-dynamic";

function formatWhen(unixSeconds: number): string {
  if (!unixSeconds) return "—";
  const d = new Date(unixSeconds * 1000);
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function verdictTone(verdict: string): { fg: string; bg: string; border: string } {
  const v = verdict.toUpperCase();
  if (v.startsWith("BUY")) {
    return { fg: "var(--color-green-soft)", bg: "rgba(0,255,65,0.08)", border: "var(--color-green)" };
  }
  if (v.includes("AVOID")) {
    return { fg: "var(--color-red-soft)", bg: "rgba(255,68,68,0.08)", border: "var(--color-red)" };
  }
  return { fg: "var(--color-amber)", bg: "rgba(255,170,64,0.08)", border: "var(--color-amber)" };
}

async function fetchHistory(): Promise<HistoryListEntry[]> {
  try {
    return await api<HistoryListEntry[]>("/api/history?limit=100");
  } catch {
    return [];
  }
}

export default async function HistoryPage() {
  const items = await fetchHistory();

  return (
    <div className="mx-auto max-w-2xl px-4 py-8 sm:py-12">
      <div className="flex items-center justify-between mb-6">
        <Link
          href="/"
          className="stk-mono-tight"
          style={{ color: "var(--color-fg-muted)" }}
        >
          ← Search
        </Link>
        <div
          className="stk-mono-tight"
          style={{ color: "var(--color-fg-muted)" }}
        >
          {items.length} record{items.length === 1 ? "" : "s"}
        </div>
      </div>
      <h1
        className="stk-mono text-2xl sm:text-3xl font-bold mb-6"
        style={{ color: "var(--color-green)" }}
      >
        History
      </h1>

      {items.length === 0 ? (
        <div
          className="stk-mono-tight rounded-lg border px-6 py-12 text-center"
          style={{
            borderColor: "rgba(0,255,65,0.20)",
            color: "var(--color-fg-muted)",
          }}
        >
          No saved analyses yet.
          <br />
          Run an analysis from the search page to populate history.
        </div>
      ) : (
        <ul className="flex flex-col gap-3">
          {items.map((item) => {
            const tone = verdictTone(item.verdict);
            return (
              <li key={item.id}>
                <Link
                  href={`/history/${item.id}`}
                  className="block rounded-lg border p-4 transition-colors hover:opacity-90"
                  style={{
                    borderColor: "rgba(0,255,65,0.20)",
                    background: "rgba(255,255,255,0.025)",
                  }}
                >
                  <div className="flex items-baseline justify-between gap-3">
                    <div className="flex-1 min-w-0">
                      <div
                        className="stk-mono truncate"
                        style={{ color: "var(--color-green)", fontSize: "1.05rem" }}
                      >
                        {item.ticker}
                        <span
                          className="ml-2"
                          style={{ color: "var(--color-fg)" }}
                        >
                          {item.company_name}
                        </span>
                      </div>
                      <div
                        className="stk-mono-tight mt-1"
                        style={{ color: "var(--color-fg-muted)" }}
                      >
                        {formatWhen(item.created_at)}
                        {item.sector && ` · ${item.sector}`}
                      </div>
                    </div>
                    <span
                      className="stk-mono-tight shrink-0 rounded px-2 py-1"
                      style={{
                        border: `1px solid ${tone.border}`,
                        background: tone.bg,
                        color: tone.fg,
                      }}
                    >
                      {item.verdict || "—"}
                    </span>
                  </div>
                  {item.bottom_line && (
                    <p
                      className="mt-3 text-sm leading-relaxed line-clamp-3"
                      style={{ color: "var(--color-fg)" }}
                    >
                      {item.bottom_line}
                    </p>
                  )}
                </Link>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

import Link from "next/link";
import { api } from "@/lib/api";
import type { GeoAlert } from "@/lib/types";

export const dynamic = "force-dynamic";

function formatWhen(unixSeconds: number): string {
  if (!unixSeconds) return "—";
  return new Date(unixSeconds * 1000).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function severityTone(severity: string): { fg: string; bg: string; border: string } {
  const s = severity.toUpperCase();
  if (s === "CRITICAL") {
    return { fg: "var(--color-red-soft)", bg: "rgba(255,68,68,0.12)", border: "var(--color-red)" };
  }
  if (s === "HIGH") {
    return { fg: "var(--color-amber)", bg: "rgba(255,170,64,0.10)", border: "var(--color-amber)" };
  }
  return { fg: "var(--color-fg-muted)", bg: "rgba(255,255,255,0.04)", border: "rgba(0,255,65,0.20)" };
}

async function fetchAlerts(): Promise<GeoAlert[]> {
  try {
    return await api<GeoAlert[]>("/api/alerts?limit=100&min_severity=MEDIUM");
  } catch {
    return [];
  }
}

export default async function AlertsPage() {
  const items = await fetchAlerts();

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
        <div className="stk-mono-tight" style={{ color: "var(--color-fg-muted)" }}>
          {items.length} alert{items.length === 1 ? "" : "s"}
        </div>
      </div>

      <h1
        className="stk-mono text-2xl sm:text-3xl font-bold mb-2"
        style={{ color: "var(--color-green)" }}
      >
        Geo alerts
      </h1>
      <p className="mb-6 text-sm" style={{ color: "var(--color-fg-muted)" }}>
        From the always-on monitor (<code className="stk-mono-tight">python -m geo_monitor</code>).
        Email fires on HIGH / CRITICAL.
      </p>

      {items.length === 0 ? (
        <div
          className="stk-mono-tight rounded-lg border px-6 py-12 text-center"
          style={{
            borderColor: "rgba(0,255,65,0.20)",
            color: "var(--color-fg-muted)",
          }}
        >
          No alerts yet.
          <br />
          Run the monitor with Gemini + email configured in{" "}
          <code>.env</code>.
        </div>
      ) : (
        <ul className="flex flex-col gap-3">
          {items.map((item) => {
            const tone = severityTone(item.severity);
            return (
              <li
                key={item.id}
                className="rounded-lg border px-4 py-3"
                style={{
                  borderColor: tone.border,
                  background: tone.bg,
                }}
              >
                <div className="flex flex-wrap items-center gap-2 mb-1">
                  <span
                    className="stk-mono-tight text-xs font-bold px-2 py-0.5 rounded"
                    style={{ color: tone.fg, border: `1px solid ${tone.border}` }}
                  >
                    {item.severity} · {item.disruption_score}
                  </span>
                  <span className="stk-mono-tight text-xs" style={{ color: "var(--color-fg-muted)" }}>
                    {formatWhen(item.created_at)}
                  </span>
                  {item.emailed_at ? (
                    <span className="stk-mono-tight text-xs" style={{ color: "var(--color-fg-muted)" }}>
                      emailed
                    </span>
                  ) : null}
                </div>
                <div className="font-medium" style={{ color: "var(--color-fg)" }}>
                  {item.url ? (
                    <a href={item.url} target="_blank" rel="noreferrer" className="hover:underline">
                      {item.title}
                    </a>
                  ) : (
                    item.title
                  )}
                </div>
                {item.affected_tickers?.length > 0 && (
                  <div className="stk-mono-tight mt-1 text-xs" style={{ color: "var(--color-green-soft)" }}>
                    {item.affected_tickers.join(" · ")}
                  </div>
                )}
                {item.why_disruptive && (
                  <p className="mt-2 text-sm" style={{ color: "var(--color-fg-muted)" }}>
                    {item.why_disruptive}
                  </p>
                )}
                {item.investor_action && (
                  <p className="mt-1 text-sm" style={{ color: "var(--color-fg)" }}>
                    {item.investor_action}
                  </p>
                )}
                <div className="stk-mono-tight mt-2 text-xs" style={{ color: "var(--color-fg-muted)" }}>
                  {item.source || "—"}
                  {item.scope ? ` · ${item.scope}` : ""}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

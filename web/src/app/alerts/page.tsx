import { api } from "@/lib/api";
import type { GeoAlert } from "@/lib/types";
import { Chip, PageHead, Panel, Table } from "@/components/ui";

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

function severityTone(severity: string): "down" | "warn" | undefined {
  const s = severity.toUpperCase();
  if (s === "CRITICAL") return "down";
  if (s === "HIGH") return "warn";
  return undefined;
}

/** "CRITICAL" becomes "Critical". */
function severityWord(severity: string): string {
  const s = severity.toLowerCase();
  return s.charAt(0).toUpperCase() + s.slice(1);
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
    <div className="stk-page">
      <PageHead
        title="Geo alerts"
        lede={
          <>
            From the always-on monitor (<code>python -m geo_monitor</code>). Email fires on High and Critical alerts.
            {items.length > 0 && ` ${items.length} shown.`}
          </>
        }
      />

      {items.length === 0 ? (
        <Panel>
          <p className="stk-empty">
            No alerts yet.
            <br />
            Run the monitor with Gemini + email configured in <code>.env</code>.
          </p>
        </Panel>
      ) : (
        <Table
          caption="Geo alerts, newest first"
          head={[
            { label: "Severity" },
            { label: "When" },
            { label: "Alert" },
            { label: "Tickers affected" },
            { label: "Source" },
          ]}
        >
          {items.map((item) => (
            <tr key={item.id}>
              <td>
                <Chip tone={severityTone(item.severity)}>{severityWord(item.severity)}</Chip>
                <div className="stk-label">Score {item.disruption_score}</div>
              </td>
              <td className="stk-label" data-wrap>
                {formatWhen(item.created_at)}
                {item.emailed_at ? <div>Emailed</div> : null}
              </td>
              <td data-wrap>
                <div className="stk-alert-title">
                  {item.url ? (
                    <a href={item.url} target="_blank" rel="noreferrer">
                      {item.title}
                    </a>
                  ) : (
                    item.title
                  )}
                </div>
                {item.why_disruptive && <p className="stk-label">{item.why_disruptive}</p>}
                {item.investor_action && <p>{item.investor_action}</p>}
              </td>
              <td className="stk-mono" data-wrap>
                {item.affected_tickers?.length > 0 ? item.affected_tickers.join(", ") : "—"}
              </td>
              <td className="stk-label" data-wrap>
                {item.source || "—"}
                {item.scope ? <div>{item.scope}</div> : null}
              </td>
            </tr>
          ))}
        </Table>
      )}
    </div>
  );
}

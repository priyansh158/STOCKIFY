import { api } from "@/lib/api";
import { istWhenEpoch } from "@/lib/format";
import { lastCheckWords } from "@/lib/monitor";
import type { GeoAlert, GeoMonitorStatus } from "@/lib/types";
import CheckNews from "./CheckNews";
import { Chip, PageHead, Panel, Table } from "@/components/ui";

export const dynamic = "force-dynamic";

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

/** What an alert is about: the monitor writes macro, holding or both. Anything else is shown as written. */
function scopeWords(scope: string): string {
  if (scope === "macro") return "The whole market";
  if (scope === "holding") return "Your holdings";
  if (scope === "both") return "The market and your holdings";
  return scope;
}

async function fetchAlerts(): Promise<{ items: GeoAlert[]; failed: boolean }> {
  try {
    return { items: await api<GeoAlert[]>("/api/alerts?limit=100&min_severity=MEDIUM"), failed: false };
  } catch {
    return { items: [], failed: true };
  }
}

async function fetchMonitor(): Promise<GeoMonitorStatus | null> {
  try {
    return await api<GeoMonitorStatus>("/api/alerts/monitor");
  } catch {
    return null;
  }
}

export default async function AlertsPage() {
  const [{ items, failed }, monitor] = await Promise.all([fetchAlerts(), fetchMonitor()]);

  return (
    <div className="stk-page">
      <PageHead
        title="Geo alerts"
        lede={
          <>
            The always-on news monitor reads Google News for world events that could
            disrupt Indian stocks. It emails you High and Critical alerts.
            {items.length > 0 && ` ${items.length} shown.`}
          </>
        }
      />

      {!failed && (
        <div className="stk-stack-s" style={{ marginBottom: "var(--space-lg)" }}>
          <CheckNews checkingElsewhere={Boolean(monitor?.checking)} />
          <p className="stk-fine">{lastCheckWords(monitor, istWhenEpoch(monitor?.last_check_at))}</p>
        </div>
      )}

      {failed ? (
        <Panel tone="red">
          <p role="alert">Could not reach the STOCKIFY API, so the alerts cannot be shown. Check that the app is running, then reload this page.</p>
        </Panel>
      ) : items.length === 0 ? (
        <Panel>
          <p className="stk-empty">
            No alerts yet.
            <br />
            Press Check the news now to look for world events straight away. The background monitor makes the same
            check on a timer once it is turned on. Both need Gemini and email set up in the settings file (.env).
          </p>
        </Panel>
      ) : (
        <Table
          caption="Geo alerts, newest first"
          head={[
            { label: "Severity" },
            { label: "When (IST)" },
            { label: "Alert" },
            { label: "Tickers affected" },
            { label: "News source" },
          ]}
        >
          {items.map((item) => (
            <tr key={item.id}>
              <td>
                <Chip tone={severityTone(item.severity)}>{severityWord(item.severity)}</Chip>
                <div className="stk-label">Disruption score {item.disruption_score} of 100</div>
              </td>
              <td className="stk-label" data-wrap>
                {istWhenEpoch(item.created_at)}
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
                {item.scope ? <div>About: {scopeWords(item.scope)}</div> : null}
              </td>
            </tr>
          ))}
        </Table>
      )}
    </div>
  );
}

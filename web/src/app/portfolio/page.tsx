import { api } from "@/lib/api";
import { rupeesFloat } from "@/lib/format";
import type { PortfolioSnapshot, KiteStatus } from "@/lib/types";
import { PageHead, Panel, Stat, StatGrid } from "@/components/ui";
import { PortfolioActions } from "./PortfolioActions";
import { HoldingsList } from "./HoldingsList";

export const dynamic = "force-dynamic";

async function loadData(): Promise<{ status: KiteStatus; snap: PortfolioSnapshot | null }> {
  const status = await api<KiteStatus>("/api/kite/status").catch(() => ({
    configured: false,
    authenticated: false,
    daemon_waiting_for_login: false,
    error: "unreachable",
  }));
  let snap: PortfolioSnapshot | null = null;
  if (status.authenticated) {
    snap = await api<PortfolioSnapshot>("/api/portfolio/snapshot").catch(() => null);
  }
  return { status, snap };
}

export default async function PortfolioPage() {
  const { status, snap } = await loadData();

  return (
    <div className="stk-page">
      <PageHead
        title="Portfolio"
        lede="Read-only Zerodha holdings. Prices update live when the ticker is connected."
        actions={<PortfolioActions authenticated={status.authenticated} />}
      />

      <div className="stk-stack">
        {!status.configured && (
          <Panel tone="amber">
            <p>
              Kite Connect isn&apos;t configured. Set <code>KITE_API_KEY</code> and <code>KITE_API_SECRET</code> in{" "}
              <code>.env</code> and restart the server.
            </p>
          </Panel>
        )}

        {status.configured && !status.authenticated && (
          <Panel>
            <div className="stk-stack-s">
              <h2>Not logged in to Kite</h2>
              <p className="stk-lede">
                Kite Connect access tokens expire daily at ~06:00 IST. Log in through Zerodha; you&apos;ll be
                redirected back here when done.
              </p>
              <div>
                <PortfolioActions authenticated={false} compact={false} primary />
              </div>
            </div>
          </Panel>
        )}

        {status.authenticated && snap && (
          <>
            {snap.error && (
              <Panel tone="red">
                <p role="alert">{snap.error}</p>
              </Panel>
            )}

            <Panel>
              <StatGrid cols={2}>
                <Stat label="Total value" value={rupeesFloat(snap.total_value, true)} />
                <Stat
                  label="Total P&L"
                  value={rupeesFloat(snap.total_pnl, true)}
                  tone={snap.total_pnl >= 0 ? "up" : "down"}
                />
              </StatGrid>
            </Panel>

            <HoldingsList initial={snap.holdings} />
          </>
        )}
      </div>
    </div>
  );
}

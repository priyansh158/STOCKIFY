import { api } from "@/lib/api";
import { rupeesFloat } from "@/lib/format";
import type { DeskListDTO, PortfolioSnapshot, KiteStatus } from "@/lib/types";
import { PageHead, Panel, Stat, StatGrid } from "@/components/ui";
import { PortfolioActions } from "./PortfolioActions";
import { DeskLastViews, HoldingsList } from "./HoldingsList";

export const dynamic = "force-dynamic";

async function loadData(): Promise<{ status: KiteStatus | null; snap: PortfolioSnapshot | null; desk: DeskListDTO | null }> {
  // status is null when the API itself could not be reached: that is not the same as "Kite is not set up".
  const status = await api<KiteStatus>("/api/kite/status").catch(() => null);
  let snap: PortfolioSnapshot | null = null;
  if (status?.authenticated) {
    snap = await api<PortfolioSnapshot>("/api/portfolio/snapshot").catch(() => null);
  }
  // the desk's verdict per holding; logged out, its last closing verdicts. Null when it cannot be read.
  const desk = status ? await api<DeskListDTO>("/api/desk").catch(() => null) : null;
  return { status, snap, desk };
}

export default async function PortfolioPage() {
  const { status, snap, desk } = await loadData();

  return (
    <div className="stk-page">
      <PageHead
        title="Portfolio"
        lede="Read-only Zerodha holdings. A price is marked Live when live prices are on."
        actions={status ? <PortfolioActions authenticated={status.authenticated} /> : undefined}
      />

      <div className="stk-stack">
        {!status && (
          <Panel tone="red">
            <p role="alert">
              Could not reach the STOCKIFY API, so nothing can be shown here. Check that the app is running, then reload
              this page.
            </p>
          </Panel>
        )}

        {status && !status.configured && (
          <Panel tone="amber">
            <p>
              Kite Connect isn&apos;t set up: the Kite API key and secret are missing from the settings file (.env). Add
              them there and restart STOCKIFY.
            </p>
          </Panel>
        )}

        {status && status.configured && !status.authenticated && (
          <Panel>
            <div className="stk-stack-s">
              <h2>Not logged in to Kite</h2>
              <p className="stk-lede">
                Your Kite login runs out each morning; log in again after 6:00 IST. You&apos;ll be sent back here when
                you are done.
              </p>
              <div>
                <PortfolioActions authenticated={false} compact={false} primary />
              </div>
            </div>
          </Panel>
        )}

        {status && status.configured && !status.authenticated && <DeskLastViews desk={desk} />}

        {status?.authenticated && !snap && (
          <Panel tone="red">
            <p role="alert">
              Could not load your holdings. Reload the page to try again. If it keeps failing, sign out and log in to
              Kite again.
            </p>
          </Panel>
        )}

        {status?.authenticated && snap && (
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
                  label="Profit or loss"
                  value={rupeesFloat(snap.total_pnl, true)}
                  tone={snap.total_pnl >= 0 ? "up" : "down"}
                />
              </StatGrid>
            </Panel>

            <HoldingsList initial={snap.holdings} desk={desk} />
          </>
        )}
      </div>
    </div>
  );
}

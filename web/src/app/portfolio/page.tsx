import Link from "next/link";
import { api } from "@/lib/api";
import type { PortfolioSnapshot, KiteStatus } from "@/lib/types";
import { PortfolioActions } from "./PortfolioActions";
import { HoldingsList } from "./HoldingsList";

export const dynamic = "force-dynamic";

function fmtInr(n: number): string {
  if (!Number.isFinite(n)) return "—";
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 0,
  }).format(n);
}

function fmtPct(p: number): string {
  if (!Number.isFinite(p)) return "—";
  const sign = p > 0 ? "+" : "";
  return `${sign}${p.toFixed(2)}%`;
}

async function loadData(): Promise<{ status: KiteStatus; snap: PortfolioSnapshot | null }> {
  const status = await api<KiteStatus>("/api/kite/status").catch(() => ({
    configured: false,
    authenticated: false,
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
    <div className="mx-auto max-w-2xl px-4 py-8 sm:py-12">
      <div className="flex items-center justify-between mb-6">
        <Link
          href="/"
          className="stk-mono-tight"
          style={{ color: "var(--color-fg-muted)" }}
        >
          ← Search
        </Link>
        <PortfolioActions authenticated={status.authenticated} />
      </div>

      <h1
        className="stk-mono text-2xl sm:text-3xl font-bold mb-2"
        style={{ color: "var(--color-green)" }}
      >
        Portfolio
      </h1>
      <p
        className="stk-mono-tight mb-8"
        style={{ color: "var(--color-fg-muted)" }}
      >
        Read-only Zerodha holdings · Live LTP coming in P5
      </p>

      {!status.configured && (
        <div
          className="rounded-lg border-l-4 px-5 py-4 mb-6"
          style={{
            borderLeftColor: "var(--color-amber)",
            background: "rgba(255,170,64,0.06)",
            color: "var(--color-amber)",
          }}
        >
          Kite Connect isn&apos;t configured. Set <code className="stk-mono">KITE_API_KEY</code> and{" "}
          <code className="stk-mono">KITE_API_SECRET</code> in <code className="stk-mono">.env</code> and restart the server.
        </div>
      )}

      {status.configured && !status.authenticated && (
        <div
          className="rounded-lg border px-5 py-6"
          style={{
            borderColor: "rgba(0,255,65,0.20)",
            background: "rgba(0,255,65,0.04)",
          }}
        >
          <div
            className="stk-mono-tight mb-3"
            style={{ color: "var(--color-green)" }}
          >
            Not logged in to Kite
          </div>
          <p className="text-sm mb-4" style={{ color: "var(--color-fg)" }}>
            Kite Connect access tokens expire daily at ~06:00 IST. Tap below to
            authenticate via Zerodha — you&apos;ll be redirected back here when done.
          </p>
          <PortfolioActions authenticated={false} compact={false} primary />
        </div>
      )}

      {status.authenticated && snap && (
        <>
          {snap.error && (
            <div
              className="rounded-md border-l-4 px-4 py-3 mb-6 text-sm"
              style={{
                borderLeftColor: "var(--color-red)",
                background: "rgba(255,68,68,0.06)",
                color: "#ffdada",
              }}
            >
              {snap.error}
            </div>
          )}

          <div
            className="grid grid-cols-2 gap-3 mb-8 rounded-lg border p-4"
            style={{
              borderColor: "rgba(0,255,65,0.20)",
              background: "rgba(255,255,255,0.025)",
            }}
          >
            <div>
              <div
                className="stk-mono-tight"
                style={{ color: "var(--color-fg-muted)" }}
              >
                Total value
              </div>
              <div
                className="stk-mono text-xl mt-1"
                style={{ color: "var(--color-fg)" }}
              >
                {fmtInr(snap.total_value)}
              </div>
            </div>
            <div>
              <div
                className="stk-mono-tight"
                style={{ color: "var(--color-fg-muted)" }}
              >
                Total P&amp;L
              </div>
              <div
                className="stk-mono text-xl mt-1"
                style={{
                  color: snap.total_pnl >= 0
                    ? "var(--color-green)"
                    : "var(--color-red-soft)",
                }}
              >
                {fmtInr(snap.total_pnl)}
              </div>
            </div>
          </div>

          <HoldingsList initial={snap.holdings} />
        </>
      )}
    </div>
  );
}

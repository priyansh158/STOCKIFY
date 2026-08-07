"use client";

import Link from "next/link";
import { useMemo } from "react";
import { useTicks } from "@/lib/useTicks";
import type { Holding } from "@/lib/types";

function fmtInr(n: number): string {
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

interface Props {
  initial: Holding[];
}

/**
 * Client wrapper that subscribes to /api/ws/ticks for every holding's
 * instrument_token and updates LTP / P&L in real time. When the WS isn't
 * connected (Kite not authed, server down), we fall back to the static
 * `last_price` baked into the initial snapshot.
 */
export function HoldingsList({ initial }: Props) {
  const tokens = useMemo(
    () =>
      initial
        .map((h) => h.instrument_token ?? 0)
        .filter((t): t is number => !!t),
    [initial],
  );
  const ticks = useTicks(tokens);

  if (initial.length === 0) {
    return (
      <div
        className="stk-mono-tight rounded-lg border px-6 py-12 text-center"
        style={{
          borderColor: "rgba(0,255,65,0.20)",
          color: "var(--color-fg-muted)",
        }}
      >
        No holdings.
      </div>
    );
  }

  return (
    <ul className="flex flex-col gap-3">
      {initial.map((h) => {
        const live =
          h.instrument_token && ticks?.[String(h.instrument_token)];
        const ltp = live ? live.ltp : h.last_price;
        const currentValue = ltp * h.quantity;
        const invested = h.average_price * h.quantity;
        const pnl = currentValue - invested;
        const pnlPct = invested ? (pnl / invested) * 100 : 0;
        const pnlTone =
          pnl >= 0 ? "var(--color-green)" : "var(--color-red-soft)";

        return (
          <li key={`${h.exchange}-${h.symbol}`}>
            <div
              className="rounded-lg border p-4"
              style={{
                borderColor: "rgba(0,255,65,0.20)",
                background: "rgba(255,255,255,0.025)",
              }}
            >
              <div className="flex items-baseline justify-between gap-3">
                <Link
                  href={`/analyze/${encodeURIComponent(h.symbol)}`}
                  className="stk-mono"
                  style={{
                    color: "var(--color-green)",
                    fontSize: "1.05rem",
                  }}
                >
                  {h.symbol}
                </Link>
                <span
                  className="stk-mono-tight"
                  style={{ color: "var(--color-fg-muted)" }}
                >
                  {h.exchange} · {h.quantity} sh
                  {live && (
                    <span
                      className="ml-2"
                      style={{ color: "var(--color-green)" }}
                      title="live tick"
                    >
                      ●
                    </span>
                  )}
                </span>
              </div>
              <div className="mt-2 grid grid-cols-3 gap-3 text-sm">
                <div>
                  <div
                    className="stk-mono-tight text-xs"
                    style={{ color: "var(--color-fg-muted)" }}
                  >
                    avg
                  </div>
                  <div style={{ color: "var(--color-fg)" }}>
                    ₹{h.average_price.toFixed(2)}
                  </div>
                </div>
                <div>
                  <div
                    className="stk-mono-tight text-xs"
                    style={{ color: "var(--color-fg-muted)" }}
                  >
                    LTP
                  </div>
                  <div style={{ color: "var(--color-fg)" }}>
                    ₹{ltp.toFixed(2)}
                  </div>
                </div>
                <div>
                  <div
                    className="stk-mono-tight text-xs"
                    style={{ color: "var(--color-fg-muted)" }}
                  >
                    P&amp;L
                  </div>
                  <div style={{ color: pnlTone }}>
                    {fmtInr(pnl)}{" "}
                    <span className="text-xs">{fmtPct(pnlPct)}</span>
                  </div>
                </div>
              </div>
            </div>
          </li>
        );
      })}
    </ul>
  );
}

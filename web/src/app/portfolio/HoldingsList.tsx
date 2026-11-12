"use client";

import { useMemo } from "react";
import { rupeesFloat } from "@/lib/format";
import { useTicks } from "@/lib/useTicks";
import type { Holding } from "@/lib/types";
import { Chip, Empty, Table } from "@/components/ui";

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
    return <Empty>No holdings.</Empty>;
  }

  return (
    <Table
      caption="Holdings"
      head={[
        { label: "Symbol" },
        { label: "Exchange" },
        { label: "Shares", num: true },
        { label: "Average price", num: true },
        { label: "Last price", num: true },
        { label: "Price source" },
        { label: "Profit or loss", num: true },
        { label: "Change (%)", num: true },
      ]}
    >
      {initial.map((h) => {
        const live = h.instrument_token && ticks?.[String(h.instrument_token)];
        const ltp = live ? live.ltp : h.last_price;
        const currentValue = ltp * h.quantity;
        const invested = h.average_price * h.quantity;
        const pnl = currentValue - invested;
        const pnlPct = invested ? (pnl / invested) * 100 : 0;
        const toneClass = pnl >= 0 ? "stk-up" : "stk-down";

        return (
          <tr key={`${h.exchange}-${h.symbol}`}>
            <td className="stk-mono">{h.symbol}</td>
            <td>{h.exchange}</td>
            <td className="num">{h.quantity.toLocaleString("en-IN")}</td>
            <td className="num">{rupeesFloat(h.average_price)}</td>
            <td className="num">{rupeesFloat(ltp)}</td>
            <td>{live ? <Chip tone="up">Live</Chip> : <span className="stk-label">Last snapshot</span>}</td>
            <td className={`num ${toneClass}`}>{rupeesFloat(pnl, true)}</td>
            <td className={`num ${toneClass}`}>{fmtPct(pnlPct)}</td>
          </tr>
        );
      })}
    </Table>
  );
}

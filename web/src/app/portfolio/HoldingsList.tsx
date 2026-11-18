"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { rupeesFloat } from "@/lib/format";
import { DESK_POLL_MS, getDeskList, nsWhen, reasonWords, verdictClass } from "@/lib/desk";
import { useTicks } from "@/lib/useTicks";
import type { DeskChipDTO, DeskListDTO, Holding } from "@/lib/types";
import { Chip, Empty, Table } from "@/components/ui";

function fmtPct(p: number): string {
  if (!Number.isFinite(p)) return "—";
  const sign = p > 0 ? "+" : "";
  return `${sign}${p.toFixed(2)}%`;
}

interface Props {
  initial: Holding[];
  /** The desk's verdict per holding (GET /api/desk), or null when it could not be read. */
  desk: DeskListDTO | null;
}

/** The desk list, asked for again every minute while any verdict is live (the session is open). */
function useDesk(initial: DeskListDTO | null): DeskListDTO | null {
  const [desk, setDesk] = useState(initial);
  const live = !!desk?.items.some((c) => c.live);
  useEffect(() => {
    if (!live) return;
    const ctl = new AbortController();
    const timer = window.setInterval(() => {
      if (document.hidden) return;
      getDeskList({ signal: ctl.signal }).then(setDesk, () => undefined); // a failed refresh keeps what is shown
    }, DESK_POLL_MS);
    return () => {
      window.clearInterval(timer);
      ctl.abort();
    };
  }, [live]);
  return desk;
}

export function deskHref(symbol: string): string {
  return `/portfolio/${encodeURIComponent(symbol)}`;
}

/** A verdict as its word in its colour, linking to the holding's desk page. */
export function VerdictLink({ chip }: { chip: DeskChipDTO }) {
  return (
    <Link
      href={deskHref(chip.symbol)}
      className={`stk-link ${verdictClass(chip.verdict)}`}
      title={chip.live ? "Live" : `As of ${nsWhen(chip.as_of_ns)}`}
    >
      {chip.verdict}
    </Link>
  );
}

function Suggestion({ h, desk }: { h: Holding; desk: DeskListDTO }) {
  const sym = h.symbol.toUpperCase();
  const ex = h.exchange.toUpperCase();
  // the chip names the holding it came from (a BSE holding's NSE symbol may differ); the NSE symbol is the fallback
  const chip =
    desk.items.find((c) => c.holding_symbol?.toUpperCase() === sym && (c.holding_exchange ?? "").toUpperCase() === ex) ??
    desk.items.find((c) => !c.holding_symbol && c.symbol.toUpperCase() === sym);
  if (chip) return <VerdictLink chip={chip} />;
  const skip = desk.skipped.find((s) => s.symbol.toUpperCase() === sym);
  return <span className="stk-label">{skip ? reasonWords(skip.reason) : "Not analysed"}</span>;
}

/**
 * Client wrapper that subscribes to /api/ws/ticks for every holding's
 * instrument_token and updates LTP / P&L in real time. When the WS isn't
 * connected (Kite not authed, server down), we fall back to the static
 * `last_price` baked into the initial snapshot.
 */
export function HoldingsList({ initial, desk: firstDesk }: Props) {
  const tokens = useMemo(
    () =>
      initial
        .map((h) => h.instrument_token ?? 0)
        .filter((t): t is number => !!t),
    [initial],
  );
  const ticks = useTicks(tokens);
  const desk = useDesk(firstDesk);

  if (initial.length === 0) {
    return <Empty>No holdings.</Empty>;
  }

  return (
    <div className="stk-stack-s">
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
        ...(desk ? [{ label: "Suggestion" }] : []),
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
            {desk && (
              <td data-wrap>
                <Suggestion h={h} desk={desk} />
              </td>
            )}
          </tr>
        );
      })}
    </Table>
    {desk ? (
      <p className="stk-fine">
        Suggestion: what the desk&apos;s tested exit rules say about each holding, from prices alone. Open one to see why.
        Suggestions, not orders.
      </p>
    ) : (
      <p className="stk-fine">The desk&apos;s suggestions could not be loaded; reload the page to try again.</p>
    )}
    </div>
  );
}

/** Kite logged out: the last closing verdicts the desk stored, each linking to its page. Nothing when there are none. */
export function DeskLastViews({ desk }: { desk: DeskListDTO | null }) {
  if (!desk || desk.items.length === 0) return null;
  return (
    <section className="stk-section" aria-labelledby="desk-last">
      <h2 id="desk-last">Last suggestions</h2>
      {desk.plain && <p className="stk-fine stk-gap-bottom">{desk.plain}</p>}
      <Table caption="The desk's last closing suggestions" head={[{ label: "Symbol" }, { label: "Suggestion" }, { label: "As of" }]}>
        {desk.items.map((c) => (
          <tr key={c.instrument_id}>
            <td className="stk-mono">{c.symbol}</td>
            <td>
              <VerdictLink chip={c} />
            </td>
            <td className="stk-label">{nsWhen(c.as_of_ns)}</td>
          </tr>
        ))}
      </Table>
    </section>
  );
}

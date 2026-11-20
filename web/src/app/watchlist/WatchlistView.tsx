"use client";

import { Fragment, useCallback, useMemo, useState } from "react";
import Link from "next/link";
import { dayWords, pct, rupees } from "@/lib/format";
import { verdictClass } from "@/lib/desk";
import { useTicks } from "@/lib/useTicks";
import type { WatchRowDTO, WatchlistDTO } from "@/lib/types";
import { addWatched, aboveLow, belowHigh, getWatchlist, ltpPaise, problemWords, removeWatched } from "@/lib/watchlist";
import StockChart from "@/components/StockChart";
import { Chip, Empty, Table } from "@/components/ui";
import AddStock from "./AddStock";

/** A distance as a percentage, or the words for a price at or past the high or low (`at`). */
function Distance({ fraction, at }: { fraction: number | null; at: string }) {
  if (fraction === null) return <>—</>;
  if (fraction <= 0) return <span className="stk-label">{at}</span>;
  return <>{pct(fraction)}</>;
}

/** The desk's verdict for a watched stock, linking to its desk page; plain words while the desk has not graded it. */
function DeskVerdict({ row }: { row: WatchRowDTO }) {
  if (!row.desk_verdict) {
    return (
      <span className="stk-label" title="The desk grades watched stocks after the close, a few new ones each night">
        Not graded yet
      </span>
    );
  }
  return (
    <Link
      href={`/portfolio/${encodeURIComponent(row.symbol)}`}
      className={`stk-link ${verdictClass(row.desk_verdict)}`}
      title={row.desk_close_date ? `The desk's view at the close on ${dayWords(row.desk_close_date)}` : undefined}
      aria-label={`${row.symbol}: the desk says ${row.desk_verdict}. Open its desk page`}
    >
      {row.desk_verdict}
    </Link>
  );
}

function Price({ row, livePaise }: { row: WatchRowDTO; livePaise: number | null }) {
  if (livePaise !== null) {
    return (
      <>
        {rupees(livePaise)}
        <div>
          <Chip tone="up">Live</Chip>
        </div>
      </>
    );
  }
  if (row.last_close_paise == null) {
    return <span className="stk-label">{row.note ?? "—"}</span>;
  }
  return (
    <>
      {rupees(row.last_close_paise)}
      <div className="stk-label">Close, {dayWords(row.last_close_date)}</div>
    </>
  );
}

/**
 * The watchlist: one row per stock with its price (live while the app is open and Kite is logged in, otherwise the last
 * close), its 52-week high and low and how far the price is from each. The list is the page's own state: an add or a
 * removal asks the API, then reads the list again.
 */
export default function WatchlistView({ initial }: { initial: WatchlistDTO }) {
  const [data, setData] = useState<WatchlistDTO>(initial);
  const [busy, setBusy] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null); // the stock whose chart is showing under its row

  const tokens = useMemo(() => data.items.map((r) => r.token ?? 0).filter((t) => t > 0), [data.items]);
  const ticks = useTicks(tokens);
  const watched = useMemo(() => new Set(data.items.map((r) => r.symbol.toUpperCase())), [data.items]);
  const full = data.items.length >= data.max_stocks;

  /** Reads the list again. A failure here is its own problem: the add or removal before it did work. */
  const reload = useCallback(async (): Promise<string | null> => {
    try {
      setData(await getWatchlist());
      return null;
    } catch {
      return "Done, but the list could not be read again. Reload the page to see it.";
    }
  }, []);

  const onAdd = useCallback(
    async (symbol: string): Promise<string | null> => {
      try {
        await addWatched(symbol);
      } catch (e) {
        return problemWords(e, "add that stock");
      }
      return reload();
    },
    [reload],
  );

  async function onRemove(symbol: string) {
    setBusy(symbol);
    setProblem(null);
    try {
      await removeWatched(symbol);
      setProblem(await reload());
    } catch (e) {
      setProblem(problemWords(e, `remove ${symbol}`));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="stk-stack">
      <section className="stk-section stk-stack-s" aria-label="Add a stock">
        <AddStock watched={watched} full={full} onAdd={onAdd} />
        <p className="stk-fine">Recorded and alerted from the next session</p>
      </section>

      <section className="stk-section stk-stack-s" aria-labelledby="watch-list">
        <div className="stk-row-between">
          <h2 id="watch-list">Your stocks</h2>
          <span className="stk-label">
            {data.items.length} of {data.max_stocks}
          </span>
        </div>
        {problem && (
          <p role="alert" className="stk-warn">
            {problem}
          </p>
        )}
        {data.items.length === 0 ? (
          <Empty>No stocks yet. Search for an NSE share above to start following it.</Empty>
        ) : (
          <>
            <Table
              caption="Watchlist"
              head={[
                { label: "Stock" },
                { label: "Price", num: true },
                { label: "52-week high", num: true },
                { label: "Below the high", num: true },
                { label: "52-week low", num: true },
                { label: "Above the low", num: true },
                { label: "Desk view" },
                { label: "Chart" },
                { label: "Remove" },
              ]}
            >
              {data.items.map((r) => {
                const live = r.token ? ticks?.[String(r.token)] : undefined;
                const livePaise = live && Number.isFinite(live.ltp) && live.ltp > 0 ? ltpPaise(live.ltp) : null;
                const price = livePaise ?? r.last_close_paise ?? null;
                const showing = open === r.symbol;
                return (
                  <Fragment key={r.symbol}>
                  <tr>
                    <td className="stk-mono">{r.symbol}</td>
                    <td className="num">
                      <Price row={r} livePaise={livePaise} />
                    </td>
                    <td className="num">
                      {r.high_paise == null ? "—" : rupees(r.high_paise)}
                      {r.high_date && <div className="stk-label">{dayWords(r.high_date)}</div>}
                    </td>
                    <td className="num">
                      <Distance fraction={belowHigh(price, r.high_paise)} at="At a new high" />
                    </td>
                    <td className="num">
                      {r.low_paise == null ? "—" : rupees(r.low_paise)}
                      {r.low_date && <div className="stk-label">{dayWords(r.low_date)}</div>}
                    </td>
                    <td className="num">
                      <Distance fraction={aboveLow(price, r.low_paise)} at="At a new low" />
                    </td>
                    <td>
                      <DeskVerdict row={r} />
                    </td>
                    <td>
                      <button
                        type="button"
                        className="stk-btn"
                        data-tone="quiet"
                        aria-expanded={showing}
                        aria-controls={`chart-${r.symbol}`}
                        aria-label={`${showing ? "Hide" : "Show"} the chart of ${r.symbol}`}
                        onClick={() => setOpen(showing ? null : r.symbol)}
                      >
                        {showing ? "Hide chart" : "Chart"}
                      </button>
                    </td>
                    <td>
                      <button
                        type="button"
                        className="stk-btn"
                        data-tone="quiet"
                        disabled={busy === r.symbol}
                        aria-label={`Remove ${r.symbol} from the watchlist`}
                        onClick={() => void onRemove(r.symbol)}
                      >
                        {busy === r.symbol ? "Removing…" : "Remove"}
                      </button>
                    </td>
                  </tr>
                  {showing && (
                    <tr>
                      <td colSpan={9} id={`chart-${r.symbol}`} data-wrap>
                        <StockChart symbol={r.symbol} />
                      </td>
                    </tr>
                  )}
                  </Fragment>
                );
              })}
            </Table>
            <p className="stk-fine">
              Daily prices arrive after tonight&apos;s fetch. The 52-week range comes from them, so a stock added today
              shows its close and range from tomorrow.
            </p>
          </>
        )}
      </section>
    </div>
  );
}

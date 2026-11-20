"use client";

/**
 * One holding on the desk (HD 10, 12): its daily chart with the rules' exit levels, the suggestion beside it, then
 * each rule's signal and record. Exit suggestions, not orders: nothing here places one, and the owner's average cost,
 * profit and the ₹ outcome of a level are never shown (owner, 2026-11-17).
 *
 * The desk updates once a day, after the session: the page shows the latest nightly view and the close it is from,
 * and is not asked for again on a timer.
 */
import Link from "next/link";
import { use, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { asOfClose, getDeskView, verdictTone, type DeskViewResult } from "@/lib/desk";
import type { KiteStatus } from "@/lib/types";
import { Chip, Empty, Loading, Panel, Skeleton } from "@/components/ui";
import DeskChart from "./DeskChart";
import ThesisPanel from "./ThesisPanel";
import SignalsTable from "./SignalsTable";

function safeDecode(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

export default function DeskHoldingPage({ params }: { params: Promise<{ symbol: string }> }) {
  const { symbol: raw } = use(params);
  const symbol = safeDecode(raw).toUpperCase();
  const [result, setResult] = useState<DeskViewResult | null>(null);
  const [kite, setKite] = useState<KiteStatus | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const ctl = new AbortController();
    const load = async () => {
      const [r, k] = await Promise.all([
        getDeskView(symbol, ctl.signal),
        api<KiteStatus>("/api/kite/status", { signal: ctl.signal }).catch(() => null),
      ]);
      if (ctl.signal.aborted) return;
      setKite(k);
      setResult(r);
    };
    load();
    return () => ctl.abort();
  }, [symbol, attempt]);

  // a watched stock that is not held is a watchlist stock: its crumb goes back to the watchlist (CW 6)
  const watchedOnly = result?.kind === "view" && result.view.held === false;
  return (
    <div className="stk-page">
      <Link href={watchedOnly ? "/watchlist" : "/portfolio"} className="stk-crumb">
        {watchedOnly ? "Watchlist" : "Portfolio"}
      </Link>

      {result === null && (
        <Loading label="Loading the holding">
          <Skeleton size="title" />
          <Skeleton size="m" />
          <Skeleton size="l" />
        </Loading>
      )}

      {result?.kind === "error" && (
        <div className="stk-stack">
          <div className="stk-head">
            <div>
              <h1>{symbol}</h1>
              <p className="stk-lede">Exit suggestions, not orders.</p>
            </div>
          </div>
          <Panel tone="red">
            <div className="stk-row-between">
              <p role="alert">
                Could not reach the STOCKIFY API, so the desk&apos;s view of {symbol} cannot be shown. Check that the app
                is running, then try again.
              </p>
              <button type="button" className="stk-btn" onClick={() => setAttempt((n) => n + 1)}>
                Try again
              </button>
            </div>
          </Panel>
        </div>
      )}

      {result?.kind === "missing" && (
        <div className="stk-stack">
          <div className="stk-head">
            <div>
              <h1>{symbol}</h1>
              <p className="stk-lede">Exit suggestions, not orders.</p>
            </div>
          </div>
          <Empty>
            The desk has not analysed {symbol}: {result.reason.trim().replace(/\.?$/, ".")}{" "}
            <Link href="/portfolio" className="stk-link">
              Back to your holdings
            </Link>
          </Empty>
        </div>
      )}

      {result?.kind === "view" && <View result={result} kite={kite} />}
    </div>
  );
}

function View({ result, kite }: { result: Extract<DeskViewResult, { kind: "view" }>; kite: KiteStatus | null }) {
  const v = result.view;
  const loggedOut = kite !== null && !kite.authenticated;
  const plain = v.plain;
  const held = v.held !== false;
  return (
    <div className="stk-stack" data-ruled>
      <div className="stk-head">
        <div>
          <div className="stk-row">
            <Chip tone={verdictTone(v.verdict)}>{v.verdict}</Chip>
            <span className="stk-label">{asOfClose(v.close_date, v.as_of_ns)}</span>
          </div>
          <h1 className="stk-gap-s">{v.symbol}</h1>
          <p className="stk-lede">
            {held ? "Exit suggestions, not orders." : "What the tested rules say about this watched stock."}
          </p>
        </div>
      </div>

      {loggedOut && held ? (
        <Panel tone="amber">
          <p>
            {plain}{" "}
            <Link href="/portfolio" className="stk-link">
              Log in to Kite on the Portfolio page.
            </Link>
          </p>
        </Panel>
      ) : (
        plain && (
          <p className="stk-fine" role="status">
            {plain}{" "}
            {!held && (
              <Link href="/watchlist" className="stk-link">
                Back to the watchlist
              </Link>
            )}
          </p>
        )
      )}

      <div className="stk-section stk-desk-top">
        <Panel>
          <DeskChart symbol={v.symbol} />
        </Panel>
        <ThesisPanel view={v} />
      </div>

      <SignalsTable view={v} />
    </div>
  );
}

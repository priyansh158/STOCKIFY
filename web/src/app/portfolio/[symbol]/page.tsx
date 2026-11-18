"use client";

/**
 * One holding on the desk (HD 10): its chart with the rules' exit levels, the suggestion beside it, then each rule's
 * signal and record, the option chain and option ideas. Exit suggestions, not orders: nothing here places one, and the
 * owner's average cost, profit and the ₹ outcome of a level are never shown (owner, 2026-11-17).
 *
 * While the API says the view is live (the session is open and the price can be read) the view is asked for again
 * every minute; otherwise the page says when it was last updated.
 */
import Link from "next/link";
import { use, useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { DESK_POLL_MS, getDeskView, nsTime, nsWhen, plainWithoutOptions, verdictTone, type DeskViewResult } from "@/lib/desk";
import type { KiteStatus } from "@/lib/types";
import { Chip, Empty, Loading, Panel, Skeleton } from "@/components/ui";
import DeskChart from "./DeskChart";
import ThesisPanel from "./ThesisPanel";
import SignalsTable from "./SignalsTable";
import ChainGauge from "./ChainGauge";
import IdeasList from "./IdeasList";

const IDEAS_NEED_KITE = "Option ideas need your holdings from Kite.";

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
  const [refreshFailed, setRefreshFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const shown = useRef(false); // a view is on screen: a failed refresh keeps it and says so

  const load = useCallback(
    async (signal?: AbortSignal) => {
      const [r, k] = await Promise.all([
        getDeskView(symbol, signal),
        api<KiteStatus>("/api/kite/status", { signal }).catch(() => null),
      ]);
      if (signal?.aborted) return;
      setKite(k);
      if (r.kind === "error" && shown.current) {
        setRefreshFailed(true);
        return;
      }
      shown.current = r.kind === "view";
      setRefreshFailed(false);
      setResult(r);
    },
    [symbol],
  );

  useEffect(() => {
    const ctl = new AbortController();
    const first = async () => {
      await load(ctl.signal);
    };
    first();
    return () => ctl.abort();
  }, [load, attempt]);

  const live = result?.kind === "view" && result.view.live;
  useEffect(() => {
    if (!live) return;
    const ctl = new AbortController();
    const timer = window.setInterval(() => {
      if (!document.hidden) load(ctl.signal);
    }, DESK_POLL_MS);
    return () => {
      window.clearInterval(timer);
      ctl.abort();
    };
  }, [live, load]);

  return (
    <div className="stk-page">
      <Link href="/portfolio" className="stk-crumb">
        Portfolio
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

      {result?.kind === "view" && <View result={result} kite={kite} refreshFailed={refreshFailed} />}
    </div>
  );
}

function View({ result, kite, refreshFailed }: { result: Extract<DeskViewResult, { kind: "view" }>; kite: KiteStatus | null; refreshFailed: boolean }) {
  const v = result.view;
  const loggedOut = kite !== null && !kite.authenticated;
  const full = plainWithoutOptions(v.plain);
  const ideasNote = full.includes(IDEAS_NEED_KITE) ? IDEAS_NEED_KITE : undefined; // said in the ideas section instead
  const plain = full.replace(IDEAS_NEED_KITE, "").trim();
  return (
    <div className="stk-stack" data-ruled>
      <div className="stk-head">
        <div>
          <div className="stk-row">
            <Chip tone={verdictTone(v.verdict)}>{v.verdict}</Chip>
            <span className={v.live ? "stk-mark stk-up" : "stk-label"}>
              {v.live ? `Live, updated ${nsTime(v.as_of_ns)}` : `Last updated ${nsWhen(v.as_of_ns)}`}
            </span>
          </div>
          <h1 className="stk-gap-s">{v.symbol}</h1>
          <p className="stk-lede">Exit suggestions, not orders.</p>
        </div>
      </div>

      {loggedOut ? (
        <Panel tone="amber">
          <p>
            {plain}{" "}
            <Link href="/portfolio" className="stk-link">
              Log in to Kite on the Portfolio page.
            </Link>
          </p>
        </Panel>
      ) : (
        plain && <p className="stk-fine" role="status">{plain}</p>
      )}
      {refreshFailed && (
        <Panel tone="red">
          <p role="alert">Could not refresh from the STOCKIFY API; this is the view from {nsWhen(v.as_of_ns)}. The page keeps trying.</p>
        </Panel>
      )}

      <div className="stk-section stk-desk-top">
        <Panel>
          <DeskChart symbol={v.symbol} live={v.live} />
        </Panel>
        <ThesisPanel view={v} />
      </div>

      <SignalsTable view={v} />
      <ChainGauge view={v} />
      <IdeasList view={v} note={ideasNote} />
    </div>
  );
}

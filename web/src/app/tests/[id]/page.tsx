"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useState } from "react";
import { growth, pct, rupees, rupeesWhole } from "@/lib/format";
import { cancelTest, getCharges, getEquity, getFills, getLogs, getTest } from "@/lib/tests";
import type { TestCharge, TestEquity, TestFill, TestLogs, TestRunDetail } from "@/lib/types";
import { buttonStyle, Card, LabelChips, Stat, StatusChip, runSummary } from "../ui";
import EquityChart from "./EquityChart";

const POLL_MS = 2000;
// A yearly rate from a few weeks of results is noise: show it only for runs of about a trading year or more.
const MIN_DAYS_FOR_YEARLY = 240;

const COST_NAMES: Record<string, string> = {
  brokerage: "Brokerage",
  stt: "Securities transaction tax (STT)",
  exchange: "Exchange fees",
  sebi: "SEBI fee",
  stamp: "Stamp duty",
  gst: "GST",
  dp: "Demat (DP) charges",
  other: "Other",
};

type Extra = { fills: TestFill[]; equity: TestEquity[]; charges: TestCharge[]; logs: TestLogs | null };

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

export default function TestDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [detail, setDetail] = useState<TestRunDetail | null>(null);
  const [extra, setExtra] = useState<Extra>({ fills: [], equity: [], charges: [], logs: null });
  const [error, setError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const d = await getTest(id);
      setDetail(d);
      setError(null);
      if (d.run.status === "done") {
        const [fills, equity, charges] = await Promise.all([getFills(id), getEquity(id), getCharges(id)]);
        setExtra({ fills, equity, charges, logs: null });
      } else if (d.run.status === "failed" || d.run.status === "cancelled") {
        setExtra({ fills: [], equity: [], charges: [], logs: await getLogs(id).catch(() => null) });
      }
      return d.run.status;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return null;
    }
  }, [id]);

  useEffect(() => {
    let stop = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      const status = await refresh();
      if (!stop && (status === "queued" || status === "running" || status === null)) {
        timer = setTimeout(tick, POLL_MS);
      }
    };
    tick();
    return () => {
      stop = true;
      if (timer) clearTimeout(timer);
    };
  }, [refresh]);

  async function cancel() {
    setCancelling(true);
    try {
      await cancelTest(id);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCancelling(false);
    }
  }

  const run = detail?.run;
  const m = detail?.metrics ?? {};
  const g = growth(run?.nav_start_paise, run?.nav_end_paise);
  const costTotals: Record<string, number> = {};
  for (const c of extra.charges) {
    for (const [k, v] of Object.entries(c.components)) costTotals[k] = (costTotals[k] ?? 0) + v;
  }
  const summaryLine = run
    ? [runSummary(run, extra.equity[0]?.trade_date, extra.equity[extra.equity.length - 1]?.trade_date),
      run.capital_paise ? `started with ${rupeesWhole(run.capital_paise)}` : null].filter(Boolean).join(" · ")
    : "";
  const costTotal = extra.charges.reduce((s, c) => s + c.total_paise, 0);

  return (
    <div className="mx-auto max-w-2xl px-4 py-8 sm:py-12">
      <Link href="/tests" className="stk-mono-tight mb-6 flex min-h-11 items-center" style={{ color: "var(--color-fg-muted)" }}>
        ← All tests
      </Link>

      {error && (
        <div className="mb-4">
          <Card tone="red"><p className="text-sm" style={{ color: "var(--color-red-soft)" }}>{error}</p></Card>
        </div>
      )}
      {!run ? (
        !error && <p style={{ color: "var(--color-fg-muted)" }}>Loading…</p>
      ) : (
        <div className="flex flex-col gap-4">
          <div>
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <StatusChip status={run.status} />
              <span className="stk-mono-tight text-xs" style={{ color: "var(--color-fg-muted)" }}>{run.impl ?? ""}</span>
            </div>
            <h1 className="stk-mono break-words text-xl font-bold sm:text-2xl" style={{ color: "var(--color-green)" }}>
              {run.name ?? run.run_id}
            </h1>
            {summaryLine && (
              <p className="text-sm" style={{ color: "var(--color-fg-muted)" }}>{summaryLine}</p>
            )}
            <div className="mt-2"><LabelChips run={run} /></div>
          </div>

          {(run.status === "queued" || run.status === "running") && (
            <Card>
              <p className="mb-3 text-sm" style={{ color: "var(--color-fg)" }}>
                {run.status === "queued" ? "Waiting for a free slot…" : "Running… this page updates by itself."}
              </p>
              <button type="button" onClick={cancel} disabled={cancelling}
                className="stk-mono-tight min-h-11 w-full rounded-md border px-4 py-2"
                style={{ ...buttonStyle, color: "var(--color-amber)", borderColor: "var(--color-amber)" }}>
                {cancelling ? "Cancelling…" : "Cancel test"}
              </button>
            </Card>
          )}

          {run.status === "failed" && (
            <Card tone="red">
              <h2 className="stk-mono-tight mb-2" style={{ color: "var(--color-red-soft)" }}>What went wrong</h2>
              <p className="stk-mono break-words text-sm" style={{ color: "var(--color-red-soft)" }}>{run.error ?? "no message"}</p>
            </Card>
          )}

          {extra.logs && (extra.logs.events_log.length > 0 || extra.logs.runner_log.length > 0) && (
            <Card>
              <h2 className="stk-mono-tight mb-2" style={{ color: "var(--color-fg-muted)" }}>Last lines of the log</h2>
              <pre className="stk-mono max-h-80 overflow-auto whitespace-pre-wrap break-words text-xs" style={{ color: "var(--color-fg)" }}>
                {[...extra.logs.runner_log, ...extra.logs.events_log].join("\n")}
              </pre>
            </Card>
          )}

          {run.status === "done" && (
            <>
              <Card>
                <div className="grid grid-cols-2 gap-3">
                  <Stat label="Started with" value={rupeesWhole(run.nav_start_paise)} />
                  <Stat label="Ended with" value={rupeesWhole(run.nav_end_paise)} tone={g === null ? undefined : g >= 0 ? "up" : "down"} />
                  <Stat label="Gain or loss" value={pct(g, true)} tone={g === null ? undefined : g >= 0 ? "up" : "down"} />
                  <Stat label="Per year, on average"
                    value={(num(m.days) ?? 0) >= MIN_DAYS_FOR_YEARLY ? pct(num(m.cagr), true) : "too short to say"} />
                  <Stat label="Worst fall from a high" value={pct(run.max_drawdown)} />
                  <Stat label="Trades" value={String(run.trades ?? 0)} />
                  <Stat label="Trades that made money" value={pct(num(m.win_rate))} />
                  <Stat label="Costs paid" value={rupees(run.costs_total_paise)} />
                </div>
              </Card>

              <Card>
                <h2 className="stk-mono-tight mb-3" style={{ color: "var(--color-fg-muted)" }}>Money over time</h2>
                <EquityChart rows={extra.equity} start={run.nav_start_paise} />
              </Card>

              <Card>
                <h2 className="stk-mono-tight mb-3" style={{ color: "var(--color-fg-muted)" }}>
                  Fills ({extra.fills.length})
                </h2>
                {extra.fills.length === 0 ? (
                  <p className="text-sm" style={{ color: "var(--color-fg-muted)" }}>No orders filled.</p>
                ) : (
                  <ul className="flex flex-col gap-2">
                    {extra.fills.slice(0, 200).map((f) => (
                      <li key={f.fill_id} className="rounded-md border px-3 py-2" style={{ borderColor: "rgba(0,255,65,0.15)" }}>
                        <div className="flex items-center justify-between gap-2">
                          <span className="stk-mono text-sm font-semibold"
                            style={{ color: f.side === "BUY" ? "var(--color-green)" : "var(--color-red-soft)" }}>
                            {f.side === "BUY" ? "Bought" : "Sold"} {f.qty.toLocaleString("en-IN")}
                          </span>
                          <span className="stk-mono text-sm">{rupees(f.price_paise)}</span>
                        </div>
                        <div className="text-xs" style={{ color: "var(--color-fg-muted)" }}>
                          {f.tradingsymbol ?? `instrument ${f.instrument_id}`} · {f.t_ist.slice(0, 16).replace("T", " ")} IST · order {f.order_id}
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>

              <Card>
                <h2 className="stk-mono-tight mb-3" style={{ color: "var(--color-fg-muted)" }}>Where the costs went</h2>
                {costTotal === 0 ? (
                  <p className="text-sm" style={{ color: "var(--color-fg-muted)" }}>No costs were charged in this test.</p>
                ) : (
                <ul className="flex flex-col gap-1 text-sm">
                  {Object.entries(costTotals).filter(([, v]) => v !== 0).map(([k, v]) => (
                    <li key={k} className="flex justify-between gap-3"><span>{COST_NAMES[k] ?? k.replace(/_/g, " ")}</span><span className="stk-mono">{rupees(v)}</span></li>
                  ))}
                  <li className="mt-1 flex justify-between border-t pt-1 font-semibold" style={{ borderColor: "rgba(0,255,65,0.15)" }}>
                    <span>Total</span><span className="stk-mono">{rupees(costTotal)}</span>
                  </li>
                </ul>
                )}
              </Card>
            </>
          )}

          {detail?.manifest && (
            <Card>
              <h2 className="stk-mono-tight mb-2" style={{ color: "var(--color-fg-muted)" }}>For reproducing this run</h2>
              <dl className="stk-mono grid grid-cols-1 gap-1 text-xs" style={{ color: "var(--color-fg)" }}>
                {Object.entries(detail.manifest)
                  .filter(([k]) => ["engine_build_id", "bundle_sha256", "packs", "duration_ms", "python", "os"].includes(k))
                  .map(([k, v]) => (
                    <div key={k} className="break-all">
                      <dt className="inline" style={{ color: "var(--color-fg-muted)" }}>{k}: </dt>
                      <dd className="inline">{Array.isArray(v) ? v.join(", ") : String(v)}</dd>
                    </div>
                  ))}
              </dl>
              <p className="mt-2 break-all text-xs" style={{ color: "var(--color-fg-muted)" }}>run id {run.run_id}</p>
            </Card>
          )}
        </div>
      )}
    </div>
  );
}

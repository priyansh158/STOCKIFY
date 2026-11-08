"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useState } from "react";
import { growth, pct, rupees, rupeesWhole } from "@/lib/format";
import { ApiError } from "@/lib/api";
import { cancelTest, getEquity, getFills, getGreeks, getLogs, getMargin, getSettlements, getTest } from "@/lib/tests";
import type { TestEquity, TestFill, TestGreek, TestLogs, TestMargin, TestRunDetail, TestSettlement } from "@/lib/types";
import { buttonStyle, Card, LabelChips, Stat, StatusChip, labelMeaning, runSummary } from "../ui";
import EquityChart from "./EquityChart";

const POLL_MS = 2000;
const MAX_BACKOFF_MS = 30_000;  // after errors the poll waits longer each time, up to this
const FILL_PAGE = 200;
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

type Extra = {
  fills: TestFill[];
  equity: TestEquity[];
  margin: TestMargin[];
  settlements: TestSettlement[];
  greeks: TestGreek[];
  logs: TestLogs | null;
};
const NO_EXTRA: Extra = { fills: [], equity: [], margin: [], settlements: [], greeks: [], logs: null };
// pending: poll again soon; terminal: the run finished (stop); missing: 404 (stop); error: retry with back-off
type Outcome = "pending" | "terminal" | "missing" | "error";

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

export default function TestDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [detail, setDetail] = useState<TestRunDetail | null>(null);
  const [extra, setExtra] = useState<Extra>(NO_EXTRA);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);

  const refresh = useCallback(async (): Promise<Outcome> => {
    try {
      const d = await getTest(id);
      setDetail(d);
      setError(null);
      if (d.run.status === "done") {
        const [fills, equity, margin, settlements, greeks] = await Promise.all([
          getFills(id, 0, FILL_PAGE), getEquity(id), getMargin(id).catch(() => []), getSettlements(id).catch(() => []),
          getGreeks(id).catch(() => []),
        ]);
        setExtra({ fills, equity, margin, settlements, greeks, logs: null });
      } else if (d.run.status === "failed" || d.run.status === "cancelled") {
        setExtra({ ...NO_EXTRA, logs: await getLogs(id).catch(() => null) });
      }
      return d.run.status === "queued" || d.run.status === "running" ? "pending" : "terminal";
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        setError(`There is no test run called ${id}.`);
        return "missing";
      }
      setError(`${e instanceof Error ? e.message : String(e)} (retrying)`);
      return "error";
    }
  }, [id]);

  useEffect(() => {
    let stop = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let delay = POLL_MS;
    const tick = async () => {
      const outcome = await refresh();
      if (stop || outcome === "terminal" || outcome === "missing") return;
      delay = outcome === "error" ? Math.min(delay * 2, MAX_BACKOFF_MS) : POLL_MS;
      timer = setTimeout(tick, delay);
    };
    tick();
    return () => {
      stop = true;
      if (timer) clearTimeout(timer);
    };
  }, [refresh]);

  async function moreFills() {
    setLoadingMore(true);
    try {
      const next = await getFills(id, extra.fills.length, FILL_PAGE);
      setExtra((x) => ({ ...x, fills: [...x.fills, ...next] }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoadingMore(false);
    }
  }

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
  const costTotals = detail?.cost_totals ?? {};      // the whole run, summed by the server (/charges is paged)
  const fillCount = detail?.fill_count ?? extra.fills.length;
  const summaryLine = run
    ? [runSummary(run, extra.equity[0]?.trade_date, extra.equity[extra.equity.length - 1]?.trade_date),
      run.capital_paise ? `started with ${rupeesWhole(run.capital_paise)}` : null].filter(Boolean).join(" · ")
    : "";
  const costTotal = run?.costs_total_paise ?? Object.values(costTotals).reduce((a, b) => a + b, 0);

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
                  Fills ({fillCount.toLocaleString("en-IN")})
                </h2>
                {extra.fills.length === 0 ? (
                  <p className="text-sm" style={{ color: "var(--color-fg-muted)" }}>No orders filled.</p>
                ) : (
                  <ul className="flex flex-col gap-2">
                    {extra.fills.map((f) => (
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
                {extra.fills.length < fillCount && (
                  <button type="button" onClick={moreFills} disabled={loadingMore}
                    className="stk-mono-tight mt-3 min-h-11 w-full rounded-md border px-4 py-2" style={buttonStyle}>
                    {loadingMore ? "Loading…" : `Show more (${extra.fills.length.toLocaleString("en-IN")} of ${fillCount.toLocaleString("en-IN")} shown)`}
                  </button>
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
                  <li className="text-xs" style={{ color: "var(--color-fg-muted)" }}>
                    Each part is rounded to the paisa; the total is exact, so the parts can differ from it by a few paise.
                  </li>
                </ul>
                )}
              </Card>

              <MarginByDay rows={extra.margin} equity={extra.equity} />
              <GreeksAtDayEnd rows={extra.greeks} />
              <Settlements rows={extra.settlements} />
              <LabelMeanings labels={run.labels} />
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

const muted = { color: "var(--color-fg-muted)" };
const rowBorder = { borderColor: "rgba(0,255,65,0.15)" };

/** Margin held by day: the SPAN-lite breakdown when the run has one (margin_daily.csv), else the flat model's
 * day-end amount from the equity rows. */
function MarginByDay({ rows, equity }: { rows: TestMargin[]; equity: TestEquity[] }) {
  const days = Array.from(new Set(rows.map((r) => r.trade_date)));
  const held = equity.filter((e) => (e.margin_paise ?? 0) > 0);
  if (days.length === 0 && held.length === 0) return null;
  return (
    <Card>
      <h2 className="stk-mono-tight mb-3" style={muted}>Margin by day</h2>
      {days.length === 0 ? (
        <>
          <p className="mb-2 text-sm" style={muted}>
            This test used the flat margin: a set percentage of each position&apos;s value. Margin held at the end of
            each day:
          </p>
          <ul className="flex flex-col gap-1 text-sm">
            {held.slice(-30).map((e) => (
              <li key={e.trade_date} className="flex justify-between gap-3">
                <span>{e.trade_date}</span><span className="stk-mono">{rupees(e.margin_paise)}</span>
              </li>
            ))}
          </ul>
          {held.length > 30 && <p className="mt-1 text-xs" style={muted}>The last 30 days with margin held are shown.</p>}
        </>
      ) : (
        <ul className="flex flex-col gap-2">
          {days.map((d) => {
            const today = rows.filter((r) => r.trade_date === d);
            const total = today.reduce((a, r) => a + r.total_paise, 0);
            return (
              <li key={d} className="rounded-md border px-3 py-2" style={rowBorder}>
                <div className="flex items-center justify-between gap-2">
                  <span className="stk-mono text-sm font-semibold">{d}</span>
                  <span className="stk-mono text-sm">{rupees(total)}</span>
                </div>
                {today.map((r) => (
                  <dl key={r.underlying_id} className="mt-1 grid grid-cols-2 gap-x-3 text-xs" style={muted}>
                    <dt className="col-span-2" style={{ color: "var(--color-fg)" }}>
                      {r.underlying ?? `underlying ${r.underlying_id}`}
                    </dt>
                    <dt>Price and volatility risk</dt><dd className="stk-mono text-right">{rupees(r.scan_paise)}</dd>
                    <dt>Short-option minimum</dt><dd className="stk-mono text-right">{rupees(r.somc_paise)}</dd>
                    <dt>Extreme-loss margin</dt><dd className="stk-mono text-right">{rupees(r.elm_paise)}</dd>
                    <dt>Expiry-day and other add-ons</dt><dd className="stk-mono text-right">{rupees(r.addons_paise)}</dd>
                  </dl>
                ))}
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

const IV_REASONS: Record<string, string> = {
  BELOW_INTRINSIC: "its price is below what exercising it now would pay, so no volatility fits",
  ABOVE_FORWARD: "a call priced above the forward itself, so no volatility fits",
  ABOVE_STRIKE: "a put priced above its discounted strike, so no volatility fits",
  BELOW_TICK: "its price is under one tick, too small to read a volatility from",
  ABOVE_MAX_VOL: "its price needs a volatility above the highest one searched",
  LOW_VEGA: "its price barely moves with volatility, so the volatility can't be read reliably",
  NO_UNDERLYING_PRICE: "there was no price for the underlying at day end",
  NO_PRICE: "it had no price at day end",
  EXPIRED: "it had expired",
};

function signed(v: number, digits: number): string {
  return `${v > 0 ? "+" : ""}${v.toLocaleString("en-IN", { maximumFractionDigits: digits, minimumFractionDigits: digits })}`;
}

function signedRupees(v: number): string {
  return `${v > 0 ? "+" : ""}${rupees(Math.round(v * 100))}`;
}

/** Delta, gamma, vega and theta of each open F&O position at a day end, and their totals per underlying. Computed by
 * the API with Black-76 from the day-end marks; an option with no implied volatility shows why, never a number. */
function GreeksAtDayEnd({ rows }: { rows: TestGreek[] }) {
  const days = Array.from(new Set(rows.map((r) => r.trade_date))).sort();
  const [picked, setPicked] = useState<string | null>(null);
  if (days.length === 0) return null;
  const day = picked && days.includes(picked) ? picked : days[days.length - 1];
  const today = rows.filter((r) => r.trade_date === day);
  const unders = Array.from(new Set(today.map((r) => r.underlying ?? "")));
  return (
    <Card>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 className="stk-mono-tight" style={muted}>Greeks at day end</h2>
        {days.length > 1 && (
          <select aria-label="Day" value={day} onChange={(e) => setPicked(e.target.value)}
                  className="stk-mono rounded-md border px-2 text-sm"
                  style={{ ...rowBorder, minHeight: 44, background: "transparent", color: "var(--color-fg)" }}>
            {days.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        )}
      </div>
      <p className="mb-2 text-xs" style={muted}>
        Delta: units of the underlying the positions behave like. Gamma: how much delta changes per ₹1 move. Vega: ₹
        gained or lost per volatility point. Theta: ₹ gained or lost per day from time passing. Positive means long.
      </p>
      <ul className="flex flex-col gap-2">
        {unders.map((u) => {
          const legs = today.filter((r) => (r.underlying ?? "") === u);
          const priced = legs.filter((r) => r.delta_units != null);
          const sum = (f: (r: TestGreek) => number | null | undefined) => priced.reduce((a, r) => a + (f(r) ?? 0), 0);
          const missing = legs.length - priced.length;
          const spot = legs.find((r) => r.underlying_paise != null)?.underlying_paise ?? null;
          return (
            <li key={u} className="rounded-md border px-3 py-2" style={rowBorder}>
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-semibold">{u || "Unknown underlying"}</span>
                {spot != null && <span className="stk-mono text-xs" style={muted}>at {rupees(spot)}</span>}
              </div>
              <dl className="mt-1 grid grid-cols-2 gap-x-3 text-xs">
                <dt style={muted}>Delta</dt><dd className="stk-mono text-right">{signed(sum((r) => r.delta_units), 1)} units</dd>
                {spot != null && (<>
                  <dt style={muted}>Delta in ₹</dt>
                  <dd className="stk-mono text-right">{signedRupees(sum((r) => r.delta_units) * spot / 100)}</dd>
                </>)}
                <dt style={muted}>Gamma, per ₹1</dt><dd className="stk-mono text-right">{signed(sum((r) => r.gamma_units_per_rupee), 4)}</dd>
                <dt style={muted}>Vega, per point</dt><dd className="stk-mono text-right">{signedRupees(sum((r) => r.vega_rupees_per_point))}</dd>
                <dt style={muted}>Theta, per day</dt><dd className="stk-mono text-right">{signedRupees(sum((r) => r.theta_rupees_per_day))}</dd>
              </dl>
              {missing > 0 && (
                <p className="mt-1 text-xs" style={{ color: "var(--color-amber)" }}>
                  {missing === 1 ? "One position is" : `${missing} positions are`} left out of these totals: no volatility could be found (below).
                </p>
              )}
              <ul className="mt-2 flex flex-col gap-1">
                {legs.map((r) => (
                  <li key={r.instrument_id} className="border-t pt-1 text-xs" style={rowBorder}>
                    <div className="flex justify-between gap-2">
                      <span className="break-all">
                        {r.qty > 0 ? "Long" : "Short"} {Math.abs(r.qty).toLocaleString("en-IN")} {r.tradingsymbol ?? `instrument ${r.instrument_id}`}
                      </span>
                      <span className="stk-mono">{rupees(r.mark_paise)}</span>
                    </div>
                    {r.kind === "FUT" ? (
                      <div style={muted}>Future: delta {signed(r.delta_units ?? 0, 1)} units</div>
                    ) : r.iv_reason ? (
                      <div style={{ color: "var(--color-amber)" }}>
                        No Greeks: {IV_REASONS[r.iv_reason] ?? r.iv_reason}.
                      </div>
                    ) : (
                      <div className="stk-mono" style={muted}>
                        IV {pct(r.iv ?? 0)} · Δ {signed(r.delta_units ?? 0, 1)} · vega {signedRupees(r.vega_rupees_per_point ?? 0)} · θ {signedRupees(r.theta_rupees_per_day ?? 0)}/day
                      </div>
                    )}
                    {r.expiry_close_estimated && r.kind !== "FUT" && (
                      <div style={muted}>Expiry after the test ends: its closing time is estimated.</div>
                    )}
                  </li>
                ))}
              </ul>
            </li>
          );
        })}
      </ul>
    </Card>
  );
}

/** Expiry settlements in cash and physical deliveries, with whether NSE's price or an estimate was used. */
function Settlements({ rows }: { rows: TestSettlement[] }) {
  if (rows.length === 0) return null;
  return (
    <Card>
      <h2 className="stk-mono-tight mb-3" style={muted}>Expiry settlements</h2>
      <ul className="flex flex-col gap-2">
        {rows.map((s, i) => {
          const name = s.tradingsymbol ?? `instrument ${s.instrument_id}`;
          const what = s.kind === "cash"
            ? `${name} settled in cash`
            : `${s.side === "BUY" ? "Took" : "Gave"} delivery of ${(s.qty ?? 0).toLocaleString("en-IN")} ${name}`;
          return (
            <li key={`${s.t_ns}-${s.instrument_id}-${i}`} className="rounded-md border px-3 py-2" style={rowBorder}>
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-semibold">{what}</span>
                <span className="stk-mono text-sm">{rupees(s.price_paise)}</span>
              </div>
              <div className="text-xs" style={s.source === "fallback" ? { color: "var(--color-amber)" } : muted}>
                {s.trade_date} · {s.source === "fallback" ? "estimated price (NSE had published none)" : "NSE's settlement price"}
                {s.charges_paise ? ` · charges ${rupees(s.charges_paise)}` : ""}
              </div>
            </li>
          );
        })}
      </ul>
    </Card>
  );
}

/** Each of the run's labels as a sentence: what it means for how far the results can be trusted. */
function LabelMeanings({ labels }: { labels: string[] }) {
  if (labels.length === 0) return null;
  return (
    <Card>
      <h2 className="stk-mono-tight mb-2" style={muted}>What the labels mean</h2>
      <ul className="flex list-disc flex-col gap-1 pl-5 text-sm">
        {labels.map((l) => <li key={l}>{labelMeaning(l)}</li>)}
      </ul>
    </Card>
  );
}

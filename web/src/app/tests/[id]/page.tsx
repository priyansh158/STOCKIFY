"use client";

import Link from "next/link";
import { Fragment, use, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { growth, pct, rupees, rupeesWhole } from "@/lib/format";
import { ApiError } from "@/lib/api";
import { getRunChart } from "@/lib/chart_api";
import { chartViewState, joinFillsAndMarkers, type ChartMarker } from "@/lib/chart";
import { chartHint, fillWhen } from "@/lib/runView";
import { cancelTest, getEquity, getFills, getGreeks, getLogs, getMargin, getSettlements, getTest } from "@/lib/tests";
import type { TestEquity, TestFill, TestGreek, TestLogs, TestMargin, TestRunDetail, TestSettlement } from "@/lib/types";
import { Empty, Loading, Panel, Skeleton, Stat, StatGrid, Table } from "@/components/ui";
import { LabelChips, StatusChip, labelMeaning, marginStep, runSummary } from "../ui";
import { PriceChartLazy } from "@/components/PriceChartLazy";
import EquityChart from "./EquityChart";
import ResearchPanel from "./ResearchPanel";
import { costScale } from "@/lib/research";
import { implWords } from "@/lib/names";

const POLL_MS = 2000;
const MAX_BACKOFF_MS = 30_000;  // after errors the poll waits longer each time, up to this
const FILL_PAGE = 200;

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
  // The chart's markers, kept here so the Fills table can show each order's note; a fill row and its marker point at each other.
  const [chartMarkers, setChartMarkers] = useState<ChartMarker[]>([]);
  const [focusId, setFocusId] = useState<string | undefined>(undefined);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [chartDrawn, setChartDrawn] = useState(false); // the chart has price data and is on screen (else "Show on chart" has nothing to show)
  const chartRef = useRef<HTMLDivElement>(null);

  const loadChart = useCallback<NonNullable<React.ComponentProps<typeof PriceChartLazy>["load"]>>(
    async (range, opts) => {
      const model = await getRunChart(id, { ...opts, ...range });
      if (model) {
        setChartDrawn(chartViewState(model) === "full" || chartViewState(model) === "empty");
        setChartMarkers((have) => {
          const seen = new Set(have.map((m) => m.id));
          const add = model.markers.filter((m) => !seen.has(m.id));
          return add.length ? [...have, ...add] : have;
        });
      }
      return model;
    },
    [id],
  );

  // A focus request is a one-shot: clear it once the chart has seen it, so the same row can be asked for again.
  useEffect(() => {
    if (focusId === undefined) return;
    const t = setTimeout(() => setFocusId(undefined), 0);
    return () => clearTimeout(t);
  }, [focusId]);

  function showOnChart(markerId: string) {
    setActiveId(markerId);
    setFocusId(markerId);
    chartRef.current?.scrollIntoView({ block: "nearest" });
  }

  function markerClicked(markerId: string) {
    setActiveId(markerId);
    const row = document.querySelector(`[data-marker-id="${CSS.escape(markerId)}"]`);
    row?.scrollIntoView({ block: "nearest" });
  }

  const joined = useMemo(() => joinFillsAndMarkers(extra.fills, chartMarkers), [extra.fills, chartMarkers]);

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
  const m = detail?.metrics ?? {};      // figures the server withheld (too short a run, D-135) are null here; `detail.withheld` says why
  const g = growth(run?.nav_start_paise, run?.nav_end_paise);
  const costTotals = detail?.cost_totals ?? {};      // the whole run, summed by the server (/charges is paged)
  const fillCount = detail?.fill_count ?? extra.fills.length;
  const summaryLine = run
    ? [runSummary(run, extra.equity[0]?.trade_date, extra.equity[extra.equity.length - 1]?.trade_date),
      run.capital_paise ? `started with ${rupeesWhole(run.capital_paise)}` : null].filter(Boolean).join(", ")
    : "";
  const scale = costScale(detail?.config);
  const costTotal = run?.costs_total_paise ?? Object.values(costTotals).reduce((a, b) => a + b, 0);

  return (
    <div className="stk-page">
      <Link href="/tests" className="stk-crumb">
        All tests
      </Link>

      {error && (
        <div role="alert" className="stk-gap-s">
          <Panel tone="red">
            <p>{error}</p>
          </Panel>
        </div>
      )}
      {!run ? (
        !error && (
          <Loading label="Loading the test">
            <Skeleton size="title" />
            <Skeleton size="m" />
            <Skeleton size="l" />
          </Loading>
        )
      ) : (
        <div className="stk-stack">
          <div className="stk-head">
            <div>
              <div className="stk-row">
                <StatusChip status={run.status} />
                {run.impl && <span className="stk-label">{implWords(run.impl)}</span>}
              </div>
              <h1 className="stk-gap-s stk-break" title={run.name ? `Saved as ${run.name}` : undefined}>{run.name_label || run.name || run.run_id}</h1>
              {summaryLine && <p className="stk-lede">{summaryLine}</p>}
            </div>
            <LabelChips run={run} />
          </div>

          {scale && (
            <section className="stk-costs-banner" aria-label="Costs were changed in this test">
              <svg viewBox="0 0 24 24" width="28" height="28" aria-hidden="true" focusable="false">
                <path d="M12 3 2.5 20h19L12 3Z" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" />
                <path d="M12 10v5M12 17.5v.01" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
              </svg>
              <p>
                <strong>Costs ×{scale} — not real costs.</strong>{" "}
                Every trading cost in this test was multiplied by {scale}, so its results are not what real costs would give.
              </p>
            </section>
          )}

          {(run.status === "queued" || run.status === "running") && (
            <Panel>
              <div className="stk-row-between">
                <p role="status">{run.status === "queued" ? "Waiting for a free slot…" : "Running… this page updates by itself."}</p>
                <button type="button" onClick={cancel} disabled={cancelling} className="stk-btn" data-tone="warn">
                  {cancelling ? "Cancelling…" : "Cancel test"}
                </button>
              </div>
            </Panel>
          )}

          {run.status === "failed" && (
            <Panel tone="red">
              <h2>What went wrong</h2>
              <p className="stk-mono stk-break">{run.error ?? "no message"}</p>
            </Panel>
          )}

          {extra.logs && (extra.logs.events_log.length > 0 || extra.logs.runner_log.length > 0) && (
            <Panel>
              <h2>Last lines of the log</h2>
              <pre className="stk-pre">{[...extra.logs.runner_log, ...extra.logs.events_log].join("\n")}</pre>
            </Panel>
          )}

          {run.status !== "done" && (
            <Panel>
              <h2>Price and trades</h2>
              <p className="stk-muted">
                {run.status === "queued" || run.status === "running"
                  ? "The chart appears here when the test has finished."
                  : `This test ${run.status === "failed" ? "failed" : "was cancelled"}, so there is no chart.`}
              </p>
            </Panel>
          )}

          {run.status === "done" && (
            <>
              <Panel>
                <StatGrid>
                  <Stat label="Started with" value={rupeesWhole(run.nav_start_paise)} />
                  <Stat label="Ended with" value={rupeesWhole(run.nav_end_paise)} tone={g === null ? undefined : g >= 0 ? "up" : "down"} />
                  <Stat label="Gain or loss" value={pct(g, true)} tone={g === null ? undefined : g >= 0 ? "up" : "down"} />
                  <Stat label="Per year, on average"
                    value={detail?.withheld?.cagr ?? pct(num(m.cagr), true)} />
                  <Stat label="Worst fall from a high" value={pct(run.max_drawdown)} />
                  <Stat label="Trades" value={String(run.trades ?? 0)} />
                  <Stat label="Trades that made money" value={pct(num(m.win_rate))} />
                  <Stat label="Costs paid" value={rupees(run.costs_total_paise)} />
                </StatGrid>
              </Panel>

              <Panel>
                <div ref={chartRef}>
                  <PriceChartLazy load={loadChart} title="Price and trades" headingLevel={2} hideTradesList focusMarkerId={focusId} onMarkerClick={markerClicked} />
                </div>
              </Panel>

              <ResearchPanel runId={id} />

              <Panel>
                <h2>Money over time</h2>
                <EquityChart rows={extra.equity} start={run.nav_start_paise} />
              </Panel>

              <div className="stk-cols-split">
                <section className="stk-section">
                  <h2>Fills ({fillCount.toLocaleString("en-IN")})</h2>
                  {extra.fills.length === 0 ? (
                    <Empty>No orders filled.</Empty>
                  ) : (
                    <Table
                      caption="Orders that filled"
                      head={[
                        { label: "Time (IST)" },
                        { label: "Trade" },
                        { label: "Why" },
                        { label: "Price", num: true },
                        { label: "Order", num: true },
                      ]}
                    >
                      {joined.map(({ fill: f, markerId, why }) => (
                        <tr key={f.fill_id} data-marker-id={markerId ?? undefined} data-active={markerId !== null && markerId === activeId ? "" : undefined}>
                          <td className="stk-label" title={fillWhen(f).hint ?? undefined}>{fillWhen(f).text}</td>
                          <td className={f.side === "BUY" ? "stk-up" : "stk-down"} data-wrap>
                            {f.side === "BUY" ? "Bought" : "Sold"} {f.qty.toLocaleString("en-IN")} {f.tradingsymbol ?? `instrument ${f.instrument_id}`}
                            {chartDrawn && markerId !== null && (
                              <button type="button" className="stk-btn" data-tone="quiet" aria-pressed={markerId === activeId}
                                aria-label={`Show on chart: ${f.side === "BUY" ? "bought" : "sold"} ${f.qty.toLocaleString("en-IN")} at ${f.t_ist.slice(11, 16)}`}
                                onClick={() => showOnChart(markerId)}>
                                Show on chart
                              </button>
                            )}
                            {chartHint(chartDrawn, markerId) && (
                              <span className="stk-muted stk-label"> {chartHint(chartDrawn, markerId)}</span>
                            )}
                          </td>
                          <td data-wrap>
                            {why.text}
                            {why.tag !== null && <> <span className="stk-label" title={why.raw ?? undefined}>({why.tag})</span></>}
                          </td>
                          <td className="num">{rupees(f.price_paise)}</td>
                          <td className="num">{f.order_id}</td>
                        </tr>
                      ))}
                    </Table>
                  )}
                  {extra.fills.some((f) => f.at_close) && (
                    <p className="stk-label stk-gap-s">{fillWhen(extra.fills.find((f) => f.at_close)!).hint}</p>
                  )}
                  {extra.fills.length < fillCount && (
                    <div className="stk-gap-s">
                      <button type="button" onClick={moreFills} disabled={loadingMore} className="stk-btn">
                        {loadingMore ? "Loading…" : `Show more (${extra.fills.length.toLocaleString("en-IN")} of ${fillCount.toLocaleString("en-IN")} shown)`}
                      </button>
                    </div>
                  )}
                </section>

                <Panel>
                  <h2>Where the costs went</h2>
                  {costTotal === 0 ? (
                    <p className="stk-muted">No costs were charged in this test.</p>
                  ) : (
                    <div className="stk-stack-s">
                      <dl className="stk-kv">
                        {Object.entries(costTotals).filter(([, v]) => v !== 0).map(([k, v]) => (
                          <Fragment key={k}>
                            <dt>{COST_NAMES[k] ?? k.replace(/_/g, " ")}</dt>
                            <dd>{rupees(v)}</dd>
                          </Fragment>
                        ))}
                        <div className="stk-kv-total">
                          <span>Total</span>
                          <span className="stk-mono">{rupees(costTotal)}</span>
                        </div>
                      </dl>
                      <p className="stk-fine">
                        Each part is rounded to the paisa; the total is exact, so the parts can differ from it by a few paise.
                      </p>
                    </div>
                  )}
                </Panel>
              </div>

              <MarginByDay rows={extra.margin} equity={extra.equity} />
              <GreeksAtDayEnd rows={extra.greeks} />
              <Settlements rows={extra.settlements} />
              <LabelMeanings labels={run.labels} />
            </>
          )}

          {detail?.manifest && (
            <Panel>
              <h2>For reproducing this run</h2>
              <dl className="stk-kv">
                {Object.entries(detail.manifest)
                  .filter(([k]) => ["engine_build_id", "bundle_sha256", "packs", "duration_ms", "python", "os"].includes(k))
                  .map(([k, v]) => (
                    <Fragment key={k}>
                      <dt title={k}>{detail.manifest_labels?.[k] ?? k}</dt>
                      <dd className="stk-break stk-mono">{Array.isArray(v) ? v.join(", ") : String(v)}</dd>
                    </Fragment>
                  ))}
              </dl>
              <p className="stk-fine stk-break stk-gap-s">Run id {run.run_id}</p>
            </Panel>
          )}
        </div>
      )}
    </div>
  );
}

/** What the `scan` amount is for a row: the whole SPAN requirement (B1/B2), the flat requirement (B5), else the scan risk. */
function scanLabel(step?: string): string {
  if (step === "B1_span_nse_elm" || step === "B2_span_rule_elm") return "SPAN requirement";
  if (step === "B5_flat") return "Flat margin";
  return "Price and volatility risk";
}

/** Margin held by day: the SPAN-lite breakdown when the run has one (margin_daily.csv), else the flat model's
 * day-end amount from the equity rows. */
function MarginByDay({ rows, equity }: { rows: TestMargin[]; equity: TestEquity[] }) {
  const days = Array.from(new Set(rows.map((r) => r.trade_date)));
  const held = equity.filter((e) => (e.margin_paise ?? 0) > 0);
  if (days.length === 0 && held.length === 0) return null;
  return (
    <section className="stk-section">
      <h2>Margin by day</h2>
      {days.length === 0 ? (
        <div className="stk-stack-s">
          <p className="stk-lede">
            This test used the flat margin: a set percentage of each position&apos;s value. Margin held at the end of
            each day:
          </p>
          <Table caption="Margin held at the end of each day" head={[{ label: "Day" }, { label: "Margin held", num: true }]}>
            {held.slice(-30).map((e) => (
              <tr key={e.trade_date}>
                <td>{e.trade_date}</td>
                <td className="num">{rupees(e.margin_paise)}</td>
              </tr>
            ))}
          </Table>
          {held.length > 30 && <p className="stk-fine">The last 30 days with margin held are shown.</p>}
        </div>
      ) : (
        <Table
          caption="Margin held each day, by underlying"
          head={[
            { label: "Day" },
            { label: "Underlying" },
            { label: "How it was priced" },
            { label: "Main amount", num: true },
            { label: "Short-option minimum", num: true },
            { label: "Extreme-loss margin", num: true },
            { label: "Expiry-day and other add-ons", num: true },
            { label: "Total", num: true },
          ]}
        >
          {days.map((d) => {
            const today = rows.filter((r) => r.trade_date === d);
            const total = today.reduce((a, r) => a + r.total_paise, 0);
            return (
              <Fragment key={d}>
                {today.map((r) => (
                  <tr key={`${d}-${r.underlying_id}`}>
                    <td>{d}</td>
                    <td>{r.underlying ?? `underlying ${r.underlying_id}`}</td>
                    <td data-wrap title={marginStep(r.step)?.detail}>
                      {marginStep(r.step)?.text ?? "—"}
                      {r.step && marginStep(r.step)!.text !== r.step && (
                        <span className="sr-only"> ({marginStep(r.step)!.detail})</span>
                      )}
                    </td>
                    <td className="num">
                      {rupees(r.scan_paise)}
                      <div className="stk-label">{scanLabel(r.step)}</div>
                    </td>
                    <td className="num">{rupees(r.somc_paise)}</td>
                    <td className="num">{rupees(r.elm_paise)}</td>
                    <td className="num">{rupees(r.addons_paise)}</td>
                    <td className="num">{rupees(r.total_paise)}</td>
                  </tr>
                ))}
                {today.length > 1 && (
                  <tr className="sub">
                    <td colSpan={7}>Total for {d}</td>
                    <td className="num">{rupees(total)}</td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </Table>
      )}
    </section>
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
    <section className="stk-section">
      <div className="stk-row-between">
        <h2>Greeks at day end</h2>
        {days.length > 1 && (
          <select aria-label="Day" value={day} onChange={(e) => setPicked(e.target.value)} className="stk-field" data-fit data-fig>
            {days.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        )}
      </div>
      <p className="stk-fine stk-gap-s">
        Delta: units of the underlying the positions behave like. Gamma: how much delta changes per ₹1 move. Vega: ₹
        gained or lost per volatility point. Theta: ₹ gained or lost per day from time passing. Positive means long.
      </p>
      <div className="stk-stack stk-gap-top">
        {unders.map((u) => {
          const legs = today.filter((r) => (r.underlying ?? "") === u);
          const priced = legs.filter((r) => r.delta_units != null);
          const sum = (f: (r: TestGreek) => number | null | undefined) => priced.reduce((a, r) => a + (f(r) ?? 0), 0);
          const missing = legs.length - priced.length;
          const spot = legs.find((r) => r.underlying_paise != null)?.underlying_paise ?? null;
          return (
            <Panel key={u}>
              <div className="stk-stack-s">
                <div className="stk-row-between">
                  <h3>{u || "Unknown underlying"}</h3>
                  {spot != null && <span className="stk-label">At {rupees(spot)}</span>}
                </div>
                <StatGrid>
                  <Stat size="s" label="Delta" value={`${signed(sum((r) => r.delta_units), 1)} units`} />
                  {spot != null && (
                    <Stat size="s" label="Delta in ₹" value={signedRupees(sum((r) => r.delta_units) * spot / 100)} />
                  )}
                  <Stat size="s" label="Gamma, per ₹1" value={signed(sum((r) => r.gamma_units_per_rupee), 4)} />
                  <Stat size="s" label="Vega, per point" value={signedRupees(sum((r) => r.vega_rupees_per_point))} />
                  <Stat size="s" label="Theta, per day" value={signedRupees(sum((r) => r.theta_rupees_per_day))} />
                </StatGrid>
                {missing > 0 && (
                  <p className="stk-warn">
                    {missing === 1 ? "One position is" : `${missing} positions are`} left out of these totals: no volatility could be found (see the notes in the table).
                  </p>
                )}
                <p className="stk-fine">Delta, vega and theta are explained in the note at the top of this section.</p>
                <Table
                  caption={`Positions in ${u || "the unknown underlying"}`}
                  head={[
                    { label: "Position" },
                    { label: "Mark", num: true },
                    { label: "Implied volatility", num: true },
                    { label: "Delta (units)", num: true },
                    { label: "Vega (₹ per point)", num: true },
                    { label: "Theta (₹ per day)", num: true },
                  ]}
                >
                  {legs.map((r) => {
                    const name = `${r.qty > 0 ? "Long" : "Short"} ${Math.abs(r.qty).toLocaleString("en-IN")} ${r.tradingsymbol ?? `instrument ${r.instrument_id}`}`;
                    const note = r.kind === "FUT"
                      ? null
                      : r.iv_reason
                        ? `No Greeks: ${IV_REASONS[r.iv_reason] ?? r.iv_reason}.`
                        : null;
                    return (
                      <Fragment key={r.instrument_id}>
                        <tr>
                          <td data-wrap>{name}</td>
                          <td className="num">{rupees(r.mark_paise)}</td>
                          <td className="num">{r.kind === "FUT" || r.iv_reason ? "—" : pct(r.iv ?? 0)}</td>
                          <td className="num">{r.delta_units == null ? "—" : signed(r.delta_units, 1)}</td>
                          <td className="num">{r.kind === "FUT" || r.iv_reason ? "—" : signedRupees(r.vega_rupees_per_point ?? 0)}</td>
                          <td className="num">{r.kind === "FUT" || r.iv_reason ? "—" : signedRupees(r.theta_rupees_per_day ?? 0)}</td>
                        </tr>
                        {(note || (r.expiry_close_estimated && r.kind !== "FUT")) && (
                          <tr>
                            <td colSpan={6} className="stk-label" data-wrap>
                              {note && <span className="stk-warn">{note} </span>}
                              {r.expiry_close_estimated && r.kind !== "FUT" && "Expiry after the test ends: its closing time is estimated."}
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </Table>
              </div>
            </Panel>
          );
        })}
      </div>
    </section>
  );
}

/** Expiry settlements in cash and physical deliveries, with whether NSE's price or an estimate was used. */
function Settlements({ rows }: { rows: TestSettlement[] }) {
  if (rows.length === 0) return null;
  return (
    <section className="stk-section">
      <h2>Expiry settlements</h2>
      <Table
        caption="Expiry settlements"
        head={[
          { label: "Day" },
          { label: "What happened" },
          { label: "Price", num: true },
          { label: "Price source" },
          { label: "Charges", num: true },
        ]}
      >
        {rows.map((s, i) => {
          const name = s.tradingsymbol ?? `instrument ${s.instrument_id}`;
          const what = s.kind === "cash"
            ? `${name} settled in cash`
            : `${s.side === "BUY" ? "Took" : "Gave"} delivery of ${(s.qty ?? 0).toLocaleString("en-IN")} ${name}`;
          return (
            <tr key={`${s.t_ns}-${s.instrument_id}-${i}`}>
              <td>{s.trade_date}</td>
              <td data-wrap>{what}</td>
              <td className="num">{rupees(s.price_paise)}</td>
              <td className={s.source === "fallback" ? "stk-warn" : "stk-label"} data-wrap>
                {s.source === "fallback" ? "Estimated price (NSE had published none)" : "NSE's settlement price"}
              </td>
              <td className="num">{s.charges_paise ? rupees(s.charges_paise) : "—"}</td>
            </tr>
          );
        })}
      </Table>
    </section>
  );
}

/** Each of the run's labels as a sentence: what it means for how far the results can be trusted. */
function LabelMeanings({ labels }: { labels: string[] }) {
  if (labels.length === 0) return null;
  return (
    <Panel>
      <h2>What the labels mean</h2>
      <ul className="stk-bullets">
        {labels.map((l) => <li key={l}>{labelMeaning(l)}</li>)}
      </ul>
    </Panel>
  );
}

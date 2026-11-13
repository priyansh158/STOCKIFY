"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError } from "@/lib/api";
import { getSessionChart } from "@/lib/chart_api";
import { pollDelay } from "@/lib/chart";
import { istWhen, pct, rupees } from "@/lib/format";
import {
  getFills,
  getLogin,
  getMargin,
  getOrders,
  getPositions,
  getSession,
  getTimeline,
  orderStatusText,
  PARITY_HELP,
  parityText,
  sendControl,
  signedRupees,
  timelineText,
  tone,
  type Control,
} from "@/lib/trade";
import type {
  SessionDetailDTO,
  SessionFillDTO,
  SessionMarginDTO,
  SessionOrderDTO,
  SessionPositionDTO,
  SessionStatsDTO,
  SessionTimelineDTO,
  TradeLoginDTO,
} from "@/lib/types";
import { useTicks } from "@/lib/useTicks";
import { PriceChartLazy } from "@/components/PriceChartLazy";
import { Empty, Loading, Panel, Skeleton, Stat, StatGrid, Table, toneClass } from "../ui";
import { CommandMessage, ConfirmDialog, LoginBanner, PauseReasons, StatusChip, useCommandWatch } from "../Commands";

const FAST_MS = 2000; // while RUNNING
const SLOW_MS = 15_000;
const MAX_BACKOFF_MS = 30_000;
const CHART_POLL_MS = 5000; // the chart polls only while the day is open and the tab is visible

type Extra = {
  positions: SessionPositionDTO[];
  positionsNote: string | null;
  orders: SessionOrderDTO[];
  fills: SessionFillDTO[];
  timeline: SessionTimelineDTO[];
  margin: SessionMarginDTO[];
  login: TradeLoginDTO | null;
};
const NO_EXTRA: Extra = { positions: [], positionsNote: null, orders: [], fills: [], timeline: [], margin: [], login: null };

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="stk-section">
      <h2>{title}</h2>
      {children}
    </section>
  );
}

function sideWord(side: string | null | undefined, past = false): string {
  if (side === "BUY") return past ? "Bought" : "Buy";
  if (side === "SELL") return past ? "Sold" : "Sell";
  return side ?? "—";
}

function PositionsTable({ rows, tickOf }: { rows: SessionPositionDTO[]; tickOf: (p: SessionPositionDTO) => number | null }) {
  return (
    <Table
      caption="Positions held now"
      head={[
        { label: "Instrument" },
        { label: "Holding", num: true },
        { label: "Average price", num: true },
        { label: "Price now", num: true },
        { label: "Made or lost so far", num: true },
        { label: "Price source" },
      ]}
    >
      {rows.map((p) => {
        const ltp = tickOf(p);
        const livePaise = ltp === null ? null : Math.round(ltp * 100);
        const mark = livePaise ?? p.mark_paise ?? null;
        const unrealised =
          livePaise !== null && p.avg_price_paise !== null && p.avg_price_paise !== undefined
            ? (livePaise - p.avg_price_paise) * p.qty
            : p.unrealised_paise ?? null;
        return (
          <tr key={`${p.instrument_id}-${p.product ?? ""}`}>
            <td>{p.tradingsymbol ?? `Instrument ${p.instrument_id}`}</td>
            <td className="num">{p.qty > 0 ? `${p.qty} held` : `${Math.abs(p.qty)} short`}</td>
            <td className="num">{rupees(p.avg_price_paise)}</td>
            <td className="num">{rupees(mark)}</td>
            <td className={`num ${toneClass(tone(unrealised)) ?? ""}`}>{signedRupees(unrealised, rupees)}</td>
            <td className="stk-label">
              {livePaise !== null ? "Live" : p.source === "eod" ? "End of the last day" : "Worked out from today's fills"}
            </td>
          </tr>
        );
      })}
    </Table>
  );
}

function OrdersTable({ rows, name }: { rows: SessionOrderDTO[]; name: (id: number | null | undefined) => string }) {
  return (
    <Table
      caption="Paper orders"
      head={[
        { label: "Order" },
        { label: "Filled", num: true },
        { label: "Average price", num: true },
        { label: "Status" },
      ]}
    >
      {rows.map((o) => (
        <tr key={o.order_id}>
          <td data-wrap>
            {sideWord(o.side)} {o.qty ?? "—"} {name(o.instrument_id)}
            {o.price_paise ? <div className="stk-label">Limit {rupees(o.price_paise)}</div> : null}
          </td>
          <td className="num">{o.filled_qty == null ? "—" : `${o.filled_qty} of ${o.qty ?? "—"}`}</td>
          <td className="num">{rupees(o.avg_price_paise)}</td>
          <td data-wrap>
            {orderStatusText(o.status)}
            {o.reason && <div className="stk-warn">{o.reason}</div>}
          </td>
        </tr>
      ))}
    </Table>
  );
}

function FillsTable({ rows, name }: { rows: SessionFillDTO[]; name: (id: number | null | undefined) => string }) {
  return (
    <Table
      caption="Trades that went through"
      head={[{ label: "Trade" }, { label: "Price", num: true }, { label: "Time (IST)", num: true }]}
    >
      {rows.map((f) => (
        <tr key={f.fill_id}>
          <td data-wrap>
            {sideWord(f.side, true)} {f.qty ?? "—"} {name(f.instrument_id)}
          </td>
          <td className="num">{rupees(f.price_paise)}</td>
          <td className="num">
            {f.t_ns == null ? "—" : new Date(f.t_ns / 1e6).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false })}
          </td>
        </tr>
      ))}
    </Table>
  );
}

function StatsTable({ all, full }: { all: SessionStatsDTO; full: SessionStatsDTO }) {
  const rows: { label: string; get: (s: SessionStatsDTO) => { text: string; t?: "up" | "down" } }[] = [
    { label: "Days counted", get: (s) => ({ text: String(s.days) }) },
    { label: "Total made or lost", get: (s) => ({ text: signedRupees(s.pnl_paise, rupees), t: tone(s.pnl_paise) }) },
    { label: "Days it made money", get: (s) => ({ text: String(s.win_days) }) },
    { label: "Days it lost money", get: (s) => ({ text: String(s.loss_days) }) },
    { label: "Best day", get: (s) => ({ text: signedRupees(s.best_day_paise, rupees), t: tone(s.best_day_paise) }) },
    { label: "Worst day", get: (s) => ({ text: signedRupees(s.worst_day_paise, rupees), t: tone(s.worst_day_paise) }) },
    { label: "Biggest fall from a high point", get: (s) => ({ text: rupees(s.max_drawdown_paise) }) },
  ];
  const cell = (c: { text: string; t?: "up" | "down" }, empty: boolean) => (
    <td className={`num ${toneClass(c.t) ?? ""}`}>{empty ? "—" : c.text}</td>
  );
  return (
    <Table caption="Results, all days and full-data days" head={[{ label: "Measure" }, { label: "All days", num: true }, { label: "Days with nearly complete data", num: true }]}>
      {rows.map((r) => (
        <tr key={r.label}>
          <td>{r.label}</td>
          {cell(r.get(all), all.days === 0 && r.label !== "Days counted")}
          {cell(r.get(full), full.days === 0 && r.label !== "Days counted")}
        </tr>
      ))}
    </Table>
  );
}

function MarginCard({ rows }: { rows: SessionMarginDTO[] }) {
  const last = rows.length ? rows[rows.length - 1] : null;
  if (!last) return <Empty>No margin check yet. Kite is asked while positions are open.</Empty>;
  const gap = last.gap_paise ?? null;
  return (
    <Panel>
      <div className="stk-stack-s">
        <StatGrid cols={2}>
          <Stat label="Kite says it needs" value={rupees(last.kite_total_paise)} />
          <Stat label="Our model says" value={rupees(last.ours_total_paise)} />
        </StatGrid>
        <StatGrid cols={2}>
          <Stat
            size="s"
            label="Difference (Kite minus ours)"
            value={gap === null ? "—" : `${gap > 0 ? "+" : ""}${rupees(gap)}${last.gap_ppm != null ? ` (${pct(last.gap_ppm / 1_000_000, true)})` : ""}`}
          />
        </StatGrid>
        <p className="stk-fine">
          Margin is the money a broker holds back while positions are open. Checked {istWhen(last.wall_ist)}.
        </p>
        {last.error && <p className="stk-down">{last.error}</p>}
      </div>
    </Panel>
  );
}

function detailText(d: unknown): string | null {
  if (d === null || d === undefined) return null;
  if (typeof d === "string") return d;
  if (typeof d !== "object") return String(d);
  const o = d as Record<string, unknown>;
  const bits: string[] = [];
  for (const k of ["reason", "reasons", "why", "cause", "error", "state", "day"]) {
    const v = o[k];
    if (v === undefined || v === null) continue;
    bits.push(Array.isArray(v) ? v.join(", ") : String(v));
  }
  return bits.length ? bits.join("; ") : null;
}

export default function SessionPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [detail, setDetail] = useState<SessionDetailDTO | null>(null);
  const [extra, setExtra] = useState<Extra>(NO_EXTRA);
  const [error, setError] = useState<string | null>(null);
  const [missing, setMissing] = useState(false);
  const [confirmStop, setConfirmStop] = useState(false);
  const [sending, setSending] = useState(false);
  const [version, setVersion] = useState(0);
  const refreshNow = useCallback(() => setVersion((v) => v + 1), []);
  const cmd = useCommandWatch(refreshNow);
  const { watch, fail } = cmd;

  const status = detail?.session.status ?? null;
  const running = status === "RUNNING";

  useEffect(() => {
    let stop = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let fails = 0;
    async function tick() {
      let next = SLOW_MS;
      try {
        const d = await getSession(id);
        if (stop) return;
        setDetail(d);
        setError(null);
        fails = 0;
        const [positions, orders, fills, timeline, margin, login] = await Promise.all([
          getPositions(id).then((p) => ({ p, note: null as string | null })).catch((e) => ({
            p: [] as SessionPositionDTO[],
            note: e instanceof Error ? e.message : "Positions are not available right now.",
          })),
          getOrders(id).catch(() => [] as SessionOrderDTO[]),
          getFills(id).catch(() => [] as SessionFillDTO[]),
          getTimeline(id).catch(() => [] as SessionTimelineDTO[]),
          getMargin(id).catch(() => [] as SessionMarginDTO[]),
          getLogin().catch(() => null),
        ]);
        if (stop) return;
        setExtra({ positions: positions.p, positionsNote: positions.note, orders, fills, timeline, margin, login });
        next = d.session.status === "RUNNING" ? FAST_MS : d.session.status === "STOPPED" ? 60_000 : SLOW_MS;
      } catch (e) {
        if (stop) return;
        if (e instanceof ApiError && e.status === 404) {
          setMissing(true);
          return;
        }
        fails += 1;
        setError(`${e instanceof Error ? e.message : String(e)} (retrying)`);
        next = Math.min(MAX_BACKOFF_MS, FAST_MS * 2 ** fails);
      }
      timer = setTimeout(tick, next);
    }
    tick();
    return () => {
      stop = true;
      clearTimeout(timer);
    };
  }, [id, version]);

  const tokens = useMemo(
    () => extra.positions.map((p) => p.instrument_token).filter((t): t is number => typeof t === "number"),
    [extra.positions],
  );
  const ticks = useTicks(tokens);

  const names = useMemo(() => {
    const m = new Map<number, string>();
    for (const p of extra.positions) if (p.tradingsymbol) m.set(p.instrument_id, p.tradingsymbol);
    return m;
  }, [extra.positions]);
  const name = useCallback((iid: number | null | undefined) => (iid == null ? "" : names.get(iid) ?? `(instrument ${iid})`), [names]);

  // The chart polls only while its day is open (the API says so in each answer) and the tab is visible.
  const [chartLive, setChartLive] = useState(false);
  const [tabHidden, setTabHidden] = useState(false);
  const wantedDay = useRef<string | undefined>(undefined);
  useEffect(() => {
    const sync = () => setTabHidden(document.hidden);
    sync();
    document.addEventListener("visibilitychange", sync);
    return () => document.removeEventListener("visibilitychange", sync);
  }, []);
  const loadChart = useCallback<NonNullable<React.ComponentProps<typeof PriceChartLazy>["load"]>>(
    async (_range, opts) => {
      const asked = opts?.trade_date;
      wantedDay.current = asked ?? wantedDay.current; // a poll repeats the day already shown
      const model = await getSessionChart(id, opts);
      if (asked === undefined || asked === wantedDay.current) setChartLive(Boolean(model?.live)); // not for a day the picker has left
      return model;
    },
    [id],
  );

  async function control(verb: Control) {
    setSending(true);
    try {
      const { cmd_id } = await sendControl(id, verb);
      watch(cmd_id);
    } catch (e) {
      fail(`${verb.charAt(0).toUpperCase() + verb.slice(1)} could not be sent: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSending(false);
    }
  }

  const threshold = detail ? Number(detail.coverage_threshold) : NaN;
  const login = extra.login;
  const loginDetail = login
    ? `Log in between ${login.window_local[0]} and ${login.window_local[1]} on ${login.day} (your time). ` +
      `The last usable login is ${login.last_usable_local}; the market opens at ${login.market_open_local}.`
    : undefined;

  const tickOf = (p: SessionPositionDTO): number | null =>
    p.instrument_token != null ? ticks?.[String(p.instrument_token)]?.ltp ?? null : null;

  return (
    <div className="stk-page">
      <Link href="/trade" className="stk-crumb">
        Back to paper trading
      </Link>

      {missing && (
        <Panel tone="red">
          <p role="alert">There is no paper session called {id}.</p>
        </Panel>
      )}
      {error && (
        <div role="alert" className="stk-gap-s">
          <Panel tone="red">
            <p>Could not reach the API: {error}</p>
          </Panel>
        </div>
      )}

      {!detail && !missing && (
        <Loading label="Loading the session">
          <Skeleton size="title" />
          <Skeleton size="m" />
          <Skeleton size="l" />
        </Loading>
      )}

      {detail && (
        <div className="stk-stack">
          <div className="stk-head">
            <div>
              <div className="stk-row">
                <StatusChip status={detail.session.status} />
                <span className="stk-label" aria-live="polite">
                  Created {istWhen(detail.session.created_ist)}
                  {running ? ". Live, updating." : ""}
                </span>
              </div>
              <h1 className="stk-gap-s">{detail.session.name || detail.session.session_id}</h1>
              <PauseReasons reasons={detail.session.pause_reasons} />
            </div>
            {detail.session.status !== "STOPPED" && (
              <div className="stk-head-actions">
                {status === "PAUSED" ? (
                  <button type="button" disabled={sending || cmd.pending} onClick={() => control("resume")} className="stk-btn" data-tone="primary">
                    Resume
                  </button>
                ) : (
                  <button type="button" disabled={sending || cmd.pending} onClick={() => control("pause")} className="stk-btn">
                    Pause
                  </button>
                )}
                <button type="button" disabled={sending || cmd.pending} onClick={() => setConfirmStop(true)} className="stk-btn" data-tone="danger">
                  Stop for good
                </button>
              </div>
            )}
          </div>
          <CommandMessage message={cmd.message} failed={cmd.failed} pending={cmd.pending} />

          {login?.waiting_for_login ? (
            <LoginBanner detail={loginDetail} />
          ) : (
            loginDetail && <p className="stk-label">Next Kite login reminder: {loginDetail}</p>
          )}

          <Panel>
            <StatGrid>
              <Stat label="Made or lost today" value={signedRupees(detail.session.today_pnl_paise, rupees)} tone={tone(detail.session.today_pnl_paise)} />
              <Stat label="Positions held" value={detail.session.open_positions == null ? "—" : String(detail.session.open_positions)} />
              <Stat label="Market data received today" value={detail.session.coverage_today == null ? "—" : pct(detail.session.coverage_today)} />
              <Stat size="s" plain label="Last replay check" value={parityText(detail.session.parity_last)} />
            </StatGrid>
            <p className="stk-fine stk-gap-s">Last replay check: {PARITY_HELP}</p>
          </Panel>

          <Panel>
            <PriceChartLazy
              load={loadChart}
              title="Price and trades"
              headingLevel={2}
              paperBadge
              pollMs={pollDelay(chartLive, tabHidden, CHART_POLL_MS) ?? undefined}
            />
          </Panel>

          <Section title="What it holds now">
            {extra.positionsNote ? (
              <Empty>Positions cannot be shown right now: {extra.positionsNote}</Empty>
            ) : extra.positions.length === 0 ? (
              <Empty>It holds nothing at the moment.</Empty>
            ) : (
              <PositionsTable rows={extra.positions} tickOf={tickOf} />
            )}
          </Section>

          <div className="stk-cols-2">
            <Section title="Orders it placed (paper)">
              {extra.orders.length === 0 ? <Empty>No orders yet today.</Empty> : <OrdersTable rows={extra.orders} name={name} />}
            </Section>
            <Section title="Trades that went through">
              {extra.fills.length === 0 ? <Empty>No trades yet today.</Empty> : <FillsTable rows={extra.fills} name={name} />}
            </Section>
          </div>

          <div className="stk-cols-2">
            <Section title="Margin: Kite against our model">
              <MarginCard rows={extra.margin} />
            </Section>
            <Section title="Results">
              <StatsTable all={detail.stats_all} full={detail.stats_full_coverage} />
              <p className="stk-fine stk-gap-s">
                The second column counts only days where at least{" "}
                {Number.isNaN(threshold) ? detail.coverage_threshold : pct(threshold)} of the market data arrived.
                {detail.has_coverage_holes ? " Some days had gaps, so it covers fewer days than the first." : ""}
              </p>
            </Section>
          </div>

          <Section title="What happened, in order">
            {extra.timeline.length === 0 ? (
              <Empty>Nothing recorded yet.</Empty>
            ) : (
              <Table caption="Session timeline, newest first" head={[{ label: "When (IST)" }, { label: "What" }, { label: "Details" }]}>
                {[...extra.timeline].reverse().map((t, i) => (
                  <tr key={`${t.wall_ns}-${i}`}>
                    <td className="stk-label">{istWhen(t.wall_ist)}</td>
                    <td>{timelineText(t.kind)}</td>
                    <td data-wrap className="stk-muted">{detailText(t.detail) ?? ""}</td>
                  </tr>
                ))}
              </Table>
            )}
          </Section>
        </div>
      )}

      <ConfirmDialog
        open={confirmStop}
        title="Stop this session for good?"
        body="It will stop trading and cannot be started again. Positions it holds stay as they are. To keep it but halt trading for now, use Pause instead."
        confirmLabel="Yes, stop it"
        onConfirm={() => {
          setConfirmStop(false);
          void control("stop");
        }}
        onCancel={() => setConfirmStop(false)}
      />
    </div>
  );
}

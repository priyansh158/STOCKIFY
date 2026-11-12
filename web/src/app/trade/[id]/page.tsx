"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useMemo, useState } from "react";
import { ApiError } from "@/lib/api";
import { istWhen, pct, rupees } from "@/lib/format";
import {
  getFills,
  getLogin,
  getMargin,
  getOrders,
  getPositions,
  getSession,
  getTimeline,
  num,
  sendControl,
  signedRupees,
  str,
  timelineText,
  tone,
  type Control,
  type Row,
} from "@/lib/trade";
import type {
  SessionDetailDTO,
  SessionMarginDTO,
  SessionPositionDTO,
  SessionStatsDTO,
  SessionTimelineDTO,
  TradeLoginDTO,
} from "@/lib/types";
import { useTicks } from "@/lib/useTicks";
import { Empty, Panel, Skeleton, Stat, Table } from "../ui";
import { CommandMessage, ConfirmDialog, LoginBanner, PauseReasons, StatusChip, useCommandWatch } from "../Commands";

const FAST_MS = 2000; // while RUNNING
const SLOW_MS = 15_000;
const MAX_BACKOFF_MS = 30_000;

type Extra = {
  positions: SessionPositionDTO[];
  positionsNote: string | null;
  orders: Row[];
  fills: Row[];
  timeline: SessionTimelineDTO[];
  margin: SessionMarginDTO[];
  login: TradeLoginDTO | null;
};
const NO_EXTRA: Extra = { positions: [], positionsNote: null, orders: [], fills: [], timeline: [], margin: [], login: null };

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="min-w-0">
      <h2 className="mb-2 text-sm font-semibold text-fg-muted">{title}</h2>
      {children}
    </section>
  );
}

function sideWord(side: string | null, past = false): string {
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
        const t = tone(unrealised);
        return (
          <tr key={`${p.instrument_id}-${p.product ?? ""}`}>
            <td className="font-medium">{p.tradingsymbol ?? `Instrument ${p.instrument_id}`}</td>
            <td className="num">{p.qty > 0 ? `${p.qty} held` : `${Math.abs(p.qty)} short`}</td>
            <td className="num">{rupees(p.avg_price_paise)}</td>
            <td className="num">{rupees(mark)}</td>
            <td className={`num ${t === "up" ? "text-green" : t === "down" ? "text-red-soft" : ""}`}>{signedRupees(unrealised, rupees)}</td>
            <td className="text-fg-muted">
              {livePaise !== null ? "Live" : p.source === "eod" ? "End of the last day" : "Worked out from today's fills"}
            </td>
          </tr>
        );
      })}
    </Table>
  );
}

function OrdersTable({ rows, name }: { rows: Row[]; name: (id: number | null) => string }) {
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
      {rows.map((o, i) => {
        const qty = num(o.qty);
        const filled = num(o.filled_qty);
        const limit = num(o.price_paise);
        const reason = str(o.reason);
        return (
          <tr key={String(o.order_id ?? i)}>
            <td>
              <span className="font-medium">
                {sideWord(str(o.side))} {qty ?? "—"} {name(num(o.instrument_id))}
              </span>
              {limit ? <div className="stk-label">Limit {rupees(limit)}</div> : null}
            </td>
            <td className="num">{filled === null ? "—" : `${filled} of ${qty ?? "—"}`}</td>
            <td className="num">{rupees(num(o.avg_price_paise))}</td>
            <td>
              {str(o.status) ?? "—"}
              {reason && <div className="text-amber">{reason}</div>}
            </td>
          </tr>
        );
      })}
    </Table>
  );
}

function FillsTable({ rows, name }: { rows: Row[]; name: (id: number | null) => string }) {
  return (
    <Table
      caption="Trades that went through"
      head={[{ label: "Trade" }, { label: "Price", num: true }, { label: "Time (IST)", num: true }]}
    >
      {rows.map((f, i) => {
        const t = num(f.t_ns);
        return (
          <tr key={String(f.fill_id ?? i)}>
            <td className="font-medium">
              {sideWord(str(f.side), true)} {num(f.qty) ?? "—"} {name(num(f.instrument_id))}
            </td>
            <td className="num">{rupees(num(f.price_paise))}</td>
            <td className="num">
              {t === null ? "—" : new Date(t / 1e6).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false })}
            </td>
          </tr>
        );
      })}
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
    <td className={`num ${c.t === "up" ? "text-green" : c.t === "down" ? "text-red-soft" : ""}`}>{empty ? "—" : c.text}</td>
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
      <div className="grid grid-cols-2 gap-3">
        <Stat label="Kite says it needs" value={rupees(last.kite_total_paise)} />
        <Stat label="Our model says" value={rupees(last.ours_total_paise)} />
        <div className="col-span-2">
          <Stat
            label="Difference (Kite minus ours)"
            value={gap === null ? "—" : `${gap > 0 ? "+" : ""}${rupees(gap)}${last.gap_ppm != null ? ` (${pct(last.gap_ppm / 1_000_000, true)})` : ""}`}
          />
        </div>
      </div>
      <p className="mt-2 stk-label">
        Margin is the money a broker holds back while positions are open. Checked {istWhen(last.wall_ist)}.
      </p>
      {last.error && <p className="mt-1 text-sm text-red-soft">{last.error}</p>}
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
          getOrders(id).catch(() => [] as Row[]),
          getFills(id).catch(() => [] as Row[]),
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
  const name = useCallback((iid: number | null) => (iid === null ? "" : names.get(iid) ?? `(instrument ${iid})`), [names]);

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
    <div className="mx-auto max-w-6xl px-4 py-8 sm:py-10">
      <div className="mb-4 flex items-center justify-between">
        <Link href="/trade" className="stk-btn" data-tone="quiet">
          Back to paper trading
        </Link>
        <div className="stk-label" aria-live="polite">
          {running ? "Live, updating" : ""}
        </div>
      </div>

      {missing && (
        <Panel tone="red">
          <p role="alert" className="text-sm text-red-soft">
            There is no paper session called {id}.
          </p>
        </Panel>
      )}
      {error && (
        <div className="mb-4" role="alert">
          <Panel tone="red">
            <p className="text-sm text-red-soft">Could not reach the API: {error}</p>
          </Panel>
        </div>
      )}

      {!detail && !missing && (
        <div className="flex flex-col gap-3" aria-busy="true" aria-label="Loading the session">
          <Skeleton className="h-8 w-1/3" />
          <Skeleton className="h-20" />
          <Skeleton className="h-40" />
        </div>
      )}

      {detail && (
        <div className="flex flex-col gap-8">
          <header className="flex flex-wrap items-start justify-between gap-4">
            <div className="min-w-0">
              <div className="mb-1 flex flex-wrap items-center gap-3">
                <StatusChip status={detail.session.status} />
                <span className="stk-label">Created {istWhen(detail.session.created_ist)}</span>
              </div>
              <h1 className="text-2xl font-bold text-green">{detail.session.name || detail.session.session_id}</h1>
              <PauseReasons reasons={detail.session.pause_reasons} />
            </div>
            {detail.session.status !== "STOPPED" && (
              <div className="flex gap-2">
                {status === "PAUSED" ? (
                  <button type="button" disabled={sending} onClick={() => control("resume")} className="stk-btn">
                    Resume
                  </button>
                ) : (
                  <button type="button" disabled={sending} onClick={() => control("pause")} className="stk-btn">
                    Pause
                  </button>
                )}
                <button type="button" disabled={sending} onClick={() => setConfirmStop(true)} className="stk-btn" data-tone="danger">
                  Stop for good
                </button>
              </div>
            )}
          </header>
          <CommandMessage message={cmd.message} failed={cmd.failed} />

          {login?.waiting_for_login ? (
            <LoginBanner detail={loginDetail} />
          ) : (
            loginDetail && <p className="stk-label">Next Kite login reminder: {loginDetail}</p>
          )}

          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Panel>
              <Stat label="Made or lost today" value={signedRupees(detail.session.today_pnl_paise, rupees)} tone={tone(detail.session.today_pnl_paise)} />
            </Panel>
            <Panel>
              <Stat label="Positions held" value={detail.session.open_positions == null ? "—" : String(detail.session.open_positions)} />
            </Panel>
            <Panel>
              <Stat label="Market data received today" value={detail.session.coverage_today == null ? "—" : pct(detail.session.coverage_today)} />
            </Panel>
            <Panel>
              <Stat label="Last replay check" value={detail.session.parity_last ?? "—"} />
            </Panel>
          </div>

          <Section title="What it holds now">
            {extra.positionsNote ? (
              <Empty>Positions cannot be shown right now: {extra.positionsNote}</Empty>
            ) : extra.positions.length === 0 ? (
              <Empty>It holds nothing at the moment.</Empty>
            ) : (
              <PositionsTable rows={extra.positions} tickOf={tickOf} />
            )}
          </Section>

          <div className="grid gap-8 lg:grid-cols-2">
            <Section title="Orders it placed (paper)">
              {extra.orders.length === 0 ? <Empty>No orders yet today.</Empty> : <OrdersTable rows={extra.orders} name={name} />}
            </Section>
            <Section title="Trades that went through">
              {extra.fills.length === 0 ? <Empty>No trades yet today.</Empty> : <FillsTable rows={extra.fills} name={name} />}
            </Section>
          </div>

          <div className="grid gap-8 lg:grid-cols-2">
            <Section title="Margin: Kite against our model">
              <MarginCard rows={extra.margin} />
            </Section>
            <Section title="Results">
              <StatsTable all={detail.stats_all} full={detail.stats_full_coverage} />
              <p className="stk-label mt-2">
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
                    <td className="whitespace-nowrap">{istWhen(t.wall_ist)}</td>
                    <td className="font-medium">{timelineText(t.kind)}</td>
                    <td className="break-words text-fg-muted">{detailText(t.detail) ?? ""}</td>
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

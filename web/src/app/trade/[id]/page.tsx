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
import { buttonStyle, Card, Stat } from "../../tests/ui";
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
    <section className="mt-8">
      <h2 className="stk-mono-tight mb-3" style={{ color: "var(--color-fg-muted)" }}>{title}</h2>
      {children}
    </section>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <Card>
      <p className="py-4 text-center text-sm" style={{ color: "var(--color-fg-muted)" }}>{children}</p>
    </Card>
  );
}

function sideWord(side: string | null, past = false): string {
  if (side === "BUY") return past ? "Bought" : "Buy";
  if (side === "SELL") return past ? "Sold" : "Sell";
  return side ?? "—";
}

function PositionCard({ p, ltp }: { p: SessionPositionDTO; ltp: number | null }) {
  const livePaise = ltp === null ? null : Math.round(ltp * 100);
  const mark = livePaise ?? p.mark_paise ?? null;
  const unrealised =
    livePaise !== null && p.avg_price_paise !== null && p.avg_price_paise !== undefined
      ? (livePaise - p.avg_price_paise) * p.qty
      : p.unrealised_paise ?? null;
  return (
    <li>
      <Card>
        <div className="flex items-baseline justify-between gap-2">
          <span className="font-medium">{p.tradingsymbol ?? `Instrument ${p.instrument_id}`}</span>
          <span className="text-sm" style={{ color: "var(--color-fg-muted)" }}>
            {p.qty > 0 ? `Holding ${p.qty}` : `Short ${Math.abs(p.qty)}`}
          </span>
        </div>
        <div className="mt-3 grid grid-cols-2 gap-3">
          <Stat label="Bought or sold at (avg)" value={rupees(p.avg_price_paise)} />
          <Stat label={livePaise !== null ? "Price now (live)" : "Last known price"} value={rupees(mark)} />
          <div className="col-span-2">
            <Stat label="Made or lost so far on this" value={signedRupees(unrealised, rupees)} tone={tone(unrealised)} />
          </div>
        </div>
        {livePaise === null && (
          <p className="mt-2 text-xs" style={{ color: "var(--color-fg-muted)" }}>
            {p.source === "eod" ? "From the end of the last day." : "Worked out from today's fills."}
          </p>
        )}
      </Card>
    </li>
  );
}

function OrderCard({ o, name }: { o: Row; name: (id: number | null) => string }) {
  const side = str(o.side);
  const qty = num(o.qty);
  const filled = num(o.filled_qty);
  const status = str(o.status);
  const reason = str(o.reason);
  const limit = num(o.price_paise);
  return (
    <li>
      <Card>
        <div className="flex items-baseline justify-between gap-2">
          <span className="font-medium">
            {sideWord(side)} {qty ?? "—"} {name(num(o.instrument_id))}
          </span>
          <span className="text-sm" style={{ color: "var(--color-fg-muted)" }}>{status ?? "—"}</span>
        </div>
        <div className="mt-2 grid grid-cols-2 gap-3">
          <Stat label="Filled" value={filled === null ? "—" : `${filled} of ${qty ?? "—"}`} />
          <Stat label="Average price" value={rupees(num(o.avg_price_paise))} />
          {limit ? <Stat label="Limit price" value={rupees(limit)} /> : null}
          {str(o.type) ? <Stat label="Kind" value={str(o.type) as string} /> : null}
        </div>
        {reason && <p className="mt-2 text-sm" style={{ color: "var(--color-amber)" }}>{reason}</p>}
      </Card>
    </li>
  );
}

function FillCard({ f, name }: { f: Row; name: (id: number | null) => string }) {
  const t = num(f.t_ns);
  return (
    <li>
      <Card>
        <div className="flex items-baseline justify-between gap-2">
          <span className="font-medium">
            {sideWord(str(f.side), true)} {num(f.qty) ?? "—"} {name(num(f.instrument_id))}
          </span>
          <span className="stk-mono text-sm">{rupees(num(f.price_paise))}</span>
        </div>
        {t !== null && (
          <p className="mt-1 text-xs" style={{ color: "var(--color-fg-muted)" }}>
            {new Date(t / 1e6).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false })} IST
          </p>
        )}
      </Card>
    </li>
  );
}

function StatsCard({ s }: { s: SessionStatsDTO }) {
  if (s.days === 0) return <Empty>No finished days yet.</Empty>;
  return (
    <Card>
      <div className="grid grid-cols-2 gap-3">
        <div className="col-span-2">
          <Stat label={`Total made or lost over ${s.days} ${s.days === 1 ? "day" : "days"}`} value={signedRupees(s.pnl_paise, rupees)} tone={tone(s.pnl_paise)} />
        </div>
        <Stat label="Days it made money" value={String(s.win_days)} />
        <Stat label="Days it lost money" value={String(s.loss_days)} />
        <Stat label="Best day" value={signedRupees(s.best_day_paise, rupees)} tone={tone(s.best_day_paise)} />
        <Stat label="Worst day" value={signedRupees(s.worst_day_paise, rupees)} tone={tone(s.worst_day_paise)} />
        <div className="col-span-2">
          <Stat label="Biggest fall from a high point" value={rupees(s.max_drawdown_paise)} />
        </div>
      </div>
    </Card>
  );
}

function MarginCard({ rows }: { rows: SessionMarginDTO[] }) {
  const last = rows.length ? rows[rows.length - 1] : null;
  if (!last) return <Empty>No margin check yet. Kite is asked while positions are open.</Empty>;
  const gap = last.gap_paise ?? null;
  return (
    <Card>
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
      <p className="mt-2 text-xs" style={{ color: "var(--color-fg-muted)" }}>
        Margin is the money a broker holds back while positions are open. Checked {istWhen(last.wall_ist)}.
      </p>
      {last.error && <p className="mt-1 text-sm" style={{ color: "var(--color-red-soft)" }}>{last.error}</p>}
    </Card>
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
  return bits.length ? bits.join(" · ") : null;
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

  return (
    <div className="mx-auto max-w-2xl px-4 py-8 sm:py-12">
      <div className="mb-6 flex items-center justify-between">
        <Link href="/trade" className="stk-mono-tight flex min-h-11 items-center" style={{ color: "var(--color-fg-muted)" }}>
          ← Paper trading
        </Link>
        <div className="stk-mono-tight" style={{ color: "var(--color-fg-muted)" }} aria-live="polite">
          {running ? "Live, updating" : ""}
        </div>
      </div>

      {missing && (
        <Card tone="red">
          <p role="alert" className="text-sm" style={{ color: "var(--color-red-soft)" }}>
            There is no paper session called {id}.
          </p>
        </Card>
      )}
      {error && (
        <div className="mb-4" role="alert">
          <Card tone="red">
            <p className="text-sm" style={{ color: "var(--color-red-soft)" }}>Could not reach the API: {error}</p>
          </Card>
        </div>
      )}

      {detail && (
        <>
          <div className="mb-1 flex flex-wrap items-center gap-2">
            <StatusChip status={detail.session.status} />
            <span className="stk-mono-tight text-xs" style={{ color: "var(--color-fg-muted)" }}>
              Created {istWhen(detail.session.created_ist)}
            </span>
          </div>
          <h1 className="stk-mono text-2xl font-bold sm:text-3xl" style={{ color: "var(--color-green)" }}>
            {detail.session.name || detail.session.session_id}
          </h1>
          <PauseReasons reasons={detail.session.pause_reasons} />

          <div className="mt-4 grid grid-cols-2 gap-3">
            <Card>
              <Stat label="Made or lost today" value={signedRupees(detail.session.today_pnl_paise, rupees)} tone={tone(detail.session.today_pnl_paise)} />
            </Card>
            <Card>
              <Stat label="Market data received today" value={detail.session.coverage_today == null ? "—" : pct(detail.session.coverage_today)} />
            </Card>
          </div>

          {login?.waiting_for_login ? (
            <div className="mt-4">
              <LoginBanner detail={loginDetail} />
            </div>
          ) : (
            loginDetail && (
              <div className="mt-4">
                <Card>
                  <p className="text-xs" style={{ color: "var(--color-fg-muted)" }}>
                    Next Kite login reminder: {loginDetail}
                  </p>
                </Card>
              </div>
            )
          )}

          {detail.session.status !== "STOPPED" && (
            <div className="mt-4 flex flex-col gap-2">
              <div className="flex flex-col gap-2 sm:flex-row">
                {status === "PAUSED" ? (
                  <button type="button" disabled={sending} onClick={() => control("resume")}
                    className="stk-mono-tight min-h-11 flex-1 rounded-md border px-4 py-2 disabled:opacity-60" style={buttonStyle}>
                    Resume
                  </button>
                ) : (
                  <button type="button" disabled={sending} onClick={() => control("pause")}
                    className="stk-mono-tight min-h-11 flex-1 rounded-md border px-4 py-2 disabled:opacity-60" style={buttonStyle}>
                    Pause
                  </button>
                )}
                <button type="button" disabled={sending} onClick={() => setConfirmStop(true)}
                  className="stk-mono-tight min-h-11 flex-1 rounded-md border px-4 py-2 disabled:opacity-60"
                  style={{ borderColor: "var(--color-red)", color: "var(--color-red-soft)", background: "rgba(255,68,68,0.10)" }}>
                  Stop for good
                </button>
              </div>
              <CommandMessage message={cmd.message} failed={cmd.failed} />
            </div>
          )}
          {detail.session.status === "STOPPED" && <CommandMessage message={cmd.message} failed={cmd.failed} />}

          <Section title="What it holds now">
            {extra.positionsNote ? (
              <Empty>Positions cannot be shown right now: {extra.positionsNote}</Empty>
            ) : extra.positions.length === 0 ? (
              <Empty>It holds nothing at the moment.</Empty>
            ) : (
              <ul className="flex flex-col gap-3">
                {extra.positions.map((p) => (
                  <PositionCard
                    key={`${p.instrument_id}-${p.product ?? ""}`}
                    p={p}
                    ltp={p.instrument_token != null ? ticks?.[String(p.instrument_token)]?.ltp ?? null : null}
                  />
                ))}
              </ul>
            )}
          </Section>

          <Section title="Orders it placed (paper)">
            {extra.orders.length === 0 ? (
              <Empty>No orders yet today.</Empty>
            ) : (
              <ul className="flex flex-col gap-3">
                {extra.orders.map((o, i) => (
                  <OrderCard key={String(o.order_id ?? i)} o={o} name={name} />
                ))}
              </ul>
            )}
          </Section>

          <Section title="Trades that went through">
            {extra.fills.length === 0 ? (
              <Empty>No trades yet today.</Empty>
            ) : (
              <ul className="flex flex-col gap-3">
                {extra.fills.map((f, i) => (
                  <FillCard key={String(f.fill_id ?? i)} f={f} name={name} />
                ))}
              </ul>
            )}
          </Section>

          <Section title="Margin: Kite against our model">
            <MarginCard rows={extra.margin} />
          </Section>

          <Section title="Results, all days">
            <StatsCard s={detail.stats_all} />
          </Section>

          <Section title="Results, only days with nearly complete data">
            <p className="mb-2 text-sm" style={{ color: "var(--color-fg-muted)" }}>
              Counts only days where at least {Number.isNaN(threshold) ? detail.coverage_threshold : pct(threshold)} of
              the market data arrived.
              {detail.has_coverage_holes ? " Some days had gaps, so these numbers cover fewer days than the ones above." : ""}
            </p>
            <StatsCard s={detail.stats_full_coverage} />
          </Section>

          <Section title="What happened, in order">
            {extra.timeline.length === 0 ? (
              <Empty>Nothing recorded yet.</Empty>
            ) : (
              <ol className="flex flex-col gap-2">
                {[...extra.timeline].reverse().map((t, i) => {
                  const d = detailText(t.detail);
                  return (
                    <li key={`${t.wall_ns}-${i}`}>
                      <Card>
                        <div className="flex flex-wrap items-baseline justify-between gap-2">
                          <span className="text-sm font-medium">{timelineText(t.kind)}</span>
                          <span className="text-xs" style={{ color: "var(--color-fg-muted)" }}>{istWhen(t.wall_ist)}</span>
                        </div>
                        {d && <p className="mt-1 break-words text-xs" style={{ color: "var(--color-fg-muted)" }}>{d}</p>}
                      </Card>
                    </li>
                  );
                })}
              </ol>
            )}
          </Section>
        </>
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

"use client";

import Link from "next/link";
import { canStopAll } from "@/lib/stopAll";
import { useCallback, useEffect, useState } from "react";
import { istWhen, pct, rupees } from "@/lib/format";
import { listStrategies } from "@/lib/tests";
import { getKiteStatus, listSessions, PARITY_HELP, parityText, signedRupees, stopAll, tone } from "@/lib/trade";
import type { SessionDTO, Strategy } from "@/lib/types";
import { Empty, Loading, PageHead, Panel, Skeleton, Table, toneClass } from "./ui";
import { CommandMessage, ConfirmDialog, LoginBanner, PauseReasons, StatusChip, useCommandWatch } from "./Commands";
import NewSessionForm from "./NewSessionForm";

const FAST_MS = 2000; // while any session is RUNNING
const SLOW_MS = 15_000;
const MAX_BACKOFF_MS = 30_000;

function SessionRow({ s }: { s: SessionDTO }) {
  return (
    <tr>
      <td data-wrap>
        <Link href={`/trade/${encodeURIComponent(s.session_id)}`} title={s.name ? `Saved as ${s.name}` : undefined}>{s.name_label || s.name || s.session_id}</Link>
        <div className="stk-label">{[s.strategy_name, istWhen(s.created_ist)].filter(Boolean).join(", ")}</div>
      </td>
      <td data-wrap>
        <StatusChip status={s.status} />
        <PauseReasons reasons={s.pause_reasons} />
      </td>
      <td className={`num ${toneClass(tone(s.today_pnl_paise)) ?? ""}`}>{signedRupees(s.today_pnl_paise, rupees)}</td>
      <td className="num">{s.open_positions === null || s.open_positions === undefined ? "—" : s.open_positions}</td>
      <td className="num">{s.coverage_today === null || s.coverage_today === undefined ? "—" : pct(s.coverage_today)}</td>
      <td data-wrap>{parityText(s.parity_last)}</td>
    </tr>
  );
}

export default function TradePage() {
  const [sessions, setSessions] = useState<SessionDTO[] | null>(null);
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [waiting, setWaiting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [sending, setSending] = useState(false);
  const cmd = useCommandWatch();
  const { watch, fail } = cmd;

  const anyRunning = sessions?.some((s) => s.status === "RUNNING") ?? false;
  const anyLive = canStopAll(sessions);

  useEffect(() => {
    let stop = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let fails = 0;
    async function tick() {
      let next = SLOW_MS;
      try {
        const [list, kite] = await Promise.all([
          listSessions(),
          getKiteStatus().catch(() => null),
        ]);
        if (stop) return;
        setSessions(list);
        setWaiting(Boolean(kite?.daemon_waiting_for_login));
        setError(null);
        fails = 0;
        next = list.some((s) => s.status === "RUNNING") ? FAST_MS : SLOW_MS;
      } catch (e) {
        if (stop) return;
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
  }, []);

  useEffect(() => {
    listStrategies().then(setStrategies).catch(() => setStrategies([]));
  }, []);

  const doStopAll = useCallback(async () => {
    setConfirm(false);
    setSending(true);
    try {
      const { cmd_id } = await stopAll();
      watch(cmd_id);
    } catch (e) {
      fail(`Stop all could not be sent: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSending(false);
    }
  }, [watch, fail]);

  return (
    <div className="stk-page" data-bar={anyLive ? "" : undefined}>
      <PageHead
        title="Paper trading"
        lede="Strategies trade live prices with pretend money."
        actions={
          <span className="stk-label" aria-live="polite">
            {error ? "Not updating" : anyRunning ? "Live, updating" : ""}
          </span>
        }
      />

      <div className="stk-stack">
        {waiting && <LoginBanner />}

        {error && (
          <div role="alert">
            <Panel tone="red">
              <p>Could not reach the API: {error}</p>
            </Panel>
          </div>
        )}

        <NewSessionForm strategies={strategies} />

        <section className="stk-section">
          <h2>Sessions</h2>
          <CommandMessage message={cmd.message} failed={cmd.failed} pending={cmd.pending} />
          {sessions === null ? (
            <Loading label="Loading sessions">
              <Skeleton size="s" />
              <Skeleton size="m" />
              <Skeleton size="m" />
            </Loading>
          ) : sessions.length === 0 ? (
            <Empty>No paper sessions yet. Create one above.</Empty>
          ) : (
            <Table
              caption="Paper sessions"
              head={[
                { label: "Session" },
                { label: "Status" },
                { label: "Made or lost today", num: true },
                { label: "Positions held", num: true },
                { label: "Market data received", num: true },
                { label: "Last replay check" },
              ]}
            >
              {sessions.map((s) => (
                <SessionRow key={s.session_id} s={s} />
              ))}
            </Table>
          )}
          {sessions !== null && sessions.length > 0 && <p className="stk-fine stk-gap-s">Last replay check: {PARITY_HELP}</p>}
        </section>
      </div>

      {anyLive && (
      <div className="stk-bar">
        <div className="stk-bar-in">
          <button
            type="button"
            onClick={() => setConfirm(true)}
            disabled={sending || cmd.pending}
            className="stk-btn"
            data-tone="danger"
          >
            {sending ? "Sending…" : "Stop all"}
          </button>
        </div>
      </div>
      )}

      <ConfirmDialog
        open={confirm}
        title="Stop all sessions?"
        body="Every session that is running or waiting will stop taking new trades right away. Open positions stay as they are. You can resume a session later from its page."
        confirmLabel="Yes, stop all"
        onConfirm={doStopAll}
        onCancel={() => setConfirm(false)}
      />
    </div>
  );
}

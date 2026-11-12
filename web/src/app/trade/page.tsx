"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { istWhen, pct, rupees } from "@/lib/format";
import { listStrategies } from "@/lib/tests";
import { getKiteStatus, listSessions, signedRupees, stopAll, tone } from "@/lib/trade";
import type { SessionDTO, Strategy } from "@/lib/types";
import { Empty, Panel, Skeleton, Table } from "./ui";
import { CommandMessage, ConfirmDialog, LoginBanner, PauseReasons, StatusChip, useCommandWatch } from "./Commands";
import NewSessionForm from "./NewSessionForm";

const FAST_MS = 2000; // while any session is RUNNING
const SLOW_MS = 15_000;
const MAX_BACKOFF_MS = 30_000;

function SessionRow({ s }: { s: SessionDTO }) {
  return (
    <tr>
      <td>
        <Link href={`/trade/${encodeURIComponent(s.session_id)}`} className="font-medium text-green-soft underline-offset-2 hover:underline">
          {s.name || s.session_id}
        </Link>
        <div className="stk-label">{istWhen(s.created_ist)}</div>
      </td>
      <td>
        <StatusChip status={s.status} />
        <PauseReasons reasons={s.pause_reasons} />
      </td>
      <td className={`num stk-mono ${tone(s.today_pnl_paise) === "up" ? "text-green" : tone(s.today_pnl_paise) === "down" ? "text-red-soft" : ""}`}>
        {signedRupees(s.today_pnl_paise, rupees)}
      </td>
      <td className="num">{s.open_positions === null || s.open_positions === undefined ? "—" : s.open_positions}</td>
      <td className="num">{s.coverage_today === null || s.coverage_today === undefined ? "—" : pct(s.coverage_today)}</td>
      <td>{s.parity_last ?? "—"}</td>
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
  const anyLive = sessions?.some((s) => s.status !== "STOPPED") ?? false;

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
    <div className="mx-auto max-w-6xl px-4 py-8 pb-28 sm:py-10">
      <div className="mb-6 flex items-center justify-between">
        <Link href="/" className="stk-btn" data-tone="quiet" >
          Home
        </Link>
        <div className="stk-label" aria-live="polite">
          {anyRunning ? "Live, updating" : "Updating slowly"}
        </div>
      </div>

      <h1 className="mb-2 text-2xl font-bold text-green sm:text-3xl">
        Paper trading
      </h1>
      <p className="mb-6 text-sm text-fg-muted">
        Strategies trade live prices with pretend money. Simulation only: no real orders are ever sent.
      </p>

      {waiting && (
        <div className="mb-4">
          <LoginBanner />
        </div>
      )}

      {error && (
        <div className="mb-4" role="alert">
          <Panel tone="red">
            <p className="text-sm text-red-soft">
              Could not reach the API: {error}
            </p>
          </Panel>
        </div>
      )}

      <NewSessionForm strategies={strategies} />

      <h2 className="mb-3 mt-8 text-sm font-semibold text-fg-muted">
        Sessions
      </h2>
      {sessions === null ? (
        <div className="flex flex-col gap-3" aria-busy="true" aria-label="Loading sessions">
          <Skeleton className="h-10" />
          <Skeleton className="h-16" />
          <Skeleton className="h-16" />
        </div>
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

      <div
        className="fixed inset-x-0 bottom-0 z-20 border-t border-red bg-bg-soft px-4 py-3"
      >
        <div className="mx-auto flex max-w-6xl items-center justify-end gap-4">
          <CommandMessage message={cmd.message} failed={cmd.failed} />
          <button
            type="button"
            onClick={() => setConfirm(true)}
            disabled={!anyLive || sending}
            className="stk-btn"
            data-tone="danger"
          >
            {sending ? "Sending…" : "Stop all"}
          </button>
        </div>
      </div>

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

"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { pct, rupees } from "@/lib/format";
import { listStrategies } from "@/lib/tests";
import { getKiteStatus, listSessions, signedRupees, stopAll, tone } from "@/lib/trade";
import type { SessionDTO, Strategy } from "@/lib/types";
import { Card, Stat } from "../tests/ui";
import { CommandMessage, ConfirmDialog, LoginBanner, PauseReasons, StatusChip, useCommandWatch } from "./Commands";
import NewSessionForm from "./NewSessionForm";

const FAST_MS = 2000; // while any session is RUNNING
const SLOW_MS = 15_000;
const MAX_BACKOFF_MS = 30_000;

function SessionCard({ s }: { s: SessionDTO }) {
  return (
    <li>
      <Link href={`/trade/${encodeURIComponent(s.session_id)}`} className="block min-h-11">
        <Card tone={s.status === "PAUSED" ? "amber" : undefined}>
          <div className="mb-1 flex flex-wrap items-center gap-2">
            <StatusChip status={s.status} />
          </div>
          <div className="font-medium" style={{ color: "var(--color-fg)" }}>
            {s.name || s.session_id}
          </div>
          <PauseReasons reasons={s.pause_reasons} />
          <div className="mt-3 grid grid-cols-2 gap-3">
            <Stat
              label="Made or lost today"
              value={signedRupees(s.today_pnl_paise, rupees)}
              tone={tone(s.today_pnl_paise)}
            />
            <Stat label="Positions held" value={s.open_positions === null || s.open_positions === undefined ? "—" : String(s.open_positions)} />
            <Stat label="Market data received today" value={s.coverage_today === null || s.coverage_today === undefined ? "—" : pct(s.coverage_today)} />
            <Stat label="Last replay check" value={s.parity_last ?? "—"} />
          </div>
        </Card>
      </Link>
    </li>
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
    <div className="mx-auto max-w-2xl px-4 py-8 pb-32 sm:py-12">
      <div className="mb-6 flex items-center justify-between">
        <Link href="/" className="stk-mono-tight flex min-h-11 items-center" style={{ color: "var(--color-fg-muted)" }}>
          ← Home
        </Link>
        <div className="stk-mono-tight" style={{ color: "var(--color-fg-muted)" }} aria-live="polite">
          {anyRunning ? "Live, updating" : "Updating slowly"}
        </div>
      </div>

      <h1 className="stk-mono mb-2 text-2xl font-bold sm:text-3xl" style={{ color: "var(--color-green)" }}>
        Paper trading
      </h1>
      <p className="mb-6 text-sm" style={{ color: "var(--color-fg-muted)" }}>
        Strategies trade live prices with pretend money. Simulation only: no real orders are ever sent.
      </p>

      {waiting && (
        <div className="mb-4">
          <LoginBanner />
        </div>
      )}

      {error && (
        <div className="mb-4" role="alert">
          <Card tone="red">
            <p className="text-sm" style={{ color: "var(--color-red-soft)" }}>
              Could not reach the API: {error}
            </p>
          </Card>
        </div>
      )}

      <NewSessionForm strategies={strategies} />

      <h2 className="stk-mono-tight mb-3 mt-8" style={{ color: "var(--color-fg-muted)" }}>
        Sessions
      </h2>
      {sessions === null ? (
        <Card>
          <p className="py-6 text-center text-sm" style={{ color: "var(--color-fg-muted)" }}>
            Loading…
          </p>
        </Card>
      ) : sessions.length === 0 ? (
        <Card>
          <p className="py-6 text-center text-sm" style={{ color: "var(--color-fg-muted)" }}>
            No paper sessions yet. Create one above.
          </p>
        </Card>
      ) : (
        <ul className="flex flex-col gap-3">
          {sessions.map((s) => (
            <SessionCard key={s.session_id} s={s} />
          ))}
        </ul>
      )}

      <div
        className="fixed inset-x-0 bottom-0 z-20 border-t px-4 py-3"
        style={{ background: "var(--color-bg-soft)", borderColor: "rgba(255,68,68,0.5)" }}
      >
        <div className="mx-auto flex max-w-2xl flex-col gap-2">
          <CommandMessage message={cmd.message} failed={cmd.failed} />
          <button
            type="button"
            onClick={() => setConfirm(true)}
            disabled={!anyLive || sending}
            className="stk-mono-tight min-h-11 w-full rounded-md border px-4 py-2 font-bold disabled:opacity-50"
            style={{ borderColor: "var(--color-red)", color: "var(--color-red-soft)", background: "rgba(255,68,68,0.10)" }}
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

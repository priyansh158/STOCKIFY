"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { commandText, getCommand, pauseReasonText, startKiteLogin, statusText } from "@/lib/trade";
import { Panel } from "./ui";

const WATCH_MS = 1000;
const WATCH_MAX = 90; // about 90 s of watching, then the owner is told it is still queued

/**
 * Tells the owner whether a queued command (pause, resume, stop, stop all) took effect: it asks
 * /api/trade/commands/{id} until the command is applied or failed.
 */
export function useCommandWatch(onSettled?: () => void) {
  const [message, setMessage] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      clearTimeout(timer.current);
    };
  }, []);

  const watch = useCallback(
    (cmdId: number) => {
      clearTimeout(timer.current);
      setFailed(false);
      setMessage("Sent. Waiting for the background service to pick it up…");
      let tries = 0;
      const tick = async () => {
        if (!alive.current) return;
        tries += 1;
        try {
          const c = await getCommand(cmdId);
          if (!alive.current) return;
          setMessage(commandText(c));
          if (c.status !== "pending") {
            setFailed(c.status === "failed");
            onSettled?.();
            return;
          }
        } catch {
          if (!alive.current) return;
          setMessage("Sent, but the answer could not be read. It may still apply.");
        }
        if (tries < WATCH_MAX) timer.current = setTimeout(tick, WATCH_MS);
        else setMessage("Still queued. It takes effect when the background service next runs.");
      };
      timer.current = setTimeout(tick, 400);
    },
    [onSettled],
  );

  const fail = useCallback((text: string) => {
    clearTimeout(timer.current);
    setFailed(true);
    setMessage(text);
  }, []);

  return { message, failed, watch, fail };
}

/** A live status line for the outcome of a command (announced to screen readers). */
export function CommandMessage({ message, failed }: { message: string | null; failed: boolean }) {
  return (
    <div role="status" aria-live="polite" className="min-h-0">
      {message && (
        <p className={`text-sm ${failed ? "text-red-soft" : "text-fg-muted"}`}>
          {message}
        </p>
      )}
    </div>
  );
}

/** Shown while the background service is waiting for the owner to log in to Kite. */
export function LoginBanner({ detail }: { detail?: string }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  async function go() {
    setBusy(true);
    setErr(null);
    try {
      await startKiteLogin();
    } catch (e) {
      setErr(`Could not start the Kite login: ${e instanceof Error ? e.message : String(e)}`);
      setBusy(false);
    }
  }
  return (
    <Panel tone="amber">
      <p className="text-sm text-amber">
        Paper trading is waiting for you to log in to Kite. Nothing trades until you do.
      </p>
      {detail && (
        <p className="stk-label mt-1">
          {detail}
        </p>
      )}
      <button
        type="button"
        onClick={go}
        disabled={busy}
        className="stk-btn mt-3 w-full"
      >
        {busy ? "Opening Kite…" : "Log in to Kite"}
      </button>
      {err && (
        <p role="alert" className="mt-2 text-sm text-red-soft">
          {err}
        </p>
      )}
    </Panel>
  );
}

/** A confirm dialog (the native <dialog>, which traps focus and closes on Escape). */
export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  body: string;
  confirmLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog
      ref={ref}
      onCancel={(e) => {
        e.preventDefault();
        onCancel();
      }}
      aria-labelledby="confirm-title"
      className="stk-dialog m-auto w-[calc(100%-2rem)] max-w-md rounded-lg border border-red bg-bg-soft p-4 text-fg"
    >
      <h2 id="confirm-title" className="text-lg font-bold text-red-soft">
        {title}
      </h2>
      <p className="mt-2 text-sm">{body}</p>
      <div className="mt-4 flex flex-col gap-2 sm:flex-row-reverse">
        <button
          type="button"
          onClick={onConfirm}
          className="stk-btn flex-1"
          data-tone="danger"
        >
          {confirmLabel}
        </button>
        <button
          type="button"
          onClick={onCancel}
          autoFocus
          className="stk-btn flex-1"
        >
          Keep going
        </button>
      </div>
    </dialog>
  );
}

export function StatusChip({ status }: { status: string }) {
  const color = status === "RUNNING" ? "text-green" : status === "PAUSED" ? "text-amber" : "text-fg-muted";
  return (
    <span className={`stk-chip ${color}`}>
      {statusText(status)}
    </span>
  );
}

export function PauseReasons({ reasons }: { reasons: string[] }) {
  if (reasons.length === 0) return null;
  return (
    <ul className="mt-2 flex flex-col gap-1">
      {reasons.map((r) => (
        <li key={r} className="text-sm text-amber">
          {pauseReasonText(r)}
        </li>
      ))}
    </ul>
  );
}


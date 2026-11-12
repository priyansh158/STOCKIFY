"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { commandText, getCommand, pauseReasonText, startKiteLogin, statusText } from "@/lib/trade";
import { buttonStyle, Card } from "../tests/ui";

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
        <p className="text-sm" style={{ color: failed ? "var(--color-red-soft)" : "var(--color-fg-muted)" }}>
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
    <Card tone="amber">
      <p className="text-sm" style={{ color: "var(--color-amber)" }}>
        Paper trading is waiting for you to log in to Kite. Nothing trades until you do.
      </p>
      {detail && (
        <p className="mt-1 text-xs" style={{ color: "var(--color-fg-muted)" }}>
          {detail}
        </p>
      )}
      <button
        type="button"
        onClick={go}
        disabled={busy}
        className="stk-mono-tight mt-3 min-h-11 w-full rounded-md border px-4 py-2 disabled:opacity-60"
        style={buttonStyle}
      >
        {busy ? "Opening Kite…" : "Log in to Kite"}
      </button>
      {err && (
        <p role="alert" className="mt-2 text-sm" style={{ color: "var(--color-red-soft)" }}>
          {err}
        </p>
      )}
    </Card>
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
      className="m-auto w-[calc(100%-2rem)] max-w-md rounded-lg border p-4"
      style={{ background: "var(--color-bg-soft)", color: "var(--color-fg)", borderColor: "var(--color-red)" }}
    >
      <h2 id="confirm-title" className="stk-mono text-lg font-bold" style={{ color: "var(--color-red-soft)" }}>
        {title}
      </h2>
      <p className="mt-2 text-sm">{body}</p>
      <div className="mt-4 flex flex-col gap-2 sm:flex-row-reverse">
        <button
          type="button"
          onClick={onConfirm}
          className="stk-mono-tight min-h-11 flex-1 rounded-md border px-4 py-2 font-bold"
          style={{ borderColor: "var(--color-red)", color: "var(--color-red-soft)", background: "rgba(255,68,68,0.10)" }}
        >
          {confirmLabel}
        </button>
        <button
          type="button"
          onClick={onCancel}
          autoFocus
          className="stk-mono-tight min-h-11 flex-1 rounded-md border px-4 py-2"
          style={buttonStyle}
        >
          Keep going
        </button>
      </div>
    </dialog>
  );
}

export function StatusChip({ status }: { status: string }) {
  const color =
    status === "RUNNING" ? "var(--color-green)" : status === "PAUSED" ? "var(--color-amber)" : "var(--color-fg-muted)";
  return (
    <span
      className="stk-mono-tight inline-flex items-center rounded px-2 py-0.5 text-xs font-bold"
      style={{ color, border: `1px solid ${color}` }}
    >
      {statusText(status)}
    </span>
  );
}

export function PauseReasons({ reasons }: { reasons: string[] }) {
  if (reasons.length === 0) return null;
  return (
    <ul className="mt-2 flex flex-col gap-1">
      {reasons.map((r) => (
        <li key={r} className="text-sm" style={{ color: "var(--color-amber)" }}>
          {pauseReasonText(r)}
        </li>
      ))}
    </ul>
  );
}


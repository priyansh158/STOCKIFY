"use client";

import { useEffect, useRef, useState } from "react";
import type { TickEvent } from "@/lib/types";

/**
 * Subscribe to the live tick WS for one or more instrument tokens.
 *
 * Returns a map `tokenString → latestTick`. Each token's value is the most
 * recent tick we've seen — we don't accumulate history; the LTP chip + the
 * portfolio rows want the current price, not a stream.
 *
 * Connects on mount, reconnects every 3s on disconnect, sends subscribe
 * commands when the `tokens` prop changes. Returns `null` until the WS opens.
 * Returns `{}` when the server rejects auth (Kite not logged in) so the UI
 * can fall back to the static last_price.
 */
export function useTicks(tokens: number[]): Record<string, TickEvent> | null {
  const [latest, setLatest] = useState<Record<string, TickEvent> | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const tokensRef = useRef<number[]>([]);
  const closedByAppRef = useRef(false);

  // Snapshot the tokens so the effect can compare set-equality cheaply.
  const tokensKey = [...tokens].sort().join(",");

  useEffect(() => {
    closedByAppRef.current = false;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    const wsBase =
      typeof window === "undefined"
        ? null
        : (window.location.protocol === "https:" ? "wss://" : "ws://") +
          window.location.host;
    if (!wsBase) return;

    function connect() {
      const url = `${wsBase}/api/ws/ticks${
        tokensKey ? `?tokens=${tokensKey}` : ""
      }`;
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        // Empty state once connected (so the UI knows "connected, awaiting
        // ticks" vs the initial null = "not connected yet").
        setLatest((prev) => prev ?? {});
        tokensRef.current = tokensKey ? tokensKey.split(",").map(Number) : [];
      };

      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data) as TickEvent | { type: "error"; message: string };
          if (msg.type === "tick") {
            setLatest((prev) => ({
              ...(prev ?? {}),
              [String(msg.token)]: msg,
            }));
          } else if (msg.type === "error") {
            // Server signalled an unrecoverable state (e.g. Kite not auth'd).
            // Stop reconnecting — UI should fall back to static last_price.
            closedByAppRef.current = true;
            setLatest({});
          }
        } catch {
          // Drop malformed frame.
        }
      };

      ws.onclose = () => {
        wsRef.current = null;
        if (closedByAppRef.current) return;
        // Reconnect after 3s.
        reconnectTimer = setTimeout(connect, 3000);
      };
      ws.onerror = () => { try { ws.close(); } catch { /* */ } };
    }

    connect();

    return () => {
      closedByAppRef.current = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      try { wsRef.current?.close(); } catch { /* */ }
      wsRef.current = null;
    };
  }, [tokensKey]);

  return latest;
}

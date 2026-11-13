"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { recorderWords } from "@/lib/data";
import type { RecorderStatus, RecorderStatusResponse } from "@/lib/types";
import { Chip, Panel } from "@/components/ui";

const REFRESH_MS = 5000;

/**
 * The live recorder strip: is it connected, how many packets a second, how old the freshest packet is, in words.
 * The first figures come from the server render; it then asks again every few seconds. Nothing here is a live region:
 * a ticking figure would be read out over and over.
 */
export default function RecorderStrip({ initial }: { initial: RecorderStatus | null }) {
  const [status, setStatus] = useState<RecorderStatus | null>(initial);
  const [stale, setStale] = useState(false);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const r = await api<RecorderStatusResponse>("/api/data/recorder");
        if (alive) {
          setStatus(r.status);
          setStale(false);
        }
      } catch {
        if (alive) setStale(true);
      }
    };
    const id = window.setInterval(tick, REFRESH_MS);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);

  const w = recorderWords(status);
  return (
    <Panel tone={w.tone === "up" ? undefined : "amber"}>
      <div className="stk-strip">
        <h2 className="stk-strip-title">Recorder</h2>
        <Chip tone={w.tone}>
          <span aria-hidden>{w.tone === "up" ? "✓" : w.tone === "down" ? "✕" : "~"}</span>&nbsp;{w.headline}
        </Chip>
        <p>{w.rate}</p>
        <p>{w.last}</p>
        {stale && <Chip tone="warn">Can&apos;t refresh right now</Chip>}
      </div>
      {w.note && <p className="stk-fine stk-gap-xs">{w.note}</p>}
    </Panel>
  );
}

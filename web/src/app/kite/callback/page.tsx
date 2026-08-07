"use client";

/**
 * /kite/callback — page Zerodha redirects to after auth.
 *
 * The FastAPI `/api/kite/callback` does the request_token exchange and then
 * 302s here with `?status=ok` or `?status=err&reason=…`. We just render the
 * outcome and offer a return CTA. (No client-side token exchange.)
 */

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";

function CallbackBody() {
  const params = useSearchParams();
  const status = params.get("status");
  const reason = params.get("reason");

  const ok = status === "ok";
  const tone = ok ? "var(--color-green)" : "var(--color-red-soft)";

  return (
    <div className="mx-auto max-w-md px-4 py-16 text-center">
      <div
        className="stk-mono mb-2"
        style={{ color: tone, fontSize: "1.5rem" }}
      >
        {ok ? "✓ Connected to Kite" : "× Kite login failed"}
      </div>
      <p
        className="stk-mono-tight mt-4 mb-8"
        style={{ color: "var(--color-fg-muted)" }}
      >
        {ok
          ? "Access token saved. You can close this tab or continue below."
          : `Reason: ${reason ?? "unknown"}`}
      </p>
      <div className="flex flex-col gap-3">
        <Link
          href="/portfolio"
          className="stk-mono-tight rounded-md border px-4 py-3"
          style={{
            borderColor: "var(--color-green)",
            color: "var(--color-green)",
            background: "rgba(0,255,65,0.10)",
          }}
        >
          → Portfolio
        </Link>
        <Link
          href="/"
          className="stk-mono-tight rounded-md border px-4 py-3"
          style={{
            borderColor: "rgba(0,255,65,0.25)",
            color: "var(--color-fg-muted)",
          }}
        >
          ← Search
        </Link>
      </div>
    </div>
  );
}

export default function KiteCallbackPage() {
  return (
    <Suspense fallback={<div className="px-4 py-16 text-center">Loading…</div>}>
      <CallbackBody />
    </Suspense>
  );
}

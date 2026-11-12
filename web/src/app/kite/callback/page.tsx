"use client";

/**
 * /kite/callback: the page Zerodha redirects to after auth.
 *
 * The FastAPI `/api/kite/callback` does the request_token exchange and then
 * 302s here with `?status=ok` or `?status=err&reason=…`. We just render the
 * outcome and offer a return CTA. (No client-side token exchange.)
 */

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { Panel } from "@/components/ui";

function CallbackBody() {
  const params = useSearchParams();
  const status = params.get("status");
  const reason = params.get("reason");

  const ok = status === "ok";

  return (
    <div className="stk-page" data-narrow>
      <Panel tone={ok ? undefined : "red"}>
        <div className="stk-stack-s" role="status">
          <h1 className={ok ? "stk-up" : "stk-down"}>{ok ? "Connected to Kite" : "Kite login failed"}</h1>
          <p className="stk-lede">
            {ok
              ? "Access token saved. You can close this tab or continue below."
              : `Reason: ${reason ?? "unknown"}`}
          </p>
          <div className="stk-row">
            <Link href="/portfolio" className="stk-btn" data-tone="primary">
              Go to the portfolio
            </Link>
            <Link href="/" className="stk-btn">
              Back to search
            </Link>
          </div>
        </div>
      </Panel>
    </div>
  );
}

export default function KiteCallbackPage() {
  return (
    <Suspense fallback={<div className="stk-page" data-narrow><p className="stk-muted">Loading…</p></div>}>
      <CallbackBody />
    </Suspense>
  );
}

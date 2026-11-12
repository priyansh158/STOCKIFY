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

/** Why the Kite login failed, in words. The API sends "missing_request_token" or the name of the error it hit. */
function reasonWords(reason: string | null): string {
  if (!reason) return "Zerodha did not say why.";
  if (reason === "missing_request_token") return "Zerodha did not send back the one-time code that finishes a login.";
  return `The login could not be finished (${reason}). Try logging in again.`;
}

function CallbackBody() {
  const params = useSearchParams();
  const status = params.get("status");
  const reason = params.get("reason");

  const ok = status === "ok";
  const why = reasonWords(reason);

  return (
    <div className="stk-page" data-narrow>
      <Panel tone={ok ? undefined : "red"}>
        <div className="stk-stack-s" role="status">
          <h1 className={ok ? "stk-up" : "stk-down"}>{ok ? "Logged in to Kite" : "Kite login failed"}</h1>
          <p className="stk-lede">
            {ok ? "You are logged in until about 6:00 IST tomorrow. You can close this tab or continue below." : why}
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

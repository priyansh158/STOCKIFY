"""Kite Connect OAuth glue + connection status.

Flow:
  1. Client GETs /api/kite/login_url → server returns the Zerodha auth URL.
  2. Client opens that URL in a new tab; Zerodha redirects back to
     /api/kite/callback?request_token=… (or to a Next.js page that calls
     POST /api/kite/exchange with the request_token — see below).
  3. /api/kite/exchange exchanges request_token → access_token, persists
     the token to disk, returns success.
  4. /api/kite/status is a cheap auth-state probe.

`/api/kite/callback` is kept as a GET passthrough that 302-redirects back to
the Next.js `/kite/callback` page (with `?status=ok|err` flags) so the user's
browser ends up on a friendly UI page, not a JSON response.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

import kite_client
import stockify_paths

log = logging.getLogger("stockify.kite")

router = APIRouter(prefix="/api/kite", tags=["kite"])

# Where to send the user after the OAuth callback (loopback). A login that came in through the
# laptop's Tailscale address (STOCKIFY_REMOTE_HOST) goes back there instead; see _web_callback.
# The login URL itself is Kite's own: the redirect target is set in the Kite developer console.
_NEXTJS_CALLBACK = (
    stockify_paths.loopback_origin(
        "STOCKIFY_WEB_ORIGIN", "http://127.0.0.1:3000"
    )
    + "/kite/callback"
)


@router.get("/login_url")
async def login_url() -> dict:
    """Return the Zerodha auth URL the client should open in a new tab."""
    if not kite_client.is_enabled():
        raise HTTPException(
            status_code=503,
            detail="Kite is not configured. Set KITE_API_KEY/KITE_API_SECRET in .env.",
        )
    return {"url": kite_client.login_url()}


def _web_callback(request: Request) -> str:
    """The web page to land on: the Tailscale address when the login came in through it, else loopback.

    The guards (server/local_only.py) have already refused any Host or X-Forwarded-Host that is neither
    loopback nor the one STOCKIFY_REMOTE_HOST, so a name seen here is one of those.
    """
    remote = stockify_paths.remote_host()
    if remote:
        names = [request.headers.get("host", "")]
        names += [n for v in request.headers.getlist("x-forwarded-host") for n in v.split(",")]
        if any(n.strip().split(":")[0] == remote for n in names):
            return f"https://{remote}/kite/callback"
    return _NEXTJS_CALLBACK


@router.get("/callback")
async def callback(request: Request, request_token: str = Query("", description="Zerodha request_token")):
    """Browser landing endpoint after Zerodha auth.

    Exchanges the request_token for an access_token, persists it, then
    redirects to the Next.js callback page (which shows success / failure),
    on the address the browser came in through (loopback, or the Tailscale one).
    """
    page = _web_callback(request)
    if not request_token:
        return RedirectResponse(
            url=f"{page}?status=err&reason=missing_request_token",
            status_code=302,
        )
    try:
        kite_client.exchange_request_token(request_token)
    except Exception as e:
        log.warning("Kite token exchange failed: %s", e)
        return RedirectResponse(
            url=f"{page}?status=err&reason={type(e).__name__}",
            status_code=302,
        )
    return RedirectResponse(url=f"{page}?status=ok", status_code=302)


@router.post("/exchange")
async def exchange(payload: dict) -> dict:
    """Programmatic exchange — useful when the frontend handles the redirect
    itself and POSTs the captured request_token back.

    Body: {"request_token": "..."}.
    """
    req_tok = (payload or {}).get("request_token", "")
    if not req_tok:
        raise HTTPException(status_code=400, detail="missing request_token")
    try:
        kite_client.exchange_request_token(req_tok)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"authenticated": kite_client.is_authenticated()}


@router.post("/logout")
async def logout() -> dict:
    """Clear the cached token. Idempotent."""
    try:
        kite_client.clear_token()
    except Exception:
        pass
    return {"authenticated": False}


@router.get("/status")
async def status() -> dict:
    """Cheap auth-state check — safe to call frequently."""
    try:
        authenticated = kite_client.is_authenticated()
        return {
            "configured":    kite_client.is_enabled(),
            "authenticated": authenticated,
            "daemon_waiting_for_login": _daemon_waiting_for_login(),
        }
    except Exception as e:
        return {"configured": False, "authenticated": False, "daemon_waiting_for_login": False, "error": str(e)}


def _daemon_waiting_for_login() -> bool:
    """True while the daemon says it is waiting for a login (token expired mid-day, or stale at the start) and no
    login newer than the rejected token has been made. Reads daemon_status.json and stats the token file; never
    opens it, so a token value never reaches this route."""
    try:
        from stockify.daemon import token_watch
        if not token_watch.read_status().get("waiting_for_login"):
            return False
        return not token_watch.login_ok(kite_client.is_authenticated)
    except Exception:
        return False

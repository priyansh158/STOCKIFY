"""The API serves this machine's own UI and nothing else.

uvicorn binds 127.0.0.1, but a browser on this machine will carry any web page's request to it.
Four checks close that, and all sit outside the routers so no route can forget them:

  * the `Host` must be loopback (`LOOPBACK_HOSTS`, with or without a port; the name is what is
    compared, exactly, so `LOCALHOST`, `localhost.` and `[::1]` are refused). A page that rebinds
    its own DNS name to 127.0.0.1 and talks to port 8001 directly sends that name as the Host.
    `server/main.py` hands these names to Starlette's `TrustedHostMiddleware`.

  * `ForwardedHostOnly`: every `X-Forwarded-Host`, when present, must name loopback hosts by the
    same rule. The UI reaches the API through the web server's /api rewrite, which replaces the
    Host with the API's own and carries the browser's Host in that header
    (next/dist/server/lib/router-utils/proxy-request.js, `changeOrigin`); so for everything that
    comes through port 3000 this header is where a rebound name shows up. A header holding a list
    (one host per proxy hop) is refused if any entry is not loopback.

  * `WebOriginOnly`: a state-changing request (every method but GET, HEAD and OPTIONS) and every
    WebSocket handshake that carries an `Origin` must carry the configured web origin. Browsers
    put the page's origin on exactly those requests, so another site's page cannot log the owner
    out, queue a run or open the tick stream. CORS does not cover any of this: it only decides
    who may read an answer, and it never applies to WebSockets.

  * `NoFunnel`: a request or handshake carrying a `Tailscale-Funnel-Request` header (any value) is
    refused, whatever the setting below. Tailscale Funnel, if it were ever turned on, would carry
    clients from the public internet to the laptop's Tailscale name, which the setting below lets
    through the other three checks; Tailscale marks every such request with that header. It runs
    first. web/src/proxy.ts refuses the same header for pages.

A request with no `Origin` is served: curl, the CLI, the tests and the UI's server-side fetches
send none, and a browser cannot be made to leave it off a cross-site POST or WebSocket. A request
with no `X-Forwarded-Host` is checked on its `Host` alone (the same clients send none).

One setting widens the three host and origin checks, and only by one name: `STOCKIFY_REMOTE_HOST`
(`stockify_paths.remote_host()`). It exists so the owner can do the daily Kite login from their
phone through Tailscale (docs/superpowers/specs/2026-11-18-phone-login-tailscale-design.md):
`tailscale serve` publishes the web server at the laptop's private `https://<machine>.<tailnet>.ts.net`
address, reachable only from the owner's own Tailscale devices, while the API and the web server
keep binding 127.0.0.1. When it is set, that one exact host name is accepted in `Host` and
`X-Forwarded-Host` (by the same rule as loopback: the name before the first colon, compared
exactly), and the one Origin `https://<that host>` (exactly, as a browser sends it) for writes and
handshakes. Nothing else is accepted: no other `.ts.net` name, no wildcard, no `http` origin. A
value that is not a single lower-case `.ts.net` host name is ignored with a warning, so a mistyped
setting leaves the loopback-only behaviour in place. Unset or empty, the three are exactly loopback.
"""
from __future__ import annotations

import stockify_paths
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

LOOPBACK_HOSTS = ("127.0.0.1", "localhost")

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_DEFAULT_PORTS = {"http": ":80", "https": ":443"}


def _as_a_browser_sends_it(origin: str) -> str:
    """Trimmed, lower-cased, and without the scheme's own default port, which a browser never puts in Origin."""
    origin = origin.strip().lower()
    scheme, sep, rest = origin.partition("://")
    default = _DEFAULT_PORTS.get(scheme)
    if sep and default and rest.endswith(default):
        origin = origin[: -len(default)]
    return origin


def _is_loopback(host: str) -> bool:
    """Starlette's rule for `Host`: the name before the first colon, compared exactly."""
    return host.strip().split(":")[0] in LOOPBACK_HOSTS


def _checked(remote_host: str | None) -> str | None:
    """`remote_host`, or None when empty. Anything but a valid Tailscale name is a programming error here:
    `stockify_paths.remote_host()` already dropped a bad setting, and `*` would mean any host to Starlette."""
    if not remote_host:
        return None
    if not stockify_paths.is_remote_host(remote_host):
        raise ValueError("remote_host must be one Tailscale host name (stockify_paths.remote_host)")
    return remote_host


def allowed_hosts(remote_host: str | None) -> list[str]:
    """The `Host` names served: loopback, and the one remote host when it is set."""
    remote_host = _checked(remote_host)
    return [*LOOPBACK_HOSTS, remote_host] if remote_host else list(LOOPBACK_HOSTS)


def _is_allowed(host: str, remote_host: str | None) -> bool:
    """`_is_loopback`, or the same rule for the one remote host."""
    return _is_loopback(host) or (bool(remote_host) and host.strip().split(":")[0] == remote_host)


class NoFunnel:
    """Refuse a request or a WebSocket handshake that came through Tailscale Funnel (from the internet)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    def refuses(self, scope: Scope) -> bool:
        if scope["type"] not in ("http", "websocket"):
            return False
        return any(name.lower() == b"tailscale-funnel-request" for name, _ in scope["headers"])

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.refuses(scope):
            await PlainTextResponse("Invalid host header", status_code=400)(scope, receive, send)
        else:
            await self.app(scope, receive, send)


class ForwardedHostOnly:
    """Refuse a request or a WebSocket handshake whose `X-Forwarded-Host` is not loopback (or `remote_host`)."""

    def __init__(self, app: ASGIApp, remote_host: str | None = None) -> None:
        self.app = app
        self.remote_host = _checked(remote_host)

    def refuses(self, scope: Scope) -> bool:
        if scope["type"] not in ("http", "websocket"):
            return False
        values = [value.decode("latin-1") for name, value in scope["headers"] if name.lower() == b"x-forwarded-host"]
        # Every header counts, and each may list one host per proxy hop; an empty one names no loopback host.
        return any(not _is_allowed(host, self.remote_host) for value in values for host in value.split(","))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.refuses(scope):
            # The same answer TrustedHostMiddleware gives, for a handshake too (the upgrade is denied).
            await PlainTextResponse("Invalid host header", status_code=400)(scope, receive, send)
        else:
            await self.app(scope, receive, send)


class WebOriginOnly:
    """Refuse a write or a WebSocket handshake whose `Origin` is not `web_origin` (or `https://<remote_host>`)."""

    def __init__(self, app: ASGIApp, web_origin: str, remote_host: str | None = None) -> None:
        self.app = app
        self.web_origin = _as_a_browser_sends_it(web_origin)
        if not self.web_origin:
            raise ValueError("WebOriginOnly needs the web origin to compare with")
        # Compared exactly (no case folding, no port): a browser sends a https origin as `https://<host>`.
        remote_host = _checked(remote_host)
        self.remote_origin = f"https://{remote_host}" if remote_host else None

    def _allowed(self, origin: str) -> bool:
        if self.remote_origin is not None and origin.strip(" \t") == self.remote_origin:
            return True
        return _as_a_browser_sends_it(origin) == self.web_origin

    def refuses(self, scope: Scope) -> bool:
        if scope["type"] == "http":
            if scope["method"].upper() in _READ_METHODS:
                return False
        elif scope["type"] != "websocket":
            return False
        # Every Origin header counts: two of them, or one holding a list, is not the web origin.
        origins = [value for name, value in scope["headers"] if name.lower() == b"origin"]
        return any(not self._allowed(value.decode("latin-1")) for value in origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.refuses(scope):
            await self.app(scope, receive, send)
        elif scope["type"] == "websocket":
            await WebSocketClose(code=1008)(scope, receive, send)      # before accept: the handshake fails
        else:
            await PlainTextResponse("Origin not allowed", status_code=403)(scope, receive, send)

"""The API serves this machine's own UI and nothing else.

uvicorn binds 127.0.0.1, but a browser on this machine will carry any web page's request to it.
Three checks close that, and all sit outside the routers so no route can forget them:

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

A request with no `Origin` is served: curl, the CLI, the tests and the UI's server-side fetches
send none, and a browser cannot be made to leave it off a cross-site POST or WebSocket. A request
with no `X-Forwarded-Host` is checked on its `Host` alone (the same clients send none).
There is no setting to relax any of the three.
"""
from __future__ import annotations

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


class ForwardedHostOnly:
    """Refuse a request or a WebSocket handshake whose `X-Forwarded-Host` is not loopback."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    def refuses(self, scope: Scope) -> bool:
        if scope["type"] not in ("http", "websocket"):
            return False
        values = [value.decode("latin-1") for name, value in scope["headers"] if name.lower() == b"x-forwarded-host"]
        # Every header counts, and each may list one host per proxy hop; an empty one names no loopback host.
        return any(not _is_loopback(host) for value in values for host in value.split(","))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.refuses(scope):
            # The same answer TrustedHostMiddleware gives, for a handshake too (the upgrade is denied).
            await PlainTextResponse("Invalid host header", status_code=400)(scope, receive, send)
        else:
            await self.app(scope, receive, send)


class WebOriginOnly:
    """Refuse a write or a WebSocket handshake whose `Origin` is not `web_origin`."""

    def __init__(self, app: ASGIApp, web_origin: str) -> None:
        self.app = app
        self.web_origin = _as_a_browser_sends_it(web_origin)
        if not self.web_origin:
            raise ValueError("WebOriginOnly needs the web origin to compare with")

    def refuses(self, scope: Scope) -> bool:
        if scope["type"] == "http":
            if scope["method"].upper() in _READ_METHODS:
                return False
        elif scope["type"] != "websocket":
            return False
        # Every Origin header counts: two of them, or one holding a list, is not the web origin.
        origins = [value for name, value in scope["headers"] if name.lower() == b"origin"]
        return any(_as_a_browser_sends_it(value.decode("latin-1")) != self.web_origin for value in origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.refuses(scope):
            await self.app(scope, receive, send)
        elif scope["type"] == "websocket":
            await WebSocketClose(code=1008)(scope, receive, send)      # before accept: the handshake fails
        else:
            await PlainTextResponse("Origin not allowed", status_code=403)(scope, receive, send)

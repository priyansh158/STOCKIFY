"""The API serves this machine's own UI and nothing else.

uvicorn binds 127.0.0.1, but a browser on this machine will carry any web page's request to it.
Two checks close that, and both sit outside the routers so no route can forget them:

  * the `Host` must be loopback (`LOOPBACK_HOSTS`, with or without a port). A page that rebinds
    its own DNS name to 127.0.0.1 still sends its own name as the Host, and is refused.
    `server/main.py` hands these names to Starlette's `TrustedHostMiddleware`.

  * `WebOriginOnly`: a state-changing request (every method but GET, HEAD and OPTIONS) and every
    WebSocket handshake that carries an `Origin` must carry the configured web origin. Browsers
    put the page's origin on exactly those requests, so another site's page cannot log the owner
    out, queue a run or open the tick stream. CORS does not cover any of this: it only decides
    who may read an answer, and it never applies to WebSockets.

A request with no `Origin` is served: curl, the CLI, the tests and the UI's server-side fetches
send none, and a browser cannot be made to leave it off a cross-site POST or WebSocket.
There is no setting to relax either check.
"""
from __future__ import annotations

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

LOOPBACK_HOSTS = ("127.0.0.1", "localhost")

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class WebOriginOnly:
    """Refuse a write or a WebSocket handshake whose `Origin` is not `web_origin`."""

    def __init__(self, app: ASGIApp, web_origin: str) -> None:
        self.app = app
        self.web_origin = web_origin.strip().lower()
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
        return any(value.decode("latin-1").strip().lower() != self.web_origin for value in origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.refuses(scope):
            await self.app(scope, receive, send)
        elif scope["type"] == "websocket":
            await WebSocketClose(code=1008)(scope, receive, send)      # before accept: the handshake fails
        else:
            await PlainTextResponse("Origin not allowed", status_code=403)(scope, receive, send)

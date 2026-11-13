"""WS /api/ws/ticks — live KiteTicker stream bridged to the browser.

Wire protocol:
  → client sends:  {"subscribe":   [738561, 408065, ...]}
                   {"unsubscribe": [738561]}
  ← server pushes: {"type": "tick", "token": 738561, "ltp": 1374.7,
                    "change_pct": -1.23, "ts": 1778636500}
                   {"type": "error", "message": "..."} (terminal-ish; client
                    may keep the socket open and re-subscribe after fixing)

Multiple tokens per tick batch are flushed one frame per event to keep the
client-side parser dumb. The server doesn't enforce auth on the WS itself —
if Kite isn't authenticated, the very first response is an error event and
the socket is closed. The handshake itself is refused for a foreign Origin or
Host before this code runs (server/local_only.py).

One client may hold at most MAX_TOKENS_PER_CLIENT distinct tokens. A request
that would go over is refused whole, with an error event and nothing
subscribed: an over-long `?tokens=` list also closes the socket (1008), an
over-long subscribe message leaves it open.
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

import kite_client
from server.services import ticker_bridge

log = logging.getLogger("stockify.ticks")

router = APIRouter(prefix="/api/ws", tags=["ticks"])

# The UI opens one socket per portfolio page with one token per holding, all in `?tokens=`
# (web/src/lib/useTicks.ts, HoldingsList.tsx), and sends no subscribe messages. 200 is several
# times a large retail portfolio, and small next to Kite's own limit per connection (3000).
# Every subscribed token is also written to the tick buffer's subscription table, so an
# unbounded list would be an unbounded write.
MAX_TOKENS_PER_CLIENT = 200


def _over_cap_event() -> dict:
    return {
        "type": "error",
        "message": f"Too many instruments: one connection may subscribe at most {MAX_TOKENS_PER_CLIENT}.",
    }


def _tokens_in(items: list) -> list[int]:
    return [int(t) for t in items if isinstance(t, (int, str)) and str(t).isdigit()]


@router.websocket("/ticks")
async def ticks_ws(websocket: WebSocket) -> None:
    await websocket.accept()

    if not kite_client.is_authenticated():
        await websocket.send_json({
            "type": "error",
            "message": "Kite not authenticated — log in via /portfolio first.",
        })
        await websocket.close(code=1008)
        return

    # Optional initial token list from query string: ?tokens=738561,408065
    qs_tokens: list[int] = []
    raw = websocket.query_params.get("tokens", "")
    if raw:
        for t in raw.split(","):
            try:
                qs_tokens.append(int(t.strip()))
            except ValueError:
                continue

    # What this client holds; the cap counts distinct tokens.
    held: set[int] = set(qs_tokens)
    if len(held) > MAX_TOKENS_PER_CLIENT:
        await websocket.send_json(_over_cap_event())
        await websocket.close(code=1008)
        return

    loop = asyncio.get_running_loop()
    queue = ticker_bridge.register_client(loop)

    # Bring the supervisor + subs online if needed.
    if qs_tokens:
        ticker_bridge.ensure_subscribed(qs_tokens, mode="ltp")

    # Background reader: drain client → command messages (subscribe/unsubscribe).
    async def reader() -> None:
        try:
            while True:
                raw_msg = await websocket.receive_text()
                try:
                    msg = json.loads(raw_msg)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if "subscribe" in msg and isinstance(msg["subscribe"], list):
                    toks = _tokens_in(msg["subscribe"])
                    if len(held | set(toks)) > MAX_TOKENS_PER_CLIENT:
                        # Refused whole. The writer below is the only sender on this socket,
                        # so the answer goes through the client's own queue.
                        try:
                            queue.put_nowait([_over_cap_event()])
                        except asyncio.QueueFull:
                            pass
                    elif toks:
                        held.update(toks)
                        ticker_bridge.ensure_subscribed(toks, mode="ltp")
                if "unsubscribe" in msg and isinstance(msg["unsubscribe"], list):
                    import kite_ticker_worker
                    # Only what this client holds: another socket's tokens are not its to drop,
                    # and give it no room under the cap.
                    gone = set(_tokens_in(msg["unsubscribe"])) & held
                    if gone:
                        held.difference_update(gone)
                        kite_ticker_worker.unsubscribe(gone)
        except WebSocketDisconnect:
            pass

    reader_task = asyncio.create_task(reader())

    try:
        while True:
            events: list[dict] = await queue.get()
            for ev in events:
                await websocket.send_json(ev)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception as e:  # pragma: no cover
        log.warning("WS /ticks crashed: %s", e)
    finally:
        reader_task.cancel()
        ticker_bridge.unregister_client(queue)
        try:
            await websocket.close()
        except Exception:
            pass

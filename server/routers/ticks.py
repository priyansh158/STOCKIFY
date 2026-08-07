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
the socket is closed.
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
                    toks = [int(t) for t in msg["subscribe"] if isinstance(t, (int, str)) and str(t).isdigit()]
                    if toks:
                        ticker_bridge.ensure_subscribed(toks, mode="ltp")
                if "unsubscribe" in msg and isinstance(msg["unsubscribe"], list):
                    import kite_ticker_worker
                    toks = [int(t) for t in msg["unsubscribe"] if isinstance(t, (int, str)) and str(t).isdigit()]
                    if toks:
                        kite_ticker_worker.unsubscribe(set(toks))
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

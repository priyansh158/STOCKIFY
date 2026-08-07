"""Bridge KiteTicker callbacks (worker thread) to asyncio queues (main loop).

WebSocket clients call `ensure_running_then_subscribe()` to make sure the
supervisor thread is up and the requested tokens are subscribed, then await
`register_client(loop)` to receive a `Queue` whose items are normalized tick
events. On disconnect, call `unregister_client(queue)` to clean up.

Per CLAUDE.md:
  * The KiteTicker callback runs on a daemon thread and MUST NOT touch any
    asyncio object directly. We use `loop.call_soon_threadsafe(...)` to push
    items onto each registered queue.
  * The buffer-write path (tick_buffer.write_ticks) is unchanged — this
    bridge is a fanout in addition to persistence.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Iterable

import kite_client
import kite_ticker_worker

log = logging.getLogger("stockify.ticker_bridge")

# (asyncio_loop, asyncio_Queue) for each active WS client.
_clients: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
_clients_lock = threading.Lock()


# ── Tick fanout (worker thread) ───────────────────────────────────────────────

def _broadcast(ticks: list[dict]) -> None:
    """Invoked by `kite_ticker_worker._on_ticks` on the KiteTicker thread.

    We do NOT serialize the full Kite payload — only the fields the browser
    needs for the LTP chip + simple charts. Keeps each WS frame ~80 bytes vs
    a full ~500 byte tick.
    """
    if not ticks:
        return
    now_s = int(time.time())
    events: list[dict] = []
    for t in ticks:
        try:
            token = int(t.get("instrument_token") or 0)
            ltp = float(t.get("last_price") or 0)
        except (TypeError, ValueError):
            continue
        if not token:
            continue
        # Day change vs previous close (when KiteTicker provides ohlc).
        prev_close = (t.get("ohlc") or {}).get("close")
        change_pct: float | None = None
        try:
            if prev_close:
                prev = float(prev_close)
                if prev:
                    change_pct = (ltp - prev) / prev * 100.0
        except (TypeError, ValueError):
            change_pct = None
        events.append({
            "type":  "tick",
            "token": token,
            "ltp":   ltp,
            "change_pct": change_pct,
            "ts":    now_s,
        })

    if not events:
        return

    # Fanout to every registered client queue. Drop on full queue to keep
    # backpressure local to the slow consumer.
    with _clients_lock:
        clients = list(_clients)
    for loop, q in clients:
        try:
            loop.call_soon_threadsafe(_try_put, q, events)
        except Exception as e:
            log.debug("ticker_bridge fanout failed: %s", e)


def _try_put(q: asyncio.Queue, events: list[dict]) -> None:
    """Drop newest if queue is full — UI catches up on next tick."""
    try:
        q.put_nowait(events)
    except asyncio.QueueFull:
        pass


def attach() -> None:
    """Register the broadcast callback exactly once at app startup."""
    kite_ticker_worker.add_tick_listener(_broadcast)


def detach() -> None:
    """Tear down on app shutdown."""
    kite_ticker_worker.remove_tick_listener(_broadcast)


# ── Client lifecycle (asyncio side) ───────────────────────────────────────────

def register_client(loop: asyncio.AbstractEventLoop) -> asyncio.Queue:
    """Create + register an asyncio.Queue for one WS client. Bounded to 200
    pending batches; a stalled client drops new ticks (UI catches up via the
    next batch — we don't need lossless delivery for live LTP)."""
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    with _clients_lock:
        _clients.append((loop, q))
    return q


def unregister_client(q: asyncio.Queue) -> None:
    with _clients_lock:
        for i, (_, qq) in enumerate(_clients):
            if qq is q:
                _clients.pop(i)
                return


# ── Subscription helper (delegates to the supervisor) ────────────────────────

def ensure_subscribed(tokens: Iterable[int], *, mode: str = "ltp") -> bool:
    """Boot the supervisor if needed, then subscribe the requested tokens.

    Returns False when Kite isn't authenticated (caller should signal "log in
    first" to the client).
    """
    if not kite_client.is_authenticated():
        return False
    started = kite_ticker_worker.ensure_running()
    if not started:
        return False
    kite_ticker_worker.subscribe(set(int(t) for t in tokens if t), mode=mode)
    return True

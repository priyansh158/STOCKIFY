"""
KiteTicker background worker for STOCKIFY.

Module-level singleton supervisor that:
- Runs KiteTicker on a daemon thread (survives Streamlit reruns because
  the Python module cache is process-wide).
- On connect, re-subscribes every instrument_token persisted in
  tick_buffer.subscriptions (idempotent across reconnects / process restarts).
- On every batch of ticks, upserts to tick_buffer.ticks.
- Exposes status() for the sidebar.

Thread-safety rule: KiteTicker callbacks run on a non-main thread. They MUST
NOT call Streamlit APIs. State propagation goes through tick_buffer writes
and the supervisor's own status dict only.

Phase 2 of the Kite roadmap.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

try:
    from kiteconnect import KiteTicker
    KITETICKER_AVAILABLE = True
except ImportError:
    KiteTicker = None  # type: ignore
    KITETICKER_AVAILABLE = False

import kite_client
import tick_buffer


# Default macro instruments — subscribed at boot when authed.
# These are the NSE INDICES segment instrument_tokens, stable for years.
# We resolve via the local instruments cache so we don't hard-code the numbers.
_DEFAULT_MACRO_LABELS = [
    ("NIFTY_50",    "NIFTY 50"),
    ("BANK_NIFTY",  "NIFTY BANK"),
    ("NIFTY_IT",    "NIFTY IT"),
    ("NIFTY_PHARMA", "NIFTY PHARMA"),
    ("NIFTY_AUTO",  "NIFTY AUTO"),
    ("NIFTY_METAL", "NIFTY METAL"),
    ("NIFTY_FMCG",  "NIFTY FMCG"),
    ("NIFTY_ENERGY", "NIFTY ENERGY"),
]


def resolve_macro_tokens() -> dict[str, int]:
    """{label: instrument_token} for the macro indices found in the cache.

    Skips labels that aren't found — caller should treat missing macros as
    "feature unavailable for that index this session" rather than fatal.
    """
    out: dict[str, int] = {}
    for label, query in _DEFAULT_MACRO_LABELS:
        tok = kite_client.get_instrument_token_for_index(query)
        if tok:
            out[label] = tok
    return out


# ── Supervisor singleton ──────────────────────────────────────────────────────

class _Supervisor:
    def __init__(self) -> None:
        self.ticker: Optional["KiteTicker"] = None
        self.thread: Optional[threading.Thread] = None
        self.started: bool = False
        self.connected: bool = False
        self.last_tick_ts: int = 0
        self.connect_attempts: int = 0
        self.errors: list[str] = []
        self._lock = threading.Lock()
        # Token mode tracking — analysis tokens get "quote", everything else "ltp"
        self._modes: dict[int, str] = {}
        # Tick listeners (callable[[list[dict]], None]) — fanned out on every
        # batch in `_on_ticks`. Used by the FastAPI WS bridge in P5 so live
        # ticks reach the browser without polling the SQLite buffer. Each
        # listener runs on the KiteTicker thread; listeners MUST be cheap
        # (queue.put_nowait / loop.call_soon_threadsafe) — NEVER do blocking
        # I/O here or you'll back-pressure the entire tick stream.
        self._listeners: list = []
        self._listeners_lock = threading.Lock()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def ensure_running(self) -> bool:
        """Idempotent start. Returns True if (now) running, False otherwise."""
        if not KITETICKER_AVAILABLE:
            return False
        if not kite_client.is_authenticated():
            return False
        with self._lock:
            if self.thread is not None and self.thread.is_alive():
                return True
            payload = kite_client.load_cached_token()
            if not payload or not payload.get("access_token"):
                return False
            self.ticker = KiteTicker(
                api_key=kite_client._api_key(),
                access_token=payload["access_token"],
            )
            self.ticker.on_connect = self._on_connect
            self.ticker.on_ticks = self._on_ticks
            self.ticker.on_close = self._on_close
            self.ticker.on_error = self._on_error
            self.ticker.on_reconnect = self._on_reconnect
            self.ticker.on_noreconnect = self._on_noreconnect

            def _run():
                try:
                    self.ticker.connect(threaded=True)
                except Exception as e:
                    self._record_error(f"connect failed: {e}")

            self.thread = threading.Thread(
                target=_run, name="kite-ticker", daemon=True)
            self.thread.start()
            self.started = True
            return True

    def shutdown(self) -> None:
        with self._lock:
            try:
                if self.ticker is not None:
                    self.ticker.close()
            except Exception:
                pass
            self.ticker = None
            self.thread = None
            self.started = False
            self.connected = False

    # ── Subscription API ──────────────────────────────────────────────────────

    def subscribe(self, tokens: set[int] | list[int], kind: str = "analysis",
                  mode: str = "quote") -> None:
        """Persist + live-subscribe a set of instrument tokens."""
        tokens = {int(t) for t in tokens if t}
        if not tokens:
            return
        for t in tokens:
            tick_buffer.add_subscription(t, kind=kind)
            self._modes[t] = mode
        self._apply_subscriptions(tokens, mode)

    def unsubscribe(self, tokens: set[int] | list[int]) -> None:
        tokens = {int(t) for t in tokens if t}
        if not tokens:
            return
        for t in tokens:
            tick_buffer.remove_subscription(t)
            self._modes.pop(t, None)
        try:
            if self.ticker is not None and self.connected:
                self.ticker.unsubscribe(list(tokens))
        except Exception as e:
            self._record_error(f"unsubscribe failed: {e}")

    def _apply_subscriptions(self, tokens: set[int], mode: str) -> None:
        if not self.ticker or not self.connected:
            return  # will pick up on next on_connect
        try:
            self.ticker.subscribe(list(tokens))
            kt_mode = self._kt_mode(mode)
            if kt_mode is not None:
                self.ticker.set_mode(kt_mode, list(tokens))
        except Exception as e:
            self._record_error(f"subscribe failed: {e}")

    @staticmethod
    def _kt_mode(mode: str):
        if not KITETICKER_AVAILABLE or KiteTicker is None:
            return None
        mode = (mode or "quote").lower()
        if mode == "ltp":
            return KiteTicker.MODE_LTP
        if mode == "full":
            return KiteTicker.MODE_FULL
        return KiteTicker.MODE_QUOTE

    # ── KiteTicker callbacks (non-main thread!) ──────────────────────────────

    def _on_connect(self, ws, response):  # noqa: ANN001
        self.connected = True
        self.connect_attempts += 1
        # Re-subscribe everything persisted in tick_buffer
        all_tokens = tick_buffer.subscribed_tokens()
        if not all_tokens:
            return
        try:
            ws.subscribe(list(all_tokens))
            # Apply modes from memory; default to QUOTE for unknown.
            ltp_tokens = [t for t in all_tokens if self._modes.get(t) == "ltp"]
            quote_tokens = [t for t in all_tokens if self._modes.get(t) != "ltp"]
            if ltp_tokens:
                ws.set_mode(KiteTicker.MODE_LTP, ltp_tokens)
            if quote_tokens:
                ws.set_mode(KiteTicker.MODE_QUOTE, quote_tokens)
        except Exception as e:
            self._record_error(f"on_connect resubscribe failed: {e}")

    def _on_ticks(self, ws, ticks):  # noqa: ANN001
        if not ticks:
            return
        try:
            tick_buffer.write_ticks(ticks)
            self.last_tick_ts = int(time.time())
        except Exception as e:
            self._record_error(f"on_ticks write failed: {e}")
        # Fan out to any registered listeners (FastAPI WS bridge, etc).
        # Each listener is wrapped in try/except so one bad consumer can't
        # break the buffer write path above.
        with self._listeners_lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(ticks)
            except Exception as e:
                self._record_error(f"tick listener failed: {e}")

    def add_tick_listener(self, callback) -> None:
        """Register a callback `(ticks: list[dict]) -> None` invoked on every
        tick batch. Listener runs on the KiteTicker thread — keep it cheap."""
        with self._listeners_lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_tick_listener(self, callback) -> None:
        with self._listeners_lock:
            try:
                self._listeners.remove(callback)
            except ValueError:
                pass

    def _on_close(self, ws, code, reason):  # noqa: ANN001
        self.connected = False
        # Auth errors → stop reconnecting (token expired); the sidebar widget
        # will detect lack of fresh token and re-prompt for login.
        msg = f"socket closed: code={code} reason={reason!r}"
        self._record_error(msg)

    def _on_error(self, ws, code, reason):  # noqa: ANN001
        self._record_error(f"error: code={code} reason={reason!r}")

    def _on_reconnect(self, ws, attempts):  # noqa: ANN001
        # KiteTicker handles reconnect internally — just log.
        self._record_error(f"reconnect attempt #{attempts}")

    def _on_noreconnect(self, ws):  # noqa: ANN001
        self.connected = False
        self._record_error("noreconnect — exhausted retries")

    # ── Utilities ────────────────────────────────────────────────────────────

    def _record_error(self, msg: str) -> None:
        with self._lock:
            self.errors.append(f"[{int(time.time())}] {msg}")
            # Keep last 20
            if len(self.errors) > 20:
                self.errors = self.errors[-20:]

    def status(self) -> dict:
        return {
            "available":      KITETICKER_AVAILABLE,
            "started":        self.started,
            "alive":          bool(self.thread and self.thread.is_alive()),
            "connected":      self.connected,
            "last_tick_ts":   self.last_tick_ts,
            "subscriptions":  len(tick_buffer.subscribed_tokens()),
            "errors":         list(self.errors[-5:]),
        }


# Module-level singleton
_supervisor = _Supervisor()


# ── Public API ────────────────────────────────────────────────────────────────

def ensure_running() -> bool:
    return _supervisor.ensure_running()


def subscribe(tokens: set[int] | list[int], kind: str = "analysis",
              mode: str = "quote") -> None:
    _supervisor.subscribe(tokens, kind=kind, mode=mode)


def unsubscribe(tokens: set[int] | list[int]) -> None:
    _supervisor.unsubscribe(tokens)


def add_tick_listener(callback) -> None:
    """Register a cheap callback fired on each KiteTicker batch."""
    _supervisor.add_tick_listener(callback)


def remove_tick_listener(callback) -> None:
    _supervisor.remove_tick_listener(callback)


def status() -> dict:
    return _supervisor.status()


def shutdown() -> None:
    _supervisor.shutdown()


def ensure_macro_subscriptions() -> dict[str, int]:
    """Subscribe to the default macro index set in LTP mode. Returns the
    resolved token map ({label: token}).

    Safe to call repeatedly — subscriptions are idempotent.
    """
    if not kite_client.is_authenticated():
        return {}
    tokens = resolve_macro_tokens()
    if not tokens:
        return {}
    subscribe(set(tokens.values()), kind="macro", mode="ltp")
    return tokens

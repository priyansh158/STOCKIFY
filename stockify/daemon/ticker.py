"""The recorder's ticker supervisor (M1.5).

`RecorderSession` wires a ticker (live KiteTicker or `ReplayTicker`) to the
recorder and the subscription manager, and turns connection events into gap
rows (I-7):

* `on_connect`: close any open disconnect gap; (re)apply the subscriptions.
* `on_close` (not a normal finish): open a `disconnect` gap.
* `on_error` with 403 / token messages: a `token_expired` gap, then stop with
  exit code 76 (the launcher waits for a fresh Kite login).
* `on_noreconnect`: the library gave up reconnecting; stop with exit code 78
  (the launcher restarts the process).
* stalls: a FULL index silent for more than `stall_s` during the continuous
  phase gets an instrument-scoped `stall` gap until its packets resume.

The KiteTicker reactor is not thread-safe, so subscription changes made from
the health loop are handed to the reactor thread through `call_in_ticker`
(`reactor.callFromThread` live, a direct call in replay).

Every 5 s the session writes a `recorder_status` row (lab.sqlite).
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Callable

from stockify import store
from stockify.log import redact
from stockify.daemon import exitcodes
from stockify.daemon.clock import Clock
from stockify.daemon.recorder import Recorder
from stockify.daemon.subscriptions import FULL, Diff, SubscriptionManager
from stockify.data import calendar

log = logging.getLogger("stockify.daemon.ticker")
SEC = 1_000_000_000

RECONNECT_MAX_TRIES = 300      # library maximum (RECON M1.0)
RECONNECT_MAX_DELAY = 60


def make_live_ticker(api_key: str, access_token: str, *, role: str = "daemon"):
    """A KiteTicker with the longest reconnect budget the library allows.

    Takes a connection-budget ticket first (M5.14): raises conn_budget.BudgetExceeded when two live tickers are
    already open. The ticket rides on the ticker as `budget_ticket`; the caller releases it after closing.
    """
    from kiteconnect import KiteTicker

    from stockify.daemon import conn_budget
    ticket = conn_budget.register(role, limit=conn_budget.limit())
    try:
        ticker = KiteTicker(api_key, access_token, reconnect=True,
                            reconnect_max_tries=RECONNECT_MAX_TRIES, reconnect_max_delay=RECONNECT_MAX_DELAY)
    except BaseException:
        ticket.release()
        raise
    ticker.budget_ticket = ticket
    return ticker


def live_call_in_ticker(fn: Callable, *args) -> None:
    from twisted.internet import reactor
    reactor.callFromThread(fn, *args)


def direct_call(fn: Callable, *args) -> None:
    fn(*args)


class RecorderSession:
    def __init__(self, ticker, recorder: Recorder, manager: SubscriptionManager, clock: Clock, *,
                 call_in_ticker: Callable = direct_call, health_every_s: float = 5.0,
                 stall_s: float = 30.0, stall_group: str = "CM"):
        self.ticker = ticker
        self.recorder = recorder
        self.manager = manager
        self.clock = clock
        self.call_in_ticker = call_in_ticker
        self.health_every_ns = int(health_every_s * SEC)
        self.stall_ns = int(stall_s * SEC)
        self.stall_group = stall_group
        self.exit_code = exitcodes.OK
        self.connected = False
        self.stopped = threading.Event()
        self._lock = threading.Lock()
        # Every subscription diff (re-centre, first build, pin) is computed and applied under this lock, on the ticker
        # thread: one order, so `manager.current` is always what Kite holds (RM 5.5).
        self.sub_lock = threading.RLock()
        self._sent_prices: dict[int, int] = {}
        self._disconnect_gap: int | None = None
        self._stall_gaps: dict[int, int] = {}
        self._token_iid: dict[int, int] = {}
        self._prices: dict[int, int] = {}
        self._last_packet: dict[int, int] = {}
        self._last_health_ns = 0
        self._last_packets = 0
        self._ref_ids = manager.reference_ids()
        # Gap taps (M5.1 fan-out): called as (phase "start"|"end", reason, t_ns, instrument_id | None) beside every
        # landing open_gap/close_gap, with the same time, on the thread that saw the event. They must be cheap
        # (the dispatcher's `put_gap` is a put_nowait); a failing tap is counted and logged once, never raised.
        self.gap_taps: list[Callable[[str, str, int, int | None], None]] = []
        self.gap_tap_errors = 0
        recorder.on_packet = self._on_packet           # writer-thread hook (cheap)
        ticker.on_connect = self._on_connect
        ticker.on_message = recorder.on_message
        ticker.on_close = self._on_close
        ticker.on_error = self._on_error
        ticker.on_noreconnect = self._on_noreconnect

    # ── subscriptions ────────────────────────────────────────────────────────
    def _apply(self, diff: Diff, ws=None) -> None:  # noqa: ANN001
        ws = ws or self.ticker
        for sub in diff.subscribe + diff.mode_change:
            self._token_iid[sub.token] = sub.instrument_id
        groups: dict[str, list[int]] = {}
        for sub in diff.subscribe + diff.mode_change:
            groups.setdefault(sub.mode, []).append(sub.token)
        new = [s.token for s in diff.subscribe]
        if new:
            ws.subscribe(new)
        for mode, tokens in groups.items():
            ws.set_mode(mode, tokens)
        gone = [s.token for s in diff.unsubscribe]
        if gone:
            ws.unsubscribe(gone)

    def _reconcile(self, prices: dict[int, int]) -> None:
        """Re-centre the chains on `prices` and subscribe what changed. Runs on the ticker thread, in the same order
        as every pin change, so a diff is never applied after a later one."""
        with self.sub_lock:
            diff = self.manager.update_prices(prices)
            if not diff.empty():
                self._apply(diff)
            if any(c.center is None for c in self.manager.chains):
                first = self.manager.build(prices)          # chains still waiting for a price
                if not first.empty():
                    self._apply(first)

    def _apply_all(self, ws) -> None:  # noqa: ANN001
        """After a (re)connect: subscribe everything currently wanted."""
        diff = Diff(subscribe=list(self.manager.current.values()))
        self._apply(diff, ws)

    def start(self) -> None:
        """Choose the first set (chains wait for their reference price)."""
        self.manager.build(self._prices)

    def _gap(self, phase: str, reason: str, t_ns: int, iid: int | None = None) -> None:
        for tap in self.gap_taps:
            try:
                tap(phase, reason, t_ns, iid)
            except Exception:  # noqa: BLE001
                self.gap_tap_errors += 1
                if self.gap_tap_errors == 1:
                    log.exception("gap tap failed (counted in gap_tap_errors; its further failures are not logged)")

    # ── ticker callbacks (ticker thread) ─────────────────────────────────────
    def _on_connect(self, ws, response) -> None:  # noqa: ANN001
        now = self.clock.now_ns()
        with self._lock:
            self.connected = True
            if self._disconnect_gap is not None:
                self.recorder.landing.close_gap(self._disconnect_gap, now)
                self._disconnect_gap = None
                self._gap("end", "disconnect", now)
        self._apply_all(ws)
        log.info("ticker connected; %d subscriptions", len(self.manager.current))

    def _on_close(self, ws, code, reason) -> None:  # noqa: ANN001
        # Every close is logged loudly (M5.14); never the websocket's URL, which carries the access token.
        log.error("ticker closed: code %s, reason %s", code, redact(reason))
        with self._lock:
            self.connected = False
            if self.stopped.is_set() or code == 1000:
                return
            if self._disconnect_gap is None:
                now = self.clock.now_ns()
                self._disconnect_gap = self.recorder.landing.open_gap(now, "disconnect")
                self._gap("start", "disconnect", now)

    def _on_error(self, ws, code, reason) -> None:  # noqa: ANN001
        log.error("ticker error: code %s, reason %s", code, redact(reason))
        if str(code) == "403" or "token" in str(reason).lower():
            with self._lock:
                if self._disconnect_gap is not None:
                    now = self.clock.now_ns()
                    self.recorder.landing.close_gap(self._disconnect_gap, now)
                    self._disconnect_gap = None
                    self._gap("end", "disconnect", now)
                now = self.clock.now_ns()
                self.recorder.landing.open_gap(now, "token_expired")
                self._gap("start", "token_expired", now)
            self.stop(exitcodes.TOKEN_EXPIRED)

    def _on_noreconnect(self, ws) -> None:  # noqa: ANN001
        if self.exit_code == exitcodes.OK:
            self.stop(exitcodes.RECONNECT_EXHAUSTED)

    # ── writer-thread hook ───────────────────────────────────────────────────
    def _on_packet(self, iid: int | None, token: int, ltp: int, recv_ns: int) -> None:
        if iid is None:
            return
        self._last_packet[iid] = recv_ns
        if iid in self._ref_ids:
            self._prices[iid] = ltp

    # ── health loop (main thread) ────────────────────────────────────────────
    def tick(self) -> None:
        """Re-centre chains, detect stalls and write health; call about once a second."""
        now = self.clock.now_ns()
        if self._prices and self._prices != self._sent_prices:
            self._sent_prices = dict(self._prices)
            self.call_in_ticker(self._reconcile, dict(self._prices))
        self._check_stalls(now)
        if now - self._last_health_ns >= self.health_every_ns:
            self._write_health(now)

    def _check_stalls(self, now: int) -> None:
        if not self.connected or calendar.phase_at(self.stall_group, now) != "CONTINUOUS":
            return
        for sub in self.manager.current.values():
            if sub.mode != FULL or not sub.reason.startswith("index:"):
                continue
            last = self._last_packet.get(sub.instrument_id)
            silent = last is None or now - last > self.stall_ns
            gap = self._stall_gaps.get(sub.instrument_id)
            if silent and gap is None and last is not None:
                self._stall_gaps[sub.instrument_id] = self.recorder.landing.open_gap(
                    last, "stall", f"instrument:{sub.instrument_id}")
                self._gap("start", "stall", last, sub.instrument_id)
            elif not silent and gap is not None:
                self.recorder.landing.close_gap(gap, last)
                del self._stall_gaps[sub.instrument_id]
                self._gap("end", "stall", last, sub.instrument_id)

    def _write_health(self, now: int) -> None:
        stats = self.recorder.stats
        elapsed = (now - self._last_health_ns) / SEC if self._last_health_ns else 0
        rate = (stats.packets - self._last_packets) / elapsed if elapsed > 0 else 0.0
        ages: dict[str, int | None] = {}
        for tier in ("full", "quote"):
            ids = [s.instrument_id for s in self.manager.current.values() if s.mode == tier]
            seen = [self._last_packet[i] for i in ids if i in self._last_packet]
            ages[tier] = (now - max(seen)) // 1_000_000 if seen else None
        counts = self.manager.counts()
        size = self.recorder.landing.path.stat().st_size if self.recorder.landing.path.exists() else 0
        store.write_recorder_status({
            "wall_ns": now, "connected": int(self.connected),
            "subs_full": counts["full"], "subs_quote": counts["quote"],
            "packets_per_s": round(rate, 3), "queue_depth": self.recorder.queue.qsize(),
            "writer_lag_ms": None, "last_packet_age_ms_json": json.dumps(ages, sort_keys=True),
            "bytes_today": size,
        })
        self._last_health_ns, self._last_packets = now, stats.packets

    # ── shutdown ─────────────────────────────────────────────────────────────
    def end_session(self) -> None:
        """Planned end (after the F&O close): unsubscribe everything, log it."""
        diff = self.manager.unsubscribe_all()
        if diff.unsubscribe and self.connected:
            self.call_in_ticker(self.ticker.unsubscribe, [s.token for s in diff.unsubscribe])
        self.stop(exitcodes.OK)

    def stop(self, code: int) -> None:
        if self.stopped.is_set():
            return
        self.exit_code = code
        self.stopped.set()
        now = self.clock.now_ns()
        for iid, gap in list(self._stall_gaps.items()):
            self.recorder.landing.close_gap(gap, now)
            self._gap("end", "stall", now, iid)
        self._stall_gaps.clear()

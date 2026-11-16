"""The tick recorder's queue and writer (M1.5; A1 §3.1, D-08, I-7).

Two halves:

* `on_message(ws, payload, is_binary)` runs on the KiteTicker thread. It only
  stamps the frame (`frame_no`, `clock.now_ns()`, `clock.mono_ns()`) and
  puts it on a queue ("listeners must be cheap"). A full queue never blocks:
  the frame is counted as dropped and an alarm is raised.
* The writer thread parses frames, resolves token → instrument_id with the
  day's map, assigns `seq` (strictly increasing for the day, continuing from
  the landing file after a restart) and inserts batches every 250 ms or
  2,000 rows, storing the parsed core fields **and the raw packet bytes**.

Restarts are explicit gaps: a new Recorder on an existing day file records a
`process_stop` gap from the previous run's last heartbeat to now. Missing data
is never filled in (I-7).
"""
from __future__ import annotations

import collections
import itertools
import json
import logging
import queue
import socket
import threading
from dataclasses import dataclass, field
from typing import Callable

from stockify.daemon import kite_packets
from stockify.daemon.clock import Clock
from stockify.daemon.landing import LandingDB

RECORDER_VERSION = "1"
MINUTE_NS = 60 * 1_000_000_000

log = logging.getLogger("stockify.daemon.recorder")

# Kite text frame types whose contents carry no account data and help read the log. Anything else (order postbacks,
# "message", "trade", unknown or unparseable text) is logged by type alone (RM 7.4): they hold the owner's account id.
HARMLESS_TEXT_TYPES = frozenset({"instruments_meta", "app_code", "error"})


def text_frame_note(payload) -> str:  # noqa: ANN001
    """The log line for a Kite text frame: contents only for HARMLESS_TEXT_TYPES, else just the type."""
    try:
        text = payload if isinstance(payload, str) else bytes(payload).decode("utf-8", "replace")
        doc = json.loads(text)
        kind = doc.get("type") if isinstance(doc, dict) else None
    except Exception:  # noqa: BLE001
        kind = None
    if not isinstance(kind, str):
        return "text frame: unrecognised (contents not logged)"
    if kind in HARMLESS_TEXT_TYPES:
        return f"text frame: {text[:200]}"
    label = "order update" if kind == "order" else f"{kind[:30]!r} message" if kind.isprintable() else "message"
    return f"text frame: {label} (contents not logged)"


@dataclass
class RecorderStats:
    frames: int = 0
    packets: int = 0
    heartbeats: int = 0
    text_frames: int = 0
    bad_frames: int = 0
    unknown_tokens: int = 0
    dropped_frames: int = 0
    tap_errors: int = 0
    batches: int = 0
    alarms: list[str] = field(default_factory=list)


class Recorder:
    def __init__(self, landing: LandingDB, resolve: Callable[[int], int | None], clock: Clock, *,
                 batch_rows: int = 2000, batch_ms: int = 250, queue_max: int = 200_000,
                 meta: dict | None = None):
        self.landing = landing
        self.resolve = resolve
        self.clock = clock
        self.batch_rows = batch_rows
        self.batch_ns = batch_ms * 1_000_000
        self.queue: queue.Queue = queue.Queue(maxsize=queue_max)
        self.stats = RecorderStats()
        self._next_seq = landing.max_seq() + 1
        self._frame_counter = itertools.count(landing.max_frame_no() + 1)
        self._rows: list[tuple] = []
        self._heartbeats: dict[int, int] = {}
        self._last_flush_mono = clock.mono_ns()
        self._last_recv_ns = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._started_meta = dict(meta or {})
        # Optional writer-thread hook: on_packet(instrument_id, token, ltp, recv_ns). Must be cheap.
        self.on_packet: Callable[[int | None, int, int, int], None] | None = None
        # Frames the queue refused: (frame_no, recv_ns), appended by the ticker thread (deque appends are atomic),
        # drained and written as `queue_full` gap rows by the writer at each flush (RM 5.3).
        self._dropped: collections.deque = collections.deque()
        # Subscription log rows from the ticker thread (re-centres, pins): appended here (atomic), written by the writer
        # at each flush, so the reactor thread never waits for the landing file (RM 5.5).
        self._sub_rows: collections.deque = collections.deque()
        self._taps: list[Callable[[tuple], None]] = []
        self._tap_failures: list[int] = []

    def log_subscriptions(self, rows: list[tuple]) -> None:
        """`SubscriptionManager.log_rows`: any thread, never blocks; the writer stores them at its next flush."""
        self._sub_rows.extend(rows)

    def _write_subscriptions(self) -> None:
        rows = []
        while True:
            try:
                rows.append(self._sub_rows.popleft())
            except IndexError:
                break
        if rows:
            self.landing.log_subscriptions(rows)

    # ── frame taps (M5.1 fan-out) ────────────────────────────────────────────
    @property
    def taps(self) -> tuple:
        """The registered taps, read-only. Register with `add_tap`."""
        return tuple(self._taps)

    def add_tap(self, q) -> None:  # noqa: ANN001
        """Register a consumer queue. Every stamped item (frames, heartbeats, text frames) is offered to its
        `put_nowait` on the KiteTicker thread, right after the landing queue, so a tap must be a bounded queue the
        consumer owns: nothing that blocks, locks, does I/O or works per frame. Only an object with `put_nowait`
        is accepted; a full queue raises `queue.Full`, which is counted and never reaches the recorder's frames."""
        put = getattr(q, "put_nowait", None)
        if not callable(put):
            raise TypeError(f"a frame tap must be a queue with put_nowait, got {type(q).__name__}")
        self._taps.append(put)
        self._tap_failures.append(0)

    # ── start / stop ─────────────────────────────────────────────────────────
    def begin(self) -> None:
        """Record the restart gap (if any) and the run's metadata."""
        now = self.clock.now_ns()
        previous = self.landing.meta().get("last_alive_ns")
        if previous is not None and int(previous) < now:
            self.landing.add_gap(int(previous), now, "process_stop")
        try:
            import kiteconnect.__version__ as kv
            kite_version = kv.__version__
        except ImportError:
            kite_version = "absent"
        self.landing.set_meta(trade_date=self.landing.day.isoformat(),
                              recorder_version=RECORDER_VERSION,
                              parser_version=kite_packets.PARSER_VERSION,
                              kiteconnect_version=kite_version,
                              host=socket.gethostname(),
                              last_alive_ns=now,
                              **self._started_meta)

    def start(self) -> None:
        self.begin()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="recorder-writer", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self.drain()

    # ── listener side (KiteTicker thread): must stay cheap ───────────────────
    def on_message(self, ws, payload, is_binary) -> None:  # noqa: ANN001
        # Only data frames get a frame number: heartbeats are kept as per-minute
        # counts and text frames are logged, so numbering them would leave holes
        # a replay could never reproduce.
        data_frame = bool(is_binary) and len(payload) > 1
        item = (next(self._frame_counter) if data_frame else 0, self.clock.now_ns(), self.clock.mono_ns(),
                bytes(payload) if is_binary else payload, bool(is_binary))
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            self.stats.dropped_frames += 1
            if self.stats.dropped_frames == 1:
                self.stats.alarms.append("queue_full")
            self._dropped.append((item[0], item[1]))
            log.error("recorder queue full: frame dropped (%d so far)", self.stats.dropped_frames)
        # After the landing queue, so a tap can never cost the recorder a frame; a failing tap is counted, not raised.
        for i, tap in enumerate(self._taps):
            try:
                tap(item)
            except Exception:  # noqa: BLE001
                self.stats.tap_errors += 1
                self._tap_failures[i] += 1
                if self._tap_failures[i] == 1:
                    log.exception("frame tap %d failed (counted in tap_errors; its further failures are not logged)", i)

    # ── writer side ──────────────────────────────────────────────────────────
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self.queue.get(timeout=0.05)
            except queue.Empty:
                self.maybe_flush()
                continue
            self.process(item)
            self.maybe_flush()

    def drain(self) -> None:
        while True:
            try:
                self.process(self.queue.get_nowait())
            except queue.Empty:
                break
        self.flush()

    def process(self, item: tuple) -> None:
        frame_no, recv_ns, mono_ns, payload, is_binary = item
        self._last_recv_ns = recv_ns
        if not is_binary:
            self.stats.text_frames += 1
            log.info("%s", text_frame_note(payload))
            return
        try:
            frame = kite_packets.split_frame(payload)
        except kite_packets.PacketError as e:
            self.stats.bad_frames += 1
            log.warning("bad frame %d (%d bytes): %s", frame_no, len(payload), e)
            return
        self.stats.frames += 1
        if frame.heartbeat:
            self.stats.heartbeats += 1
            minute = recv_ns // MINUTE_NS * MINUTE_NS
            self._heartbeats[minute] = self._heartbeats.get(minute, 0) + 1
            return
        for raw in frame.packets:
            try:
                p = kite_packets.parse_packet(raw)
            except kite_packets.PacketError as e:
                self.stats.bad_frames += 1
                log.warning("bad packet in frame %d: %s", frame_no, e)
                continue
            iid = self.resolve(p["instrument_token"])
            if iid is None:
                self.stats.unknown_tokens += 1
            self._rows.append((self._next_seq, frame_no, recv_ns, mono_ns, iid, p["instrument_token"],
                               p["mode"], p["pkt_len"], p["ltp"], p["last_qty"], p["atp"], p["volume"],
                               p["buy_qty"], p["sell_qty"], p["open"], p["high"], p["low"], p["close"],
                               p["last_trade_ts_s"], p["oi"], p["oi_high"], p["oi_low"], p["exch_ts_s"],
                               raw))
            self._next_seq += 1
            self.stats.packets += 1
            if self.on_packet is not None:
                self.on_packet(iid, p["instrument_token"], p["ltp"], recv_ns)
        if len(self._rows) >= self.batch_rows:
            self.flush()

    def _write_dropped(self) -> None:
        """Turn the frames the queue refused into `queue_full` gap rows, one per run of consecutive frame_nos.
        Heartbeats and text frames carry no frame_no (0) and are only counted in `stats.dropped_frames`."""
        runs: list[list[int]] = []
        while True:
            try:
                frame_no, recv_ns = self._dropped.popleft()
            except IndexError:
                break
            if not frame_no:
                continue
            if runs and frame_no == runs[-1][1] + 1:
                runs[-1][1], runs[-1][3] = frame_no, recv_ns
            else:
                runs.append([frame_no, frame_no, recv_ns, recv_ns])
        if runs:
            self.landing.add_dropped_frames([tuple(r) for r in runs])

    def maybe_flush(self) -> None:
        if self.clock.mono_ns() - self._last_flush_mono >= self.batch_ns:
            self.flush()

    def flush(self) -> None:
        self._last_flush_mono = self.clock.mono_ns()
        self._write_dropped()
        self._write_subscriptions()
        if not self._rows and not self._heartbeats:
            return
        rows, beats = self._rows, self._heartbeats
        self._rows, self._heartbeats = [], {}
        alive = max(self._last_recv_ns, self.clock.now_ns())
        self.landing.write_batch(rows, beats, {"last_alive_ns": str(alive)})
        self.stats.batches += 1

    @property
    def next_seq(self) -> int:
        return self._next_seq

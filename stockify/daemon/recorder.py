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

import itertools
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


@dataclass
class RecorderStats:
    frames: int = 0
    packets: int = 0
    heartbeats: int = 0
    text_frames: int = 0
    bad_frames: int = 0
    unknown_tokens: int = 0
    dropped_frames: int = 0
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
            log.error("recorder queue full: frame dropped (%d so far)", self.stats.dropped_frames)

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
            log.info("text frame: %s", payload if isinstance(payload, str) else payload[:200])
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

    def maybe_flush(self) -> None:
        if self.clock.mono_ns() - self._last_flush_mono >= self.batch_ns:
            self.flush()

    def flush(self) -> None:
        self._last_flush_mono = self.clock.mono_ns()
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

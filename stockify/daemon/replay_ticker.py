"""Replay a recorded day through the real daemon code (M1.6, D-15).

`ReplayTicker` has the live ticker's interface (`subscribe`, `set_mode`,
`unsubscribe`, `connect`, `close`, `stop` and the `on_connect` /
`on_message` / `on_close` / `on_error` / `on_noreconnect` callbacks). Instead
of a socket it reads a landing file and rebuilds the original binary frames
exactly: packets grouped by `frame_no`, in `seq` order, behind the count and
length prefix. Heartbeats are re-emitted from the per-minute counts.

* Speed: `1x` and `10x` keep the original inter-frame gaps divided by the
  factor; `max` delivers as fast as possible.
* Subscriptions are honoured: only subscribed tokens are delivered, and a
  packet recorded in FULL mode is cut to the mode asked for (QUOTE: 44 bytes
  tradable, 28 index; LTP: 8).
* `ReplayClock` follows the recorded receive times, so components stamping
  `clock.now_ns()` reproduce the original timeline.
* Fault injection for tests: `disconnect@HH:MM:SS+90s`, `token_expiry@HH:MM:SS`,
  `kill@HH:MM:SS` (IST on the replayed day).

Select it with `--ticker replay:<YYYY-MM-DD>:<1x|10x|max>`.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import date, datetime

from stockify import timeutil
from stockify.daemon import kite_packets as kp
from stockify.daemon.clock import Clock
from stockify.daemon.landing import LandingDB

MODE_LTP, MODE_QUOTE, MODE_FULL = "ltp", "quote", "full"
_CUT = {MODE_LTP: {8: 8, 28: 8, 32: 8, 44: 8, 184: 8},
        MODE_QUOTE: {8: 8, 28: 28, 32: 28, 44: 44, 184: 44},
        MODE_FULL: {8: 8, 28: 28, 32: 32, 44: 44, 184: 184}}
_SPEC = re.compile(r"^replay:(\d{4}-\d{2}-\d{2}):(\d+x|max)$")
_FAULT = re.compile(r"^(disconnect|token_expiry|kill)@(\d{2}:\d{2}:\d{2})(?:\+(\d+)s)?$")


class LandingSource:
    """Packets and heartbeats from a landing file."""

    def __init__(self, landing: LandingDB):
        self.landing = landing

    def heartbeats(self):
        return [(r["minute_ns"], r["count"]) for r in
                self.landing.conn.execute("SELECT minute_ns, count FROM heartbeats ORDER BY minute_ns")]

    def packets(self):
        """(frame_no, recv_ns, mono_ns, instrument_token, raw) in seq order."""
        cur = self.landing.conn.execute(
            "SELECT frame_no, recv_ns, mono_ns, instrument_token, raw FROM packets ORDER BY seq")
        for r in cur:
            yield r["frame_no"], r["recv_ns"], r["mono_ns"], r["instrument_token"], bytes(r["raw"])

    def first_recv_ns(self) -> int | None:
        return self.landing.conn.execute("SELECT MIN(recv_ns) FROM packets").fetchone()[0]


class ArchiveSource:
    """Packets and heartbeats from a verified Parquet archive (M1.7 layout, D-37)."""

    def __init__(self, folder):
        self.folder = folder

    def heartbeats(self):
        import pyarrow.parquet as pq
        t = pq.read_table(self.folder / "heartbeats.parquet").to_pydict()
        return sorted(zip(t.get("minute_ns", []), t.get("count", [])))

    def packets(self):
        import heapq
        from stockify.data import compact
        cols = ["seq", "frame_no", "recv_ns", "mono_ns", "instrument_token", "raw"]
        streams = [compact.iter_rows(self.folder / f"{n}.parquet", cols) for n in ("full", "quote", "index")]
        for _seq, frame_no, recv_ns, mono_ns, token, raw in heapq.merge(*streams, key=lambda r: r[0]):
            yield frame_no, recv_ns, mono_ns, token, raw

    def first_recv_ns(self) -> int | None:
        first = next(self.packets(), None)
        return first[1] if first else None


def source_for(day: date, path=None):
    """The verified archive when there is one (preferred), otherwise the landing file."""
    from stockify import paths
    if path is None:
        folder = paths.ticks_archive_dir() / f"date={day:%Y-%m-%d}"
        if (folder / "meta.json").exists():
            return ArchiveSource(folder)
    return LandingSource(LandingDB(day, path, readonly=True))


class ReplayClock(Clock):
    """Wall and monotonic time taken from the frame being replayed."""

    def __init__(self, start_ns: int = 0, mono_ns: int = 0):
        self._now, self._mono = start_ns, mono_ns
        self._lock = threading.Lock()

    def now_ns(self) -> int:
        with self._lock:
            return self._now

    def mono_ns(self) -> int:
        with self._lock:
            return self._mono

    def set(self, now_ns: int, mono_ns: int | None = None) -> None:
        with self._lock:
            mono = self._mono + max(0, now_ns - self._now) if mono_ns is None else mono_ns
            self._now, self._mono = now_ns, mono

    def sleep_until(self, t_ns: int, stop: threading.Event | None = None) -> bool:
        if stop is not None and stop.is_set():
            return False
        if t_ns > self.now_ns():
            self.set(t_ns)
        return True


@dataclass(frozen=True)
class Fault:
    kind: str           # disconnect | token_expiry | kill
    at_ns: int
    duration_ns: int = 0


def parse_spec(spec: str) -> tuple[date, float | None]:
    """'replay:2026-10-28:10x' -> (date, 10.0); 'max' -> None (no sleeping)."""
    m = _SPEC.match(spec)
    if not m:
        raise ValueError(f"bad ticker spec {spec!r}; expected replay:YYYY-MM-DD:<Nx|max>")
    speed = None if m.group(2) == "max" else float(m.group(2)[:-1])
    if speed is not None and speed <= 0:
        raise ValueError("replay speed must be positive")
    return date.fromisoformat(m.group(1)), speed


def parse_fault(text: str, day: date) -> Fault:
    m = _FAULT.match(text)
    if not m:
        raise ValueError(f"bad fault {text!r}; expected kind@HH:MM:SS[+Ns]")
    seconds = int(m.group(3) or 0)
    return Fault(m.group(1), timeutil.ist_ns(day, m.group(2)), seconds * 1_000_000_000)


class ReplayTicker:
    MODE_LTP, MODE_QUOTE, MODE_FULL = MODE_LTP, MODE_QUOTE, MODE_FULL

    def __init__(self, source, clock: ReplayClock, *, speed: float | None = None,
                 faults: list[Fault] | None = None, heartbeats: bool = True):
        # A LandingDB is accepted directly for convenience.
        self.source = LandingSource(source) if isinstance(source, LandingDB) else source
        self.clock = clock
        self.speed = speed
        self.faults = sorted(faults or [], key=lambda f: f.at_ns)
        self.heartbeats = heartbeats
        self.modes: dict[int, str] = {}
        self.on_connect = None
        self.on_message = None
        self.on_close = None
        self.on_error = None
        self.on_noreconnect = None
        self.connected = False
        self.killed = False
        self.finished = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.delivered_frames = 0

    # ── KiteTicker-shaped API ────────────────────────────────────────────────
    def subscribe(self, tokens) -> None:
        for t in tokens:
            self.modes.setdefault(int(t), MODE_QUOTE)

    def set_mode(self, mode: str, tokens) -> None:
        for t in tokens:
            self.modes[int(t)] = mode

    def unsubscribe(self, tokens) -> None:
        for t in tokens:
            self.modes.pop(int(t), None)

    def connect(self, threaded: bool = False, **_ignored) -> None:
        if threaded:
            self._thread = threading.Thread(target=self.run, name="replay-ticker", daemon=True)
            self._thread.start()
        else:
            self.run()

    def close(self, code=None, reason=None) -> None:  # noqa: ANN001
        self._stop.set()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(10)

    def is_connected(self) -> bool:
        return self.connected

    # ── frames ───────────────────────────────────────────────────────────────
    def _events(self):
        """(recv_ns, mono_ns, kind, payload) for every frame, in time order.

        Heartbeats are spread 1 ms apart from the start of their minute; data
        frames carry their recorded times.
        """
        beats = []
        if self.heartbeats:
            for minute_ns, count in self.source.heartbeats():
                beats.extend(minute_ns + i * 1_000_000 for i in range(count))
        b = 0
        current, packets, rec_ns, rec_mono = None, [], 0, 0
        for frame_no, recv_ns, mono_ns, token, raw in self.source.packets():
            if frame_no != current:
                if current is not None:
                    while b < len(beats) and beats[b] < rec_ns:
                        yield beats[b], None, "beat", b"\x00"
                        b += 1
                    yield rec_ns, rec_mono, "data", packets
                current, packets = frame_no, []
                rec_ns, rec_mono = recv_ns, mono_ns
            packets.append((token, raw))
        if current is not None:
            while b < len(beats) and beats[b] < rec_ns:
                yield beats[b], None, "beat", b"\x00"
                b += 1
            yield rec_ns, rec_mono, "data", packets
        while b < len(beats):
            yield beats[b], None, "beat", b"\x00"
            b += 1

    def _filter(self, packets: list[tuple[int, bytes]]) -> list[bytes]:
        out = []
        for token, raw in packets:
            mode = self.modes.get(token)
            if mode is None:
                continue
            out.append(raw[:_CUT[mode][len(raw)]])
        return out

    def _wait(self, prev_ns: int | None, t_ns: int) -> None:
        if self.speed is None or prev_ns is None or t_ns <= prev_ns:
            return
        delay = (t_ns - prev_ns) / 1e9 / self.speed
        self._stop.wait(delay)

    def _fire(self, cb, *args) -> None:
        if cb is not None:
            cb(self, *args)

    def run(self) -> None:
        self.connected = True
        self._fire(self.on_connect, {})
        faults = list(self.faults)
        down_until = None
        prev = None
        try:
            for t_ns, mono, kind, payload in self._events():
                if self._stop.is_set():
                    break
                while faults and faults[0].at_ns <= t_ns:
                    fault = faults.pop(0)
                    self.clock.set(fault.at_ns)
                    if fault.kind == "disconnect":
                        self.connected = False
                        self._fire(self.on_close, 1006, "replay: injected disconnect")
                        down_until = fault.at_ns + fault.duration_ns
                    elif fault.kind == "token_expiry":
                        self.connected = False
                        self._fire(self.on_error, 403, "replay: injected token expiry")
                        self._fire(self.on_noreconnect)
                        return
                    elif fault.kind == "kill":
                        self.killed = True
                        return
                if down_until is not None:
                    if t_ns < down_until:
                        continue                      # frames during an outage never arrive
                    self.clock.set(down_until)
                    self.connected = True
                    self._fire(self.on_connect, {"reconnect": True})
                    down_until = None
                self._wait(prev, t_ns)
                prev = t_ns
                if kind == "beat":
                    self.clock.set(t_ns)
                    self._fire(self.on_message, payload, True)
                    continue
                kept = self._filter(payload)
                if not kept:
                    continue
                self.clock.set(t_ns, mono)
                self._fire(self.on_message, kp.build_frame(kept), True)
                self.delivered_frames += 1
        finally:
            if not self.killed:
                self.connected = False
                self._fire(self.on_close, 1000, "replay finished")
            self.finished.set()


def open_replay(spec: str, *, faults: list[str] | None = None, path=None):
    """Build (ticker, clock) for a `replay:<date>:<speed>` spec (archive preferred)."""
    day, speed = parse_spec(spec)
    source = source_for(day, path)
    first = source.first_recv_ns() or timeutil.ist_ns(day, "09:00")
    clock = ReplayClock(int(first) - 1_000_000)
    ticker = ReplayTicker(source, clock, speed=speed, faults=[parse_fault(f, day) for f in faults or []])
    return ticker, clock


def ist_label(t_ns: int) -> str:
    return datetime.strftime(timeutil.ns_to_ist(t_ns), "%H:%M:%S")


__all__ = ["ReplayTicker", "ReplayClock", "Fault", "parse_spec", "parse_fault", "open_replay", "ist_label"]

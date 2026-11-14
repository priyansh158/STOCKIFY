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
  `kill@HH:MM:SS` (IST on the replayed day). A duration on `token_expiry` is where the restart resumes
  (`resume_at_ns`: the frames in between are lost); on `disconnect` it is the outage inside this run. `kill` runs
  `on_kill` (the daemon drains and syncs) and then ends the process with `terminate(137)` (D-71).
* Resume (GK 5.3): `resume_after=(frame_no, recv_ns)` skips every frame and heartbeat stamped at or before the old
  process's last dispatched frame, and `resume_not_before_ns` skips everything before a fault's `T + N`. The
  comparison is on the stamp, never on `frame_no` (a restarted recorder numbers its frames afresh).

Select it with `--ticker replay:<YYYY-MM-DD>:<1x|10x|max>`.
"""
from __future__ import annotations

import ctypes
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from datetime import date, datetime

from stockify import timeutil
from stockify.daemon import kite_packets as kp
from stockify.daemon.clock import Clock
from stockify.daemon.landing import LandingDB

log = logging.getLogger("stockify.daemon.replay_ticker")
MODE_LTP, MODE_QUOTE, MODE_FULL = "ltp", "quote", "full"
_CUT = {MODE_LTP: {8: 8, 28: 8, 32: 8, 44: 8, 184: 8},
        MODE_QUOTE: {8: 8, 28: 28, 32: 28, 44: 44, 184: 44},
        MODE_FULL: {8: 8, 28: 28, 32: 32, 44: 44, 184: 184}}
_SPEC = re.compile(r"^replay:(\d{4}-\d{2}-\d{2}):(\d+x|max)$")
_FAULT = re.compile(r"^(disconnect|token_expiry|kill)@(\d{2}:\d{2}:\d{2})(?:\+(\d+)s)?$")


# Sources come from stockify.data.ticks (archive first, D-38); kept under the
# old names for callers and tests.
from stockify.data.ticks import ArchiveDay as ArchiveSource  # noqa: E402
from stockify.data.ticks import LandingDay as LandingSource  # noqa: E402


# `--replay-sandbox` moves every output (and the state folder) away from the real lab, but the recording being replayed
# lives in the real lab. This is the real lab's folder while a replay is sandboxed: the source is read from it strictly
# read-only (parquet reads; the landing file as `mode=ro&immutable=1`), never written, never created.
_SOURCE_LAB: Path | None = None


def set_source_lab(lab: Path | None) -> None:
    """Read replay sources from `lab` (the real lab folder) instead of the current state folder; None undoes it."""
    global _SOURCE_LAB
    _SOURCE_LAB = Path(lab) if lab is not None else None


def seed_refdata_from(lab: Path, target: Path) -> bool:
    """Give a sandbox its own copy of the real lab's instrument reference data (a consistent SQLite backup taken through a
    read-only connection, so the real file is never written). False when there is nothing to copy or the target exists."""
    import sqlite3
    from stockify import paths
    source = Path(lab) / "refdata.sqlite"
    if target.exists() or not source.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(paths.readonly_uri(source), uri=True)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return True


def _source_from_lab(day: date, lab: Path):
    """The day from the real lab without touching it: the archive when it has a `meta.json`, else the landing file
    opened immutable (refused while its `-wal` holds rows: a recorder may be writing and an immutable read would miss
    them). None when the lab never recorded the day."""
    from stockify.data import ticks
    folder = lab / "ticks" / "archive" / f"date={day:%Y-%m-%d}"
    if (folder / "meta.json").exists():
        return ticks.ArchiveDay(day, folder)
    landing = lab / "ticks" / "landing" / f"ticks_{day:%Y%m%d}.sqlite"
    if not landing.exists():
        return None
    for suffix in ticks.WRITER_FILES:
        side = landing.with_name(landing.name + suffix)
        if side.is_file() and side.stat().st_size > 0:
            raise RuntimeError(f"the recording for {day} is busy, not replayed (its {suffix} file is not empty: the "
                               "recorder may be writing); stop the recorder or replay the archived day")
    return ticks.LandingDay(day, landing, immutable=True)


def source_for(day: date, path=None):
    """The verified archive when there is one (preferred), otherwise the landing file. While a replay is sandboxed the
    day is read, read-only, from the real lab (`set_source_lab`), unless the sandbox holds its own recording of that day
    (a copy the owner or a test put there), which wins: it is the folder the run was pointed at."""
    from stockify.data import ticks
    if path is None and _SOURCE_LAB is not None:
        own = ticks.open_day(day)
        if own is not None:
            return own
        found = _source_from_lab(day, _SOURCE_LAB)
        if found is None:
            raise FileNotFoundError(f"no recording for {day}")
        return found
    found = ticks.open_day(day, landing_path=path)
    if found is None:
        raise FileNotFoundError(f"no recording for {day}")
    return found


KILLED_EXIT = 137


def terminate_process(code: int = KILLED_EXIT) -> None:
    """End this process the way the OS would on a kill: no cleanup, no atexit, no flush (Windows only, D-26)."""
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel32.TerminateProcess(kernel32.GetCurrentProcess(), code)


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
                 faults: list[Fault] | None = None, heartbeats: bool = True,
                 resume_after: tuple[int, int] | None = None, resume_not_before_ns: int | None = None):
        # A LandingDB is accepted directly for convenience.
        self.source = LandingSource.from_db(source) if isinstance(source, LandingDB) else source
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
        self.on_kill = None                 # () before a `kill` ends the process: drain and sync what was dispatched
        self.terminate = None               # (exit code) ends the process; the daemon sets `terminate_process`
        self.resume_after = resume_after
        self.resume_not_before_ns = resume_not_before_ns
        self.resume_at_ns: int | None = None    # set when a token_expiry with a duration ended this run
        self.on_resume_reached = None       # () once, when the first event at or after `resume_not_before_ns` arrives
        self._passed = 0                    # events that were not skipped as already dispatched or lost
        self._skipped = 0
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
        for frame_no, recv_ns, mono_ns, token, raw in self.source.frames():
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

    def _through(self) -> str:
        t = max([x for x in (self.resume_after[1] if self.resume_after is not None else None,
                             self.resume_not_before_ns) if x is not None] or [0])
        return timeutil.ns_to_ist(t).strftime("%H:%M:%S IST")

    def run(self) -> None:
        self.connected = True
        self._fire(self.on_connect, {})
        mark = self.resume_after[1] if self.resume_after is not None else None
        # A restart is given the same --inject list: a fault the earlier process already went through (its time is at
        # or before the resume point) is not injected again. The kill is always after the mark, so it stays armed.
        floor = max([x for x in (mark, self.resume_not_before_ns) if x is not None], default=None)
        faults = [f for f in self.faults if floor is None or f.at_ns > floor]
        exhausted = False
        down_until = None
        prev = None
        try:
            for t_ns, mono, kind, payload in self._events():
                if self._stop.is_set():
                    break
                if mark is not None and t_ns <= mark:
                    self._skipped += 1
                    continue                              # the old process dispatched it already
                if self.resume_not_before_ns is not None and t_ns < self.resume_not_before_ns:
                    self._skipped += 1
                    continue                              # lost in the outage the restart follows
                if self._passed == 0 and self.resume_not_before_ns is not None and self.on_resume_reached is not None:
                    self.on_resume_reached()              # the restart has used `T + N`: it is never applied again
                self._passed += 1
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
                        if fault.duration_ns:
                            self.resume_at_ns = fault.at_ns + fault.duration_ns
                        return
                    elif fault.kind == "kill":
                        self.killed = True
                        if self.on_kill is not None:
                            self.on_kill()
                        if self.terminate is not None:
                            self.terminate(KILLED_EXIT)
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
            exhausted = True
        finally:
            if exhausted and self._skipped and not self._passed and not self._stop.is_set():
                log.info("replay already complete through %s: nothing to do", self._through())
            if not self.killed:
                self.connected = False
                self._fire(self.on_close, 1000, "replay finished")
            self.finished.set()


def open_replay(spec: str, *, faults: list[str] | None = None, path=None,
                resume_after: tuple[int, int] | None = None, resume_not_before_ns: int | None = None):
    """Build (ticker, clock) for a `replay:<date>:<speed>` spec (archive preferred). `resume_after` is the old
    process's `(frame_no, recv_ns)` mark (frames at or before its stamp are skipped); `resume_not_before_ns` is a
    fault duration's `T + N`."""
    day, speed = parse_spec(spec)
    source = source_for(day, path)
    first = source.first_recv_ns() or timeutil.ist_ns(day, "09:00")
    clock = ReplayClock(max(int(first) - 1_000_000, resume_after[1] if resume_after else 0))
    ticker = ReplayTicker(source, clock, speed=speed, faults=[parse_fault(f, day) for f in faults or []],
                          resume_after=resume_after, resume_not_before_ns=resume_not_before_ns)
    return ticker, clock


def ist_label(t_ns: int) -> str:
    return datetime.strftime(timeutil.ns_to_ist(t_ns), "%H:%M:%S")


__all__ = ["ReplayTicker", "ReplayClock", "Fault", "parse_spec", "parse_fault", "open_replay", "ist_label", "terminate_process",
           "KILLED_EXIT"]

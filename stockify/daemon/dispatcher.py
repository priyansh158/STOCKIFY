"""The paper daemon's dispatcher: one FIFO in, one engine thread (DM 5.7; M5.1; D-72).

Every producer (the recorder's frame tap, the ticker's gap taps, the margin and band pollers) stamps its item with
`clock.now_ns()` and calls `put_nowait`; nothing else crosses the thread boundary. The engine thread takes items
in order and turns them into journal records for each session:

    ("frame", (frame_no, recv_ns, mono_ns, payload, is_binary))   a recorder item: data frame, heartbeat or text
    ("gap", phase, reason, t_ns, instrument_id | None)            a ticker gap tap
    ("inject", t_ns, session_id | None, rank, record)             a band, a margin answer, a control
    ("command", t_ns, row)                                        an owner command from `store.pending_commands`

Time is the stamp, never the wall clock. For each item stamped `s`, `CLOCK(floor(s / every) * every)` precedes the
item's records when that boundary is later than the last one emitted (the same rule for every item kind, so a
silent feed with only heartbeats still ticks). The same stamped stream therefore gives the same journal bytes,
however slowly the engine thread runs. Commands are polled from the store once per `commands_every_s` of stamp
time and queued behind what is already waiting, so they apply in order with the frames around them.

A full queue never blocks a producer: the item is counted as dropped, the alarm `dispatcher_queue_full` is raised
once, and the engine thread journals a scope-all `GAP(stall)` start and end (the first and last dropped stamps)
once the items queued before the loss are done, with a `gap` timeline row `{"cause": "dispatcher_overflow"}` per session. Silence is never an
answer: the loss is in the journal.

A session whose day fails (`SessionDayError` after a push or store error, or any error from its feed) is dropped
from dispatch and listed in `stats.failed`; the others go on. Replay it to recover.
"""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass, field

from stockify import store
from stockify.daemon import lifecycle
from stockify.daemon.clock import Clock
from stockify.daemon.encoder import FrameEncoder
from stockify.daemon.session_day import SessionDay
from stockify.data import packs

log = logging.getLogger("stockify.daemon.dispatcher")

SEC = 1_000_000_000
POLL_S = 0.05
FRAME, GAP, INJECT, COMMAND = "frame", "gap", "inject", "command"
_PHASE = {"start": 0, "end": 1}
_CLOCK_RANK = packs.RANK[packs.CLOCK]
_GAP_RANK = packs.RANK[packs.GAP_K]
_REASON_DEFAULT = "owner"


@dataclass
class DispatcherStats:
    items: int = 0
    frames: int = 0
    records: int = 0
    clocks: int = 0
    gaps: int = 0
    commands: int = 0
    dropped: int = 0
    alarms: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)       # session_id -> first error


class Dispatcher:
    def __init__(self, sessions: list[SessionDay], encoder: FrameEncoder, *, clock: Clock, cfg: dict,
                 replay: bool) -> None:
        self.sessions = list(sessions)
        self.encoder = encoder
        self.clock = clock
        self.replay = replay
        self.clock_every_ns = int(cfg["clock_every_s"]) * SEC
        self.commands_every_ns = int(cfg["commands_every_s"]) * SEC
        self.queue: queue.Queue = queue.Queue(maxsize=int(cfg["dispatcher_queue_max"]))
        self.stats = DispatcherStats()
        self._lock = threading.Lock()
        self._overflow: list[int] | None = None                 # [first stamp, last stamp, dropped] not yet journaled
        self._last_clock = 0
        self._next_poll = 0
        self._queued_cmds: set[int] = set()
        self._stopping: set[str] = set()
        self._by_id = {s.session_id: s for s in self.sessions}

    # ── producers (any thread): cheap, never block ───────────────────────────
    def put_frame(self, item: tuple) -> None:
        """A Recorder tap: `(frame_no, recv_ns, mono_ns, payload, is_binary)`, stamped by the recorder."""
        self._offer((FRAME, item), item[1])

    put_nowait = put_frame          # `Recorder.add_tap` wants an object with put_nowait: the dispatcher is its tap

    def put_gap(self, phase: str, reason: str, t: int, iid: int | None) -> None:
        """A RecorderSession gap tap."""
        self._offer((GAP, phase, reason, t, iid), t)

    def put_inject(self, t: int, session_id: str | None, rank: int, record: bytes) -> None:
        """A record for one session (or all, with None) that is neither a frame nor a gap: a band, a margin
        answer, a control. `t` is its stamp, `clock.now_ns()` at the producer."""
        self._offer((INJECT, t, session_id, rank, record), t)

    def _offer(self, item: tuple, stamp: int) -> None:
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            self._dropped(stamp)

    def _dropped(self, stamp: int) -> None:
        with self._lock:
            self.stats.dropped += 1
            if "dispatcher_queue_full" not in self.stats.alarms:
                self.stats.alarms.append("dispatcher_queue_full")
                log.error("dispatcher queue full: item dropped; the loss is journaled as a stall gap")
            if self._overflow is None:
                self._overflow = [stamp, stamp, 1]
            else:
                self._overflow[0] = min(self._overflow[0], stamp)
                self._overflow[1] = max(self._overflow[1], stamp)
                self._overflow[2] += 1

    # ── engine thread ────────────────────────────────────────────────────────
    def run(self, stop: threading.Event) -> None:
        """The engine thread: take items in order until `stop`, then drain what is queued."""
        while not stop.is_set():
            try:
                item = self.queue.get(timeout=POLL_S)
            except queue.Empty:
                self._flush_overflow()
                continue
            self._process(item)
        self.drain()

    def drain(self) -> None:
        """Process everything queued (and anything those items queue, such as commands), then any overflow gap."""
        while True:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                break
            self._process(item)
        self._flush_overflow()
        if not self.queue.empty():
            self.drain()

    def _process(self, item: tuple) -> None:
        self.stats.items += 1
        kind = item[0]
        stamp = {FRAME: lambda: item[1][1], GAP: lambda: item[3], INJECT: lambda: item[1],
                 COMMAND: lambda: item[1]}[kind]()
        lost = self._overflow
        if lost is not None and stamp >= lost[0]:
            self._flush_overflow()           # items queued before the loss (earlier stamps) go first
        if kind != COMMAND:
            self._poll_commands(stamp)
        if kind == FRAME:
            self._frame(item[1])
        elif kind == GAP:
            self._gap(item[1], item[2], item[3], item[4])
        elif kind == INJECT:
            self._inject(item[1], item[2], item[3], item[4])
        else:
            self._clock_and_deliver(stamp, {}, None)
            self._command(item[1], item[2])

    # ── delivery ─────────────────────────────────────────────────────────────
    def _clock_record(self, stamp: int) -> tuple[int, int, bytes] | None:
        boundary = stamp // self.clock_every_ns * self.clock_every_ns
        if boundary <= self._last_clock:
            return None
        self._last_clock = boundary
        self.stats.clocks += 1
        return boundary, _CLOCK_RANK, packs.enc_clock(boundary)

    def _clock_and_deliver(self, stamp: int, per: dict[str, list], last_frame: tuple[int, int] | None) -> None:
        """CLOCK first (when a boundary passed), then each session's records, as one feed per session."""
        clock = self._clock_record(stamp)
        for sd in self.sessions:
            if sd.session_id in self.stats.failed:
                continue
            records = ([clock] if clock else []) + per.get(sd.session_id, [])
            if not records and last_frame is None:
                continue
            self.stats.records += len(records)
            try:
                sd.feed(records, last_frame)
            except Exception as e:  # noqa: BLE001
                self._fail(sd.session_id, e)

    def _fail(self, sid: str, e: BaseException) -> None:
        self.stats.failed.setdefault(sid, repr(e))
        log.error("session %s failed (its day stays failed; replay it to recover): %r", sid, e)
        alarm = f"session_failed:{sid}"
        if alarm not in self.stats.alarms:
            self.stats.alarms.append(alarm)

    def _frame(self, item: tuple) -> None:
        frame_no, recv_ns, _mono, payload, is_binary = item
        per: dict[str, list] = {}
        if is_binary:
            for iid, record in self.encoder.encode(payload, recv_ns):
                rank = packs.RANK[packs.PREFIX.unpack_from(record)[0]]
                for sd in self.sessions:
                    if iid in sd.ids:
                        per.setdefault(sd.session_id, []).append((recv_ns, rank, record))
        self.stats.frames += 1
        self._clock_and_deliver(recv_ns, per, (frame_no, recv_ns) if frame_no else None)

    def _gap_record(self, phase: str, reason: str, t: int, iid: int | None) -> tuple[int, int, bytes]:
        return t, _GAP_RANK, packs.enc_gap(iid or 0, t, _PHASE[phase], reason)

    def _targets(self, iid: int | None) -> list[SessionDay]:
        return [sd for sd in self.sessions if iid is None or iid in sd.ids]

    def _gap(self, phase: str, reason: str, t: int, iid: int | None) -> None:
        targets = self._targets(iid)
        if reason == "stall" and self.replay:
            # A stall in a replayed day is the recording's own silence, not a loss of ours: a timeline row only.
            for sd in targets:
                self._timeline(sd.session_id, "stall_replay_only",
                               {"phase": phase, "reason": reason, "t_ns": t, "instrument_id": iid}, t)
            self._clock_and_deliver(t, {}, None)
            return
        if reason not in packs.GAP_REASONS:
            log.warning("gap reason %r has no journal record; not journaled", reason)
            self._clock_and_deliver(t, {}, None)
            return
        record = self._gap_record(phase, reason, t, iid)
        self.stats.gaps += 1
        self._clock_and_deliver(t, {sd.session_id: [record] for sd in targets}, None)

    def _inject(self, t: int, session_id: str | None, rank: int, record: bytes) -> None:
        targets = self.sessions if session_id is None else [s for s in self.sessions if s.session_id == session_id]
        self._clock_and_deliver(t, {sd.session_id: [(t, rank, record)] for sd in targets}, None)

    def _timeline(self, sid: str, kind: str, detail: dict, t: int) -> None:
        try:
            store.add_timeline(sid, kind, detail, wall_ns=t, engine_ns=t)
        except KeyError:
            log.warning("no session %s for the timeline row %s", sid, kind)

    # ── overflow ─────────────────────────────────────────────────────────────
    def _flush_overflow(self) -> None:
        """Journal what the queue refused: a scope-all stall gap from the first to the last dropped stamp."""
        if self._overflow is None:
            return
        with self._lock:
            lost, self._overflow = self._overflow, None
        if lost is None:
            return
        first, last, count = lost
        for phase, t in (("start", first), ("end", last)):
            record = self._gap_record(phase, "stall", t, None)
            self.stats.gaps += 1
            self._clock_and_deliver(t, {sd.session_id: [record] for sd in self.sessions}, None)
            for sd in self.sessions:
                self._timeline(sd.session_id, "gap", {"cause": "dispatcher_overflow", "phase": phase,
                                                      "dropped": count, "from_ns": first, "to_ns": last}, t)

    # ── commands ─────────────────────────────────────────────────────────────
    def _poll_commands(self, stamp: int) -> None:
        """Once per `commands_every_s` of stamp time: queue the store's pending commands behind the queued items."""
        if stamp < self._next_poll:
            return
        self._next_poll = stamp // self.commands_every_ns * self.commands_every_ns + self.commands_every_ns
        for row in store.pending_commands():
            if row["cmd_id"] in self._queued_cmds:
                continue
            try:
                self.queue.put_nowait((COMMAND, stamp, row))
            except queue.Full:
                self._dropped(stamp)
                break                                     # kept in the store: polled again next time
            self._queued_cmds.add(row["cmd_id"])

    def _command(self, t: int, row: dict) -> None:
        self.stats.commands += 1
        try:
            result = self._apply(t, row)
        except Exception as e:  # noqa: BLE001
            log.exception("command %s (%s) failed", row["cmd_id"], row["command"])
            result = f"error: {e!r}"
        store.mark_command(row["cmd_id"], t, result)
        self._queued_cmds.discard(row["cmd_id"])

    def _apply(self, t: int, row: dict) -> str:
        name, args = row["command"], row["args"] or {}
        if name not in ("pause", "resume", "stop", "stop_all"):
            return f"error: unknown command {name!r}"
        if row["session_id"] is None:
            targets = list(self.sessions)
        else:
            sd = self._by_id.get(row["session_id"])
            if sd is None:
                return f"error: no such session {row['session_id']!r} in this daemon"
            targets = [sd]
        notes = []
        for sd in targets:
            try:
                self._apply_one(sd, name, args, row["cmd_id"], t)
            except Exception as e:  # noqa: BLE001 — one session's refusal never blocks the others
                notes.append(f"{sd.session_id}: {e}")
        return "ok" if not notes else "error: " + "; ".join(notes)

    def _apply_one(self, sd: SessionDay, name: str, args: dict, cmd_id: int, t: int) -> None:
        sid = sd.session_id
        if sid in self.stats.failed:
            raise RuntimeError("its day failed; replay it first")
        if sid in self._stopping:
            if name == "stop":
                return
            raise RuntimeError("it is stopping")
        reason = args.get("reason", _REASON_DEFAULT)
        if name == "pause":
            reasons = lifecycle.pause(sid, reason, wall_ns=t, engine_ns=t)
            self._control(sd, reasons, t)
        elif name == "stop_all":
            reasons = lifecycle.pause(sid, "stop_all", wall_ns=t, engine_ns=t)
            self._control(sd, reasons, t)
        elif name == "resume":
            reasons = lifecycle.resume(sid, reason, wall_ns=t, engine_ns=t)
            self._control(sd, reasons, t)
        else:
            # `stop`: the strategy goes off for good now; the day is finalised and the status STOPPED (lifecycle.stop)
            # by the day's finalisation, which is not the dispatcher's to do (RECON, per-session stop rule).
            held = (store.get_session(sid) or {}).get("pause_reasons", [])
            self._stopping.add(sid)
            self._control(sd, sorted({*held, "stopped"}), t)
            self._timeline(sid, "stop_requested", {"cmd_id": cmd_id}, t)

    def _control(self, sd: SessionDay, reasons: list[str], t: int) -> None:
        try:
            sd.control(not reasons, reasons, t)
        except Exception as e:  # noqa: BLE001
            self._fail(sd.session_id, e)
            raise

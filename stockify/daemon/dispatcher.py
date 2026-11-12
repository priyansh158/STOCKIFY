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

Staged records are fed in micro-batches: up to `dispatch.batch_max_items` items or `dispatch.batch_max_ms` (the
clock's monotonic time, which only groups and never enters a record), at every command, and whenever the queue runs
dry; one `feed` per session per batch, journal then engine then store. `append` stamps each record on its own, so the
journal bytes are those of feeding item by item. A frame that leaves a session no records makes no store commit for it:
its frame mark is held and written with the session's next feed or when the queue runs dry.

Commands are polled on stamp time and, whenever the queue is empty, on the dispatcher's clock (a silent feed must
not strand an owner's stop); the clock reading is the command's stamp, like any producer's. Pause, resume, stop_all
and stop write the journal record first and the store second (D-68 outranks the order of the first brief); a crash
between the two leaves the command unmarked, and reapplying it after the restart repeats the record and finishes
the store write. `stop` ends with `lifecycle.stop`: the store says STOPPED from the command on.

After each feed, when `on_book` is set, the engine's open positions are read (the engine thread is idle then) and
`on_book(session_day, instrument_ids)` is called when they changed: the daemon keeps them pinned until flat (RM 5.4).
A tick for a position's instrument that the session's bundle lacks is not journaled: it writes the timeline row
`unbundled_instrument` once per instrument and day, and labels the day `unbundled_instrument`.

A live restart (`start`) first journals the gaps the restart itself made: the end of a GAP(process_stop) at the first
new frame, and, for a process that died inside market hours, its start at the journal's last record; then it applies
the pending commands before anything live is pushed (GK 5.4).

A session whose day fails (`SessionDayError` after a push or store error, or any error from its feed) is dropped
from dispatch and listed in `stats.failed`; the others go on. Replay it to recover.
"""
from __future__ import annotations

import json
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
    commands_delayed: int = 0           # a command that did not fit the queue: delayed, never lost
    clamped: int = 0                    # items stamped inside an overflow stall, journaled after its END (t lifted)
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
        self.batch_max_items = int(cfg["dispatch"]["batch_max_items"])
        self.batch_max_ns = int(cfg["dispatch"]["batch_max_ms"]) * 1_000_000
        self.queue: queue.Queue = queue.Queue(maxsize=int(cfg["dispatcher_queue_max"]))
        self.stats = DispatcherStats()
        self._lock = threading.Lock()
        self._overflow: list | None = None          # [first stamp, last stamp, dropped, [(reason, iid) of dropped gap ENDs]]
        self._clamp_until = 0
        self._open_gaps: set[tuple[str, int | None]] = set()
        self._pending: dict[str, list] = {}
        self._marks: dict[str, tuple[int, int]] = {}
        self._batch_n = 0
        self._batch_t0 = 0
        self._stamp = 0
        self._next_poll = 0
        self._queued_cmds: set[int] = set()
        self._by_id = {s.session_id: s for s in self.sessions}
        self.on_book = None                         # (SessionDay, frozenset[int]) on the engine thread, when a book changes
        self.on_failed = None                       # (session_id) once, when a session's day fails
        # The margin poller (IWB 5.1), set by the daemon for a live run; None in a replay. The engine thread alone reads
        # the book for it (`margin_now`) and hands it plain snapshots; the poller holds no engine.
        self.margin_poller = None
        self._snap_next: dict[str, int] = {}         # session -> stamp from which the next periodic snapshot is due
        self._no_margin: set[str] = set()            # sessions whose margin model has no `margin_now` (never polled)
        self._book: dict[str, frozenset[int]] = {}
        self._extras: dict[str, frozenset[int]] = {}     # open positions the bundle does not carry
        self._unbundled: set[tuple[str, int]] = set()
        self._resume_end: dict[str, str] = {}        # session -> the reason of its open gap, which ends at the next frame
        # A restart, live or replay, repeats no CLOCK record: a boundary at or below the journal's last CLOCK is not
        # written again. CLOCK is a periodic marker, never data, so a live restart that shares the last boundary loses
        # nothing by this, and the journal's last CLOCK stays the restart's boundary (LCB 5.2, Task 18 review).
        self._last_clock = max([sd.last_clock for sd in sessions if sd.resumed] or [0])
        self._skip: dict[str, int] = {}              # session -> recv_ns of its last dispatched frame (replay restarts)
        if replay:
            for sd in sessions:
                mark = store.session_last_frame(sd.session_id, sd.trade_date) if sd.resumed else None
                if mark is not None:
                    self._skip[sd.session_id] = mark[1]

    # ── producers (any thread): cheap, never block ───────────────────────────
    def put_frame(self, item: tuple) -> None:
        """A Recorder tap: `(frame_no, recv_ns, mono_ns, payload, is_binary)`, stamped by the recorder."""
        self._offer((FRAME, item), item[1])

    put_nowait = put_frame          # `Recorder.add_tap` wants an object with put_nowait: the dispatcher is its tap

    def put_gap(self, phase: str, reason: str, t: int, iid: int | None) -> None:
        """A RecorderSession gap tap."""
        self._offer((GAP, phase, reason, t, iid), t)

    def put_inject(self, t: int, session_id: str | None, rank: int, record: bytes) -> bool:
        """A record for one session (or all, with None) that is neither a frame nor a gap: a band, a margin
        answer, a control. `t` is its stamp, `clock.now_ns()` at the producer. True when it was queued, False when
        the queue was full and it was dropped (the loss is journaled as a stall gap): a producer that logs the record
        as applied must check."""
        return self._offer((INJECT, t, session_id, rank, record), t)

    def _offer(self, item: tuple, stamp: int) -> bool:
        try:
            self.queue.put_nowait(item)
            return True
        except queue.Full:
            self._dropped(stamp, item)
            return False

    def _dropped(self, stamp: int, item: tuple | None = None) -> None:
        with self._lock:
            self.stats.dropped += 1
            if "dispatcher_queue_full" not in self.stats.alarms:
                self.stats.alarms.append("dispatcher_queue_full")
                log.error("dispatcher queue full: item dropped; the loss is journaled as a stall gap")
            if self._overflow is None:
                self._overflow = [stamp, stamp, 1, []]
            else:
                self._overflow[0] = min(self._overflow[0], stamp)
                self._overflow[1] = max(self._overflow[1], stamp)
                self._overflow[2] += 1
            if item is not None and item[0] == GAP and item[1] == "end":
                self._overflow[3].append((item[2], item[4]))

    # ── engine thread ────────────────────────────────────────────────────────
    def run(self, stop: threading.Event) -> None:
        """The engine thread: take items in order until `stop`, then drain what is queued."""
        while not stop.is_set():
            try:
                item = self.queue.get(timeout=POLL_S)
            except queue.Empty:
                self._flush_overflow()
                self._flush(force_marks=True)
                self._poll_commands(self.clock.now_ns())
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
        self._flush(force_marks=True)
        if not self.queue.empty():
            self.drain()

    def start(self, t: int) -> None:
        """Before the first live item, on the calling thread, with the engine thread not yet started (GK 5.4): a live
        restart's gaps are journaled (see `_resume_gaps`), then every pending owner command is applied, its
        `strategy_control` journaled first, so nothing live reaches an engine that an owner's stop has not reached."""
        self._stamp = t
        if not self.replay:
            self._resume_gaps(t)
        else:
            self._resume_outages()
        self.apply_pending(t)

    def apply_pending(self, t: int) -> None:
        """Apply the store's pending commands now, in order, as if they had been polled at stamp `t`."""
        self._stamp = t
        for row in store.pending_commands():
            if row["cmd_id"] not in self._queued_cmds:
                self._command(t, row)
        self._flush(force_marks=True)

    def _resume_gaps(self, t: int) -> None:
        """A live restart did not see the frames between the old process's last record and the first new frame. A
        session whose journal ends in a GAP(process_stop) start (a graceful stop inside market hours) only needs its
        end, at the first new frame. One whose journal has no such start (the process died) gets one now, at the
        journal's last `t`, provided it is inside market hours; the end follows at the first frame either way. One that
        ends inside an open token_expired or disconnect gap continues that gap instead (no process_stop inside it)."""
        from stockify.daemon.market_day import inside_market_hours
        for sd in self.sessions:
            sid = sd.session_id
            if not sd.resumed or sid in self.stats.failed:
                continue
            if sd.open_outage:
                # The journal ends inside a scope-all token_expired (or disconnect) gap: it already says the frames
                # are missing, so nothing nests inside it. The gap goes on and ends at the first new frame, as a
                # replay's does (`_resume_outages`; D-68: only the journal-first records, never a second gap).
                self._resume_end[sid] = sd.open_outage
            elif sd.open_process_stop:
                self._resume_end[sid] = "process_stop"
            elif sd.last_t is not None and inside_market_hours(t):
                try:
                    sd.feed([self._gap_record("start", "process_stop", sd.last_t, None)], None)
                except Exception as e:  # noqa: BLE001
                    self._fail(sid, e, t)
                    continue
                self.stats.gaps += 1
                self._open_gaps.add(("process_stop", None))
                self._timeline(sid, "gap", {"cause": "process_crash", "phase": "start", "t_ns": sd.last_t}, t)
                self._resume_end[sid] = "process_stop"

    def _resume_outages(self) -> None:
        """A replay restarted after a fault duration (`token_expiry@T+Ns`): the journal ends inside a disconnect or
        token_expired gap. The frames between T and T+N are lost, so the gap ends at the first new frame, and no
        process_stop gap is journaled: a replay's restart is not a process that missed live frames (GK 5.3)."""
        for sd in self.sessions:
            if sd.resumed and sd.open_outage and sd.session_id not in self.stats.failed:
                self._resume_end[sd.session_id] = sd.open_outage

    def journal_process_stop(self, t: int) -> None:
        """Journal a scope-all `GAP(process_stop)` start at `t` for every session, on the calling thread, never
        through the queue (GK 5.2): a full queue would drop it into a `stall` overflow gap, and a process stop must
        stay a process stop. Only for the shutdown, after the engine thread has stopped; call `drain()` first."""
        self._stamp = t
        self._gap("start", "process_stop", t, None)
        self._flush(force_marks=True)

    def _process(self, item: tuple) -> None:
        self.stats.items += 1
        kind = item[0]
        stamp = {FRAME: lambda: item[1][1], GAP: lambda: item[3], INJECT: lambda: item[1],
                 COMMAND: lambda: item[1]}[kind]()
        self._stamp = stamp
        lost = self._overflow
        if lost is not None and stamp >= lost[0]:
            self._flush_overflow()           # items queued before the loss (earlier stamps) go first
        if stamp < self._clamp_until:
            self.stats.clamped += 1          # accepted inside a stall whose END is already journaled
        if kind != COMMAND:
            self._poll_commands(stamp)
        if kind == FRAME:
            self._frame(item[1])
        elif kind == GAP:
            self._gap(item[1], item[2], item[3], item[4])
        elif kind == INJECT:
            self._inject(item[1], item[2], item[3], item[4])
        else:
            self._stage(stamp, {}, None)
            self._flush()                    # records before the command's own journal records
            self._command(item[1], item[2])

    # ── delivery ─────────────────────────────────────────────────────────────
    def _clock_record(self, stamp: int) -> tuple[int, int, bytes] | None:
        boundary = stamp // self.clock_every_ns * self.clock_every_ns
        if boundary <= self._last_clock:
            return None
        self._last_clock = boundary
        self.stats.clocks += 1
        return boundary, _CLOCK_RANK, packs.enc_clock(boundary)

    def _stage(self, stamp: int, per: dict[str, list], last_frame: tuple[int, int] | None) -> None:
        """CLOCK first (when a boundary passed), then each session's records, staged for the next batch."""
        clock = self._clock_record(stamp)
        for sd in self.sessions:
            sid = sd.session_id
            if sid in self.stats.failed:
                continue
            recs = per.get(sid)
            if clock or recs:
                staged = self._pending.setdefault(sid, [])
                if clock:
                    staged.append(clock)
                if recs:
                    staged.extend(recs)
            if last_frame is not None:
                self._marks[sid] = last_frame
        if self._batch_n == 0:
            self._batch_t0 = self.clock.mono_ns()
        self._batch_n += 1
        if self._batch_n >= self.batch_max_items or self.clock.mono_ns() - self._batch_t0 >= self.batch_max_ns:
            self._flush()

    def _flush(self, force_marks: bool = False) -> None:
        """One feed per session with staged records (and its latest frame mark). A session with no records keeps
        its mark for later unless `force_marks` (the queue ran dry): no store commit for nothing."""
        self._batch_n = 0
        for sd in self.sessions:
            sid = sd.session_id
            recs = self._pending.pop(sid, None)
            if sid in self.stats.failed:
                self._marks.pop(sid, None)
                continue
            if recs:
                mark = self._marks.pop(sid, None)
            elif force_marks and sid in self._marks:
                recs, mark = [], self._marks.pop(sid)
            else:
                continue
            self.stats.records += len(recs)
            try:
                sd.feed(recs, mark)
            except Exception as e:  # noqa: BLE001
                self._fail(sid, e, self._stamp)
                continue
            if self.on_book is not None and recs:
                self._watch_book(sd)
            if self.margin_poller is not None and not self.replay and recs:
                self._snapshot(sd)

    def _snapshot(self, sd: SessionDay) -> None:
        """After a batch (engine thread, the engine idle): a fill pokes the poller and puts a snapshot of the book on
        its queue (reason "poke"); otherwise, every `every_s` of stamp time, one more ("periodic") while the book has
        legs. Reading the book is the only cost, and only when one of those is due. Never fails the day."""
        sid, poller = sd.session_id, self.margin_poller
        if sid in self._no_margin:
            return
        filled = sd.last_fills > 0
        if not filled and self._stamp < self._snap_next.get(sid, 0):
            return
        try:
            now = sd.engine.margin_now()
        except Exception as e:  # noqa: BLE001 — another margin model has none: this session is never polled
            self._no_margin.add(sid)
            log.info("session %s: no margin snapshots (%s)", sid, e)
            return
        try:
            legs = [(int(i), int(u)) for i, u in now["legs"]]
            self._snap_next[sid] = self._stamp + poller.every_ns
            if not filled and not legs:
                return
            from stockify.daemon.margin_poller import MarginSnapshot, put_latest
            put_latest(poller.snapshots, MarginSnapshot(
                sid, self._stamp, legs, now["portfolio_sha256"], [dict(r) for r in now["fallback"]],
                "poke" if filled else "periodic"))
            if filled:
                poller.poke(sid)                       # after the snapshot it is about: never a poke without one
        except Exception:  # noqa: BLE001 — the poller is a courtesy; the day's feed already succeeded
            log.warning("session %s: handing the book to the margin poller failed", sid, exc_info=True)

    def _watch_book(self, sd: SessionDay) -> None:
        """Read the book between pushes (engine thread); report it when it changed. Never fails the day."""
        sid = sd.session_id
        try:
            positions = frozenset(int(p[0]) for p in sd.engine.book())
            if positions == self._book.get(sid, frozenset()):
                return
            self._book[sid] = positions
            self._extras[sid] = positions - sd.ids
            self.on_book(sd, positions)
        except Exception:  # noqa: BLE001 — pins are a courtesy to the recording; the feed already succeeded
            log.warning("session %s: reading or pinning its book failed", sid, exc_info=True)

    def _note_unbundled(self, sd: SessionDay, iid: int, t: int) -> None:
        key = (sd.session_id, iid)
        if key in self._unbundled:
            return
        self._unbundled.add(key)
        log.warning("session %s: a tick for instrument %d, which its bundle does not carry; not journaled",
                    sd.session_id, iid)
        self._timeline(sd.session_id, "unbundled_instrument", {"instrument_id": iid, "day": sd.trade_date}, t)
        try:
            row = store.get_session_day(sd.session_id, sd.trade_date) or {}
            labels = sorted(set(json.loads(row.get("labels_json") or "[]")) | {"unbundled_instrument"})
            store.upsert_session_day(sd.session_id, sd.trade_date, labels_json=json.dumps(labels))
        except Exception:  # noqa: BLE001
            log.warning("no label for session %s", sd.session_id, exc_info=True)

    def _fail(self, sid: str, e: BaseException, t: int) -> None:
        first = sid not in self.stats.failed
        self.stats.failed.setdefault(sid, repr(e))
        log.error("session %s failed (its day stays failed; replay it to recover): %r", sid, e)
        alarm = f"session_failed:{sid}"
        if alarm not in self.stats.alarms:
            self.stats.alarms.append(alarm)
        if first:
            self._timeline(sid, "session_failed", {"error": repr(e)}, t)
            from stockify.daemon import notify              # only enqueues: this runs on the engine's thread
            notify.session_event("session_error", f"Session {sid} failed for the day",
                                 {"session": sid, "error_type": type(e).__name__,
                                  "what_to_do": "replay the day from its journal to recover; the log has the detail"},
                                 key=f"session_failed:{sid}:{t}")
            if self.on_failed is not None:
                try:
                    self.on_failed(sid)                # its pins are released: a failed day needs no market data
                    self._timeline(sid, "pins_released", {"reason": "session_failed"}, t)
                except Exception:  # noqa: BLE001
                    log.warning("session %s: releasing its pins failed", sid, exc_info=True)

    def _frame(self, item: tuple) -> None:
        frame_no, recv_ns, _mono, payload, is_binary = item
        per: dict[str, list] = {}
        if is_binary:
            for iid, record in self.encoder.encode(payload, recv_ns):
                rank = packs.RANK[packs.PREFIX.unpack_from(record)[0]]
                for sd in self.sessions:
                    if recv_ns <= self._skip.get(sd.session_id, -1):
                        continue                     # at or below this session's own mark: it has the frame already
                    if iid in sd.ids:
                        per.setdefault(sd.session_id, []).append((recv_ns, rank, record))
                    elif iid in self._extras.get(sd.session_id, ()):
                        self._note_unbundled(sd, iid, recv_ns)
        logins: list[str] = []
        for sid, reason in sorted(self._resume_end.items()):    # the first frame since the restart: the open gap is over
            end = self._gap_record("end", reason, recv_ns, None)
            if reason == "token_expired":
                self._stage(recv_ns, {sid: [end]}, None)        # alone, so the login's control precedes the frame
                logins.append(sid)
            else:
                per.setdefault(sid, []).insert(0, end)
            self.stats.gaps += 1
            self._open_gaps.discard((reason, None))
            self._timeline(sid, "gap", {"cause": f"{reason}_resumed", "phase": "end", "t_ns": recv_ns}, recv_ns)
        self._resume_end.clear()
        for sid in logins:
            self._login(self._by_id[sid], recv_ns)
        self.stats.frames += 1
        self._stage(recv_ns, per, (frame_no, recv_ns) if frame_no else None)

    def pause_for_token_expiry(self, t: int) -> list[str]:
        """Kite rejected the token (LCB 5.2): every session gets the `token_expired` pause reason, journal first
        (`strategy_control`, D-68), then the store (`lifecycle.pause`, one transaction). On the calling thread, for the
        shutdown, after the engine thread has stopped and `drain()` has journaled the gap start. Returns the ids of
        the sessions that now hold the reason; a session whose day failed or that is STOPPED is left alone."""
        self._stamp = t
        paused = []
        for sd in self.sessions:
            sid = sd.session_id
            if sid in self.stats.failed:
                continue
            try:
                session = store.get_session(sid)
                if session is None or session["status"] == "STOPPED":
                    continue
                held = set(session["pause_reasons"])
                if "token_expired" not in held:
                    self._control(sd, sorted(held | {"token_expired"}), t)
                    lifecycle.pause(sid, "token_expired", wall_ns=t, engine_ns=t)
                paused.append(sid)
            except Exception:  # noqa: BLE001 — one session's trouble never stops the others
                log.exception("session %s: pausing it for the token expiry failed", sid)
        self._flush(force_marks=True)
        return paused

    def _login(self, sd: SessionDay, t: int) -> None:
        """The first frame after a login: the `token_expired` reason goes, journal first, then the store; the owner's
        own pause and every other reason stay. The timeline gets a `login` row."""
        sid = sd.session_id
        if sid in self.stats.failed:
            return
        try:
            session = store.get_session(sid)
            held = set(session["pause_reasons"]) if session is not None else set()
            if "token_expired" not in held or session["status"] == "STOPPED":
                return
            reasons = sorted(held - {"token_expired"})
            self._control(sd, reasons, t)
            stored = lifecycle.resume(sid, "token_expired", wall_ns=t, engine_ns=t)
            self._timeline(sid, "login", {"t_ns": t, "reasons": stored}, t)
        except Exception:  # noqa: BLE001
            log.exception("session %s: resuming it after the login failed", sid)

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
            self._stage(t, {}, None)
            return
        if reason not in packs.GAP_REASONS:
            log.warning("gap reason %r has no journal record; not journaled", reason)
            self._stage(t, {}, None)
            return
        record = self._gap_record(phase, reason, t, iid)
        self.stats.gaps += 1
        (self._open_gaps.add if phase == "start" else self._open_gaps.discard)((reason, iid))
        self._stage(t, {sd.session_id: [record] for sd in targets}, None)

    def _inject(self, t: int, session_id: str | None, rank: int, record: bytes) -> None:
        targets = self.sessions if session_id is None else [s for s in self.sessions if s.session_id == session_id]
        self._stage(t, {sd.session_id: [(t, rank, record)] for sd in targets}, None)

    def _timeline(self, sid: str, kind: str, detail: dict, t: int) -> None:
        try:
            store.add_timeline(sid, kind, detail, wall_ns=t, engine_ns=t)
        except Exception:  # noqa: BLE001 — a timeline row is a courtesy; never cost the queue
            log.warning("no timeline row %s for session %s", kind, sid, exc_info=True)

    # ── overflow ─────────────────────────────────────────────────────────────
    def _flush_overflow(self) -> None:
        """Journal what the queue refused: a scope-all stall gap from the first to the last dropped stamp."""
        if self._overflow is None:
            return
        with self._lock:
            lost, self._overflow = self._overflow, None
        if lost is None:
            return
        first, last, count, ends = lost
        self._clamp_until = max(self._clamp_until, last)
        for phase, t in (("start", first), ("end", last)):
            record = self._gap_record(phase, "stall", t, None)
            self.stats.gaps += 1
            self._stage(t, {sd.session_id: [record] for sd in self.sessions}, None)
            for sd in self.sessions:
                self._timeline(sd.session_id, "gap", {"cause": "dispatcher_overflow", "phase": phase,
                                                      "dropped": count, "from_ns": first, "to_ns": last}, t)
        for reason, iid in ends:             # a gap END the queue refused must not leave its gap open
            if (reason, iid) in self._open_gaps or (self.replay and reason == "stall"):
                self._gap("end", reason, last, iid)

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
                self.stats.commands_delayed += 1          # delayed, not lost: it stays in the store
                self._next_poll = 0                       # and is offered again at the next chance
                break
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
        """Journal first, store second (D-68). The reason set is worked out from the store's current one with the
        rules `lifecycle` applies, journaled, and then `lifecycle` makes its atomic change."""
        sid = sd.session_id
        if sid in self.stats.failed:
            raise RuntimeError("its day failed; replay it first")
        session = store.get_session(sid)
        if session is None:
            raise RuntimeError("no such session in the store")
        held = set(session["pause_reasons"])
        if session["status"] == "STOPPED":
            if name == "stop":
                return                                    # already stopped: nothing to journal or change
            raise RuntimeError("it is STOPPED")
        reason = args.get("reason", _REASON_DEFAULT)
        if name == "pause":
            lifecycle._check_reason(reason)
            reasons = sorted(held | {reason})
        elif name == "stop_all":
            reasons = sorted(held | {"stop_all"})
        elif name == "resume":
            lifecycle._check_reason(reason)
            reasons = sorted(held - ({reason, "stop_all"} if reason == "owner" else {reason}))
        else:
            reasons = sorted(held | {"stopped"})
        self._control(sd, reasons, t)                     # the journal record exists before the store changes
        if name in ("pause", "stop_all"):
            stored = lifecycle.pause(sid, "stop_all" if name == "stop_all" else reason, wall_ns=t, engine_ns=t)
        elif name == "resume":
            stored = lifecycle.resume(sid, reason, wall_ns=t, engine_ns=t)
        else:
            lifecycle.stop(sid, wall_ns=t)
            self._timeline(sid, "stop_requested", {"cmd_id": cmd_id}, t)
            return
        if stored != reasons:
            log.warning("session %s: journaled reasons %s, stored %s (another writer changed them)", sid, reasons, stored)

    def _control(self, sd: SessionDay, reasons: list[str], t: int) -> None:
        self._flush()
        try:
            sd.control(not reasons, reasons, t)
        except Exception as e:  # noqa: BLE001
            self._fail(sd.session_id, e, t)
            raise

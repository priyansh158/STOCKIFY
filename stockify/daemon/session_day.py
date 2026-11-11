"""One paper session-day: journal first, then the engine, then the store (DM 5.4, 5.5; M5.3; D-68).

    day = SessionDay("orb", date(2026, 11, 17), cfg, mono=time.monotonic_ns)
    day.open(sod_state, reasons, t0)           # bundle on disk, engine built, journal opened
    day.feed(records, last_frame)              # append -> push -> drain -> store

The order is the point. A batch of `(t, rank, record)` is appended to the session journal (which numbers it, makes
its `t` monotonic and flushes it) and only then pushed to the engine, whose outputs are then stored in one
transaction together with the frame mark. A crash therefore leaves a journal that is at least as far along as the
engine and the store, and `replay` rebuilds both from it exactly.

Any failure inside `feed` or `control` fails the day: the engine's outputs may already have been drained, or the
engine may be in an unknown state, so every later call raises `SessionDayError` naming the first error. Nothing is
lost, because the journal already holds the input; reopen the day with `replay`.

`session_engine` is the only place a session builds an engine.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Callable

from stockify import bundle as bundle_mod, engine, paths, store
from stockify.config import loader
from stockify.daemon import journal
from stockify.daemon.engine_log import EngineLog
from stockify.data import packs

log = logging.getLogger("stockify.daemon.session_day")

CONTROL_RANK = packs.RANK[packs.REFDATA_K]


class SessionDayError(RuntimeError):
    """A session-day that cannot be opened, replayed or fed."""


def session_engine(bundle_path: Path, cfg: dict):  # noqa: ANN201
    """The engine for one day of a session: built from the day bundle (which applies its `sod_state`, all of it
    or nothing) with the session's strategy set. Sessions build engines nowhere else."""
    mod = engine.load()
    e = mod.Engine(str(bundle_path))
    chosen = engine.strategy_for({"strategy": cfg["strategy"]})
    if isinstance(chosen, str):
        e.set_strategy_cpp(chosen)
    else:
        e.set_strategy_py(chosen)
    return e


class SessionDay:
    def __init__(self, session_id: str, day: date, cfg: dict, *, mono: Callable[[], int],
                 fsync_every_s: int | None = None, log_sink: Callable[[int, str], None] | None = None) -> None:
        self.session_id = session_id
        self.day = day
        self.cfg = cfg
        self._mono = mono
        # paper.json journal.fsync_every_s (bounded by the loader) unless the caller says otherwise.
        self._fsync_ns = int(fsync_every_s if fsync_every_s is not None
                             else loader.load_json("paper.json")["journal"]["fsync_every_s"]) * 1_000_000_000
        self.engine = None
        self.ids: frozenset[int] = frozenset()
        self.bundle_sha256: str | None = None
        self.carried_orders: list[int] = []     # orders from a prior day that got events or fills today (sorted, unique)
        self._journal: journal.JournalWriter | None = None
        self._next_event_no = 1
        self._failed: BaseException | None = None
        self._replay_only = False
        caps = loader.load_json("paper.json")["engine_log"]        # the engine's log lines, throttled and capped
        self._elog = EngineLog(session_id, max_lines_per_min=caps["max_lines_per_min"],
                               max_line_chars=caps["max_line_chars"], sink=log_sink)

    @property
    def trade_date(self) -> str:
        return self.day.isoformat()

    @property
    def journal_records(self) -> int:
        return self._journal.records if self._journal is not None else 0

    def open(self, sod_state: dict | None, reasons: list[str], t0: int) -> None:
        """Write the day's bundle, build the engine, open the journal. If the state's `strategy_enabled` /
        `pause_reasons` differ from `reasons` (the session's set now), the journal's first record is a
        `strategy_control(enabled = not reasons, reasons)` at `t0`. A journal that already holds records is a
        restart: that is `replay`, so this refuses it."""
        if self._journal is not None or self.engine is not None:
            raise SessionDayError("the day is already open")
        reasons = sorted(set(reasons))
        jpath = paths.journal_file(self.session_id, self.day)
        if jpath.exists() and jpath.stat().st_size > journal.HEADER_SIZE:
            raise SessionDayError(f"{jpath.name} already holds records: replay it, do not open it afresh")
        bundle = bundle_mod.resolve_session_day(self.session_id, self.cfg, self.day, sod_state)
        text = bundle_mod.dumps(bundle) + "\n"
        bpath = paths.day_bundle_file(self.session_id, self.day)
        tmp = bpath.with_name(bpath.name + ".tmp")
        tmp.write_bytes(text.encode("utf-8"))
        os.replace(tmp, bpath)
        self.bundle_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self.ids = frozenset(int(i["id"]) for i in bundle["instruments"])
        self.engine = session_engine(bpath, self.cfg)
        try:
            self._journal = self._writer(jpath)
            if self._journal.records:
                raise SessionDayError(f"{jpath.name} already holds records: replay it, do not open it afresh")
            store.upsert_session_day(self.session_id, self.trade_date, bundle_sha256=self.bundle_sha256,
                                     journal_path=str(jpath), status="open")
            enabled = True if sod_state is None else bool(sod_state["strategy_enabled"])
            held = [] if sod_state is None else sorted(sod_state["pause_reasons"])
            if (enabled, held) != (not reasons, reasons):
                self.control(not reasons, reasons, t0)
        except BaseException:
            self.close()
            raise

    def _writer(self, jpath: Path) -> journal.JournalWriter:
        return journal.JournalWriter(jpath, fsync_every_mono_ns=self._fsync_ns, mono=self._mono)

    def replay(self) -> dict:
        """Rebuild the day from what is on disk after a restart (or to audit it): the day's bundle and journal are
        read, its stored outputs are reset (so nothing stored from beyond the journal survives), a fresh engine is
        fed the journal's records, and the engine's outputs are stored. An open journal is reopened (a torn tail is
        repaired by the writer) and the day goes on taking input; a finalised one makes a replay-only day. The
        journal is never written. Returns {"records", "next_event_no", "carried_orders"}."""
        if self._journal is not None or self.engine is not None:
            raise SessionDayError("the day is already open")
        jpath = paths.journal_file(self.session_id, self.day)
        bpath = paths.day_bundle_file(self.session_id, self.day)
        if not jpath.exists() or not bpath.exists():
            raise SessionDayError(f"no bundle and journal to replay for {self.session_id} on {self.trade_date}")
        raw = bpath.read_bytes()
        self.bundle_sha256 = hashlib.sha256(raw).hexdigest()
        self.ids = frozenset(int(i["id"]) for i in json.loads(raw)["instruments"])
        try:
            if journal.is_finalised(jpath):
                self._replay_only = True
            else:
                self._journal = self._writer(jpath)
            _, body, _ = journal.read(jpath)
            records = len(body) and self._count(body)
            store.reset_session_outputs(self.session_id, self.trade_date)
            self._next_event_no = 1
            self.carried_orders = []
            self.engine = session_engine(bpath, self.cfg)
            if body:
                self._push_and_store(body, None)
        except BaseException:
            self.close()
            raise
        return {"records": records, "next_event_no": self._next_event_no, "carried_orders": list(self.carried_orders)}

    @staticmethod
    def _count(body: bytes) -> int:
        n, off = 0, 0
        while off < len(body):
            off += int.from_bytes(body[off + 2:off + 4], "little")
            n += 1
        return n

    def _push_and_store(self, batch: bytes, last_frame: tuple[int, int] | None) -> None:
        if batch:
            self.engine.push(batch)
        carried: list[int] = []
        outputs = self.engine.drain_outputs()
        self._elog.feed(outputs.get("log") or [])
        self._next_event_no = store.apply_session_outputs(
            self.session_id, self.trade_date, outputs, first_event_no=self._next_event_no,
            last_frame=last_frame, carried=carried,
            latency_ns=int(self.cfg.get("latency_ms", 0)) * 1_000_000)
        for oid in carried:
            if oid not in self.carried_orders:
                self.carried_orders = sorted({*self.carried_orders, oid})
                log.warning("order %d of an earlier day has events today; it has no row for %s", oid, self.trade_date)

    def _guard(self) -> None:
        if self._failed is not None:
            raise SessionDayError(f"the day failed earlier ({self._failed!r}); replay it to recover") \
                from self._failed
        if self._replay_only:
            raise SessionDayError("the day's journal is finalised: it takes no more input")
        if self._journal is None or self.engine is None:
            raise SessionDayError("the day is not open")

    def feed(self, records: list[tuple[int, int, bytes]], last_frame: tuple[int, int] | None) -> None:
        """Journal the batch, push exactly those bytes to the engine, drain, store (with the frame mark). Any error
        fails the day (see the module doc); the error itself propagates this once."""
        self._guard()
        try:
            if records:
                batch = self._journal.append(records)
            elif last_frame is None:
                return
            else:
                batch = b""
            self._push_and_store(batch, last_frame)
        except BaseException as e:
            self._failed = e
            raise

    def control(self, enabled: bool, reasons: list[str], t: int) -> None:
        """Journal a pause or resume (a `strategy_control` REFDATA record, D-70) and apply it like any input."""
        reasons = sorted(set(reasons))
        if enabled == bool(reasons):
            raise SessionDayError(f"enabled={enabled} with reasons {reasons}: enabled has none, disabled has some")
        payload = {"enabled": enabled, "reasons": reasons}
        self.feed([(t, CONTROL_RANK, packs.enc_refdata(0, t, "strategy_control", payload))], None)

    def fsync(self) -> None:
        if self._journal is not None:
            self._journal.fsync()

    def close(self, *, finalise: bool = False) -> None:
        """Close the journal (finalising it only when asked) and drop the engine, which holds the packs mapped."""
        self._elog.flush()
        if self._journal is not None:
            try:
                self._journal.close(finalise=finalise)
            finally:
                self._journal = None
        self.engine = None

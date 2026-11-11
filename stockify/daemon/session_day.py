"""One paper session-day: journal first, then the engine, then the store (DM 5.4; M5.3; D-68).

    day = SessionDay("orb", date(2026, 11, 17), cfg, mono=time.monotonic_ns)
    day.open(sod_state, reasons, t0)           # bundle on disk, engine built, journal opened
    day.feed(records, last_frame)              # append -> push -> drain -> store

The order is the point. A batch of `(t, rank, record)` is appended to the session journal (which numbers it, makes
its `t` monotonic and flushes it) and only then pushed to the engine, whose outputs are then stored in one
transaction together with the frame mark. A crash therefore leaves a journal that is at least as far along as the
engine and the store, and replaying it rebuilds both exactly. Every failure propagates: a journal error leaves the
engine untouched; an engine error leaves the journal holding the input that caused it.

`session_engine` is the only place a session builds an engine.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Callable

from stockify import bundle as bundle_mod, engine, paths, store
from stockify.daemon import journal
from stockify.data import packs

log = logging.getLogger("stockify.daemon.session_day")

FSYNC_EVERY_NS = 1_000_000_000          # the journal's fsync cadence, on the caller's monotonic clock
CONTROL_RANK = packs.RANK[packs.REFDATA_K]


class SessionDayError(RuntimeError):
    """A session-day that cannot be opened or fed."""


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
    def __init__(self, session_id: str, day: date, cfg: dict, *, mono: Callable[[], int]) -> None:
        self.session_id = session_id
        self.day = day
        self.cfg = cfg
        self._mono = mono
        self.engine = None
        self.ids: frozenset[int] = frozenset()
        self.bundle_sha256: str | None = None
        self._journal: journal.JournalWriter | None = None
        self._next_event_no = 1

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
        restart, which replays it first; that is the caller's job, so this refuses it."""
        if self._journal is not None:
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
        import hashlib
        self.bundle_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self.ids = frozenset(int(i["id"]) for i in bundle["instruments"])
        self.engine = session_engine(bpath, self.cfg)
        try:
            self._journal = journal.JournalWriter(jpath, fsync_every_mono_ns=FSYNC_EVERY_NS, mono=self._mono)
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

    def feed(self, records: list[tuple[int, int, bytes]], last_frame: tuple[int, int] | None) -> None:
        """Journal the batch, push exactly those bytes to the engine, drain, store (with the frame mark)."""
        if self._journal is None or self.engine is None:
            raise SessionDayError("the day is not open")
        if records:
            self.engine.push(self._journal.append(records))
        elif last_frame is None:
            return
        self._next_event_no = store.apply_session_outputs(
            self.session_id, self.trade_date, self.engine.drain_outputs(), first_event_no=self._next_event_no,
            last_frame=last_frame)

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
        if self._journal is not None:
            try:
                self._journal.close(finalise=finalise)
            finally:
                self._journal = None
        self.engine = None

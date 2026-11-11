"""Finalising a session-day, and the next day's start-of-day state (DM 5.5; M5.3; A4 §7.2).

    finalise_day("orb", day, files_complete=True, cutoff_passed=False)     # -> FinaliseResult
    next_sod_state("orb", next_day)                                        # the state the next day opens with

Each (session, day) is finalised under an OS file lock beside its journal (a second caller waits briefly, then gets
`busy`; a crashed holder never blocks). A day is finalised once NSE's evening files are in (`files_complete`), or, if they never come, at the job's cut-off
(`cutoff_passed`): until one of the two it is `waiting` and nothing is touched. Then:

    1. the SETTLE records the day needs are appended to the session journal (`ordered_t` keeps their `t` from going
       before the journal's last), those a crashed earlier attempt already wrote are not written again;
    2. the engine ends the day: the live one, if the caller still has it, else a fresh one replays the journal.
       Its book, end-of-day positions, equity row, state and labels are stored in one transaction, the stored
       outputs of a replayed day rebuilt from the replay;
    3. the journal is finalised (sidecar, journal, header, journal: the writer's own order);
    4. the session goes ARMED with its reasons kept (STOPPED stays STOPPED);
    5. only now the day's row says `finalised`, which is what `next_sod_state` waits for.

Every step is repeatable, and a crash between any two leaves a day that is not yet `finalised`: running it again
finishes the job and gives the rows a clean run gives. A day whose row already says `finalised` is left alone, so a
re-run never arms a session that has since started its next day. A journal is only ever appended to and then
finalised, never rewritten. Finalising writes no `GAP(process_stop)`: that belongs to a shutdown, which never
finalises.
"""
from __future__ import annotations

import csv
import errno
import json
import logging
import msvcrt
import os
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from stockify import paths, store, timeutil
from stockify.config import loader
from stockify.daemon import journal, lifecycle
from stockify.daemon.session_day import SessionDay, SessionDayError, session_engine
from stockify.data import packs
from stockify.oracle import sod_state

log = logging.getLogger("stockify.daemon.finalise")

FALLBACK_LABEL = "settlement_fallback"
CLAIM_WAIT_S = 5.0                                  # how long a second caller waits for the day's claim before `busy`
_CLAIM_OFFSET = 1 << 20                              # the locked byte of the claim file
_RESTING = ("CREATED", "RUNNING", "PAUSED")          # the states a finished day arms; ARMED and STOPPED stay as they are


class FinaliseError(RuntimeError):
    """A day that cannot be finalised, or whose end-of-day state cannot be handed on."""


@dataclass
class FinaliseResult:
    session_id: str
    day: date
    status: str                                       # finalised | waiting | busy | failed
    labels: list[str] = field(default_factory=list)
    settle_count: int = 0
    error: str | None = None


class Claim:
    """An OS byte lock on a file beside the day's journal. The OS drops it when the holder ends, however it ends, so
    a crashed holder never blocks the next caller; two handles in one process exclude each other too."""

    def __init__(self, fd: int) -> None:
        self._fd: int | None = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            os.lseek(fd, _CLAIM_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            os.close(fd)

    def __enter__(self) -> "Claim":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def claim_path(session_id: str, day: date) -> Path:
    return paths.journal_file(session_id, day).with_suffix(".finalise.lock")


def try_claim(path: Path, wait_s: float) -> Claim | None:
    """Take the claim, waiting up to `wait_s` seconds; None if someone else still holds it."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_BINARY, 0o600)
    deadline = time.monotonic() + wait_s
    while True:
        try:
            os.lseek(fd, _CLAIM_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return Claim(fd)
        except OSError as e:
            if e.errno not in (errno.EACCES, errno.EDEADLK):
                os.close(fd)
                raise
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.05)


def _split(body: bytes) -> list[bytes]:
    out, off = [], 0
    while off < len(body):
        size = int.from_bytes(body[off + 2:off + 4], "little")
        if size < 24 or off + size > len(body):
            raise FinaliseError("the journal's records do not tile its body")
        out.append(body[off:off + size])
        off += size
    return out


def replay_into(engine, journal_path: Path, batch_records: int = 2000) -> int:  # noqa: ANN001
    """Push a journal's whole records (never a torn tail) to `engine`, `batch_records` at a time; returns how many.
    The journal is only read."""
    if batch_records < 1:
        raise ValueError("batch_records must be at least 1")
    _, body, _ = journal.read(Path(journal_path))
    records = _split(body)
    for i in range(0, len(records), batch_records):
        engine.push(b"".join(records[i:i + batch_records]))
    return len(records)


def _config(session_id: str) -> dict:
    row = store.get_session(session_id)
    if row is None or not row.get("config"):
        raise FinaliseError(f"no such session: {session_id!r}")
    return row["config"]


def fresh_engine(session_id: str, day: date):  # noqa: ANN201
    """A new engine on the day's own bundle (its `sod_state` included), the session's strategy set."""
    return session_engine(paths.day_bundle_file(session_id, day), _config(session_id))


def _bundle_ids(session_id: str, day: date) -> set[int]:
    raw = json.loads(paths.day_bundle_file(session_id, day).read_text(encoding="utf-8"))
    return {int(i["id"]) for i in raw["instruments"]}


def _settle_keys(jpath: Path) -> tuple[set[tuple[int, int]], int]:
    """({(instrument id, subtype)} of the SETTLE records already in the journal, how many there are)."""
    _, body, _ = journal.read(jpath)
    keys, off = set(), 0
    while off < len(body):
        rec, off = packs.decode(body, off)
        if rec["kind"] == "SETTLE":
            keys.add((rec["instrument_id"], rec["subtype"]))
    return keys, len(keys)


def _missing_settles(session_id: str, day: date, jpath: Path) -> tuple[list[tuple[int, int, bytes]], int]:
    have, n = _settle_keys(jpath)
    wanted = packs.settle_records(_bundle_ids(session_id, day), day)
    missing = []
    for t, rank, rec in wanted:
        parsed, _ = packs.decode(rec, 0)
        if (parsed["instrument_id"], parsed["subtype"]) not in have:
            missing.append((t, rank, rec))
    return missing, n + len(missing)


def _csv_rows(folder: Path, name: str) -> list[dict]:
    with open(folder / name, newline="", encoding="utf-8") as f:
        return [dict(r) for r in csv.DictReader(f)]


def _writer(jpath: Path) -> journal.JournalWriter:
    secs = int(loader.load_json("paper.json")["journal"]["fsync_every_s"])
    return journal.JournalWriter(jpath, fsync_every_mono_ns=secs * 1_000_000_000, mono=time.monotonic_ns)


def finalise_day(session_id: str, day: date, *, files_complete: bool, cutoff_passed: bool,
                 live: SessionDay | None = None) -> FinaliseResult:
    """Finalise one session-day (see the module doc). `live` is the day's own SessionDay when this process still
    holds it (its engine is used, nothing is replayed, and it is closed); otherwise a fresh engine replays the
    journal. A day that cannot be finalised for what it holds (no journal, a refused state) is returned as
    `failed`; an I/O or database error is raised, for the caller to retry."""
    if not files_complete and not cutoff_passed:
        return FinaliseResult(session_id, day, "waiting")
    claim = try_claim(claim_path(session_id, day), CLAIM_WAIT_S)
    if claim is None:
        log.info("session %s: %s is being finalised elsewhere", session_id, day)
        return FinaliseResult(session_id, day, "busy")
    try:
        return _finalise(session_id, day, live=live)
    except (FinaliseError, journal.JournalError, SessionDayError, _run_error()) as e:
        log.error("session %s: cannot finalise %s: %s", session_id, day, e)
        return FinaliseResult(session_id, day, "failed", error=str(e))
    finally:
        claim.release()


def _run_error() -> type[BaseException]:
    from stockify import engine
    return engine.load().RunError


def _finalise(session_id: str, day: date, *, live: SessionDay | None) -> FinaliseResult:
    d = day.isoformat()
    session = store.get_session(session_id)
    if session is None:
        raise FinaliseError(f"no such session: {session_id!r}")
    cfg = _config(session_id)
    row = store.get_session_day(session_id, d)
    jpath, bpath = paths.journal_file(session_id, day), paths.day_bundle_file(session_id, day)
    if row is not None and row["status"] == "finalised":                       # nothing to do, and nothing to arm
        if live is not None:
            live.close()
        _, n = _settle_keys(jpath)
        return FinaliseResult(session_id, day, "finalised", json.loads(row["labels_json"] or "[]"), n)
    if row is None or not jpath.exists() or not bpath.exists():
        raise FinaliseError(f"{session_id} has no journal and bundle for {d}: nothing to finalise")

    # 1. the SETTLE records, appended to the journal (the live day's own writer when there is one)
    finalised_already = journal.is_finalised(jpath)
    missing, total = _missing_settles(session_id, day, jpath)
    writer = None
    journal_lacks = bool(missing) and finalised_already     # finalised before the evening files: append nothing (I2)
    if journal_lacks:
        missing = []
    try:
        if missing:
            if live is not None:
                live.feed(missing, None)
            else:
                writer = _writer(jpath)
                writer.append(missing)
        # 2. the end of the day in the engine, and its rows in the store
        if live is not None:
            engine, own = live.engine, False
            live.finish()
        else:
            engine, own = fresh_engine(session_id, day), True
            replay_into(engine, jpath)
            engine.finish()
        try:
            state_text = engine.eod_state()
            folder = paths.journal_day_dir(session_id, day)
            engine.write_artifacts(str(folder))
            labels = sorted(set(engine.labels()) | ({FALLBACK_LABEL} if journal_lacks else set()))
            if own:                                      # a replayed day's stored outputs are rebuilt from the replay
                frame = store.session_last_frame(session_id, d)
                outputs = engine.drain_outputs()
                carried: list[int] = []
        finally:
            engine = None
        if own:
            store.reset_session_outputs(session_id, d)
            store.apply_session_outputs(session_id, d, outputs, first_event_no=1, last_frame=frame, carried=carried,
                                        latency_ns=int(cfg.get("latency_ms", 0)) * 1_000_000)
            for oid in sorted(set(carried)):
                log.warning("order %d of an earlier day has events today; it has no row for %s", oid, d)
        equity = [r for r in _csv_rows(folder, "equity.csv") if r["trade_date"] == d]
        if len(equity) != 1:
            raise FinaliseError(f"the engine wrote {len(equity)} equity rows for {d}, not one")
        timeline = []
        warning = sod_state.size_warning(state_text, sod_state.warn_bytes())
        if warning:
            log.warning("session %s %s: %s", session_id, d, warning)
            timeline.append(("sod_state_size", {"day": d, "warning": warning}))
        if journal_lacks:
            log.warning("session %s %s: the journal was finalised before the settlement files; the day ends with "
                        "the cut-off fallback", session_id, d)
            timeline.append((FALLBACK_LABEL, {"day": d, "reason": "journal finalised before the settlement files"}))
        store.save_session_day_end(
            session_id, d, orders=_csv_rows(folder, "orders.csv"), positions=_csv_rows(folder, "positions_eod.csv"),
            equity={"nav_paise": equity[0]["nav_paise"], "cash_paise": equity[0]["cash_paise"],
                    "margin_paise": equity[0]["margin_blocked_paise"]},
            eod_state_json=state_text, labels=labels, timeline=timeline, wall_ns=timeutil.now_ns())
        # 3. the journal
        if live is not None:
            live.close(finalise=True)
        elif writer is not None:
            writer.close(finalise=True)
        elif not finalised_already:
            w = _writer(jpath)
            w.close(finalise=True)
    except BaseException:
        if writer is not None:
            try:
                writer.close(finalise=False)
            except Exception:  # noqa: BLE001 — the first error is the one to report
                log.warning("could not close the journal of %s %s after an error", session_id, d, exc_info=True)
        raise
    # 4. the session, 5. the day's row
    now = timeutil.now_ns()
    status = store.get_session(session_id)["status"]
    if status == "STOPPED":
        lifecycle.stop(session_id, wall_ns=now)
    elif status in _RESTING:
        lifecycle.arm(session_id, wall_ns=now)
    store.upsert_session_day(session_id, d, status="finalised", finalised_ns=now)
    log.info("session %s: %s finalised (%d settlement records)", session_id, d, total)
    return FinaliseResult(session_id, day, "finalised", labels, total)


def previous_trading_day(day: date) -> date:
    """The dated calendar's trading day before `day` (weekends and NSE holidays skipped)."""
    from stockify.bundle import _is_trading_day
    prev = day - timedelta(days=1)
    for _ in range(60):
        if _is_trading_day(prev):
            return prev
        prev -= timedelta(days=1)
    raise FinaliseError(f"no trading day in the 60 days before {day}")


def next_sod_state(session_id: str, day: date) -> dict | None:
    """The state the session's `day` opens with: the `eod_state_json` of the calendar's previous trading day, or
    None for a session's first day (no earlier day of its own). An earlier day that is missing or not finalised is
    an error, never a start from nothing and never an older state handed on."""
    d = day.isoformat()
    if store.previous_session_day(session_id, d) is None:
        return None
    prev = previous_trading_day(day).isoformat()
    row = store.get_session_day(session_id, prev)
    if row is None:
        raise FinaliseError(f"{session_id} has no record of {prev}, the trading day before {d}: it cannot start {d} "
                            "from an older state")
    if row["status"] != "finalised" or not row["eod_state_json"]:
        raise FinaliseError(f"{session_id}: {prev} is not finalised ({row['status']}); {d} cannot start from it")
    return json.loads(row["eod_state_json"])

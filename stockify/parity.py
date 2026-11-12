"""Nightly journal parity (RD 5.1; M5.10; E-01): a finalised session-day, replayed from its journal, must reproduce
what the store holds.

    journal_parity("orb", day)      # -> ParityResult(outcome="pass" | "fail" | "skipped", summary=...)

A fresh engine is built on the day's own saved bundle (its `sod_state` included), fed the day's journal, finished, and
its artifacts and drained outputs are compared with the stored orders, order events, fills, charges, end-of-day
positions, equity row and end-of-day state (`eod_state`, which seeds the next day), table by table and key by key, in that order. Every row must be identical, a row on one
side only is a difference, and the first difference is named. Parity is stricter than recovery on purpose: recovery
repairs a store that is behind or ahead of the journal, parity repairs nothing.

    pass       the result goes into `parity_results` and `session_days.parity`, and a `parity` timeline row says so;
    fail       the session is paused with `parity_failure` (a STOPPED one cannot be), the timeline row names the
               first difference, `log.error` says so, and the result is stored LAST, so a crash before it leaves a
               rerun that still pauses. The store is left as found: it is the evidence. This is a bug (E-01).
               A later day whose journal is open gets the pause through the owner command queue (the dispatcher
               journals `strategy_control` first); otherwise the store carries it and the next day opens with it.
               The same mismatch found again (by digest) writes one `parity_still_open` row and no new alarm.
    skipped    the day is not finalised (or the session is unknown): nothing is written, a later run decides.

A finalised day whose journal or bundle cannot be read, or that the engine refuses, is a `fail` too: the gate did
not pass, and saying nothing would be silence. A transient I/O or database error is raised, not recorded.

Parity only reads: the journal, the bundle and the store (a finished day's journal is never written, and nothing here
takes the daemon's lock or the day's finalise claim: a day whose status says `finalised` is complete).
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from stockify import paths, store, timeutil
from stockify.daemon import finalise, journal, lifecycle
from stockify.daemon.session_day import SessionDayError

log = logging.getLogger("stockify.parity")

KIND = "journal"
TABLES = ("orders", "order_events", "fills", "charges", "positions", "equity", "eod_state")   # the order of the comparison
_ALL = 2 ** 31


@dataclass
class ParityResult:
    session_id: str
    day: date
    kind: str
    outcome: str                       # pass | fail | skipped
    summary: dict = field(default_factory=dict)

    @property
    def difference_line(self) -> str | None:
        """`differs: <table> <first row>` for a failed comparison, else None."""
        first = self.summary.get("first_difference")
        if first is not None:
            return (f"differs: {first['table']} [{first['key']}] stored {json.dumps(first['stored'], sort_keys=True)} "
                    f"!= regenerated {json.dumps(first['regenerated'], sort_keys=True)}")
        if self.outcome == "fail" and self.summary.get("error"):
            return f"differs: journal {self.summary['error']}"
        return None


def _csv(folder: Path, name: str) -> list[dict]:
    with open(folder / name, newline="", encoding="utf-8") as f:
        return [dict(r) for r in csv.DictReader(f)]


def _norm(row):  # noqa: ANN001, ANN202
    return None if row is None else json.loads(json.dumps(row, sort_keys=True))


def _first_difference(table: str, stored: dict, twin: dict) -> dict | None:
    for key in sorted(set(stored) | set(twin)):
        have, want = _norm(stored.get(key)), _norm(twin.get(key))
        if have != want:
            return {"table": table, "key": key, "stored": have, "regenerated": want}
    return None


def _stored(session_id: str, day: date) -> dict[str, dict]:
    d = day.isoformat()
    rows = store.session_day_rows(session_id, d)                    # keyed by the tables' own keys
    end = store.session_day_end(session_id, d)
    rows["order_events"] = {n: {**r, "event_no": n} for n, r in rows["order_events"].items()}
    return {**rows, "positions": {n: r for n, r in enumerate(end["positions"], 1)}, "equity": {d: end["equity"]},
            "eod_state": {d: store.get_session_day(session_id, d)["eod_state_json"]}}


def _regenerated(session_id: str, day: date) -> tuple[dict[str, dict], int]:
    """A fresh engine's rows for the day, and how many journal records it was fed. Writes only a temp folder."""
    d = day.isoformat()
    jpath = paths.journal_file(session_id, day)
    engine = finalise.fresh_engine(session_id, day)
    try:
        records = finalise.replay_into(engine, jpath)
        engine.finish()
        eod_state = engine.eod_state()
        with tempfile.TemporaryDirectory(prefix="stockify-parity-") as tmp:
            engine.write_artifacts(tmp)
            outputs = engine.drain_outputs()
            orders, positions, equity = (_csv(Path(tmp), n) for n in ("orders.csv", "positions_eod.csv", "equity.csv"))
    finally:
        engine = None
    rows = [r for r in equity if r["trade_date"] == d]
    if len(rows) != 1:
        raise finalise.FinaliseError(f"the engine wrote {len(rows)} equity rows for {d}, not one")
    return {
        "orders": {int(r["order_id"]): r for r in orders},
        "order_events": {n: {**e, "event_no": n} for n, e in enumerate(outputs.get("order_events", ()), 1)},
        "fills": {int(r["fill_id"]): r for r in outputs.get("fills", ())},
        "charges": {int(r["row_no"]): r for r in outputs.get("charges", ())},
        "positions": {n: r for n, r in enumerate(positions, 1)},
        "equity": {d: {"nav_paise": int(rows[0]["nav_paise"]), "cash_paise": int(rows[0]["cash_paise"]),
                       "margin_paise": int(rows[0]["margin_blocked_paise"])}},
        "eod_state": {d: eod_state},
    }, records


def _run_error() -> tuple[type[BaseException], ...]:
    """The engine's RunError, resolved before any `except` needs it; an engine that cannot be loaded gives () so the
    day's own error is the one reported (fresh_engine raises the load error itself)."""
    try:
        from stockify import engine
        return (engine.load().RunError,)
    except Exception:  # noqa: BLE001
        return ()


def journal_parity(session_id: str, day: date) -> ParityResult:
    """Replay the finalised day's journal and compare it with the store (see the module doc). Records the outcome
    unless the day is skipped."""
    d = day.isoformat()
    if store.get_session(session_id) is None:
        return ParityResult(session_id, day, KIND, "skipped", {"reason": f"no such session: {session_id!r}"})
    row = store.get_session_day(session_id, d)
    if row is None or row["status"] != "finalised":
        state = "no such day" if row is None else (row["status"] or "open")
        return ParityResult(session_id, day, KIND, "skipped", {"reason": f"{d} is not finalised ({state})"})

    jpath = paths.journal_file(session_id, day)
    summary: dict = {"day": d}
    run_error = _run_error()
    try:
        if not jpath.exists() or not paths.day_bundle_file(session_id, day).exists():
            raise finalise.FinaliseError(f"{session_id} has no journal and bundle for {d}, though the day is finalised")
        if not journal.is_finalised(jpath):
            raise finalise.FinaliseError(f"the journal of {d} is not finalised, though the day is")
        twin, records = _regenerated(session_id, day)
    except (finalise.FinaliseError, journal.JournalError, SessionDayError, *run_error) as e:
        summary.update(error=f"{type(e).__name__}: {e}", first_difference=None)
        return _record(ParityResult(session_id, day, KIND, "fail", summary))
    stored = _stored(session_id, day)
    first = None
    for table in TABLES:
        first = first or _first_difference(table, stored[table], twin[table])
    summary.update(records=records, rows={t: len(twin[t]) for t in TABLES},
                   stored_rows={t: len(stored[t]) for t in TABLES}, first_difference=first)
    return _record(ParityResult(session_id, day, KIND, "fail" if first else "pass", summary))


def _digest(result: ParityResult) -> str:
    what = result.summary.get("first_difference") or {"error": result.summary.get("error")}
    return hashlib.sha256(json.dumps(what, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _later_day_open(session_id: str, day: str) -> bool:
    """Whether a day after `day` has a journal that is still open (a running or crashed day, not a finalised one)."""
    for row in store.later_session_days(session_id, day):
        if row["status"] == "finalised":
            continue
        jpath = paths.journal_file(session_id, date.fromisoformat(row["trade_date"]))
        try:
            if jpath.exists() and not journal.is_finalised(jpath):
                return True
        except journal.JournalError:
            continue
    return False


def _pause(sid: str, day: str, now: int) -> str:
    """Pause the session with `parity_failure`; returns how. A stopped session cannot be paused ("stopped"). When a
    later day's journal is open the pause is queued as an owner command, so the dispatcher journals the
    `strategy_control` first (the running engine sees it and a restart keeps it); otherwise the store carries it
    and the next day opens with it."""
    session = store.get_session(sid)
    if session["status"] == "STOPPED":
        return "stopped"
    if not _later_day_open(sid, day):
        lifecycle.pause(sid, "parity_failure", wall_ns=now)
        return "stored"
    queued = any(c["session_id"] == sid and c["command"] == "pause"
                 and (c["args"] or {}).get("reason") == "parity_failure" for c in store.pending_commands())
    if "parity_failure" not in session["pause_reasons"] and not queued:
        store.add_command(sid, "pause", {"reason": "parity_failure"}, now)
    return "queued"


def _record(result: ParityResult) -> ParityResult:
    sid, d, now = result.session_id, result.day.isoformat(), timeutil.now_ns()
    if result.outcome == "pass":
        detail = {"day": d, "kind": KIND, "outcome": "pass", **{k: v for k, v in result.summary.items() if k != "day"}}
        log.info("parity %s %s: pass (%s records, %s)", sid, d, result.summary["records"],
                 ", ".join(f"{t} {n}" for t, n in result.summary["rows"].items()))
        store.add_timeline(sid, "parity", detail, wall_ns=now, engine_ns=None)
        store.put_parity_result(sid, d, KIND, "pass", result.summary, created_ns=now)
        return result
    digest = result.summary["digest"] = _digest(result)
    known = any(r["kind"] == "parity" and (r["detail"] or {}).get("outcome") == "fail"
                and (r["detail"] or {}).get("day") == d and (r["detail"] or {}).get("digest") == digest
                for r in store.timeline(sid, limit=1_000_000))
    how = _pause(sid, d, now)
    if known:                                       # the same mismatch as an earlier run: one short row, no new alarm
        log.warning("parity still failing for session %s on %s (%s): %s", sid, d, digest[:12], result.difference_line)
        store.add_timeline(sid, "parity_still_open", {"day": d, "digest": digest, "pause": how}, wall_ns=now,
                           engine_ns=None)
    else:
        log.error("parity FAILED for session %s on %s: %s. The stored rows disagree with a replay of the journal "
                  "(E-01): this is a bug. Nothing was repaired; the stored rows are the evidence.", sid, d,
                  result.difference_line)
        detail = {"day": d, "kind": KIND, "outcome": "fail", "pause": how,
                  **{k: v for k, v in result.summary.items() if k != "day"}}
        store.add_timeline(sid, "parity", detail, wall_ns=now, engine_ns=None)
    store.put_parity_result(sid, d, KIND, "fail", result.summary, created_ns=now)      # last: a rerun still pauses
    return result


# ── Recorder parity (RD 5.4; M5.10 report) ───────────────────────────────────────────────────────────────────────
#
#     recorder_parity("orb", day)     # -> ParityResult(kind="recorder", outcome="pass" | "skipped", summary=...)
#
# The finalised day is run again on a pack built from what the recorder kept (landing, or the archive once compacted),
# over the ids of the day's own bundle with a 1 s CLOCK: the same day bundle (its `sod_state` applied by the engine's
# constructor), the same strategy, a fresh engine. Its fills and NAV are set against the stored live ones. How far a
# backtest over recorded data drifts from the live session is the answer, not a verdict: this report NEVER fails and
# NEVER pauses a session, and what it stores is a `pass` with the numbers.
#
# Differences it can already explain are named so that they are not mistaken for bugs: frames the recorder dropped on
# a full queue (`queue_full` gap rows, which no pack can carry), and what only the journal holds (a journaled Kite
# margin answer, a strategy pause). `unexplained` is a difference with none of these to blame.
#
# It only reads the recorder's files: an archive is never written after compaction, and a landing file is opened
# `mode=ro&immutable=1` and only when its `-wal` is empty; a landing file still being written is "busy" and skipped
# (D-75, as the calendar audit). Skipped (nothing stored): an unknown session, a day not finalised, a day with no
# recording or a busy landing file, or a run that could not be made (no pack, a bundle the engine refuses).

RECORDER_KIND = "recorder"
CLOCK_MS = 1000                                  # the live day's CLOCK cadence (paper.json clock_every_s)
_FILL_KEY = ("t_ns", "instrument_id", "side", "qty", "price_paise")
_NO_JOURNAL_ONLY = {"margin_requirement": 0, "strategy_control": 0}


def _open_ticks(day: date):  # noqa: ANN202
    """The day's recorded ticks (archive first), or None; a landing file is opened immutable."""
    from stockify.data import ticks
    return ticks.open_day(day, immutable=True)


def _queue_full(gaps: list[dict]) -> dict:
    """The `queue_full` gap rows as spans, and how many frames they name. A landing file from before migration 0002
    has no frame columns: those are read with `.get`, and a span that names no frames adds none."""
    spans = [{"start_ns": g.get("start_ns"), "end_ns": g.get("end_ns"), "first_frame_no": g.get("first_frame_no"),
              "last_frame_no": g.get("last_frame_no"), "frames": g.get("frames")}
             for g in gaps if g.get("reason") == "queue_full"]
    spans.sort(key=lambda s: (s["start_ns"] or 0, s["first_frame_no"] or 0))
    return {"spans": spans, "frames": sum(s["frames"] or 0 for s in spans)}


def _journal_only(jpath: Path) -> dict:
    """What the journal holds that no pack can: journaled margin answers, and strategy pauses (the opening `enabled`
    record is not a difference)."""
    from stockify.data import packs
    counts = dict(_NO_JOURNAL_ONLY)
    _, body, _ = journal.read(jpath)
    off = 0
    while off < len(body):
        rec, off = packs.decode(body, off)
        if rec["kind"] != "REFDATA":
            continue
        if rec["subtype"] == "margin_requirement":
            counts["margin_requirement"] += 1
        elif rec["subtype"] == "strategy_control" and not rec["payload"].get("enabled", True):
            counts["strategy_control"] += 1
    return counts


def _fills(rows) -> list[dict]:  # noqa: ANN001
    """Fill rows in time order, normalised the way the stores compare them."""
    return sorted((_norm(r) for r in rows), key=lambda r: (r["t_ns"], r["fill_id"]))


def _first_fill_divergence(live: list[dict], twin: list[dict]) -> dict | None:
    for i in range(max(len(live), len(twin))):
        a = live[i] if i < len(live) else None
        b = twin[i] if i < len(twin) else None
        if a is None or b is None or [a[k] for k in _FILL_KEY] != [b[k] for k in _FILL_KEY]:
            return {"index": i, "live": a, "recorder": b}
    return None


def _recorder_run(session_id: str, day: date, sha: str) -> tuple[list[dict], int]:
    """(fills, NAV) of a fresh engine run over the pack. Writes only a temp folder."""
    from stockify.data import packs
    d = day.isoformat()
    engine = finalise.fresh_engine(session_id, day)
    try:
        finalise.push_records(engine, packs.read_bytes(packs.pack_path(sha))[packs.HEADER.size:])
        engine.finish()
        with tempfile.TemporaryDirectory(prefix="stockify-parity-") as tmp:
            engine.write_artifacts(tmp)
            outputs = engine.drain_outputs()
            equity = [r for r in _csv(Path(tmp), "equity.csv") if r["trade_date"] == d]
    finally:
        engine = None
    if len(equity) != 1:
        raise finalise.FinaliseError(f"the engine wrote {len(equity)} equity rows for {d}, not one")
    return _fills(outputs.get("fills", ())), int(equity[0]["nav_paise"])


def _matched(live: list[dict], twin: list[dict]) -> int:
    """How many live fills have an equal fill (time, instrument, side, quantity, price) on the other side, each used once."""
    left: dict[tuple, int] = {}
    for r in twin:
        k = tuple(r[f] for f in _FILL_KEY)
        left[k] = left.get(k, 0) + 1
    n = 0
    for r in live:
        k = tuple(r[f] for f in _FILL_KEY)
        if left.get(k):
            left[k] -= 1
            n += 1
    return n


def recorder_parity(session_id: str, day: date) -> ParityResult:
    """Run the finalised day again on the recorder's data and report the drift (see the section note above)."""
    from stockify.data import packs, ticks

    def skipped(why: str) -> ParityResult:
        return ParityResult(session_id, day, RECORDER_KIND, "skipped", {"reason": why})

    d = day.isoformat()
    if store.get_session(session_id) is None:
        return skipped(f"no such session: {session_id!r}")
    row = store.get_session_day(session_id, d)
    if row is None or row["status"] != "finalised":
        state = "no such day" if row is None else (row["status"] or "open")
        return skipped(f"{d} is not finalised ({state})")
    if not ticks.has_day(day):
        return skipped(f"{d} was not recorded: no landing file and no archive")
    busy = ticks.landing_busy(day)
    if busy:
        return skipped(busy)
    jpath = paths.journal_file(session_id, day)
    if not paths.day_bundle_file(session_id, day).exists():
        return skipped(f"{session_id} has no bundle for {d}")
    try:
        ids = sorted(finalise._bundle_ids(session_id, day))
        tick_day = _open_ticks(day)
        if tick_day is None:
            return skipped(f"{d} was not recorded: no landing file and no archive")
        try:
            queue_full = _queue_full(tick_day.gaps())
        finally:
            tick_day.close()
        sha = packs.build({"dataset": "ticks", "from": d, "to": d, "instruments": [{"ids": ids}],
                           "clock_cadence_ms": CLOCK_MS}, immutable=True)
        twin_fills, twin_nav = _recorder_run(session_id, day, sha)
        journal_only = _journal_only(jpath) if jpath.exists() else dict(_NO_JOURNAL_ONLY)
    except (packs.PackError, finalise.FinaliseError, journal.JournalError, SessionDayError, *_run_error()) as e:
        return skipped(f"the recorder's run could not be made: {type(e).__name__}: {e}")
    busy = ticks.landing_busy(day)                  # an immutable read takes no lock: a writer may have started under it
    if busy:
        return skipped(busy)

    live_fills = _fills(store.session_day_rows(session_id, d)["fills"].values())
    first = _first_fill_divergence(live_fills, twin_fills)
    pnl_diff = twin_nav - store.session_day_end(session_id, d)["equity"]["nav_paise"]
    explained_by = [name for name, n in (("queue_full", len(queue_full["spans"])),
                                         ("margin_requirement", journal_only["margin_requirement"]),
                                         ("strategy_control", journal_only["strategy_control"])) if n]
    summary = {"day": d, "fills_live": len(live_fills), "fills_recorder": len(twin_fills),
               "fills_matched": _matched(live_fills, twin_fills),
               "pnl_diff_paise": pnl_diff,                         # the recorder run's NAV less the live NAV
               "first_divergence_ns": None if first is None else min(
                   r["t_ns"] for r in (first["live"], first["recorder"]) if r is not None),
               "first_divergence": first,
               "known_differences": {"queue_full": queue_full, "journal_only": journal_only},
               "explained_by": explained_by,
               "unexplained": bool((first is not None or pnl_diff != 0) and not explained_by),
               "pack": sha}
    return _record_report(ParityResult(session_id, day, RECORDER_KIND, "pass", summary))


def _record_report(result: ParityResult) -> ParityResult:
    sid, d, s = result.session_id, result.day.isoformat(), result.summary
    now = timeutil.now_ns()
    log.info("recorder parity %s %s: fills %s live, %s recorder, %s matched; NAV diff %s paise%s", sid, d,
             s["fills_live"], s["fills_recorder"], s["fills_matched"], s["pnl_diff_paise"],
             " (unexplained)" if s["unexplained"] else "")
    detail = {"day": d, "kind": RECORDER_KIND, "outcome": "pass", **{k: v for k, v in s.items() if k != "day"}}
    store.add_timeline(sid, "parity", detail, wall_ns=now, engine_ns=None)
    store.put_parity_result(sid, d, RECORDER_KIND, "pass", s, created_ns=now)
    return result

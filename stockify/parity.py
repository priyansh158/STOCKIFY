"""Nightly journal parity (RD 5.1; M5.10; E-01): a finalised session-day, replayed from its journal, must reproduce
what the store holds.

    journal_parity("orb", day)      # -> ParityResult(outcome="pass" | "fail" | "skipped", summary=...)

A fresh engine is built on the day's own saved bundle (its `sod_state` included), fed the day's journal, finished, and
its artifacts and drained outputs are compared with the stored orders, order events, fills, charges, end-of-day
positions and equity row, table by table and key by key, in that order. Every row must be identical, a row on one
side only is a difference, and the first difference is named. Parity is stricter than recovery on purpose: recovery
repairs a store that is behind or ahead of the journal, parity repairs nothing.

    pass       the result goes into `parity_results` and `session_days.parity`, and a `parity` timeline row says so;
    fail       the session is paused with `parity_failure` (a STOPPED one cannot be), the timeline row names the
               first difference, `log.error` says so, and the result is stored LAST, so a crash before it leaves a
               rerun that still pauses. The store is left as found: it is the evidence. This is a bug (E-01).
    skipped    the day is not finalised (or the session is unknown): nothing is written, a later run decides.

A finalised day whose journal or bundle cannot be read, or that the engine refuses, is a `fail` too: the gate did
not pass, and saying nothing would be silence. A transient I/O or database error is raised, not recorded.

Parity only reads: the journal, the bundle and the store (a finished day's journal is never written, and nothing here
takes the daemon's lock or the day's finalise claim: a day whose status says `finalised` is complete).
"""
from __future__ import annotations

import csv
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
TABLES = ("orders", "order_events", "fills", "charges", "positions", "equity")     # the order of the comparison
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
    return {**rows, "positions": {n: r for n, r in enumerate(end["positions"], 1)}, "equity": {d: end["equity"]}}


def _regenerated(session_id: str, day: date) -> tuple[dict[str, dict], int]:
    """A fresh engine's rows for the day, and how many journal records it was fed. Writes only a temp folder."""
    d = day.isoformat()
    jpath = paths.journal_file(session_id, day)
    engine = finalise.fresh_engine(session_id, day)
    try:
        records = finalise.replay_into(engine, jpath)
        engine.finish()
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
    }, records


def _run_error() -> type[BaseException]:
    from stockify import engine
    return engine.load().RunError


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
    try:
        if not jpath.exists() or not paths.day_bundle_file(session_id, day).exists():
            raise finalise.FinaliseError(f"{session_id} has no journal and bundle for {d}, though the day is finalised")
        if not journal.is_finalised(jpath):
            raise finalise.FinaliseError(f"the journal of {d} is not finalised, though the day is")
        twin, records = _regenerated(session_id, day)
    except (finalise.FinaliseError, journal.JournalError, SessionDayError, _run_error()) as e:
        summary.update(error=f"{type(e).__name__}: {e}", first_difference=None)
        return _record(ParityResult(session_id, day, KIND, "fail", summary))
    stored = _stored(session_id, day)
    first = None
    for table in TABLES:
        first = first or _first_difference(table, stored[table], twin[table])
    summary.update(records=records, rows={t: len(twin[t]) for t in TABLES},
                   stored_rows={t: len(stored[t]) for t in TABLES}, first_difference=first)
    return _record(ParityResult(session_id, day, KIND, "fail" if first else "pass", summary))


def _record(result: ParityResult) -> ParityResult:
    sid, d, now = result.session_id, result.day.isoformat(), timeutil.now_ns()
    detail = {"day": d, "kind": KIND, "outcome": result.outcome,
              **{k: v for k, v in result.summary.items() if k != "day"}}
    if result.outcome == "pass":
        log.info("parity %s %s: pass (%s records, %s)", sid, d, result.summary["records"],
                 ", ".join(f"{t} {n}" for t, n in result.summary["rows"].items()))
        store.add_timeline(sid, "parity", detail, wall_ns=now, engine_ns=None)
        store.put_parity_result(sid, d, KIND, "pass", result.summary, created_ns=now)
        return result
    log.error("parity FAILED for session %s on %s: %s. The stored rows disagree with a replay of the journal (E-01): "
              "this is a bug. Nothing was repaired; the stored rows are the evidence.", sid, d,
              result.difference_line)
    if store.get_session(sid)["status"] != "STOPPED":              # a stopped session cannot be paused; the rows say so
        lifecycle.pause(sid, "parity_failure", wall_ns=now)
    store.add_timeline(sid, "parity", detail, wall_ns=now, engine_ns=None)
    store.put_parity_result(sid, d, KIND, "fail", result.summary, created_ns=now)      # last: a rerun still pauses
    return result

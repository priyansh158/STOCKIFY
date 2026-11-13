"""Session lifecycle with stacked pause reasons (M5.2; D-70).

States: CREATED -> ARMED -> RUNNING <-> PAUSED, ARMED again after each day, STOPPED final. A pause is a SET of
reasons, not a flag: the owner's pause survives a token expiry and the next login, which removes only its own
reason. The set is what the engine sees as the journaled `strategy_control` record (enabled = empty set), so a
session in any state with a non-empty set is disabled. Every change writes a `state` timeline row
{from, to, reasons}.

Each transition is one SQLite transaction (store.transition: BEGIN IMMEDIATE, read, decide, write state and
row, commit), so a crash leaves neither half and two connections can never overwrite each other's reason.
`conn` on each function is another connection to the lab file (tests, other processes); default is the
process's own. The `stopped` reason is stop()'s alone.
"""
from __future__ import annotations

import sqlite3

from stockify import store

STATES = store.SESSION_STATES
PAUSE_REASONS = store.SESSION_PAUSE_REASONS


class LifecycleError(ValueError):
    pass


def _run(session_id: str, decide, *, wall_ns: int, engine_ns: int | None, conn) -> dict:
    def guarded(s: dict):
        if s["status"] == "STOPPED" and not getattr(decide, "allow_stopped", False):
            raise LifecycleError(f"session {session_id!r} is STOPPED")
        return decide(s)

    try:
        after, _ = store.transition(session_id, guarded, wall_ns=wall_ns, engine_ns=engine_ns, conn=conn)
    except KeyError:
        raise LifecycleError(f"no such session: {session_id!r}") from None
    return after


def _check_reason(reason: str) -> None:
    if reason not in PAUSE_REASONS:
        raise LifecycleError(f"unknown pause reason {reason!r}; one of {', '.join(PAUSE_REASONS)}")
    if reason == "stopped":
        raise LifecycleError("the `stopped` reason is set by stop() alone")


def pause(session_id: str, reason: str, *, wall_ns: int, engine_ns: int | None = None,
          conn: sqlite3.Connection | None = None) -> list[str]:
    """Add a reason and return the set. RUNNING -> PAUSED; CREATED, ARMED and PAUSED keep their state."""
    _check_reason(reason)

    def decide(s: dict):
        return ("PAUSED" if s["status"] == "RUNNING" else s["status"],
                sorted(set(s["pause_reasons"]) | {reason}))

    return _run(session_id, decide, wall_ns=wall_ns, engine_ns=engine_ns, conn=conn)["pause_reasons"]


def resume(session_id: str, reason: str, *, wall_ns: int, engine_ns: int | None = None,
           conn: sqlite3.Connection | None = None) -> list[str]:
    """Remove only that reason (`owner` also clears `stop_all`). PAUSED -> RUNNING only when the set is empty."""
    _check_reason(reason)

    def decide(s: dict):
        drop = {reason, "stop_all"} if reason == "owner" else {reason}
        rest = sorted(set(s["pause_reasons"]) - drop)
        return ("RUNNING" if s["status"] == "PAUSED" and not rest else s["status"], rest)

    return _run(session_id, decide, wall_ns=wall_ns, engine_ns=engine_ns, conn=conn)["pause_reasons"]


def arm(session_id: str, *, wall_ns: int, conn: sqlite3.Connection | None = None) -> None:
    """CREATED, RUNNING or PAUSED -> ARMED (waiting for the next day), reasons kept."""
    _run(session_id, lambda s: ("ARMED", s["pause_reasons"]), wall_ns=wall_ns, engine_ns=None, conn=conn)


def start_continuous(session_id: str, *, wall_ns: int, engine_ns: int,
                     conn: sqlite3.Connection | None = None) -> str:
    """ARMED -> RUNNING when the reason set is empty, else PAUSED. Returns the new state."""
    def decide(s: dict):
        if s["status"] != "ARMED":
            raise LifecycleError(f"cannot start from {s['status']}; arm first")
        return ("PAUSED" if s["pause_reasons"] else "RUNNING", s["pause_reasons"])

    return _run(session_id, decide, wall_ns=wall_ns, engine_ns=engine_ns, conn=conn)["status"]


def stop(session_id: str, *, wall_ns: int, conn: sqlite3.Connection | None = None) -> None:
    """Final. Called by the dispatcher at the `stop` command, right after it journals `strategy_control(false, ..., "stopped")`
    (D-72); the day's finalisation calls it again harmlessly. The state, the `stopped` reason (earlier
    reasons are kept) and stopped_ns are written in one transaction; a retried stop changes nothing and keeps
    the first stopped_ns."""
    def decide(s: dict):
        if s["status"] == "STOPPED":
            return None
        return ("STOPPED", sorted(set(s["pause_reasons"]) | {"stopped"}))

    decide.allow_stopped = True
    _run(session_id, decide, wall_ns=wall_ns, engine_ns=None, conn=conn)

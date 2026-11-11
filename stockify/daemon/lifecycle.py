"""Session lifecycle with stacked pause reasons (M5.2; D-70).

States: CREATED -> ARMED -> RUNNING <-> PAUSED, ARMED again after each day, STOPPED final. A pause is a SET of
reasons, not a flag: the owner's pause survives a token expiry and the next login, which removes only its own
reason. The set is what the engine sees as the journaled `strategy_control` record (enabled = empty set), so a
session in any state with a non-empty set is disabled. Every change writes a `state` timeline row
{from, to, reasons}. A transition is a read-modify-write under one process-wide lock; the daemon is the only
writer of session state (the API queues commands in `session_commands`).
"""
from __future__ import annotations

import threading

from stockify import store

STATES = ("CREATED", "ARMED", "RUNNING", "PAUSED", "STOPPED")
PAUSE_REASONS = ("owner", "stop_all", "token_expired", "parity_failure", "determinism", "error", "stopped")

_lock = threading.RLock()


class LifecycleError(ValueError):
    pass


def _load(session_id: str) -> dict:
    s = store.get_session(session_id)
    if s is None:
        raise LifecycleError(f"no such session: {session_id!r}")
    if s["status"] == "STOPPED":
        raise LifecycleError(f"session {session_id!r} is STOPPED")
    return s


def _check_reason(reason: str) -> None:
    if reason not in PAUSE_REASONS:
        raise LifecycleError(f"unknown pause reason {reason!r}; one of {', '.join(PAUSE_REASONS)}")


def _move(session_id: str, old: str, new: str, old_reasons: list[str], reasons: list[str], *,
          wall_ns: int, engine_ns: int | None) -> None:
    reasons = sorted(set(reasons))
    if (old, old_reasons) == (new, reasons):
        return
    store.set_session_state(session_id, new, reasons)
    store.add_timeline(session_id, "state", {"from": old, "to": new, "reasons": reasons},
                       wall_ns=wall_ns, engine_ns=engine_ns)


def pause(session_id: str, reason: str, *, wall_ns: int, engine_ns: int | None = None) -> list[str]:
    """Add a reason and return the set. RUNNING -> PAUSED; CREATED, ARMED and PAUSED keep their state."""
    _check_reason(reason)
    with _lock:
        s = _load(session_id)
        reasons = sorted(set(s["pause_reasons"]) | {reason})
        new = "PAUSED" if s["status"] == "RUNNING" else s["status"]
        _move(session_id, s["status"], new, s["pause_reasons"], reasons, wall_ns=wall_ns, engine_ns=engine_ns)
        return reasons


def resume(session_id: str, reason: str, *, wall_ns: int, engine_ns: int | None = None) -> list[str]:
    """Remove only that reason (`owner` also clears `stop_all`). PAUSED -> RUNNING only when the set is empty."""
    _check_reason(reason)
    with _lock:
        s = _load(session_id)
        drop = {reason, "stop_all"} if reason == "owner" else {reason}
        reasons = sorted(set(s["pause_reasons"]) - drop)
        new = "RUNNING" if s["status"] == "PAUSED" and not reasons else s["status"]
        _move(session_id, s["status"], new, s["pause_reasons"], reasons, wall_ns=wall_ns, engine_ns=engine_ns)
        return reasons


def arm(session_id: str, *, wall_ns: int) -> None:
    """CREATED, RUNNING or PAUSED -> ARMED (waiting for the next day), reasons kept."""
    with _lock:
        s = _load(session_id)
        _move(session_id, s["status"], "ARMED", s["pause_reasons"], s["pause_reasons"],
              wall_ns=wall_ns, engine_ns=None)


def start_continuous(session_id: str, *, wall_ns: int, engine_ns: int) -> str:
    """ARMED -> RUNNING when the reason set is empty, else PAUSED. Returns the new state."""
    with _lock:
        s = _load(session_id)
        if s["status"] != "ARMED":
            raise LifecycleError(f"cannot start from {s['status']}; arm first")
        new = "PAUSED" if s["pause_reasons"] else "RUNNING"
        _move(session_id, "ARMED", new, s["pause_reasons"], s["pause_reasons"], wall_ns=wall_ns, engine_ns=engine_ns)
        return new


def stop(session_id: str, *, wall_ns: int) -> None:
    """Final. Called at finalisation of the day after a `stop` command; the `stopped` reason keeps the engine
    disabled. Stopping a stopped session does nothing."""
    with _lock:
        s = store.get_session(session_id)
        if s is None:
            raise LifecycleError(f"no such session: {session_id!r}")
        if s["status"] == "STOPPED":
            return
        reasons = sorted(set(s["pause_reasons"]) | {"stopped"})
        store.set_session_stopped_ns(session_id, wall_ns)
        _move(session_id, s["status"], "STOPPED", s["pause_reasons"], reasons, wall_ns=wall_ns, engine_ns=None)

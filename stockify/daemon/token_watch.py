"""Waiting for the daily Kite login (LCB 5.2; M5.7; E-03; D-65).

The Kite token dies at 06:00 IST, or earlier when Kite rejects it mid-day. This module never holds a token value:
it looks only at the token file's modification time (`token_mtime_ns` stats the file and never opens it). The 06:00
rule itself is `kite_client.is_authenticated`, which does read and parse the file in memory on every poll (every 30 s);
the value is never logged, written or returned. The watcher keeps its own state in `daemon_status.json` beside the fields `market_day.write_status` writes:

    waiting_for_login          True from the expiry (or from a start with no usable token) until a login is accepted
    since_ns                   when the wait began
    sessions_paused            the sessions the expiry paused (`token_expired` reason)
    rejected_token_mtime_ns    the token file's mtime when Kite rejected it; a login is a file newer than this
    last_frame_no / last_frame_recv_ns   the last frame recorded

A token that is "fresh" by the 06:00 IST rule but is the very file Kite just rejected is not a login: the wait goes on
until the file is rewritten. The API reads `waiting_for_login` for `/api/kite/status`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Callable

import kite_client
from stockify import paths

log = logging.getLogger("stockify.daemon.token_watch")
_FORBIDDEN = ("token", "secret", "key", "password", "bearer", "auth", "credential")
_ALLOWED = ("rejected_token_mtime_ns",)     # the mtime of the rejected token file: an int or None, checked below
_OPAQUE = re.compile(r"^[A-Za-z0-9_\-.=+/]{32,}$")     # one long run with no spaces: what a token or a secret looks like


def _check_value(name: str, value) -> None:  # noqa: ANN001
    if isinstance(value, str):
        if _OPAQUE.match(value):
            raise ValueError(f"status field {name!r} holds a long opaque string; the status file never holds a secret")
    elif isinstance(value, dict):
        for k, v in value.items():
            _check_value(f"{name}.{k}", v)
            _check_value(f"{name}.{k}", k)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _check_value(name, v)


def read_status() -> dict:
    """`daemon_status.json` as a dict; `{}` when it is missing or unreadable."""
    try:
        status = json.loads(paths.daemon_status_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return status if isinstance(status, dict) else {}


def write_status(**fields) -> dict:
    """Merge `fields` into `daemon_status.json` (written whole, then renamed into place) and return the result.
    A field whose name could hold a token or a secret is refused, and so is any value (at any depth) that is one
    long opaque run of characters; `rejected_token_mtime_ns` must be an int or None."""
    for name, value in fields.items():
        if name not in _ALLOWED and any(word in name.lower() for word in _FORBIDDEN):
            raise ValueError(f"status field {name!r} could hold a secret; the status file never does")
        if name == "rejected_token_mtime_ns":
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
                raise ValueError("rejected_token_mtime_ns must be an int (a file time) or None")
        _check_value(name, value)
    status = read_status()
    status.update(fields)
    target = paths.daemon_status_file()
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return status


def token_mtime_ns() -> int | None:
    """The token file's modification time in ns, or None. The file is only ever stat'ed, never opened."""
    try:
        return kite_client._token_file().stat().st_mtime_ns
    except OSError:
        return None


def login_ok(token_fresh: Callable[[], bool]) -> bool:
    """True when `token_fresh()` is true and the token file is not the one Kite rejected (it is newer than
    `rejected_token_mtime_ns`, when there is one)."""
    if not token_fresh():
        return False
    rejected = read_status().get("rejected_token_mtime_ns")
    if not isinstance(rejected, int):
        return True
    mtime = token_mtime_ns()
    return mtime is not None and mtime > rejected


def begin_wait(now_ns: int) -> None:
    """Say the daemon is waiting for a login (keeps the first `since_ns`)."""
    if not read_status().get("waiting_for_login"):
        write_status(waiting_for_login=True, since_ns=now_ns)
        from stockify.daemon import notify               # one message per wait: the status flag is the "still open"
        notify.post("login_needed", "[STOCKIFY] Kite login needed: the recorder is waiting",
                    "Kite rejected the login token, so the paper sessions are paused and nothing is recorded.\n\n"
                    "Log in at http://127.0.0.1:3000/portfolio (Login to Kite); the sessions resume by themselves."
                    "\n\n- STOCKIFY daemon\n", key=f"token_expired:{now_ns}")


def login_accepted() -> None:
    """A login newer than the rejected token was seen: the wait is over."""
    write_status(waiting_for_login=False, rejected_token_mtime_ns=None, sessions_paused=[])


def poll_s() -> float:
    """How often the wait for a login looks again: paper.json `token_poll_s` (15 s, D-65), the only place it is set."""
    from stockify.config import loader
    return float(loader.load_json("paper.json")["token_poll_s"])


def wait_for_login(clock, stop: threading.Event, *, poll_s: float | None = None,  # noqa: ANN001
                   token_fresh: Callable[[], bool], on_idle: Callable[[], None], until_ns: int | None) -> bool:
    """Wait until `login_ok(token_fresh)`, polling every `poll_s`; `on_idle` runs on every poll that finds none (the
    scheduler's catch-up jobs, the evening's work). True on a login; False when `stop` is set or `until_ns` passes."""
    if login_ok(token_fresh):
        return True
    begin_wait(clock.now_ns())
    log.warning("waiting for a Kite login newer than the last rejected token (log in at /portfolio)")
    every = globals()["poll_s"]() if poll_s is None else poll_s
    step = max(1, int(every * 1_000_000_000))
    while True:
        try:
            on_idle()
        except Exception:  # noqa: BLE001 — catch-up work never ends the wait
            log.exception("a job run while waiting for the login failed")
        now = clock.now_ns()
        if until_ns is not None and now >= until_ns:
            return login_ok(token_fresh)
        nxt = now + step if until_ns is None else min(now + step, until_ns)
        if not clock.sleep_until(nxt, stop):
            return False
        if login_ok(token_fresh):
            return True


__all__ = ["begin_wait", "login_accepted", "login_ok", "read_status", "token_mtime_ns", "poll_s", "wait_for_login",
           "write_status"]

"""The KiteTicker connection budget (M5.14; K-1, D-20; LCB 5.1).

Kite allows `kite_limits.json` → `ws_max_connections` (3) websocket connections per API key. Normal operation uses
two: the API's (UI prices, role `api`) and the daemon's (role `daemon`); one stays spare, and only the daemon may use it: any other role is refused once `limit - 1` tickets are live, so the recorder is never refused because of the API or a dev ticket. A process that opens a
live ticker first takes a ticket, `$LAB/ticker_connections/<role>-<pid>.json` = `{pid, created, role}`.
`register` refuses (BudgetExceeded) when the live tickets already number `limit - 1` or more (`limit` for the daemon). A ticket whose
process has gone (`proc.is_alive(pid, created)` is false, or the file is unreadable) is removed first, so a killed
process never holds the budget. Development runs use `--ticker replay:...`, which takes no ticket.
"""
from __future__ import annotations

import json
import logging
import msvcrt
import os
import time
from contextlib import contextmanager
from pathlib import Path

from stockify import paths, proc

log = logging.getLogger("stockify.daemon.conn_budget")
FOLDER = "ticker_connections"
_LOCK = ".lock"


class BudgetExceeded(RuntimeError):
    """Opening another live KiteTicker would leave no spare connection."""


def limit() -> int:
    from stockify.config import loader
    return int(loader.load_json("kite_limits.json")["ws_max_connections"])


def _folder() -> Path:
    folder = paths.lab_dir() / FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@contextmanager
def _locked(folder: Path):  # noqa: ANN202
    """One registration at a time across processes (a byte-range lock on `.lock`)."""
    fd = os.open(folder / _LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + 10.0
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
        try:
            yield
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)


def _live(path: Path) -> bool:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        return proc.is_alive(int(body["pid"]), int(body["created"]))
    except (OSError, ValueError, KeyError, TypeError):
        return False


class Ticket:
    """One live connection's claim on the budget; `release()` gives it back (idempotent)."""

    def __init__(self, path: Path, role: str):
        self.path, self.role = path, role
        self.released = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        try:
            self.path.unlink(missing_ok=True)
        except OSError as e:
            log.warning("could not remove ticker ticket %s: %s", self.path.name, e)


def register(role: str, *, limit: int) -> Ticket:
    """Take a ticket for one live KiteTicker of `role`, or raise BudgetExceeded."""
    if not paths.safe_segment(role, 32):
        raise ValueError(f"unsafe ticker role {role!r}")
    folder = _folder()
    pid = os.getpid()
    own = folder / f"{role}-{pid}.json"
    with _locked(folder):
        live = []
        for path in sorted(folder.glob("*.json")):
            if path == own:
                continue
            if _live(path):
                live.append(path.stem)
            else:
                log.info("removing the ticket of a ticker that is gone: %s", path.name)
                path.unlink(missing_ok=True)
        # The recorder has priority (differential review M4): every other role stops one short of the limit, so the
        # recorder's ticket always fits, on the spare when the API and a dev run hold two.
        room = limit if role == "daemon" else limit - 1
        if len(live) >= room:
            raise BudgetExceeded(f"{len(live)} live KiteTicker connection(s) already ({', '.join(live)}) of "
                                 f"{limit}: refusing a {role} connection so one stays spare (M5.14); "
                                 "use --ticker replay:... for development")
        if role == "daemon" and len(live) >= limit - 1:
            log.warning("the recorder takes the spare KiteTicker connection (%s live)", ", ".join(live))
        tmp = own.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pid": pid, "created": proc.creation_time(pid), "role": role}),
                       encoding="utf-8")
        tmp.replace(own)
    return Ticket(own, role)

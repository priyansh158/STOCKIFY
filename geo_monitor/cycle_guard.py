"""One monitor cycle at a time, across processes (D-150).

The scheduled background monitor and the "Check the news now" button of the web app both run `worker.run_once`;
two cycles at once would fetch and email the same stories twice. Each cycle holds an OS lock on one byte of
`geo_cycle.lock` in the state folder. Windows frees the lock when the holder's process ends, so a crash never leaves
a stale lock behind (no pid files to judge). The lock is per file handle, so two threads of one process exclude each
other too.
"""
from __future__ import annotations

import contextlib
import msvcrt
import os
from typing import Iterator

import stockify_paths

LOCK_NAME = "geo_cycle.lock"
_OFFSET = 1 << 20          # the locked byte lies beyond the file's end: the file itself stays readable


class CycleBusy(Exception):
    """Another cycle is running (in the scheduled monitor or in the app)."""


def _open() -> int:
    return os.open(str(stockify_paths.state_dir() / LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600)


def _try_lock(fd: int) -> bool:
    os.lseek(fd, _OFFSET, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    os.lseek(fd, _OFFSET, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def is_busy() -> bool:
    """Whether a cycle is running right now (a cheap look: lock and release at once)."""
    fd = _open()
    try:
        if _try_lock(fd):
            _unlock(fd)
            return False
        return True
    finally:
        os.close(fd)


@contextlib.contextmanager
def hold() -> Iterator[None]:
    """Hold the cycle lock for the block; raise CycleBusy when another cycle holds it."""
    fd = _open()
    try:
        if not _try_lock(fd):
            raise CycleBusy("a news check is already running")
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)

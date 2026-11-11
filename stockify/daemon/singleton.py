"""One daemon per lab (GK 5.1).

Two daemons recording one day would interleave their frames into the same landing file and journals. The daemon
takes an OS byte lock on `daemon.lock` in the lab and holds it for the life of the process. The OS drops the lock
when the process ends, however it ends (a crash, a kill, a power cut), so a dead holder never blocks the next daemon
and nothing has to be cleaned up. A second daemon fails at once, with a message that names the holder.

The locked byte lies far beyond the holder's pid text at the start of the file, so a refused daemon can still read
the pid (Windows refuses reads of a locked byte).
"""
from __future__ import annotations

import errno
import msvcrt
import os
from pathlib import Path

from stockify import paths

LOCK_OFFSET = 1 << 20           # the locked byte; the pid text sits at the start of the file


class AlreadyRunning(RuntimeError):
    """Another daemon holds this lab's lock."""


class LockUnavailable(RuntimeError):
    """The lock file itself cannot be opened or locked (permissions, a missing disk): not another daemon."""


class DaemonLock:
    def __init__(self, path: Path, fd: int) -> None:
        self.path = path
        self._fd: int | None = fd

    def release(self) -> None:
        """Give the lock back (the OS does it anyway at exit). Releasing twice is harmless."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            os.close(fd)

    def __enter__(self) -> "DaemonLock":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def _holder(path: Path) -> str:
    try:
        text = path.read_text(encoding="ascii", errors="replace").strip("\x00 \r\n")
    except OSError:
        return "another process"
    return f"process {text}" if text.isdigit() else "another process"


def acquire(path: Path | None = None) -> DaemonLock:
    """Take the lock or raise `AlreadyRunning` at once. The caller keeps the returned lock for its lifetime."""
    path = Path(path) if path is not None else paths.daemon_lock_file()
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_BINARY, 0o600)
    except OSError as e:
        raise LockUnavailable(f"cannot take the daemon lock {path}: {e}") from None
    try:
        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError as e:
        os.close(fd)
        if e.errno not in (errno.EACCES, errno.EDEADLK):
            raise LockUnavailable(f"cannot take the daemon lock {path}: {e}") from None
        raise AlreadyRunning(f"another STOCKIFY daemon is already running on this lab ({_holder(path)}, lock "
                             f"{path}); stop it first") from None
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()}".ljust(32).encode("ascii"))
    except OSError:
        pass                                  # the pid is only for the refusal message
    return DaemonLock(path, fd)

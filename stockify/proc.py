"""Process identity on Windows (D-26): is the process that wrote a pid file still that process?

A pid alone is not enough, because Windows reuses pids. A runner records its pid and its process creation time
(`runner.pid` in the run folder); another process accepts it as alive only if a process with that pid exists, has
not exited, and was created at that same moment. Uses kernel32 through ctypes (no extra dependency).
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path

PIDFILE = "runner.pid"

_SYNCHRONIZE = 0x00100000
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x102

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_k32.CloseHandle.argtypes = (wintypes.HANDLE,)
_k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
_k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
_k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
_k32.WaitForSingleObject.restype = wintypes.DWORD
_k32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)


def _open(pid: int, extra: int = 0):  # noqa: ANN202
    return _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE | extra, False, pid) or None


def _created(handle) -> int | None:  # noqa: ANN001
    times = [wintypes.FILETIME() for _ in range(4)]
    if not _k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
        return None
    return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime  # 100 ns since 1601


def creation_time(pid: int) -> int | None:
    """The process's creation time (FILETIME units), or None if no such process can be opened."""
    h = _open(pid)
    if h is None:
        return None
    try:
        return _created(h)
    finally:
        _k32.CloseHandle(h)


def is_alive(pid: int, created: int) -> bool:
    """Whether process `pid` is still running and is the one created at `created` (not a reused pid)."""
    h = _open(pid)
    if h is None:
        return False
    try:
        code = wintypes.DWORD()
        if not _k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != _STILL_ACTIVE:
            return False
        return _created(h) == created
    finally:
        _k32.CloseHandle(h)


def write_pidfile(folder: Path) -> None:
    """Record this process as the run's runner: `<pid> <creation time>`."""
    pid = os.getpid()
    (folder / PIDFILE).write_text(f"{pid} {creation_time(pid)}\n", encoding="utf-8")


def read_pidfile(folder: Path) -> tuple[int, int] | None:
    try:
        pid, created = (folder / PIDFILE).read_text(encoding="utf-8").split()
        return int(pid), int(created)
    except (OSError, ValueError):
        return None


class Adopted:
    """A live runner this process did not start (it survived an API restart), with the subprocess-like
    interface the test queue uses: wait, poll, kill. The exit code is unknown to a non-parent: -1."""

    def __init__(self, pid: int, created: int):
        self.pid, self.created = pid, created

    def wait(self) -> int:
        h = _open(self.pid)
        if h is None:
            return -1
        try:
            if _created(h) != self.created:
                return -1
            while _k32.WaitForSingleObject(h, 1000) == _WAIT_TIMEOUT:
                pass
            return -1
        finally:
            _k32.CloseHandle(h)

    def poll(self) -> int | None:
        return None if is_alive(self.pid, self.created) else -1

    def kill(self) -> None:
        if not is_alive(self.pid, self.created):
            return
        h = _open(self.pid, _PROCESS_TERMINATE)
        if h is not None:
            try:
                _k32.TerminateProcess(h, 1)
            finally:
                _k32.CloseHandle(h)

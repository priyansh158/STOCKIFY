"""Clocks for the daemon (invariants I-2, I-4).

Every daemon component takes a `Clock` instead of calling `time.*`:

* `WallClock`: live use. `now_ns` is UTC epoch ns, `mono_ns` a monotonic
  counter for intervals and latency diagnostics only.
* `ManualClock`: tests; time moves only when the test says so.
* `ReplayClock` (stockify/daemon/replay_ticker.py) follows recorded receive
  times, so a replayed day reproduces the original timeline.

This module and `stockify/timeutil.py` are the only places that may read
the wall clock.
"""
from __future__ import annotations

import threading
import time


class Clock:
    def now_ns(self) -> int:
        raise NotImplementedError

    def mono_ns(self) -> int:
        raise NotImplementedError

    def sleep_until(self, t_ns: int, stop: threading.Event | None = None) -> bool:
        """Sleep until wall time t_ns; return False if `stop` was set first."""
        raise NotImplementedError


class WallClock(Clock):
    def now_ns(self) -> int:
        return time.time_ns()

    def mono_ns(self) -> int:
        return time.monotonic_ns()

    def sleep_until(self, t_ns: int, stop: threading.Event | None = None) -> bool:
        while True:
            remaining = (t_ns - time.time_ns()) / 1e9
            if remaining <= 0:
                return True
            if stop is not None:
                if stop.wait(min(remaining, 1.0)):
                    return False
            else:
                time.sleep(min(remaining, 1.0))


class ManualClock(Clock):
    """A clock tests move by hand. Wall and monotonic time advance together."""

    def __init__(self, now_ns: int = 0, mono_ns: int = 0):
        self._now = int(now_ns)
        self._mono = int(mono_ns)
        self._lock = threading.Lock()

    def now_ns(self) -> int:
        with self._lock:
            return self._now

    def mono_ns(self) -> int:
        with self._lock:
            return self._mono

    def advance(self, ns: int) -> None:
        with self._lock:
            self._now += int(ns)
            self._mono += int(ns)

    def set(self, now_ns: int) -> None:
        with self._lock:
            self._mono += max(0, int(now_ns) - self._now)
            self._now = int(now_ns)

    def sleep_until(self, t_ns: int, stop: threading.Event | None = None) -> bool:
        if stop is not None and stop.is_set():
            return False
        self.set(max(self.now_ns(), t_ns))
        return True

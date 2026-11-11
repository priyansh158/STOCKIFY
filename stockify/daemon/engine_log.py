"""The paper daemon's view of the engine's log (GK 5.1, GK 5.2; carried from Task 7).

A disabled strategy (paused, or stopped) skips every hook, and the engine writes one INFO line per skip:
`<now_ns> INFO <hook> skipped: strategy disabled (<reasons>)`. Those lines are part of a run's `events_log.txt`
and stay as they are. A daemon day is long, though, so what reaches the Python log is limited three ways:

* the skips are throttled: the first, then one summary line per `every_s` of engine time with the counts by hook
  (a pause that ends, or a day that closes, reports what is pending first);
* every other engine line (a strategy's `ctx.log`, a rejection, a gap, a settlement) counts against a cap of
  `max_lines_per_min` per minute of engine time; the rest are counted and reported in one summary line;
* a line is cut to `max_line_chars` and its newlines are escaped, so a strategy cannot forge log lines or write
  a megabyte at once.

Time is the engine's own, read from the lines, so the same input gives the same log however fast the day is
replayed. The sink may be slow (a disk, a pipe): `LogPump` hands lines to a logging thread through a bounded queue
and counts what it had to drop, so the engine thread never waits for the log.
"""
from __future__ import annotations

import logging
import queue
import re
import threading
from typing import Callable

log = logging.getLogger("stockify.daemon.engine_log")

SEC = 1_000_000_000
MINUTE_NS = 60 * SEC
_LEVELS = {"INFO": logging.INFO, "WARN": logging.WARNING, "WARNING": logging.WARNING, "ERROR": logging.ERROR}
_LINE = re.compile(r"^(\d+) (\w+) (.*)$", re.DOTALL)
_SKIP = re.compile(r"^(\S+) skipped: strategy disabled \((.*)\)$")
Sink = Callable[[int, str], None]


def _direct(level: int, text: str) -> None:
    log.log(level, "%s", text)


class LogPump:
    """A logging thread behind a bounded queue. `__call__(level, text)` never blocks: a full queue drops the line
    and counts it. `close()` flushes what is queued, says how many were dropped, and stops the thread."""

    def __init__(self, sink: Sink = _direct, *, maxsize: int = 10_000) -> None:
        self._sink = sink
        self._queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self.dropped = 0
        self._thread = threading.Thread(target=self._run, name="engine-log", daemon=True)
        self._thread.start()

    def __call__(self, level: int, text: str) -> None:
        try:
            self._queue.put_nowait((level, text))
        except queue.Full:
            self.dropped += 1

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                self._sink(*item)
            except Exception:  # noqa: BLE001 — a log sink must never take anything down
                pass

    def close(self, timeout: float = 10.0) -> None:
        try:
            self._queue.put(None, timeout=timeout)
        except queue.Full:
            return
        self._thread.join(timeout)
        if self.dropped:
            try:
                self._sink(logging.WARNING, f"engine log: {self.dropped} lines dropped because the log sink was slow")
            except Exception:  # noqa: BLE001
                pass


class EngineLog:
    def __init__(self, session_id: str, *, every_s: int = 60, max_lines_per_min: int = 120,
                 max_line_chars: int = 500, sink: Sink | None = None) -> None:
        self.session_id = session_id
        self._every_ns = int(every_s) * SEC
        self._cap = int(max_lines_per_min)
        self._max_chars = int(max_line_chars)
        self._sink: Sink = sink or _direct
        self._reasons: str | None = None        # the reasons of the skips being counted; None = not skipping
        self._since = 0                         # engine time of the last line written for them
        self._counts: dict[str, int] = {}
        self._minute: int | None = None          # the minute of engine time the cap is counting in
        self._emitted = 0
        self._suppressed = 0
        self._now = 0

    # ── output: cap, cut, escape ─────────────────────────────────────────────
    def _clean(self, text: str) -> str:
        text = text.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")
        if len(text) > self._max_chars:
            text = f"{text[:self._max_chars]}...[truncated {len(text) - self._max_chars} chars]"
        return text

    def _write(self, level: int, text: str) -> None:
        self._sink(level, f"session {self.session_id}: {text}")

    def _emit(self, now: int, level: int, text: str, *, force: bool = False) -> None:
        minute = now // MINUTE_NS
        if minute != self._minute:
            self._report_suppressed()
            self._minute, self._emitted = minute, 0
        if not force and self._emitted >= self._cap:
            self._suppressed += 1
            return
        self._emitted += 1
        self._write(level, self._clean(text))

    def _report_suppressed(self) -> None:
        if self._suppressed:
            self._write(logging.INFO, f"{self._suppressed} engine log lines suppressed (cap {self._cap} per minute of "
                                    "engine time)")
            self._suppressed = 0

    # ── input ────────────────────────────────────────────────────────────────
    def feed(self, lines: list[str]) -> None:
        for line in lines:
            m = _LINE.match(line)
            if m is None:
                self._emit(self._now, logging.INFO, line)
                continue
            now, level, msg = int(m.group(1)), m.group(2), m.group(3)
            self._now = now
            skip = _SKIP.match(msg) if level == "INFO" else None
            if skip is not None:
                self._skip(now, skip.group(1), skip.group(2))
                continue
            if msg == "strategy enabled":
                self.flush()
                self._reasons = None
            self._emit(now, _LEVELS.get(level, logging.INFO), msg)

    def _skip(self, now: int, hook: str, reasons: str) -> None:
        if reasons != self._reasons:
            self._report()                                   # a different set (or the first skip): say so at once
            self._reasons, self._since = reasons, now
            self._emit(now, logging.INFO, f"{hook} skipped: strategy disabled ({reasons}); further skips are "
                                          "summarised once a minute", force=True)
            return
        self._counts[hook] = self._counts.get(hook, 0) + 1
        if now - self._since >= self._every_ns:
            self._report()
            self._since = now

    def _report(self) -> None:
        if not self._counts:
            return
        total = sum(self._counts.values())
        detail = ", ".join(f"{hook} {n}" for hook, n in sorted(self._counts.items()))
        self._emit(self._now, logging.INFO,
                   f"strategy still disabled ({self._reasons}): {total} hook calls skipped since the last report "
                   f"({detail})", force=True)
        self._counts = {}

    def flush(self) -> None:
        """Report what is pending (a pause that ended, a day that closed)."""
        self._report()
        self._report_suppressed()

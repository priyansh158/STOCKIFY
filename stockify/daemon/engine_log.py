"""The paper daemon's view of the engine's log (GK 5.1; carried from Task 7).

A disabled strategy (paused, or stopped) skips every hook, and the engine writes one INFO line per skip:
`<now_ns> INFO <hook> skipped: strategy disabled (<reasons>)`. Those lines are part of a run's `events_log.txt`
and stay as they are. A daemon day is long, though, so what reaches the Python log is throttled: the first skip,
then one summary line per `every_s` of engine time with the counts by hook. Time is the engine's own, read from the
lines, so the same input gives the same log however fast the day is replayed. A pause that ends (`strategy enabled`)
or a day that closes reports what is pending first. Every other engine line is logged as it is, at its level.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("stockify.daemon.engine_log")

SEC = 1_000_000_000
_LEVELS = {"INFO": logging.INFO, "WARN": logging.WARNING, "WARNING": logging.WARNING, "ERROR": logging.ERROR}
_LINE = re.compile(r"^(\d+) (\w+) (.*)$", re.DOTALL)
_SKIP = re.compile(r"^(\S+) skipped: strategy disabled \((.*)\)$")


class EngineLog:
    def __init__(self, session_id: str, *, every_s: int = 60) -> None:
        self.session_id = session_id
        self._every_ns = int(every_s) * SEC
        self._reasons: str | None = None        # the reasons of the skips being counted; None = not skipping
        self._since = 0                         # engine time of the last line written for them
        self._counts: dict[str, int] = {}

    def feed(self, lines: list[str]) -> None:
        for line in lines:
            m = _LINE.match(line)
            if m is None:
                log.info("session %s: %s", self.session_id, line)
                continue
            now, level, msg = int(m.group(1)), m.group(2), m.group(3)
            skip = _SKIP.match(msg) if level == "INFO" else None
            if skip is not None:
                self._skip(now, skip.group(1), skip.group(2))
                continue
            if msg == "strategy enabled":
                self.flush()
                self._reasons = None
            log.log(_LEVELS.get(level, logging.INFO), "session %s: %s", self.session_id, msg)

    def _skip(self, now: int, hook: str, reasons: str) -> None:
        if reasons != self._reasons:
            self.flush()                                     # a different set (or the first skip): say so at once
            self._reasons, self._since = reasons, now
            log.info("session %s: %s skipped: strategy disabled (%s); further skips are summarised once a minute",
                     self.session_id, hook, reasons)
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
        log.info("session %s: strategy still disabled (%s): %d hook calls skipped since the last report (%s)",
                 self.session_id, self._reasons, total, detail)
        self._counts = {}

    def flush(self) -> None:
        """Report what is pending (a pause that ended, a day that closed)."""
        self._report()

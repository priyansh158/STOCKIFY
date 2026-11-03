"""Market daemon (M0 stub).

M1.5 turns this into the recorder; M5 adds paper sessions. Until then it only
proves the process supervision in `run.ps1 -Daemon` works: it logs "idle"
once a minute and exits cleanly on Ctrl+C.
"""
from __future__ import annotations

import time

from simlab import log

IDLE_INTERVAL_SEC = 60

_log = log.get("daemon")


def run_forever(*, once: bool = False) -> None:
    _log.info("SimLab daemon started (stub: no recorder or sessions yet)")
    try:
        while True:
            _log.info("idle")
            if once:
                return
            time.sleep(IDLE_INTERVAL_SEC)
    except KeyboardInterrupt:
        _log.info("SimLab daemon stopped")

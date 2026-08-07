"""Always-on geopolitical news monitor for STOCKIFY."""

__all__ = ["run_forever", "run_once"]


def __getattr__(name: str):
    if name in ("run_forever", "run_once"):
        from geo_monitor.worker import run_forever, run_once

        return run_forever if name == "run_forever" else run_once
    raise AttributeError(name)

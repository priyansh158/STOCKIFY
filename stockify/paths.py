"""Every on-disk STOCKIFY location (00_overview.md §3).

All paths hang off `stockify_paths.state_dir()`, which the test harness
sandboxes. No other STOCKIFY module builds a path by hand. Directories are
created lazily, on first use.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import stockify_paths


def _dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def lab_dir() -> Path:
    """`$LAB` in the plan docs."""
    return _dir(stockify_paths.state_dir() / "lab")


def refdata_db() -> Path:
    return lab_dir() / "refdata.sqlite"


def bars_db() -> Path:
    return lab_dir() / "bars.sqlite"


def lab_db() -> Path:
    return lab_dir() / "lab.sqlite"


def ticks_landing_dir() -> Path:
    return _dir(lab_dir() / "ticks" / "landing")


def ticks_landing_file(day: date) -> Path:
    return ticks_landing_dir() / f"ticks_{day:%Y%m%d}.sqlite"


def ticks_replay_file(day: date) -> Path:
    """Where a recorder fed by the replay ticker writes: never the real landing file."""
    return _dir(lab_dir() / "ticks" / "replay") / f"ticks_{day:%Y%m%d}.sqlite"


def ticks_archive_dir(day: date | None = None) -> Path:
    base = lab_dir() / "ticks" / "archive"
    return _dir(base / f"date={day:%Y-%m-%d}" if day else base)


def nse_raw_dir(dataset: str | None = None) -> Path:
    base = lab_dir() / "nse" / "raw"
    return _dir(base / dataset if dataset else base)


def packs_dir() -> Path:
    return _dir(lab_dir() / "packs")


def runs_dir() -> Path:
    return _dir(lab_dir() / "runs")


def run_dir(run_id: str) -> Path:
    return _dir(runs_dir() / run_id)


def journals_dir(session_id: str | None = None) -> Path:
    base = lab_dir() / "journals"
    return _dir(base / session_id if session_id else base)


def logs_dir() -> Path:
    return _dir(stockify_paths.logs_dir())

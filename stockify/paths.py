"""Every on-disk STOCKIFY location (00_overview.md §3).

All paths hang off `stockify_paths.state_dir()`, which the test harness
sandboxes. No other STOCKIFY module builds a path by hand. Directories are
created lazily, on first use.
"""
from __future__ import annotations

import re
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


MARGIN_KINDS = ("span", "elm", "manifest")


def nse_margin_dir(kind: str) -> Path:
    """$LAB/nse/margin/<kind>: NSE's SPAN zips, ELM gzips and the per-day manifests."""
    if kind not in MARGIN_KINDS:
        raise ValueError(f"unknown margin archive kind {kind!r}; known: {', '.join(MARGIN_KINDS)}")
    return _dir(lab_dir() / "nse" / "margin" / kind)


def span_cache_dir() -> Path:
    """$LAB/cache/span: the bundle's parsed SPAN entries, one JSON per (file, wanted contracts) (CAM 4.4). Only a
    cache: any file here may be deleted, and a bad one is rebuilt."""
    return _dir(lab_dir() / "cache" / "span")


def packs_dir() -> Path:
    return _dir(lab_dir() / "packs")


def runs_dir() -> Path:
    return _dir(lab_dir() / "runs")


# A name that becomes a folder: letters, digits, '.', '_' and '-', starting with a letter or digit, not ending with
# '.', and not a Windows device name (CON, NUL, COM1 …, also with an extension: "nul.x" is the device too).
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_DEVICES = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                      *(f"LPT{i}" for i in range(1, 10))})


def safe_segment(text: object, max_len: int) -> bool:
    """Whether `text` is safe as one path segment of at most `max_len` characters (run names and ids)."""
    return (isinstance(text, str) and 0 < len(text) <= max_len and _SEGMENT.fullmatch(text) is not None
            and not text.endswith(".") and text.split(".")[0].upper() not in _DEVICES)


def run_path(run_id: str) -> Path:
    """The folder of a run, without creating it. Every run folder goes through here: the id must be one safe
    segment and resolve to a direct child of runs_dir(), else ValueError (no `..`, no drive, no separator)."""
    if not safe_segment(run_id, 128):
        raise ValueError(f"not a valid run id: {run_id!r}")
    base = runs_dir()
    folder = base / run_id
    if folder.resolve().parent != base.resolve():
        raise ValueError(f"not a valid run id: {run_id!r}")
    return folder


def run_dir(run_id: str) -> Path:
    return _dir(run_path(run_id))


def journals_dir(session_id: str | None = None) -> Path:
    base = lab_dir() / "journals"
    return _dir(base / session_id if session_id else base)


def logs_dir() -> Path:
    return _dir(stockify_paths.logs_dir())

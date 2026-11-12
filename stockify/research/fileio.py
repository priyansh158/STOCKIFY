"""Atomic file writes for research artifacts: a reader sees the whole old file or the whole new one."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """Write `text` (UTF-8, newlines untouched) to a temporary file in the same folder, then os.replace it over
    `path`. On any error the temporary file is removed and the old file stays."""
    path = Path(path)
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent, prefix=path.name + ".",
                                         suffix=".tmp", delete=False) as f:
            tmp = Path(f.name)
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        raise

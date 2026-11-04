"""Gateway to the compiled C++ engine module `stockify._engine`.

The module is built by `run.ps1 -BuildEngine` and copied into `stockify/`.
Importing it is deferred to first use so that the API, the daemon and the
test suite still import when the engine has not been built yet.
"""
from __future__ import annotations

import sys

_REBUILD = "rebuild with `.\\run.ps1 -BuildEngine`"


class EngineUnavailable(RuntimeError):
    """The engine module is missing or was built for another Python."""


_module = None


def load():
    """Import `stockify._engine`, checking it matches this interpreter."""
    global _module
    if _module is not None:
        return _module
    try:
        from stockify import _engine  # type: ignore[attr-defined]
    except ImportError as e:
        raise EngineUnavailable(f"stockify._engine is not built ({e}); {_REBUILD}") from e
    built_for = _engine.build_info().get("python_version", "")
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    if built_for != running:
        raise EngineUnavailable(
            f"stockify._engine was built for Python {built_for or '?'}, "
            f"this is Python {running}; {_REBUILD}"
        )
    _module = _engine
    return _module


def build_info() -> dict:
    return dict(load().build_info())


def available() -> bool:
    try:
        load()
        return True
    except EngineUnavailable:
        return False

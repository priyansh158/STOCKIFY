"""Test harness support for STOCKIFY.

Import this module FIRST in any test module that touches `kite_client`,
`tick_buffer`, `analysis_history` or `portfolio_sync`.

Two jobs:

1. **Sandbox the on-disk state.** `kite_client._cache_dir()` resolves to
   ``$LOCALAPPDATA/STOCKIFY/kite`` (falling back to ``~/.cache/...``). Merely
   importing this module repoints ``LOCALAPPDATA`` at a throwaway temp dir, so
   a test run can never read, overwrite or delete the developer's real Kite
   token, instruments dump, tick ringbuffer or analysis history.

2. **Stub unimportable optional dependencies.** `anthropic` is a hard top-level
   import in `technical_analyst` / `stream_claude`, so those modules cannot be
   imported at all without it. We install a minimal stub *only when the real
   package is absent* — if the developer has the venv installed, the real one
   wins and nothing is faked.

   `yfinance` and `kiteconnect` are deliberately NOT stubbed: the production
   code already guards them with try/except and flips an AVAILABLE flag, and
   leaving them absent is what keeps the suite offline by construction.

Nothing in this package writes to, patches, or otherwise mutates any file
under the repo — the whole harness is process-local.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

# ── Repo root on sys.path ────────────────────────────────────────────────────
# Lets `python -m unittest discover tests` work from any cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ── 1. Sandbox the cache dir ─────────────────────────────────────────────────
#
# Every on-disk store (Kite token, instruments dump, tick ringbuffer, analysis
# history, geo alerts) resolves its path through `kite_client._cache_dir()`,
# which reads $LOCALAPPDATA.
#
# Setting that env var is NOT sufficient on its own: a test using
# `patch.dict(os.environ, {...}, clear=True)` wipes it, and the very next DB
# call silently lands in the developer's real ~/.cache/STOCKIFY/kite. That
# happened once and it is exactly the failure this harness exists to prevent.
#
# So the sandbox is enforced two ways, and the second one is authoritative:
#   1. LOCALAPPDATA is set   — for any code reading the env var directly.
#   2. `kite_client._cache_dir` is REPLACED with a function returning the
#      sandbox path — immune to anything a test does to os.environ.

SANDBOX_ROOT = Path(tempfile.mkdtemp(prefix="stockify-tests-"))

# What the real cache dir would have been — used only for error messages.
REAL_LOCALAPPDATA = os.environ.get("LOCALAPPDATA")

os.environ["LOCALAPPDATA"] = str(SANDBOX_ROOT)

# The directory `_cache_dir()` currently resolves to. Rebound by fresh_cache_dir().
_ACTIVE_CACHE_DIR = SANDBOX_ROOT / "STOCKIFY" / "kite"


def _sandboxed_cache_dir() -> Path:
    """Drop-in replacement for kite_client._cache_dir(). Env-proof."""
    _ACTIVE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _ACTIVE_CACHE_DIR


def _install_cache_dir_guard() -> None:
    """Replace kite_client._cache_dir so no env manipulation can escape."""
    import kite_client

    if getattr(kite_client._cache_dir, "__stockify_sandboxed__", False):
        return
    _sandboxed_cache_dir.__stockify_sandboxed__ = True  # type: ignore[attr-defined]
    kite_client._real_cache_dir = kite_client._cache_dir  # keep the original
    kite_client._cache_dir = _sandboxed_cache_dir


def assert_sandboxed(path) -> None:
    """Fail loudly if `path` is not inside the throwaway sandbox.

    Called from the setUp *and tearDown* of every DB-touching test — tearDown
    matters because a test can clear the environment mid-run.
    """
    resolved = Path(path).resolve()
    root = SANDBOX_ROOT.resolve()
    if root not in resolved.parents and resolved != root:
        raise AssertionError(
            "REFUSING TO RUN: test DB path escaped the sandbox.\n"
            f"  path    = {resolved}\n"
            f"  sandbox = {root}\n"
            "Import `tests.support` before importing kite_client / tick_buffer / "
            "analysis_history, and use tests.support.isolated_env() instead of "
            "patch.dict(os.environ, clear=True)."
        )


def fresh_cache_dir(name: str) -> Path:
    """Rebind the cache dir to a brand-new empty directory.

    Returns the STOCKIFY/kite path the modules will use.
    """
    global _ACTIVE_CACHE_DIR
    d = Path(tempfile.mkdtemp(prefix=f"stockify-{name}-", dir=str(SANDBOX_ROOT)))
    os.environ["LOCALAPPDATA"] = str(d)
    _ACTIVE_CACHE_DIR = d / "STOCKIFY" / "kite"
    _install_cache_dir_guard()
    return _ACTIVE_CACHE_DIR


def isolated_env(overrides=None, clear: bool = True):
    """`patch.dict(os.environ, ...)` that never drops the sandbox pointer.

    Use this instead of `patch.dict(os.environ, {...}, clear=True)` in any test
    whose code path can reach the on-disk stores.
    """
    from unittest.mock import patch as _patch

    env = dict(overrides or {})
    env.setdefault("LOCALAPPDATA", os.environ.get("LOCALAPPDATA", str(SANDBOX_ROOT)))
    return _patch.dict(os.environ, env, clear=clear)


# ── 2. Optional-dependency stubs ─────────────────────────────────────────────

STUBBED: list = []


def _have(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def _build_anthropic_stub() -> types.ModuleType:
    """Minimal `anthropic` surface: enough to import, never enough to call out.

    Any attempt to actually open a stream raises, so a test that accidentally
    reaches the network fails fast and loudly instead of hanging.
    """
    m = types.ModuleType("anthropic")

    class _NoNetwork(RuntimeError):
        pass

    class _Messages:
        def stream(self, *a, **kw):
            raise _NoNetwork(
                "stub anthropic: tests must not call the Claude API. "
                "Patch the client or the stream function instead."
            )

        def create(self, *a, **kw):
            raise _NoNetwork("stub anthropic: tests must not call the Claude API.")

    class Anthropic:
        def __init__(self, *a, **kw):
            self.api_key = kw.get("api_key", "")
            self.messages = _Messages()

    class AsyncAnthropic(Anthropic):
        pass

    class AnthropicError(Exception):
        pass

    class APIError(AnthropicError):
        pass

    class APIStatusError(APIError):
        pass

    class RateLimitError(APIStatusError):
        pass

    class APIConnectionError(APIError):
        pass

    m.Anthropic = Anthropic
    m.AsyncAnthropic = AsyncAnthropic
    m.AnthropicError = AnthropicError
    m.APIError = APIError
    m.APIStatusError = APIStatusError
    m.RateLimitError = RateLimitError
    m.APIConnectionError = APIConnectionError
    m.NoNetwork = _NoNetwork
    m.__stockify_test_stub__ = True
    return m


if not _have("anthropic"):
    sys.modules["anthropic"] = _build_anthropic_stub()
    STUBBED.append("anthropic")


# Install the env-proof cache-dir guard as soon as the harness loads, so even a
# test module that never calls fresh_cache_dir() is protected.
_install_cache_dir_guard()


HAVE_PYDANTIC = _have("pydantic")
HAVE_FASTAPI = _have("fastapi")
HAVE_YFINANCE = _have("yfinance")
HAVE_KITECONNECT = _have("kiteconnect")

requires_pydantic = unittest.skipUnless(HAVE_PYDANTIC, "pydantic not installed")
requires_fastapi = unittest.skipUnless(HAVE_FASTAPI, "fastapi not installed")


# ── 3. SQLite singleton helpers ──────────────────────────────────────────────

def reset_sqlite_singleton(module) -> None:
    """Close and clear a module's cached `_conn`.

    `tick_buffer`, `analysis_history` and friends memoise one process-wide
    connection in a module global. Tests need a clean DB per case, so we close
    the old handle and nil the global; the next `_connect()` reopens against
    whatever `LOCALAPPDATA` currently points at.
    """
    conn = getattr(module, "_conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    module._conn = None


class SandboxedDBTest(unittest.TestCase):
    """Base class for tests against the SQLite-backed modules.

    Subclasses set `db_modules` to the modules whose `_conn` singleton must be
    reset. Each test gets a pristine, sandboxed cache dir.
    """

    db_modules: tuple = ()
    # Subclasses may override to assert a specific file lands in the sandbox.
    db_path_fn = None

    def setUp(self):
        super().setUp()
        self._cache_dir = fresh_cache_dir(type(self).__name__)
        for mod in self.db_modules:
            reset_sqlite_singleton(mod)
        for mod in self.db_modules:
            path_fn = getattr(mod, "_db_path", None)
            if path_fn is not None:
                assert_sandboxed(path_fn())

    def tearDown(self):
        # Re-check AFTER the test body. A test that cleared os.environ mid-run
        # would otherwise have written to real user state without any signal.
        try:
            for mod in self.db_modules:
                path_fn = getattr(mod, "_db_path", None)
                if path_fn is not None:
                    assert_sandboxed(path_fn())
        finally:
            for mod in self.db_modules:
                reset_sqlite_singleton(mod)
        super().tearDown()


def cleanup_sandbox() -> None:
    """Remove the whole sandbox tree. Wired to atexit by the runner."""
    shutil.rmtree(SANDBOX_ROOT, ignore_errors=True)


__all__ = [
    "SANDBOX_ROOT",
    "SandboxedDBTest",
    "STUBBED",
    "assert_sandboxed",
    "cleanup_sandbox",
    "fresh_cache_dir",
    "isolated_env",
    "reset_sqlite_singleton",
    "requires_fastapi",
    "requires_pydantic",
    "HAVE_FASTAPI",
    "HAVE_KITECONNECT",
    "HAVE_PYDANTIC",
    "HAVE_YFINANCE",
]

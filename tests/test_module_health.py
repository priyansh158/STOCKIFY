"""Import smoke test across every module in the repo.

This is the cheapest, broadest regression net for a repo under constant
pushes: a syntax error, a bad import, a renamed symbol or a module-level
crash shows up here within milliseconds of the commit, regardless of whether
anyone wrote a unit test for that file.

It distinguishes three outcomes deliberately:

  * **imports cleanly**            → pass
  * **missing third-party dep**    → skipped, reported by name (not a defect;
                                     the venv just isn't installed here)
  * **anything else**              → failure, with the traceback

Modules are imported into the sandboxed environment, so a module that opens
its SQLite store at import time writes to a temp dir, not to real user state.
"""
from __future__ import annotations

import importlib
import sys
import traceback
import unittest
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports

REPO_ROOT = Path(__file__).resolve().parent.parent

# Third-party packages the project declares in requirements.txt. A module that
# fails to import *only* because one of these is absent is reported as skipped.
OPTIONAL_DEPS = {
    "fastapi", "pydantic", "uvicorn", "starlette",
    "yfinance", "pandas", "numpy", "pyarrow", "yaml", "requests",
    "dotenv", "python_dotenv", "kiteconnect", "multipart", "google",
    "google_genai", "genai",
}

# Directories that are not importable Python packages.
SKIP_DIRS = {"web", "tests", "docs", "engine", ".git", "node_modules", "__pycache__",
             ".next", ".venv", "venv"}


def discover_modules() -> list:
    """Every importable module path in the repo, as dotted names."""
    modules = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts):
            continue
        if rel.name.startswith("."):
            continue
        parts = list(rel.parts)
        if parts[-1] == "__init__.py":
            parts = parts[:-1]
            if not parts:
                continue
        else:
            parts[-1] = parts[-1][:-3]
        modules.append(".".join(parts))
    return modules


def missing_dependency(exc: BaseException):
    """Return the third-party package name if `exc` is just an absent dep."""
    if not isinstance(exc, ImportError):
        return None
    name = (getattr(exc, "name", "") or "").split(".")[0]
    if name in OPTIONAL_DEPS:
        return name
    # ImportError without .name — fall back to matching the message.
    message = str(exc)
    for dep in OPTIONAL_DEPS:
        if dep in message:
            return dep
    return None


class ModuleImportTests(unittest.TestCase):
    """One subtest per module so a single broken file names itself."""

    def test_every_module_imports(self):
        modules = discover_modules()
        self.assertGreater(len(modules), 5, "module discovery found almost nothing")

        skipped = {}
        for name in modules:
            with self.subTest(module=name):
                try:
                    importlib.import_module(name)
                except BaseException as exc:  # noqa: BLE001 — reporting, not handling
                    dep = missing_dependency(exc)
                    if dep:
                        skipped[name] = dep
                        continue
                    if isinstance(exc, TypeError) and "unsupported operand type" in str(exc):
                        # PEP 604 (`X | None`) evaluated at runtime needs 3.10+.
                        # Only excuse it on an older interpreter.
                        if sys.version_info < (3, 10):
                            skipped[name] = "python>=3.10"
                            continue
                    self.fail(
                        f"{name} failed to import:\n"
                        + "".join(traceback.format_exception(type(exc), exc,
                                                             exc.__traceback__))
                    )
        if skipped:
            print(f"\n  [module_health] {len(skipped)} module(s) skipped: "
                  + ", ".join(f"{m} ({why})" for m, why in sorted(skipped.items())))


class PublicApiTests(unittest.TestCase):
    """Symbols other modules import by name. A rename here breaks callers at
    import time, which is exactly what these assertions catch."""

    def assert_has(self, module_name: str, *attrs: str):
        mod = importlib.import_module(module_name)
        for attr in attrs:
            with self.subTest(module=module_name, attr=attr):
                self.assertTrue(hasattr(mod, attr), f"{module_name}.{attr} is missing")

    def test_india_utils_surface(self):
        self.assert_has("india_utils", "strip_exchange_suffix", "to_yfinance_ticker",
                        "to_kite_symbol",
                        "format_inr_compact", "format_inr_full", "format_price")

    def test_kite_client_surface(self):
        self.assert_has("kite_client", "_cache_dir", "is_enabled", "is_authenticated",
                        "login_url", "exchange_request_token", "save_token",
                        "load_cached_token", "clear_token", "token_is_fresh",
                        "get_client", "resolve_symbol", "get_instrument_token",
                        "get_instrument_token_for_index", "refresh_instruments",
                        "ltp", "quote", "historical", "holdings", "positions",
                        "status_snapshot", "KiteAuthError", "KiteNotAuthenticated",
                        "KiteTokenExpired")

    def test_stockify_paths_surface(self):
        self.assert_has("stockify_paths", "env_file", "local_appdata", "app_dir",
                        "state_dir", "logs_dir", "loopback_origin")

    def test_tick_buffer_surface(self):
        self.assert_has("tick_buffer", "init_db", "write_ticks", "trim_old",
                        "checkpoint", "latest_tick", "latest_ticks_bulk",
                        "add_subscription", "remove_subscription",
                        "remove_subscriptions_of_kind", "subscribed_tokens",
                        "total_rows")

    def test_portfolio_sync_surface(self):
        self.assert_has("portfolio_sync", "Holding", "PortfolioSnapshot",
                        "fetch_snapshot", "cached_snapshot", "invalidate_cache")

    def test_geo_alerts_surface(self):
        self.assert_has("geo_alerts", "init_db", "mark_seen", "is_seen",
                        "save_alert", "mark_emailed", "acknowledge",
                        "list_alerts", "get_alert", "GeoAlert")

    def test_simlab_surface(self):
        self.assert_has("simlab.paths", "simlab_dir", "runs_dir", "packs_dir")
        self.assert_has("simlab.store", "init_db", "_db_path")
        self.assert_has("simlab.numerics", "to_ppb", "round_half_up_div",
                        "floor_div", "ceil_div", "format_float", "dumps_canonical")
        self.assert_has("simlab.engine", "build_info", "EngineUnavailable")


class SandboxIntegrityTests(unittest.TestCase):
    """The tripwire that keeps the suite from touching real user state."""

    def test_cache_dir_is_redirected(self):
        import kite_client
        support.assert_sandboxed(kite_client._cache_dir())

    def test_every_sqlite_store_resolves_inside_the_sandbox(self):
        import simlab.store
        import geo_alerts
        import tick_buffer
        for module in (tick_buffer, simlab.store, geo_alerts):
            with self.subTest(module=module.__name__):
                support.assert_sandboxed(module._db_path())

    def test_shared_state_and_log_dirs_are_sandboxed(self):
        import stockify_paths

        support.assert_sandboxed(stockify_paths.state_dir())
        support.assert_sandboxed(stockify_paths.logs_dir())

    def test_clearing_the_environment_cannot_escape_the_sandbox(self):
        # Regression: a test using patch.dict(os.environ, clear=True) wiped
        # LOCALAPPDATA. Shared path functions are replaced outright, so the
        # sandbox no longer depends on the environment at all.
        import os
        from unittest.mock import patch

        import simlab.store
        import geo_alerts
        import kite_client
        import tick_buffer

        with patch.dict(os.environ, {}, clear=True):
            support.assert_sandboxed(kite_client._cache_dir())
            for module in (tick_buffer, simlab.store, geo_alerts):
                with self.subTest(module=module.__name__):
                    support.assert_sandboxed(module._db_path())

    def test_isolated_env_preserves_the_sandbox_pointer(self):
        import os

        import kite_client

        with support.isolated_env({"SOME_KEY": "v"}):
            self.assertEqual(os.environ.get("SOME_KEY"), "v")
            support.assert_sandboxed(kite_client._cache_dir())

    def test_assert_sandboxed_rejects_a_path_outside_the_sandbox(self):
        # The tripwire must actually trip; a silently-passing guard is worse
        # than none.
        with self.assertRaises(AssertionError):
            support.assert_sandboxed(Path.home() / ".cache" / "STOCKIFY" / "kite")

    def test_missing_localappdata_fails_safely(self):
        import os
        from unittest.mock import patch

        import stockify_paths

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "LOCALAPPDATA"):
                stockify_paths.local_appdata()

    def test_windows_paths_with_spaces_are_supported(self):
        import os
        from unittest.mock import patch

        import stockify_paths

        base = support.SANDBOX_ROOT / "Local App Data"
        with patch.dict(os.environ, {"LOCALAPPDATA": str(base)}, clear=False):
            path = stockify_paths.app_dir()
        support.assert_sandboxed(path)
        self.assertIn("Local App Data", str(path))

    def test_loopback_origin_accepts_local_ports_and_rejects_remote_hosts(self):
        import os
        from unittest.mock import patch

        import stockify_paths

        with patch.dict(
            os.environ, {"TEST_ORIGIN": "http://localhost:4321/"}, clear=False
        ):
            self.assertEqual(
                stockify_paths.loopback_origin("TEST_ORIGIN", "http://127.0.0.1:1"),
                "http://localhost:4321",
            )
        with patch.dict(
            os.environ, {"TEST_ORIGIN": "https://example.com"}, clear=False
        ):
            with self.assertRaisesRegex(RuntimeError, "loopback"):
                stockify_paths.loopback_origin(
                    "TEST_ORIGIN", "http://127.0.0.1:1"
                )

    def test_kiteconnect_is_not_stubbed(self):
        # Leaving it absent is what keeps the suite offline by construction.
        self.assertNotIn("kiteconnect", support.STUBBED)

    def test_yfinance_is_not_stubbed(self):
        self.assertNotIn("yfinance", support.STUBBED)


if __name__ == "__main__":
    unittest.main()

"""The search fallback loads yfinance only when it is used (LCB 5.11).

yfinance pulls in a large tree of packages; the API must not load it at start-up, only inside the one
function that falls back to it. Nothing here reaches the network: the fallback is given a fake module.
"""
from __future__ import annotations

import os
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import tests.support as support  # noqa: F401  — must precede project imports

REPO = Path(__file__).resolve().parents[2]

# What the child does: load the harness (which itself probes yfinance), forget yfinance, import the app,
# and say whether the app's import brought yfinance back.
CHILD = """
import sys
import tests.support
def loaded():
    return sorted(m for m in sys.modules if m == "yfinance" or m.startswith("yfinance."))
for name in loaded():
    del sys.modules[name]
assert not loaded()
import {module}
print("LOADED", loaded())
"""


def child(module: str) -> str:
    env = {**os.environ, "LOCALAPPDATA": str(support.SANDBOX_ROOT)}
    done = subprocess.run([sys.executable, "-c", CHILD.format(module=module)], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        raise AssertionError(f"the child failed:\n{done.stderr[-2000:]}")
    return done.stdout


@support.requires_fastapi
class ImportTests(unittest.TestCase):
    def test_importing_the_api_does_not_import_yfinance(self):
        self.assertIn("LOADED []", child("server.main"))

    def test_importing_the_search_router_does_not_import_yfinance(self):
        self.assertIn("LOADED []", child("server.routers.search"))


def fake_yfinance(quotes=(), error: Exception | None = None):  # noqa: ANN201
    calls = []

    class Search:
        def __init__(self, query, max_results=None):  # noqa: ANN001
            calls.append((query, max_results))
            if error is not None:
                raise error
            self.quotes = list(quotes)

    module = types.ModuleType("yfinance")
    module.Search = Search
    return module, calls


QUOTES = [
    {"quoteType": "EQUITY", "exchange": "NSI", "symbol": "RELIANCE.NS", "longname": "Reliance Industries"},
    {"quoteType": "EQUITY", "exchange": "BSE", "symbol": "RELIANCE.BO", "shortname": "RELIANCE"},
    {"quoteType": "EQUITY", "exchange": "NMS", "symbol": "RELI", "longname": "not an Indian listing"},
    {"quoteType": "ETF", "exchange": "NSI", "symbol": "NIFTYBEES.NS"},
]


@support.requires_fastapi
class FallbackTests(unittest.TestCase):
    def setUp(self):
        from server.routers import search
        self.search = search
        # The module keeps no yfinance object of its own: the import happens in the function.
        self.assertFalse(hasattr(search, "yf"), "server.routers.search imports yfinance at module level")

    def hits(self, module, available=True):  # noqa: ANN001, ANN201
        with patch.dict(sys.modules, {"yfinance": module}), \
                patch.object(self.search, "_YFINANCE_AVAILABLE", available):
            return self.search._yfinance_hits("reliance", 7)

    def test_the_fallback_imports_yfinance_when_it_runs(self):
        module, calls = fake_yfinance(QUOTES)
        hits = self.hits(module)
        self.assertEqual(calls, [("reliance", 7)])
        self.assertEqual([(h.ticker, h.exchange, h.name, h.source, h.instrument_token) for h in hits],
                         [("RELIANCE", "NSE", "Reliance Industries", "yfinance", None),
                          ("RELIANCE", "BSE", "RELIANCE", "yfinance", None)])

    def test_the_flag_still_switches_the_fallback_off(self):
        module, calls = fake_yfinance(QUOTES)
        self.assertEqual(self.hits(module, available=False), [])
        self.assertEqual(calls, [], "yfinance was used although the flag says it is absent")

    def test_the_flag_says_whether_yfinance_is_installed(self):
        self.assertIs(self.search._YFINANCE_AVAILABLE, support.HAVE_YFINANCE)

    def test_a_missing_yfinance_gives_no_hits_and_no_error(self):
        # None in sys.modules makes `import yfinance` raise ImportError, as an uninstalled package does.
        self.assertEqual(self.hits(None), [])

    def test_a_failing_search_gives_no_hits(self):
        module, calls = fake_yfinance(error=RuntimeError("no network in tests"))
        self.assertEqual(self.hits(module), [])
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()

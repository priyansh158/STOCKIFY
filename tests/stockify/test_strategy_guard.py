"""Strategies may not import network or Kite modules, and their source is checked before it runs (LCB 5.3)."""
from __future__ import annotations

import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import strategy as S

GOOD = """
from stockify.strategy import Strategy

class Fine(Strategy):
    def on_bar(self, ctx, bar):
        pass
"""


class StrategyGuardTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="lcbguard-", dir=str(support.SANDBOX_ROOT)))
        self.pkg = f"lcbguard_{abs(hash(self.id())) % 10**8}"
        (self.root / self.pkg).mkdir()
        (self.root / self.pkg / "__init__.py").write_text("", encoding="utf-8")
        sys.path.insert(0, str(self.root))
        self.addCleanup(sys.path.remove, str(self.root))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(self._forget)

    def _forget(self):
        for name in [n for n in sys.modules if n == self.pkg or n.startswith(self.pkg + ".")]:
            del sys.modules[name]

    def _module(self, name: str, source: str) -> str:
        (self.root / self.pkg / f"{name}.py").write_text(textwrap.dedent(source), encoding="utf-8")
        return f"{self.pkg}.{name}"

    def test_every_banned_import_and_call_is_refused(self):
        for code in ("import kite_client", "import kiteconnect", "from kiteconnect import KiteConnect as K",
                     "import requests", "import httpx", "from urllib.request import urlopen", "import socket",
                     "import http.client", "import subprocess", "import ctypes", "import importlib",
                     "from importlib import import_module", "import builtins", "from stockify import kite_readonly",
                     "import stockify.kite_readonly", "from stockify import kitecheck",
                     "x = __import__('os')", "exec('1')", "eval('1')", "compile('1', 'f', 'exec')",
                     "f = eval", "g = __builtins__", "h = vars().__builtins__"):
            with self.subTest(code=code), self.assertRaises(S.StrategyCodeError):
                S.check_source(code)

    def test_platform_imports_strategies_use_still_pass(self):
        S.check_source("from decimal import Decimal\nfrom datetime import date\nimport re\nimport math\n"
                       "from stockify import strategy as S, timeutil\nfrom stockify.strategy import Strategy\n")

    def test_source_is_checked_before_the_module_runs(self):
        ref = self._module("bad", """
            raise RuntimeError("top-level code ran before the check")
            import kite_client
        """)
        with self.assertRaises(S.StrategyCodeError):
            S.load(ref + ":Anything")
        self.assertNotIn(ref, sys.modules)

    def test_a_helper_in_the_same_package_is_checked_too(self):
        self._module("_helper", """
            raise RuntimeError("helper ran before the check")
            import kiteconnect
        """)
        ref = self._module("uses_helper", f"from {self.pkg}._helper import x\n" + GOOD)
        with self.assertRaises(S.StrategyCodeError):
            S.load(ref + ":Fine")
        self.assertNotIn(f"{self.pkg}._helper", sys.modules)

    def test_relative_helper_imports_are_followed(self):
        self._module("_rel", "import socket\n")
        ref = self._module("uses_rel", "from . import _rel\n" + GOOD)
        with self.assertRaises(S.StrategyCodeError):
            S.load(ref + ":Fine")

    def test_a_clean_strategy_loads(self):
        ref = self._module("fine", GOOD)
        self.assertIsInstance(S.load(ref + ":Fine"), S.Strategy)

    def test_the_reference_strategies_still_load(self):
        from stockify.strategies import _catalogue
        refs = [s["ref"] for s in _catalogue.python_strategies()]
        self.assertIn("stockify.strategies.opening_range_breakout:OpeningRangeBreakout", refs)
        self.assertIn("stockify.strategies.iron_fly:IronFly", refs)

    def test_a_missing_module_is_a_strategy_error(self):
        with self.assertRaises(S.StrategyCodeError):
            S.load(f"{self.pkg}.nope:X")


if __name__ == "__main__":
    unittest.main()

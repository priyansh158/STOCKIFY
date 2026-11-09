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

    def test_rereview_n5_reproductions_are_refused(self):
        for code in (
                # Kite through modules a strategy may import
                'from stockify import strategy as S\nS.importlib.import_module("kite" "connect").KiteConnect',
                'import sys\nsys.modules["kiteconnect"].KiteConnect',
                "import stockify.data.instruments as m\nm.kite_client", "import stockify.daemon as d\nd.kite_client",
                "import server.routers.kite", "import kite_ticker_worker",
                "import stockify\nstockify.kite_readonly.client()", "import stockify.strategy\nstockify.kite_readonly",
                'import pkgutil\npkgutil.resolve_name("kiteconnect:KiteConnect")',
                "().__class__.__base__.__subclasses__()", 'globals()["S"]', 'locals()', 'vars()',
                'import sys\ngetattr(sys, "mod" + "ules")', "x = __loader__", "y = __spec__.loader", "breakpoint()",
                'setattr(ctx, "x", 1)', "from stockify import strategy as S\nS.sys",
                # file, process and network access
                'import _io\n_io.open("f")', 'import codecs\ncodecs.open("f")', 'import nt\nnt.system("x")',
                "import _socket", "import ssl", "import smtplib", "import xmlrpc.client", "import webbrowser",
                "import runpy", "import code", "import zipimport", "import pickle", "import os.path",
                # the wall clock through allowed modules
                "from stockify import timeutil\ntimeutil.time.time()", "from stockify import engine\nengine.sys",
                "import datetime\ndatetime.datetime.now()", "from datetime import datetime\ndatetime.now()",
                "from datetime import date\ndate.today()",
                # private attributes off self
                "ctx._engine", "bar.__dict__", "self.__class__", "self.__dict__", "S._secret"):
            with self.subTest(code=code), self.assertRaises(S.StrategyCodeError):
                S.check_source(code)

    def test_round3_names_imported_from_allowed_modules_are_refused(self):
        for code in (
                # the re-review's probes (new_probe.py)
                "from stockify.strategy import importlib as I\nK = I.import_module('kite'+'connect')",
                "from stockify.timeutil import time as T\nx = T.time()",
                "from stockify.engine import sys as Y\nx = Y.meta_path",
                "from typing import sys as Y\nx = Y.meta_path",
                "from stockify.engine import _engine as E",
                "from stockify.strategy import ast",
                "from stockify.strategy import importlib",
                "class A:\n def f(self):\n  self.m = 1\n  self.__dict__",
                "class A:\n @classmethod\n def f(cls):\n  return cls.__mro__",
                "def f(o):\n  return o._foo",
                "x = getattr(a, 'b')",
                "x = '{0.__class__}'.format(1)",
                "x = '{0[_secret]}'.format(d)",
                "x = vars",
                # (a) `*` and private names, (b) module-like names, (c) modules resolved by importing X
                "from decimal import *", "from math import _x", "from datetime import os", "from re import inspect",
                "from stockify.strategy import builtins", "from stockify.strategy import numerics",
                "from collections import abc", "from stockify.strategies import *",
                "from stockify.strategies._option_legs import _private",
                # plain `import X.Y` when X.Y is not on the allowlist
                "import collections.abc", "import datetime.sys", "import stockify.numerics as n",
                # attributes that lead back to modules or frames
                "import math\nmath.inspect", "def f(g):\n  return g.gi_frame.f_globals", "S.ast"):
            with self.subTest(code=code), self.assertRaises(S.StrategyCodeError):
                S.check_source(code, "stockify.strategies.probe", "stockify.strategies")

    def test_round3_ordinary_names_still_import(self):
        S.check_source("from decimal import ROUND_CEILING, Decimal\nfrom datetime import date, timedelta\n"
                       "from collections import deque, defaultdict\nfrom typing import Any\nfrom math import sqrt\n"
                       "from stockify.strategy import Strategy, rupees\nfrom stockify.timeutil import ist_ns\n"
                       "from stockify.strategies._option_legs import OptionLegs\n"
                       "class A:\n def f(self):\n  self._x = 1\n  return ctx._rows()\n",
                       "stockify.strategies.probe", "stockify.strategies")

    def test_the_getattr_exception_needs_the_real_package(self):
        code = "x = getattr(a, 'b')"
        S.check_source(code, "stockify.strategies._margin_watch", "stockify.strategies")
        with self.assertRaises(S.StrategyCodeError):
            S.check_source(code, "stockify.strategies._margin_watch", "evil")

    def test_the_allowlist(self):
        self.assertEqual(S.ALLOWED_IMPORTS, frozenset({
            "__future__", "datetime", "decimal", "re", "math", "dataclasses", "typing", "enum", "collections",
            "itertools", "functools", "statistics", "stockify.strategy", "stockify.engine", "stockify.timeutil"}))

    def test_platform_imports_strategies_use_still_pass(self):
        S.check_source("from __future__ import annotations\nfrom decimal import ROUND_CEILING, Decimal\n"
                       "from datetime import date\nimport re\nimport math\nimport statistics\n"
                       "from collections import deque\nfrom dataclasses import dataclass\nfrom typing import Any\n"
                       "from stockify import strategy as S, timeutil\nfrom stockify.strategy import Strategy\n"
                       "class A(Strategy):\n    def __init__(self):\n        self._n = 0\n"
                       "    def on_bar(self, ctx, bar):\n        self._step(ctx)\n        rows = ctx._rows()\n"
                       "        ctx.now()\n        ctx.today()\n")

    def test_platform_and_other_packages_are_not_an_own_package(self):
        self.assertEqual(S.own_package("stockify.tools.replay", "stockify.tools"), "")
        self.assertEqual(S.own_package("stockify.foo", "stockify"), "")
        self.assertEqual(S.own_package("stockify.strategies.x", "stockify.strategies"), "stockify.strategies")
        with self.assertRaises(S.StrategyCodeError):
            S.check_source("from stockify.tools import capture_charges", "stockify.tools.replay", "stockify.tools")
        with self.assertRaises(S.StrategyCodeError):
            S.check_source("from stockify.data import instruments", "stockify.strategies.x", "stockify.strategies")

    def test_only_the_margin_wrapper_may_call_getattr(self):
        code = "def f(ctx, name):\n    return getattr(ctx, name)\n"
        S.check_source(code, "stockify.strategies._margin_watch", "stockify.strategies")
        with self.assertRaises(S.StrategyCodeError):
            S.check_source(code, "stockify.strategies.mine", "stockify.strategies")

    def test_every_strategy_the_repo_loads_by_ref_still_loads(self):
        for ref in ("stockify.strategies._margin_watch:MarginWatch", "stockify.tools.replay:Replay",
                    "tests.stockify.run_fixtures:FailOnSecondBar", "stockify.strategies.iron_fly:IronFly",
                    "stockify.strategies.short_straddle_stop:ShortStraddleStop",
                    "stockify.strategies.eod_short_straddle:EodShortStraddle"):
            with self.subTest(ref=ref):
                self.assertIsInstance(S.load(ref), S.Strategy)

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

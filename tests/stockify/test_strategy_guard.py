"""Strategies may not import network or Kite modules, and their source is checked before it runs (LCB 5.3)."""
from __future__ import annotations

import copy
import hashlib
import inspect
import re
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import golden, timeutil
from stockify import strategy as S

REPO = Path(__file__).resolve().parents[2]
TIMEUTIL_CLOCK = ("now_ns", "now_ist", "ist_today")

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

    def test_round4_chains_from_allowed_modules_are_resolved(self):
        for code in (
                # the round-3 re-review's probes
                "import enum as e\nx = e.bltns.eval('1')", "import enum as e\nx = e.bltns.exec('1')",
                "import enum as e\nx = e.bltns.open('f')", "import enum as e\nx = e.bltns.compile",
                "import enum as e\nx = e.bltns.getattr", "import re as r\nx = r.enum.bltns.eval('1')",
                "import dataclasses as d\nx = d.types", "import dataclasses\nx = dataclasses.types.FunctionType",
                "from datetime import datetime as D\nx = D.now()", "from datetime import datetime\nf = datetime.now\nx = f()",
                # (a) modules off the allowlist, (b) dangerous builtins, (c) unresolvable steps
                "from stockify import strategy as S\nx = S.numerics", "import functools as f\nx = f.reprlib",
                "import enum as e\nx = e.nonexistent_attribute", "import collections as c\nx = c.abc.Callable",
                "import enum as e\nx = e.types",
                # computed access to a module's names
                "import enum as e\nx = e.bltns['ev' + 'al']", "import math as m\nx = m['pi']",
                # names refused on any object
                "def f(o):\n  return o.exec", "def f(o):\n  return o.eval", "def f(o):\n  return o.compile",
                "def f(o):\n  return o.bltns", "def f(o):\n  return o.builtins",
                # the wall clock on anything but ctx
                "def f(clock):\n  return clock.now()", "def f(o):\n  return o.today()", "def f(o):\n  return o.utcnow()",
                "def f(o):\n  return o.time_ns()", "def f(o):\n  return o.monotonic()",
                "def f(o):\n  return o.perf_counter()", "def f(self):\n  return self.ctx.now()",
                "from datetime import datetime\nx = datetime.fromtimestamp()",
                "from datetime import datetime\nx = datetime.fromtimestamp(0)",
                "from datetime import datetime as ctx\nx = ctx.now()"):
            with self.subTest(code=code), self.assertRaises(S.StrategyCodeError):
                S.check_source(code, "stockify.strategies.probe", "stockify.strategies")

    def test_round5_string_eval_paths_are_refused(self):
        for code in (
                # evalprobe.py
                "from typing import get_type_hints\ndef f(a: \"__import__('math').pi\"): pass\nh = get_type_hints(f)",
                "from typing import get_type_hints\ndef f(a: \"__import__('kiteconnect').KiteConnect\"): ...\n"
                "get_type_hints(f)",
                # the same through ForwardRef, typing attributes, singledispatch.register and annotations
                "import typing as t\nx = t.get_type_hints(f)", "from typing import ForwardRef\nx = ForwardRef('1')",
                "import typing as t\nx = t.ForwardRef('1')", "import typing as t\nx = t.evaluate_forward_ref",
                "def f(o):\n  return o.get_annotations(o)", "def f(o):\n  return o.get_type_hints(o)",
                "from functools import singledispatch\n@singledispatch\ndef f(x): pass\n@f.register\n"
                "def _(x: int): pass",
                "def f(g):\n  return g.register(int)",
                "def f(a: 'x') -> None: pass", "def f(a) -> 'x': pass", "x: 'int' = 1",
                "from typing import Optional\nx: Optional['int'] = None", "def f(*a: 'x', **k: 'y'): pass",
                "class A:\n    n: 'int' = 0"):
            with self.subTest(code=code), self.assertRaises(S.StrategyCodeError):
                S.check_source(code, "stockify.strategies.probe", "stockify.strategies")

    def test_round5_ctx_cannot_be_rebound_or_fed_a_clock(self):
        for code in (
                "from datetime import datetime\ndef f():\n ctx = datetime\n return ctx.now()",
                "from datetime import datetime\ndef h(ctx):\n return ctx.now()\nx = h(datetime)",
                "from datetime import datetime\nwith datetime as ctx:\n pass",
                "from datetime import datetime\nfor ctx in [datetime]:\n ctx.now()",
                "from datetime import datetime\nf = (lambda ctx: ctx.now())(datetime)",
                "from datetime import datetime\n(ctx := datetime)\nctx.now()",
                "from datetime import datetime\nx = [ctx.now() for ctx in [datetime]]",
                "from datetime import datetime\ndef h(ctx=datetime):\n return ctx.now()",
                "from datetime import datetime\ndef h(*, ctx=datetime):\n return ctx.now()",
                "from datetime import datetime\ndef h(ctx):\n return ctx.now()\nx = h(ctx=datetime)",
                "from datetime import datetime\nclass A:\n def h(self, ctx):\n  return ctx.now()\n"
                " def on_bar(self, ctx, bar):\n  self.h(datetime)",
                "class A:\n @staticmethod\n def h(ctx):\n  return ctx.now()\n def on_bar(self, ctx, bar):\n"
                "  self.h(bar)",
                "def h(ctx):\n return ctx.now()\ndef g(*a):\n return h(*a)",
                "ctx = 1", "ctx += 1", "del ctx", "def f():\n global ctx", "from datetime import datetime as ctx",
                "try:\n pass\nexcept Exception as ctx:\n pass"):
            with self.subTest(code=code), self.assertRaises(S.StrategyCodeError):
                S.check_source(code, "stockify.strategies.probe", "stockify.strategies")

    def test_round5_passing_ctx_along_still_works(self):
        S.check_source("class A:\n"
                       "    def on_bar(self, ctx, bar):\n"
                       "        self._close(ctx, bar.instrument)\n        self._daily(ctx, '09:15')\n"
                       "        helper(ctx)\n        helper(ctx=ctx)\n        x = ctx.now()\n"
                       "    def _close(self, ctx, iid):\n        return ctx.today()\n"
                       "    @staticmethod\n    def _daily(ctx, hhmm):\n        return ctx.now()\n"
                       "def helper(ctx):\n    return ctx.now()\n",
                       "stockify.strategies.probe", "stockify.strategies")

    def test_round4_legitimate_chains_and_ctx_clock_pass(self):
        S.check_source("from __future__ import annotations\nfrom typing import Optional\n"
                       "from datetime import date, datetime, timezone\n"
                       "from decimal import Decimal, ROUND_CEILING\nimport math\nfrom stockify import timeutil\n"
                       "from stockify import strategy as S\n"
                       "class A(S.Strategy):\n"
                       "    x: Optional[int] = None\n"
                       "    def on_bar(self, ctx, bar):\n"
                       "        if ctx.today() != self.day and ctx.now() > 0:\n"
                       "            d = timeutil.ns_to_ist(bar.start_ns).date()\n"
                       "            p = bar.open + bar.close + math.floor(1.5)\n"
                       "            t = datetime.fromtimestamp(0, tz=timezone.utc)\n"
                       "            q = Decimal('1').quantize(Decimal('1'), rounding=ROUND_CEILING)\n",
                       "stockify.strategies.probe", "stockify.strategies")

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

    # ── stockify.timeutil's own wall clock (LCB 5.14) ────────────────────────

    def test_the_three_names_are_timeutil_s_wall_clock(self):
        # If timeutil grows another clock function, it belongs in the guard as well.
        clocks = sorted(n for n, f in vars(timeutil).items()
                        if callable(f) and getattr(f, "__module__", "") == timeutil.__name__
                        and not inspect.signature(f).parameters)
        self.assertEqual(clocks, sorted(TIMEUTIL_CLOCK))

    def test_timeutil_s_wall_clock_is_refused_imported_by_name(self):
        for fn in TIMEUTIL_CLOCK:
            for code in (f"from stockify.timeutil import {fn}",                      # the import alone
                         f"from stockify.timeutil import {fn}\nT = {fn}()",
                         f"from stockify.timeutil import ist_ns, {fn}\nT = {fn}()",
                         f"from stockify.timeutil import (\n    ns_to_ist,\n    {fn},\n)",
                         f"def f(ctx):\n    from stockify.timeutil import {fn}\n    return {fn}()"):
                with self.subTest(code=code), self.assertRaisesRegex(S.StrategyCodeError, fn):
                    S.check_source(code)

    def test_timeutil_s_wall_clock_is_refused_under_another_name(self):
        for fn in TIMEUTIL_CLOCK:
            for code in (f"from stockify.timeutil import {fn} as clock\nT = clock()",
                         f"from stockify.timeutil import {fn} as ist_ns\nT = ist_ns()",
                         f"from stockify.timeutil import ist_ns as {fn}",              # a harmless thing under its name
                         f"from stockify import timeutil\nclock = timeutil.{fn}\nT = clock()",
                         f"from stockify import timeutil\nclocks = [timeutil.{fn}]\nT = clocks[0]()",
                         f"from stockify import timeutil\nclass A:\n    clock = staticmethod(timeutil.{fn})"):
                with self.subTest(code=code), self.assertRaisesRegex(S.StrategyCodeError, fn):
                    S.check_source(code)

    def test_timeutil_s_wall_clock_is_refused_through_the_module(self):
        for fn in TIMEUTIL_CLOCK:
            for code in (f"from stockify import timeutil\nT = timeutil.{fn}()",
                         f"from stockify import timeutil as t\nT = t.{fn}()",
                         f"import stockify.timeutil as t\nT = t.{fn}()",
                         f"import stockify.timeutil as timeutil\ndef f(ctx):\n    return timeutil.{fn}()",
                         f"from stockify import timeutil\ndef f(ctx, m=timeutil):\n    return m.{fn}()",
                         f"from stockify import timeutil\nT = (timeutil).{fn}()",
                         f"from stockify import timeutil\nT = [timeutil][0].{fn}()"):
                with self.subTest(code=code), self.assertRaisesRegex(S.StrategyCodeError, fn):
                    S.check_source(code)
            for code in (f"from .. import timeutil\nT = timeutil.{fn}()", f"from ..timeutil import {fn}\nT = {fn}()",
                         f"from ..timeutil import {fn} as clock\nT = clock()"):
                with self.subTest(code=code), self.assertRaisesRegex(S.StrategyCodeError, fn):
                    S.check_source(code, "stockify.strategies.probe", "stockify.strategies")

    def test_timeutil_s_wall_clock_is_refused_on_any_object_even_ctx(self):
        # The context has no such names, so `ctx` is no exception here: a helper handed the timeutil module
        # as its `ctx` gets nowhere.
        for fn in TIMEUTIL_CLOCK:
            for code in (f"def f(o):\n    return o.{fn}()", f"def f(ctx):\n    return ctx.{fn}()",
                         f"class A:\n    def on_bar(self, ctx, bar):\n        return ctx.{fn}",
                         f"class A:\n    def on_bar(self, ctx, bar):\n        return self.clock.{fn}()"):
                with self.subTest(code=code), self.assertRaisesRegex(S.StrategyCodeError, fn):
                    S.check_source(code)

    def test_timeutil_s_wall_clock_is_refused_whatever_an_allowed_module_calls_it(self):
        # By what the object is, not only by its name: an allowed module that kept the clock under another name.
        for fn in TIMEUTIL_CLOCK:
            with mock.patch.object(timeutil, "started", vars(timeutil)[fn], create=True):
                for code in ("from stockify.timeutil import started", "from stockify.timeutil import started as s",
                             "from stockify import timeutil\nT = timeutil.started()",
                             "import stockify.timeutil as t\nf = t.started"):
                    with self.subTest(fn=fn, code=code), self.assertRaises(S.StrategyCodeError):
                        S.check_source(code)
        S.check_source("from stockify import timeutil\nT = timeutil.NS_PER_S")      # and nothing else is caught by it

    def test_a_strategy_file_using_timeutil_s_wall_clock_is_never_imported(self):
        for n, code in enumerate(("from stockify.timeutil import now_ns\nT = now_ns()\n",
                                  "from stockify.timeutil import now_ist as clock\nT = clock()\n",
                                  "from stockify import timeutil\nT = timeutil.ist_today()\n")):
            ref = self._module(f"clock{n}", code + GOOD)
            with self.subTest(code=code), self.assertRaises(S.StrategyCodeError):
                S.load(ref + ":Fine")
            self.assertNotIn(ref, sys.modules)

    def test_the_rest_of_timeutil_and_the_context_s_clock_still_pass(self):
        for code in ("from stockify import timeutil\ndef f(ctx, bar):\n"
                     "    return timeutil.ns_to_ist(bar.start_ns).date() == ctx.today()",
                     "from stockify import timeutil\ndef f(ctx):\n    return timeutil.ist_ns(ctx.today(), '09:15')",
                     "from stockify.timeutil import IST, NS_PER_S, ist_ns, ist_to_ns, ns_to_ist, parse_hhmm, s_to_ist",
                     "import stockify.timeutil as t\nX = t.NS_PER_S\nY = t.parse_hhmm('09:15')",
                     "def f(ctx):\n    now_ns = ctx.now()\n    ist_today = ctx.today()\n    return now_ns, ist_today",
                     "def f(ctx, now_ist=None):\n    return now_ist"):
            with self.subTest(code=code):
                S.check_source(code)

    def test_no_shipped_strategy_or_how_to_example_uses_timeutil_s_wall_clock(self):
        word = re.compile(r"\b(" + "|".join(TIMEUTIL_CLOCK) + r")\b")
        shipped = sorted((REPO / "stockify" / "strategies").glob("*.py")) + [REPO / "stockify" / "tools" / "replay.py"]
        self.assertGreater(len(shipped), 5)
        for path in shipped:
            with self.subTest(file=path.name):
                self.assertIsNone(word.search(path.read_text(encoding="utf-8")))
                if path.name not in ("__init__.py", "_catalogue.py"):       # the catalogue is not a strategy
                    S.check_module(".".join(path.relative_to(REPO).with_suffix("").parts))
        examples = 0
        for doc in sorted((REPO / "docs" / "plan").glob("HOWTO_*.md")):
            for block in re.findall(r"```python\n(.*?)```", doc.read_text(encoding="utf-8"), flags=re.S):
                examples += 1
                with self.subTest(doc=doc.name):
                    self.assertIsNone(word.search(block))
        self.assertGreater(examples, 0)

    def test_the_how_to_names_the_rule(self):
        text = (REPO / "docs" / "plan" / "HOWTO_strategy.md").read_text(encoding="utf-8")
        for fn in TIMEUTIL_CLOCK:
            self.assertIn(f"`{fn}`", text)

    # ── Goldens name strategies too (LCB 5.13) ───────────────────────────────

    def _golden(self, ref: str) -> dict:
        g = copy.deepcopy(next(g for _, g in golden.goldens(["G01"])))
        g["strategy"] = {"ref": ref, "params": {}}
        return g

    def _loaded(self) -> list[str]:
        return sorted(n for n in sys.modules if n.startswith(self.pkg + "."))

    def test_a_golden_naming_a_strategy_that_imports_socket_is_refused(self):
        ref = self._module("net", "import socket\n" + GOOD) + ":Fine"
        with self.assertRaisesRegex(S.StrategyCodeError, "socket"):
            golden.build_bundle(self._golden(ref))
        self.assertEqual(self._loaded(), [], "the strategy file ran before it was checked")
        with self.assertRaisesRegex(S.StrategyCodeError, "socket"):
            golden.run_one(self._golden(ref), "oracle")
        self.assertEqual(self._loaded(), [], "the strategy file ran before it was checked")

    def test_a_golden_is_refused_for_what_its_strategy_does_at_import_time(self):
        for n, code in enumerate(("import os\nos.environ['LCB_GOLDEN_RAN'] = '1'\n", "import kite_client\n",
                                  "import subprocess\n", "open('x', 'w')\n", "from stockify import kite_readonly\n",
                                  "import datetime\nT = datetime.datetime.now()\n")):
            ref = self._module(f"bad{n}", code + GOOD) + ":Fine"
            with self.subTest(code=code), self.assertRaises(S.StrategyCodeError):
                golden.build_bundle(self._golden(ref))
            self.assertEqual(self._loaded(), [])
        import os
        self.assertNotIn("LCB_GOLDEN_RAN", os.environ)

    def test_a_golden_strategy_s_own_helpers_are_checked_as_well(self):
        self._module("wire", "import socket\nx = 1\n")
        ref = self._module("uses_wire", f"from {self.pkg}.wire import x\n" + GOOD) + ":Fine"
        with self.assertRaisesRegex(S.StrategyCodeError, "socket"):
            golden.build_bundle(self._golden(ref))
        self.assertEqual(self._loaded(), [])

    def test_a_golden_naming_a_missing_strategy_file_is_refused(self):
        with self.assertRaises(S.StrategyCodeError):
            golden.build_bundle(self._golden(f"{self.pkg}.nope:X"))

    def test_a_golden_with_a_clean_strategy_is_built_and_run_as_before(self):
        ref = self._module("fine", GOOD) + ":Fine"
        b = golden.build_bundle(self._golden(ref))
        self.assertEqual(self._loaded(), [], "building the bundle needs the source, not the module")
        # The source hash is what it was when the module was imported to be read: the text, newlines as "\n".
        self.assertEqual(b["strategy"], {"kind": "python", "ref": ref, "params": {},
                                         "source_sha256": hashlib.sha256(textwrap.dedent(GOOD).encode()).hexdigest()})
        g = self._golden(ref)
        g["expect"] = {"orders": [], "fills": []}               # Fine never trades
        self.assertEqual(golden.run_one(g, "oracle"), [])
        self.assertEqual(self._loaded(), [f"{self.pkg}.fine"])  # the run loaded it, through strategy.load

    def test_the_source_hash_of_a_golden_strategy_has_not_changed(self):
        from stockify.tools import fuzz, replay
        b = golden.build_bundle(self._golden(fuzz.REPLAY_REF))
        self.assertEqual(b["strategy"]["source_sha256"],
                         hashlib.sha256(inspect.getsource(replay).encode("utf-8")).hexdigest())
        # A file without a final newline hashed as if it had one (inspect.getsource did that); it still does.
        ref = self._module("no_newline", GOOD)
        path = self.root / self.pkg / "no_newline.py"
        path.write_bytes(path.read_bytes().rstrip())
        b = golden.build_bundle(self._golden(ref + ":Fine"))
        self.assertEqual(b["strategy"]["source_sha256"],
                         hashlib.sha256(textwrap.dedent(GOOD).encode()).hexdigest())


if __name__ == "__main__":
    unittest.main()

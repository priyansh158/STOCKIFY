"""The Python strategy API (M2.3, A2 §4). The oracle and the C++ engine both run it.

    from stockify.strategy import Strategy

    class Mine(Strategy):
        def on_start(self, ctx): ...
        def on_bar(self, ctx, bar): ...

Money is integer paise everywhere; `rupees()` and `paise()` help readability.
Strategies must not read the wall clock, draw their own random numbers, or
touch files or the network: use `ctx.now()`, `ctx.rng()` and `ctx.log()`.
`check_source` enforces this when a strategy is loaded (A2 §4.4, I-2).
"""
from __future__ import annotations

import ast
import importlib
import math
import re
from dataclasses import dataclass
from decimal import Decimal

from stockify import numerics

NAN = float("nan")


class Strategy:
    def on_start(self, ctx): ...                 # noqa: ANN001, ANN201
    def on_bar(self, ctx, bar): ...              # noqa: ANN001, ANN201
    def on_tick(self, ctx, tick): ...            # noqa: ANN001, ANN201
    def on_timer(self, ctx, timer_id): ...       # noqa: ANN001, ANN201
    def on_fill(self, ctx, fill): ...            # noqa: ANN001, ANN201
    def on_order_update(self, ctx, order): ...   # noqa: ANN001, ANN201
    def on_session_phase(self, ctx, change): ... # noqa: ANN001, ANN201
    def on_session_end(self, ctx): ...           # noqa: ANN001, ANN201

    def save_state(self) -> dict:
        return {}

    def load_state(self, state: dict) -> None:
        pass


@dataclass(frozen=True)
class Bar:
    instrument: int
    interval_s: int
    start_ns: int
    t_ns: int
    open: int
    high: int
    low: int
    close: int
    volume: int
    oi: int
    built: bool = False


@dataclass(frozen=True)
class Tick:
    instrument: int
    t_ns: int
    ltp: int
    volume: int
    atp: int
    bid: int | None
    ask: int | None
    stale: bool = False


@dataclass(frozen=True)
class PhaseChange:
    group: str
    phase: str
    t_ns: int


@dataclass(frozen=True)
class PositionView:
    instrument: int
    product: str
    qty: int
    avg_price: int
    realised: int


@dataclass(frozen=True)
class Funds:
    available: int
    cash: int
    blocked: int
    reserved: int


class ChainView(list):
    """ctx.chain(): the option rows of one underlying and expiry (a list of dicts, so code written before M4.4 still
    works), plus the forward the IVs and Greeks were computed from.

    forward_paise: the same-expiry future's mid; else the synthetic forward (C-13); else the index value; None when
    there is none. forward_source: "future" | "synthetic" | "forward_from_index" | None. atm_strike_paise: the
    strike nearest the forward (the lower one on a tie)."""

    def __init__(self, rows=(), forward_paise: float | None = None, forward_source: str | None = None,  # noqa: ANN001
                 atm_strike_paise: int | None = None):
        super().__init__(rows)
        self.forward_paise = forward_paise
        self.forward_source = forward_source
        self.atm_strike_paise = atm_strike_paise


def rupees(p: int) -> Decimal:
    return Decimal(p) / 100


def paise(text: str) -> int:
    return numerics.to_paise(text)


# ── Indicators (A2 §4.3): exactly this expression order ──────────────────────

def sma(values: list[int], n: int) -> float:
    if n < 1 or len(values) < n:
        return NAN
    s = 0
    for v in values[-n:]:
        s += v                                     # exact int64 sum
    return float(s) / n


def ema(values: list[int], n: int) -> float:
    if n < 1 or len(values) < n:
        return NAN
    alpha = 2.0 / (n + 1.0)
    s = 0
    for v in values[:n]:
        s += v
    e = float(s) / n
    for x in values[n:]:
        e = e + alpha * (x - e)
    return e


def rsi(values: list[int], n: int) -> float:
    if n < 1 or len(values) < n + 1:
        return NAN
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    g = 0
    lo = 0
    for x in changes[:n]:
        if x > 0:
            g += x
        else:
            lo += -x
    avg_gain = float(g) / n
    avg_loss = float(lo) / n
    for x in changes[n:]:
        avg_gain = (avg_gain * (n - 1) + (x if x > 0 else 0)) / n
        avg_loss = (avg_loss * (n - 1) + (-x if x < 0 else 0)) / n
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def atr(bars: list[tuple[int, int, int]], n: int) -> float:
    """bars: (high, low, close) in order."""
    if n < 1 or len(bars) < n + 1:
        return NAN
    trs = []
    for i in range(1, len(bars)):
        h, lo, _ = bars[i]
        c_prev = bars[i - 1][2]
        trs.append(max(h - lo, abs(h - c_prev), abs(lo - c_prev)))
    s = 0
    for tr in trs[:n]:
        s += tr
    a = float(s) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def is_nan(x: float) -> bool:
    return isinstance(x, float) and math.isnan(x)


# ── Load-time checks (A2 §4.4) ───────────────────────────────────────────────
#
# A guard against accidents, not a sandbox: Python code determined to escape a
# static check can. The check exists so that an ordinary strategy cannot read
# the wall clock, draw its own randomness, touch files, processes or the
# network, or reach Kite: the paper daemon runs strategies in a process that
# holds a live Kite token (LCB 5.3, 5.4). Imports come from an allowlist; the
# source is checked BEFORE the module is imported, so its top-level code never
# runs unchecked; helper modules it imports from its own package are checked
# the same way.

ALLOWED_IMPORTS = frozenset({
    "__future__", "datetime", "decimal", "re", "math", "dataclasses", "typing", "enum", "collections", "itertools",
    "functools", "statistics", "stockify.strategy", "stockify.engine", "stockify.timeutil",
})
# A strategy may also import from its own package, except a platform package (where "own package" would be all
# of stockify): stockify.strategies.* is the one stockify package strategies live in.
_PLATFORM = "stockify"
_STRATEGY_PACKAGE = "stockify.strategies"
BANNED_CALLS = {"getattr", "setattr", "delattr", "globals", "locals", "vars", "breakpoint", "open", "input",
                "__import__", "exec", "eval", "compile"}
BANNED_NAMES = {"__import__", "__builtins__", "__loader__", "__spec__", "exec", "eval", "compile", "globals",
                "locals", "vars", "breakpoint", "getattr", "setattr", "delattr"}
# Attribute names that hand over a module or the module table (`stockify.timeutil` holds `time`, the engine `sys`).
BANNED_ATTR_NAMES = {"importlib", "sys", "modules", "kite_client", "kiteconnect", "os", "subprocess", "socket",
                     "time", "ast", "inspect", "builtins",
                     # frame and code objects lead back to globals (LCB 5.5)
                     "f_globals", "f_locals", "f_builtins", "f_back", "gi_frame", "cr_frame", "ag_frame", "tb_frame"}
# `_`-prefixed attributes other than on `self`/`cls`: only these, by name. `ctx._rows()` is the context's instrument
# table, read by the option helpers in stockify/strategies (_option_legs.py, _script.py).
ALLOWED_PRIVATE_ATTRS = {"_rows"}
# Platform strategy modules, reviewed here, that may call getattr: MarginWatch forwards its wrapped context.
GETATTR_MODULES = {"stockify.strategies._margin_watch"}
_CLOCK_CALLS = {"now", "today", "utcnow"}


class StrategyCodeError(ValueError):
    pass


def _imports(node: ast.AST, package: str) -> list[tuple[str, list[str]]]:
    """(module, the names it binds) per import; relative imports resolved against `package`."""
    if isinstance(node, ast.Import):
        return [(a.name, []) for a in node.names]
    base = node.module or ""
    if node.level:
        parts = package.split(".") if package else []
        parts = parts[:len(parts) - (node.level - 1)] if node.level > 1 else parts
        base = ".".join(p for p in (*parts, base) if p)
    return [(base, [a.name for a in node.names])]


def own_package(module_name: str, package: str) -> str:
    """The package a strategy may import from: its own, unless that is the platform's (then stockify.strategies)."""
    if package == _STRATEGY_PACKAGE or package.startswith(_STRATEGY_PACKAGE + "."):
        return package
    if package == _PLATFORM or package.startswith(_PLATFORM + "."):
        return ""
    return package


_FORMAT_ESCAPE = re.compile(r"\{[^{}]*[.\[]\s*_")      # str.format field reaching `._x` or `[_x]`
_UNSAFE_FROM_NAMES = {"ast", "importlib", "sys", "time", "os", "builtins", "inspect"}


def _bad_imported_name(module: str, names: list[str], own: str) -> str | None:
    """The first name of `from module import names` that may not be imported (LCB 5.5), else None.

    Everywhere: `*`, `_`-prefixed names and module-like names (BANNED_ATTR_NAMES, `ast`, `inspect`, …). From an
    allowlisted platform or stdlib module (imported here for the check: they are trusted), also any name that is
    itself a module or part of the import machinery. A strategy's own package is never imported for the check."""
    for n in names:
        if n == "*" or n.startswith("_") or n in BANNED_ATTR_NAMES or n in _UNSAFE_FROM_NAMES:
            return n
    if own and (module == own or module.startswith(own + ".")):
        return None
    import builtins
    import importlib.util
    import types
    mod = importlib.import_module(module)
    for n in names:
        if n not in vars(mod):
            if hasattr(mod, "__path__") and importlib.util.find_spec(f"{module}.{n}") is not None:
                return n                                  # a submodule of an allowed package
            continue                                      # the import itself will fail
        obj = vars(mod)[n]
        kind = getattr(type(obj), "__module__", "") or ""
        if isinstance(obj, types.ModuleType) or kind.startswith(("importlib", "_frozen_importlib")) \
                or any(obj is f for f in (builtins.__import__, importlib.import_module, builtins.exec,
                                          builtins.eval, builtins.compile, builtins.open)):
            return n
    return None


def _allowed(module: str, own: str) -> bool:
    return module in ALLOWED_IMPORTS or bool(own) and (module == own or module.startswith(own + "."))


def check_source(source: str, name: str = "<strategy>", package: str = "") -> list[str]:
    """Refuse imports outside the allowlist and wall-clock, file, process, network and Kite access.
    Returns the modules the source imports from its own package (to be checked in turn)."""
    tree = ast.parse(source, filename=name)
    own = own_package(name, package)
    may_getattr = name in GETATTR_MODULES and package == _STRATEGY_PACKAGE
    local: list[str] = []

    def refuse(node: ast.AST, what: str) -> None:
        raise StrategyCodeError(f"{name}:{getattr(node, 'lineno', '?')}: {what} is not allowed in strategies")

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for module, names in _imports(node, package):
                if isinstance(node, ast.ImportFrom) and not _allowed(module, own):
                    qualified = [f"{module}.{n}" for n in names if n != "*"]
                    if not qualified or not all(_allowed(q, own) for q in qualified):
                        refuse(node, f"import from {module!r}")
                    module_names = qualified
                elif not _allowed(module, own):
                    refuse(node, f"import of {module!r}")
                elif isinstance(node, ast.Import) and "." in module and not _allowed(module.split(".")[0], own) and not any(
                        a.asname for a in node.names if a.name == module):
                    refuse(node, f"'import {module}' (it binds {module.split('.')[0]!r}; use 'from ... import')")
                else:
                    if isinstance(node, ast.ImportFrom):
                        bad = _bad_imported_name(module, names, own)
                        if bad:
                            refuse(node, f"importing {bad!r} from {module!r}")
                    module_names = [module] + ([f"{module}.{n}" for n in names if n != "*"]
                                               if isinstance(node, ast.ImportFrom) else [])
                local.extend(m for m in module_names if own and (m == own or m.startswith(own + ".")))
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in BANNED_CALLS and not (f.id == "getattr" and may_getattr):
                refuse(node, f"{f.id}()")
            if isinstance(f, ast.Attribute) and f.attr in _CLOCK_CALLS:
                base = f.value
                last = base.id if isinstance(base, ast.Name) else base.attr if isinstance(base, ast.Attribute) else ""
                if last in ("datetime", "date"):
                    refuse(node, f"{last}.{f.attr}(); use ctx.now()")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and _FORMAT_ESCAPE.search(node.value):
            refuse(node, "a format string that reaches a private attribute ('{0.__class__}')")
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            if not (node.id == "getattr" and may_getattr):
                refuse(node, node.id)
        elif isinstance(node, ast.Attribute):
            attr = node.attr
            on_self = isinstance(node.value, ast.Name) and node.value.id in ("self", "cls")
            if attr.startswith("__") or (attr.startswith("_") and not on_self and attr not in ALLOWED_PRIVATE_ATTRS):
                refuse(node, f"the attribute .{attr}")
            if attr in BANNED_ATTR_NAMES:
                refuse(node, f"the attribute .{attr}")
    return local


def _module_source(module_name: str) -> tuple[str, str]:
    """(source, package) of a module, found without importing the module itself."""
    spec = _find_spec(module_name)
    if spec is None or spec.loader is None or not hasattr(spec.loader, "get_source"):
        raise StrategyCodeError(f"cannot find the source of strategy module {module_name!r}")
    source = spec.loader.get_source(spec.name)
    if source is None:
        raise StrategyCodeError(f"strategy module {module_name!r} has no source to check")
    package = module_name if spec.submodule_search_locations is not None else module_name.rpartition(".")[0]
    return source, package


def _find_spec(name: str):  # noqa: ANN202
    """A module's spec without running it (only its parent packages are imported); None if it is not a module."""
    import importlib.machinery
    import importlib.util
    parent, _, leaf = name.rpartition(".")
    try:
        if not parent:
            return importlib.util.find_spec(name)
        parent_spec = importlib.util.find_spec(parent)
        if parent_spec is None or parent_spec.submodule_search_locations is None:
            return None                       # `from module import name`: name is not a module
        return importlib.machinery.PathFinder.find_spec(leaf, parent_spec.submodule_search_locations)
    except (ImportError, ValueError):
        return None


def check_module(module_name: str) -> None:
    """Check a strategy module's source, and the modules it imports from its own package, before any import."""
    pending, seen = [module_name], set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        source, package = _module_source(name)
        for m in check_source(source, name, package):
            if m not in seen and _find_spec(m) is not None:
                pending.append(m)


def load(ref: str, params: dict | None = None) -> Strategy:
    """'package.module:ClassName' → an instance, after the source check (which runs before the import)."""
    module_name, cls_name = ref.split(":")
    check_module(module_name)
    module = importlib.import_module(module_name)
    cls = getattr(module, cls_name, None)
    if cls is None or not (isinstance(cls, type) and issubclass(cls, Strategy)):
        raise StrategyCodeError(f"{ref} is not a Strategy subclass")
    return cls()

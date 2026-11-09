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
# A guard against accidents, not a sandbox: Python code that wants to escape a
# static check can (attribute tricks, a helper module outside the strategy's
# package). The check exists so that an ordinary strategy cannot read the wall
# clock, draw its own randomness, touch files, processes or the network, or
# reach Kite: the paper daemon runs strategies in a process that holds a live
# Kite token (LCB 5.3). The source is checked BEFORE the module is imported, so
# its top-level code never runs unchecked; strategy helper modules it imports
# from its own package are checked too.

BANNED_MODULES = {"time", "random", "secrets", "os", "subprocess", "socket", "requests", "httpx", "shutil",
                  "pathlib", "io", "urllib", "http", "threading", "multiprocessing", "asyncio", "sqlite3",
                  # Kite and anything that reaches past Python (LCB 5.3)
                  "kite_client", "kiteconnect", "ctypes", "importlib", "builtins"}
BANNED_QUALIFIED = {"stockify.kite_readonly", "stockify.kitecheck"}
BANNED_CALLS = {"open", "eval", "exec", "compile", "__import__", "input"}
BANNED_NAMES = {"__import__", "__builtins__", "exec", "eval", "compile"}   # even when not called directly
BANNED_ATTRS = {("datetime", "now"), ("datetime", "today"), ("datetime", "utcnow"), ("date", "today")}


class StrategyCodeError(ValueError):
    pass


def _imports(node: ast.AST, package: str) -> list[str]:
    """The fully qualified modules an import statement names (relative imports resolved against `package`)."""
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    base = node.module or ""
    if node.level:
        parts = package.split(".") if package else []
        parts = parts[:len(parts) - (node.level - 1)] if node.level > 1 else parts
        base = ".".join(p for p in (*parts, base) if p)
    return [base] + [f"{base}.{a.name}" if base else a.name for a in node.names if a.name != "*"]


def check_source(source: str, name: str = "<strategy>", package: str = "") -> list[str]:
    """Refuse wall-clock, randomness, file, process, network and Kite access. Returns the modules it imports."""
    tree = ast.parse(source, filename=name)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for m in _imports(node, package):
                imported.append(m)
                if m.split(".")[0] in BANNED_MODULES or any(m == q or m.startswith(q + ".") for q in BANNED_QUALIFIED):
                    raise StrategyCodeError(f"{name}:{node.lineno}: import of {m!r} is not allowed in strategies")
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in BANNED_CALLS:
                raise StrategyCodeError(f"{name}:{node.lineno}: {f.id}() is not allowed in strategies")
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and (f.value.id, f.attr) in BANNED_ATTRS:
                raise StrategyCodeError(f"{name}:{node.lineno}: {f.value.id}.{f.attr}() is not allowed; use ctx.now()")
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            raise StrategyCodeError(f"{name}:{node.lineno}: {node.id} is not allowed in strategies")
        elif isinstance(node, ast.Attribute) and node.attr in ("__import__", "__builtins__"):
            raise StrategyCodeError(f"{name}:{node.lineno}: .{node.attr} is not allowed in strategies")
    return imported


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
            if package and m.startswith(package + ".") and m not in seen and _find_spec(m) is not None:
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

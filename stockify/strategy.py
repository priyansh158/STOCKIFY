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
import inspect
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

BANNED_MODULES = {"time", "random", "secrets", "os", "subprocess", "socket", "requests", "httpx", "shutil",
                  "pathlib", "io", "urllib", "http", "threading", "multiprocessing", "asyncio", "sqlite3"}
BANNED_CALLS = {"open", "eval", "exec", "compile", "__import__", "input"}
BANNED_ATTRS = {("datetime", "now"), ("datetime", "today"), ("datetime", "utcnow"), ("date", "today")}


class StrategyCodeError(ValueError):
    pass


def check_source(source: str, name: str = "<strategy>") -> None:
    """Refuse wall-clock, randomness, file, process and network access."""
    tree = ast.parse(source, filename=name)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for m in mods:
                if m.split(".")[0] in BANNED_MODULES:
                    raise StrategyCodeError(f"{name}:{node.lineno}: import of {m!r} is not allowed in strategies")
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in BANNED_CALLS:
                raise StrategyCodeError(f"{name}:{node.lineno}: {f.id}() is not allowed in strategies")
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and (f.value.id, f.attr) in BANNED_ATTRS:
                raise StrategyCodeError(f"{name}:{node.lineno}: {f.value.id}.{f.attr}() is not allowed; use ctx.now()")


def load(ref: str, params: dict | None = None) -> Strategy:
    """'package.module:ClassName' → an instance, after the source check."""
    module_name, cls_name = ref.split(":")
    module = importlib.import_module(module_name)
    check_source(inspect.getsource(module), module_name)
    cls = getattr(module, cls_name, None)
    if cls is None or not (isinstance(cls, type) and issubclass(cls, Strategy)):
        raise StrategyCodeError(f"{ref} is not a Strategy subclass")
    return cls()

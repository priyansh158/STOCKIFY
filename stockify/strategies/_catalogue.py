"""The strategy catalogue (M3.14): what the /tests form can offer, and checks of a run's params.

Python strategies are discovered from this package (non-underscore modules) and loaded through
`strategy.load`, so the A2 §4.4 source check runs on each. C++ strategies are the names the engine module
registers (STOCKSIM_STRATEGY); their params mirror the Python originals they port.

Every entry carries `display_name`, the strategy's name for a person ("Opening Range Breakout"; a class declares it as
`DISPLAY_NAME`, a C++ strategy has it in CPP_DISPLAY_NAMES). `display_name_of` is the one lookup for it: a ref that
declares none (a user's own script) is made readable by `stockify.display.humanize`.

A params schema is a list of `{"name", "label", "type", "default", "required", "help"}` (`label` is the
form's plain name for it), with `type` one of
PARAM_TYPES. Values are checked the way the strategies read them (`int(...)`, `bool(...)`, `Decimal(str(...))`).
An `int` or `decimal` entry may also carry `"min"` and/or `"max"` (decimal strings: the smallest and largest value accepted) together with
`"unit"` (what the number is, with an example); a value outside the bounds is refused and the error states the unit.

`schema_of` is the lookup every run config passes through (stockify.runconfig, REG 5.3): one strategy by its
ref, without listing the catalogue or loading the engine.
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil
import re
import sys
from decimal import Decimal, InvalidOperation

from stockify import display
from stockify import strategy as S

PARAM_TYPES = ("int", "bool", "str", "decimal", "symbol", "hhmm", "underlying")

_HHMM = re.compile(r"^\s*\d{1,2}:\d{1,2}(:\d{1,2})?\s*$")
_SYMBOL = re.compile(r"^([A-Z]+:)?\S.*$")
_REF = re.compile(r"^stockify\.strategies\.(?!_)\w+:\w+$")     # a non-underscore module of this package


# The C++ strategies' names (their Python originals' names, marked).
CPP_DISPLAY_NAMES = {"sma_cross_cpp": "SMA Crossover (C++)", "orb_cpp": "Opening Range Breakout (C++)"}


def _cpp_params() -> dict[str, list[dict]]:
    from stockify.strategies.opening_range_breakout import OpeningRangeBreakout
    from stockify.strategies.sma_cross import SmaCross
    return {"sma_cross_cpp": SmaCross.PARAMS, "orb_cpp": OpeningRangeBreakout.PARAMS}


def _first_line(doc: str | None) -> str:
    return (doc or "").strip().splitlines()[0].strip() if (doc or "").strip() else ""


def python_strategies() -> list[dict]:
    """Every reference strategy in stockify/strategies, sorted by ref."""
    import stockify.strategies as pkg
    out = []
    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name.startswith("_"):
            continue
        S.check_module(f"{pkg.__name__}.{info.name}")   # before the import: top-level code never runs unchecked
        module = importlib.import_module(f"{pkg.__name__}.{info.name}")
        for cls_name, cls in inspect.getmembers(module, inspect.isclass):
            if cls is S.Strategy or not issubclass(cls, S.Strategy) or cls.__module__ != module.__name__:
                continue
            ref = f"{module.__name__}:{cls_name}"
            S.load(ref)  # the A2 §4.4 source check
            out.append({"kind": "python", "ref": ref, "name": info.name, "display_name": display_name_of("python", ref),
                        "doc": _first_line(module.__doc__) or _first_line(cls.__doc__),
                        "params": list(getattr(cls, "PARAMS", []))})
    return sorted(out, key=lambda s: s["ref"])


def cpp_strategies() -> list[dict]:
    """The engine's registered C++ strategies that have a params schema here; empty when the engine is not built.
    A registered strategy without a schema (e.g. the benchmark's noop_cpp) is a tool, not a user strategy, and is
    left out."""
    from stockify import engine
    if not engine.available():
        return []
    known = _cpp_params()
    return [{"kind": "cpp", "ref": name, "name": name, "display_name": display_name_of("cpp", name),
             "doc": "C++ port", "params": list(known[name])}
            for name in engine.load().strategies() if name in known]


def catalogue() -> list[dict]:
    return python_strategies() + cpp_strategies()


def schema_for(kind: str, ref: str) -> list[dict] | None:
    """The params schema of a strategy, or None when the catalogue does not know it."""
    for s in catalogue():
        if s["kind"] == kind and s["ref"] == ref:
            return s["params"]
    return None


def _cpp_classes() -> dict[str, type]:
    from stockify.strategies.opening_range_breakout import OpeningRangeBreakout
    from stockify.strategies.sma_cross import SmaCross
    return {"sma_cross_cpp": SmaCross, "orb_cpp": OpeningRangeBreakout}


def _class_of(kind: str, ref: str) -> type | None:
    """The strategy class of a catalogue ref (a C++ name gives the Python original it ports), or None when it is not a
    catalogue strategy (a user's own module, a tool such as _margin_watch, an unknown C++ name, a ref that does not
    load). No engine, and no module is imported unchecked."""
    if not isinstance(ref, str):
        return None
    if kind == "cpp":
        return _cpp_classes().get(ref)
    if kind != "python" or not _REF.match(ref):
        return None
    module_name, cls_name = ref.split(":")
    module = sys.modules.get(module_name)
    if module is None:
        try:
            S.check_module(module_name)       # before the import: top-level code never runs unchecked
            import kite_client  # noqa: F401 - as strategy.load: the Kite SDK's client class is paper only (LCB 5.7)
            module = importlib.import_module(module_name)
        except (S.StrategyCodeError, ImportError):
            return None                       # the run fails where it loads the strategy, with that error
    cls = getattr(module, cls_name, None)
    if not isinstance(cls, type) or cls is S.Strategy or not issubclass(cls, S.Strategy)             or cls.__module__ != module.__name__:
        return None
    return cls


def display_name_of(kind: str, ref: object) -> str:
    """The strategy's name as a person reads it: the class's `DISPLAY_NAME` (found for a catalogue strategy and for
    the wrapper tools), the C++ table, else the class name of the ref made readable. Never empty, never raises, and
    imports nothing that is not already loaded or a checked catalogue module."""
    if not isinstance(ref, str) or not ref.strip():
        return "Unnamed"
    if kind == "cpp":
        return CPP_DISPLAY_NAMES.get(ref) or display.strategy_ref_words(kind, ref)
    cls = _class_of(kind, ref)
    if cls is None and isinstance(ref, str) and ref.startswith("stockify.strategies._"):
        mod = sys.modules.get(ref.split(":")[0])        # a tool such as _margin_watch: not a catalogue strategy, but ours
        cls = getattr(mod, ref.split(":")[-1], None) if mod is not None else None
        if cls is None:
            try:
                S.check_module(ref.split(":")[0])
                cls = getattr(importlib.import_module(ref.split(":")[0]), ref.split(":")[-1], None)
            except Exception:  # noqa: BLE001 — a name is a nicety
                cls = None
    declared = getattr(cls, "DISPLAY_NAME", "") if isinstance(cls, type) else ""
    return declared if isinstance(declared, str) and declared.strip() else display.strategy_ref_words(kind, ref)


def schema_of(kind: str, ref: str) -> list[dict] | None:
    """The params schema of one catalogue strategy by its ref, or None when it is not a catalogue strategy (a
    user's own module, a tool such as _margin_watch, a C++ name without a schema, a ref that does not load).
    The same schemas as `catalogue()`, found without listing it: no engine, and no module is imported unchecked."""
    cls = _class_of(kind, ref)
    return None if cls is None else list(getattr(cls, "PARAMS", []))


def bar_interval_of(kind: str, ref: str) -> int | None:
    """The bar interval (seconds) a catalogue strategy subscribes to when it has no `interval_s` param: the class's
    declared `BAR_INTERVAL_S` (the strategy's own `subscribe_bars` call reads it). None when it declares none."""
    cls = _class_of(kind, ref)
    v = getattr(cls, "BAR_INTERVAL_S", None) if cls is not None else None
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def _bound_problem(p: dict, v) -> str | None:  # noqa: ANN001
    """A value outside the entry's `min`/`max`, said with the entry's `unit` (the value already passed
    `_type_problem`)."""
    if ("max" not in p and "min" not in p) or p["type"] not in ("int", "decimal"):
        return None
    number = Decimal(int(v)) if p["type"] == "int" else Decimal(str(v))
    unit = f": it is {p['unit']}" if p.get("unit") else ""
    if "min" in p and number < Decimal(p["min"]):
        return f"{v!r} is below {p['min']}{unit}"
    if "max" in p and number > Decimal(p["max"]):
        return f"{v!r} is above {p['max']}{unit}"
    return None


def _type_problem(t: str, v) -> str | None:  # noqa: ANN001
    if t == "int":
        if isinstance(v, bool):
            return None  # int(True) works, as the strategies read it
        if isinstance(v, int):
            return None
        if isinstance(v, float) and v.is_integer():
            return None
        if isinstance(v, str):
            try:
                int(v)
                return None
            except ValueError:
                pass
        return "is not a whole number"
    if t == "bool":
        return None if isinstance(v, bool) else "is not true or false"
    if t == "decimal":
        if isinstance(v, bool):
            return "is not a decimal number"
        try:
            d = Decimal(str(v))
        except InvalidOperation:
            return "is not a decimal number"
        return None if d.is_finite() else "is not a decimal number"
    if t in ("str", "underlying"):
        return None if isinstance(v, str) and v.strip() else "is not a non-empty text"
    if t == "symbol":
        return None if isinstance(v, str) and _SYMBOL.match(v.strip() or "") else "is not a symbol like NSE:RELIANCE"
    if t == "hhmm":
        if not isinstance(v, str) or not _HHMM.match(v):
            return "is not a time like 15:10"
        parts = [int(p) for p in v.strip().split(":")]
        if parts[0] > 23 or any(p > 59 for p in parts[1:]):
            return "is not a time like 15:10"
        return None
    return f"has an unknown schema type {t!r}"


def validate_params(schema: list[dict], params: dict) -> list[str]:
    """Problems with `params` against `schema`: unknown keys, missing required ones, wrong types, values above
    an entry's `max`."""
    if not isinstance(params, dict):
        return ["params must be an object"]
    problems = []
    by_name = {p["name"]: p for p in schema}
    for k in sorted(params):
        if k not in by_name:
            problems.append(f"{k}: unknown parameter")
    for p in schema:
        if p["name"] not in params or params[p["name"]] is None:
            if p.get("required"):
                problems.append(f"{p['name']}: required")
            continue
        why = _type_problem(p["type"], params[p["name"]]) or _bound_problem(p, params[p["name"]])
        if why:
            problems.append(f"{p['name']}: {why}")
    return problems

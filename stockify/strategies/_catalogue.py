"""The strategy catalogue (M3.14): what the /tests form can offer, and checks of a run's params.

Python strategies are discovered from this package (non-underscore modules) and loaded through
`strategy.load`, so the A2 §4.4 source check runs on each. C++ strategies are the names the engine module
registers (STOCKSIM_STRATEGY); their params mirror the Python originals they port.

A params schema is a list of `{"name", "label", "type", "default", "required", "help"}` (`label` is the
form's plain name for it), with `type` one of
PARAM_TYPES. Values are checked the way the strategies read them (`int(...)`, `bool(...)`, `Decimal(str(...))`).
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil
import re
from decimal import Decimal, InvalidOperation

from stockify import strategy as S

PARAM_TYPES = ("int", "bool", "str", "decimal", "symbol", "hhmm", "underlying")

_HHMM = re.compile(r"^\s*\d{1,2}:\d{1,2}(:\d{1,2})?\s*$")
_SYMBOL = re.compile(r"^([A-Z]+:)?\S.*$")


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
        module = importlib.import_module(f"{pkg.__name__}.{info.name}")
        for cls_name, cls in inspect.getmembers(module, inspect.isclass):
            if cls is S.Strategy or not issubclass(cls, S.Strategy) or cls.__module__ != module.__name__:
                continue
            ref = f"{module.__name__}:{cls_name}"
            S.load(ref)  # the A2 §4.4 source check
            out.append({"kind": "python", "ref": ref, "name": info.name,
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
    return [{"kind": "cpp", "ref": name, "name": name, "doc": "C++ port", "params": list(known[name])}
            for name in engine.load().strategies() if name in known]


def catalogue() -> list[dict]:
    return python_strategies() + cpp_strategies()


def schema_for(kind: str, ref: str) -> list[dict] | None:
    """The params schema of a strategy, or None when the catalogue does not know it."""
    for s in catalogue():
        if s["kind"] == kind and s["ref"] == ref:
            return s["params"]
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
    """Problems with `params` against `schema`: unknown keys, missing required ones, wrong types."""
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
        why = _type_problem(p["type"], params[p["name"]])
        if why:
            problems.append(f"{p['name']}: {why}")
    return problems

"""M6.10 (WF 6.1): `sweep.yaml` (schema stockify.sweep/1), loaded, validated and expanded into run configs.

A grid is the Cartesian product of the axes (file order, last axis fastest); a random search draws `trials` distinct
grid points without replacement (a sparse partial Fisher-Yates on seeding.stream(seed, "random_search")) and returns
them in grid order. Every expanded config goes through runconfig.validate. The limits come from research.settings()
(`sweep.max_trials`); a sweep is its own family (registry.job_family_id), so `family_id` is left for the runner."""
from __future__ import annotations

import copy
import dataclasses
import difflib
import json
import math
from pathlib import Path

import yaml

from stockify import paths, research, runconfig
from stockify.research import registry, seeding

SCHEMA = "stockify.sweep/1"
KEYS = ("schema", "name", "base", "objective", "search", "workers", "axes")
NAME_MAX = 40
_U32 = 1 << 32                       # Pcg32.below takes a 32-bit bound


class SweepSpecError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class SweepSpec:
    name: str
    base: dict
    axes: dict
    search: dict
    objective: str
    workers: int | None
    source: Path | None


@dataclasses.dataclass(frozen=True)
class GridPoint:
    index: int
    params: dict
    config: dict


class _Loader(yaml.SafeLoader):
    """safe_load, plus: no aliases or anchors (an alias bomb), and a repeated mapping key is an error."""

    def compose_node(self, parent, index):  # noqa: ANN001, ANN201
        if self.check_event(yaml.AliasEvent):
            raise SweepSpecError("sweep.yaml: anchors and aliases are not allowed")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):  # noqa: ANN001, ANN201
        seen = set()
        for k, _ in node.value:
            name = self.construct_object(k, deep=True)
            if name in seen:
                raise SweepSpecError(f"sweep.yaml: key {name!r} appears twice")
            seen.add(name)
        return super().construct_mapping(node, deep)


def max_trials() -> int:
    return research.settings()["sweep"]["max_trials"]


def _known(given: dict, known, where: str) -> None:  # noqa: ANN001
    for k in given:
        if k in known:
            continue
        if k == "family_id":
            raise SweepSpecError(f"{where}: unknown key 'family_id': a sweep is its own family (sweep/<job id>); "
                                 "the base config's family_id is ignored")
        close = difflib.get_close_matches(str(k), list(known), n=1)
        hint = f"did you mean {close[0]!r}?" if close else f"known keys: {', '.join(known)}"
        raise SweepSpecError(f"{where}: unknown key {k!r} ({hint})")


def set_path(cfg: dict, dotted: str, value):  # noqa: ANN001, ANN201
    """A deep copy of `cfg` with `dotted` (keys joined by '.') set. The path must exist, except that a new key may be
    added directly under strategy.params; so `strategy.parms.x` is a refused typo."""
    out = copy.deepcopy(cfg)
    parts = dotted.split(".")
    if not dotted or "" in parts:
        raise SweepSpecError(f"axis path {dotted!r} is empty somewhere")
    node = out
    for i, part in enumerate(parts[:-1]):
        if not isinstance(node.get(part), dict):
            where = ".".join(parts[: i + 1])
            close = difflib.get_close_matches(part, [k for k, v in node.items() if isinstance(v, dict)], n=1)
            hint = f" (did you mean {close[0]!r}?)" if close else ""
            raise SweepSpecError(f"axis path {dotted!r}: {where!r} is not a section of the base config{hint}")
        node = node[part]
    last = parts[-1]
    if last not in node and parts[:-1] != ["strategy", "params"]:
        close = difflib.get_close_matches(last, list(node), n=1)
        hint = f" (did you mean {close[0]!r}?)" if close else ""
        raise SweepSpecError(f"axis path {dotted!r}: the base config has no key {last!r}{hint}")
    node[last] = value
    return out


def _search(raw) -> dict:  # noqa: ANN001
    if raw is None:
        raw = {"kind": "grid"}
    if not isinstance(raw, dict) or raw.get("kind") not in ("grid", "random"):
        raise SweepSpecError("search: must be {kind: grid} or {kind: random, trials: N, seed: S}")
    known = ("kind",) if raw["kind"] == "grid" else ("kind", "trials", "seed")
    _known(raw, known, "search")
    if raw["kind"] == "random":
        for k, lo in (("trials", 1), ("seed", 0)):
            v = raw.get(k)
            if isinstance(v, bool) or not isinstance(v, int) or v < lo or v >= 2 ** 64:
                raise SweepSpecError(f"search.{k}: required, a whole number >= {lo}")
        if raw["trials"] > max_trials():
            raise SweepSpecError(f"search.trials {raw['trials']} is above research.json sweep.max_trials "
                                 f"({max_trials()})")
    return dict(raw)


def _axes(raw) -> dict:  # noqa: ANN001
    if not isinstance(raw, dict) or not raw:
        raise SweepSpecError("axes: needs at least one 'dotted.path: [values]' entry")
    for path, vals in raw.items():
        if not isinstance(path, str):
            raise SweepSpecError(f"axes: key {path!r} must be a dotted path")
        if not isinstance(vals, list) or not vals:
            raise SweepSpecError(f"axes.{path}: must be a non-empty list of values")
        for v in vals:
            if isinstance(v, float):
                raise SweepSpecError(f"axes.{path}: {v!r} is a float; write decimals as strings, like '0.05'")
        if len({json.dumps(v, sort_keys=True) for v in vals}) != len(vals):
            raise SweepSpecError(f"axes.{path}: repeats a value, which would make two equal trials")
    return {k: list(v) for k, v in raw.items()}


def load_spec(path: Path) -> SweepSpec:
    path = Path(path)
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_Loader)  # noqa: S506 — a SafeLoader subclass
    except SweepSpecError:
        raise
    except (OSError, yaml.YAMLError) as e:
        raise SweepSpecError(f"{path.name}: cannot be read as YAML: {e}") from e
    if not isinstance(raw, dict):
        raise SweepSpecError("sweep.yaml: must be a mapping")
    if raw.get("schema") != SCHEMA:
        raise SweepSpecError(f"schema: must be {SCHEMA!r}")
    _known(raw, KEYS, "sweep.yaml")
    name = raw.get("name")
    if not paths.safe_segment(name, NAME_MAX):
        raise SweepSpecError(f"name: must be 1-{NAME_MAX} letters, digits, '.', '_' or '-' (a safe path segment)")
    base = raw.get("base")
    if isinstance(base, str):
        bp = path.parent / base
        try:
            base = json.loads(bp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise SweepSpecError(f"base: cannot read run config {bp}: {e}") from e
    if not isinstance(base, dict):
        raise SweepSpecError("base: must be a path to a run.json or an inline run config")
    try:
        base = runconfig.validate(base)       # normalised, so an axis can reach a defaulted section (policies, costs)
    except runconfig.RunConfigError as e:
        raise SweepSpecError(f"base: {e}") from e
    objective = raw.get("objective", "sharpe")
    if objective not in registry.OBJECTIVES:
        raise SweepSpecError(f"objective: must be one of {list(registry.OBJECTIVES)}, not {objective!r}")
    workers = raw.get("workers")
    if workers is not None and (isinstance(workers, bool) or not isinstance(workers, int) or workers < 1):
        raise SweepSpecError("workers: null or a whole number >= 1")
    spec = SweepSpec(name=name, base=base, axes=_axes(raw.get("axes")), search=_search(raw.get("search")),
                     objective=objective, workers=workers, source=path)
    size = math.prod(len(v) for v in spec.axes.values())
    if size >= _U32:
        raise SweepSpecError(f"the grid has {size} points; the search space is too large to sample")
    expand(spec)                      # every trial config must pass runconfig.validate (and the size cap holds)
    return spec


def _point_params(axes: dict, n: int) -> dict:
    """The n-th grid point: a mixed-radix number, the last axis the fastest digit."""
    params = {}
    for path, vals in reversed(list(axes.items())):
        n, r = divmod(n, len(vals))
        params[path] = vals[r]
    return dict(reversed(list(params.items())))


def _chosen(size: int, trials: int, seed: int) -> list[int]:
    """`trials` distinct indices of range(size): a partial Fisher-Yates over a sparse swap table, sorted."""
    rng = seeding.stream(seed, "random_search")
    swapped: dict[int, int] = {}
    out = []
    for i in range(trials):
        j = i + rng.below(size - i)
        out.append(swapped.get(j, j))
        swapped[j] = swapped.get(i, i)
    return sorted(out)


def expand(spec: SweepSpec) -> list[GridPoint]:
    size = math.prod(len(v) for v in spec.axes.values())
    if spec.search["kind"] == "grid":
        if size > max_trials():
            raise SweepSpecError(f"the grid has {size} points, above research.json sweep.max_trials "
                                 f"({max_trials()}); narrow the axes or use search kind random")
        picks = list(range(size))
    elif spec.search["trials"] >= size:
        picks = list(range(size))
    else:
        picks = _chosen(size, spec.search["trials"], spec.search["seed"])
    points = []
    for index, n in enumerate(picks):
        params = _point_params(spec.axes, n)
        cfg = copy.deepcopy(spec.base)
        for path, value in params.items():
            cfg = set_path(cfg, path, value)
        cfg["name"] = f"{spec.name}-t{index:03d}"
        cfg.pop("family_id", None)
        try:
            cfg = runconfig.validate(cfg)
        except runconfig.RunConfigError as e:
            raise SweepSpecError(f"trial {index} ({json.dumps(params)}): {e}") from e
        points.append(GridPoint(index=index, params=params, config=cfg))
    return points

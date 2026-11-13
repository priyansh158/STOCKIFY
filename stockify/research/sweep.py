"""M6.10 (WF 6.1): `sweep.yaml` (schema stockify.sweep/1), loaded, validated and expanded into run configs.

A grid is the Cartesian product of the axes (file order, last axis fastest); a random search draws `trials` distinct
grid points without replacement (a sparse partial Fisher-Yates on seeding.stream(seed, "random_search")) and returns
them in grid order. Every expanded config goes through runconfig.validate. The limits come from research.settings()
(`sweep.max_trials`); a sweep is its own family (registry.job_family_id), so `family_id` is left for the runner."""
from __future__ import annotations

import copy
import csv
import dataclasses
import difflib
import io
import json
import math
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

import yaml

from stockify import paths, research, runconfig, store, timeutil
from stockify.research import fileio, holdout, plain, pool, registry, seeding

SCHEMA = "stockify.sweep/1"
RESULT_SCHEMA = "stockify.sweep.result/1"
CONFIRM_ABOVE = 50                   # the CLI asks before launching more trials than this (D-101)
_NAME_PART_IN_JOB = 24
KEYS = ("schema", "name", "base", "objective", "search", "workers", "axes")
NAME_MAX = 40
LOCKED_AXES = ("name", "family_id", "schema", "unlock_holdout")
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


# YAML 1.1 reads an unquoted 14:30 as the base-60 integer 870. A time must stay the string the user wrote, so the
# loader's int rule is the same one without its base-60 branch.
_INT_NO_SEXAGESIMAL = re.compile(r"^(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+)$")
_Loader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in rules if tag != "tag:yaml.org,2002:int"]
    for ch, rules in yaml.SafeLoader.yaml_implicit_resolvers.items()}
_Loader.add_implicit_resolver("tag:yaml.org,2002:int", _INT_NO_SEXAGESIMAL, list("-+0123456789"))


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


def _plain(node, where: str) -> None:  # noqa: ANN001
    """The base holds JSON-like values only: a bare YAML date or timestamp would otherwise fail much later."""
    if isinstance(node, dict):
        for k, v in node.items():
            _plain(v, f"{where}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _plain(v, f"{where}[{i}]")
    elif node is not None and not isinstance(node, (int, float, str, bool)):
        raise SweepSpecError(f"{where}: {node!r} is a {type(node).__name__}, not a JSON value; quote it, "
                             "like '2026-06-01'")


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
        if path in LOCKED_AXES or path.split(".")[0] in LOCKED_AXES:
            raise SweepSpecError(f"axes.{path}: {path.split('.')[0]!r} cannot be swept (a sweep names its own runs and "
                                 "family, and the holdout is not unlocked from a sweep)")
        for v in vals:
            if isinstance(v, float):
                raise SweepSpecError(f"axes.{path}: {v!r} is a float; write decimals as strings, like '0.05'")
            if v is not None and not isinstance(v, (int, str, bool)):
                if isinstance(v, (list, dict)):
                    raise SweepSpecError(f"axes.{path}: an axis value must be a single number, text, true/false or "
                                         "null; sweep a list by naming each choice as text")
                raise SweepSpecError(f"axes.{path}: {v!r} is a {type(v).__name__}, not a number, string, true/false "
                                     "or null; quote it, like '2026-06-01'")
        if len({json.dumps(v, sort_keys=True) for v in vals}) != len(vals):
            raise SweepSpecError(f"axes.{path}: repeats a value, which would make two equal trials")
    return {k: list(v) for k, v in raw.items()}


def load_spec(path: Path) -> SweepSpec:
    path = Path(path)
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_Loader)  # noqa: S506 — a SafeLoader subclass
    except SweepSpecError:
        raise
    except (OSError, yaml.YAMLError, UnicodeDecodeError) as e:
        raise SweepSpecError(f"{path.name}: cannot be read as YAML: {e}") from e
    except RecursionError as e:
        raise SweepSpecError(f"{path.name}: nested too deeply") from e
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
    _plain(base, "base")
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


# ---- running a sweep (M6.10, WF 6.2) -------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class SweepResult:
    job_id: str
    name: str
    family_id: str
    objective: str
    axes: dict
    rows: list
    best_run_id: str | None


def _job_id(name: str, now_ns: int) -> str:
    part = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)[:_NAME_PART_IN_JOB].rstrip(".") or "run"
    stamp = datetime.fromtimestamp(now_ns / 1e9, tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    while True:                                                    # unique among jobs and research folders
        job_id = f"sweep-{part}-{stamp}-{secrets.token_hex(3)}"
        if store.get_research_job(job_id) is None and not paths.research_path(job_id).exists():
            return job_id


def _finite(x):  # noqa: ANN001, ANN202
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _metrics(run_id: str) -> dict:
    """The result columns of a finished run, read from its stored metrics: None for a missing or non-finite one."""
    try:
        m = json.loads((store.get_run(run_id) or {}).get("metrics_json") or "{}")
    except ValueError:
        m = {}
    m = m if isinstance(m, dict) else {}
    a, b = m.get("nav_start_paise"), m.get("nav_end_paise")
    ints = all(isinstance(v, int) and not isinstance(v, bool) for v in (a, b))
    trades = m.get("trades")
    return {"sharpe": _finite(m.get("sharpe")), "net_pnl_paise": b - a if ints else None,
            "trades": trades if isinstance(trades, int) and not isinstance(trades, bool) else None,
            "max_drawdown": _finite(m.get("max_drawdown"))}


def _csv(axes: dict, rows: list[dict]) -> str:
    tail = ["run_id", "status", "objective_value", "sharpe", "net_pnl_paise", "trades", "max_drawdown", "error"]
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["index", *axes, *tail])
    for r in rows:
        cells = [r["index"], *(r["params"][a] for a in axes), *(r[c] for c in tail)]
        w.writerow(["" if c is None else repr(c) if isinstance(c, float) else c for c in cells])
    return out.getvalue()


def run_sweep(spec: SweepSpec, *, unlock_reason: str | None = None, workers: int | None = None,
              pool_run=None, now_ns: int | None = None) -> SweepResult:  # noqa: ANN001
    """Run every grid point through the pool (role `trial`) as one job and one family of its own. A locked holdout
    (no `unlock_reason`) or a pool that raises marks the job `failed` with the message and re-raises; a trial that
    fails is a row with its error, not an exception. `pool_run` is looked up late (pool.run_all) so tests can patch."""
    points = expand(spec)
    now = timeutil.now_ns() if now_ns is None else now_ns
    job_id = _job_id(spec.name, now)
    family = registry.job_family_id("sweep", job_id)
    registry.ensure_family(family, spec.objective)
    folder = paths.research_path(job_id)
    store.create_research_job({
        "job_id": job_id, "kind": "sweep", "name": spec.name, "family_id": family, "status": "running",
        "created_ns": now, "folder": str(folder),
        "spec_json": json.dumps({"name": spec.name, "objective": spec.objective, "search": spec.search,
                                 "axes": spec.axes, "workers": spec.workers, "trials": len(points),
                                 "base": {k: v for k, v in spec.base.items() if k != "unlock_holdout"},
                                 "unlock_given": unlock_reason is not None},
                                sort_keys=True, allow_nan=False)})
    try:
        jobs = []
        for p in points:
            cfg = copy.deepcopy(p.config)
            cfg["family_id"] = family
            cfg.pop("unlock_holdout", None)
            if unlock_reason is not None:
                cfg["unlock_holdout"] = unlock_reason
            cfg = runconfig.validate(cfg)
            holdout.enforce(cfg)                                   # fails closed before any run is made
            jobs.append(pool.PoolJob(cfg, role="trial", job_id=job_id))
        outcomes = (pool_run or pool.run_all)(jobs, workers=workers if workers is not None else spec.workers)
        if len(outcomes) != len(jobs):
            raise SweepSpecError(f"the pool returned {len(outcomes)} outcomes for {len(jobs)} runs")
        rows = []
        for p, o in zip(points, outcomes):
            row = {"index": p.index, "params": p.params, "run_id": o.run_id, "status": o.status,
                   "objective_value": None, "sharpe": None, "net_pnl_paise": None, "trades": None,
                   "max_drawdown": None, "error": o.error}
            if o.status == "done" and o.run_id:
                row.update(_metrics(o.run_id))
                row["objective_value"] = registry.objective_value(store.get_run(o.run_id) or {}, spec.objective)
            elif o.status == "done":
                row.update(status="failed", error="the run did not report its folder")
            elif row["error"] is None:
                row["error"] = "the run failed"
            rows.append(row)
        best = registry.select_best(rows, spec.objective)
        res = SweepResult(job_id, spec.name, family, spec.objective, spec.axes, rows,
                          best["run_id"] if best else None)
        doc = json.dumps({"schema": RESULT_SCHEMA, "job_id": job_id, "name": spec.name, "family_id": family,
                          "objective": spec.objective, "axes": spec.axes, "best_run_id": res.best_run_id,
                          "rows": rows}, sort_keys=True, indent=2, allow_nan=False)
        folder.mkdir(parents=True, exist_ok=True)
        fileio.atomic_write_text(folder / "sweep.json", doc + "\n")
        fileio.atomic_write_text(folder / "results.csv", _csv(spec.axes, rows))
    except BaseException as e:
        try:
            store.update_research_job(job_id, status="failed", finished_ns=timeutil.now_ns(),
                                      error=f"{type(e).__name__}: {e}"[:500])
        except Exception:  # noqa: BLE001 — a failed bookkeeping write must not hide the error that caused it
            pass
        raise
    store.update_research_job(job_id, status="done", finished_ns=timeutil.now_ns(), state_json=doc)
    return res


def ranked(res: SweepResult) -> list[dict]:
    """Rows best first: eligible rows by objective (ties by grid index), then the rest by grid index."""
    ok = [r for r in res.rows if r["status"] == "done" and r["objective_value"] is not None]
    taken = {id(r) for r in ok}
    rest = [r for r in res.rows if id(r) not in taken]
    return sorted(ok, key=lambda r: (-r["objective_value"], r["index"])) + sorted(rest, key=lambda r: r["index"])


def format_table(res: SweepResult) -> str:
    axes = list(res.axes)
    head = ["Trial", *axes, "Objective", "Sharpe", "Net P&L", "Trades", "Max drawdown", "Status"]
    body = []
    for r in ranked(res):
        status = "best" if r["run_id"] and r["run_id"] == res.best_run_id else r["status"]
        if r["status"] == "failed":
            status = f"failed: {r['error']}"
        ov = r["objective_value"]
        body.append([str(r["index"]), *(str(r["params"][a]) for a in axes),
                     "-" if ov is None else f"{ov:.4g}",
                     "-" if r["sharpe"] is None else f"{r['sharpe']:.2f}",
                     "-" if r["net_pnl_paise"] is None else plain.rupees(r["net_pnl_paise"] / 100),
                     "-" if r["trades"] is None else str(r["trades"]),
                     "-" if r["max_drawdown"] is None else f"{r['max_drawdown'] * 100:.1f}%", status])
    widths = [max(len(row[i]) for row in [head, *body]) for i in range(len(head))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in [head, *body]]
    return "\n".join([f"Sweep {res.name}: {len(res.rows)} trials, objective {res.objective}", *lines,
                      f"Job: {res.job_id}", f"Family: {res.family_id}"])

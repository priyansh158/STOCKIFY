"""Runs (M3.11, A2 §5): queue and launch a run.json, import a run folder's summary into lab.sqlite, re-run one.

A run is a folder `runs/<run_id>/` (A1 §6.3) and a `runs` row in lab.sqlite (A1 §4). The folder is the record;
the row and its child rows are a summary for the API and UI, rebuilt from the folder by `import_run`, so
importing twice is harmless.

Status flow: `queued` (by `queue`, nothing started) → `running` → `done` | `failed`. Someone else (the API)
may set `cancelled` at any time; the runner's final updates only apply while the row is still `running`, so a
cancelled row stays cancelled.

Run ids: `queue` makes `<name>-<UTC YYYYmmddTHHMMSS>-<6 hex>`; `launch` without one makes
`<name>-<bundle sha12>` (the oracle's scheme), with `-2`, `-3` … added when that folder or row already exists.
A run folder is never overwritten.
"""
from __future__ import annotations

import csv
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

from stockify import numerics, paths, runconfig, store, timeutil

IMPLS = ("engine", "oracle")


class RunFailed(RuntimeError):
    """The run stopped; the row is `failed` and, when the run got that far, events_log.txt has the traceback."""

    def __init__(self, run_id: str, error: str):
        super().__init__(f"run {run_id} failed: {error}")
        self.run_id = run_id
        self.error = error


class RunNotQueued(ValueError):
    """`launch(run_id=…)` for a row that does not exist or is not `queued`."""


# ── ids ──────────────────────────────────────────────────────────────────────

def _taken(run_id: str) -> bool:
    return paths.run_path(run_id).exists() or store.get_run(run_id) is not None


def unique_run_id(base: str) -> str:
    """`base`, or `base-2`, `base-3` … : the first whose folder and row do not exist yet."""
    if not _taken(base):
        return base
    n = 2
    while _taken(f"{base}-{n}"):
        n += 1
    return f"{base}-{n}"


def bundle_run_id(name: str, bundle: dict) -> str:
    """`<name>-<sha12 of the canonical bundle>` (as `oracle.run` names its folders), made unique."""
    import hashlib

    from stockify import bundle as bundle_mod
    digest = hashlib.sha256(bundle_mod.dumps(bundle).encode()).hexdigest()[:12]
    return unique_run_id(f"{name}-{digest}")


def queued_run_id(name: str, now_ns: int | None = None) -> str:
    """`<name>-<UTC YYYYmmddTHHMMSS>-<6 hex>`, made unique."""
    ns = timeutil.now_ns() if now_ns is None else now_ns
    t = datetime.fromtimestamp(ns / 1e9, tz=timezone.utc)
    return unique_run_id(f"{name}-{t.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}")


# ── queue and launch ─────────────────────────────────────────────────────────

def _config_json(cfg: dict) -> str:
    return numerics.dumps_canonical(cfg)


def _with_family(cfg: dict) -> dict:
    """`cfg` with its `family_id` filled by the default family when it has none (an explicit one is kept), and
    the family row ensured (M6.4)."""
    from stockify.research import registry
    if cfg.get("family_id") is None:
        cfg = {**cfg, "family_id": registry.default_family_id(cfg)}
    registry.ensure_family(cfg["family_id"])
    return cfg


def _check_role(role, job_id) -> None:  # noqa: ANN001
    from stockify.research import registry
    registry.check_role(role)
    if job_id is not None and not isinstance(job_id, str):
        raise ValueError(f"job_id must be a string or None, not {job_id!r}")


def queue(config, *, role: str = "trial", job_id: str | None = None) -> str:  # noqa: ANN001
    """Validate the config (RunConfigError propagates), write `runs/<id>/request.json` and a `queued` row.
    A config without a `family_id` gets the default family (written into the stored config too); `role` is one
    of registry.ROLES (else ValueError). Starts nothing; returns the run id."""
    _check_role(role, job_id)
    cfg = _with_family(runconfig.load(config))
    run_id = queued_run_id(cfg["name"])
    folder = paths.run_path(run_id)
    folder.mkdir(parents=True)
    text = _config_json(cfg)
    (folder / "request.json").write_text(text + "\n", encoding="utf-8", newline="\n")
    store.create_run({"run_id": run_id, "status": "queued", "name": cfg["name"], "family_id": cfg["family_id"],
                      "role": role, "job_id": job_id, "config_json": text, "folder": str(folder), "impl": "engine"})
    return run_id


def _claim(run_id: str) -> None:
    """Record this process as the run's runner (`runner.pid`), so an API restart can tell a live run from a dead
    one (stockify.proc)."""
    from stockify import proc
    proc.write_pidfile(paths.run_dir(run_id))


def _error_text(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"


def _fail(run_id: str, e: BaseException) -> RunFailed:
    err = _error_text(e)
    store.update_run(run_id, only_if_status="running", status="failed", finished_ns=timeutil.now_ns(), error=err)
    return RunFailed(run_id, err)


def launch(config=None, *, run_id: str | None = None, impl: str = "engine", report: bool = True,  # noqa: ANN001
           role: str = "trial", job_id: str | None = None) -> str:
    """Run a config to completion and return its run id.

    With `run_id`, run that `queued` row (its stored config; `config` is ignored). Without, validate `config`
    and pick `<name>-<bundle sha12>`. A failure at any stage after validation, bundle resolution included,
    leaves the row `failed` and raises RunFailed. A new row gets `role` (one of registry.ROLES, else ValueError),
    `job_id` and the default family when the config has none; a queued row keeps what `queue` stored.
    """
    from stockify import bundle as bundle_mod
    if impl not in IMPLS:
        raise ValueError(f"impl must be one of {IMPLS}, not {impl!r}")
    _check_role(role, job_id)
    if run_id is not None:
        try:
            paths.run_path(run_id)
        except ValueError as e:
            raise RunNotQueued(str(e)) from e
        row = store.get_run(run_id)
        if row is None or row["status"] != "queued":
            raise RunNotQueued(f"run {run_id} is {'unknown' if row is None else row['status']}, not queued")
        cfg = runconfig.load(json.loads(row["config_json"]))
        if not store.update_run(run_id, only_if_status="queued", status="running", started_ns=timeutil.now_ns(),
                                impl=impl):
            raise RunNotQueued(f"run {run_id} stopped being queued before it started")
        _claim(run_id)
    else:
        cfg = _with_family(runconfig.load(config))
    text = _config_json(cfg)

    try:
        b = bundle_mod.resolve_bundle(cfg)
    except Exception as e:
        if run_id is None:
            run_id = queued_run_id(cfg["name"])
            store.create_run({"run_id": run_id, "status": "running", "name": cfg["name"],
                              "family_id": cfg["family_id"], "role": role, "job_id": job_id,
                              "config_json": text, "impl": impl, "started_ns": timeutil.now_ns()})
        raise _fail(run_id, e) from e

    if run_id is None:
        run_id = bundle_run_id(cfg["name"], b)
        store.create_run({"run_id": run_id, "status": "running", "name": cfg["name"],
                          "family_id": cfg["family_id"], "role": role, "job_id": job_id,
                          "config_json": text, "impl": impl, "started_ns": timeutil.now_ns()})
        _claim(run_id)
    folder = paths.run_path(run_id)
    try:
        if impl == "engine":
            from stockify import engine as runner
            build_id = runner.build_info()["engine_build_id"]
        else:
            from stockify import oracle as runner
            build_id = None
        store.update_run(run_id, only_if_status="running", folder=str(folder), engine_build_id=build_id)
        pack_paths = [paths.lab_dir() / p["path"] for p in b["packs"]]
        runner.run_bundle(b, pack_paths, folder, report=report)
        _finish(run_id, folder)
    except Exception as e:
        raise _fail(run_id, e) from e
    return run_id


def _finish(run_id: str, folder: Path) -> None:
    """running → done with the folder's summary. The row keeps the runner's own wall times (the manifest's are
    for import_run); a row cancelled meanwhile stays cancelled and gets no child rows."""
    fields, children = _summarise(folder)
    for k in ("status", "started_ns", "finished_ns"):
        fields.pop(k, None)
    store.finish_run(run_id, {**fields, "status": "done", "finished_ns": timeutil.now_ns()}, **children)


# ── import ───────────────────────────────────────────────────────────────────

_INT_COLUMNS = {
    "orders.csv": {"order_id", "parent_id", "instrument_id", "qty", "price_paise", "trigger_paise", "protection_bp",
                   "t_submit_ns", "t_activate_ns", "filled_qty", "avg_price_paise"},
    "fills.csv": {"fill_id", "order_id", "t_ns", "instrument_id", "qty", "price_paise"},
    "equity.csv": {"nav_paise", "cash_paise", "margin_blocked_paise", "gross_pnl_paise", "charges_paise"},
    "charges.csv": {"row_no", "order_id", "instrument_id", "turnover_paise", "brokerage", "stt", "exchange", "sebi",
                    "stamp", "gst", "dp", "other", "total"},
    "margin_daily.csv": {"underlying_id", "scan", "somc", "elm", "addons", "total"},
}
CHARGE_COMPONENTS = ("turnover_paise", "brokerage", "stt", "exchange", "sebi", "stamp", "gst", "dp", "other")


def read_csv(path: Path) -> list[dict]:
    """A run CSV as typed dicts: its integer columns as int, empty cells as None, the rest as str."""
    ints = _INT_COLUMNS.get(path.name, set())
    with path.open(encoding="utf-8", newline="") as f:
        return [{k: (None if v == "" else int(v) if k in ints else v) for k, v in r.items()} for r in csv.DictReader(f)]


def _summarise(folder: Path) -> tuple[dict, dict]:
    """The runs-row fields and child rows a finished run folder holds."""
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    metrics = json.loads((folder / "metrics.json").read_text(encoding="utf-8"))
    fields = {"folder": str(folder), "bundle_sha256": manifest.get("bundle_sha256"),
              "engine_build_id": manifest.get("engine_build_id"),
              "impl": "oracle" if manifest.get("engine") == "oracle" else "engine",
              "metrics_json": json.dumps(metrics, sort_keys=True),
              "labels_json": json.dumps(sorted(manifest.get("labels", [])))}
    if manifest.get("started_ns") is not None:
        fields["started_ns"] = manifest["started_ns"]
        fields["finished_ns"] = manifest["started_ns"] + manifest.get("duration_ms", 0) * 1_000_000
    children = {
        "orders": read_csv(folder / "orders.csv"),
        "fills": read_csv(folder / "fills.csv"),
        "equity": [{"trade_date": r["trade_date"], "nav_paise": r["nav_paise"], "cash_paise": r["cash_paise"],
                    "margin_paise": r["margin_blocked_paise"]} for r in read_csv(folder / "equity.csv")],
        "charges": [{"row_no": r["row_no"], "kind": r["kind"], "order_id": r["order_id"], "trade_date": r["trade_date"],
                     "components_json": json.dumps({c: r[c] for c in CHARGE_COMPONENTS}, sort_keys=True),
                     "total_paise": r["total"]} for r in read_csv(folder / "charges.csv")],
    }
    return fields, children


def import_run(folder) -> str:  # noqa: ANN001
    """Rebuild the `runs` row and child rows from a finished run folder; returns the run id (the folder name).
    A new row is `done`; an existing row keeps a `cancelled` status, any other becomes `done`. The family is the
    folder's `family_id`; a pre-M6 folder has none, so the stored row's family is kept (a new row gets the default
    family). An existing row keeps its `role` and `job_id`."""
    folder = Path(folder)
    run_id = folder.name
    if paths.run_path(run_id).resolve() != folder.resolve():
        raise ValueError(f"{folder} is not a run folder in {paths.runs_dir()}")
    fields, children = _summarise(folder)
    cfg = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    row = store.get_run(run_id)
    if cfg.get("family_id") is None:
        cfg = {**cfg, "family_id": (row or {}).get("family_id")}
    cfg = _with_family(cfg)
    fields.update(name=cfg.get("name"), family_id=cfg["family_id"], config_json=_config_json(cfg))
    if row is None:
        store.create_run({"run_id": run_id, "status": "done", **fields})
    else:
        if row["status"] != "cancelled":
            fields["status"] = "done"
        store.update_run(run_id, **fields)
    store.replace_run_rows(run_id, **children)
    return run_id


# ── re-run (DT-03, I-8) ──────────────────────────────────────────────────────

def rerun(run_id: str) -> tuple[str, bool]:
    """Run a finished run again from its folder: the archived bundle.json exactly (never re-resolved), the
    packs it names, the same impl. Returns (new run id, determinism.json reproduced byte for byte)."""
    import hashlib
    try:
        src = paths.run_path(run_id)
    except ValueError as e:
        raise RunFailed(run_id, str(e)) from e
    try:
        bundle_text = (src / "bundle.json").read_bytes()
        manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
        cfg = json.loads((src / "run.json").read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise RunFailed(run_id, f"not a finished run folder: {e.filename} is missing") from e
    from stockify.oracle import sod_state
    try:
        bundle = sod_state.loads_bundle(bundle_text)      # its sod_state is checked like a state text (depth, ints)
    except sod_state.StateError as e:
        raise RunFailed(run_id, str(e)) from e
    pack_paths = [paths.lab_dir() / p["path"] for p in bundle["packs"]]
    missing = [str(p) for p in pack_paths if not p.exists()]
    if missing:
        raise RunFailed(run_id, f"pack file missing: {', '.join(missing)}")
    impl = "oracle" if manifest.get("engine") == "oracle" else "engine"
    new_id = unique_run_id(f"{run_id}-rerun")
    folder = paths.run_path(new_id)
    src_row = store.get_run(run_id) or {}
    cfg = _with_family({**cfg, "family_id": src_row.get("family_id") or cfg.get("family_id")})   # the source's family
    store.create_run({"run_id": new_id, "status": "running", "name": cfg.get("name"), "role": "rerun",
                      "family_id": cfg["family_id"], "config_json": _config_json(cfg), "impl": impl,
                      "folder": str(folder), "started_ns": timeutil.now_ns()})
    _claim(new_id)
    try:
        if impl == "engine":
            from stockify import engine as runner
        else:
            from stockify import oracle as runner
        runner.run_bundle(bundle, pack_paths, folder, report=False)
        new_sha = hashlib.sha256((folder / "bundle.json").read_bytes()).hexdigest()
        if new_sha != hashlib.sha256(bundle_text).hexdigest():
            raise RuntimeError(f"the re-run's bundle.json hashes to {new_sha}, not to the archived bundle's")
        _finish(new_id, folder)
    except Exception as e:
        raise _fail(new_id, e) from e
    same = (src / "determinism.json").read_bytes() == (folder / "determinism.json").read_bytes()
    return new_id, same


# margin_daily.csv's header before D-64 added the `step` column (MEZ 4.9).
MARGIN_HEADER_BEFORE_D64 = "trade_date,underlying_id,model,scan,somc,elm,addons,total"
STEP_COLUMN_NOTE = "margin_daily.csv gained the step column, D-64"


def step_column_only(run_old: str, run_new: str) -> bool:
    """Whether a re-run of a folder saved before D-64 differs from it only by margin_daily.csv's added `step`
    column (final I-3): determinism.json differs in margin_daily.csv alone, the old file is the one its
    determinism.json names, its header is the pre-D-64 one, the new one has today's header, and row by row the new
    file without its `step` cell is the old file."""
    import hashlib
    import io
    from stockify.oracle.artifacts import MARGIN
    if determinism_diff(run_old, run_new) != ["margin_daily.csv"]:
        return False
    old_folder, new_folder = paths.run_path(run_old), paths.run_path(run_new)
    try:
        old_bytes = (old_folder / "margin_daily.csv").read_bytes()
        hashes = json.loads((old_folder / "determinism.json").read_text(encoding="utf-8"))
        old_rows = list(csv.reader(io.StringIO(old_bytes.decode("utf-8"), newline="")))
        new_rows = list(csv.reader(io.StringIO((new_folder / "margin_daily.csv").read_text(encoding="utf-8"),
                                               newline="")))
    except (OSError, ValueError):
        return False
    if hashes.get("margin_daily.csv") != hashlib.sha256(old_bytes).hexdigest():
        return False
    if not old_rows or not new_rows or old_rows[0] != MARGIN_HEADER_BEFORE_D64.split(",")             or new_rows[0] != MARGIN.split(",") or len(old_rows) != len(new_rows):
        return False
    step = new_rows[0].index("step")
    return all(new[:step] + new[step + 1:] == old for old, new in zip(old_rows[1:], new_rows[1:]))


def determinism_diff(run_a: str, run_b: str) -> list[str]:
    """The deterministic files whose hashes differ between two run folders' determinism.json."""
    def hashes(run_id: str) -> dict:
        return json.loads((paths.run_path(run_id) / "determinism.json").read_text(encoding="utf-8"))
    a, b = hashes(run_a), hashes(run_b)
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))

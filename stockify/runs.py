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
import time
from datetime import datetime, timezone
from pathlib import Path

from stockify import numerics, paths, runconfig, store

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
    return (paths.runs_dir() / run_id).exists() or store.get_run(run_id) is not None


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
    t = datetime.fromtimestamp((time.time_ns() if now_ns is None else now_ns) / 1e9, tz=timezone.utc)
    return unique_run_id(f"{name}-{t.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}")


# ── queue and launch ─────────────────────────────────────────────────────────

def _config_json(cfg: dict) -> str:
    return numerics.dumps_canonical(cfg)


def queue(config) -> str:  # noqa: ANN001
    """Validate the config (RunConfigError propagates), write `runs/<id>/request.json` and a `queued` row.
    Starts nothing; returns the run id."""
    cfg = runconfig.load(config)
    run_id = queued_run_id(cfg["name"])
    folder = paths.runs_dir() / run_id
    folder.mkdir(parents=True)
    text = _config_json(cfg)
    (folder / "request.json").write_text(text + "\n", encoding="utf-8", newline="\n")
    store.create_run({"run_id": run_id, "status": "queued", "name": cfg["name"], "family_id": cfg.get("family_id"),
                      "config_json": text, "folder": str(folder), "impl": "engine"})
    return run_id


def _error_text(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"


def _fail(run_id: str, e: BaseException) -> RunFailed:
    err = _error_text(e)
    store.update_run(run_id, only_if_status="running", status="failed", finished_ns=time.time_ns(), error=err)
    return RunFailed(run_id, err)


def launch(config=None, *, run_id: str | None = None, impl: str = "engine", report: bool = True) -> str:  # noqa: ANN001
    """Run a config to completion and return its run id.

    With `run_id`, run that `queued` row (its stored config; `config` is ignored). Without, validate `config`
    and pick `<name>-<bundle sha12>`. A failure at any stage after validation, bundle resolution included,
    leaves the row `failed` and raises RunFailed.
    """
    from stockify import bundle as bundle_mod
    if impl not in IMPLS:
        raise ValueError(f"impl must be one of {IMPLS}, not {impl!r}")
    if run_id is not None:
        row = store.get_run(run_id)
        if row is None or row["status"] != "queued":
            raise RunNotQueued(f"run {run_id} is {'unknown' if row is None else row['status']}, not queued")
        cfg = runconfig.load(json.loads(row["config_json"]))
        if not store.update_run(run_id, only_if_status="queued", status="running", started_ns=time.time_ns(),
                                impl=impl):
            raise RunNotQueued(f"run {run_id} stopped being queued before it started")
    else:
        cfg = runconfig.load(config)
    text = _config_json(cfg)

    try:
        b = bundle_mod.resolve_bundle(cfg)
    except Exception as e:
        if run_id is None:
            run_id = queued_run_id(cfg["name"])
            store.create_run({"run_id": run_id, "status": "running", "name": cfg["name"],
                              "family_id": cfg.get("family_id"), "config_json": text, "impl": impl,
                              "started_ns": time.time_ns()})
        raise _fail(run_id, e) from e

    if run_id is None:
        run_id = bundle_run_id(cfg["name"], b)
        store.create_run({"run_id": run_id, "status": "running", "name": cfg["name"],
                          "family_id": cfg.get("family_id"), "config_json": text, "impl": impl,
                          "started_ns": time.time_ns()})
    folder = paths.runs_dir() / run_id
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
    if store.update_run(run_id, only_if_status="running", status="done", finished_ns=time.time_ns(), **fields):
        store.replace_run_rows(run_id, **children)


# ── import ───────────────────────────────────────────────────────────────────

_INT_COLUMNS = {
    "orders.csv": {"order_id", "parent_id", "instrument_id", "qty", "price_paise", "trigger_paise", "protection_bp",
                   "t_submit_ns", "t_activate_ns", "filled_qty", "avg_price_paise"},
    "fills.csv": {"fill_id", "order_id", "t_ns", "instrument_id", "qty", "price_paise"},
    "equity.csv": {"nav_paise", "cash_paise", "margin_blocked_paise", "gross_pnl_paise", "charges_paise"},
    "charges.csv": {"row_no", "order_id", "instrument_id", "turnover_paise", "brokerage", "stt", "exchange", "sebi",
                    "stamp", "gst", "dp", "other", "total"},
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
    A new row is `done`; an existing row keeps a `cancelled` status, any other becomes `done`."""
    folder = Path(folder)
    run_id = folder.name
    fields, children = _summarise(folder)
    cfg = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    fields.update(name=cfg.get("name"), family_id=cfg.get("family_id"), config_json=_config_json(cfg))
    row = store.get_run(run_id)
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
    src = paths.runs_dir() / run_id
    try:
        bundle_text = (src / "bundle.json").read_bytes()
        manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
        cfg = json.loads((src / "run.json").read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise RunFailed(run_id, f"not a finished run folder: {e.filename} is missing") from e
    bundle = json.loads(bundle_text)
    pack_paths = [paths.lab_dir() / p["path"] for p in bundle["packs"]]
    missing = [str(p) for p in pack_paths if not p.exists()]
    if missing:
        raise RunFailed(run_id, f"pack file missing: {', '.join(missing)}")
    impl = "oracle" if manifest.get("engine") == "oracle" else "engine"
    new_id = unique_run_id(f"{run_id}-rerun")
    folder = paths.runs_dir() / new_id
    store.create_run({"run_id": new_id, "status": "running", "name": cfg.get("name"),
                      "family_id": cfg.get("family_id"), "config_json": _config_json(cfg), "impl": impl,
                      "folder": str(folder), "started_ns": time.time_ns()})
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


def determinism_diff(run_a: str, run_b: str) -> list[str]:
    """The deterministic files whose hashes differ between two run folders' determinism.json."""
    def hashes(run_id: str) -> dict:
        return json.loads((paths.runs_dir() / run_id / "determinism.json").read_text(encoding="utf-8"))
    a, b = hashes(run_a), hashes(run_b)
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))

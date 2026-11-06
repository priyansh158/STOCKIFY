"""The Python oracle backtester (M2): a readable implementation of A4.

    from stockify.oracle import run
    res = run(config_dict_or_path)        # RunResult with DataFrames for notebooks
    res.folder                             # runs/<run_id>/
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from stockify import paths, timeutil


@dataclass
class RunResult:
    folder: Path
    run_id: str
    metrics: dict
    labels: list
    hashes: dict
    _frames: dict = field(default_factory=dict, repr=False)

    def _df(self, name: str):  # noqa: ANN202
        import pandas as pd
        if name not in self._frames:
            self._frames[name] = pd.read_csv(self.folder / name)
        return self._frames[name]

    @property
    def orders(self):  # noqa: ANN201
        return self._df("orders.csv")

    @property
    def fills(self):  # noqa: ANN201
        return self._df("fills.csv")

    @property
    def equity(self):  # noqa: ANN201
        return self._df("equity.csv")

    @property
    def charges(self):  # noqa: ANN201
        return self._df("charges.csv")


def run_bundle(bundle: dict, pack_paths: list, folder: Path, strategy=None, manifest: dict | None = None,  # noqa: ANN001
               report: bool = True) -> RunResult:
    """Run a resolved bundle over its packs and write the run folder."""
    from stockify import bundle as bundle_mod
    from stockify import strategy as strategy_mod
    from stockify.oracle import artifacts, metrics
    from stockify.oracle.runner import Engine, load_records
    folder.mkdir(parents=True, exist_ok=True)
    bundle_sha = bundle_mod.write(bundle, folder)
    # Run from exactly what bundle.json holds (canonical: sorted keys), as the engine and a re-run do (I-8),
    # so nothing a strategy can see (params, spec dicts) depends on how the dict was built in memory.
    bundle = json.loads((folder / "bundle.json").read_text(encoding="utf-8"))
    run_cfg = bundle["run"]
    (folder / "run.json").write_text(json.dumps(run_cfg, indent=2, sort_keys=True) + "\n", encoding="utf-8",
                                     newline="\n")
    if strategy is None:
        s = bundle["strategy"]
        if s["kind"] == "script":
            from stockify.strategies._script import Script
            strategy = Script(s["actions"])
        else:
            strategy = strategy_mod.load(s["ref"])
    started = timeutil.now_ns()
    engine = Engine(bundle, load_records(pack_paths), strategy)
    engine.run()
    risk_free = bundle["pricing"].get("risk_free", [])
    m = metrics.compute(capital=run_cfg["capital_paise"], equity=engine.equity, fills=engine.book.fills,
                        orders=engine.book.orders, charges=engine.book.charges, risk_free=risk_free,
                        rejects=engine.rejects, participation_ppm=engine.participation_used,
                        exposure=engine.exposure(), closes=engine.settle_closes)
    hashes = artifacts.write(folder, engine, m, {
        "run_id": folder.name, "bundle_sha256": bundle_sha, **(manifest or {}),
        "packs": [p["sha256"] for p in bundle["packs"]], "started_ns": started,
        "duration_ms": (timeutil.now_ns() - started) // 1_000_000, "engine": "oracle"})
    if report:
        from stockify.oracle import report as report_mod
        report_mod.write(folder)
    return RunResult(folder, folder.name, m, sorted(engine.labels), hashes)


def run(config, *, run_id: str | None = None) -> RunResult:  # noqa: ANN001
    """Validate the config, resolve the bundle (building packs if needed), run, write `runs/<run_id>/`."""
    from stockify import bundle as bundle_mod
    from stockify import runconfig
    cfg = runconfig.load(config)
    b = bundle_mod.resolve_bundle(cfg)
    if run_id is None:
        digest = hashlib.sha256(bundle_mod.dumps(b).encode()).hexdigest()[:12]
        run_id = f"{cfg['name']}-{digest}"
    folder = paths.run_path(run_id)
    pack_paths = [paths.lab_dir() / p["path"] for p in b["packs"]]
    return run_bundle(b, pack_paths, folder)

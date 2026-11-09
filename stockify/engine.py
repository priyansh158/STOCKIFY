"""Gateway to the compiled C++ engine module `stockify._engine`.

The module is built by `run.ps1 -BuildEngine` and copied into `stockify/`.
Importing it is deferred to first use so that the API, the daemon and the
test suite still import when the engine has not been built yet.
"""
from __future__ import annotations

import sys

_REBUILD = "rebuild with `.\\run.ps1 -BuildEngine`"


class EngineUnavailable(RuntimeError):
    """The engine module is missing or was built for another Python."""


_module = None


def load():
    """Import `stockify._engine`, checking it matches this interpreter."""
    global _module
    if _module is not None:
        return _module
    try:
        from stockify import _engine  # type: ignore[attr-defined]
    except ImportError as e:
        raise EngineUnavailable(f"stockify._engine is not built ({e}); {_REBUILD}") from e
    built_for = _engine.build_info().get("python_version", "")
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    if built_for != running:
        raise EngineUnavailable(
            f"stockify._engine was built for Python {built_for or '?'}, "
            f"this is Python {running}; {_REBUILD}"
        )
    _module = _engine
    return _module


def build_info() -> dict:
    return dict(load().build_info())


def available() -> bool:
    try:
        load()
        return True
    except EngineUnavailable:
        return False


def strategy_for(bundle: dict):  # noqa: ANN201
    """The strategy a bundle names: a Python object as the oracle builds it (a golden's script, or a loaded
    ref), or for `kind: cpp` the registered C++ strategy's name (a `str`)."""
    s = bundle["strategy"]
    if s["kind"] == "cpp":
        return s["ref"]
    if s["kind"] == "script":
        from stockify.strategies._script import Script
        return Script(s["actions"])
    from stockify import strategy as strategy_mod
    return strategy_mod.load(s["ref"])


def _write_log(folder, lines: list[str]) -> None:  # noqa: ANN001
    """events_log.txt exactly as the engine writes it: each line ends in a newline."""
    (folder / "events_log.txt").write_bytes("".join(f"{x}\n" for x in lines).encode("utf-8"))


def run_bundle(bundle: dict, pack_paths: list, folder, strategy=None, manifest: dict | None = None,  # noqa: ANN001, ANN201
               report: bool = True):
    """The engine's twin of `stockify.oracle.run_bundle`: write bundle.json and run.json, run the C++ engine
    over the packs from that bundle.json, write the run folder. Returns an `oracle.RunResult`.

    `strategy` is a Python strategy object or a registered C++ strategy's name; by default, what the bundle
    names. If the run fails (a strategy hook raises, or the engine stops), events_log.txt is written with the
    traceback at its end, no other artifact is, and the exception propagates: the caller marks the run failed."""
    import json
    import platform
    from pathlib import Path

    from stockify import bundle as bundle_mod, timeutil
    from stockify.oracle import RunResult

    if bundle.get("sod_state") is not None or "next_days" in bundle or "prev_days" in bundle:
        # VOL 5.3 (Task 6) teaches the C++ engine both; until then it must not run a day bundle as a fresh run.
        # Refused before anything is written, so no partial run folder is left behind.
        raise NotImplementedError("the C++ engine does not yet apply sod_state, prev_days or next_days "
                                  "(A1 §6.4, VOL 5.3)")
    mod = load()
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    bundle_sha = bundle_mod.write(bundle, folder)
    on_disk = json.loads((folder / "bundle.json").read_text(encoding="utf-8"))
    (folder / "run.json").write_text(json.dumps(on_disk["run"], indent=2, sort_keys=True) + "\n", encoding="utf-8",
                                     newline="\n")
    e = mod.Engine(str(folder / "bundle.json"))
    try:
        for p in pack_paths:
            e.add_pack(str(p))
        chosen = strategy if strategy is not None else strategy_for(on_disk)
        if isinstance(chosen, str):
            e.set_strategy_cpp(chosen)
        else:
            e.set_strategy_py(chosen)
        started = timeutil.now_ns()
        try:
            e.run()
        except BaseException:
            _write_log(folder, e.events_log())
            raise
        hashes = e.write_artifacts(str(folder))
        labels = list(e.labels())
    finally:
        # Drop the engine now, even on an error: a traceback keeps this frame alive, and the engine keeps its
        # packs memory-mapped (Windows will not delete a mapped file).
        e = None
    info = build_info()
    out = {"run_id": folder.name, "bundle_sha256": bundle_sha, **(manifest or {}),
           "packs": [p["sha256"] for p in on_disk["packs"]], "started_ns": started,
           "duration_ms": (timeutil.now_ns() - started) // 1_000_000, "engine": "engine",
           "engine_build_id": info["engine_build_id"], "python": sys.version.split()[0], "os": platform.platform(),
           "labels": labels}
    (folder / "manifest.json").write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    metrics = json.loads((folder / "metrics.json").read_text(encoding="utf-8"))
    del e
    if report:
        from stockify.oracle import report as report_mod
        report_mod.write(folder)
    return RunResult(folder, folder.name, metrics, labels, dict(hashes))

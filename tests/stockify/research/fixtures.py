"""Helpers for the research tests: a finished run's database rows (and, if asked, a minimal folder) without
running an engine. The metrics come from the real `oracle.metrics.compute` on the same equity."""
from __future__ import annotations

import json
from datetime import date, timedelta

import stockify.store as store
from stockify import numerics, paths
from stockify.oracle import metrics as oracle_metrics

DEFAULT_RETURNS = [0.002, -0.001, 0.003, -0.002, 0.001, 0.004, -0.003, 0.002, 0.001, -0.001]


def weekdays(start: str, n: int) -> list[str]:
    d, out = date.fromisoformat(start), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def make_run(run_id: str, family_id: str | None, *, daily_returns: list[float] | None = None,
             navs: list[int] | None = None, capital_paise: int = 100_000_000, role: str = "trial",
             status: str = "done", job_id: str | None = None, name: str | None = None, seed: int = 0,
             start: str = "2026-01-05", with_folder: bool = False) -> None:
    """A `runs` row (config_json, metrics_json) and its `run_equity` rows over consecutive weekdays."""
    if navs is None:
        nav, navs = capital_paise, []
        for r in (daily_returns if daily_returns is not None else DEFAULT_RETURNS):
            nav = round(nav * (1 + r))
            navs.append(nav)
    dates = weekdays(start, len(navs))
    equity = list(zip(dates, navs))
    name = name if name is not None else run_id
    metrics = oracle_metrics.compute(capital=capital_paise, equity=equity, fills=[], orders={}, charges=[],
                                     risk_free=[], rejects={}, participation_ppm=0, exposure=None)
    cfg = {"schema": "stockify.run/1", "name": name, "family_id": family_id, "capital_paise": capital_paise,
           "seed": seed}
    store.create_run({"run_id": run_id, "name": name, "family_id": family_id, "status": status, "role": role,
                      "job_id": job_id, "config_json": numerics.dumps_canonical(cfg),
                      "metrics_json": json.dumps(metrics, sort_keys=True)})
    store.replace_run_rows(run_id, orders=[], fills=[], charges=[], equity=[
        {"trade_date": d, "nav_paise": n, "cash_paise": n, "margin_paise": 0} for d, n in equity])
    if with_folder:
        folder = paths.run_dir(run_id)
        (folder / "run.json").write_text(json.dumps(cfg, sort_keys=True), encoding="utf-8")
        (folder / "manifest.json").write_text(json.dumps({"engine": "oracle", "labels": []}), encoding="utf-8")
        (folder / "metrics.json").write_text(json.dumps(metrics, sort_keys=True), encoding="utf-8")
        lines = ["trade_date,nav_paise"] + [f"{d},{n}" for d, n in equity]
        (folder / "equity.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")

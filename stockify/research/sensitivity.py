"""The sensitivity suite (M6.7, FW 6.3): re-run a finished backtest one change at a time and see whether its profit
survives a small worsening.

    variants(cfg)                 # baseline first, then costs / extra slippage ticks / latency / other fill models
    applicable_fill_models(cfg)   # kind -> None, or the plain reason the validator or the bundle gives
    run_suite(run_id)             # the job, the runs (role "sensitivity"), runs/<id>/sensitivity.json, the verdict
    format_table(result)          # the console table

"Fragile" is one plain rule: the baseline made money (net P&L above zero) and the net P&L is below zero when every
cost is multiplied by `fragile_cost_scale`, or when `fragile_extra_ticks` extra slippage ticks are added. A baseline
at or below zero is `already_losing`, not fragile. `robust` is earned only when BOTH checks ran and neither
flipped; when nothing flipped and a check could not run (a failed or not applicable variant) the verdict is
`inconclusive` (D-100). A check that is the baseline itself (a baseline already at costs ×1.5, say) uses the
baseline's own net P&L. Net P&L is `nav_end - nav_start`, in paise. A variant run takes
no part in N (role `sensitivity`), and the source run's own `unlock_holdout` is never inherited: a job over the
holdout carries the one reason it was started with.
"""
from __future__ import annotations

import copy
import json
import math
import secrets
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone

from stockify import bundle, numerics, paths, runconfig, store
from stockify.research import fileio, holdout, plain, pool

AXES = ("baseline", "costs", "slippage", "latency", "fill_model")
STATUSES = ("baseline", "done", "failed", "not_applicable")
VERDICTS = ("fragile", "robust", "inconclusive", "already_losing")
SCHEMA = "stockify.sensitivity/1"
_NAME_PART_IN_JOB = 30
_BASELINE_KEY = "baseline"


class SensitivityError(ValueError):
    """The run cannot be put through the suite (unknown, unfinished, unreadable). The CLI exits 2."""


class SensitivityRunError(SensitivityError):
    """A failure after the job exists (the pool misbehaved): the job is marked failed. The command exits 1 for it."""


@dataclass(frozen=True)
class Variant:
    key: str
    axis: str
    label: str
    config: dict


@dataclass(frozen=True)
class SensitivityRow:
    key: str
    axis: str
    label: str
    run_id: str | None
    status: str
    net_pnl_paise: int | None
    sharpe: float | None
    delta_net_paise: int | None
    error: str | None


@dataclass(frozen=True)
class SensitivityResult:
    run_id: str
    job_id: str
    rows: list
    baseline_net_paise: int
    fragile: bool
    verdict: str
    reasons: list


def _settings() -> dict:
    from stockify import research
    return research.settings()["sensitivity"]


def _fragile_settings(s: dict) -> tuple[str, int]:
    """(fragile_cost_scale, fragile_extra_ticks) from the settings; a missing or unreadable one is an error, never a
    skipped check."""
    try:
        scale, ticks = s["fragile_cost_scale"], s["fragile_extra_ticks"]
        Decimal(scale)
        if isinstance(ticks, bool) or not isinstance(ticks, int) or ticks < 0:
            raise ValueError(ticks)
    except (KeyError, TypeError, ValueError, InvalidOperation) as e:
        raise SensitivityError("research.json sensitivity needs fragile_cost_scale (a decimal string) and "
                               f"fragile_extra_ticks (a whole number): {type(e).__name__} {e}") from e
    return scale, ticks


def scale_text(scale) -> str:  # noqa: ANN001
    """A cost scale in its one shortest spelling ("1.50" and "1.5" are the same variant)."""
    return format(Decimal(str(scale)).normalize(), "f")


def variant_key(axis: str, value) -> str:  # noqa: ANN001
    """The key of the variant on `axis` (costs: a scale, slippage: extra ticks, latency: ms, fill_model: a kind)."""
    if axis == "costs":
        return f"costs_x{scale_text(value)}"
    return {"slippage": f"slip_plus{value}", "latency": f"latency_{value}", "fill_model": f"fill_{value}"}[axis]


def _normalised(cfg: dict) -> tuple[dict, str | None]:
    """(the validator's normal form, None) or (cfg unchanged, the validator's plain text)."""
    try:
        return runconfig.validate(copy.deepcopy(cfg)), None
    except runconfig.RunConfigError as e:
        return cfg, str(e)


def _named(cfg: dict, key: str) -> dict:
    """`cfg` renamed `<name>-<key>`: the name part is cut so the whole is at most 64 characters (a safe segment
    stays safe: the key's characters are letters, digits, `_` and `.`, and it ends the name)."""
    suffix = "-" + key
    out = copy.deepcopy(cfg)
    out["name"] = str(cfg["name"])[:max(1, 64 - len(suffix))] + suffix
    return out


def _canon(cfg: dict) -> str:
    return numerics.dumps_canonical(cfg)


def variants(cfg: dict, settings: dict | None = None) -> list[Variant]:
    """The baseline (the stored config without its holdout unlock), then one change per variant. A variant whose
    normalised config equals the baseline's is dropped. A variant the validator refuses is kept as it is: it becomes
    a `not_applicable` row when the suite runs."""
    s = settings if settings is not None else _settings()
    frag_scale, frag_ticks = _fragile_settings(s)
    scales = list(s["cost_scale"]) + ([frag_scale] if scale_text(frag_scale) not in
                                       [scale_text(x) for x in s["cost_scale"]] else [])
    ticks_list = list(s["extra_ticks"]) + ([frag_ticks] if frag_ticks not in s["extra_ticks"] else [])
    base = copy.deepcopy(cfg)
    base["unlock_holdout"] = None
    base_norm, _ = _normalised(base)
    base_canon = _canon(base_norm)
    out = [Variant(_BASELINE_KEY, "baseline", "Baseline (as run)", base)]
    seen = {base_canon}

    def add(key: str, axis: str, label: str, change) -> None:  # noqa: ANN001
        c = copy.deepcopy(base)
        change(c)
        norm, _ = _normalised(c)
        canon = _canon(norm)
        if canon in seen:
            return
        seen.add(canon)
        out.append(Variant(key, axis, label, _named(c, key)))

    for scale in scales:
        text = scale_text(scale)
        add(variant_key("costs", scale), "costs", f"All costs ×{text}",
            lambda c, v=text: c.setdefault("costs", {}).update(scale=v))
    base_ticks = base.get("fill_model", {}).get("slippage", {}).get("extra_ticks", 0)
    for ticks in ticks_list:
        word = "tick" if ticks == 1 else "ticks"
        add(variant_key("slippage", ticks), "slippage", f"Slippage +{ticks} extra {word}",
            lambda c, t=base_ticks + ticks: c["fill_model"]["slippage"].update(extra_ticks=t))
    for ms in s["latency_ms"]:
        add(variant_key("latency", ms), "latency", f"Latency {ms} ms", lambda c, v=ms: c.update(latency_ms=v))
    for kind in runconfig.FILL_KINDS:
        add(variant_key("fill_model", kind), "fill_model", f"Fill model {kind}", lambda c, k=kind: c["fill_model"].update(kind=k))
    return out


def applicable_fill_models(cfg: dict) -> dict[str, str | None]:
    """For every fill kind: None when the validator and a dry bundle resolve accept the config with that kind, else
    the error text (nothing is re-encoded here: the rules are the validator's and the resolver's). The packs are
    resolved ONCE (a spec pack is built at most once) and pinned by hash for every kind's check."""
    out: dict[str, str | None] = {}
    try:
        pinned = copy.deepcopy(runconfig.validate(copy.deepcopy(cfg)))
        pinned["data"] = {"packs": [{"sha256": row["sha256"]} for row in bundle.resolve_packs(pinned)]}
    except (runconfig.RunConfigError, bundle.BundleError, ValueError, KeyError, OSError) as e:
        text = str(e) or type(e).__name__
        return {kind: text for kind in runconfig.FILL_KINDS}
    for kind in runconfig.FILL_KINDS:
        c = copy.deepcopy(pinned)
        c["fill_model"]["kind"] = kind
        try:
            bundle.resolve_bundle(runconfig.validate(c))
            out[kind] = None
        except (runconfig.RunConfigError, bundle.BundleError, ValueError, KeyError, OSError) as e:
            out[kind] = str(e) or type(e).__name__
    return out


# ---- reading a finished run --------------------------------------------------------------------------------------

def _finite(x) -> float | None:  # noqa: ANN001
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _result_of(run_id: str) -> tuple[int | None, float | None]:
    """(net P&L in paise, Sharpe) of a run from its stored metrics; (None, None) when it has none."""
    row = store.get_run(run_id)
    try:
        m = json.loads(row["metrics_json"]) if row and row.get("metrics_json") else {}
        return int(m["nav_end_paise"]) - int(m["nav_start_paise"]), _finite(m.get("sharpe"))
    except (KeyError, TypeError, ValueError):
        return None, None


def _job_id(name: str, now_ns: int) -> str:
    part = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)[:_NAME_PART_IN_JOB].rstrip(".") or "run"
    stamp = datetime.fromtimestamp(now_ns / 1e9, tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"sens-{part}-{stamp}-{secrets.token_hex(3)}"


def _money(paise: int) -> str:
    return plain.rupees(paise / 100)


# ---- the suite ----------------------------------------------------------------------------------------------------

def run_suite(run_id: str, *, workers: int | None = None, unlock_reason: str | None = None,
              pool_run=None, now_ns: int | None = None) -> SensitivityResult:  # noqa: ANN001
    """Run every variant of a finished run through `pool_run`. SensitivityError for an unknown or unfinished run;
    HoldoutLocked (a RunConfigError) when the run reaches the locked holdout and no reason is given."""
    from stockify import timeutil
    src = store.get_run(run_id)
    if src is None:
        raise SensitivityError(f"there is no run {run_id!r}")
    if src.get("status") != "done":
        raise SensitivityError(f"run {run_id} is not finished (it is {src.get('status')!r}): only a finished run "
                               "can be put through the sensitivity suite")
    try:
        stored = json.loads(src["config_json"])
        cfg = runconfig.validate(stored)
    except (TypeError, ValueError, runconfig.RunConfigError) as e:
        raise SensitivityError(f"run {run_id}: its stored config cannot be read ({e})") from e
    holdout.enforce({**cfg, "unlock_holdout": unlock_reason})            # raises HoldoutLocked before anything is made
    base_net, base_sharpe = _result_of(run_id)
    if base_net is None:
        raise SensitivityError(f"run {run_id} has no stored result to compare with")
    s = _settings()
    _fragile_settings(s)                                                  # a missing one is an error, up front
    now = timeutil.now_ns() if now_ns is None else now_ns
    job_id = _job_id(str(src.get("name") or cfg["name"]), now)
    family = src.get("family_id") or cfg.get("family_id")
    planned = variants(cfg, s)
    store.create_research_job({"job_id": job_id, "kind": "sensitivity", "name": str(src.get("name") or cfg["name"]),
                               "family_id": family, "status": "running", "created_ns": now,
                               "folder": str(paths.run_path(run_id)),
                               "spec_json": json.dumps({"run_id": run_id, "variants": [v.key for v in planned[1:]]},
                                                       sort_keys=True)})
    try:                                    # from here on a failure closes the job as failed, never leaves it running
        fills = applicable_fill_models(cfg)
        rows: dict[str, SensitivityRow] = {}
        runnable: list[Variant] = []
        for v in planned[1:]:
            _, problem = _normalised(v.config)
            if problem is None and v.axis == "fill_model":
                problem = fills.get(v.config["fill_model"]["kind"])
            if problem is not None:
                rows[v.key] = SensitivityRow(v.key, v.axis, v.label, None, "not_applicable", None, None, None, problem)
            else:
                runnable.append(v)
        jobs = [pool.PoolJob({**v.config, "unlock_holdout": unlock_reason}, role="sensitivity", job_id=job_id)
                for v in runnable]
        outcomes = (pool_run or pool.run_all)(jobs, workers=workers) if jobs else []   # looked up late: patchable
        if len(outcomes) != len(jobs):
            raise SensitivityRunError(f"the pool returned {len(outcomes)} outcomes for {len(jobs)} runs")
        for v, o in zip(runnable, outcomes):
            net, sharpe = _result_of(o.run_id) if o.status == "done" and o.run_id else (None, None)
            if o.status == "done" and net is None:
                rows[v.key] = SensitivityRow(v.key, v.axis, v.label, o.run_id, "failed", None, None, None,
                                             "the run finished but has no stored result")
            elif o.status == "done":
                rows[v.key] = SensitivityRow(v.key, v.axis, v.label, o.run_id, "done", net, sharpe, net - base_net,
                                             None)
            else:
                rows[v.key] = SensitivityRow(v.key, v.axis, v.label, o.run_id, "failed", None, None, None,
                                             o.error or "the run failed")
        ordered = [SensitivityRow(_BASELINE_KEY, "baseline", planned[0].label, run_id, "baseline", base_net,
                                  base_sharpe, 0, None)] + [rows[v.key] for v in planned[1:]]
        verdict, reasons = _verdict(base_net, rows, s)
        res = SensitivityResult(run_id, job_id, ordered, base_net, verdict == "fragile", verdict, reasons)
        doc = json.dumps({"schema": SCHEMA, "run_id": run_id, "job_id": job_id, "verdict": verdict,
                          "fragile": res.fragile, "reasons": reasons, "baseline_net_paise": base_net,
                          "fragile_cost_scale": s["fragile_cost_scale"],
                          "fragile_extra_ticks": s["fragile_extra_ticks"],
                          "rows": [r.__dict__ for r in ordered]}, sort_keys=True, indent=2, allow_nan=False)
        note = None
        folder = paths.run_path(run_id)
        if folder.is_dir():
            fileio.atomic_write_text(folder / "sensitivity.json", doc + "\n")
        else:
            note = "the run folder is gone, so sensitivity.json was not written; the result is kept on the job"
    except BaseException as e:
        store.update_research_job(job_id, status="failed", finished_ns=timeutil.now_ns(),
                                  error=f"{type(e).__name__}: {e}"[:500])
        raise
    store.update_research_job(job_id, status="done", finished_ns=timeutil.now_ns(), state_json=doc, error=note)
    return res


def _verdict(base_net: int, rows: dict, s: dict) -> tuple[str, list[str]]:
    """fragile when either required check flipped the sign; else inconclusive when one could not run; else robust.
    A check whose variant is the baseline itself (dropped as a duplicate) uses the baseline's net P&L."""
    if base_net <= 0:
        return "already_losing", [f"The run already lost money ({_money(base_net)}), so a worse case proves nothing: "
                                  "it is not called fragile."]
    frag_scale, frag_ticks = _fragile_settings(s)
    tests = ((variant_key("costs", frag_scale), f"all costs ×{scale_text(frag_scale)}"),
             (variant_key("slippage", frag_ticks), f"{frag_ticks} extra slippage tick{'' if frag_ticks == 1 else 's'}"))
    flipped, unrun, reasons = [], [], []
    for key, what in tests:
        row = rows.get(key)
        if row is None:                                   # dropped as the baseline itself
            net = base_net
        elif row.status == "done":
            net = row.net_pnl_paise
        else:
            unrun.append(what)
            reasons.append(f"The {what} case could not be checked ({row.error or 'it did not run'}).")
            continue
        if net < 0:
            flipped.append(what)
            reasons.append(f"With {what} the net result goes from {_money(base_net)} to {_money(net)}.")
        else:
            reasons.append(f"With {what} it still makes {_money(net)}.")
    if flipped:
        return "fragile", reasons
    if unrun:
        return "inconclusive", [*reasons, "Not called robust: a check that decides it did not run."]
    return "robust", reasons


def format_table(res: SensitivityResult) -> str:
    """The rows and the verdict as plain text."""
    head = ("Variant", "Status", "Net P&L", "Change", "Sharpe")
    body = []
    for r in res.rows:
        status = r.status.replace("_", " ")
        body.append((r.label, status,
                     _money(r.net_pnl_paise) if r.net_pnl_paise is not None else "-",
                     (("+" if r.delta_net_paise > 0 else "") + _money(r.delta_net_paise))
                     if r.delta_net_paise is not None and r.status != "baseline" else "-",
                     f"{r.sharpe:.2f}" if r.sharpe is not None else "-"))
    widths = [max(len(x[i]) for x in (head, *body)) for i in range(5)]

    def line(cells) -> str:  # noqa: ANN001
        return "  ".join(c.ljust(w) if i < 2 else c.rjust(w) for i, (c, w) in enumerate(zip(cells, widths))).rstrip()

    out = [f"Sensitivity of {res.run_id} (job {res.job_id})", "", line(head), line(["-" * w for w in widths])]
    out += [line(b) for b in body]
    for r in res.rows:
        if r.error:
            out.append(f"  {r.label}: {r.error}")
    word = {"fragile": "FRAGILE", "robust": "robust", "inconclusive": "inconclusive (a check did not run)",
            "already_losing": "already losing"}[res.verdict]
    out += ["", f"Verdict: {word}", *(f"  {x}" for x in res.reasons)]
    return "\n".join(out)

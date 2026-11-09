"""`run.json`: parse and validate a run configuration (M2.1, A1 §6.1).

    cfg = runconfig.load("examples/runs/sma_reliance_daily.json")   # or a dict

Returns a new dict with every default filled in and every decimal string
converted exactly (participation → ppm, bps slippage → bp), so the oracle,
the C++ engine (M3) and paper sessions (M5) read one normalised form. A bad
config raises `RunConfigError` naming the field; nothing runs on a guess.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from stockify import numerics, paths

SCHEMA = "stockify.run/1"
FILL_KINDS = ("bar", "snapshot_ltp", "snapshot_quote", "eod_close")
SLIPPAGE_KINDS = ("none", "fixed_ticks", "random_ticks", "bps")
# VOL 5.1 (D-66, A4 §4.5): "run" seeds both PCG32 streams once from `seed`; "per_day" re-creates them from each
# trading day's own seed (bundle days[].seed), as paper sessions do, so day by day equals the whole period.
SEED_MODES = ("run", "per_day")
# span_exact and staircase read NSE's SPAN files from the bundle (CAM 4.3). Both engines run them (MEZ 4.9 oracle,
# MEZ 4.10 engine), so the staircase is the default; goldens stay pinned to flat (golden.build_bundle).
# <block name="margin-models" same-as="docs/plan/A1_schemas.md:margin-models" same-as-mode="sequence" same-as-pattern='"(?P<value>[a-z_]+)"'>
MARGIN_MODELS = ("flat", "spanlite", "span_exact", "staircase")
# </block>
DEFAULTS = {
    "family_id": None,
    "price_basis": None,
    "latency_ms": 0,
    "costs": {"broker": "zerodha", "allow_unverified": False, "auto_square_off_fee": True, "dp_charges": True},
    "margin": {"model": "staircase"},
    "policies": {"preopen": "queue", "cas": "none", "freeze": "slice", "max_slices": 10, "rms_square_off": False},
    "mark": "ltp",
    "seed": 0,
    "unlock_holdout": None,
}
FILL_DEFAULTS = {"limit_touch_policy": "through", "participation": "0.10", "depth_take": "0.50",
                 "slippage": {"kind": "none"}, "half_spread_ticks": 1}


class RunConfigError(ValueError):
    pass


def seed_mode(cfg: dict) -> str:
    """A validated config's seed mode: "run" or "per_day". The normalised form leaves the default out, so read it
    through here, never as cfg["seed_mode"] (VOL 5.1)."""
    return cfg.get("seed_mode", "run")


def _need(cond: bool, where: str, msg: str) -> None:
    if not cond:
        raise RunConfigError(f"{where}: {msg}")


def _choice(value, options, where: str):  # noqa: ANN001, ANN202
    _need(value in options, where, f"{value!r} is not one of {', '.join(map(str, options))}")
    return value


def _int(value, where: str, minimum: int | None = None) -> int:  # noqa: ANN001
    _need(isinstance(value, int) and not isinstance(value, bool), where, f"must be an integer, got {value!r}")
    if minimum is not None:
        _need(value >= minimum, where, f"must be >= {minimum}")
    return value


def _merge(defaults: dict, given: dict | None) -> dict:
    out = copy.deepcopy(defaults)
    for k, v in (given or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _strategy(s: dict) -> dict:
    _need(isinstance(s, dict), "strategy", "must be an object")
    kind = _choice(s.get("kind"), ("python", "cpp", "script"), "strategy.kind")
    if kind == "python":
        ref = s.get("ref", "")
        _need(isinstance(ref, str) and ref.count(":") == 1 and all(ref.split(":")), "strategy.ref",
              "must be 'package.module:ClassName'")
    elif kind == "cpp":
        _need(isinstance(s.get("ref"), str) and s["ref"], "strategy.ref", "must name a registered C++ strategy")
    else:
        _need(isinstance(s.get("actions"), list), "strategy.actions", "a script strategy needs a list of actions")
    params = s.get("params", {})
    _need(isinstance(params, dict), "strategy.params", "must be an object")
    return {**s, "params": params}


def _packs(data: dict) -> list[dict]:
    _need(isinstance(data, dict) and isinstance(data.get("packs"), list) and data["packs"], "data.packs",
          "must be a non-empty list")
    out = []
    for i, p in enumerate(data["packs"]):
        where = f"data.packs[{i}]"
        _need(isinstance(p, dict) and (("spec" in p) != ("sha256" in p)), where, "needs exactly one of spec or sha256")
        if "sha256" in p:
            _need(isinstance(p["sha256"], str) and len(p["sha256"]) == 64, where + ".sha256", "must be 64 hex digits")
        out.append(p)
    return out


def _fill(f: dict) -> dict:
    _need(isinstance(f, dict), "fill_model", "must be an object")
    f = _merge(FILL_DEFAULTS, f)
    _choice(f.get("kind"), FILL_KINDS, "fill_model.kind")
    _choice(f["limit_touch_policy"], ("through", "touch"), "fill_model.limit_touch_policy")
    for key in ("participation", "depth_take"):
        try:
            f[f"{key}_ppm"] = numerics.to_ppm(str(f[key]))
        except numerics.NumericError as e:
            raise RunConfigError(f"fill_model.{key}: {e}") from e
        _need(0 < f[f"{key}_ppm"] <= 1_000_000, f"fill_model.{key}", "must be in (0, 1]")
    slip = f["slippage"]
    _choice(slip.get("kind"), SLIPPAGE_KINDS, "fill_model.slippage.kind")
    if slip["kind"] in ("fixed_ticks", "random_ticks"):
        _int(slip.get("k"), "fill_model.slippage.k", 0)
    if slip["kind"] == "bps":
        try:
            slip["bp"] = numerics.to_bp(str(slip.get("x")))
        except numerics.NumericError as e:
            raise RunConfigError(f"fill_model.slippage.x: {e}") from e
    _int(f["half_spread_ticks"], "fill_model.half_spread_ticks", 0)
    return f


def validate(raw: dict) -> dict:
    _need(isinstance(raw, dict), "run", "must be a JSON object")
    _need(raw.get("schema") == SCHEMA, "schema", f"must be {SCHEMA!r}")
    _need(isinstance(raw.get("name"), str) and raw["name"], "name", "required")
    # The name becomes the run folder's name: one safe path segment (paths.safe_segment).
    _need(paths.safe_segment(raw["name"], 64), "name",
          "must be 1-64 letters, digits, '.', '_' or '-', start with a letter or digit, not end with '.', "
          "and not be a Windows device name (CON, NUL, COM1 ...)")
    fam = raw.get("family_id")
    _need(fam is None or (isinstance(fam, str) and len(fam) <= 128
                          and all(paths.safe_segment(part, 64) for part in fam.split("/"))), "family_id",
          "must be up to 128 characters: segments like a name, separated by '/'")
    cfg = _merge(DEFAULTS, {k: v for k, v in raw.items()})
    cfg["strategy"] = _strategy(raw.get("strategy"))
    cfg["data"] = {**raw.get("data", {}), "packs": _packs(raw.get("data"))}
    _choice(cfg["price_basis"], ("raw", "adjusted"), "price_basis")
    _int(cfg.get("capital_paise"), "capital_paise", 1)
    cfg["fill_model"] = _fill(raw.get("fill_model"))
    _int(cfg["latency_ms"], "latency_ms", 0)
    _choice(cfg["costs"]["allow_unverified"], (True, False), "costs.allow_unverified")
    _choice(cfg["margin"]["model"], MARGIN_MODELS, "margin.model")
    pol = cfg["policies"]
    _choice(pol["preopen"], ("queue", "reject"), "policies.preopen")
    _choice(pol["cas"], ("none", "close_price"), "policies.cas")
    _choice(pol["freeze"], ("slice", "reject"), "policies.freeze")
    _int(pol["max_slices"], "policies.max_slices", 1)
    _choice(pol["rms_square_off"], (True, False), "policies.rms_square_off")
    _choice(cfg["mark"], ("ltp", "mid"), "mark")
    _int(cfg["seed"], "seed", 0)
    _need(cfg["seed"] < 2 ** 64, "seed", "must fit in 64 bits")
    # The default is left out of the normalised form, so a run config (and its bundle) without seed_mode, or
    # with "run", is byte for byte what it was before VOL 5.1.
    if _choice(cfg.pop("seed_mode", "run"), SEED_MODES, "seed_mode") == "per_day":
        cfg["seed_mode"] = "per_day"
    if cfg["fill_model"]["kind"] == "eod_close":
        _need(cfg["price_basis"] == "raw", "price_basis", "eod_close runs use NSE files, basis raw")
    return cfg


def load(source) -> dict:  # noqa: ANN001
    """A path to run.json, or a dict."""
    if isinstance(source, (str, Path)):
        try:
            source = json.loads(Path(source).read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise RunConfigError(f"run.json is not valid JSON: {e}") from e
    return validate(source)

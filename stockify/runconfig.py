"""`run.json`: parse and validate a run configuration (M2.1, A1 §6.1).

    cfg = runconfig.load("examples/runs/sma_reliance_daily.json")   # or a dict

Returns a new dict with every default filled in and every decimal string
converted exactly (participation → ppm, bps slippage → bp), so the oracle,
the C++ engine (M3) and paper sessions (M5) read one normalised form. A bad
config raises `RunConfigError` naming the field; nothing runs on a guess.
"""
from __future__ import annotations

import copy
import difflib
import json
import re
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
MARGIN_MODELS = ("flat", "spanlite", "span_exact", "staircase", "live_with_fallback")
# </block>
MARGIN_OPTIONAL = ("live_fresh_s",)             # MEZ 5.1: how old a journaled live margin answer may be; not a default
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
# REG 5.1: the keys a run config may carry, section by section. Any other key is refused (`_known`), so a
# misspelt key is an error and never a silent default. The sets are the tables above plus the keys that have
# no default; the keys of a pack spec are the pack builder's own tables (stockify.data.packs).
REQUIRED = ("schema", "name", "strategy", "data", "capital_paise", "fill_model")
RUN_KEYS = (*REQUIRED, *DEFAULTS, "seed_mode")
FILL_KEYS = ("kind", *FILL_DEFAULTS)
# What `validate` adds to the normalised form, by the key it is computed from. A stored normalised config is
# validated again when its run starts (runs.launch), so a derived key is accepted when it is what its source
# gives, and refused when it is anything else.
FILL_DERIVED = {"participation": "participation_ppm", "depth_take": "depth_take_ppm"}
SLIPPAGE_KEYS = {"none": ("kind",), "fixed_ticks": ("kind", "k"), "random_ticks": ("kind", "k"),
                 "bps": ("kind", "x", "bp")}                                        # bp is derived from x
STRATEGY_KEYS = {"python": ("kind", "ref", "params"), "cpp": ("kind", "ref", "params"),
                 "script": ("kind", "actions", "params")}
DATA_KEYS = ("packs",)
PACK_KEYS = ("spec", "sha256")
# REG 5.2 (audit H-2): `x` of bps slippage is a fraction of the price written as a decimal string, like every
# other rate in a run config. It is bounded, so "5" (meant as 5 bp) is refused and does not run as 50,000 bp.
MAX_SLIPPAGE_BP = 1000
SLIPPAGE_X_UNIT = 'a fraction written as a decimal string, from "0" to "0.1" ("0.0005" = 5 bp)'


# A pack's hash names its file (packs/<sha>.evt). Exactly this and nothing else, so a config can never
# turn it into a path with separators or dots; stockify.data.packs checks again where the path is built.
_PACK_SHA = re.compile(r"[0-9a-f]{64}")


class RunConfigError(ValueError):
    """A run config that is refused. `problems` is the same refusal as a list, one entry per problem: more than
    one only for a catalogue strategy's params, which are checked together (REG 5.3)."""

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.problems = list(problems) if problems else [str(message)]


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


LIVE_FRESH_MAX_S = 86_400


def check_live_fresh(value, model: str) -> int:  # noqa: ANN001
    """MEZ 5.1: `margin.live_fresh_s` is 1 to a day, and means something only under `live_with_fallback`."""
    _need(isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= LIVE_FRESH_MAX_S,
          "margin.live_fresh_s", f"must be a whole number of seconds from 1 to {LIVE_FRESH_MAX_S}, got {value!r}")
    _need(model == "live_with_fallback", "margin.live_fresh_s",
          f"is only for margin.model 'live_with_fallback', not {model!r}")
    return value


def _known(given: dict, known, where: str) -> None:  # noqa: ANN001
    """REG 5.1: every key of `given` is one of `known`; else an error that names the key, its section and the
    closest known key."""
    known = list(known)
    for key in given:
        if key in known:
            continue
        close = difflib.get_close_matches(str(key), known, n=1)
        hint = f"did you mean {close[0]!r}?" if close else f"known keys: {', '.join(known)}"
        raise RunConfigError(f"{where}: unknown key {key!r} ({hint})")


def _derived(section: dict, key: str, value: int, where: str, source: str) -> None:
    """Set a derived key. One that is already there (a normalised config validated again) must be that value."""
    given = section.get(key, value)
    _need(isinstance(given, int) and not isinstance(given, bool) and given == value, f"{where}.{key}",
          f"is derived from {source} (here {value}): leave it out")
    section[key] = value


def _merge(defaults: dict, given: dict | None) -> dict:
    out = copy.deepcopy(defaults)
    for k, v in (given or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _strategy(s: dict) -> dict:
    _need(isinstance(s, dict), "strategy", "must be an object")
    kind = _choice(s.get("kind"), tuple(STRATEGY_KEYS), "strategy.kind")
    _known(s, STRATEGY_KEYS[kind], "strategy")
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
    _catalogue_params(kind, s.get("ref"), params)
    return {**s, "params": params}


def _catalogue_params(kind: str, ref, params: dict) -> None:  # noqa: ANN001
    """REG 5.3 (audit M-3): a catalogue strategy's params are checked against its schema here, the one place
    every way of starting a run passes through (runs.launch and queue, oracle.run, both CLIs, compare-strategies,
    the API). A strategy outside the catalogue (a user's own module, a script) has no schema and is not checked."""
    if kind == "script":
        return
    from stockify.strategies import _catalogue      # here, not at the top: it imports the strategy modules
    schema = _catalogue.schema_of(kind, ref)
    if schema is None:
        return
    problems = [f"strategy.params.{p}" for p in _catalogue.validate_params(schema, params)]
    if problems:
        raise RunConfigError("; ".join(problems), problems)


def _spec(spec: dict, where: str) -> None:
    """The keys of a pack spec, down to its instrument selectors. The values are the pack builder's to check."""
    from stockify.data import packs
    _need(isinstance(spec, dict), where, "must be an object")
    _known(spec, packs.SPEC_KEYS, where)
    selectors_known(spec.get("instruments"), where + ".instruments")


def selectors_known(selectors, where: str) -> None:  # noqa: ANN001
    """The keys of an instrument selector list (a pack spec's `instruments`, a session's `universe`), down to a
    chain's own keys. A string (the "NSE:RELIANCE" shorthand) has none. The values are `packs.resolve`'s to check."""
    from stockify.data import packs
    for n, sel in enumerate(selectors if isinstance(selectors, list) else []):
        if not isinstance(sel, dict):
            continue
        at = f"{where}[{n}]"
        _known(sel, packs.SELECTOR_KEYS, at)
        # The builder reads the first form it finds, so a second one would be dropped without a word.
        _need(len(sel) == 1, at, f"needs exactly one of {', '.join(packs.SELECTOR_KEYS)}")
        if isinstance(sel.get("chain"), dict):
            _known(sel["chain"], packs.CHAIN_KEYS, at + ".chain")


def _packs(data: dict) -> list[dict]:
    _need(isinstance(data, dict) and isinstance(data.get("packs"), list) and data["packs"], "data.packs",
          "must be a non-empty list")
    _known(data, DATA_KEYS, "data")
    out = []
    for i, p in enumerate(data["packs"]):
        where = f"data.packs[{i}]"
        _need(isinstance(p, dict), where, "needs exactly one of spec or sha256")
        _known(p, PACK_KEYS, where)
        _need(("spec" in p) != ("sha256" in p), where, "needs exactly one of spec or sha256")
        if "sha256" in p:
            _need(isinstance(p["sha256"], str) and _PACK_SHA.fullmatch(p["sha256"]) is not None, where + ".sha256",
                  "must be exactly 64 lower-case hex digits")
        else:
            _spec(p["spec"], where + ".spec")
        out.append(p)
    return out


def _fill(f: dict) -> dict:
    _need(isinstance(f, dict), "fill_model", "must be an object")
    _known(f, (*FILL_KEYS, *FILL_DERIVED.values()), "fill_model")
    f = _merge(FILL_DEFAULTS, f)
    _choice(f.get("kind"), FILL_KINDS, "fill_model.kind")
    _choice(f["limit_touch_policy"], ("through", "touch"), "fill_model.limit_touch_policy")
    for key, derived in FILL_DERIVED.items():
        try:
            ppm = numerics.to_ppm(str(f[key]))
        except numerics.NumericError as e:
            raise RunConfigError(f"fill_model.{key}: {e}") from e
        _need(0 < ppm <= 1_000_000, f"fill_model.{key}", "must be in (0, 1]")
        _derived(f, derived, ppm, "fill_model", key)
    slip = f["slippage"]
    _need(isinstance(slip, dict), "fill_model.slippage", "must be an object")
    _known(slip, SLIPPAGE_KEYS[_choice(slip.get("kind"), SLIPPAGE_KINDS, "fill_model.slippage.kind")],
           "fill_model.slippage")
    if slip["kind"] in ("fixed_ticks", "random_ticks"):
        _int(slip.get("k"), "fill_model.slippage.k", 0)
    if slip["kind"] == "bps":
        x = slip.get("x")
        _need(isinstance(x, str), "fill_model.slippage.x", f"{x!r} is not a string: it must be {SLIPPAGE_X_UNIT}")
        try:
            bp = numerics.to_bp(x)
        except OverflowError:
            bp = MAX_SLIPPAGE_BP + 1
        except numerics.NumericError as e:
            raise RunConfigError(f"fill_model.slippage.x: {e}: it must be {SLIPPAGE_X_UNIT}") from e
        _need(0 <= bp <= MAX_SLIPPAGE_BP, "fill_model.slippage.x",
              f"{x!r} is outside the range: it must be {SLIPPAGE_X_UNIT}")
        _derived(slip, "bp", bp, "fill_model.slippage", "x")
    _int(f["half_spread_ticks"], "fill_model.half_spread_ticks", 0)
    return f


def validate(raw: dict, *, packs_required: bool = True) -> dict:
    """The normalised run config. `packs_required=False` is for a paper session's config (stockify.sessionconfig):
    it has no `data` (the key is refused like any other unknown one) and its days come from a universe."""
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
    _known(raw, RUN_KEYS if packs_required else tuple(k for k in RUN_KEYS if k != "data"), "run")
    for section in ("costs", "margin", "policies"):
        _need(isinstance(raw.get(section, {}), dict), section, "must be an object")
        _known(raw.get(section, {}), (*DEFAULTS[section], *(MARGIN_OPTIONAL if section == "margin" else ())), section)
    cfg = _merge(DEFAULTS, {k: v for k, v in raw.items()})
    cfg["strategy"] = _strategy(raw.get("strategy"))
    if packs_required:
        cfg["data"] = {"packs": _packs(raw.get("data"))}
    _choice(cfg["price_basis"], ("raw", "adjusted"), "price_basis")
    _int(cfg.get("capital_paise"), "capital_paise", 1)
    cfg["fill_model"] = _fill(raw.get("fill_model"))
    _int(cfg["latency_ms"], "latency_ms", 0)
    _choice(cfg["costs"]["allow_unverified"], (True, False), "costs.allow_unverified")
    _choice(cfg["margin"]["model"], MARGIN_MODELS, "margin.model")
    if "live_fresh_s" in cfg["margin"]:
        check_live_fresh(cfg["margin"]["live_fresh_s"], cfg["margin"]["model"])
    pol = cfg["policies"]
    _choice(pol["preopen"], ("queue", "reject"), "policies.preopen")
    _choice(pol["cas"], ("none", "close_price"), "policies.cas")
    _choice(pol["freeze"], ("slice", "reject"), "policies.freeze")
    _int(pol["max_slices"], "policies.max_slices", 1)
    _choice(pol["rms_square_off"], (True, False), "policies.rms_square_off")
    _choice(cfg["mark"], ("ltp", "mid"), "mark")
    unlock = cfg["unlock_holdout"]
    _need(unlock is None or isinstance(unlock, str), "unlock_holdout",
          f"must be null or a written reason (a string), got {unlock!r}")
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

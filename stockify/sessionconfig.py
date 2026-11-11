"""`session.json`: a paper session's configuration (REG 5.1, M5.2, D-67, A1 §6.4a).

    cfg = sessionconfig.load("examples/sessions/orb_nifty.json")   # or a dict

A session config is a run config without `data` (its day comes from the universe, `bundle.resolve_session_day`),
plus `universe`: the instrument selectors of the M1.8 format. It is validated by the run config's own rules
(`runconfig.validate(..., packs_required=False)`), so every market rule, every decimal conversion and the catalogue
strategies' param checks are the same, and an unknown key at any level is refused naming the key and its section
(D-76). What differs from a run is only what a live day needs:

* `fill_model.kind` defaults to `snapshot_quote` (the live feed is quotes);
* `margin.model` defaults to `staircase` (until Task 20: `live_with_fallback`), and `margin.live_fresh_s` (default 360)
  is how old a live margin answer may be before the session falls back;
* `seed_mode` is `per_day` and `price_basis` is `raw`: both are the only values a live session can have, so any
  other is refused instead of ignored.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from stockify import runconfig

SCHEMA = "stockify.session/1"
LIVE_FRESH_S = 360
SESSION_KEYS = (*(k for k in runconfig.RUN_KEYS if k != "data"), "universe")
MARGIN_KEYS = (*runconfig.DEFAULTS["margin"], "live_fresh_s")


class SessionConfigError(ValueError):
    """A session config that is refused. `problems` is as for `RunConfigError`."""

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.problems = list(problems) if problems else [str(message)]


def _need(cond: bool, where: str, msg: str) -> None:
    if not cond:
        raise SessionConfigError(f"{where}: {msg}")


def validate(raw: dict) -> dict:
    """The normalised session config: a run config's normalised form without `data`, with `universe` and
    `margin.live_fresh_s`. A bad config raises `SessionConfigError` naming the field."""
    try:
        return _validate(raw)
    except runconfig.RunConfigError as e:
        raise SessionConfigError(str(e), e.problems) from e


def _validate(raw: dict) -> dict:
    _need(isinstance(raw, dict), "session", "must be a JSON object")
    _need(raw.get("schema") == SCHEMA, "schema", f"must be {SCHEMA!r}")
    _need("data" not in raw, "session", "a session has no `data`: its day comes from `universe`")
    runconfig._known(raw, SESSION_KEYS, "session")
    universe = raw.get("universe")
    _need(isinstance(universe, list) and universe, "universe", "must be a non-empty list of instrument selectors")
    for n, sel in enumerate(universe):
        _need(isinstance(sel, (str, dict)), f"universe[{n}]", "must be a selector object or a symbol like 'NSE:RELIANCE'")
    runconfig.selectors_known(universe, "universe")
    margin = raw.get("margin", {})
    _need(isinstance(margin, dict), "margin", "must be an object")
    runconfig._known(margin, MARGIN_KEYS, "margin")
    fresh = margin.get("live_fresh_s", LIVE_FRESH_S)
    _need(isinstance(fresh, int) and not isinstance(fresh, bool) and fresh >= 1, "margin.live_fresh_s",
          f"must be a whole number of seconds, at least 1, got {fresh!r}")
    _need(raw.get("price_basis", "raw") == "raw", "price_basis",
          f"{raw.get('price_basis')!r}: a live session's prices are raw")
    _need(raw.get("seed_mode", "per_day") == "per_day", "seed_mode",
          f"{raw.get('seed_mode')!r}: a session re-seeds each trading day, so only 'per_day'")
    fill = raw.get("fill_model", {})
    _need(isinstance(fill, dict), "fill_model", "must be an object")
    run = {k: copy.deepcopy(v) for k, v in raw.items() if k not in ("universe", "margin")}
    run.update(schema=runconfig.SCHEMA, price_basis="raw", seed_mode="per_day",
               fill_model={"kind": "snapshot_quote", **fill},
               margin={k: v for k, v in margin.items() if k != "live_fresh_s"})
    cfg = runconfig.validate(run, packs_required=False)
    cfg["schema"] = SCHEMA
    cfg["universe"] = copy.deepcopy(universe)
    cfg["margin"]["live_fresh_s"] = fresh
    return cfg


def load(source) -> dict:  # noqa: ANN001
    """A path to session.json, or a dict."""
    if isinstance(source, (str, Path)):
        try:
            source = json.loads(Path(source).read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise SessionConfigError(f"session.json is not valid JSON: {e}") from e
    return validate(source)

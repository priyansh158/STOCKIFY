"""Load and validate STOCKIFY config files (A1_schemas.md §7, invariant I-1).

Rules enforced for every file:

* the `schema` tag matches the file;
* every dated row (a dict with `effective_from`) carries `source` and a
  boolean `verified`, plus `verified_on` when `verified` is true;
* `effective_from` is an ISO date, or `"VERIFY"` (charges only, A1 §7.2):
  that row becomes effective from its `observed_on` date when an earlier row
  exists for the same `applies` key, otherwise from the 2015-01-01 floor, and
  must stay `verified: false`;
* within one dated list, rows are sorted by `effective_from` per `applies`
  key, and two rows for the same key may not share a date;
* charge rates are decimal strings converted exactly to ppb through
  `stockify.numerics` (a `<field>_ppb` key is added next to each one).

`jsonschema` is not a dependency, so the checks are written by hand.
"""
from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

from stockify import numerics

CONFIG_DIR = Path(__file__).resolve().parent

FLOOR_DATE = "2015-01-01"
VERIFY = "VERIFY"

# file name -> expected schema tag
SCHEMAS = {
    "universe.json": "stockify.universe/1",
    "charges.json": "stockify.charges/1",
    "sessions.json": "stockify.sessions/1",
    "contract_rules.json": "stockify.contract_rules/1",
    "freeze_limits.json": "stockify.freeze_limits/1",
    "broker_policy.json": "stockify.broker_policy/1",
    "margin_params.json": "stockify.margin_params/1",
    "pricing.json": "stockify.pricing/1",
    "schedule.json": "stockify.schedule/1",
    "storage.json": "stockify.storage/1",
    "sources.json": "stockify.sources/1",
    "kite_limits.json": "stockify.kite_limits/1",
    "notify.json": "stockify.notify/1",
    "research.json": "stockify.research/1",
    "paper.json": "stockify.paper/1",
}

CSV_HEADERS = {
    "calendar/nse_holidays.csv": ["date", "segments", "description", "source"],
    "calendar/special_sessions.csv": ["date", "group", "phase", "start", "end", "name", "source"],
}

# Charge fields holding a fraction as a decimal string (A1 §7.2).
CHARGE_RATE_FIELDS = (
    "pct", "buy", "sell", "rate", "pct_of_intrinsic",
    "brokerage_pct", "netted_brokerage_pct", "stt_each_side_pct_of_value",
)


class ConfigError(ValueError):
    """A config file breaks one of the rules above."""


def _iso_date(text, where: str) -> str:
    try:
        date.fromisoformat(text)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{where}: not an ISO date: {text!r}") from e
    return text


def _applies_key(row: dict) -> str:
    return json.dumps(row.get("applies", row.get("group", row.get("underlying"))), sort_keys=True)


def _check_dated_row(row: dict, where: str, allow_verify: bool) -> None:
    if "source" not in row:
        raise ConfigError(f"{where}: dated row has no 'source'")
    if not isinstance(row.get("verified"), bool):
        raise ConfigError(f"{where}: dated row needs a boolean 'verified'")
    if row["verified"]:
        _iso_date(row.get("verified_on"), f"{where}.verified_on")
    if row["effective_from"] == VERIFY:
        if not allow_verify:
            raise ConfigError(f"{where}: 'VERIFY' is only allowed in charges.json")
        if row["verified"]:
            raise ConfigError(f"{where}: a 'VERIFY' row must stay verified: false")
        _iso_date(row.get("observed_on"), f"{where}.observed_on")
    else:
        _iso_date(row["effective_from"], f"{where}.effective_from")


def _resolve_dated_list(rows: list, where: str, allow_verify: bool) -> list:
    """Validate, resolve VERIFY dates, and return rows sorted per applies key."""
    for i, row in enumerate(rows):
        _check_dated_row(row, f"{where}[{i}]", allow_verify)

    by_key: dict[str, list[dict]] = {}
    for row in rows:
        by_key.setdefault(_applies_key(row), []).append(row)

    out: list[dict] = []
    for key in sorted(by_key):
        group = by_key[key]
        dated = [r for r in group if r["effective_from"] != VERIFY]
        resolved = []
        for row in group:
            row = dict(row)
            if row["effective_from"] == VERIFY:
                earlier = any(r["effective_from"] < row["observed_on"] for r in dated)
                row["effective_from"] = row["observed_on"] if earlier else FLOOR_DATE
                row["effective_from_resolved"] = True
            resolved.append(row)
        resolved.sort(key=lambda r: r["effective_from"])
        for a, b in zip(resolved, resolved[1:]):
            if a["effective_from"] == b["effective_from"]:
                raise ConfigError(
                    f"{where}: two rows for applies={key} share effective_from {a['effective_from']}"
                )
        out.extend(resolved)
    return out


def _walk_dated(node, where: str, allow_verify: bool):
    """Resolve every list of dated rows anywhere in the document."""
    if isinstance(node, dict):
        return {k: _walk_dated(v, f"{where}.{k}", allow_verify) for k, v in node.items()}
    if isinstance(node, list):
        if node and all(isinstance(r, dict) and "effective_from" in r for r in node):
            return _resolve_dated_list(node, where, allow_verify)
        return [_walk_dated(v, f"{where}[{i}]", allow_verify) for i, v in enumerate(node)]
    return node


def _convert_charge_rates(doc: dict) -> None:
    for comp, rows in doc.get("components", {}).items():
        if not isinstance(rows, list):
            raise ConfigError(f"charges.components.{comp} must be a list")
        for i, row in enumerate(rows):
            for field in CHARGE_RATE_FIELDS:
                if field in row:
                    try:
                        row[f"{field}_ppb"] = numerics.to_ppb(row[field])
                    except numerics.NumericError as e:
                        raise ConfigError(f"charges.components.{comp}[{i}].{field}: {e}") from e


def load_json(name: str, config_dir: Path | None = None) -> dict:
    if name not in SCHEMAS:
        raise ConfigError(f"unknown config file: {name}")
    path = (config_dir or CONFIG_DIR) / name
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{name}: invalid JSON: {e}") from e
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMAS[name]:
        raise ConfigError(f"{name}: schema must be {SCHEMAS[name]!r}")
    doc = _walk_dated(doc, name.removesuffix(".json"), allow_verify=(name == "charges.json"))
    if name == "charges.json":
        _convert_charge_rates(doc)
    return doc


def load_csv(name: str, config_dir: Path | None = None) -> list[dict]:
    if name not in CSV_HEADERS:
        raise ConfigError(f"unknown config file: {name}")
    path = (config_dir or CONFIG_DIR) / name
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != CSV_HEADERS[name]:
            raise ConfigError(f"{name}: header must be {','.join(CSV_HEADERS[name])}")
        rows = list(reader)
    for i, row in enumerate(rows):
        _iso_date(row["date"], f"{name}[{i}].date")
    return rows


def load_all(config_dir: Path | None = None) -> dict:
    """Every config file, validated. Raises ConfigError on the first problem."""
    out = {name: load_json(name, config_dir) for name in SCHEMAS}
    out.update({name: load_csv(name, config_dir) for name in CSV_HEADERS})
    return out

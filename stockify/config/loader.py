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
  `stockify.numerics` (a `<field>_ppb` key is added next to each one);
* in `broker_policy.json`, an `FO` row of `mis_margin_pct` is exactly 1: it is a share of the NRML
  margin, and intraday F&O leverage is not modelled (A4 §6.5, D-80).

`jsonschema` is not a dependency, so the checks are written by hand.
"""
from __future__ import annotations

import csv
import json
import re
from datetime import date
from decimal import Decimal
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
    "system_notes.json": "stockify.system_notes/1",
    "margin_params.json": "stockify.margin_params/1",
    "pricing.json": "stockify.pricing/1",
    "schedule.json": "stockify.schedule/1",
    "storage.json": "stockify.storage/1",
    "sources.json": "stockify.sources/1",
    "kite_limits.json": "stockify.kite_limits/1",
    "notify.json": "stockify.notify/1",
    "research.json": "stockify.research/1",
    "paper.json": "stockify.paper/1",
    "fundamentals.json": "stockify.fundamentals/1",
    "candles.json": "stockify.candles/1",
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
        if any(isinstance(r, dict) and "effective_from" in r for r in node):
            # a dated list: every row must carry its date (a row without one would be skipped, and so never applied)
            first_bad = next(i for i, r in enumerate(node) if not (isinstance(r, dict) and "effective_from" in r))
            raise ConfigError(f"{where}[{first_bad}]: a row in a dated list has no effective_from")
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


def _check_mis_margin(doc: dict) -> None:
    """broker_policy.mis_margin_pct: a CM row is a share of the traded value; an FO row is a share of the NRML
    margin, and only the whole of it is modelled (A4 §6.5, D-80). Any other FO value is refused here, so that it is
    never applied as a silent scale."""
    for i, row in enumerate(doc.get("mis_margin_pct", [])):
        if not isinstance(row, dict) or (row.get("applies") or {}).get("segment") != "FO":
            continue
        where = f"broker_policy.mis_margin_pct[{i}]"
        try:
            whole = numerics.to_ppb(row.get("pct")) == numerics.to_ppb("1")
        except (ArithmeticError, TypeError, ValueError) as e:      # not a decimal string
            raise ConfigError(f"{where}.pct: {e}") from e
        if not whole:
            raise ConfigError(f"{where}: the FO row from {row.get('effective_from')} has pct {row.get('pct')!r}; only "
                              '"1" (the full NRML margin) is accepted: intraday F&O leverage is not modelled (D-80)')


# paper.json whole-number keys and their largest accepted value (RM 5.3): generous, but a typo (an hour as 3600000,
# a queue of a billion frames) is refused. A bound is a sanity limit, not a market rule.
PAPER_BOUNDS = {"clock_every_s": 3600, "dispatcher_queue_max": 10_000_000, "token_poll_s": 3600,
                "coverage_gap_tolerance_s": 3600, "commands_every_s": 60, "sod_state_warn_mb": 100_000,
                "journal.fsync_every_s": 60, "margin_poll.debounce_s": 60, "margin_poll.every_s": 86_400,
                "margin_poll.max_rps": 10, "dispatch.batch_max_items": 100_000, "dispatch.batch_max_ms": 1000,
                "engine_log.max_lines_per_min": 10_000, "engine_log.max_line_chars": 10_000}
_PLAIN_DECIMAL = re.compile(r"[0-9]+(\.[0-9]+)?")


def _bounded_int(value, where: str, key: str) -> int:  # noqa: ANN001
    top = PAPER_BOUNDS[key]
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= top:
        raise ConfigError(f"{where}: must be a whole number from 1 to {top}, got {value!r}")
    return value


NOTIFY_CHANNELS = (None, "email")                  # email only (owner, 2026-11-11)
NOTIFY_EVENTS = ("login_needed", "session_error", "parity_failure", "recorder_stall", "disk_low", "daily_summary")


def _check_notify(doc: dict) -> None:
    """notify.json (stockify.notify/1): `channel` is null or one of NOTIFY_CHANNELS, `events` has exactly the
    NOTIFY_EVENTS keys, each true or false. The old `channels` list is refused (it is an unknown key)."""
    extra = set(doc) - {"schema", "channel", "events"}
    if extra:
        raise ConfigError(f"notify.json: unknown keys {sorted(extra)}")
    if "channel" not in doc or doc["channel"] not in NOTIFY_CHANNELS:
        raise ConfigError(f"notify.json: channel must be null or one of {list(NOTIFY_CHANNELS[1:])}, "
                          f"got {doc.get('channel')!r}")
    events = doc.get("events")
    if not isinstance(events, dict) or set(events) != set(NOTIFY_EVENTS):
        raise ConfigError(f"notify.json: events must be an object with exactly {list(NOTIFY_EVENTS)}")
    for name, on in events.items():
        if not isinstance(on, bool):
            raise ConfigError(f"notify.json: events.{name} must be true or false, got {on!r}")


STORAGE_KEYS = ("retention_days", "disk_warn_free_gb", "disk_warn_free_fraction", "parquet_zstd_level",
                "log_max_mb", "log_backups", "backup")
LOG_BOUNDS = {"log_max_mb": (1, 1000), "log_backups": (0, 100)}      # a log file's size cap and how many rotated files stay
BACKUP_KEYS = ("target", "include_span", "keep_snapshots")
KEEP_SNAPSHOTS_BOUNDS = (1, 365)


def _check_storage(doc: dict) -> None:
    """storage.json (stockify.storage/1): an unknown key at any level is refused, naming it (D-76's style: a
    misspelt `include_span` must not silently keep its default). `backup` is `{target, include_span, keep_snapshots}`: `target` is null
    (no backup) or a non-empty folder (`~` and `%VAR%` allowed), `include_span` is true or false (GK 5.7),
    `keep_snapshots` is a whole number from 1 to 365 (D-147)."""
    extra = set(doc) - set(STORAGE_KEYS) - {"schema"}
    if extra:
        raise ConfigError(f"storage.json: unknown keys {sorted(extra)}; known: {list(STORAGE_KEYS)}")
    for key, (low, high) in LOG_BOUNDS.items():
        if key in doc and (isinstance(doc[key], bool) or not isinstance(doc[key], int) or not low <= doc[key] <= high):
            raise ConfigError(f"storage.json: {key} must be a whole number from {low} to {high}, got {doc[key]!r}")
    if "backup" not in doc:
        return
    cfg = doc["backup"]
    if not isinstance(cfg, dict):
        raise ConfigError(f"storage.json: backup must be an object with {list(BACKUP_KEYS)}, got {cfg!r}")
    extra = set(cfg) - set(BACKUP_KEYS)
    if extra:
        raise ConfigError(f"storage.json: backup has unknown keys {sorted(extra)}; known: {list(BACKUP_KEYS)}")
    for key in BACKUP_KEYS:
        if key not in cfg:
            raise ConfigError(f"storage.json: backup.{key} is missing")
    target = cfg["target"]
    if target is not None and (not isinstance(target, str) or not target.strip()):
        raise ConfigError(f"storage.json: backup.target must be null or a folder path, got {target!r}")
    if not isinstance(cfg["include_span"], bool):
        raise ConfigError(f"storage.json: backup.include_span must be true or false, got {cfg['include_span']!r}")
    keep, (low, high) = cfg["keep_snapshots"], KEEP_SNAPSHOTS_BOUNDS
    if isinstance(keep, bool) or not isinstance(keep, int) or not low <= keep <= high:
        raise ConfigError(f"storage.json: backup.keep_snapshots must be a whole number from {low} to {high}, got {keep!r}")


def _check_system_notes(doc: dict) -> None:
    """system_notes.json (CH 6.2): words, not market rules (no `effective_from`). A key is a system order tag
    (`orders.SYSTEM_TAGS`) or SETTLEMENT; every value is text that `clean_note` leaves unchanged; SETTLEMENT holds the
    one placeholder `{price}` (the chart fills it) and no other value or key may hold a brace."""
    from stockify.oracle.notes import clean_note
    from stockify.oracle.orders import SYSTEM_TAGS
    notes = doc.get("notes")
    if not isinstance(notes, dict):
        raise ConfigError("system_notes.json: notes must be an object")
    for tag, text in notes.items():
        where = f"system_notes.json: notes.{tag}"
        if tag not in SYSTEM_TAGS and tag != "SETTLEMENT":
            raise ConfigError(f"{where}: not a system tag (one of {', '.join(SYSTEM_TAGS)} or SETTLEMENT)")
        if not isinstance(text, str) or clean_note(text) != text:
            raise ConfigError(f"{where}: must be text that clean_note leaves unchanged (trimmed, no control "
                              "characters, at most 200 characters)")
        if tag == "SETTLEMENT":
            rest = text.replace("{price}", "")
            if text.count("{price}") != 1 or "{" in rest or "}" in rest:
                raise ConfigError(f"{where}: must contain the placeholder {{price}} once and no other brace")
        elif "{" in text or "}" in text:
            raise ConfigError(f"{where}: only SETTLEMENT may hold a placeholder")


def _check_paper(doc: dict) -> None:
    """paper.json (stockify.paper/1): every key present, whole numbers within PAPER_BOUNDS, and
    `coverage_threshold` a plain decimal string (digits and one optional dot: no sign, spaces or exponent) in (0, 1]."""
    shape = {"clock_every_s": None, "journal": ("fsync_every_s",), "dispatcher_queue_max": None,
             "dispatch": ("batch_max_items", "batch_max_ms"),
             "margin_poll": ("debounce_s", "every_s", "max_rps"), "token_poll_s": None,
             "coverage_threshold": "fraction", "coverage_gap_tolerance_s": None, "commands_every_s": None, "sod_state_warn_mb": None,
             "engine_log": ("max_lines_per_min", "max_line_chars")}
    extra = set(doc) - set(shape) - {"schema"}
    if extra:
        raise ConfigError(f"paper.json: unknown keys {sorted(extra)}")
    for key, kind in shape.items():
        if key not in doc:
            raise ConfigError(f"paper.json: {key} is missing")
        where = f"paper.json: {key}"
        if kind is None:
            _bounded_int(doc[key], where, key)
        elif kind == "fraction":
            text = doc[key]
            try:
                frac = numerics.to_ppb(text) if isinstance(text, str) and _PLAIN_DECIMAL.fullmatch(text) else None
            except (numerics.NumericError, OverflowError):
                frac = None
            if frac is None or not 0 < frac <= numerics.PPB:
                raise ConfigError(f"{where}: must be a plain decimal string in (0, 1], got {text!r}")
        else:
            sub = doc[key]
            if not isinstance(sub, dict) or set(sub) != set(kind):
                raise ConfigError(f"{where}: must be an object with exactly {list(kind)}")
            for k in kind:
                _bounded_int(sub[k], f"{where}.{k}", f"{key}.{k}")


_SIGNED_DECIMAL = re.compile(r"-?[0-9]+(\.[0-9]+)?")
# Equal to stockify.research.registry.OBJECTIVES (a test holds them equal; the registry imports the store).
RESEARCH_OBJECTIVES = ("sharpe", "sortino", "calmar", "cagr", "net_pnl")


def _research_decimal(value, where: str) -> Decimal:
    """A decimal string (digits, one optional dot, optional leading minus) as an exact Decimal. A JSON number is
    refused: a rate is never a float."""
    if not isinstance(value, str) or _SIGNED_DECIMAL.fullmatch(value) is None:
        raise ConfigError(f'research.json: {where} must be a decimal string such as "1.5", got {value!r}')
    return Decimal(value)


def _research_int(value, where: str, low: int, high: int = 10_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"research.json: {where} must be a whole number from {low} to {high}, got {value!r}")
    return value


def _research_text(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"research.json: {where} must be a non-empty string, got {value!r}")
    return value


def _research_section(doc: dict, name: str, keys: tuple) -> dict:
    sub = doc.get(name)
    if not isinstance(sub, dict):
        raise ConfigError(f"research.json: {name} must be an object with {list(keys)}")
    extra = set(sub) - set(keys)
    if extra:
        raise ConfigError(f"research.json: {name} has unknown keys {sorted(extra)}; known: {list(keys)}")
    for key in keys:
        if key not in sub:
            raise ConfigError(f"research.json: {name}.{key} is missing")
    return sub


def _research_edges(values, where: str, decimal: bool, low=None) -> None:
    """A non-empty list, strictly increasing: decimal strings (exact compare) or whole numbers."""
    if not isinstance(values, list) or not values:
        raise ConfigError(f"research.json: {where} must be a non-empty list")
    parsed = [_research_decimal(v, f"{where}[{i}]") if decimal else _research_int(v, f"{where}[{i}]", 0)
              for i, v in enumerate(values)]
    if low is not None and parsed[0] < low:
        raise ConfigError(f"research.json: {where} must not go below {low}, got {values!r}")
    if any(not a < b for a, b in zip(parsed, parsed[1:])):
        raise ConfigError(f"research.json: {where} must be strictly increasing, got {values!r}")


def _check_research(doc: dict) -> None:
    """research.json (stockify.research/1): every key present, an unknown key at any level refused naming it, rates
    and scales decimal strings, counts whole numbers within sanity bounds, edge lists strictly increasing.
    `holdout_from` is null (no holdout) or an ISO date; `sweep.max_workers` is null (cpu_count - 1) or >= 1; `sweep.max_trials` is the most trials one sweep may expand to."""
    top = ("holdout_from", "walk_forward", "bootstrap", "sensitivity", "regimes", "pbo", "sweep", "coverage", "export", "pool",
           "reporting", "web_jobs")
    extra = set(doc) - set(top) - {"schema"}
    if extra:
        raise ConfigError(f"research.json: unknown keys {sorted(extra)}; known: {list(top)}")
    for key in top:
        if key not in doc:
            raise ConfigError(f"research.json: {key} is missing")
    if doc["holdout_from"] is not None:
        _iso_date(doc["holdout_from"], "research.json: holdout_from")
        # Python 3.11+ also reads "20260801" and "2026-W31-6" as dates; the holdout is written YYYY-MM-DD only.
        if date.fromisoformat(doc["holdout_from"]).isoformat() != doc["holdout_from"]:
            raise ConfigError(f"research.json: holdout_from: not a YYYY-MM-DD date: {doc['holdout_from']!r}")

    wf = _research_section(doc, "walk_forward", ("train_days", "test_days", "step_days", "objective",
                                                         "min_oos_days"))
    for key in ("train_days", "test_days", "step_days"):
        _research_int(wf[key], f"walk_forward.{key}", 1, 10_000)
    _research_int(wf["min_oos_days"], "walk_forward.min_oos_days", 2, 10_000)    # D-102
    if wf["objective"] not in RESEARCH_OBJECTIVES:
        raise ConfigError(f"research.json: walk_forward.objective must be one of {list(RESEARCH_OBJECTIVES)}, "
                          f"got {wf['objective']!r}")

    bs = _research_section(doc, "bootstrap", ("mean_block", "resamples", "ci", "min_days"))
    if _research_decimal(bs["mean_block"], "bootstrap.mean_block") < 1:
        raise ConfigError(f"research.json: bootstrap.mean_block must be at least 1, got {bs['mean_block']!r}")
    _research_int(bs["resamples"], "bootstrap.resamples", 100, 1_000_000)
    if not 0 < _research_decimal(bs["ci"], "bootstrap.ci") < 1:
        raise ConfigError(f"research.json: bootstrap.ci must be strictly between 0 and 1, got {bs['ci']!r}")
    _research_int(bs["min_days"], "bootstrap.min_days", 1, 100_000)

    sens = _research_section(doc, "sensitivity", ("cost_scale", "extra_ticks", "latency_ms",
                                                  "fragile_cost_scale", "fragile_extra_ticks"))
    _research_edges(sens["cost_scale"], "sensitivity.cost_scale", True, low=0)
    _research_edges(sens["extra_ticks"], "sensitivity.extra_ticks", False)
    _research_edges(sens["latency_ms"], "sensitivity.latency_ms", False)
    if _research_decimal(sens["fragile_cost_scale"], "sensitivity.fragile_cost_scale") <= 0:
        raise ConfigError("research.json: sensitivity.fragile_cost_scale must be above 0")
    _research_int(sens["fragile_extra_ticks"], "sensitivity.fragile_extra_ticks", 1, 1000)

    rg = _research_section(doc, "regimes", ("reference_index", "vix_symbol", "vix_edges", "gap_edges_pct",
                                            "thin_days", "close_slack_min"))
    _research_text(rg["reference_index"], "regimes.reference_index")
    _research_text(rg["vix_symbol"], "regimes.vix_symbol")
    _research_edges(rg["vix_edges"], "regimes.vix_edges", True, low=0)
    _research_edges(rg["gap_edges_pct"], "regimes.gap_edges_pct", True)
    _research_int(rg["thin_days"], "regimes.thin_days", 1, 100_000)
    _research_int(rg["close_slack_min"], "regimes.close_slack_min", 0, 30)   # how far before the close the last VIX print may be

    pbo = _research_section(doc, "pbo", ("slices", "min_trials"))
    slices = _research_int(pbo["slices"], "pbo.slices", 4, 1000)
    if slices % 2:
        raise ConfigError(f"research.json: pbo.slices must be even, got {slices}")
    _research_int(pbo["min_trials"], "pbo.min_trials", 2, 100_000)

    sweep = _research_section(doc, "sweep", ("max_workers", "max_trials"))
    if sweep["max_workers"] is not None:
        _research_int(sweep["max_workers"], "sweep.max_workers", 1, 256)
    _research_int(sweep["max_trials"], "sweep.max_trials", 1, 1_000_000)

    exp = _research_section(doc, "export", ("max_total_mib",))     # the most an export zip may hold uncompressed
    _research_int(exp["max_total_mib"], "export.max_total_mib", 1, 16_384)
    pool = _research_section(doc, "pool", ("max_run_hours",))      # a pool child that runs longer is killed
    _research_int(pool["max_run_hours"], "pool.max_run_hours", 1, 10_000)

    rep = _research_section(doc, "reporting", ("min_days_yearly",))   # D-135: shorter runs show no yearly figure
    _research_int(rep["min_days_yearly"], "reporting.min_days_yearly", 2, 100_000)

    web = _research_section(doc, "web_jobs", ("max_research_jobs", "max_data_jobs"))   # D-145: jobs the web app starts
    _research_int(web["max_research_jobs"], "web_jobs.max_research_jobs", 1, 8)
    _research_int(web["max_data_jobs"], "web_jobs.max_data_jobs", 1, 8)

    cov = _research_section(doc, "coverage", ("ok_pct", "default_days", "max_days"))
    if not 0 < _research_decimal(cov["ok_pct"], "coverage.ok_pct") <= 100:
        raise ConfigError(f"research.json: coverage.ok_pct must be in (0, 100], got {cov['ok_pct']!r}")
    default = _research_int(cov["default_days"], "coverage.default_days", 1, 100_000)
    maximum = _research_int(cov["max_days"], "coverage.max_days", 1, 100_000)
    if default > maximum:
        raise ConfigError(f"research.json: coverage.default_days ({default}) must not exceed "
                          f"coverage.max_days ({maximum})")


def _check_fundamentals(doc: dict) -> None:
    """fundamentals.json (stockify.fundamentals/1): every key present and none unknown; the list URLs and the API
    base are https on NSE's own hosts; counts are whole numbers within sanity bounds; `balances` (the test record's
    valuation weights) decimal strings in [0, 1], no two equal as numbers, and `basket_sizes` distinct whole numbers from 5 to 50 and
    at most `top_kept`; `daily_request_budget` a whole number of requests from 1; `filings_from` a YYYY-MM-DD
    date; `session_rotate_requests` (NS 2.1) a whole number from 10 and `session_slow_s` whole seconds from 1 to 600; each
    `api.<name>` an object {path: "/api/...", warmup: an https://www.nseindia.com/ page}."""
    keys = ("nifty500_list_url", "nifty500_list_fallback_url", "liquidity_floor_paise", "liquidity_days",
            "filings_from", "basket_size", "top_kept", "balances", "basket_sizes", "corporate_action_ratios",
            "corporate_action_tolerance", "corporate_action_hold_fall", "daily_request_budget",
            "session_rotate_requests", "session_slow_s", "api")
    extra = set(doc) - set(keys) - {"schema", "note"}
    if extra:
        raise ConfigError(f"fundamentals.json: unknown keys {sorted(extra)}; known: {list(keys)}")
    for key in keys:
        if key not in doc:
            raise ConfigError(f"fundamentals.json: {key} is missing")

    def nse_url(value, where: str, prefixes: tuple) -> None:
        if not isinstance(value, str) or not value.startswith(prefixes):
            raise ConfigError(f"fundamentals.json: {where} must start with one of {list(prefixes)}, got {value!r}")

    def whole(value, where: str, low: int, high: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ConfigError(f"fundamentals.json: {where} must be a whole number from {low} to {high}, got {value!r}")

    nse_url(doc["nifty500_list_url"], "nifty500_list_url", ("https://nsearchives.nseindia.com/",))
    nse_url(doc["nifty500_list_fallback_url"], "nifty500_list_fallback_url",
            ("https://nsearchives.nseindia.com/", "https://www.niftyindices.com/"))
    whole(doc["liquidity_floor_paise"], "liquidity_floor_paise", 0, 10 ** 15)
    whole(doc["liquidity_days"], "liquidity_days", 1, 1000)
    whole(doc["basket_size"], "basket_size", 1, 500)
    whole(doc["top_kept"], "top_kept", 1, 500)
    whole(doc["daily_request_budget"], "daily_request_budget", 1, 100_000)
    whole(doc["session_rotate_requests"], "session_rotate_requests", 10, 100_000)
    whole(doc["session_slow_s"], "session_slow_s", 1, 600)
    balances = doc["balances"]
    if (not isinstance(balances, list) or not balances
            or not all(isinstance(b, str) and _SIGNED_DECIMAL.fullmatch(b) and 0 <= Decimal(b) <= 1 for b in balances)
            or len({Decimal(b) for b in balances}) != len(balances)):      # "0.5" and "0.50" are one balance
        raise ConfigError(f"fundamentals.json: balances must be a non-empty list of decimal strings from 0 "
                          f"(quality only) to 1 (valuation only), no two equal as numbers, got {balances!r}")
    sizes = doc["basket_sizes"]
    if not isinstance(sizes, list) or not sizes or len(set(map(repr, sizes))) != len(sizes):
        raise ConfigError(f"fundamentals.json: basket_sizes must be a non-empty list of distinct whole numbers, "
                          f"got {sizes!r}")
    for n in sizes:                                  # the strategy's own bounds (fundamentals_basket.py), within top_kept
        whole(n, "basket_sizes", 5, min(50, doc["top_kept"]))
    tol = doc["corporate_action_tolerance"]
    if not isinstance(tol, str) or _SIGNED_DECIMAL.fullmatch(tol) is None or not 0 < Decimal(tol) <= Decimal("0.1"):
        raise ConfigError(f"fundamentals.json: corporate_action_tolerance must be a decimal string in (0, 0.1], "
                          f"got {tol!r}")
    ratios = doc["corporate_action_ratios"]
    if (not isinstance(ratios, list) or not ratios
            or not all(isinstance(r, str) and _SIGNED_DECIMAL.fullmatch(r) and Decimal(r) > 1 for r in ratios)):
        raise ConfigError(f"fundamentals.json: corporate_action_ratios must be a non-empty list of decimal strings "
                          f"above 1, got {ratios!r}")
    t = Decimal(tol)
    for lo, hi in zip(ratios, ratios[1:]):
        if not Decimal(lo) * (1 + t) < Decimal(hi) * (1 - t):
            raise ConfigError(f"fundamentals.json: corporate_action_ratios must ascend with bands that do not overlap "
                              f"at the tolerance: {lo} and {hi}")
    hold = doc["corporate_action_hold_fall"]
    if (not isinstance(hold, str) or _SIGNED_DECIMAL.fullmatch(hold) is None
            or not 1 < Decimal(hold) < Decimal(ratios[0]) * (1 - t)):
        raise ConfigError(f"fundamentals.json: corporate_action_hold_fall must be a decimal string above 1 and below "
                          f"the smallest ratio's band, got {hold!r}")
    _iso_date(doc["filings_from"], "fundamentals.json: filings_from")
    if date.fromisoformat(doc["filings_from"]).isoformat() != doc["filings_from"]:
        raise ConfigError(f"fundamentals.json: filings_from: not a YYYY-MM-DD date: {doc['filings_from']!r}")
    api = doc["api"]
    if not isinstance(api, dict) or api.get("base") != "https://www.nseindia.com":
        raise ConfigError("fundamentals.json: api must be an object whose base is \"https://www.nseindia.com\"")
    for name, entry in api.items():
        if name == "base":
            continue
        if not isinstance(entry, dict) or set(entry) != {"path", "warmup"}:
            raise ConfigError(f"fundamentals.json: api.{name} must be an object with exactly ['path', 'warmup']")
        if not isinstance(entry["path"], str) or not entry["path"].startswith("/api/") or "?" in entry["path"]:
            raise ConfigError(f"fundamentals.json: api.{name}.path must start with /api/ and carry no query, "
                              f"got {entry['path']!r}")
        nse_url(entry["warmup"], f"api.{name}.warmup", ("https://www.nseindia.com/",))


CANDLES_KEYS = ("history_years", "rps", "rate_limit_backoff_s")
CANDLES_BOUNDS = {"history_years": (1, 20), "rps": (0, 10), "backoff_s": (1, 600)}


def _check_candles(doc: dict, config_dir: Path | None = None) -> None:
    """candles.json (stockify.candles/1, CW 1): every key present and none unknown (`note` allowed, a string);
    `history_years` a whole number from 1 to 20; `rps` a decimal string above 0, at most 10 and at most
    kite_limits.json `historical_rps` (Kite's historical limit, K-8; the file beside it, else the shipped one);
    `rate_limit_backoff_s` a non-empty list of whole seconds from 1 to 600 that never shrinks."""
    extra = set(doc) - set(CANDLES_KEYS) - {"schema", "note"}
    if extra:
        raise ConfigError(f"candles.json: unknown keys {sorted(extra)}; known: {list(CANDLES_KEYS)}")
    for key in CANDLES_KEYS:
        if key not in doc:
            raise ConfigError(f"candles.json: {key} is missing")
    if "note" in doc and not isinstance(doc["note"], str):
        raise ConfigError(f"candles.json: note must be a string, got {doc['note']!r}")
    low, high = CANDLES_BOUNDS["history_years"]
    years = doc["history_years"]
    if isinstance(years, bool) or not isinstance(years, int) or not low <= years <= high:
        raise ConfigError(f"candles.json: history_years must be a whole number from {low} to {high}, got {years!r}")
    rps = doc["rps"]
    low, high = CANDLES_BOUNDS["rps"]
    if not isinstance(rps, str) or _PLAIN_DECIMAL.fullmatch(rps) is None or not low < Decimal(rps) <= high:
        raise ConfigError(f'candles.json: rps must be a decimal string above {low} and at most {high} such as "2", '
                          f"got {rps!r}")
    limits_dir = config_dir if config_dir is not None and (config_dir / "kite_limits.json").exists() else None
    kite_rps = Decimal(str(load_json("kite_limits.json", limits_dir)["historical_rps"]))
    if Decimal(rps) > kite_rps:
        raise ConfigError(f"candles.json: rps {rps} is above kite_limits.json historical_rps {kite_rps} (K-8)")
    waits = doc["rate_limit_backoff_s"]
    low, high = CANDLES_BOUNDS["backoff_s"]
    if (not isinstance(waits, list) or not waits
            or not all(not isinstance(w, bool) and isinstance(w, int) and low <= w <= high for w in waits)
            or any(b < a for a, b in zip(waits, waits[1:]))):
        raise ConfigError(f"candles.json: rate_limit_backoff_s must be a non-empty list of whole seconds from {low} "
                          f"to {high} that never shrinks, got {waits!r}")


def load_json(name: str,config_dir: Path | None = None) -> dict:
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
    if name == "broker_policy.json":
        _check_mis_margin(doc)
    if name == "system_notes.json":
        _check_system_notes(doc)
    if name == "paper.json":
        _check_paper(doc)
    if name == "notify.json":
        _check_notify(doc)
    if name == "research.json":
        _check_research(doc)
    if name == "storage.json":
        _check_storage(doc)
    if name == "fundamentals.json":
        _check_fundamentals(doc)
    if name == "candles.json":
        _check_candles(doc, config_dir)
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

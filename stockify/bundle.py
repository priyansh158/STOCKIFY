"""`resolve_bundle(run_config) -> bundle` (M2.1, A1 §6.2, D-22).

The bundle is everything a run needs besides the packs, resolved once:

* the trading days in range with each session group's phases in UTC ns, the
  MIS cut-offs and `DAY_END`;
* every instrument referenced by the packs, with its session group, its dated
  contract specs (lot, tick, freeze quantity) and its underlying;
* the charge, broker-policy, margin and pricing rows in force on any day in
  range (rates already converted to ppb by the config loader);
* pack paths and hashes, and the strategy reference with its source hash.

Engines read only the bundle and the packs: no config file, no database.
Validation happens here, before anything runs (I-5, I-8, I-14).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import date, timedelta
from pathlib import Path

from stockify import numerics, paths, timeutil

SCHEMA = "stockify.bundle/1"
GROUPS = ("CM", "CM_CAS", "FO_FUT", "FO_OPT")
DAY_END = "23:59:59.999999999"


class BundleError(ValueError):
    pass


# ── Dated rows ───────────────────────────────────────────────────────────────

def _key(row: dict) -> str:
    return json.dumps(row.get("applies", row.get("group", row.get("underlying"))), sort_keys=True)


def rows_in_range(rows: list[dict], start: date, end: date) -> list[dict]:
    """Rows of a dated list whose validity [effective_from, next row of the same key)
    overlaps [start, end]. The list is sorted per key by the config loader."""
    out, by_key = [], {}
    for r in rows:
        by_key.setdefault(_key(r), []).append(r)
    for group in by_key.values():
        group = sorted(group, key=lambda r: r["effective_from"])
        for i, r in enumerate(group):
            until = group[i + 1]["effective_from"] if i + 1 < len(group) else "9999-12-31"
            if r["effective_from"] <= end.isoformat() and until > start.isoformat():
                out.append(r)
    return out


def _dated_section(doc: dict, start: date, end: date) -> dict:
    out = {}
    for k, v in doc.items():
        if k == "schema":
            continue
        if isinstance(v, list) and v and all(isinstance(r, dict) and "effective_from" in r for r in v):
            out[k] = rows_in_range(v, start, end)
        elif isinstance(v, dict) and k == "components":
            out[k] = {c: rows_in_range(rows, start, end) for c, rows in v.items()}
        else:
            out[k] = v
    return out


def _ppb_fields(doc: dict, fields: tuple[str, ...]) -> None:
    """Add `<field>_ppb` next to decimal-string rates in dated rows (exact)."""
    for rows in doc.values():
        if not isinstance(rows, list):
            continue
        for r in rows:
            if not isinstance(r, dict):
                continue
            for f in fields:
                if isinstance(r.get(f), str):
                    r[f"{f}_ppb"] = numerics.to_ppb(r[f])


def segment_of(kind: str) -> str:
    return "FO" if kind in ("FUT", "CE", "PE", "FUT_CONT") else "CM"


def _row_can_apply(row: dict, instruments: list[dict]) -> bool:
    """Could the row match an order in one of the run's instruments? (segment and kind keys)"""
    applies = row.get("applies", {})
    for inst in instruments:
        if inst["kind"] == "INDEX":
            continue
        values = {"segment": segment_of(inst["kind"]), "kind": inst["kind"]}
        if all(k not in values or (v == values[k] if not isinstance(v, list) else values[k] in v)
               for k, v in applies.items()):
            return True
    return False


def _charge_rows_unverified(charges: dict, instruments: list[dict]) -> list[str]:
    bad = []
    for comp, rows in charges.get("components", {}).items():
        for r in rows:
            if not r.get("verified", False) and _row_can_apply(r, instruments):
                bad.append(f"{comp}:{r['effective_from']}:{json.dumps(r.get('applies', {}), sort_keys=True)}")
    return bad


# ── Days ─────────────────────────────────────────────────────────────────────

def _cutoff(policy: dict, group: str, d: date) -> int | None:
    rows = [r for r in policy.get("mis_cutoff", []) if r["group"] == group and r["effective_from"] <= d.isoformat()]
    if not rows:
        return None
    return timeutil.ist_ns(d, max(rows, key=lambda r: r["effective_from"])["time"])


def _cas_cutoff(policy: dict, d: date) -> int | None:
    """CAS stocks on their CAS days (Z-5: 15:12 since CAS began); before a CM_CAS row, the CM time."""
    t = _cutoff(policy, "CM_CAS", d)
    return t if t is not None else _cutoff(policy, "CM", d)


def _day_end_ns(d: date) -> int:
    return timeutil.ist_ns(d, "23:59:59") + 999_999_999


def resolve_days(start: date, end: date, policy: dict) -> list[dict]:
    from stockify.data import calendar
    days = []
    d = start
    while d <= end:
        if calendar.is_trading_day("CM", d) or calendar.is_trading_day("FO", d):
            groups = {g: [{"phase": p.phase, "start_ns": p.start_ns, "end_ns": p.end_ns}
                          for p in calendar.phases(g, d)] for g in GROUPS}
            days.append({"date": d.isoformat(), "groups": groups,
                         "mis_cutoff_ns": {"CM": _cutoff(policy, "CM", d), "CM_CAS": _cas_cutoff(policy, d),
                                           "FO": _cutoff(policy, "FO", d)},
                         "day_end_ns": _day_end_ns(d)})
        d += timedelta(days=1)
    return days


def group_close_ns(day: dict, group: str) -> int | None:
    """End of the group's last CONTINUOUS or CAS phase (A4 §1.9)."""
    ends = [p["end_ns"] for p in day["groups"][group] if p["phase"] in ("CONTINUOUS", "CAS")]
    return max(ends) if ends else None


# ── Instruments ──────────────────────────────────────────────────────────────

def _circular_limits() -> dict[str, list[dict]]:
    """freeze_limits.json rows by underlying, oldest first (X-5)."""
    from stockify.config import loader
    out: dict[str, list[dict]] = {}
    for r in loader.load_json("freeze_limits.json")["limits"]:
        out.setdefault(r["underlying"], []).append(r)
    for rows in out.values():
        rows.sort(key=lambda r: r["effective_from"])
    return out


def _freeze(conn, underlying: str | None, on: str) -> int | None:  # noqa: ANN001
    """The quantity-freeze limit in force on `on`. An underlying with circular rows (freeze_limits.json, X-5) is
    decided by them alone (None before the first one); any other uses the refdata snapshots (qtyfreeze.csv)."""
    if not underlying:
        return None
    circular = _circular_limits().get(underlying)
    if circular is not None:
        rows = [r for r in circular if r["effective_from"] <= on]
        return rows[-1]["freeze_qty"] if rows else None
    row = conn.execute("SELECT freeze_qty FROM freeze_limits WHERE underlying=? AND effective_from<=? "
                       "ORDER BY effective_from DESC LIMIT 1", (underlying, on)).fetchone()
    return row[0] if row else None


def freeze_cross_check(conn) -> list[dict]:  # noqa: ANN001
    """qtyfreeze.csv snapshots that disagree with the circular limit in force on their date (S-6 lags X-5)."""
    out = []
    circular = _circular_limits()
    for und, day, qty in conn.execute("SELECT underlying, effective_from, freeze_qty FROM freeze_limits "
                                      "ORDER BY underlying, effective_from").fetchall():
        if und not in circular:
            continue
        rows = [r for r in circular[und] if r["effective_from"] <= day]
        expected = rows[-1]["freeze_qty"] if rows else None
        if expected is not None and qty != expected:
            out.append({"underlying": und, "date": day, "snapshot": qty, "circular": expected})
    return out


EQUITY_TICK_ASSUMED = 1          # paise (D-43)


def resolve_instruments(ids: set[int], start: date, end: date, days: list[dict], traded: dict[int, str],
                        labels: list[str] | None = None) -> list[dict]:
    """Bundle rows for `ids`; `traded` maps instrument → first date with market data.

    D-43: an equity with market data on a day before its first recorded spec
    gets lot 1 (EQ series) and a 1-paisa tick from the start of the range, and
    the run is labelled `specs:equity_tick_assumed`. NSE equity ticks are
    price-based and reviewed periodically, so today's tick cannot be copied back.
    Derivatives without a spec in force are an error.
    """
    from stockify.config import loader
    from stockify.data import instruments, refdata
    conn = refdata._connect()
    index_keys = {r[0] for r in conn.execute("SELECT key FROM index_underlyings")}
    settlement = loader.load_json("contract_rules.json")["settlement"]
    day_by_date = {d["date"]: d for d in days}
    out = []
    for iid in sorted(ids):
        info = instruments.get(iid)
        if info is None:
            raise BundleError(f"instrument {iid} is in a pack but not in refdata")
        kind = info["kind"]
        derivative = kind in ("FUT", "CE", "PE", "FUT_CONT")
        row = {"id": iid, "exchange": info["exchange"], "kind": kind, "tradingsymbol": info["tradingsymbol"],
               "underlying": info["underlying"], "underlying_id": info["underlying_id"],
               "expiry": info["expiry"], "actual_expiry": info.get("actual_expiry"),
               "strike_paise": info["strike_paise"], "underlying_class": None, "settlement": None,
               "last_trading_ns": None, "cas_dates": []}
        if derivative:
            row["underlying_class"] = "INDEX" if info["underlying"] in index_keys else "STOCK"
            row["session_group"] = "FO_OPT" if kind in ("CE", "PE") else "FO_FUT"
            rule = max((r for r in settlement if r["effective_from"] <= (info["expiry"] or end.isoformat())),
                       key=lambda r: r["effective_from"], default=None)
            row["settlement"] = (rule or {}).get(row["underlying_class"], "cash").lower() if rule else None
            last = info.get("actual_expiry") or info["expiry"]
            if last and last in day_by_date:
                row["last_trading_ns"] = group_close_ns(day_by_date[last], row["session_group"])
            elif last:                                   # expiry after the run (D-61): its own dated session
                from stockify.data import calendar
                row["last_trading_ns"] = calendar.session_close_ns(row["session_group"], date.fromisoformat(last))
        elif kind == "INDEX":
            row["session_group"] = "CM"
        else:
            row["session_group"] = "CM"
            for d in days:
                if any(p["phase"] == "CAS" for p in d["groups"]["CM_CAS"]) and conn.execute(
                        "SELECT 1 FROM fo_universe WHERE trade_date=? AND underlying=? LIMIT 1",
                        (d["date"], info["tradingsymbol"])).fetchone():
                    row["cas_dates"].append(d["date"])
            if row["cas_dates"]:
                row["session_group"] = "CM_CAS"
        specs = []
        first = instruments.spec_at(iid, start)
        later = conn.execute("SELECT effective_from, lot_size, tick_paise FROM contract_specs WHERE instrument_id=? "
                             "AND effective_from>? AND effective_from<=? ORDER BY effective_from",
                             (iid, start.isoformat(), end.isoformat())).fetchall()
        und = info["underlying"] if derivative else None
        if first:
            specs.append({"from": first["effective_from"], "lot": first["lot_size"], "tick_paise": first["tick_paise"],
                          "freeze_qty": _freeze(conn, und, start.isoformat())})
        for r in later:
            specs.append({"from": r["effective_from"], "lot": r["lot_size"], "tick_paise": r["tick_paise"],
                          "freeze_qty": _freeze(conn, und, r["effective_from"])})
        if kind != "INDEX" and iid in traded:
            on = traded[iid]
            if kind == "EQ" and not any(s["from"] <= on for s in specs):
                specs.insert(0, {"from": start.isoformat(), "lot": 1, "tick_paise": EQUITY_TICK_ASSUMED,
                                 "freeze_qty": None, "assumed": True})
                if labels is not None:
                    labels.append("specs:equity_tick_assumed")
            if not any(s["from"] <= on for s in specs):
                raise BundleError(f"instrument {iid} ({info['tradingsymbol'] or info['underlying']}) has market data "
                                  f"on {on} but no contract spec in force (lot, tick)")
        row["specs"] = specs
        out.append(row)
    return out


# ── Packs and strategy ───────────────────────────────────────────────────────

def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_packs(cfg: dict) -> list[dict]:
    from stockify.data import packs
    out = []
    for i, p in enumerate(cfg["data"]["packs"]):
        sha = packs.build(p["spec"]) if "spec" in p else p["sha256"]
        path = packs.pack_path(sha)
        if not path.exists():
            raise BundleError(f"data.packs[{i}]: pack {sha} not found in {paths.packs_dir()}")
        actual = packs.sha_file(path)   # refused-open retry (pack-race form 3)
        if actual != sha:
            raise BundleError(f"data.packs[{i}]: pack hash mismatch: file {path.name} hashes to {actual}")
        m = packs.manifest(sha)
        out.append({"sha256": sha, "path": f"packs/{sha}.evt", "price_basis": m["price_basis"], "spec": m["spec"]})
    bases = {p["price_basis"] for p in out}
    if len(bases) > 1:
        raise BundleError(f"packs mix price bases {sorted(bases)} (I-5)")
    if cfg.get("price_basis") and bases and cfg["price_basis"] not in bases:
        raise BundleError(f"run price_basis {cfg['price_basis']!r} but packs are {bases.pop()!r} (I-5)")
    return out


def strategy_ref(cfg: dict) -> dict:
    s = cfg["strategy"]
    if s["kind"] == "python":
        module = s["ref"].split(":")[0]
        try:
            spec = importlib.util.find_spec(module)
        except (ImportError, ValueError) as e:
            raise BundleError(f"strategy {s['ref']}: {e}") from e
        if spec is None or not spec.origin or not Path(spec.origin).is_file():
            raise BundleError(f"strategy {s['ref']}: source cannot be found to hash")
        source = _file_sha(Path(spec.origin))
    elif s["kind"] == "script":
        source = hashlib.sha256(numerics.dumps_canonical(s["actions"]).encode()).hexdigest()
    else:
        # A registered C++ strategy: its code is the engine build, so the build id and the name identify it.
        from stockify import engine
        try:
            mod = engine.load()
        except engine.EngineUnavailable as e:
            raise BundleError(f"strategy {s['ref']}: a C++ strategy needs the engine: {e}") from e
        known = list(mod.strategies())
        if s["ref"] not in known:
            raise BundleError(f"strategy {s['ref']}: no C++ strategy is registered under that name "
                              f"(known: {', '.join(known)})")
        build_id = mod.build_info()["engine_build_id"]
        source = hashlib.sha256(f"{build_id}:{s['ref']}".encode()).hexdigest()
    return {"kind": s["kind"], "ref": s.get("ref"), "source_sha256": source, "params": s.get("params", {}),
            **({"actions": s["actions"]} if s["kind"] == "script" else {})}


def _scan_packs(pack_rows: list[dict]) -> tuple[set[int], dict[int, str], date, date]:
    from stockify.data import packs
    ids: set[int] = set()
    traded: dict[int, str] = {}
    lo = hi = None
    for p in pack_rows:
        spec = p["spec"]
        s, e = date.fromisoformat(spec["from"]), date.fromisoformat(spec["to"])
        lo, hi = min(lo or s, s), max(hi or e, e)
        _, records = packs.read(paths.lab_dir() / p["path"])
        for r in records:
            iid = r["instrument_id"]
            if iid:
                ids.add(iid)
            if r["kind"] in ("TICK_FULL", "TICK_QUOTE", "INDEX", "BAR") and iid not in traded:
                traded[iid] = timeutil.ns_to_ist(r["t_event"]).date().isoformat()
            if r["kind"] == "REFDATA" and r["subtype"] == "instrument_add" and r["payload"].get("underlying_id"):
                ids.add(r["payload"]["underlying_id"])
    return ids, traded, lo, hi


# ── Resolve ──────────────────────────────────────────────────────────────────

def resolve_bundle(cfg: dict) -> dict:
    """The bundle for a validated run config (`runconfig.load`)."""
    from stockify.config import loader
    pack_rows = resolve_packs(cfg)
    ids, traded, start, end = _scan_packs(pack_rows)
    policy = _dated_section(loader.load_json("broker_policy.json"), start, end)
    _ppb_fields(policy, ("pct", "fraction"))
    days = resolve_days(start, end, policy)
    if not days:
        raise BundleError(f"no trading days between {start} and {end}")
    charges = _dated_section(loader.load_json("charges.json"), start, end)
    margin = _dated_section(loader.load_json("margin_params.json"), start, end)
    _ppb_fields(margin, ("index_pct", "stock_pct", "pct", "psr_pct", "vsr_pct", "extreme_weight", "elm_pct",
                         "default_iv"))
    pricing = _dated_section(loader.load_json("pricing.json"), start, end)
    _ppb_fields(pricing, ("rate",))
    labels = [f"fill:{cfg['fill_model']['kind']}", f"margin:{cfg['margin']['model']}(approx)"]
    if cfg["margin"]["model"] == "spanlite":     # M4.8: the parameters' calibration, e.g. "placeholder" (I-14)
        calibrations = sorted({r.get("calibration_id", "?") for r in margin.get("spanlite", [])}) or ["none"]
        labels.append("margin_spanlite:" + "+".join(calibrations))
    instruments_rows = resolve_instruments(ids, start, end, days, traded, labels)
    if cfg["fill_model"]["kind"] == "eod_close":
        labels.append("eod_approx")
    if cfg["fill_model"]["kind"] == "snapshot_ltp":
        labels.append("fill:approximate")
    unverified = _charge_rows_unverified(charges, instruments_rows)
    if unverified:
        if not cfg["costs"]["allow_unverified"]:
            raise BundleError("charge rows in range are not verified (set costs.allow_unverified to run anyway, "
                              "labelled): " + "; ".join(unverified[:5]) + (" …" if len(unverified) > 5 else ""))
        labels.append("charges:unverified_rows")
    if not rows_in_range(policy.get("market_protection_auto_bp", []), start, end):
        raise BundleError("no market_protection_auto_bp row in force in range (broker_policy.json)")
    return {
        "schema": SCHEMA, "run": cfg, "seed": cfg["seed"], "days": days,
        "instruments": instruments_rows,
        "charges": charges, "broker_policy": policy, "margin_params": margin, "pricing": pricing,
        "packs": pack_rows, "strategy": strategy_ref(cfg), "labels": sorted(set(labels)), "sod_state": None,
    }


def dumps(bundle: dict) -> str:
    return numerics.dumps_canonical(bundle)


def write(bundle: dict, folder: Path) -> str:
    """Write bundle.json into the run folder; returns its SHA-256 (I-8)."""
    text = dumps(bundle)
    (folder / "bundle.json").write_text(text + "\n", encoding="utf-8", newline="\n")
    return hashlib.sha256((text + "\n").encode("utf-8")).hexdigest()

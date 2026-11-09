"""Golden scenarios (M2.5, A3 §2): tiny hand-checked worlds run on an implementation.

    python -m stockify golden run --impl oracle [--only G01,G02]

A golden YAML holds its own bundle (dates, instruments, charges, policies,
phases), a list of events, a `script` strategy and the expected outputs, with
`notes` that explain every number. Nothing comes from the live config files,
so a golden only changes when its author changes it.

`F<seed>.yaml` goldens are saved by the fuzz shrinker (`stockify.tools.fuzz`): their strategy is a
`ref` with `params` (a recording replayed by `stockify.tools.replay:Replay`) instead of script actions,
and their expected values are the oracle's artifacts.

Defaults (all overridable in the YAML under `bundle:`):

* sessions: CM PREOPEN 09:00–09:15, CONTINUOUS 09:15–15:30, POSTCLOSE 15:40–16:00;
  FO_FUT CONTINUOUS 09:15–15:30; FO_OPT CONTINUOUS 09:15–15:30; CM_CAS = CM.
* `mis_cutoff`: none (no square-off) unless given, e.g. `{CM: "15:20", FO: "15:25"}`; `CM_CAS`
  (CAS stocks on their CAS days, Z-5) defaults to the CM time.
* charges `zero`: every component zero; or explicit rows in the A1 §7.2 format.
* policy: protection 200 bp, MIS margin CM 20 % / FO 100 %, no CNC sale hold-back.
* margin: flat index 12 %, stock 20 %. Pricing: stale 5000 ms, risk-free 0.
"""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
from datetime import date, datetime
from pathlib import Path

from stockify import numerics, runconfig, timeutil

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "tests" / "golden"
DEFAULT_SESSIONS = {
    "CM": [["PREOPEN", "09:00", "09:15"], ["CONTINUOUS", "09:15", "15:30"], ["POSTCLOSE", "15:40", "16:00"]],
    "FO_FUT": [["CONTINUOUS", "09:15", "15:30"]],
    "FO_OPT": [["CONTINUOUS", "09:15", "15:30"]],
}
ZERO_COMPONENTS = ("brokerage", "stt", "exchange_txn", "sebi", "stamp", "gst", "exercise_stt", "exercise_brokerage")


class GoldenError(AssertionError):
    pass


def ts(text) -> int:  # noqa: ANN001
    """'2026-03-02T09:15:00+05:30' -> UTC ns (ints pass through)."""
    if isinstance(text, int):
        return text
    dt = datetime.fromisoformat(str(text))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timeutil.IST)
    return int(dt.timestamp()) * 1_000_000_000 + dt.microsecond * 1000


# ── Bundle ───────────────────────────────────────────────────────────────────

def _charges(spec) -> dict:  # noqa: ANN001
    from stockify.config import loader
    if spec in (None, "zero"):
        comps = {c: [{"applies": {}, "effective_from": "2015-01-01", "kind": "zero", "pct": "0", "flat_paise": 0,
                      "rate": "0", "on": [], "pct_of_intrinsic": "0", "verified": True, "verified_on": "2026-01-01",
                      "source": "golden"}] for c in ZERO_COMPONENTS}
        comps["brokerage"][0] = {"applies": {}, "effective_from": "2015-01-01", "kind": "zero", "verified": True,
                                 "verified_on": "2026-01-01", "source": "golden"}
        comps["physical_delivery"] = [{"applies": {}, "effective_from": "2015-01-01", "brokerage_pct": "0",
                                       "netted_brokerage_pct": "0", "stt_each_side_pct_of_value": "0",
                                       "verified": True, "verified_on": "2026-01-01", "source": "golden"}]
        doc = {"components": comps}
    else:
        doc = {"components": copy.deepcopy(spec)}
        for rows in doc["components"].values():
            for r in rows:
                r.setdefault("verified", True)
                r.setdefault("source", "golden")
                r.setdefault("applies", {})
    loader._convert_charge_rates(doc)
    for rows in doc["components"].values():
        for r in rows:
            for f in ("rate",):
                if isinstance(r.get(f), str):
                    r[f"{f}_ppb"] = numerics.to_ppb(r[f])
    return doc


def _days(g: dict) -> list[dict]:
    return [_day(g, d) for d in g["bundle"]["dates"]]


def _day(g: dict, d: str) -> dict:
    """One day's sessions from the golden's own session table (also used for an expiry after the run, D-61)."""
    b = g["bundle"]
    sessions = {**DEFAULT_SESSIONS, **b.get("sessions", {})}
    sessions.setdefault("CM_CAS", sessions["CM"])
    cut = b.get("mis_cutoff", {})
    day = date.fromisoformat(d)
    groups = {}
    for gname in ("CM", "CM_CAS", "FO_FUT", "FO_OPT"):
        rows = b.get("sessions_by_date", {}).get(d, {}).get(gname, sessions[gname])
        groups[gname] = [{"phase": p, "start_ns": timeutil.ist_ns(day, s), "end_ns": timeutil.ist_ns(day, e)}
                         for p, s, e in rows]
    cuts = cut.get(d, cut) if isinstance(cut.get(d), dict) else cut
    return {"date": d, "groups": groups,
            "mis_cutoff_ns": {"CM": timeutil.ist_ns(day, cuts["CM"]) if "CM" in cuts else None,
                              "CM_CAS": timeutil.ist_ns(day, cuts.get("CM_CAS", cuts.get("CM")))
                              if "CM_CAS" in cuts or "CM" in cuts else None,
                              "FO": timeutil.ist_ns(day, cuts["FO"]) if "FO" in cuts else None},
            "day_end_ns": timeutil.ist_ns(day, "23:59:59") + 999_999_999}


def _instruments(g: dict, days: list[dict]) -> list[dict]:
    from stockify import bundle as bundle_mod
    by_date = {d["date"]: d for d in days}
    out = []
    for i in g["bundle"]["instruments"]:
        kind = i["kind"]
        group = i.get("session_group") or ("FO_OPT" if kind in ("CE", "PE") else "FO_FUT" if kind == "FUT" else "CM")
        row = {"id": i["id"], "exchange": i.get("exchange", "NFO" if kind in ("FUT", "CE", "PE") else "NSE"),
               "kind": kind, "tradingsymbol": i.get("tradingsymbol", f"I{i['id']}"), "underlying": i.get("underlying"),
               "underlying_id": i.get("underlying_id"), "expiry": i.get("expiry"),
               "actual_expiry": i.get("actual_expiry"), "strike_paise": i.get("strike_paise"),
               "underlying_class": i.get("underlying_class", "INDEX" if kind in ("FUT", "CE", "PE") else None),
               "settlement": i.get("settlement", "cash" if kind in ("FUT", "CE", "PE") else None),
               "last_trading_ns": None, "cas_dates": i.get("cas_dates", []), "session_group": group,
               "specs": [{"from": s["from"], "lot": s["lot"], "tick_paise": s["tick_paise"],
                          "freeze_qty": s.get("freeze_qty")} for s in i.get("specs", [])]}
        last = row["actual_expiry"] or row["expiry"]
        if last in by_date:
            row["last_trading_ns"] = bundle_mod.group_close_ns(by_date[last], group)
        elif last:                                       # expiry after the run (D-61)
            row["last_trading_ns"] = bundle_mod.group_close_ns(_day(g, last), group)
        out.append(row)
    return out


def _bundle_strategy(s: dict) -> dict:
    if s["kind"] == "python":
        import importlib
        import inspect
        source = inspect.getsource(importlib.import_module(s["ref"].split(":")[0]))
        return {"kind": "python", "ref": s["ref"], "params": s["params"],
                "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest()}
    return {"kind": "script", "actions": s["actions"], "params": {},
            "source_sha256": hashlib.sha256(numerics.dumps_canonical(s["actions"]).encode()).hexdigest()}


def build_bundle(g: dict) -> dict:
    from stockify import bundle as bundle_mod
    b = g["bundle"]
    run = {"schema": "stockify.run/1", "name": g["id"], "strategy": {"kind": "script", "actions": []},
           "data": {"packs": [{"sha256": "0" * 64}]}, "capital_paise": 10_000_000, "price_basis": "raw",
           "fill_model": {"kind": "bar"}, **copy.deepcopy(g.get("run", {}))}
    run["margin"] = {"model": "flat", **run.get("margin", {})}   # pinned: goldens never follow a changed default
    ref = g["strategy"].get("ref")
    actions = copy.deepcopy(g["strategy"].get("actions", []))
    for a in actions:
        if isinstance(a.get("at_ns"), str):
            a["at_ns"] = ts(a["at_ns"])
    if ref:
        params = copy.deepcopy(g["strategy"].get("params", {}))
        run["strategy"] = {"kind": "python", "ref": ref, "params": params}
    else:
        run["strategy"] = {"kind": "script", "actions": actions}
    cfg = runconfig.load(run)
    days = _days(g)
    policy = {
        "market_protection_auto_bp": [{"applies": {}, "effective_from": "2015-01-01", "bp": b.get("protection_bp", 200)}],
        "mis_margin_pct": [{"applies": {"segment": "CM"}, "effective_from": "2015-01-01",
                            "pct_ppb": numerics.to_ppb(b.get("mis_margin_cm", "0.20"))},
                           {"applies": {"segment": "FO"}, "effective_from": "2015-01-01",
                            "pct_ppb": numerics.to_ppb(b.get("mis_margin_fo", "1"))}],
        "cnc_sell_credit_same_day": [],
    }
    if b.get("delivery_schedule"):                   # the shipped Z-4 delivery-margin schedule (D-60)
        from stockify.config import loader
        policy["delivery_margin_schedule"] = loader.load_json("broker_policy.json")["delivery_margin_schedule"]
    margin = {"flat": [{"applies": {}, "effective_from": "2015-01-01",
                        "index_pct_ppb": numerics.to_ppb(b.get("margin_index", "0.12")),
                        "stock_pct_ppb": numerics.to_ppb(b.get("margin_stock", "0.20"))}]}
    if b.get("spanlite_placeholder"):                # the shipped SPAN-lite rows (M4.8; placeholders until M4.9)
        from stockify.config import loader
        rows = copy.deepcopy(loader.load_json("margin_params.json")["spanlite"])
        for r in rows:
            for f in ("psr_pct", "vsr_pct", "extreme_weight", "elm_pct", "default_iv"):
                r[f"{f}_ppb"] = numerics.to_ppb(r[f])
        if "spanlite_calibration_id" in b:          # an adopted row instead of the placeholder (staircase B3)
            for r in rows:
                r["calibration_id"] = b["spanlite_calibration_id"]
        margin["spanlite"] = rows
    if "expiry_addon" in b:                          # the expiry-day ELM on short index options (X-9)
        margin["expiry_day_short_index_option_addon"] = [{"applies": {}, "effective_from": "2015-01-01",
                                                          "pct_ppb": numerics.to_ppb(b["expiry_addon"])}]
    sqoff = b.get("sqoff_fee_paise")
    charges = _charges(b.get("charges", "zero"))
    if "gst_rate" in b:                              # a GST row on brokerage, exchange and SEBI (zero charges)
        charges["components"]["gst"] = [{"applies": {}, "effective_from": "2015-01-01",
                                         "rate_ppb": numerics.to_ppb(b["gst_rate"]),
                                         "on": ["brokerage", "exchange_txn", "sebi"]}]
    if "exercise_stt" in b:
        charges["components"]["exercise_stt"] = [{"applies": {}, "effective_from": "2015-01-01",
                                                  "pct_of_intrinsic_ppb": numerics.to_ppb(b["exercise_stt"])}]
    if "exercise_brokerage_paise" in b:
        charges["components"]["exercise_brokerage"] = [{"applies": {}, "effective_from": "2015-01-01", "kind": "flat",
                                                        "flat_paise": b["exercise_brokerage_paise"]}]
    if "physical_delivery" in b:                     # {brokerage_pct, netted_brokerage_pct, stt_each_side_pct_of_value}
        row = {"applies": {}, "effective_from": "2015-01-01", **b["physical_delivery"]}
        for f in ("brokerage_pct", "netted_brokerage_pct", "stt_each_side_pct_of_value"):
            row[f"{f}_ppb"] = numerics.to_ppb(row[f])
        charges["components"]["physical_delivery"] = [row]
    if sqoff is not None:
        charges["components"]["auto_square_off_fee"] = [{"applies": {}, "effective_from": "2015-01-01",
                                                         "per_order_paise": sqoff, "plus_gst": b.get("sqoff_plus_gst", True)}]
    out = {"schema": "stockify.bundle/1", "run": cfg, "seed": cfg["seed"],
           "days": bundle_mod.apply_seed_mode(cfg, days),
           "instruments": _instruments(g, days), "charges": charges, "broker_policy": policy,
           "margin_params": margin, "pricing": {"stale_after_ms": 5000, "risk_free": []},
           "packs": [], "strategy": _bundle_strategy(run["strategy"]),
           "labels": [], "sod_state": None}
    for key in ("span", "span_skipped", "elm"):     # NSE SPAN / ELM sections as written (A1 §6.2; MEZ 4.10 fuzz)
        if key in b:
            out[key] = copy.deepcopy(b[key])
    return out


# ── Events → pack ────────────────────────────────────────────────────────────

def build_pack(g: dict, folder: Path) -> Path:
    from stockify.data import packs as P
    recs = []
    for n, e in enumerate(g["events"], start=1):
        k = e["kind"]
        iid = e.get("instrument", 0)
        if k == "BAR":
            start = ts(e["start"])
            t = start + e["interval_s"] * 1_000_000_000 if "t" not in e else ts(e["t"])
            rec = P.enc_bar(iid, t, e["interval_s"], start, e["o"], e["h"], e["l"], e["c"], e.get("v", 0), e.get("oi", 0))
            rank = 4
        elif k == "INDEX":
            t = ts(e["t"])
            body = P.INDEX.pack(e["value"], e.get("high", e["value"]), e.get("low", e["value"]), e.get("open", e["value"]),
                                e.get("close", e["value"]), e.get("change", 0), 0)
            rec = P._prefix(P.INDEX_K, 80, iid, t) + body
            rank = 4
        elif k in ("TICK_QUOTE", "TICK_FULL"):
            t = ts(e["t"])
            q = P.QUOTE.pack(e["ltp"], e.get("last_qty", 0), e.get("atp", e["ltp"]), e.get("volume", 0),
                             e.get("buy_qty", 0), e.get("sell_qty", 0), e.get("open", e["ltp"]), e.get("high", e["ltp"]),
                             e.get("low", e["ltp"]), e.get("close", e["ltp"]))
            if k == "TICK_QUOTE":
                rec = P._prefix(P.TICK_QUOTE, 104, iid, t) + q
            else:
                levels = [tuple(x) for x in e.get("bids", [])] + [(0, 0, 0)] * (5 - len(e.get("bids", [])))
                levels += [tuple(x) for x in e.get("asks", [])] + [(0, 0, 0)] * (5 - len(e.get("asks", [])))
                depth = b"".join(P.LEVEL.pack(p, qty, o, 0) for p, qty, o in levels)
                rec = P._prefix(P.TICK_FULL, 384, iid, t) + q + P.FULL_EXTRA.pack(0, e.get("oi", 0), 0, 0, 0) + depth
            rank = 4
        elif k == "SETTLE":
            t = ts(e["t"])
            rec = P.enc_settle(iid, t, 0 if e.get("subtype", "final") == "final" else 1, e["date"], e["price"])
            rank = 3
        elif k == "GAP":
            t = ts(e["t"])
            rec = P.enc_gap(iid, t, 0 if e.get("phase", "start") == "start" else 1, e.get("reason", "disconnect"))
            rank = 1
        elif k == "PRICE_BAND":
            t = ts(e["t"])
            rec = P.enc_refdata(iid, t, "price_band", {"instrument_id": iid, "lower_paise": e["lower"],
                                                       "upper_paise": e["upper"], "source": "golden"})
            rank = 2
        else:
            raise GoldenError(f"{g['id']}: unknown event kind {k}")
        recs.append(((t, rank, n, iid), rec))
    recs.sort(key=lambda x: x[0])
    body = b"".join(P._with_seq(r, i) for i, (_, r) in enumerate(recs, start=1))
    tmin = recs[0][0][0] if recs else 0
    tmax = recs[-1][0][0] if recs else 0
    header = P.HEADER.pack(P.MAGIC, P.VERSION, 0, 64, len(recs), tmin, tmax, 0)
    path = folder / f"{g['id']}.evt"
    path.write_bytes(header + body)
    return path


# ── Compare ──────────────────────────────────────────────────────────────────

def _read_csv(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    head = lines[0].split(",")
    import csv
    return [dict(zip(head, row)) for row in csv.reader(lines[1:])]


def _norm(v):  # noqa: ANN001, ANN202
    return str(v) if not isinstance(v, bool) else ("true" if v else "false")


def _expect_rows(name: str, rows: list[dict], expected: list[dict], key: str | None, problems: list) -> None:
    if key:
        by = {r[key]: r for r in rows}
    for i, exp in enumerate(expected):
        exp = dict(exp)
        if "t" in exp:
            exp["t_ns"] = ts(exp.pop("t"))
        for k in list(exp):
            if k.endswith("_ns") and isinstance(exp[k], str):
                exp[k] = ts(exp[k])
        target = by.get(str(exp[key])) if key else (rows[i] if i < len(rows) else None)
        if target is None:
            problems.append(f"{name}: expected row {exp} is missing")
            continue
        for k, v in exp.items():
            if _norm(target.get(k)) != _norm(v):
                problems.append(f"{name}[{exp.get(key, i)}].{k}: got {target.get(k)!r}, expected {v!r}")


def check(g: dict, folder: Path) -> list[str]:
    exp = g.get("expect", {})
    problems: list[str] = []
    tables = {"orders": ("orders.csv", "order_id"), "fills": ("fills.csv", None), "charges": ("charges.csv", None),
              "equity": ("equity.csv", None), "positions": ("positions_eod.csv", None)}
    for name, (file, key) in tables.items():
        if name in exp:
            rows = _read_csv(folder / file)
            _expect_rows(name, rows, exp[name], key, problems)
            if exp.get(f"{name}_count") is not None and len(rows) != exp[f"{name}_count"]:
                problems.append(f"{name}: {len(rows)} rows, expected {exp[f'{name}_count']}")
    for name in ("orders", "fills", "charges"):
        if f"{name}_count" in exp and name not in exp:
            n = len(_read_csv(folder / tables[name][0]))
            if n != exp[f"{name}_count"]:
                problems.append(f"{name}: {n} rows, expected {exp[f'{name}_count']}")
    if "order_events" in exp:                        # each expected event must appear (contains)
        rows = _read_csv(folder / "order_events.csv")
        for want in exp["order_events"]:
            want = dict(want)
            if "t" in want:
                want["t_ns"] = ts(want.pop("t"))
            if not any(all(_norm(r.get(k)) == _norm(v) for k, v in want.items()) for r in rows):
                problems.append(f"order_events: no row {want}")
    if "labels" in exp:
        labels = json.loads((folder / "manifest.json").read_text())["labels"]
        for lab in exp["labels"]:
            if lab not in labels:
                problems.append(f"label {lab!r} missing from {labels}")
    return problems


# ── Run ──────────────────────────────────────────────────────────────────────

def load(path: Path) -> dict:
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def goldens(only: list[str] | None = None, include_capture: bool = False) -> list[tuple[Path, dict]]:
    """Simulated goldens; `include_capture` adds G08-style checks against captured Kite data."""
    out = []
    for path in sorted(GOLDEN_DIR.glob("G*.yaml")) + sorted(GOLDEN_DIR.glob("F*.yaml")):
        g = load(path)
        if only and g["id"] not in only:
            continue
        if g.get("impl") == "data_layer" or (g.get("impl") == "kite_capture" and not include_capture):
            continue
        out.append((path, g))
    return out


def capture_status(g: dict) -> tuple[str, list[str]]:
    """('PENDING', []) without a capture; otherwise the latest capture compared with our charges."""
    from stockify.tools import capture_charges
    caps = sorted(capture_charges.FIXTURES.glob("*.json"))
    if not caps:
        return "PENDING", [rf"no capture yet: run `.\.venv\Scripts\python.exe -m stockify tools capture-charges "
                           rf"--cases {g['cases']}`"]
    problems = capture_charges.compare_capture(caps[-1])
    return ("PASS" if not problems else "FAIL"), problems


def _merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def run_one(g: dict, impl: str = "oracle") -> list[str]:
    """Problems found (empty = pass). `variants` re-run the scenario with overrides."""
    if not g.get("variants"):
        return _run_single(g, impl)
    problems = []
    for v in g["variants"]:
        h = {k: x for k, x in g.items() if k != "variants"}
        for part in ("bundle", "run", "strategy"):
            if part in v:
                h[part] = _merge(h.get(part, {}), v[part])
        if "drop_events" in v:
            h["events"] = [e for n, e in enumerate(g["events"], start=1) if n not in v["drop_events"]]
        h["expect"] = v.get("expect", g.get("expect", {}))
        problems += [f"[{v['name']}] {p}" for p in _run_single(h, impl)]
    return problems


def _run_single(g: dict, impl: str) -> list[str]:
    if impl == "oracle":
        from stockify.oracle import run_bundle
    elif impl == "engine":
        from stockify.engine import run_bundle  # the C++ engine, same inputs and artifacts (M3.12)
    else:
        raise GoldenError(f"unknown implementation {impl!r}")
    with tempfile.TemporaryDirectory(prefix=f"golden-{g['id']}-") as tmp:
        folder = Path(tmp)
        pack = build_pack(g, folder)
        bundle = build_bundle(g)
        expect_error = g.get("expect", {}).get("error")
        try:
            run_bundle(bundle, [pack], folder / "run", report=False)
        except Exception as e:                       # noqa: BLE001 — goldens may expect a failure
            if expect_error and expect_error in str(e):
                return []
            return [f"run failed: {type(e).__name__}: {e}"]
        if expect_error:
            return [f"expected an error containing {expect_error!r}, but the run succeeded"]
        return check(g, folder / "run")

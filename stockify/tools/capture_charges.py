"""Capture Kite's virtual contract note for imaginary orders (M2.5 G08, K-10).

    python -m stockify tools capture-charges --cases tests/golden/charges_cases.json

Each case names an instrument class (`EQ` symbol, or the nearest NIFTY future /
an at-the-money NIFTY option resolved from refdata on the capture day), a side,
product, quantity and price. The tool calls `POST /charges/orders` (needs a fresh
Kite login) and writes `tests/fixtures/kite_charges/<YYYY-MM-DD>.json` with the
request and Kite's breakdown per case. No tokens, keys or account data are saved.
Old captures are kept: charges change, and each capture is the truth on its date.
"""
from __future__ import annotations

import json
from pathlib import Path

from stockify import timeutil

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "kite_charges"


def _symbol(case: dict, day) -> tuple[str, str, str]:  # noqa: ANN001
    """(exchange, tradingsymbol, kind) live on `day`."""
    from stockify.data import instruments, refdata
    kind = case["kind"]
    if kind == "EQ":
        return "NSE", case["symbol"], "EQ"
    und = case.get("underlying", "NIFTY")
    if kind == "FUT":
        e = instruments.expiries(und, "FUT", day)[0]
        iid = instruments.derivative(und, "FUT", e)
    else:
        e = instruments.expiries(und, "OPT", day)[0]
        chain = [r for r in instruments.option_chain(und, e, day) if r["kind"] == kind and r["tradingsymbol"]]
        target = case.get("strike_paise")
        if target is None:
            prev = refdata._connect().execute(
                "SELECT close_paise FROM index_eod i JOIN instruments x USING (instrument_id) WHERE x.tradingsymbol=? "
                "ORDER BY trade_date DESC LIMIT 1", (case.get("index", "NIFTY 50"),)).fetchone()
            target = prev[0] if prev else chain[len(chain) // 2]["strike_paise"]
        iid = min(chain, key=lambda r: (abs(r["strike_paise"] - target), r["strike_paise"]))["instrument_id"]
    return "NFO", instruments.get(iid)["tradingsymbol"], kind


def capture(cases_path: Path, client=None) -> Path:  # noqa: ANN001
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    day = timeutil.ist_today()
    if client is None:
        import kite_client
        client = kite_client.get_client()
    params, meta = [], []
    for n, c in enumerate(cases, start=1):
        exchange, symbol, kind = _symbol(c, day)
        params.append({"order_id": str(n), "exchange": exchange, "tradingsymbol": symbol,
                       "transaction_type": c["side"], "variety": "regular", "product": c["product"],
                       "order_type": "MARKET", "quantity": c["qty"], "average_price": c["price_paise"] / 100})
        meta.append({"case": c["name"], "kind": kind, "price_paise": c["price_paise"], **params[-1]})
    response = client.get_virtual_contract_note(params)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    out = FIXTURES / f"{day.isoformat()}.json"
    out.write_text(json.dumps({"captured_on": day.isoformat(), "endpoint": "POST /charges/orders",
                               "cases": [{"request": m, "response": r} for m, r in zip(meta, response)]},
                              indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


# ── Comparison (G08) ─────────────────────────────────────────────────────────

KITE_FIELDS = {"brokerage": "brokerage", "stt": "transaction_tax", "exchange": "exchange_turnover_charge",
               "sebi": "sebi_turnover_charge", "stamp": "stamp_duty"}


def compare_capture(path: Path) -> list[str]:
    """Our charges (live config rows) vs Kite's note, component by component, in paise."""
    from stockify import bundle as bundle_mod, numerics
    from stockify.config import loader
    from stockify.oracle import costs
    data = json.loads(path.read_text(encoding="utf-8"))
    d = data["captured_on"]
    from datetime import date
    rows = bundle_mod._dated_section(loader.load_json("charges.json"), date.fromisoformat(d), date.fromisoformat(d))
    c = costs.Costs(rows, {})
    problems = []
    for case in data["cases"]:
        req, resp = case["request"], case["response"]
        turnover = req["quantity"] * req["price_paise"]
        ours = c.order(req["kind"], req["product"], req["transaction_type"], turnover, d)
        charges = resp["charges"]
        for k, field in KITE_FIELDS.items():
            theirs = numerics.to_paise(str(charges.get(field, 0)))
            if ours[k] != theirs:
                problems.append(f"{case['request']['case']} {k}: ours {ours[k]} vs Kite {theirs}")
        gst = numerics.to_paise(str(charges["gst"]["total"]))
        if ours["gst"] != gst:
            problems.append(f"{case['request']['case']} gst: ours {ours['gst']} vs Kite {gst}")
    return problems

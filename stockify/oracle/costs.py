"""Charges (A4 §5), from the bundle's charge rows. Integer ppb arithmetic only.

Charges are computed per executed order per trade date. Each component has
exactly one matching row in force on the trade date: a row matches when every
key of its `applies` object matches the order (list values by membership);
among rows with the same `applies`, the one in force is the latest whose
`effective_from` is on or before the date. Zero or several matches is an error.
"""
from __future__ import annotations

import json

from stockify import numerics

ORDER_COMPONENTS = ("brokerage", "stt", "exchange_txn", "sebi", "stamp")
PPB = 1_000_000_000


class CostError(RuntimeError):
    pass


def _matches(applies: dict, attrs: dict) -> bool:
    for k, v in applies.items():
        if k not in attrs:
            return False
        if isinstance(v, list):
            if attrs[k] not in v:
                return False
        elif attrs[k] != v:
            return False
    return True


def row_for(rows: list[dict], attrs: dict, trade_date: str, component: str, required: bool = True) -> dict | None:
    groups: dict[str, dict] = {}
    for r in rows:
        if r["effective_from"] <= trade_date and _matches(r.get("applies", {}), attrs):
            key = json.dumps(r.get("applies", {}), sort_keys=True)
            if key not in groups or r["effective_from"] > groups[key]["effective_from"]:
                groups[key] = r
    if len(groups) > 1:
        raise CostError(f"{component}: {len(groups)} rows match {attrs} on {trade_date}")
    if not groups:
        if required:
            raise CostError(f"{component}: no row matches {attrs} on {trade_date}")
        return None
    return next(iter(groups.values()))


def _rate(row: dict, side: str) -> int:
    """A side-specific rate (`buy`/`sell`) or a plain one (`pct`); 0 when the row has none for this side."""
    side_key = f"{side.lower()}_ppb"
    if side_key in row:
        return row[side_key]
    if "buy_ppb" in row or "sell_ppb" in row:
        return 0
    return row.get("pct_ppb", row.get("rate_ppb", 0))


def _pct_of(value: int, ppb: int) -> int:
    return numerics.round_half_up_div(value * ppb, PPB)


def segment(kind: str) -> str:
    return "FO" if kind in ("FUT", "CE", "PE") else "CM"


class Costs:
    def __init__(self, charges: dict, costs_cfg: dict):
        self.rows = charges.get("components", {})
        self.cfg = costs_cfg

    def _gst(self, trade_date: str, parts: dict) -> int:
        row = row_for(self.rows.get("gst", []), {}, trade_date, "gst")
        base = 0
        for name in row.get("on", []):
            base += parts.get(name, 0)
        return _pct_of(base, row["rate_ppb"])

    def order(self, kind: str, product: str, side: str, turnover: int, trade_date: str) -> dict:
        """Charge components of one executed order on one trade date."""
        attrs = {"segment": segment(kind), "kind": kind, "product": product, "side": side}
        parts = {}
        b = row_for(self.rows["brokerage"], attrs, trade_date, "brokerage")
        if b["kind"] == "zero":
            parts["brokerage"] = 0
        elif b["kind"] == "flat":
            parts["brokerage"] = b["flat_paise"]
        elif b["kind"] == "min_flat_pct":
            parts["brokerage"] = min(b["flat_paise"], _pct_of(turnover, b["pct_ppb"]))
        else:
            raise CostError(f"brokerage kind {b['kind']!r}")
        for comp in ("stt", "exchange_txn", "sebi", "stamp"):
            row = row_for(self.rows[comp], attrs, trade_date, comp)
            parts[comp] = _pct_of(turnover, _rate(row, side))
        parts["gst"] = self._gst(trade_date, parts)
        return {"turnover": turnover, "brokerage": parts["brokerage"], "stt": parts["stt"],
                "exchange": parts["exchange_txn"], "sebi": parts["sebi"], "stamp": parts["stamp"],
                "gst": parts["gst"], "dp": 0, "other": 0}

    def dp(self, trade_date: str) -> dict | None:
        if not self.cfg.get("dp_charges", True):
            return None
        row = row_for(self.rows.get("dp", []), {"segment": "CM", "product": "CNC", "side": "SELL"}, trade_date, "dp",
                      required=False)
        if row is None:
            return None
        amount = row["per_scrip_per_day_paise"]
        gst = 0 if row.get("includes_gst") else self._gst_on(trade_date, amount)
        return {"turnover": 0, "brokerage": 0, "stt": 0, "exchange": 0, "sebi": 0, "stamp": 0, "gst": gst,
                "dp": amount, "other": 0}

    def _gst_on(self, trade_date: str, amount: int) -> int:
        row = row_for(self.rows.get("gst", []), {}, trade_date, "gst")
        return _pct_of(amount, row["rate_ppb"])

    def sqoff_fee(self, trade_date: str) -> dict | None:
        if not self.cfg.get("auto_square_off_fee", True):
            return None
        row = row_for(self.rows.get("auto_square_off_fee", []), {}, trade_date, "auto_square_off_fee", required=False)
        if row is None:
            return None
        fee = row["per_order_paise"]
        gst = self._gst_on(trade_date, fee) if row.get("plus_gst") else 0
        return {"turnover": 0, "brokerage": 0, "stt": 0, "exchange": 0, "sebi": 0, "stamp": 0, "gst": gst,
                "dp": 0, "other": fee}

    def exercise(self, kind: str, intrinsic_value: int, trade_date: str) -> dict:
        """Long ITM index option at expiry: exercise STT on intrinsic value and exercise brokerage (A4 §7.2)."""
        attrs = {"segment": "FO", "kind": kind}
        stt_row = row_for(self.rows.get("exercise_stt", []), attrs, trade_date, "exercise_stt")
        brk_row = row_for(self.rows.get("exercise_brokerage", []), attrs, trade_date, "exercise_brokerage",
                          required=False)
        stt = _pct_of(intrinsic_value, stt_row["pct_of_intrinsic_ppb"])
        brokerage = brk_row["flat_paise"] if brk_row and brk_row["kind"] == "flat" else 0
        gst = self._gst(trade_date, {"brokerage": brokerage})
        return {"turnover": intrinsic_value, "brokerage": brokerage, "stt": stt, "exchange": 0, "sebi": 0,
                "stamp": 0, "gst": gst, "dp": 0, "other": 0}


def total(parts: dict) -> int:
    t = 0
    for k in ("brokerage", "stt", "exchange", "sebi", "stamp", "gst", "dp", "other"):
        t += parts[k]
    return t

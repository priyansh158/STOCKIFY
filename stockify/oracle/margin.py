"""T0 flat margin model (D-14; labelled approximate).

Requirement of a portfolio = Σ over its NRML futures and short NRML options
of |units| × reference price × flat rate (`margin_params.json` → `flat`:
`index_pct` for index underlyings, `stock_pct` for stocks). MIS uses its own
rule (`mis`, A4 §6.5: `round_half_up_div(units·price·mis_ppb, 10⁹)`). Reference price: futures → their mark;
short options → the underlying's last price, else the strike. Long options
need no margin (their premium is paid, X-10). On its expiry day a short index option adds the
expiry-day ELM (X-9, M4.7): |units| × the same reference × `expiry_day_short_index_option_addon`.
The total is rounded up to the next whole rupee: `ceil_to(ceil_div(x, 10⁹), 100)` (A4 §0.3, §6.5).

SPAN-lite replaces this in M4.8.
"""
from __future__ import annotations

from stockify import numerics

PPB = 1_000_000_000


def _in_force(rows: list[dict], key: str, date: str, applies: dict | None = None) -> int:
    best = None
    for r in rows:
        if r["effective_from"] <= date and all(r.get("applies", {}).get(k) == v for k, v in (applies or {}).items()):
            if best is None or r["effective_from"] > best["effective_from"]:
                best = r
    if best is None:
        raise RuntimeError(f"no margin row for {key} on {date}")
    return best[f"{key}_ppb"]


class FlatMargin:
    def __init__(self, bundle: dict):
        self.flat = bundle["margin_params"].get("flat", [])
        self.addon = bundle["margin_params"].get("expiry_day_short_index_option_addon", [])
        self.mis_rows = bundle["broker_policy"].get("mis_margin_pct", [])

    def _addon(self, date: str) -> int:
        """The expiry-day ELM rate in force, or 0 before its first row (2024-11-20, X-9)."""
        best = None
        for r in self.addon:
            if r["effective_from"] <= date and (best is None or r["effective_from"] > best["effective_from"]):
                best = r
        return best["pct_ppb"] if best else 0

    def mis_rate(self, segment: str, date: str) -> int:
        return _in_force(self.mis_rows, "pct", date, {"segment": segment})

    def requirement(self, legs, inst, price, date: str) -> int:  # noqa: ANN001
        """legs: [(iid, product, signed units)]; inst(iid) -> bundle row; price(iid) -> paise or None."""
        raw = 0                                         # paise × ppb
        for iid, product, units in legs:
            if not units:
                continue
            row = inst(iid)
            kind = row["kind"]
            if product != "NRML":
                continue
            if kind == "FUT":
                ref = price(iid) or 0
            elif kind in ("CE", "PE") and units < 0:
                ref = (price(row["underlying_id"]) if row.get("underlying_id") else None) or row["strike_paise"] or 0
            else:
                continue
            pct = _in_force(self.flat, "index_pct" if row.get("underlying_class") == "INDEX" else "stock_pct", date)
            raw += abs(units) * ref * pct
            if kind in ("CE", "PE") and row.get("underlying_class") == "INDEX" and \
                    (row.get("actual_expiry") or row["expiry"]) == date:
                raw += abs(units) * ref * self._addon(date)     # expiry-day ELM, underlying price × qty
        return numerics.ceil_to(numerics.ceil_div(raw, PPB), 100)

    def mis(self, kind: str, units: int, price: int, date: str) -> int:
        """MIS requirement (A4 §6.5): round_half_up_div(|units|·price·mis_ppb, 10⁹)."""
        seg = "FO" if kind in ("FUT", "CE", "PE") else "CM"
        return numerics.round_half_up_div(abs(units) * price * self.mis_rate(seg, date), PPB)

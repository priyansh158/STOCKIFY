"""T0 flat margin model (D-14; labelled approximate).

Requirement of a portfolio = Σ over its NRML futures and short NRML options
of |units| × reference price × flat rate (`margin_params.json` → `flat`:
`index_pct` for index underlyings, `stock_pct` for stocks). MIS uses its own
rule (`mis`, A4 §6.5: `round_half_up_div(units·price·mis_ppb, 10⁹)`). Reference price: futures → their mark;
short options → the underlying's last price, else the strike. Long options
need no margin (their premium is paid, X-10). On its expiry day a short index option adds the
expiry-day ELM (X-9, M4.7): |units| × the same reference × `expiry_day_short_index_option_addon`.
In the expiry week of a physically settled (stock) contract the delivery-margin schedule of Z-4 applies
(`broker_policy.json` → `delivery_margin_schedule`, D-60), with day offsets counted in the bundle's trading
days. The total is rounded up to the next whole rupee: `ceil_to(ceil_div(x, 10⁹), 100)` (A4 §0.3, §6.5);
inside, amounts are paise × 10¹⁸, so the schedule's min() and its 1.5 multiple stay exact.

SPAN-lite replaces this in M4.8.
"""
from __future__ import annotations

from stockify import numerics

PPB = 1_000_000_000
E18 = PPB * PPB


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
        self.schedule = bundle["broker_policy"].get("delivery_margin_schedule", [])
        # the bundle's days and its next_days (VOL 5.2): a one-day paper bundle counts trading days as the whole run does
        self.days = [d["date"] for d in bundle["days"] + list(bundle.get("next_days") or [])]
        self.labels: set[str] = set()               # added to the run's labels (D-60: delivery_margin_approx)
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
        raw = 0                                         # paise × 10¹⁸
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
            elif kind in ("CE", "PE"):
                raw += self._long_option_delivery(row, units, price, date)    # long options: premium, or Z-4
                continue
            else:
                continue
            pct = _in_force(self.flat, "index_pct" if row.get("underlying_class") == "INDEX" else "stock_pct", date)
            normal = abs(units) * ref * pct * PPB
            if kind in ("CE", "PE") and row.get("underlying_class") == "INDEX" and \
                    (row.get("actual_expiry") or row["expiry"]) == date:
                normal += abs(units) * ref * self._addon(date) * PPB         # expiry-day ELM, underlying price × qty
            raw += self._short_delivery(row, units, price, date, normal)
        return numerics.ceil_to(numerics.ceil_div(raw, E18), 100)

    # ── the stock F&O delivery-margin schedule (Z-4, D-60) ─────────────────

    def _offset(self, row: dict, date: str) -> int | None:
        """Trading days from `date` to the contract's expiry (0 on expiry, -1 the day before, …), counted in the
        bundle's days, so a holiday in expiry week moves the schedule a day earlier; None outside them."""
        if row.get("settlement") != "physical":
            return None
        expiry = row.get("actual_expiry") or row["expiry"]
        if date not in self.days or expiry not in self.days:
            return None
        return self.days.index(date) - self.days.index(expiry)

    def _rows(self, date: str, offset: int, applies_to: str) -> dict | None:
        best = None
        for r in self.schedule:
            if r["effective_from"] <= date and (best is None or r["effective_from"] > best["effective_from"]):
                best = r
        for x in (best or {}).get("rows", []):
            if x["day_offset"] == offset and x["applies_to"] == applies_to:
                return x
        return None

    def _contract_value(self, row: dict, units: int, price) -> int | None:  # noqa: ANN001
        spot = price(row["underlying_id"]) if row.get("underlying_id") else None
        return abs(units) * spot if spot else None

    def _short_delivery(self, row: dict, units: int, price, date: str, normal: int) -> int:  # noqa: ANN001
        """Futures and short options: normal NRML, but on expiry day min(50 % of contract value, 1.5 × NRML)."""
        off = self._offset(row, date)
        if off != 0:
            return normal
        cv = self._contract_value(row, units, price)
        x = self._rows(date, 0, "futures_and_short_options")
        if cv is None or x is None:
            return normal                               # D-60: no stock price → the normal margin
        by_value = cv * numerics.to_ppb(x["pct"]) * PPB
        by_nrml = normal * numerics.to_ppb(x["nrml_multiple"]) // PPB    # exact: normal is a multiple of 10⁹
        return min(by_value, by_nrml)

    def _long_option_delivery(self, row: dict, units: int, price, date: str) -> int:  # noqa: ANN001
        """Long stock options in expiry week (Z-4): a share of the contract value, or of the flat model's own
        requirement for the position as the VaR + ELM stand-in (labelled, D-60). Otherwise premium only (X-10)."""
        off = self._offset(row, date)
        if off is None:
            return 0
        cv = self._contract_value(row, units, price)
        if cv is None:
            return 0
        spot = cv // abs(units)
        k = row["strike_paise"]
        itm = spot > k if row["kind"] == "CE" else spot < k
        if itm:
            x = self._rows(date, off, "long_itm_options")
        else:
            near = abs(spot - k) * 100 <= spot if spot >= 10_000 else abs(spot - k) <= 100   # 1 % or ₹1 (Z-4)
            x = self._rows(date, off, "long_otm_near_spot") if near else None
        if x is None:
            return 0
        pct = numerics.to_ppb(x["pct"])
        if x["basis"] == "var_elm_adhoc":
            self.labels.add("delivery_margin_approx")
            return cv * _in_force(self.flat, "stock_pct", date) * pct
        return cv * pct * PPB

    def mis(self, kind: str, units: int, price: int, date: str) -> int:
        """MIS requirement (A4 §6.5): round_half_up_div(|units|·price·mis_ppb, 10⁹)."""
        seg = "FO" if kind in ("FUT", "CE", "PE") else "CM"
        return numerics.round_half_up_div(abs(units) * price * self.mis_rate(seg, date), PPB)

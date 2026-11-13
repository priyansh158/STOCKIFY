"""T1 SPAN-lite margin (M4.8, C-12; labelled `margin_spanlite`, approximate, I-14).

Per underlying, the NRML futures and options (long options too: they offset risk) are revalued together:

    scenarios: price moves m ∈ {0, +⅓, −⅓, +⅔, −⅔, +1, −1} × PSR, each with every leg's IV moved by ±VSR
               (relative: σ·(1 ± VSR)), 14 in all; plus two extreme moves ±extreme_multiple × PSR at an unchanged
               IV, whose losses count at extreme_weight (16 scenarios, as classic SPAN; D-57)
    a leg:     future → units × F·m·PSR_fut, where PSR_fut is `psr_futures_pct` when the row has it, else PSR
               (D-62: Kite's SPAN on futures is ~9.4 % of notional on 2026-11-09, more than options imply);
               option → units × (Black-76 at F·(1 + m·PSR), σ' − Black-76 at F, σ),
               where F is the leg's own forward (the M4.4 chain forward of its expiry; else the underlying)
    scan     = max(0, worst scenario loss); 0 when the underlying's legs are all bought options (MEZ 4.5: their
               loss cannot exceed their value, which the run already paid in cash; Kite's SPAN is 0 for them)
               with `net_option_value: true` in the row (D-63, textbook SPAN's net option value): scan =
               max(0, worst loss − Σ units × option value at the base), so bought options credit their value and
               sold options charge theirs (values: Black-76 at each leg's forward and IV, as the scenarios use)
    SOMC     = somc_per_lot_paise × short option lots
    ELM      = elm_pct × notional, for futures (F × |units|) and short options (underlying × |units|)
    add-on   = the expiry-day add-on on short index options expiring that day: pct × underlying × |units| (X-9)
    total    = max(scan, SOMC) + ELM + add-on, each part rounded up to the whole rupee (A4 §0.3)

The requirement is the sum over underlyings (no inter-commodity credit in v1). Each leg's IV comes from its mid
quote (the chain view's iv_mid); else its last valid IV today; else the ATM IV of the same expiry; else
`default_iv`; either fallback adds the label `margin_spanlite:iv_fallback`. Parameters are dated rows in
`margin_params.json` → `spanlite`, per underlying or per underlying class; the shipped ones are placeholders until
M4.9 calibrates them (their `calibration_id` says so and goes into the run's label).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from stockify import numerics
from stockify.fno import black76 as B
from stockify.oracle.margin import PPB, FlatMargin

MOVES = (0.0, 1 / 3, -1 / 3, 2 / 3, -2 / 3, 1.0, -1.0)
IV_FALLBACK_LABEL = "margin_spanlite:iv_fallback"
STEP_B3, STEP_B4 = "B3_spanlite_calibrated", "B4_spanlite_placeholder"


@dataclass(frozen=True)
class UnderlyingMargin:
    """One underlying's SPAN-lite breakdown, in paise, each part rounded up to the rupee (a margin_daily row)."""
    underlying_id: int
    scan: int
    somc: int
    elm: int
    addons: int
    step: str = ""      # the staircase step that priced it (MEZ 4.9): B3/B4 here, by the row's calibration_id

    @property
    def total(self) -> int:
        return max(self.scan, self.somc) + self.elm + self.addons


def _rupee_up(paise_ppb: int) -> int:
    """paise × 10⁹ → paise, rounded up to the whole rupee."""
    return numerics.ceil_to(numerics.ceil_div(paise_ppb, PPB), 100)


def step_of(row: dict) -> str:
    """The staircase step of a SPAN-lite row (MEZ 4.9): B4 for the shipped placeholder rows, else B3 (adopted)."""
    return STEP_B4 if row.get("calibration_id") == "placeholder" else STEP_B3


def _row(rows: list[dict], date: str, applies: dict) -> dict | None:
    best = None
    for r in rows:
        if r["effective_from"] <= date and all(r.get("applies", {}).get(k) == v for k, v in applies.items()):
            if best is None or r["effective_from"] > best["effective_from"]:
                best = r
    return best


class SpanLiteMargin:
    def __init__(self, bundle: dict, engine):  # noqa: ANN001
        params = bundle["margin_params"]
        self.rows = params.get("spanlite", []) or []
        self.addon_rows = params.get("expiry_day_short_index_option_addon", []) or []
        self._flat = FlatMargin(bundle)               # MIS shares: the same rule under every model (A4 §6.5)
        self._e = engine
        self._last_iv: dict[int, tuple[str, float]] = {}   # iid → (date, last valid mid IV that day)
        self.labels: set[str] = set()                 # labels this model used, merged into the run's (as FlatMargin)

    # MIS shares and the label are the flat model's / the bundle's (MIS futures and options arrive as NRML legs, D-80).
    def mis_rate(self, segment: str, date: str) -> int:
        return self._flat.mis_rate(segment, date)

    def mis(self, kind: str, units: int, price: int, date: str) -> int:
        return self._flat.mis(kind, units, price, date)

    def params(self, row: dict, date: str) -> dict:
        """The spanlite row for an instrument's underlying: one naming the underlying wins over its class."""
        found = (_row(self.rows, date, {"underlying": row.get("underlying")}) if row.get("underlying") else None) \
            or _row(self.rows, date, {"underlying_class": row.get("underlying_class") or "INDEX"})
        if found is None:
            raise RuntimeError(f"no spanlite margin row for {row.get('underlying')} on {date}")
        return found

    def _iv(self, row: dict, chain, date: str) -> float:  # noqa: ANN001
        iid = row["id"]
        mine = next((r for r in chain if r["instrument_id"] == iid), None) if chain is not None else None
        if mine is not None and mine["iv_mid"] is not None:
            self._last_iv[iid] = (date, mine["iv_mid"])
            return mine["iv_mid"]
        last = self._last_iv.get(iid)
        if last is not None and last[0] == date:
            return last[1]
        self._e.labels.add(IV_FALLBACK_LABEL)
        if chain is not None and chain.atm_strike_paise is not None:
            at_atm = [r for r in chain if r["strike_paise"] == chain.atm_strike_paise and r["iv_mid"] is not None]
            if at_atm:
                return at_atm[0]["iv_mid"]                # the call when both have one (rows are CE before PE)
        return self.params(row, date)["default_iv_ppb"] / PPB

    def breakdown(self, legs, inst, price, date: str) -> list[UnderlyingMargin]:  # noqa: ANN001
        """legs: [(iid, product, signed units)] → one UnderlyingMargin per underlying with NRML F&O legs."""
        by_und: dict[int, list[tuple[dict, int]]] = {}
        for iid, product, units in legs:
            row = inst(iid)
            if not units or product != "NRML" or row["kind"] not in ("FUT", "CE", "PE") or not row.get("underlying_id"):
                continue
            by_und.setdefault(row["underlying_id"], []).append((row, units))
        return [self._one(uid, by_und[uid], price, date) for uid in sorted(by_und)]

    def _one(self, uid: int, legs: list[tuple[dict, int]], price, date: str) -> UnderlyingMargin:  # noqa: ANN001
        e = self._e
        p = self.params(legs[0][0], date)
        psr, vsr = p["psr_pct_ppb"] / PPB, p["vsr_pct_ppb"] / PPB
        psr_fut = p["psr_futures_pct_ppb"] / PPB if "psr_futures_pct_ppb" in p else psr   # D-62
        ext_mult, ext_weight = float(p["extreme_multiple"]), p["extreme_weight_ppb"] / PPB
        und_paise = price(uid)
        rate = B.rate_on(e.bundle["pricing"].get("risk_free", []), date)

        valued = []                                     # (units, is_future, F rupees, K, t, σ, is_call)
        somc_lots = 0
        for row, units in legs:
            if row["kind"] == "FUT":
                f_paise = price(row["id"]) or und_paise
                if f_paise is None:
                    continue                            # nothing to value it at yet
                valued.append((units, True, f_paise / 100.0, 0.0, 0.0, 0.0, False))
                continue
            chain = self._chain(row)
            f_paise = self._forward(row, chain, und_paise)
            t = B.year_fraction(e.now, row["last_trading_ns"])  # always set: the bundle load checks it (D-61)
            sigma = self._iv(row, chain, date)
            valued.append((units, False, f_paise / 100.0, row["strike_paise"] / 100.0, t, sigma, row["kind"] == "CE"))
            if units < 0:
                lot = e.spec(row["id"], date)["lot"] if e.spec(row["id"], date) else 1
                somc_lots += -(-numerics.check_int64(abs(units)) // lot)       # lots, rounded up

        def value(f: float, k: float, t: float, sigma: float, is_call: bool) -> float:
            return B.price(is_call, f, k, t, rate, sigma)

        base = [value(f, k, t, s, c) if not fut else 0.0 for (_, fut, f, k, t, s, c) in valued]
        worst = 0.0
        scenarios = [(m, v, 1.0) for m in MOVES for v in (1.0, -1.0)]
        scenarios += [(ext_mult, 0.0, ext_weight), (-ext_mult, 0.0, ext_weight)]
        for move, vol, weight in scenarios:
            pnl = 0.0
            for (units, fut, f, k, t, s, c), v0 in zip(valued, base):
                shifted = f * (1.0 + move * (psr_fut if fut else psr))
                if fut:
                    pnl += units * (shifted - f)
                else:
                    pnl += units * (value(shifted, k, t, s * (1.0 + vol * vsr), c) - v0)
            worst = max(worst, -pnl * weight)
        if valued and all(units > 0 and not fut for (units, fut, *_) in valued):
            worst = 0.0                                 # MEZ 4.5: only bought options, premium paid in cash
        elif p.get("net_option_value") is True:         # D-63
            nov = 0.0
            for (units, fut, *_), v0 in zip(valued, base):
                if not fut:
                    nov += units * v0
            worst = max(0.0, worst - nov)
        scan = numerics.check_int64(int(math.ceil(worst))) * 100              # rupees → paise, rounded up to the rupee
        somc = numerics.ceil_to(p["somc_per_lot_paise"] * somc_lots, 100)
        elm, addons = self.rule_elm(legs, price, date, und_paise)
        return UnderlyingMargin(uid, scan, somc, elm, addons, step=step_of(p))

    # ── ELM and the expiry-day add-on from the dated rules (shared with the staircase's B2, MEZ 4.9) ──

    def _chain(self, row: dict):  # noqa: ANN202
        return self._e.ctx.chain(row["underlying"], row["expiry"]) if row.get("underlying") else None

    @staticmethod
    def _forward(row: dict, chain, und_paise: int | None) -> int:  # noqa: ANN001
        """An option's forward: the chain's (M4.4), else the underlying's price, else its strike (paise)."""
        return (chain.forward_paise if chain is not None and chain.forward_paise is not None
                else und_paise if und_paise is not None else row["strike_paise"])

    def exposure_notionals(self, legs: list[tuple[dict, int]], price, und_paise: int | None  # noqa: ANN001
                           ) -> list[tuple[dict, int]]:
        """(row, notional paise) of the legs that carry exposure margin: futures |units| × their price (else the
        underlying's; none while neither is known), sold options |units| × the underlying's price (else the
        option's forward). Bought options carry none."""
        out = []
        for row, units in legs:
            if row["kind"] == "FUT":
                f_paise = price(row["id"]) or und_paise
                if f_paise is not None:
                    out.append((row, f_paise * numerics.check_int64(abs(units))))
            elif units < 0:
                base = und_paise if und_paise is not None else numerics.check_int64(int(self._forward(row, self._chain(row), und_paise)))
                out.append((row, base * numerics.check_int64(abs(units))))
        return out

    def rule_elm(self, legs: list[tuple[dict, int]], price, date: str,  # noqa: ANN001
                 und_paise: int | None) -> tuple[int, int]:
        """(ELM, expiry-day add-on) in paise, each rounded up to the rupee: the row's `elm_pct` on every notional,
        and the X-9 add-on on short index options expiring today."""
        p = self.params(legs[0][0], date)
        addon = _row(self.addon_rows, date, {})
        elm_raw = addon_raw = 0                         # paise × ppb
        for row, notional in self.exposure_notionals(legs, price, und_paise):
            elm_raw += p["elm_pct_ppb"] * notional
            if row["kind"] != "FUT":
                expires_today = (row.get("actual_expiry") or row["expiry"]) == date
                if expires_today and addon is not None and row.get("underlying_class") == "INDEX":
                    addon_raw += addon["pct_ppb"] * notional
        return _rupee_up(elm_raw), _rupee_up(addon_raw)

    def requirement(self, legs, inst, price, date: str) -> int:  # noqa: ANN001
        return sum(b.total for b in self.breakdown(legs, inst, price, date))

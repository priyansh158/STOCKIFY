"""MEZ 4.9: the margin staircase (spec §5.1, §8) and the strict exact-SPAN model, in the oracle.

Per underlying per check (never mixing sources within one underlying: spread and option-value credits only make sense
together), the most accurate step that can price every one of its NRML legs:

    B1_span_nse_elm          exact SPAN from the NSE file in force at `now` (`span_exact.span_requirement`), plus
                             exposure at the ELM % of the day's NSE contract-wise file (bundle `elm[D]`), which already
                             holds the expiry-day uplift: no X-9 add-on. Needs the % of every charged leg (futures,
                             sold options) and the exact price of its notional; bought options need neither.
    B2_span_rule_elm         the same SPAN, plus ELM and the X-9 expiry-day add-on from the dated rules, exactly as
                             SPAN-lite charges them, its price fallbacks included (`SpanLiteMargin.rule_elm`; needs a
                             SPAN-lite row in force).
    B3_spanlite_calibrated   SPAN-lite with an adopted row in force (calibration_id other than "placeholder").
    B4_spanlite_placeholder  SPAN-lite with the placeholder row.
    B5_flat                  the flat model, on this underlying's legs.

The file in force (A1 §6.2): the last entry of `bundle["span"][D]` with created_ns <= now; before the first, none.
SPAN steps down when no file is in force, the file lacks the underlying or any leg's contract (a strike listed during
D), a leg has no SPAN contract key (FUT_CONT; unreachable: R_INSTRUMENT rejects its orders), or the file cannot be
applied exactly (`SpanCalcError`). A bundle with no `span` section at all is labelled `margin_span:no_section`.

B1 exposure (proven against Kite, `span_validate`): futures |units| x the future's own price; sold options short
units x the underlying index price; bought options none. A missing price is never substituted: the underlying goes
to B2. ELM % strings are converted exactly (per cent -> ppb); amounts are paise x 10^9, rounded up to the rupee per
part (A4 §0.3).

The pre-trade check (`pair`) prices each underlying before and after the order at ONE step, the lower of the two
each would get alone, so an order cannot need less margin by moving the book to another step.

margin_daily.csv rows (A1 §6.3): for B1/B2 `scan` holds the whole SPAN requirement and `somc` its short option
minimum (SPAN >= SOM, so total = SPAN + ELM + add-on); for B5 `scan` holds the flat requirement, the rest 0.

`SpanExactMargin` (`margin.model: span_exact`) is B1/B2 only. When SPAN cannot price an underlying, or the day's ELM
file is archived but lacks a charged leg's %, it raises `SpanMissingError` naming what is missing (never SPAN-lite,
never 0). With no ELM file for the day, or no exact price for a notional, it is exact SPAN with the rule ELM: B2.

Labels (I-14): `margin:<step>` for each step used and `margin_steps:<step>=<days>`, the number of run days on which
any check used it.
"""
from __future__ import annotations

from decimal import ROUND_CEILING, Context, Decimal, InvalidOperation

from stockify import numerics
from stockify.fno.span_exact import SpanCalcError, SpanMissingError, span_paise, span_requirement
from stockify.fno.span_file import CalendarSpread, CombinedCommodity, Contract, SpreadLeg, Tier
from stockify.fno.spanlite import STEP_B3, STEP_B4, SpanLiteMargin, UnderlyingMargin, _rupee_up, step_of
from stockify.oracle.margin import FlatMargin

STEP_B1, STEP_B2, STEP_B5 = "B1_span_nse_elm", "B2_span_rule_elm", "B5_flat"
STEPS = (STEP_B1, STEP_B2, STEP_B3, STEP_B4, STEP_B5)
NO_SECTION_LABEL = "margin_span:no_section"      # the bundle has no `span` section at all (not built by resolve)
_DERIVATIVES = ("FUT", "CE", "PE", "FUT_CONT")


def _tiers(rows: list[dict]) -> tuple[Tier, ...]:
    return tuple(Tier(t["number"], t["start"], t["end"], dict(t["rates"])) for t in rows)


def commodity_from_bundle(code: str, d: dict) -> CombinedCommodity:
    """A bundle span commodity (A1 §6.2) back as the reader's CombinedCommodity (what span_requirement reads)."""
    contracts = {(c["kind"], c["expiry"], c["strike_paise"] if c["kind"] != "FUT" else None):
                 Contract(c["price"], c["delta"], c["vol"], tuple(c["risk_array"]), c["composite_delta"])
                 for c in d["contracts"]}
    spreads = tuple(CalendarSpread(sp["number"], sp["charge_method"], dict(sp["rates"]),
                                   tuple(SpreadLeg(x["expiry"], x["side"], x["weight"]) for x in sp["legs"]))
                    for sp in d["calendar_spreads"])
    return CombinedCommodity(code=code, contracts=contracts, spot=d.get("spot"), cvf=dict(d["cvf"]),
                             som_method=d["som_method"], scan_tiers=_tiers(d["scan_tiers"]),
                             intra_tiers=_tiers(d["intra_tiers"]), inter_tiers=_tiers(d["inter_tiers"]),
                             som_tiers=_tiers(d["som_tiers"]), calendar_spreads=spreads)


def _key(row: dict) -> tuple | None:
    """A leg's SPAN contract key (kind, expiry, strike paise | None); None when it has none (FUT_CONT)."""
    if row["kind"] not in ("FUT", "CE", "PE") or not row.get("expiry"):
        return None
    return row["kind"], row["expiry"], None if row["kind"] == "FUT" else row["strike_paise"]


def _elm_ppb(pct: str | None) -> int | None:
    """An ELM per-cent decimal string ("2", "3.5") as an exact ppb fraction; None when absent or unusable."""
    if pct is None:
        return None
    try:
        value = Decimal(pct)
        if not value.is_finite() or value <= 0:
            return None
        exact = Context(prec=max(28, len(value.as_tuple().digits)))   # scaleb never rounds a long %
        return numerics.to_ppb(format(value.scaleb(-2, exact), "f"))
    except (InvalidOperation, ValueError):
        return None


class _NoSpan(Exception):
    """SPAN cannot price this underlying now; the message says why."""


class StaircaseMargin:
    strict = False

    def __init__(self, bundle: dict, engine):  # noqa: ANN001
        self._e = engine
        self._no_section = "span" not in bundle               # a hand-built bundle: labelled, steps down (5c)
        self._span = bundle.get("span") or {}
        self._elm = bundle.get("elm") or {}
        self._spanlite = SpanLiteMargin(bundle, engine)
        self._flat = FlatMargin(bundle)
        self._cc: dict[tuple, CombinedCommodity | None] = {}   # (day, entry no, underlying) -> commodity
        self._step_days: dict[str, set[str]] = {}

    # MIS is the same rule under every model (A4 §6.5).
    def mis_rate(self, segment: str, date: str) -> int:
        return self._flat.mis_rate(segment, date)

    def mis(self, kind: str, units: int, price: int, date: str) -> int:
        return self._flat.mis(kind, units, price, date)

    @property
    def labels(self) -> set[str]:
        out = set(self._flat.labels) | self._spanlite.labels | self._spanlite._flat.labels
        if self._no_section:
            out.add(NO_SECTION_LABEL)
        for step, days in self._step_days.items():
            out |= {f"margin:{step}", f"margin_steps:{step}={len(days)}"}
        return out

    def breakdown(self, legs, inst, price, date: str) -> list[UnderlyingMargin]:  # noqa: ANN001
        """legs: [(iid, product, signed units)] -> one UnderlyingMargin per underlying with NRML F&O legs."""
        by_und = self._group(legs, inst)
        out = []
        for uid in sorted(by_und):
            b = self._one(uid, by_und[uid], price, date)
            self._step_days.setdefault(b.step, set()).add(date)
            out.append(b)
        return out

    def requirement(self, legs, inst, price, date: str) -> int:  # noqa: ANN001
        return sum(b.total for b in self.breakdown(legs, inst, price, date))

    # ── The pre-trade check: before and after at one step ───────────────────

    def pair(self, before, after, inst, price_before, price_after, date: str) -> tuple[int, int]:  # noqa: ANN001
        """(requirement before, requirement after) for the pre-trade check. Each underlying is priced at ONE step for
        both: the lower of the steps each would get alone (an order adding a strike the file lacks prices the whole
        book at that lower step), so adding a leg can never lower the requirement through a change of step."""
        gb, ga = self._group(before, inst), self._group(after, inst)
        total_b = total_a = 0
        for uid in sorted(set(gb) | set(ga)):
            b = self._one(uid, gb[uid], price_before, date) if uid in gb else None
            a = self._one(uid, ga[uid], price_after, date) if uid in ga else None
            for _ in STEPS:                                      # each pass only moves down; at most five
                floor = max(STEPS.index(x.step) for x in (a, b) if x is not None)
                if b is not None and STEPS.index(b.step) < floor:
                    b = self._one(uid, gb[uid], price_before, date, floor)
                if a is not None and STEPS.index(a.step) < floor:
                    a = self._one(uid, ga[uid], price_after, date, floor)
                if len({x.step for x in (a, b) if x is not None}) == 1:
                    break
            for x in (a, b):
                if x is not None:
                    self._step_days.setdefault(x.step, set()).add(date)
            total_b += b.total if b is not None else 0
            total_a += a.total if a is not None else 0
        return total_b, total_a

    # ── One underlying ──────────────────────────────────────────────────────

    def _group(self, legs, inst) -> dict[int, list[tuple[dict, int]]]:  # noqa: ANN001
        by_und: dict[int, list[tuple[dict, int]]] = {}
        for iid, product, units in legs:
            row = inst(iid)
            if not units or product != "NRML" or row["kind"] not in _DERIVATIVES or not row.get("underlying_id"):
                continue
            by_und.setdefault(row["underlying_id"], []).append((row, units))
        return by_und

    def _one(self, uid: int, legs: list[tuple[dict, int]], price, date: str,  # noqa: ANN001
             floor: int = 0) -> UnderlyingMargin:
        """The first step from STEPS[floor] down that prices every leg of the underlying."""
        if floor <= STEPS.index(STEP_B2):
            try:
                return self._exact(uid, legs, price, date, allow_b1=floor == 0)
            except _NoSpan as exc:
                if self.strict:
                    raise SpanMissingError(f"span_exact: {exc}") from exc
            except SpanCalcError:                               # a file this code cannot apply exactly
                if self.strict:
                    raise
        priced = [(row, units) for row, units in legs if row["kind"] in ("FUT", "CE", "PE")]
        try:
            row = self._spanlite.params(legs[0][0], date)
        except RuntimeError:
            row = None
        if row is not None and priced and floor <= STEPS.index(STEP_B4):
            b = self._spanlite._one(uid, priced, price, date)
            return UnderlyingMargin(uid, b.scan, b.somc, b.elm, b.addons, step=step_of(row))
        flat = self._flat.requirement([(r["id"], "NRML", u) for r, u in legs], lambda i: _by_id(legs, i), price,
                                      date)
        return UnderlyingMargin(uid, flat, 0, 0, 0, step=STEP_B5)

    def _commodity(self, und: str, date: str) -> tuple[CombinedCommodity, str]:
        entries = self._span.get(date) or []
        chosen = None
        for n, entry in enumerate(entries):                     # in created order; the last one created <= now
            if entry["created_ns"] <= self._e.now:
                chosen = n
        if chosen is None:
            raise _NoSpan(f"no SPAN file in force for {und} at {self._e.now} on {date}")
        entry = entries[chosen]
        k = (date, chosen, und)
        if k not in self._cc:
            d = entry["commodities"].get(und)
            self._cc[k] = commodity_from_bundle(und, d) if d is not None else None
        if self._cc[k] is None:
            raise _NoSpan(f"{und} is not in {entry['file']}")
        return self._cc[k], entry["file"]

    def _b1_elm(self, und: str, uid: int, legs: list[tuple[dict, int]], price, date: str) -> int | None:  # noqa: ANN001
        """B1's exposure in paise (rounded up to the rupee), or None when B1 cannot price it: no ELM file for the day,
        a charged leg (future, sold option) without its %, or without the exact price its notional needs (the
        future's own price; the index for sold options). Under span_exact a day whose ELM file lacks a charged leg's
        % raises (a missing contract); with no ELM file for the day it is B2. Bought options need nothing."""
        if date not in self._elm:
            return None
        pcts = self._elm[date].get(und, {})
        und_paise = price(uid)
        raw = 0                                                 # paise x ppb
        for row, units in legs:
            if row["kind"] != "FUT" and units > 0:
                continue                                        # bought option: no exposure
            key = _key(row)
            ppb = _elm_ppb(pcts.get(f"{key[0]}|{key[1]}|{'' if key[2] is None else key[2]}"))
            if ppb is None:
                if self.strict:
                    raise SpanMissingError(f"span_exact: {und} {_name(key)} has no ELM % in the ELM file for {date}")
                return None
            base = price(row["id"]) if row["kind"] == "FUT" else und_paise   # paise per unit, exact: no substitute
            if base is None:
                return None
            raw += ppb * base * abs(units)
        return _rupee_up(raw)

    def _exact(self, uid: int, legs: list[tuple[dict, int]], price, date: str,  # noqa: ANN001
               allow_b1: bool = True) -> UnderlyingMargin:
        und = legs[0][0]["underlying"]
        keyed = []
        for row, units in legs:
            key = _key(row)
            if key is None:
                raise _NoSpan(f"{row.get('tradingsymbol') or row['id']} ({row['kind']}) has no SPAN contract")
            keyed.append((key, units))
        cc, file = self._commodity(und, date)
        res = span_requirement(cc, keyed)
        if res.missing:
            raise _NoSpan(f"{und} {', '.join(_name(k) for k in res.missing)} not in the SPAN file in force ({file}) "
                          f"on {date}")
        span = span_paise(res)
        som = int(res.som.to_integral_value(rounding=ROUND_CEILING)) * numerics.PAISE_PER_RUPEE  # rupees up -> paise
        elm = self._b1_elm(und, uid, legs, price, date) if allow_b1 else None
        if elm is not None:
            return UnderlyingMargin(uid, span, som, elm, 0, step=STEP_B1)
        try:
            elm, addons = self._spanlite.rule_elm(legs, price, date, price(uid))
        except RuntimeError as exc:                             # no dated rule row: B2 cannot price it either
            raise _NoSpan(f"no NSE ELM % for every {und} leg and {exc}") from exc
        return UnderlyingMargin(uid, span, som, elm, addons, step=STEP_B2)


def _name(key: tuple) -> str:
    return f"{key[0]} {key[1]}" + ("" if key[2] is None else f" {key[2]}")


def _by_id(legs: list[tuple[dict, int]], iid: int) -> dict:
    return next(row for row, _ in legs if row["id"] == iid)


class SpanExactMargin(StaircaseMargin):
    """Exact SPAN only (B1, or B2 without the day's ELM file): raises instead of stepping down."""
    strict = True

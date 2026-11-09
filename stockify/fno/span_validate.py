"""M4 (MEZ 4.8): the exact SPAN calculator and NSE's ELM % checked against the stored Kite basket-margin answers.

`python -m stockify margin span-validate --date 2026-11-09` recomputes every `margin_calibration` row of that IST
trade date offline (no Kite call, no network) and compares with Kite's `final.span` and `final.exposure`.

* Legs: each order's tradingsymbol is mapped through refdata (`instruments.find` / `instruments.get`, as the
  calibration's offline refit does) to the SPAN contract key (kind, expiry, strike paise); units are signed.
* SPAN file: the archived file of the day with the latest `created` at or before the row's `wall_ns` (no lookahead).
  None in force -> the sample is a miss, never a guess.
* SPAN: `span_requirement` (the code's composition, net option value included) and, for comparison only, the same
  without net option value: max(scan + intra spread - inter credit, SOM, 0).
* Exposure (spec §5.1, B1): the day's contract-wise ELM file %, applied as NSE and Kite apply it:
  futures |units| x the futures price; sold options short units x the underlying's price; bought options none.
  Prices are the stored snapshot's LTPs (paise), the only prices a row holds. Kite prices each answer at the moment
  it was asked, so the snapshot price can drift from Kite's; the convention check below is independent of that:
  Kite's per-order exposure / (units x ELM %) must be one price per sample (all option legs alike, all futures legs
  alike) on the 0.05 tick.
* Gate (spec §7): SPAN within ₹50 or 0.5 % of Kite's (whichever is larger) on at least 95 % of the samples, and
  exposure within ₹1 on at least 95 %.

The lab is only read: the store and refdata are opened read-only (`mode=ro`) for the run; the report
`$LAB/margin_calibration/span-validate-<date>.md` is the one file written (by the CLI).
"""
from __future__ import annotations

import contextlib
import gzip
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from stockify.fno.span_exact import SpanCalcError, SpanResult, span_requirement
from stockify.fno.span_file import ContractKey, SpanFile, parse_span

SPAN_ABS_TOL = Decimal(50)          # rupees
SPAN_REL_TOL = Decimal("0.005")     # 0.5 %
EXPOSURE_TOL = Decimal(1)           # rupees
GATE_SHARE = 95                     # per cent of samples
TICK = Decimal("0.05")              # rupees: NSE's index and futures tick
_KITE_PLACES = Decimal("0.000001")  # Kite answers in floats; 6 places keeps its 3-decimal values, drops float noise
COMPOSITIONS = ("with_nov", "no_nov")


def span_ok(ours: Decimal, kite: Decimal) -> bool:
    return abs(ours - kite) <= max(SPAN_ABS_TOL, abs(kite) * SPAN_REL_TOL)


def exposure_ok(ours: Decimal, kite: Decimal) -> bool:
    return abs(ours - kite) <= EXPOSURE_TOL


def file_in_force(files: list[tuple[str, int]], wall_ns: int) -> tuple[str, int] | None:
    """The (path, created_ns) with the latest created_ns at or before `wall_ns`; None if every file is later."""
    best = None
    for path, created in files:
        if created <= wall_ns and (best is None or created > best[1]):
            best = (path, created)
    return best


def _kite(value) -> Decimal:  # noqa: ANN001
    return Decimal(repr(float(value or 0))).quantize(_KITE_PLACES)


# ── Read-only access to the lab's databases ──────────────────────────────────

@contextlib.contextmanager
def read_only():
    """For the block, `stockify.store` and `stockify.data.refdata` answer from read-only connections (`mode=ro`), so
    their own readers (margin_calibrations, instruments.find / get) are used unchanged and nothing can be written."""
    from stockify import store
    from stockify.data import refdata
    saved, opened = [], []
    try:
        for mod in (store, refdata):
            conn = sqlite3.connect(Path(mod._db_path()).as_uri() + "?mode=ro", uri=True, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            opened.append(conn)
            saved.append((mod, mod._conn))
            mod._conn = conn
        yield
    finally:
        for mod, conn in saved:
            mod._conn = conn
        for conn in opened:
            conn.close()


# ── The ELM file ─────────────────────────────────────────────────────────────

_MONTH = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10,
          "NOV": 11, "DEC": 12}


def read_elm(day: date, underlyings: set[str]) -> dict[tuple[str, ContractKey], Decimal] | None:
    """{(underlying, contract key): ELM %} from the archived `F_AEL_OPT_CONTRACTS_<day>` file; None if not archived.
    Rows: instrument, symbol, expiry DD-MMM-YYYY, strike (rupees; -1 for futures), CE | PE | XX, 0, ELM %."""
    from stockify import paths
    from stockify.data import nse_margin_files as mf
    path = paths.nse_margin_dir("elm") / f"{day:%Y}" / mf.elm_name(day)
    if not path.exists():
        return None
    out = {}
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        for line in f:
            p = [c.strip() for c in line.split(",")]
            if len(p) < 7 or p[1] not in underlyings:
                continue
            dd, mon, yyyy = p[2].split("-")
            expiry = date(int(yyyy), _MONTH[mon.upper()], int(dd)).isoformat()
            if p[4] == "XX":
                key: ContractKey = ("FUT", expiry, None)
            else:
                key = (p[4], expiry, int(Decimal(p[3]) * 100))
            out[(p[1], key)] = Decimal(p[6])
    return out


# ── Samples ──────────────────────────────────────────────────────────────────

@dataclass
class Sample:
    sample_id: int
    underlying: str
    kind: str                       # portfolio type
    wall_ns: int
    legs: list[tuple[str, int]]     # (tradingsymbol, signed units)
    kite_span: Decimal
    kite_exposure: Decimal
    file: str | None = None         # the SPAN file used (name)
    result: SpanResult | None = None
    our_span: Decimal | None = None
    our_span_no_nov: Decimal | None = None
    our_exposure: Decimal | None = None
    kite_implied_prices: dict[str, Decimal] = field(default_factory=dict)  # "OPT" | "FUT" -> price (legs agree)
    convention_ok: bool | None = None   # None: Kite charged no exposure, nothing to check
    error: str | None = None

    def note(self, text: str) -> None:
        self.error = text if not self.error else f"{self.error}; {text}"

    def span_for(self, composition: str) -> Decimal | None:
        return self.our_span if composition == "with_nov" else self.our_span_no_nov

    def span_within(self, composition: str) -> bool:
        ours = self.span_for(composition)
        return ours is not None and span_ok(ours, self.kite_span)

    def exposure_within(self) -> bool:
        return self.our_exposure is not None and exposure_ok(self.our_exposure, self.kite_exposure)


@dataclass
class ValidationReport:
    day: date
    samples: list[Sample]

    def span_within(self, composition: str = "with_nov", group: list[Sample] | None = None) -> tuple[int, int]:
        group = self.samples if group is None else group
        return sum(s.span_within(composition) for s in group), len(group)

    def exposure_within(self, group: list[Sample] | None = None) -> tuple[int, int]:
        group = self.samples if group is None else group
        return sum(s.exposure_within() for s in group), len(group)

    def convention(self) -> tuple[int, int]:
        checked = [s for s in self.samples if s.convention_ok is not None]
        return sum(bool(s.convention_ok) for s in checked), len(checked)

    def groups(self) -> dict[tuple[str, str], list[Sample]]:
        out: dict[tuple[str, str], list[Sample]] = {}
        for s in self.samples:
            out.setdefault((s.underlying, s.kind), []).append(s)
        return dict(sorted(out.items()))

    def gate(self) -> dict:
        def passes(n: int, total: int) -> bool:
            return total > 0 and n * 100 >= GATE_SHARE * total
        span, exposure = self.span_within("with_nov"), self.exposure_within()
        out = {"span": span, "span_no_nov": self.span_within("no_nov"), "exposure": exposure,
               "span_passed": passes(*span), "span_no_nov_passed": passes(*self.span_within("no_nov")),
               "exposure_passed": passes(*exposure), "convention": self.convention()}
        out["passed"] = out["span_passed"] and out["exposure_passed"]
        return out

    def worst(self, n: int = 10, composition: str = "with_nov") -> list[Sample]:
        def miss(s: Sample) -> Decimal:
            ours = s.span_for(composition)
            return Decimal("Infinity") if ours is None else abs(ours - s.kite_span)
        return sorted(self.samples, key=lambda s: (-miss(s), s.sample_id))[:n]


def _legs(doc: dict) -> list[tuple[str, int, int | None, ContractKey | None]]:
    """(tradingsymbol, signed units, instrument id, contract key) per order; refdata as the calibration refit uses."""
    from stockify.data import instruments
    out = []
    for o in doc["legs"]:
        units = int(o["quantity"]) * (1 if o["transaction_type"] == "BUY" else -1)
        iid = instruments.find(o["exchange"], o["tradingsymbol"])
        info = instruments.get(iid) if iid is not None else None
        key = None
        if info is not None and info["kind"] in ("FUT", "CE", "PE"):
            key = (info["kind"], info["expiry"], info["strike_paise"] if info["kind"] != "FUT" else None)
        out.append((o["tradingsymbol"], units, iid, key))
    return out


def _price(quote: dict | None) -> Decimal | None:
    ltp = (quote or {}).get("ltp")
    return None if ltp is None else Decimal(int(ltp)) / 100  # paise -> rupees, exact


def _exposure(s: Sample, legs, market: dict, elm: dict | None, kite_orders: dict[str, Decimal]) -> None:  # noqa: ANN001
    if elm is None:
        s.note("no ELM file archived for the day")
        return
    total, implied, problems = Decimal(0), {}, []
    spot = _price(market.get("underlying"))
    for sym, units, iid, key in legs:
        pct = elm.get((s.underlying, key)) if key else None
        if pct is None:
            problems.append(f"{sym}: not in the ELM file")
            continue
        if key[0] == "FUT":
            price = _price(market.get(str(iid)))
            notional_units = abs(units)
        else:
            price, notional_units = spot, (-units if units < 0 else 0)  # bought options carry no exposure
        if notional_units:
            if price is None:
                problems.append(f"{sym}: no price in the snapshot")
                continue
            total += notional_units * price * pct / 100  # units x rupees/unit x % = rupees
        kite = kite_orders.get(sym)
        if kite:
            implied.setdefault("FUT" if key[0] == "FUT" else "OPT", []).append(kite / abs(units) / pct * 100)
    if problems:
        s.note("; ".join(problems))
    else:
        s.our_exposure = total
    if implied:
        s.kite_implied_prices = {g: v[0] for g, v in implied.items() if len(set(v)) == 1}
        s.convention_ok = len(s.kite_implied_prices) == len(implied) and all(
            (p / TICK) == (p / TICK).to_integral_value() for p in s.kite_implied_prices.values())


def validate(day: date, underlyings: list[str]) -> ValidationReport:
    """Recompute every stored calibration row of IST trade date `day` for `underlyings`."""
    from stockify import store, timeutil
    from stockify.data import nse_margin_files as mf
    wanted = set(underlyings)
    files = mf.in_force_files(day)
    elm = read_elm(day, wanted)
    parsed: dict[str, SpanFile] = {}
    samples = []
    with read_only():
        rows = [r for u in underlyings for r in store.margin_calibrations(u)
                if timeutil.ns_to_ist(r["wall_ns"]).date() == day]
        for r in sorted(rows, key=lambda r: r["sample_id"]):
            doc, kite, market = json.loads(r["portfolio_json"]), json.loads(r["kite_json"]), json.loads(r["market_json"])
            final = kite.get("final") or {}
            legs = _legs(doc)
            s = Sample(r["sample_id"], r["underlying"], doc.get("kind", "?"), r["wall_ns"],
                       [(sym, u) for sym, u, _, _ in legs], _kite(final.get("span")), _kite(final.get("exposure")))
            samples.append(s)
            kite_orders: dict[str, Decimal] = {}
            for o in kite.get("orders") or []:
                kite_orders[o.get("tradingsymbol")] = kite_orders.get(o.get("tradingsymbol"), Decimal(0)) + _kite(
                    o.get("exposure"))
            unknown = [sym for sym, _, _, key in legs if key is None]
            if unknown:
                s.note(f"not in refdata: {', '.join(unknown)}")
                continue
            _exposure(s, legs, market, elm, kite_orders)
            chosen = file_in_force(files, r["wall_ns"])
            if chosen is None:
                s.note("no SPAN file in force at the row's time")
                continue
            path = chosen[0]
            s.file = Path(path).name
            if path not in parsed:
                parsed[path] = parse_span(path, wanted)
            cc = parsed[path].commodities.get(s.underlying)
            if cc is None:
                s.note(f"{s.underlying} not in {s.file}")
                continue
            try:
                res = span_requirement(cc, [(key, u) for _, u, _, key in legs])
            except SpanCalcError as exc:
                s.note(str(exc))
                continue
            if res.missing:
                s.note(f"not in {s.file}: {res.missing}")
                continue
            s.result, s.our_span = res, res.span
            s.our_span_no_nov = max(res.scan + res.intra_spread - res.inter_credit, res.som, Decimal(0))
    return ValidationReport(day, samples)


# ── The report ───────────────────────────────────────────────────────────────

WORDS = {"single": "one option", "straddle": "straddle", "strangle": "strangle", "vertical": "vertical spread",
         "iron_fly": "iron fly", "iron_condor": "iron condor", "future": "future", "hedged_future": "future + option"}


def _r(x: Decimal | None) -> str:
    return "—" if x is None else f"₹{x.quantize(Decimal('0.01')):,}"


def _frac(n: int, total: int) -> str:
    return f"{n}/{total}"


def report_text(rep: ValidationReport) -> str:
    g = rep.gate()
    lines = [f"# Exact SPAN against Kite, {rep.day.isoformat()}", "",
             f"{len(rep.samples)} stored Kite basket-margin answers recomputed offline with the SPAN file in force at "
             "each sample's time and the day's NSE ELM file. SPAN passes when within ₹50 or 0.5 % of Kite's (whichever "
             "is larger); exposure when within ₹1. The gate needs 95 % of samples on each.", "",
             f"**Gate: {'PASSED' if g['passed'] else 'FAILED'}** — SPAN {_frac(*g['span'])} "
             f"({'pass' if g['span_passed'] else 'fail'}), exposure {_frac(*g['exposure'])} "
             f"({'pass' if g['exposure_passed'] else 'fail'}).", "",
             f"- SPAN with net option value (the code's composition): {_frac(*g['span'])}",
             f"- SPAN without net option value (for comparison): {_frac(*g['span_no_nov'])}",
             f"- Exposure within ₹1 at the snapshot's prices: {_frac(*g['exposure'])}",
             f"- Kite's exposure convention (per-order exposure / (units × ELM %) is one price on the 0.05 tick): "
             f"{_frac(*g['convention'])} of the samples Kite charged exposure on", ""]
    n_conv, total_conv = g["convention"]
    if not g["exposure_passed"] and total_conv and n_conv == total_conv:
        lines += ["Every Kite exposure is the ELM % times one price on the tick (the underlying for sold options, the "
                  "future for futures), but that price is the live one when each answer was asked, not the snapshot "
                  "price the row stores: the convention matches, the stored price does not. See \"Kite's implied "
                  "price\" below.", ""]
    lines += [
             "| Underlying | Portfolio | Samples | SPAN within (with NOV) | SPAN within (no NOV) | Exposure within ₹1 |"
             " Worst SPAN miss |",
             "|---|---|---|---|---|---|---|"]
    for (und, kind), group in rep.groups().items():
        worst = max((abs(s.our_span - s.kite_span) for s in group if s.our_span is not None), default=None)
        lines.append(f"| {und} | {WORDS.get(kind, kind)} | {len(group)} | {_frac(*rep.span_within('with_nov', group))}"
                     f" | {_frac(*rep.span_within('no_nov', group))} | {_frac(*rep.exposure_within(group))} | "
                     f"{_r(worst)} |")
    lines += ["", "## Worst 10 SPAN samples", "",
              "| Sample | Portfolio | Legs | File | Kite SPAN | Ours | Ours, no NOV | Kite exposure | Ours | Note |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for s in rep.worst(10):
        legs = ", ".join(f"{u:+d} {sym}" for sym, u in s.legs)
        lines.append(f"| {s.sample_id} | {s.underlying} {WORDS.get(s.kind, s.kind)} | {legs} | {s.file or '—'} | "
                     f"{_r(s.kite_span)} | {_r(s.our_span)} | {_r(s.our_span_no_nov)} | {_r(s.kite_exposure)} | "
                     f"{_r(s.our_exposure)} | {s.error or ''} |")
    exp = sorted((s for s in rep.samples if s.our_exposure is not None),
                 key=lambda s: (-abs(s.our_exposure - s.kite_exposure), s.sample_id))[:10]
    lines += ["", "## Worst 10 exposure samples", "",
              "| Sample | Portfolio | Kite exposure | Ours | Kite's implied price | Convention |", "|---|---|---|---|---|---|"]
    for s in exp:
        implied = ", ".join(f"{k} {_r(v)}" for k, v in sorted(s.kite_implied_prices.items())) or "—"
        lines.append(f"| {s.sample_id} | {s.underlying} {WORDS.get(s.kind, s.kind)} | {_r(s.kite_exposure)} | "
                     f"{_r(s.our_exposure)} | {implied} | {'ok' if s.convention_ok else s.convention_ok} |")
    errors = [s for s in rep.samples if s.error]
    if errors:
        lines += ["", "## Samples that could not be recomputed", ""]
        lines += [f"- {s.sample_id}: {s.error}" for s in errors]
    return "\n".join(lines) + "\n"

"""MEZ 4.11: the pre-trade margin check and the staircase scorecard, offline.

`python -m stockify margin check --leg "SELL 65 NIFTY26N1724950CE" [--leg …] [--date D] [--at HH:MM]`
    prices a proposed basket with the margin staircase (`staircase.StaircaseMargin`, B1 → B5) exactly as a run would
    at that moment: the bundle's own `span_section` / `elm_section` build the files usable on D without lookahead
    (the previous trading day's `.s`, then D's `i1`..`i4`; never D's `.s`), and the file in force is the last one
    created at or before `--at` (IST; default 15:30, the close). `--date` defaults to the latest archived day.
    Prices come from that same file, because nothing else in the archive has them: each future at its SPAN price,
    the index at the file's spot. So B1's exposure is the ELM % times those prices; live prices move it.
    Legs are Kite tradingsymbols on NFO (`NFO:` may be written); one refdata does not know is an error.

`python -m stockify margin scorecard`
    for every stored Kite basket-margin answer (`store.margin_calibrations`), what EACH step would have given, not just
    the step the staircase would choose, against Kite's span + exposure + additional (`calibrate.kite_target`):

    B1  `span_validate.validate` (the proven path: the SPAN file in force at the row's time, the day's ELM file, the
        row's snapshot LTPs), each part rounded up to the rupee as the staircase does (`span_paise`; exposure up).
    B2  the same SPAN, plus `SpanLiteMargin.rule_elm` (the dated rule's ELM and the X-9 add-on) at the same LTPs.
    B3  SPAN-lite with an adopted (calibrated) row in force; none is adopted yet, so B3 is reported as not available.
    B4  SPAN-lite with the placeholder row; B5 the flat model. Both through `calibrate.Pricer` on the run's snapshot
        rebuilt by `calibrate.snapshot_from_rows`, as the calibration priced them (its mark: mid, else LTP).

    Errors are |ours − Kite's| in paise, per (underlying, portfolio type), for the total and, where a step has the
    split, for SPAN and exposure separately (B5 has none). Median and 90th percentile: linear interpolation between
    the two nearest ranks at rank (n − 1)·q of the sorted errors (Hyndman–Fan type 7), exact over the paise integers
    (`fractions.Fraction`), shown rounded half up to the paisa. Relative error: |ours − Kite's| / Kite's, for answers
    with Kite's total > 0. An inversion: a less exact step with a strictly lower median total error than a more exact
    one, on the answers both priced, within one group.

Nothing is written: the lab is read through read-only connections (`span_validate.read_only`); the archive's path
helpers may create empty folders, as everywhere in the archive code. No Kite call, no network.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from fractions import Fraction
from pathlib import Path

from stockify import numerics
from stockify.fno import span_validate as sv
from stockify.fno.staircase import STEP_B1, STEP_B2, STEP_B5, STEPS
from stockify.fno.spanlite import STEP_B3, STEP_B4, UnderlyingMargin

DEFAULT_AT = "15:30"
SIDES = ("BUY", "SELL")
STEP_WORDS = {STEP_B1: "exact SPAN + NSE's ELM file", STEP_B2: "exact SPAN + the dated rule's ELM",
              STEP_B3: "SPAN-lite, calibrated", STEP_B4: "SPAN-lite, placeholder parameters",
              STEP_B5: "flat % of notional"}
MEDIAN, P90 = Fraction(1, 2), Fraction(9, 10)
_SPAN_NAME = re.compile(r"nsccl\.(\d{8})\.\w+\.zip$")


class CheckError(ValueError):
    """The check cannot be priced as asked; the message says why."""


class CheckInputError(CheckError):
    """The request is wrong: a malformed leg, a symbol refdata does not know, not a future or option (exit 2)."""


class CheckDataError(CheckError):
    """The archive cannot price the request: no file archived or in force, or no price for a leg (exit 1)."""


def _rupees(paise) -> str:  # noqa: ANN001
    from stockify.oracle.report import _rupees as fmt   # en-IN grouping: ₹2,19,708.00
    return fmt(paise)


def _paise(rupees: str) -> int:
    """A SPAN file's price string as paise: exact when it is whole paise, else rounded half up to the paisa."""
    try:
        return numerics.to_paise(rupees)
    except numerics.NumericError:
        return numerics.to_paise(format(Decimal(rupees).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f"))


# ── margin check ─────────────────────────────────────────────────────────────

def parse_leg(text: str) -> tuple[str, int, str]:
    """"SELL 65 NIFTY26N1724950CE" -> ("SELL", 65, "NIFTY26N1724950CE"); units are positive."""
    parts = text.split()
    if len(parts) != 3 or parts[0].upper() not in SIDES:
        raise CheckInputError(f"a leg is \"BUY or SELL, units, tradingsymbol\", e.g. \"SELL 65 NIFTY26N1724950CE\"; "
                         f"got {text!r}")
    try:
        units = int(parts[1])
    except ValueError:
        units = 0
    if units <= 0:
        raise CheckInputError(f"units must be a positive whole number; got {parts[1]!r} in {text!r}")
    return parts[0].upper(), units, parts[2].upper()


def archived_days() -> list[date]:
    """Trade dates with at least one archived SPAN file, oldest first."""
    from stockify import paths
    days = set()
    for p in paths.nse_margin_dir("span").glob("*/nsccl.*.zip"):
        m = _SPAN_NAME.search(p.name)
        if m:
            d = m.group(1)
            try:
                days.add(date(int(d[:4]), int(d[4:6]), int(d[6:])))
            except ValueError:
                continue
    return sorted(days)


def latest_archived_day() -> date | None:
    days = archived_days()
    return days[-1] if days else None


@dataclass
class CheckResult:
    day: date
    at: str
    now_ns: int
    legs: list[tuple[str, int, str]]
    file: str | None                       # the SPAN file in force (name)
    file_created_ns: int | None
    parts: list[tuple[str, UnderlyingMargin]]   # (underlying, its staircase breakdown)
    prices: dict[str, int]                 # what each price came from -> paise
    labels: list[str] = field(default_factory=list)
    at_defaulted: bool = False
    kite: dict | None = None               # `--live`: Kite's answer in paise (span, exposure, additional, premium)

    @property
    def kite_total(self) -> int | None:
        """Kite's span + exposure + additional (D-62: not the premium), when it was asked."""
        k = self.kite
        return None if k is None else k["span_paise"] + k["exposure_paise"] + k["additional_paise"]

    @property
    def total(self) -> int:
        return sum(b.total for _, b in self.parts)


def _rows_for(legs: list[tuple[str, int, str]], day: date) -> tuple[dict[int, dict], list[tuple[int, int]], dict]:
    """Bundle-shaped refdata rows (calibrate._row_for) for the legs and their underlyings; [(iid, signed units)];
    {underlying: index id}. Raises CheckError for a symbol refdata does not know or that is not a future/option."""
    from stockify.data import instruments
    from stockify.fno import calibrate
    rows: dict[int, dict] = {}
    signed, unds = [], {}
    for side, units, sym in legs:
        exch, _, name = sym.rpartition(":")
        exch = exch or "NFO"
        iid = instruments.find(exch, name)
        if iid is None:
            raise CheckInputError(f"{sym}: not in refdata (use Kite's tradingsymbol, e.g. NIFTY26N1724950CE; "
                             f"`refdata sync` adds new contracts)")
        info = instruments.get(iid)
        if info["kind"] not in ("FUT", "CE", "PE") or not info.get("underlying"):
            raise CheckInputError(f"{sym}: a {info['kind']}, not a future or option")
        und = info["underlying"]
        if und not in unds:
            try:
                unds[und] = calibrate._underlying_row(und)
            except calibrate.CalibrationError as exc:
                raise CheckDataError(str(exc)) from exc
            index_id, is_index = unds[und]
            rows[index_id] = calibrate._row_for(index_id, "INDEX" if is_index else "EQ", und, index_id, is_index,
                                                day.isoformat())
        index_id, is_index = unds[und]
        rows[iid] = calibrate._row_for(iid, info["kind"], und, index_id, is_index, day.isoformat())
        signed.append((iid, units if side == "BUY" else -units))
    return rows, signed, {u: v[0] for u, v in unds.items()}


def _ask_kite(rows: dict[int, dict], signed: list[tuple[int, int]], client) -> dict:  # noqa: ANN001
    """Kite's `/margins/basket` answer for the basket (read-only, `consider_positions=False`), as paise."""
    from stockify.daemon.margin_poller import MarginPoller, kite_parts
    from server.log_redaction import redact
    try:
        answer = client().basket_order_margins(
            MarginPoller._orders([(rows[i], u) for i, u in signed]), consider_positions=False)
        return kite_parts(answer)
    except Exception as exc:  # noqa: BLE001 — a failed ask is reported, never a traceback; secrets stay out of it
        raise CheckDataError(f"Kite did not answer: {redact(' '.join(str(exc).split()))[:300]}") from exc


def check(legs: list[tuple[str, int, str]], day: date | None = None, at: str | None = None, *, live: bool = False,
          client=None) -> CheckResult:  # noqa: ANN001
    """The staircase requirement of the basket at `at` IST on `day` (default: the latest archived day). With `live`
    Kite is also asked for the same basket (IWB 5.1; `client` is a factory of a ReadOnlyKite, default
    `kite_readonly.client`); the staircase itself never reads that answer."""
    from stockify import bundle as B
    from stockify import timeutil
    from stockify.data import refdata
    from stockify.fno import calibrate
    from stockify.fno.staircase import StaircaseMargin
    if day is None:
        day = latest_archived_day()
        if day is None:
            raise CheckDataError("no NSE SPAN file is archived (`nse margin-files --date D` archives a day)")
    defaulted = at is None
    at = at or DEFAULT_AT
    now_ns = timeutil.ist_ns(day, at)
    d = day.isoformat()
    try:                                                   # a clean message, not a traceback, without refdata
        sqlite3.connect(Path(refdata._db_path()).as_uri() + "?mode=ro", uri=True).close()
    except sqlite3.OperationalError as exc:
        raise CheckDataError(f"refdata cannot be opened read-only ({exc}); `refdata sync` creates it") from exc
    with sv.read_only(refdata):                            # the check needs refdata only, never the store
        rows, signed, index_of = _rows_for(legs, day)
        inst_rows = list(rows.values())
        span = B.span_section(inst_rows, [{"date": d}])
        elm = B.elm_section(inst_rows, [{"date": d}])
        entries = span.get(d) or []
        chosen = None
        for entry in entries:                              # created order: the last one created <= now
            if entry["created_ns"] <= now_ns:
                chosen = entry
        if chosen is None:
            usable = ", ".join(f"{Path(e['file']).name} (created "
                               f"{timeutil.ns_to_ist(e['created_ns']):%Y-%m-%d %H:%M})" for e in entries) or "none"
            raise CheckDataError(f"no SPAN file in force on {d} at {at} IST (usable that day: {usable}); offline, "
                                 "nothing else prices the legs")
        quotes, prices = {}, {}
        file = Path(chosen["file"]).name
        for und, index_id in index_of.items():
            cc = chosen["commodities"].get(und)
            if cc is None:                                 # never a silent ₹0: nothing else prices its legs
                raise CheckDataError(f"{und} is not in {file}, the SPAN file in force; offline, nothing prices its "
                                     "legs")
            if cc.get("spot"):
                quotes[index_id] = _paise(cc["spot"])
                prices[f"{und} spot"] = quotes[index_id]
            by_key = {(c["kind"], c["expiry"], c["strike_paise"]): c["price"] for c in cc["contracts"]}
            for iid, row in rows.items():
                if row.get("underlying") != und or row["kind"] not in ("FUT", "CE", "PE"):
                    continue
                key = (row["kind"], row["expiry"], None if row["kind"] == "FUT" else row["strike_paise"])
                if by_key.get(key):
                    quotes[iid] = _paise(by_key[key])
                    prices[row["tradingsymbol"]] = quotes[iid]
        # A future without its price, or a sold option without the index's, would be margined at ₹0 below B1.
        unpriced = [rows[i]["tradingsymbol"] for i, u in signed
                    if (rows[i]["kind"] == "FUT" and i not in quotes)
                    or (rows[i]["kind"] != "FUT" and u < 0 and rows[i]["underlying_id"] not in quotes)]
        if unpriced:
            raise CheckDataError(f"no price in {file} for {', '.join(unpriced)} (missing: the contract, or the "
                                 "index spot); offline, nothing else prices it")
        first_und, first = next(iter(index_of.items()))
        snap = calibrate.Snapshot(first_und, d, now_ns, first, rows,
                                  {i: {"ltp": p, "bid": None, "ask": None, "oi": None} for i, p in quotes.items()})
        margin, pricing = calibrate._config_params()
        engine = calibrate._SnapshotEngine(snap, margin, pricing)
        engine.bundle.update(span=span, elm=elm)
        model = StaircaseMargin(engine.bundle, engine)
        parts = model.breakdown([(i, "NRML", u) for i, u in signed], rows.__getitem__, quotes.get, d)
    named = [(next(u for u, i in index_of.items() if i == b.underlying_id), b) for b in parts]
    kite = None
    if live:
        if client is None:
            from stockify import kite_readonly
            client = kite_readonly.client
        kite = _ask_kite(rows, signed, client)
    return CheckResult(day, at, now_ns, legs, file, chosen["created_ns"], named, prices,
                       sorted(model.labels | engine.labels), at_defaulted=defaulted, kite=kite)


def check_text(res: CheckResult) -> str:
    from stockify import timeutil
    created = timeutil.ns_to_ist(res.file_created_ns)
    at = f"{res.at} IST" + (" (default: the close)" if res.at_defaulted else "")
    lines = [f"Margin for {len(res.legs)} leg{'s' if len(res.legs) != 1 else ''} on {res.day.isoformat()} at {at}, "
             "NRML, offline from the archived NSE files", ""]
    lines += [f"  {side} {units} {sym}" for side, units, sym in res.legs]
    lines += ["", f"SPAN file in force: {res.file} (created {created:%Y-%m-%d %H:%M} IST; none created later is used)"]
    for und, b in res.parts:
        lines += ["", f"{und}: step {b.step} ({STEP_WORDS.get(b.step, b.step)})"]
        if b.step in (STEP_B1, STEP_B2):
            lines += [f"  SPAN                {_rupees(b.scan)}",
                      f"  (short option min.  {_rupees(b.somc)}: SPAN already covers it)"]
        elif b.step in (STEP_B3, STEP_B4):
            lines += [f"  scan                {_rupees(b.scan)}", f"  short option min.   {_rupees(b.somc)}"]
        else:
            lines += [f"  flat                {_rupees(b.scan)}"]
        if b.step != STEP_B5:
            lines += [f"  exposure (ELM)      {_rupees(b.elm)}", f"  expiry-day add-on   {_rupees(b.addons)}"]
        lines.append(f"  total               {_rupees(b.total)}")
    lines += ["", f"Requirement: {_rupees(res.total)}", ""]
    if res.kite is not None:
        k = res.kite
        lines += ["Kite's answer (live, consider_positions=false; read-only, no order is placed):",
                  f"  SPAN                {_rupees(k['span_paise'])}",
                  f"  exposure            {_rupees(k['exposure_paise'])}",
                  f"  additional          {_rupees(k['additional_paise'])}",
                  f"  total               {_rupees(res.kite_total)}   (the premium of bought options, "
                  f"{_rupees(k['option_premium_paise'])}, is not margin: D-62)",
                  f"The staircase minus Kite: {_rupees(res.total - res.kite_total)}", ""]
    if res.prices:
        lines.append("Prices (from the SPAN file; live prices move the exposure): "
                     + ", ".join(f"{k} {_rupees(v)}" for k, v in sorted(res.prices.items())))
    if res.labels:
        lines.append("Labels: " + ", ".join(res.labels))
    return "\n".join(lines) + "\n"


# ── margin scorecard ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StepValue:
    total: int                  # paise
    span: int | None            # the SPAN part (B3/B4: max(scan, SOMC)); None when the step has no split (B5)
    exposure: int | None        # ELM + expiry-day add-on


@dataclass
class ScoreRow:
    sample_id: int
    underlying: str
    kind: str
    day: date
    kite_total: int             # paise: Kite's span + exposure + additional
    kite_span: int | None
    kite_exposure: int | None
    steps: dict[str, StepValue]
    expiry_day: bool = False    # a leg's contract expires on the answer's own day (the X-9 add-on can apply)


@dataclass(frozen=True)
class Inversion:
    group: tuple[str, str]
    better: str                 # the less exact step, closer to Kite
    worse: str                  # the more exact step
    better_median: Fraction
    worse_median: Fraction
    n: int                      # answers both priced


def quantile(values: list[int], q: Fraction) -> Fraction | None:
    """Linear interpolation between the two nearest ranks at rank (n - 1)·q of the sorted values, exact."""
    xs = sorted(values)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    lo = int(k)                                     # floor: k >= 0
    hi = min(lo + 1, len(xs) - 1)
    return Fraction(xs[lo]) + (xs[hi] - xs[lo]) * (k - lo)


@dataclass
class Scorecard:
    rows: list[ScoreRow]
    notes: list[str]
    live_answers: int = 0       # margin_live rows with Kite's answer (IWB 5.1: the daemon's poller)
    live_errors: int = 0        # margin_live rows that are an error (403, 429, network, a refused answer)

    def groups(self) -> dict[tuple[str, str], list[ScoreRow]]:
        out: dict[tuple[str, str], list[ScoreRow]] = {}
        for r in self.rows:
            out.setdefault((r.underlying, r.kind), []).append(r)
        return dict(sorted(out.items()))

    @staticmethod
    def errors(rows: list[ScoreRow], step: str, part: str = "total") -> list[int]:
        out = []
        for r in rows:
            v = r.steps.get(step)
            kite = {"total": r.kite_total, "span": r.kite_span, "exposure": r.kite_exposure}[part]
            ours = None if v is None else getattr(v, part)
            if ours is not None and kite is not None:
                out.append(abs(ours - kite))
        return out

    @staticmethod
    def relative(rows: list[ScoreRow], step: str) -> list[Fraction]:
        return [Fraction(abs(r.steps[step].total - r.kite_total), r.kite_total) for r in rows
                if step in r.steps and r.kite_total > 0]

    def inversions(self) -> list[Inversion]:
        out = []
        for group, rows in self.groups().items():
            for i, hi in enumerate(STEPS):
                for lo in STEPS[i + 1:]:
                    both = [r for r in rows if hi in r.steps and lo in r.steps]
                    if not both:
                        continue
                    m_hi = quantile(self.errors(both, hi), MEDIAN)
                    m_lo = quantile(self.errors(both, lo), MEDIAN)
                    if m_lo < m_hi:
                        out.append(Inversion(group, lo, hi, m_lo, m_hi, len(both)))
        return out


def _kite_paise(x: Decimal | None) -> int | None:
    return None if x is None else int((x * 100).to_integral_value(rounding=ROUND_HALF_EVEN))


def _up_to_rupee(x: Decimal) -> int:
    """Rupees -> paise, rounded up to the whole rupee (the staircase's rounding of each part)."""
    return int(x.to_integral_value(rounding=ROUND_CEILING)) * numerics.PAISE_PER_RUPEE


def _row_in_force(margin: dict, snap, adopted: bool) -> dict | None:  # noqa: ANN001
    """The SPAN-lite row in force for the snapshot among the placeholder rows (adopted=False) or the adopted ones."""
    from stockify.fno import calibrate
    rows = [r for r in margin.get("spanlite", []) if (r.get("calibration_id") != "placeholder") == adopted]
    try:
        return calibrate.class_row({"spanlite": rows}, snap)
    except calibrate.CalibrationError:
        return None


def _spanlite_value(pricer, portfolio, row: dict) -> StepValue:  # noqa: ANN001
    total, parts = pricer.t1(portfolio, row)
    return StepValue(total, sum(max(b.scan, b.somc) for b in parts), sum(b.elm + b.addons for b in parts))


def scorecard() -> Scorecard:
    """Every stored Kite answer priced by every step (see the module docstring)."""
    from stockify import store, timeutil
    from stockify.fno import calibrate
    from stockify.fno.span_exact import SpanCalcError, SpanMissingError, span_paise
    with sv.read_only():
        stored = store.margin_calibrations()
        live = store.margin_live_rows()
    live_errors = sum(1 for r in live if r.get("error"))
    live_answers = sum(1 for r in live if not r.get("error") and r.get("kite_total_paise") is not None)
    notes: list[str] = []
    by_day: dict[date, set[str]] = {}
    for r in stored:
        by_day.setdefault(timeutil.ns_to_ist(r["wall_ns"]).date(), set()).add(r["underlying"])
    samples: dict[int, sv.Sample] = {}
    for day, unds in sorted(by_day.items()):
        for s in sv.validate(day, sorted(unds)).samples:
            samples[s.sample_id] = s
    margin, pricing = calibrate._config_params()
    if not any(r.get("calibration_id") != "placeholder" for r in margin.get("spanlite", [])):
        pending = len(margin.get("spanlite_pending", []) or [])
        notes.append(f"B3 ({STEP_WORDS[STEP_B3]}) is not available: no calibrated SPAN-lite row is adopted in "
                     f"margin_params.json ({pending} fitted row{'s' if pending != 1 else ''} wait for review in "
                     "spanlite_pending).")
    runs: dict[tuple[str, int], list[dict]] = {}
    for r in stored:
        runs.setdefault((r["underlying"], r["wall_ns"]), []).append(r)
    out: list[ScoreRow] = []
    with sv.read_only():
        for (und, wall_ns), run in sorted(runs.items()):
            day = timeutil.ns_to_ist(wall_ns).date()
            run = sorted(run, key=lambda r: r["sample_id"])
            try:
                snap, cal_samples, _how = calibrate.snapshot_from_rows(und, day.isoformat(), run)
            except calibrate.CalibrationError as exc:
                notes.append(f"{und} run at {timeutil.ns_to_ist(wall_ns):%Y-%m-%d %H:%M} IST skipped: {exc}")
                continue
            pricer = calibrate.Pricer(snap, margin, pricing)
            placeholder, adopted = _row_in_force(margin, snap, False), _row_in_force(margin, snap, True)
            ltp = lambda iid: (snap.quotes.get(iid) or {}).get("ltp")  # noqa: E731 — B1's prices, for B2 too
            for r, cal in zip(run, cal_samples):
                s = samples.get(r["sample_id"])
                if s is None or s.kite_span is None or s.kite_exposure is None:
                    notes.append(f"sample {r['sample_id']}: Kite's stored answer lacks span or exposure; left out")
                    continue
                steps: dict[str, StepValue] = {}
                if s.result is not None:
                    try:
                        span = span_paise(s.result)
                    except (SpanMissingError, SpanCalcError):
                        span = None
                    if span is not None:
                        if s.our_exposure is not None:
                            exp = _up_to_rupee(s.our_exposure)
                            steps[STEP_B1] = StepValue(span + exp, span, exp)
                        legs = [(snap.rows[i], u) for i, u in cal.portfolio.legs]
                        pricer.span.rows = margin.get("spanlite", [])        # the rule row in force, as B2 reads it
                        try:
                            elm, addon = pricer.span.rule_elm(legs, ltp, day.isoformat(), ltp(snap.index_id))
                            steps[STEP_B2] = StepValue(span + elm + addon, span, elm + addon)
                        except RuntimeError:
                            pass
                for step, row in ((STEP_B3, adopted), (STEP_B4, placeholder)):
                    if row is not None:
                        steps[step] = _spanlite_value(pricer, cal.portfolio, row)
                steps[STEP_B5] = StepValue(pricer.t0(cal.portfolio), None, None)
                expiring = any((snap.rows[i].get("actual_expiry") or snap.rows[i].get("expiry")) == day.isoformat()
                               for i, _ in cal.portfolio.legs)
                out.append(ScoreRow(r["sample_id"], und, s.kind, day,
                                    calibrate.kite_target(json.loads(r["kite_json"])), _kite_paise(s.kite_span),
                                    _kite_paise(s.kite_exposure), steps, expiring))
    return Scorecard(sorted(out, key=lambda r: r.sample_id), notes, live_answers, live_errors)


def _money(x: Fraction | None) -> str:
    """Paise (exact) rounded half up to the paisa, in rupees."""
    if x is None:
        return "—"
    return _rupees(int((Decimal(x.numerator) / Decimal(x.denominator)).quantize(Decimal(1), rounding=ROUND_HALF_UP)))


def _pct(x: Fraction | None) -> str:
    if x is None:
        return "—"
    return f"{(Decimal(x.numerator) * 100 / Decimal(x.denominator)).quantize(Decimal('0.01'), ROUND_HALF_UP)} %"


def _short(step: str) -> str:
    return step.split("_", 1)[0]


def scorecard_text(card: Scorecard) -> str:
    from stockify.fno.span_validate import WORDS
    rows = card.rows
    days = sorted({r.day for r in rows})
    lines = ["# The margin staircase against Kite", "",
             f"{len(rows)} stored Kite basket-margin answers"
             + (f" ({', '.join(d.isoformat() for d in days)})" if days else "")
             + ", each priced offline by every step, not only the one the staircase would pick:", ""]
    lines += [f"- {s}: {STEP_WORDS[s]}" for s in STEPS]
    lines += ["", "Error = |ours − Kite's| for the margin Kite charges without the premium of bought options "
              "(span + exposure + additional). Median and 90th percentile (p90): linear interpolation between the "
              "two nearest ranks at rank (n − 1)·q of the sorted errors, exact over paise, shown to the paisa. "
              "Relative: the median of |ours − Kite's| / Kite's, for answers where Kite's margin is above zero.", ""]
    for note in card.notes:
        lines.append(f"Note: {note}")
    if card.notes:
        lines.append("")
    if card.live_answers or card.live_errors:
        lines += [f"Live Kite answers: {card.live_answers} logged, {card.live_errors} error"
                  f"{'s' if card.live_errors != 1 else ''} (the paper daemon's poller; `margin live-report` groups "
                  "their gaps)", ""]
    # Overall headline per step
    lines += ["## All answers", "",
              "| Step | Priced | Median error | p90 | Median relative | p90 relative | SPAN max |",
              "|---|---|---|---|---|---|---|"]
    best = None
    for step in STEPS:
        errs = card.errors(rows, step)
        if not errs:
            lines.append(f"| {step} | 0 | — | — | — | — | — |")
            continue
        med = quantile(errs, MEDIAN)
        rel, se = card.relative(rows, step), card.errors(rows, step, "span")
        lines.append(f"| {step} | {len(errs)} | {_money(med)} | {_money(quantile(errs, P90))} | "
                     f"{_pct(quantile(rel, MEDIAN))} | {_pct(quantile(rel, P90))} | "
                     f"{_money(max(se)) if se else '—'} |")
        if best is None or med < best[1]:
            best = (step, med, len(errs))
    if best:
        lines += ["", f"Closest step overall: {best[0]} ({STEP_WORDS[best[0]]}), median error {_money(best[1])} on "
                  f"{best[2]} answers."]
    for (und, kind), group in card.groups().items():
        lines += ["", f"## {und} {WORDS.get(kind, kind)} ({len(group)} answers)", "",
                  "| Step | Priced | Total median | Total p90 | Relative median | SPAN median | SPAN p90 | SPAN max | "
                  "Exposure median | Exposure p90 |", "|---|---|---|---|---|---|---|---|---|---|"]
        for step in STEPS:
            errs = card.errors(group, step)
            if not errs:
                continue
            se, ee = card.errors(group, step, "span"), card.errors(group, step, "exposure")
            lines.append(f"| {step} | {len(errs)} | {_money(quantile(errs, MEDIAN))} | {_money(quantile(errs, P90))} | "
                         f"{_pct(quantile(card.relative(group, step), MEDIAN))} | {_money(quantile(se, MEDIAN))} | "
                         f"{_money(quantile(se, P90))} | {_money(max(se)) if se else '—'} | "
                         f"{_money(quantile(ee, MEDIAN))} | "
                         f"{_money(quantile(ee, P90))} |")
    inv = card.inversions()
    lines += ["", "## Inversions", ""]
    if not inv:
        lines.append("None: on every portfolio type, each more exact step is at least as close to Kite as every less "
                     "exact one (median total error, on the answers both priced).")
    for x in inv:
        und, kind = x.group
        lines.append(f"- Inversion: on {und} {WORDS.get(kind, kind)}, {_short(x.better)} ({STEP_WORDS[x.better]}) "
                     f"beats {_short(x.worse)} ({STEP_WORDS[x.worse]}): median error {_money(x.better_median)} "
                     f"against {_money(x.worse_median)}, on {x.n} answers.")
    lines += ["", "## Reading this", "",
              "- SPAN and exposure are compared separately as well as in total. The exact SPAN (B1, B2) depends only "
              "on the file in force and the legs, not on prices: its error is the model's plus the rounding of "
              "each part up to the whole rupee (see SPAN max for the largest).",
              "- Exposure follows Kite's convention (the ELM % on the future's price for futures, on the index for "
              "sold options), but the stored snapshot prices drift by up to about 0.08 % from the live price Kite "
              "used when each answer was asked. Part of every exposure error, and so of the total, is that price "
              "drift, not model error.",
              "- Prices differ by step: B1 and B2 use each snapshot's last traded price (the convention span-validate "
              "proved against Kite), B4 and B5 the calibration's mark (mid of bid and ask, else the last price). "
              "Part of the gap between B1's and B4's exposure columns is that price basis, not the model.",
              "- B5 has no SPAN / exposure split: only its total is compared."]
    both = [r for r in rows if STEP_B1 in r.steps and STEP_B2 in r.steps]
    if both:
        same = sum(r.steps[STEP_B1] == r.steps[STEP_B2] for r in both)
        expiring = [r for r in both if r.expiry_day]
        lines.append(f"- B1 and B2 (NSE's ELM file against the dated rule) give the same margin, after rounding each "
                     f"part up to the rupee, on {same} of the {len(both)} answers both priced.")
        if not expiring:
            lines.append("- No stored Kite answer had a contract expiring that day, so the expiry-day add-on was not "
                         "tested against NSE.")
        else:
            agree = sum(r.steps[STEP_B1] == r.steps[STEP_B2] for r in expiring)
            lines.append(f"- {len(expiring)} answer{'s' if len(expiring) != 1 else ''} had a contract expiring that "
                         f"day (the dated rule adds the expiry-day add-on); B1 and B2 agree on {agree} of them.")
    return "\n".join(lines) + "\n"

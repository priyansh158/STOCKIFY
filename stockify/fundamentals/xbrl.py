"""FS 3: figures out of NSE's results XBRL, checked before use; a filing that fails is held with a plain reason.

`parse(data, period_kind=, basis=)` reads one filing with the standard library (xml.etree; a DOCTYPE is refused) and
returns its FIELDS in paise (EPS and face value in paise per share). `check(fields, period_kind=)` returns
plain-English problems. `ingest_pending()` parses every `downloaded` filing in the store: clean → its figures and
status `parsed`; any problem → status `held` with the problems as the reason. Never a guessed number.

What the real files look like (fixtures in tests/fixtures/fundamentals, fetched 2026-11-17; README there):

- Formats (`taxonomy.json` families). Legacy results (to the quarter ended 31-Dec-2024) name their format in the
  schemaRef: `Ind-AS_entry_point_2020-03-31.xsd` (indas), `banking_entry_point_2019-09-30.xsd` (banking),
  `in-bse-fin-2020-03-31.xsd` (nbfc). Integrated filings (2025 on) all use SEBI's `in-capmkt-ent-2026-01-31.xsd`
  and are told apart by the generator's comment opening the file (`<!--IFIndAs V2.1 ...-->`, `IFBanking`,
  `IFNBFC`), or by the file name's prefix on nsearchives (`hint`). The element names are the same in both.
- Contexts. NSE's own ids: `OneD` = the quarter, `FourD` = the financial year to date, `OneI` = the balance sheet
  at the period end; integrated files add `PY_I` (the previous year end, a comparative). Legacy files write the
  quarter's dates on `FourD` too, and 2021 files reference OneD / FourD / OneI without defining them, so a duration
  context's real period comes from the DateOfStartOfReportingPeriod / DateOfEndOfReportingPeriod facts inside it
  (falling back to its own dates; an undefined `OneD` with no start date is the quarter). Contexts with dimensions
  (segments, expense details) are never used. Only contexts whose real period is the filing's own are read:
  the quarter (Q) or the 6 / 12 months to the period end (H / A) for the profit and loss, the period-end instant
  for the balance sheet, the 6 / 12 months year to date for the cash flow. Comparative columns never match.
- Amounts are filed in whole rupees whatever the declared rounding (Reliance Q3 FY25 revenue, ₹2,43,865 crore, is
  `2438650000000.00` with LevelOfRounding "Crores"; VST's ₹47,055 lakh is `4705500000.00` with "Lakhs"). XBRL
  `decimals` is precision, never scale; integrated files even vary it per fact by trailing zeros. So paise =
  Decimal(text) × 100, exactly (a fraction of a paisa is a problem, not rounded); unit must be INR (INR/shares for
  per-share). The declared rounding sets only the checks' slack. A scale slip would break EPS × shares ≈ profit.
- Quirks: balance-sheet lines filed as zeros in a quarter without a balance sheet (NBFC December) mean "not filed";
  SBI files `Assets` without a unit (the equal CapitalAndLiabilities is used); the cash-flow statement is filed only
  year to date (6 months in September, 12 in March), so `operating_cash_flow` is stored only for an H or A filing
  (the Annual listing shares the March file); a quarterly row never carries it; a P&L filed as all zeros is held; a
  standalone filing has no minority interests, so profit_owners = net_profit there.

Element map (local names; first filed wins, `+` = sum of parts):

| field | indas | banking | nbfc |
|---|---|---|---|
| revenue_ops | RevenueFromOperations | — | RevenueFromOperations |
| interest_earned | — | InterestEarned | InterestEarned |
| interest_expended | — | InterestExpended | FinanceCosts |
| other_income / total_income | OtherIncome / Income | same | same |
| total_expenses | Expenses | ExpenditureExcludingProvisionsAndContingencies | Expenses |
| finance_costs / depreciation | FinanceCosts / DepreciationDepletionAndAmortisationExpense | — | same as indas |
| profit_before_tax | ProfitBeforeTax | ProfitLossFromOrdinaryActivitiesBeforeTax | ProfitBeforeTax |
| tax | TaxExpense | TaxExpense | TaxExpense |
| net_profit | ProfitLossForPeriod | ProfitLossForThePeriod | ProfitLossForPeriod |
| profit_owners | ProfitOrLossAttributableToOwnersOfParent | ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates | as indas |
| eps_basic / eps_diluted | Basic/DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations | Basic/DilutedEarningsPerShareAfterExtraordinaryItems | as indas |
| paid_up_equity / face_value | PaidUpValueOfEquityShareCapital / FaceValueOfEquityShareCapital | same | same |
| total_equity | EquityAttributableToOwnersOfParent, Equity | Capital + ReservesAndSurplus | as indas |
| borrowings_current / _noncurrent | BorrowingsCurrent / BorrowingsNoncurrent | — | — |
| total_assets | Assets | Assets, CapitalAndLiabilities | Assets |
| cash | CashAndCashEquivalents | CashAndBalancesWithReserveBankOfIndia + BalancesWithBanksAndMoneyAtCallAndShortNotice | CashAndCashEquivalents |
| operating_cash_flow | CashFlowsFromUsedInOperatingActivities (H / A filings only) | same | same |

Sources: RELIANCE 2024-12-31 consolidated + standalone, 2024-09-30 and 2021-06-30 consolidated, 2026-09-30
integrated; INFY 2024-03-31; VSTIND 2024-12-31 (lakhs); HDFCBANK 2024-12-31, 2021-06-30 and 2026-09-30 integrated;
SBIN 2024-09-30; BAJFINANCE 2024-12-31, 2024-09-30 and 2026-09-30 integrated.
"""
from __future__ import annotations

import calendar
import functools
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

from stockify import dbconn, paths
from stockify.fundamentals import store

FIELDS = ("revenue_ops", "other_income", "total_income", "total_expenses", "finance_costs", "depreciation",
          "profit_before_tax", "tax", "net_profit", "profit_owners", "eps_basic", "eps_diluted", "paid_up_equity",
          "face_value", "total_equity", "borrowings_current", "borrowings_noncurrent", "total_assets", "cash",
          "operating_cash_flow", "interest_earned", "interest_expended")

LABELS = {
    "revenue_ops": "revenue from operations", "other_income": "other income", "total_income": "total income",
    "total_expenses": "total expenses", "finance_costs": "finance costs", "depreciation": "depreciation",
    "profit_before_tax": "profit before tax", "tax": "tax", "net_profit": "net profit",
    "profit_owners": "profit attributable to owners", "eps_basic": "basic EPS", "eps_diluted": "diluted EPS",
    "paid_up_equity": "paid-up equity", "face_value": "face value", "total_equity": "total equity",
    "borrowings_current": "current borrowings", "borrowings_noncurrent": "non-current borrowings",
    "total_assets": "total assets", "cash": "cash and equivalents", "operating_cash_flow": "operating cash flow",
    "interest_earned": "interest earned", "interest_expended": "interest expended",
    "profit_after_tax": "profit after tax", "regulatory_movement": "regulatory deferral movement",
}

KINDS = {"Q": 3, "H": 6, "A": 12}
_KIND_WORDS = {"Q": "quarter", "H": "half year", "A": "year"}
_BALANCE_SHEET = ("total_equity", "borrowings_current", "borrowings_noncurrent", "total_assets", "cash")
_PL = ("revenue_ops", "interest_earned", "other_income", "total_income", "total_expenses", "profit_before_tax",
       "tax", "net_profit")
_YTD_CORE = ("revenue_ops", "interest_earned", "profit_before_tax", "net_profit")
_MARKER = re.compile(rb"<!--\s*(IF[A-Za-z]+)\s+V?([0-9.]*)")


@functools.lru_cache(maxsize=1)
def taxonomy() -> dict:
    """The element map (`taxonomy.json` beside this module)."""
    return json.loads(Path(__file__).with_name("taxonomy.json").read_text(encoding="utf-8"))


@dataclass
class ParseResult:
    fields: dict[str, int]
    taxonomy: str                                   # family name ("indas", "banking", "nbfc"), "" when unknown
    problems: list[str]
    raw: dict[str, str] = field(default_factory=dict)       # field → "Element text" as filed
    format: str = ""                                # schemaRef file (and integrated marker)
    period_end: str | None = None
    fy_start: str | None = None
    ytd: dict[str, int] = field(default_factory=dict)       # year-to-date core figures (quarters add-up check)
    ytd_months: int | None = None
    step_paise: int = 1                             # one rounding step of the filing


class _Bad(Exception):
    """A fact that cannot be used, with the plain reason."""


def to_paise(text: str) -> int:
    """A decimal rupee amount as filed → paise, exactly. ValueError when it is not a number or not whole paise."""
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        raise ValueError(f"not a number ({text.strip()[:40]!r})") from None
    if not value.is_finite():
        raise ValueError(f"not a number ({text.strip()[:40]!r})")
    paise = value * 100
    if paise != paise.to_integral_value():
        raise ValueError(f"has a fraction of a paisa ({text.strip()[:40]!r})")
    return int(paise)


def _crore(paise: int | Fraction) -> str:
    value = (Decimal(paise.numerator) / Decimal(paise.denominator) if isinstance(paise, Fraction)
             else Decimal(paise)) / Decimal(10**9)
    text = f"{abs(value.quantize(Decimal('0.01'))):f}"
    whole, frac = text.split(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    whole = ",".join(([head] if head else []) + groups + [tail]) if head or groups else tail
    return f"{'-' if value < 0 else ''}₹{whole}.{frac} crore"            # Indian grouping, as on the page


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _iso(text: str | None) -> date | None:
    try:
        return date.fromisoformat((text or "").strip()[:10])
    except ValueError:
        return None


def _months(start: date, end: date) -> int | None:
    """Whole calendar months from `start` (a 1st) to `end` (a month end), else None."""
    if start.day != 1 or end.day != calendar.monthrange(end.year, end.month)[1] or end < start:
        return None
    return (end.year - start.year) * 12 + end.month - start.month + 1


def _quarter_start(end: date) -> date:
    m = end.month - 2
    y = end.year + (m - 1) // 12
    return date(y, (m - 1) % 12 + 1, 1)


def _month_end(year: int, month: int) -> date:
    y = year + (month - 1) // 12
    m = (month - 1) % 12 + 1
    return date(y, m, calendar.monthrange(y, m)[1])


# ── the instance document ────────────────────────────────────────────────────

@dataclass
class _Ctx:
    id: str
    defined: bool
    dims: bool = False
    start: date | None = None
    end: date | None = None
    instant: date | None = None


@dataclass
class _Fact:
    name: str
    ctx: str
    unit: str | None
    decimals: str | None
    text: str


class _Doc:
    def __init__(self, root: ET.Element):
        self.contexts: dict[str, _Ctx] = {}
        self.units: dict[str, str] = {}
        self.facts: dict[tuple[str, str], list[_Fact]] = {}
        self.schema_refs: list[str] = []
        for el in root:
            name = _local(el.tag)
            if name == "context":
                self._context(el)
            elif name == "unit":
                self._unit(el)
            elif name == "schemaRef":
                href = next((v for k, v in el.attrib.items() if _local(k) == "href"), "")
                self.schema_refs.append(href.replace("\\", "/").rsplit("/", 1)[-1])
            elif el.get("contextRef") is not None:
                if any(_local(k) == "nil" and v.strip() == "true" for k, v in el.attrib.items()):
                    continue
                text = (el.text or "").strip()
                if not text:
                    continue
                fact = _Fact(name, el.get("contextRef"), el.get("unitRef"), el.get("decimals"), text)
                self.facts.setdefault((name, fact.ctx), []).append(fact)
        for _, ctx in list(self.facts):
            if ctx not in self.contexts:
                self.contexts[ctx] = _Ctx(ctx, defined=False)

    def _context(self, el: ET.Element) -> None:
        c = _Ctx(el.get("id") or "", defined=True)
        for sub in el.iter():
            n = _local(sub.tag)
            if n in ("segment", "scenario", "explicitMember", "typedMember"):
                c.dims = True
            elif n == "startDate":
                c.start = _iso(sub.text)
            elif n == "endDate":
                c.end = _iso(sub.text)
            elif n == "instant":
                c.instant = _iso(sub.text)
        self.contexts[c.id] = c

    def _unit(self, el: ET.Element) -> None:
        def measures(node):
            return [(m.text or "").strip().rsplit(":", 1)[-1] for m in node.iter() if _local(m.tag) == "measure"]
        num = next((n for n in el.iter() if _local(n.tag) == "unitNumerator"), None)
        den = next((n for n in el.iter() if _local(n.tag) == "unitDenominator"), None)
        if num is not None and den is not None:
            canon = "*".join(measures(num)) + "/" + "*".join(measures(den))
        else:
            canon = "*".join(measures(el))
        self.units[el.get("id") or ""] = canon

    def text(self, name: str, ctx: str) -> str | None:
        facts = self.facts.get((name, ctx))
        return facts[0].text if facts else None

    def first_text(self, names: tuple[str, ...], ctxs) -> str | None:
        for name in names:
            for c in ctxs:
                t = self.text(name, c)
                if t:
                    return t
        return None


# ── parse ────────────────────────────────────────────────────────────────────

def _family(doc: _Doc, data: bytes, hint: str | None) -> tuple[str, str]:
    marker = _MARKER.search(data[:2048])
    mark = marker.group(1).decode("ascii") if marker else None
    refs = doc.schema_refs
    for fam, spec in taxonomy()["families"].items():
        for m in spec["match"]:
            if m["schema_ref"] not in refs:
                continue
            if "marker" not in m:
                return fam, m["schema_ref"]
            if mark == m["marker"] or (mark is None and hint and hint.upper().startswith(m["url_prefix"])):
                return fam, f"{m['schema_ref']} ({mark or m['url_prefix']})"
    return "", (", ".join(refs) + (f" ({mark})" if mark else "")) or "no schemaRef"


def parse(data: bytes, *, period_kind: str, basis: str, period_end: str | None = None,
          hint: str | None = None) -> ParseResult:
    """One filing's FIELDS for its own period. `period_kind` Q|H|A, `basis` consolidated|standalone (as listed);
    `period_end` (ISO) when known from the listing; `hint` = the XBRL file name, used only for integrated filings
    that lack the generator's comment."""
    if period_kind not in KINDS:
        raise ValueError(f"period_kind must be Q, H or A, not {period_kind!r}")
    if basis not in ("consolidated", "standalone"):
        raise ValueError(f"basis must be consolidated or standalone, not {basis!r}")
    if b"<!DOCTYPE" in data[:4096] or b"<!ENTITY" in data[:4096]:
        return ParseResult({}, "", ["the XBRL file declares a DOCTYPE, which NSE filings never do"])
    if not data.strip():
        return ParseResult({}, "", ["the XBRL file is empty"])
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        return ParseResult({}, "", [f"the XBRL file is not readable XML ({e})"])
    if _local(root.tag) != "xbrl":
        return ParseResult({}, "", [f"the file is not an XBRL instance (its root is <{_local(root.tag)}>)"])
    doc = _Doc(root)
    family, fmt = _family(doc, data, hint)
    if not family:
        return ParseResult({}, "", [f"the filing is in a format this parser has no map for ({fmt})"], format=fmt)
    return _Parser(doc, family, fmt, period_kind, basis, period_end).run()


class _Parser:
    def __init__(self, doc: _Doc, family: str, fmt: str, kind: str, basis: str, period_end: str | None):
        self.doc, self.family, self.kind, self.basis = doc, family, kind, basis
        self.spec = taxonomy()["families"][family]
        self.res = ParseResult({}, family, [], format=fmt)
        self.want_end = _iso(period_end) if period_end else None
        nse = taxonomy()["nse_contexts"]
        self.cur_d, self.cur_i = nse["current_duration"], nse["current_instant"]

    def problem(self, text: str) -> None:
        if text not in self.res.problems:
            self.res.problems.append(text)

    # periods
    def durations(self) -> dict[str, tuple[date | None, date | None]]:
        out = {}
        for c in self.doc.contexts.values():
            if c.dims or c.instant is not None:
                continue
            if not c.defined and not c.id.endswith("D"):
                continue
            start = _iso(self.doc.text("DateOfStartOfReportingPeriod", c.id)) or c.start
            end = _iso(self.doc.text("DateOfEndOfReportingPeriod", c.id)) or c.end
            if start is None and end is not None and c.id == self.cur_d:
                start = _quarter_start(end)                  # NSE's current column is the quarter
            out[c.id] = (start, end)
        return out

    def run(self) -> ParseResult:
        doc, res = self.doc, self.res
        spans = self.durations()
        end = self.want_end or (spans.get(self.cur_d, (None, None))[1])
        if end is None:
            ends = sorted({e for _, e in spans.values() if e})
            end = ends[-1] if ends else None
        if end is None:
            self.problem("the filing does not say which period it covers")
            return res
        res.period_end = end.isoformat()
        months = KINDS[self.kind]
        flow = [c for c, (s, e) in spans.items() if e == end and s and _months(s, e) == months]
        if not flow:
            covered = sorted({f"{s} to {e}" for s, e in spans.values() if s and e})
            self.problem(f"no {_KIND_WORDS[self.kind]} ending {end} in the filing"
                         + (f" (it covers {', '.join(covered)})" if covered else ""))
            return res
        plain_d = list(spans)
        fy_start = _iso(doc.first_text(("DateOfStartOfFinancialYear",), plain_d))
        res.fy_start = fy_start.isoformat() if fy_start else None
        ytd = [c for c, (s, e) in spans.items() if fy_start and e == end and s == fy_start]
        res.ytd_months = _months(fy_start, end) if ytd else None
        instants = [c.id for c in doc.contexts.values()
                    if not c.dims and ((c.defined and c.instant == end) or (not c.defined and c.id == self.cur_i))]

        nature = doc.first_text(("NatureOfReportStandaloneConsolidated",), flow)
        if nature and nature.strip().lower() != self.basis:
            self.problem(f"the filing says it is {nature.strip().lower()}, but the listing says {self.basis}")
        level = doc.first_text(("LevelOfRoundingUsedInFinancialStatements", "LevelOfRounding"), plain_d)
        steps = {k: v for k, v in taxonomy()["rounding_levels"].items() if k != "note"}
        if level and level.strip() in steps:
            res.step_paise = steps[level.strip()]
        else:
            decs = [int(f.decimals) for fs in doc.facts.values() for f in fs
                    if f.ctx in flow and f.decimals and re.fullmatch(r"-?\d+", f.decimals) and f.unit == "INR"]
            res.step_paise = 10 ** max(0, 2 - min(decs)) if decs else 1

        ctx_for = {"flow": flow, "instant": instants,
                   # The cash flow is filed only year to date: kept only when that is the filing's own period
                   # (H: 6 months, A: 12), never on a quarter's row, where it would read as a quarter figure.
                   "ytd": ytd if self.kind in ("H", "A") and res.ytd_months == months else []}
        for name, spec in self.spec["fields"].items():
            got = self.value(name, spec, ctx_for[spec["period"]])
            if got is not None:
                res.fields[name], res.raw[name] = got
        if not res.fields.get("total_assets"):                # zeros = no balance sheet in this filing
            for name in _BALANCE_SHEET:
                res.fields.pop(name, None)
                res.raw.pop(name, None)
        if res.fields.get("operating_cash_flow") == 0:
            del res.fields["operating_cash_flow"], res.raw["operating_cash_flow"]
        if self.basis == "standalone" and "profit_owners" not in res.fields and "net_profit" in res.fields:
            res.fields["profit_owners"] = res.fields["net_profit"]
            res.raw["profit_owners"] = "= net_profit (standalone)"
        aux = {"step_paise": res.step_paise}
        for name, spec in self.spec.get("check_inputs", {}).items():
            got = self.value(name, spec, ctx_for[spec["period"]])
            if got is not None:
                aux[name] = got[0]
        if ytd:
            for name in _YTD_CORE:
                spec = self.spec["fields"].get(name)
                if spec:
                    got = self.value(name, spec, ytd, quiet=True)
                    if got is not None:
                        res.ytd[name] = got[0]
        pl = [res.fields[k] for k in _PL if k in res.fields]
        if pl and not any(pl):
            self.problem("all profit and loss figures are zero")
        for p in check(res.fields, period_kind=self.kind, aux=aux):
            self.problem(p)
        return res

    # values
    def value(self, name: str, spec: dict, ctxs: list[str], *, quiet: bool = False) -> tuple[int, str] | None:
        if not ctxs:
            return None
        per_share = spec.get("kind") == "per_share"
        if "sum" in spec:
            parts = [self.one(n, ctxs, per_share, name, quiet) for n in spec["sum"]]
            if any(p is None for p in parts):
                return None
            return sum(p[0] for p in parts), " + ".join(p[1] for p in parts)
        unitless = False
        for element in spec["names"]:
            try:
                got = self.one(element, ctxs, per_share, name, quiet, raise_unitless=True)
            except _Bad:
                unitless = True
                continue
            if got is not None:
                return got
        if unitless and not quiet:
            self.problem(f"{LABELS.get(name, name)} is filed without a unit")
        return None

    def one(self, element: str, ctxs: list[str], per_share: bool, name: str, quiet: bool,
            raise_unitless: bool = False) -> tuple[int, str] | None:
        facts = [f for c in ctxs for f in self.doc.facts.get((element, c), [])]
        if not facts:
            return None
        label = LABELS.get(name, name)
        want = "INR/shares" if per_share else "INR"
        values: dict[int, str] = {}
        for f in facts:
            if f.unit is None:
                if raise_unitless:
                    raise _Bad(label)
                if not quiet:
                    self.problem(f"{label} is filed without a unit")
                return None
            unit = self.doc.units.get(f.unit)
            if unit != want:
                if not quiet:
                    self.problem(f"{label} is in {unit}, not {'rupees per share' if per_share else 'rupees'}"
                                 if unit else f"{label} refers to a unit the filing does not declare ({f.unit})")
                return None
            try:
                values.setdefault(to_paise(f.text), f.text)
            except ValueError as e:
                if not quiet:
                    self.problem(f"{label} {e}")
                return None
        if len(values) > 1:
            if not quiet:
                shown = " and ".join(sorted(values.values()))
                self.problem(f"{label} has different values for the same period ({shown})")
            return None
        paise, text = next(iter(values.items()))
        return paise, f"{element} {text}"


# ── checks ───────────────────────────────────────────────────────────────────

def _cfg(key: str) -> Fraction:
    return Fraction(Decimal(taxonomy()["checks"][key]))


def _off(a, b, pct: Fraction, slack) -> bool:
    return abs(Fraction(a) - Fraction(b)) > max(pct * max(abs(Fraction(a)), abs(Fraction(b))), Fraction(slack))


def check(fields: dict[str, int], *, period_kind: str, aux: dict | None = None) -> list[str]:
    """Plain-English problems with one filing's fields (empty = clean). `aux` may carry the check-only inputs parse
    reads (profit_after_tax, regulatory_movement) and `step_paise`, one rounding step of the filing."""
    aux = aux or {}
    step = aux.get("step_paise", 10**9)
    f = fields
    out = []
    if "revenue_ops" not in f and "interest_earned" not in f:
        out.append("no revenue from operations (or interest earned) for the filing's own period")
    for name in ("profit_before_tax", "net_profit"):
        if name not in f:
            out.append(f"no {LABELS[name]} for the filing's own period")
    if "profit_before_tax" in f and "tax" in f and ("profit_after_tax" in aux or "net_profit" in f):
        pat = aux.get("profit_after_tax", f.get("net_profit"))
        expected = f["profit_before_tax"] - f["tax"] + aux.get("regulatory_movement", 0)
        if _off(pat, expected, _cfg("profit_pct"), 3 * step):
            out.append(f"profit after tax ({_crore(pat)}) differs from profit before tax minus tax "
                       f"({_crore(expected)}) by more than {_pct('profit_pct')} %")
    base = f.get("revenue_ops", f.get("interest_earned"))
    if "total_income" in f and base is not None and "other_income" in f:
        expected = base + f["other_income"]
        if _off(f["total_income"], expected, _cfg("income_pct"), 3 * step):
            out.append(f"total income ({_crore(f['total_income'])}) differs from revenue plus other income "
                       f"({_crore(expected)}) by more than {_pct('income_pct')} %")
    profit = f.get("profit_owners", f.get("net_profit"))
    if all(k in f for k in ("eps_basic", "paid_up_equity", "face_value")) and profit is not None:
        if f["face_value"] <= 0 or f["paid_up_equity"] <= 0:
            out.append("paid-up equity or face value is not positive, so the share count is unknown")
        else:
            shares = Fraction(f["paid_up_equity"], f["face_value"])
            implied = f["eps_basic"] * shares
            if _off(implied, profit, _cfg("eps_pct"), shares / 2 + step):
                out.append(f"EPS times shares ({_crore(implied)}) differs from profit attributable to owners "
                           f"({_crore(profit)}) by more than {_pct('eps_pct')} %")
    if period_kind in ("H", "A") and not ("total_equity" in f and "total_assets" in f):
        word = "half-year" if period_kind == "H" else "annual"
        out.append(f"a {word} filing without a balance sheet (total equity and total assets)")
    return out


def _pct(key: str) -> str:
    return f"{(Decimal(taxonomy()['checks'][key]) * 100).normalize():f}"


# ── ingest ───────────────────────────────────────────────────────────────────

def _file_for(row) -> Path:
    base = paths.fundamentals_raw_dir("xbrl").resolve()
    rel = row["xbrl_file"]
    if not rel:
        raise _Bad("the XBRL file is missing (no file recorded for the filing)")
    path = Path(rel)
    path = (path if path.is_absolute() else base / path).resolve()
    if not path.is_relative_to(base):
        raise _Bad("the recorded XBRL file is outside the raw XBRL folder")
    if not path.is_file():
        raise _Bad(f"the XBRL file is missing ({rel})")
    return path


def _earlier_quarters(conn, row, res: ParseResult) -> list[dict] | None:
    """The parsed figures of the earlier quarters of the same financial year (latest version broadcast no later
    than this filing), or None when any of them is not on record."""
    fy = date.fromisoformat(res.fy_start)
    end = date.fromisoformat(res.period_end)
    n = (_months(fy, end) or 0) // 3
    out = []
    for k in range(1, n):
        q_end = _month_end(fy.year, fy.month + 3 * k - 1).isoformat()
        hit = conn.execute(
            "SELECT filing_id FROM filings WHERE symbol = ? AND basis = ? AND period_kind = 'Q' AND status = 'parsed'"
            " AND period_end = ? AND (broadcast_ns IS NULL OR ? IS NULL OR broadcast_ns <= ?)"
            " ORDER BY broadcast_ns DESC, filing_id DESC LIMIT 1",
            (row["symbol"], row["basis"], q_end, row["broadcast_ns"], row["broadcast_ns"])).fetchone()
        if hit is None:
            return None
        out.append({r["field"]: r["value_paise"] for r in conn.execute(
            "SELECT field, value_paise FROM figures WHERE filing_id = ?", (hit["filing_id"],))})
    return out


def _add_up(conn, row, res: ParseResult) -> list[str]:
    if row["period_kind"] != "Q" or not res.fy_start or not res.ytd or (res.ytd_months or 0) <= 3:
        return []
    earlier = _earlier_quarters(conn, row, res)
    if earlier is None:
        return []
    out = []
    terms = len(earlier) + 1
    for name in _YTD_CORE:
        if name not in res.ytd or name not in res.fields or any(name not in q for q in earlier):
            continue
        total = sum(q[name] for q in earlier) + res.fields[name]
        if _off(total, res.ytd[name], _cfg("quarters_pct"), terms * res.step_paise):
            out.append(f"the quarters of the year to {res.period_end} do not add up: {LABELS[name]} "
                       f"{_crore(total)} over the quarters against {_crore(res.ytd[name])} for the year to date")
    return out


def ingest_pending(*, limit: int | None = None) -> dict:
    """Parse every `downloaded` filing (oldest period first, so a year's earlier quarters are on record before its
    later ones are checked against the year to date): clean → figures + `parsed`; problems → `held` + reason."""
    conn = store._connect()
    rows = conn.execute("SELECT * FROM filings WHERE status = 'downloaded'"
                        " ORDER BY period_end, broadcast_ns, filing_id").fetchall()
    if limit is not None:
        rows = rows[:limit]
    counts = {"parsed": 0, "held": 0}
    for row in rows:
        res = None
        try:
            path = _file_for(row)
            data = path.read_bytes()
            if row["xbrl_sha256"] and hashlib.sha256(data).hexdigest() != row["xbrl_sha256"]:
                raise _Bad("the saved XBRL file does not match its recorded sha256")
            res = parse(data, period_kind=row["period_kind"], basis=row["basis"], period_end=row["period_end"],
                        hint=Path(row["xbrl_url"] or path.name).name)
            problems = res.problems or _add_up(conn, row, res)
        except _Bad as e:
            problems = [str(e)]
        except Exception as e:  # noqa: BLE001 — one bad filing is held, never a crash of the run
            problems = [f"the parser failed on this filing ({type(e).__name__}: {e})"]
        _record(conn, row["filing_id"], None if problems else res, "; ".join(problems))
        counts["held" if problems else "parsed"] += 1
    return counts


def _record(conn, filing_id: str, res: ParseResult | None, reason: str) -> None:
    with dbconn.locked_write(store._lock, conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM figures WHERE filing_id = ?", (filing_id,))
        if res is not None:
            conn.executemany("INSERT INTO figures (filing_id, field, value_paise, value_raw) VALUES (?, ?, ?, ?)",
                             [(filing_id, k, res.fields[k], res.raw[k]) for k in FIELDS if k in res.fields])
        conn.execute("UPDATE filings SET status = ?, reason = ? WHERE filing_id = ?",
                     ("held" if res is None else "parsed", reason or None, filing_id))
        conn.execute("COMMIT")

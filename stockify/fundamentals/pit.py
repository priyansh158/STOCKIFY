"""FS 4: point-in-time figures. What the market could know on a day, and nothing it learnt later.

- **Usable day.** A filing counts from `usable_day(broadcast_ns)`: the first NSE (CM) trading day strictly after the
  IST date of NSE's broadcast (spec call 3), holidays and special sessions from `stockify.data.calendar`. A results
  broadcast at 16:00 or at 08:00 is usable the next trading day either way.
- **Versions.** Only `parsed` filings with a broadcast time count. For each (period_end, period_kind) of the
  company's basis the version used on a day is the latest broadcast already usable then; a later filing with
  different figures (a restatement) replaces it from its own usable day, never before. A later filing with the same
  figures is the same version: the first one (its filing_id and date) stays.
- **Basis** (ruling R8, point in time): `basis_on(symbol, day)` is consolidated from the usable day of the
  company's first parsed consolidated filing onward (a held one never switches it), standalone before. Every
  reader takes that one basis for the day, so nothing mixes bases: TTM needs four quarters of it (None until four
  consolidated quarters exist after a switch), and latest / shares read only its filings.
- **What real filings give** (ruling R6, task-3 re-review): quarter rows (Q) carry the quarter's P&L, and the
  September and March ones also carry the balance sheet; there are no usable half-year (H) rows; annual (A) rows
  carry the year's P&L and operating cash flow (legacy to FY2024, and from FY2025 the A row filings.py lists on each
  integrated March file). So `known_on` returns quarters, annual rows and a normally empty `half_years`. A quarter
  NSE never filed is derived as H − the other quarter only when both are on record (guarded; it rarely fires);
  derived quarters carry the H's balance sheet, no EPS, and `derived_from`.
- **TTM** is the sum of the four latest consecutive *filed* quarters: a gap, a quarter not yet usable, or a derived
  quarter (no half-year substitutes, spec call 6) gives None. `latest(symbol, day, field)` is the newest period that
  has a field (the balance sheet: the latest Sep / Mar quarter row).
- **Shares** (spec call 6): paid-up equity ÷ face value from the latest-period usable filing that has both, carried
  forward, and checked on that filing against EPS × shares ≈ profit attributable to owners (taxonomy.json
  checks.eps_pct, 2 %; slack half a share's EPS rounding plus ₹1 crore, the largest rounding step). A filing that
  fails is skipped for an older one. Only when no filing has paid-up data are shares implied by profit ÷ basic EPS.
  NSE's quote (issued size) is not used: the endpoint is blocked (ruling R4).
- **Splits and bonuses between filings.** A filing's count is as at its period end. If NSE's raw close falls from
  one trading day to the next by one of the config ratios (fundamentals.json corporate_action_ratios, within
  corporate_action_tolerance) after that period end and on or before the price day, the count is multiplied by the
  ratio, until a filing for a later period gives the new count. Without this a 1:1 bonus would halve market cap for
  up to a quarter (RELIANCE 2024-10-28 to 2025-01-16). Any other fall of corporate_action_hold_fall (1.3×) or more
  in that window (a 1:2 bonus, a split with a big ex-date move, a demerger, a crash: the price alone cannot tell)
  holds the count: shares and market cap are None until that later filing, never a guess. The fall is seen on the
  ex-date itself: no look-ahead. Each close is compared with the last close before it (so a missing close on or
  before the ex-date does not hide a split); across more than MAX_GAP_DAYS trading days without a close, a fall of
  the hold level or more holds rather than being read as a ratio. The window needs a close in the MAX_GAP_DAYS
  trading days before it to compare with: if those days precede the first close on record in cm_eod, or the stock
  has no close in them, the count is held (a newly listed stock waits for a filing after its listing). Not handled:
  a rise (a consolidation), and share issues (QIP, rights) between filings, which carry the last filed count.
- **Market cap** = cm_eod raw close (series EQ) on the day, else the latest of the 5 trading days before it, × the
  shares known on the day (adjusted up to that close's date); None when either is missing.

Money in int paise, EPS and face value in paise per share, shares in units; days are IST dates.
"""
from __future__ import annotations

import calendar as _monthcal
import functools
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction

from stockify import timeutil
from stockify.config import loader
from stockify.data import calendar as nse_calendar
from stockify.data import refdata
from stockify.fundamentals import store, xbrl

SEGMENT = "CM"
PRICE_LOOKBACK_DAYS = 5                    # ruling (task-4 dispatch): the latest close within 5 trading days
MAX_GAP_DAYS = 5                           # trading days without a close a split comparison may span
QUARTERS_KEPT = 8
ROUNDING_SLACK_PAISE = 10 ** 9             # ₹1 crore: the largest rounding step a filing declares (taxonomy.json)

# Fields of a period's own flow (they subtract: H − Q), and the levels a derived quarter takes from its H.
FLOW = ("revenue_ops", "other_income", "total_income", "total_expenses", "finance_costs", "depreciation",
        "profit_before_tax", "tax", "net_profit", "profit_owners", "interest_earned", "interest_expended")
LEVELS = ("paid_up_equity", "face_value", "total_equity", "borrowings_current", "borrowings_noncurrent",
          "total_assets", "cash")
TTM_FIELDS = FLOW + ("eps_basic", "eps_diluted")
_KIND_ORDER = {"Q": 0, "A": 1, "H": 2}


# ── days ─────────────────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=8192)
def _next_trading_day(d: date) -> date:
    return nse_calendar.next_trading_day(SEGMENT, d)


def usable_day(broadcast_ns: int) -> date:
    """The first trading day strictly after the broadcast's IST date."""
    return _next_trading_day(timeutil.ns_to_ist(int(broadcast_ns)).date())


def shift_quarter(end: date, k: int) -> date:
    """The month end `k` quarters after the month end `end` (k < 0: before)."""
    m = end.month - 1 + 3 * k
    y, m = end.year + m // 12, m % 12 + 1
    return date(y, m, _monthcal.monthrange(y, m)[1])


def _months(start: str | None, end: str) -> int | None:
    if not start:
        return None
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    return (e.year - s.year) * 12 + e.month - s.month + 1


@functools.lru_cache(maxsize=1)
def _cfg() -> tuple[tuple[tuple[str, Fraction], ...], Fraction, Fraction]:
    doc = loader.load_json("fundamentals.json")
    ratios = tuple((r, Fraction(Decimal(r))) for r in doc["corporate_action_ratios"])
    return (ratios, Fraction(Decimal(doc["corporate_action_tolerance"])),
            Fraction(Decimal(doc["corporate_action_hold_fall"])))


# ── versions ─────────────────────────────────────────────────────────────────

def basis_on(symbol: str, day: date) -> str:
    """'consolidated' from the usable day of the company's first parsed consolidated filing, else 'standalone'
    (ruling R8 amended: a held consolidated filing never switches the basis, so it cannot blank the figures)."""
    first = store._connect().execute(
        "SELECT MIN(broadcast_ns) FROM filings WHERE symbol = ? AND basis = 'consolidated' AND status = 'parsed'"
        " AND broadcast_ns IS NOT NULL", (symbol,)).fetchone()[0]
    return "consolidated" if first is not None and usable_day(first) <= day else "standalone"


def _versions(symbol: str, basis: str) -> list[dict]:
    """Every parsed filing of the basis with its figures, oldest broadcast first."""
    out: dict[str, dict] = {}
    for r in store._connect().execute(
            "SELECT f.filing_id, f.period_start, f.period_end, f.period_kind, f.broadcast_ns, g.field, g.value_paise"
            " FROM filings f JOIN figures g ON g.filing_id = f.filing_id"
            " WHERE f.symbol = ? AND f.basis = ? AND f.status = 'parsed' AND f.broadcast_ns IS NOT NULL"
            " ORDER BY f.broadcast_ns, f.filing_id, g.field", (symbol, basis)):
        v = out.get(r["filing_id"])
        if v is None:
            v = out[r["filing_id"]] = {
                "period_end": r["period_end"], "period_kind": r["period_kind"], "period_start": r["period_start"],
                "basis": basis, "filing_id": r["filing_id"], "broadcast_ns": int(r["broadcast_ns"]),
                "usable_day": usable_day(r["broadcast_ns"]).isoformat(), "fields": {}, "derived_from": None}
        v["fields"][r["field"]] = int(r["value_paise"])
    return list(out.values())                    # dict keeps the query's broadcast order


def _known(symbol: str, day: date) -> tuple[str, dict[tuple[str, str], dict]]:
    """(basis, {(period_end, period_kind): the version in use on `day`})."""
    basis = basis_on(symbol, day)
    iso = day.isoformat()
    chosen: dict[tuple[str, str], dict] = {}
    for v in _versions(symbol, basis):
        if v["usable_day"] > iso:
            continue
        key = (v["period_end"], v["period_kind"])
        cur = chosen.get(key)
        if cur is None or cur["fields"] != v["fields"]:
            chosen[key] = v
    return basis, chosen


def _derive(h: dict, q: dict) -> dict:
    fields = {k: h["fields"][k] - q["fields"][k] for k in FLOW if k in h["fields"] and k in q["fields"]}
    fields.update({k: h["fields"][k] for k in LEVELS if k in h["fields"]})
    return {"period_end": h["period_end"], "period_kind": "Q", "period_start": None, "basis": h["basis"],
            "filing_id": h["filing_id"], "broadcast_ns": max(h["broadcast_ns"], q["broadcast_ns"]),
            "usable_day": max(h["usable_day"], q["usable_day"]), "fields": fields,
            "derived_from": [h["filing_id"], q["filing_id"]]}


def _lists(symbol: str, day: date) -> dict:
    basis, chosen = _known(symbol, day)
    newest = lambda kind: sorted((v for (_, k), v in chosen.items() if k == kind),  # noqa: E731
                                 key=lambda v: v["period_end"], reverse=True)
    quarters = {v["period_end"]: v for v in newest("Q")}
    halves = newest("H")
    derived = {}
    for h in halves:
        if _months(h["period_start"], h["period_end"]) not in (None, 6) or h["period_end"] in quarters:
            continue
        other = shift_quarter(date.fromisoformat(h["period_end"]), -1).isoformat()
        if other in quarters:
            derived[h["period_end"]] = _derive(h, quarters[other])
    quarters.update(derived)
    return {"basis": basis, "quarters": [quarters[k] for k in sorted(quarters, reverse=True)],
            "half_years": halves, "annual": newest("A")}


def known_on(symbol: str, day: date) -> dict:
    """{"basis", "quarters": the latest 8 newest first, "half_years", "annual"} as known on `day`. Each entry:
    period_end, period_kind, period_start, basis, filing_id, broadcast_ns, usable_day (ISO), fields {field: paise},
    derived_from (None, or [H filing_id, quarter filing_id] for a quarter derived as H − Q)."""
    out = _lists(symbol, day)
    out["quarters"] = out["quarters"][:QUARTERS_KEPT]
    return out


def ttm(symbol: str, day: date, field: str) -> int | None:
    """The sum of `field` over the latest four consecutive filed quarters known on `day`, else None."""
    if field not in TTM_FIELDS:
        raise ValueError(f"{field!r} is not a quarterly flow (one of {', '.join(TTM_FIELDS)})")
    quarters = _lists(symbol, day)["quarters"]
    if not quarters:
        return None
    by_end = {q["period_end"]: q for q in quarters}
    newest = date.fromisoformat(quarters[0]["period_end"])
    total = 0
    for k in range(4):
        q = by_end.get(shift_quarter(newest, -k).isoformat())
        if q is None or q["derived_from"] is not None or field not in q["fields"]:
            return None
        total += q["fields"][field]
    return total


def _newest_first(lists: dict) -> list[dict]:
    entries = lists["quarters"] + lists["annual"] + lists["half_years"]
    return sorted(entries, key=lambda e: (e["period_end"], -_KIND_ORDER[e["period_kind"]], e["broadcast_ns"]),
                  reverse=True)


def latest(symbol: str, day: date, field: str) -> int | None:
    """`field` from the newest period known on `day` that has it (quarter before year before half year)."""
    for e in _newest_first(_lists(symbol, day)):
        if field in e["fields"]:
            return e["fields"][field]
    return None


# ── shares and market cap ────────────────────────────────────────────────────

def _instrument(symbol: str) -> int | None:
    r = refdata._connect().execute(
        "SELECT instrument_id FROM instruments WHERE exchange = 'NSE' AND tradingsymbol = ? AND kind = 'EQ'"
        " ORDER BY last_seen DESC, instrument_id DESC LIMIT 1", (symbol.upper(),)).fetchone()
    return None if r is None else int(r[0])


def _closes(iid: int, days: list[date]) -> dict[str, int]:
    out: dict[str, int] = {}
    isos = [d.isoformat() for d in days]
    conn = refdata._connect()
    for i in range(0, len(isos), 500):          # point reads on cm_eod's primary key (trade_date, instrument_id)
        part = isos[i:i + 500]
        for d, close in conn.execute(
                f"SELECT trade_date, close_paise FROM cm_eod WHERE trade_date IN ({', '.join('?' * len(part))})"
                " AND instrument_id = ? AND series = 'EQ'", (*part, iid)):
            if close is not None and close > 0:
                out[d] = int(close)
    return out


def _coverage_start() -> str | None:
    """The first trading day cm_eod holds any EQ close for (ISO), or None."""
    return refdata._connect().execute("SELECT MIN(trade_date) FROM cm_eod WHERE series = 'EQ'").fetchone()[0]


def _fall(seen: Fraction) -> str:
    return f"{(Decimal(seen.numerator) / Decimal(seen.denominator)).quantize(Decimal('0.001'))}"


def _splits(iid: int | None, after: date, through: date) -> tuple[list[dict], list[dict]]:
    """(splits / bonuses, holds) in NSE's raw closes on trading days after `after`, through `through`:
    [{"day", "ratio" (as configured)}], [{"day", "fall" (last close ÷ close, 3 dp), "gap_days" when it spans more
    than MAX_GAP_DAYS trading days without a close} | {"day", "reason"} when there is no close to compare with]."""
    if iid is None or through <= after:
        return [], []
    window = nse_calendar.trading_days(SEGMENT, after + timedelta(days=1), through)
    if not window:
        return [], []
    lead = [nse_calendar.previous_trading_day(SEGMENT, window[0])]
    while len(lead) < MAX_GAP_DAYS:
        lead.insert(0, nse_calendar.previous_trading_day(SEGMENT, lead[0]))
    days = lead + window
    closes = _closes(iid, days)
    ratios, tol, hold = _cfg()
    anchor = next((i for i in range(len(lead) - 1, -1, -1) if days[i].isoformat() in closes), None)
    if anchor is None:
        cover = _coverage_start()
        if cover is None or lead[-1].isoformat() < cover:
            return [], [{"day": window[0].isoformat(), "reason": f"no NSE close on record before {cover}"}]
        first = next((d for d in window if d.isoformat() in closes), None)
        if first is None:
            return [], []                         # no close at all: nothing priced, nothing to hold
        return [], [{"day": first.isoformat(),
                     "reason": f"no NSE close in the {MAX_GAP_DAYS} trading days before {window[0].isoformat()}"}]
    found, held = [], []
    for i in range(len(lead), len(days)):
        cb = closes.get(days[i].isoformat())
        if cb is None:
            continue
        seen = Fraction(closes[days[anchor].isoformat()], cb)
        gap = i - anchor - 1
        day = days[i].isoformat()
        if gap > MAX_GAP_DAYS:
            if seen >= hold:
                held.append({"day": day, "fall": _fall(seen), "gap_days": gap})
        else:
            match = next((text for text, r in ratios if abs(seen / r - 1) <= tol), None)
            if match is not None:
                found.append({"day": day, "ratio": match})
            elif seen >= hold:
                held.append({"day": day, "fall": _fall(seen)})
        anchor = i
    return found, held


def _profit(fields: dict) -> int | None:
    return fields.get("profit_owners", fields.get("net_profit"))


def _eps_agrees(fields: dict, shares: Fraction) -> bool | None:
    """EPS × shares ≈ profit to owners (None when the filing lacks either)."""
    profit = _profit(fields)
    if "eps_basic" not in fields or profit is None:
        return None
    return not xbrl._off(fields["eps_basic"] * shares, profit, xbrl._cfg("eps_pct"), shares / 2 + ROUNDING_SLACK_PAISE)


def shares_detail(symbol: str, day: date, *, price_day: date | None = None) -> dict | None:
    """The share count known on `day` and where it came from: {"shares" (None when held), "base_shares",
    "filing_id", "period_end", "period_kind", "source": "paid_up" | "eps", "adjustments": [{"day", "ratio"}],
    "held": [{"day", "fall"[, "gap_days"]} | {"day", "reason"}] (falls no ratio explains, or no close to compare
    with: the count is unknown; see _splits), "skipped": [filing_id whose paid-up
    count disagreed with its EPS]}. Splits are looked for through `price_day` (default `day`). None if no filing
    gives a count."""
    entries = [e for e in _newest_first(_lists(symbol, day)) if e["derived_from"] is None]
    base, skipped = None, []
    for e in entries:
        f = e["fields"]
        if f.get("paid_up_equity", 0) > 0 and f.get("face_value", 0) > 0:
            shares = Fraction(f["paid_up_equity"], f["face_value"])
            if _eps_agrees(f, shares) is False:
                skipped.append(e["filing_id"])
                continue
            base = (round(shares), e, "paid_up")
            break
    if base is None:
        for e in entries:
            f = e["fields"]
            profit = _profit(f)
            if f.get("eps_basic") and profit is not None and round(Fraction(profit, f["eps_basic"])) > 0:
                base = (round(Fraction(profit, f["eps_basic"])), e, "eps")
                break
    if base is None:
        return None
    count, e, source = base
    adjustments, held = _splits(_instrument(symbol), date.fromisoformat(e["period_end"]), price_day or day)
    factor = Fraction(1)
    for a in adjustments:
        factor *= Fraction(Decimal(a["ratio"]))
    return {"shares": None if held else round(count * factor), "base_shares": count, "filing_id": e["filing_id"],
            "period_end": e["period_end"], "period_kind": e["period_kind"], "source": source,
            "adjustments": adjustments, "held": held, "skipped": skipped}


def shares(symbol: str, day: date) -> int | None:
    """Shares outstanding known on `day` (see shares_detail), or None (none filed, or held by a fall)."""
    d = shares_detail(symbol, day)
    return None if d is None else d["shares"]


def market_cap(symbol: str, day: date) -> int | None:
    """NSE's raw close (EQ) on `day`, else the latest of the 5 trading days before it, × the shares known on `day`;
    paise, or None."""
    iid = _instrument(symbol)
    if iid is None:
        return None
    days, d = [day], day
    for _ in range(PRICE_LOOKBACK_DAYS):
        d = nse_calendar.previous_trading_day(SEGMENT, d)
        days.append(d)
    closes = _closes(iid, days)
    if not closes:
        return None
    price_day = max(closes)
    detail = shares_detail(symbol, day, price_day=date.fromisoformat(price_day))
    if detail is None or detail["shares"] is None:
        return None
    return closes[price_day] * detail["shares"]

"""FS 1: the screener's universe and NSE's sector classification per stock.

Universe (owner, 2026-11-17): today's Nifty 500 list, and of those only the stocks whose median daily traded value
over the last `liquidity_days` (63, about three months) trading days is at least `liquidity_floor_paise` (Rs 8
crore). Traded value is refdata `cm_eod.turnover_paise`, series EQ. The trading days are the newest
`liquidity_days` dates cm_eod holds up to the day; a stock with no row on one of them traded nothing that day and
counts 0 (a suspended stock is not liquid). A stock refdata does not know has no traded value and is not liquid.
With fewer than `liquidity_days` days in cm_eod the refresh is refused (NotEnoughHistory), never computed short.

Sectors: NSE's four levels (macro-economic sector, sector, industry, basic industry) per stock from the quote API's
`industryInfo`, stored with the day it was read; today's reading applies to the whole period (spec call 5).
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import date

from stockify import paths
from stockify.data import nse_files, refdata
from stockify.fundamentals import nse_api, store

log = logging.getLogger(__name__)

LIST_HEADER = ["Company Name", "Industry", "Symbol", "Series", "ISIN Code"]
INDUSTRY_KEYS = {"macro": "macro", "sector": "sector", "industry": "industry", "basic_industry": "basicIndustry"}


class NotEnoughHistory(ValueError):
    """refdata cm_eod holds fewer trading days up to the day than the liquidity window needs."""


# ── The Nifty 500 list ───────────────────────────────────────────────────────

def parse_list(body: bytes) -> list[dict]:
    """NSE's `ind_nifty500list.csv`: [{symbol, company, list_industry, series, isin}] in file order. ValueError for a
    file without NSE's header, a row of another width, an empty symbol, or a symbol listed twice."""
    text = body.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or [c.strip() for c in rows[0]] != LIST_HEADER:
        raise ValueError(f"the Nifty 500 list's header is {rows[0] if rows else []}, not {LIST_HEADER}")
    out, seen = [], set()
    for n, r in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in r):
            continue
        if len(r) != len(LIST_HEADER):
            raise ValueError(f"the Nifty 500 list's line {n} has {len(r)} columns, not {len(LIST_HEADER)}")
        company, industry, symbol, series, isin = (c.strip() for c in r)
        if not symbol or symbol in seen:
            raise ValueError(f"the Nifty 500 list's line {n} has an empty or repeated symbol {symbol!r}")
        seen.add(symbol)
        out.append({"symbol": symbol, "company": company, "list_industry": industry, "series": series, "isin": isin})
    if not out:
        raise ValueError("the Nifty 500 list has no rows")
    return out


def fetch_list(day: date, *, client: nse_api.Client) -> bytes:
    """Today's list from `nifty500_list_url`, else `nifty500_list_fallback_url`; kept raw as nifty500/<YYYYMMDD>.csv.
    A body that does not parse is not accepted from either. The last failure is raised when both fail."""
    cfg = client.cfg
    last: Exception | None = None
    for url in (cfg["nifty500_list_url"], cfg["nifty500_list_fallback_url"]):
        try:
            body = client.fetch(url, "the Nifty 500 list")
            parse_list(body)
        except (nse_files.NseError, ValueError) as e:
            log.warning("fundamentals: the Nifty 500 list could not be read from %s: %s", url, e)
            last = e
            continue
        nse_api.save_raw("nifty500", f"{day:%Y%m%d}.csv", body)
        return body
    raise last if last is not None else nse_files.NseError("no Nifty 500 list URL configured")


# ── Liquidity ────────────────────────────────────────────────────────────────

def _window(day: date, n: int) -> list[str]:
    rows = refdata._connect().execute(
        "SELECT DISTINCT trade_date FROM cm_eod WHERE trade_date <= ? AND series = 'EQ' "
        "ORDER BY trade_date DESC LIMIT ?", (day.isoformat(), n)).fetchall()
    return [r[0] for r in rows]


def _median(values: list[int]) -> int:
    """The middle value; for an even count, the mean of the two middle values rounded down (whole paise)."""
    v = sorted(values)
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) // 2


def traded_values(dates: list[str]) -> dict[int, dict[str, int]]:
    """{instrument_id: {trade_date: turnover_paise}} for every EQ row on `dates` (one range read on cm_eod's primary
    key, which starts with trade_date: cm_eod has no index on instrument_id)."""
    out: dict[int, dict[str, int]] = {}
    wanted = set(dates)
    for d, iid, value in refdata._connect().execute(
            "SELECT trade_date, instrument_id, turnover_paise FROM cm_eod WHERE trade_date BETWEEN ? AND ? "
            "AND series = 'EQ'", (min(dates), max(dates))):
        if d in wanted:
            out.setdefault(int(iid), {})[d] = int(value or 0)
    return out


def equity_ids() -> dict[str, int]:
    """{tradingsymbol: instrument_id} of NSE equities in refdata, as the cm_eod loader finds them
    (nse_files._equity_id: exchange NSE, kind EQ; the newest id when a symbol was reused)."""
    rows = refdata._connect().execute(
        "SELECT tradingsymbol, instrument_id, kind FROM instruments WHERE exchange = 'NSE' "
        "AND kind IN ('EQ', 'INDEX') ORDER BY instrument_id").fetchall()
    return {r[0].upper(): int(r[1]) for r in rows if r[2] == "EQ" and r[0]}


def median_value_paise(by_day: dict[str, int], dates: list[str]) -> int:
    """Median daily traded value (paise) of one stock over `dates`; a date without a cm_eod EQ row counts 0."""
    return _median([by_day.get(d, 0) for d in dates])


def refresh(day: date, *, client: nse_api.Client, today_list_csv: bytes | None = None) -> list[dict]:
    """Read today's Nifty 500 list (fetched unless given; kept raw either way), compute each stock's median traded
    value over the window ending on `day`, and store the day's universe. Returns the rows written (every listed
    stock, liquid or not)."""
    cfg = client.cfg
    n_days, floor = int(cfg["liquidity_days"]), int(cfg["liquidity_floor_paise"])
    dates = _window(day, n_days)
    if len(dates) < n_days:
        raise NotEnoughHistory(f"refdata cm_eod holds {len(dates)} trading days up to {day}, the liquidity window "
                               f"needs {n_days}: load more NSE daily files first (stockify nse backfill)")
    if today_list_csv is None:
        body = fetch_list(day, client=client)
    else:
        body = today_list_csv
        parse_list(body)
        nse_api.save_raw("nifty500", f"{day:%Y%m%d}.csv", body)
    values, ids = traded_values(dates), equity_ids()
    rows = []
    for item in parse_list(body):
        iid = ids.get(item["symbol"].upper())
        median = None if iid is None else median_value_paise(values.get(iid, {}), dates)
        rows.append({"as_of": day.isoformat(), "symbol": item["symbol"], "instrument_id": iid, "isin": item["isin"],
                     "company": item["company"], "list_industry": item["list_industry"],
                     "median_value_paise": median, "liquid": int(median is not None and median >= floor)})
    store.put_universe(day.isoformat(), rows)
    log.info("fundamentals: universe %s: %d listed, %d liquid", day, len(rows), sum(r["liquid"] for r in rows))
    return rows


def current() -> list[dict]:
    """The newest universe's liquid stocks, by symbol; [] before the first refresh."""
    as_of = store.latest_as_of()
    return [] if as_of is None else store.universe_on(as_of, liquid_only=True)


# ── Sectors ──────────────────────────────────────────────────────────────────

def parse_industry_info(doc) -> dict | None:  # noqa: ANN001
    """{macro, sector, industry, basic_industry} from a quote-equity answer's `industryInfo`; None when it is missing
    or any level is not a non-empty string."""
    info = doc.get("industryInfo") if isinstance(doc, dict) else None
    if not isinstance(info, dict):
        return None
    out = {}
    for col, key in INDUSTRY_KEYS.items():
        value = info.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
        out[col] = value.strip()
    return out


def refresh_sectors(symbols, day: date, *, client: nse_api.Client) -> int:  # noqa: ANN001
    """Read each symbol's classification from the quote API and store it (one symbol at a time, so a run stopped by
    `Blocked`, which is raised, keeps what it read). A symbol NSE answers without `industryInfo`, or with another
    error than Blocked, is logged and skipped. Returns how many symbols were stored."""
    api = client.cfg["api"]["quote_equity"]
    stamp = f"{day:%Y%m%d}"
    stored = 0
    for sym in symbols:
        try:
            doc = client.json(api["path"], {"symbol": sym}, warmup=api["warmup"].replace("{symbol}", sym),
                              name=f"{sym}_{stamp}.json" if _safe(sym) else None)
        except nse_api.Blocked:
            raise
        except nse_files.NseError as e:
            log.warning("fundamentals: no sector for %s: %s", sym, e)
            continue
        levels = parse_industry_info(doc)
        if levels is None:
            log.warning("fundamentals: no sector for %s: the answer has no complete industryInfo", sym)
            continue
        store.put_sectors(day.isoformat(), [{"symbol": sym, **levels}])
        stored += 1
    return stored


def _safe(symbol: str) -> bool:
    return paths.safe_segment(f"{symbol}_00000000.json", 160)

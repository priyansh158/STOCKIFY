"""Data fetchers used by the analysis pipeline.

Streamlit-free versions of `get_yf_info`, `_get_yf_financials`,
`_fetch_nse_quarterly_pdfs`, `_fetch_screener_in`, `_fetch_generic_ir`,
`scrape_financials_playwright`, `fetch_stage2_articles`, and
`_build_macro_context_for_geo` — all originally in `app.py`.

The behaviour is identical; the only changes are:
  * No `st.spinner` / `st.progress` calls
  * No `st.session_state` reads
  * Errors are returned as part of the result, not surfaced via `st.error`
"""
from __future__ import annotations

import io
import logging
import math
import os
import re
import time
from datetime import datetime, timezone, timedelta

import requests

import kite_client
import kite_ticker_worker
from india_utils import strip_exchange_suffix

log = logging.getLogger("stockify.data")

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False

try:
    import pypdf
    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False

_FIN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_MIN_ARTICLES = 5


# ── Env key resolvers (process-level, no Streamlit session) ───────────────────

def _resolve_newsapi_key() -> str:
    return (os.environ.get("NEWSAPI_KEY", "") or "").strip()


def _resolve_rapidapi_key() -> str:
    return (os.environ.get("RAPIDAPI_KEY", "") or "").strip()


# ── yfinance company info + financial DataFrames ──────────────────────────────

def get_yf_info(ticker: str) -> dict:
    """Pull basic company data from yfinance, preferring Kite name when available."""
    if not YFINANCE_AVAILABLE:
        return {}
    try:
        info = yf.Ticker(ticker).info  # type: ignore[attr-defined]
        name = info.get("longName") or info.get("shortName", ticker)

        if kite_client.is_enabled():
            try:
                bare = strip_exchange_suffix(ticker)
                rows = kite_client.resolve_symbol(bare, limit=1)
                if rows and (rows[0].get("name") or "").strip():
                    name = rows[0]["name"]
            except Exception:
                pass

        return {
            "name":        name,
            "sector":      info.get("sector", ""),
            "industry":    info.get("industry", ""),
            "description": info.get("longBusinessSummary", ""),
            "country":     info.get("country", ""),
            "employees":   info.get("fullTimeEmployees"),
            "market_cap":  info.get("marketCap"),
            "website":     info.get("website", ""),
        }
    except Exception as e:
        log.warning("yfinance info failed for %s: %s", ticker, e)
        return {}


def _yf_df_to_dict(df) -> dict:
    if df is None or getattr(df, "empty", True):
        return {}
    result: dict = {}
    for col in df.columns:
        col_key = str(col)
        result[col_key] = {}
        for idx, v in df[col].items():
            try:
                fv = float(v)
                result[col_key][str(idx)] = None if math.isnan(fv) else fv
            except (TypeError, ValueError):
                result[col_key][str(idx)] = str(v) if v is not None else None
    return result


def get_yf_financials(ticker: str) -> dict:
    if not YFINANCE_AVAILABLE:
        return {}
    try:
        t = yf.Ticker(ticker)  # type: ignore[attr-defined]
        return {
            "income_statement":           _yf_df_to_dict(t.financials),
            "quarterly_income_statement": _yf_df_to_dict(t.quarterly_financials),
            "balance_sheet":              _yf_df_to_dict(t.balance_sheet),
            "quarterly_balance_sheet":    _yf_df_to_dict(t.quarterly_balance_sheet),
            "cash_flow":                  _yf_df_to_dict(t.cash_flow),
            "quarterly_cash_flow":        _yf_df_to_dict(t.quarterly_cash_flow),
        }
    except Exception as e:
        log.warning("yfinance financials failed for %s: %s", ticker, e)
        return {}


# ── PDF + HTML helpers ────────────────────────────────────────────────────────

def _extract_pdf_bytes(pdf_bytes: bytes) -> str:
    if not PYPDF_AVAILABLE:
        return ""
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        pages = []
        for page in reader.pages:
            txt = page.extract_text() or ""
            if txt.strip():
                pages.append(txt)
        return "\n\n".join(pages)[:15000]
    except Exception:
        return ""


def _clean_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    for ent, ch in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                    ("&gt;", ">"), ("&#160;", " "), ("&#8211;", "–")]:
        text = text.replace(ent, ch)
    text = re.sub(r"&#\d+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


# ── Financial filings scrape pipeline ─────────────────────────────────────────

def _fetch_screener_in(symbol: str) -> str:
    headers = {"User-Agent": _FIN_UA}
    for url in [
        f"https://www.screener.in/company/{symbol}/consolidated/",
        f"https://www.screener.in/company/{symbol}/",
    ]:
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code != 200 or len(resp.text) < 2000:
                continue
            clean = _clean_html(resp.text)
            parts: list[str] = []
            for section in ["Quarterly Results", "Profit & Loss", "Balance Sheet",
                            "Cash Flows", "Ratios"]:
                pos = clean.find(section)
                if pos >= 0:
                    parts.append(f"=== {section} ===\n{clean[pos:pos+3500]}")
            if parts:
                return f"[Source: Screener.in — {symbol}]\n\n" + "\n\n".join(parts)
        except Exception as e:
            log.debug("Screener fetch failed for %s: %s", symbol, e)
            continue
    return ""


def _fetch_nse_quarterly_pdfs(symbol: str) -> str:
    headers = {
        "User-Agent": _FIN_UA,
        "Referer":    "https://www.nseindia.com",
        "Accept":     "application/json",
    }
    texts: list[str] = []
    try:
        s = requests.Session()
        s.get("https://www.nseindia.com", headers=headers, timeout=12)
        resp = s.get(
            f"https://www.nseindia.com/api/corporates-financial-results"
            f"?index=equities&symbol={symbol}&period=Quarterly",
            headers=headers, timeout=12,
        )
        if resp.status_code != 200:
            return ""
        data = resp.json()
        seen_quarters: set = set()
        quarters: list[dict] = []
        for filing in data:
            key = (filing.get("fromDate"), filing.get("toDate"))
            if "Consolidated" in filing.get("consolidated", "") and key not in seen_quarters:
                seen_quarters.add(key)
                quarters.append(filing)
                if len(quarters) >= 3:
                    break
        if not quarters:
            return ""
        for q in quarters:
            seq = q.get("seqNumber", "")
            label = f"{q.get('relatingTo','')} {q.get('financialYear','')}"
            for pdf_url in [
                f"https://nsearchives.nseindia.com/corporate/qr/{seq}.pdf",
                f"https://nsearchives.nseindia.com/corporate/financialresults/{seq}.pdf",
            ]:
                try:
                    pr = s.get(pdf_url, headers=headers, timeout=15, allow_redirects=True)
                    if pr.status_code == 200 and b"%PDF" in pr.content[:8]:
                        txt = _extract_pdf_bytes(pr.content)
                        if txt.strip():
                            texts.append(f"[NSE Quarterly Result — {label}]\n{txt[:5000]}")
                            break
                except Exception:
                    continue
    except Exception as e:
        log.debug("NSE PDF fetch failed for %s: %s", symbol, e)
    return "\n\n---\n\n".join(texts)


def _fetch_generic_ir(website: str) -> str:
    if not website:
        return ""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ""
    base = website.rstrip("/")
    ir_paths = [
        "/investor-relations",
        "/investors",
        "/ir",
        "/financials",
        "/investor-relations/financial-information",
    ]
    result = ""
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=_FIN_UA)
            page = ctx.new_page()
            for path in ir_paths:
                try:
                    resp = page.goto(base + path, timeout=12000, wait_until="domcontentloaded")
                    if resp and resp.status < 400:
                        page.wait_for_timeout(2000)
                        try:
                            page.evaluate("""() => {
                                ['nav','header','footer','script','style'].forEach(
                                s => document.querySelectorAll(s).forEach(el => el.remove()));
                            }""")
                        except Exception:
                            pass
                        text = (page.inner_text("body") or "")[:12000].strip()
                        if len(text) > 500:
                            result = f"[Source: {base+path}]\n\n{text}"
                            break
                except Exception:
                    continue
            browser.close()
    except Exception as e:
        log.debug("Playwright IR fetch failed: %s", e)
    return result


def scrape_financials(website: str, ticker: str) -> tuple[str, dict, str]:
    """NSE → Screener → IR page fallback chain. Returns (text, yf_df_dict, source_tag)."""
    yf_data = get_yf_financials(ticker)
    symbol = re.sub(r"\.(NS|BO|BSE|NSE)$", "", ticker, flags=re.IGNORECASE).upper()

    scraped = _fetch_nse_quarterly_pdfs(symbol)
    source_tag = "NSE" if scraped else "NONE"

    if not scraped:
        scraped = _fetch_screener_in(symbol)
        if scraped:
            source_tag = "SCREENER"

    if not scraped and website:
        scraped = _fetch_generic_ir(website)
        if scraped:
            source_tag = "IR_WEBSITE"

    if source_tag == "NONE" and yf_data and any(v for v in yf_data.values()):
        source_tag = "YFINANCE_ONLY"

    return scraped, yf_data, source_tag


# ── News article fetch (Streamlit-free) ───────────────────────────────────────

def _fetch_newsapi_articles(query: str, newsapi_key: str, sources: str | None = None) -> list[dict]:
    params = {
        "q":        query,
        "sortBy":   "relevancy",
        "pageSize": 10,
        "language": "en",
        "apiKey":   newsapi_key,
    }
    if sources:
        params["sources"] = sources
    resp = requests.get("https://newsapi.org/v2/everything", params=params, timeout=15)
    resp.raise_for_status()
    raw = resp.json()
    if raw.get("status") != "ok":
        return []
    out = []
    for item in raw.get("articles", []):
        title = (item.get("title") or "").strip()
        if not title or title == "[Removed]":
            continue
        out.append({
            "title":   title,
            "source":  item.get("source", {}).get("name", "NewsAPI"),
            "content": (item.get("description") or "").strip(),
            "url":     (item.get("url") or "").strip(),
        })
    return out


def _fetch_wsj_articles(query: str, rapidapi_key: str) -> list[dict]:
    headers = {
        "X-RapidAPI-Key":  rapidapi_key,
        "X-RapidAPI-Host": "wall-street-journal.p.rapidapi.com",
        "Content-Type":    "application/json",
    }
    resp = requests.get(
        "https://wall-street-journal.p.rapidapi.com/api/v1/searchArticleByKeyword",
        headers=headers,
        params={"keyword": query, "pageNumber": "1"},
        timeout=15,
    )
    resp.raise_for_status()
    raw = resp.json()
    if not raw.get("status"):
        return []
    out = []
    for item in raw.get("data", []):
        title = (item.get("headline") or "").strip()
        if not title:
            continue
        out.append({
            "title":   title,
            "source":  "Wall Street Journal",
            "content": (item.get("summary") or "").strip(),
            "url":     (item.get("url") or "").strip(),
        })
    return out


def fetch_google_news_articles(
    queries: list[str],
    *,
    open_ended: str = "3d",
    max_per_query: int = 12,
    total_max: int = 40,
) -> tuple[list[dict], list[str]]:
    """Primary news source for geo stage — STOCKIFY gnews Google News client."""
    errors: list[str] = []
    try:
        from gnews import GoogleNewsClient
    except Exception as e:
        return [], [f"gnews import failed: {e}"]

    qs = [q.strip() for q in (queries or []) if (q or "").strip()]
    if not qs:
        return [], ["no queries"]

    try:
        client = GoogleNewsClient(language="en", region="IN")
        result = client.search_many(
            qs,
            max_items_per_query=max_per_query,
            open_ended=open_ended,
            fetch_article_details=False,
            total_max=total_max,
        )
        errors.extend(result.errors)
        return result.to_pipeline_articles(), errors
    except Exception as e:
        log.warning("Google News fetch failed: %s", e)
        return [], [str(e)]


def fetch_stage2_articles(queries: list[str]) -> tuple[list[dict], list[str]]:
    """Primary: Google News (gnews). Fallback: NewsAPI ToI, then WSJ RapidAPI."""
    seen: dict[str, dict] = {}
    errors: list[str] = []

    gnews_arts, gnews_errs = fetch_google_news_articles(queries)
    errors.extend(gnews_errs)
    for art in gnews_arts:
        key = (art.get("title") or "").strip().lower()
        if key and key not in seen:
            seen[key] = art

    rapidapi_key = _resolve_rapidapi_key()
    newsapi_key = _resolve_newsapi_key()

    if len(seen) < _MIN_ARTICLES and newsapi_key:
        for q in queries:
            try:
                for art in _fetch_newsapi_articles(q, newsapi_key, sources="the-times-of-india"):
                    key = art["title"].strip().lower()
                    if key not in seen:
                        seen[key] = art
            except Exception as e:
                errors.append(str(e))

    if len(seen) < _MIN_ARTICLES and rapidapi_key:
        for q in queries:
            try:
                for art in _fetch_wsj_articles(q, rapidapi_key):
                    key = art["title"].strip().lower()
                    if key not in seen:
                        seen[key] = art
            except Exception as e:
                errors.append(str(e))

    return list(seen.values()), errors


def recent_geo_alerts_for_ticker(
    ticker: str,
    *,
    limit: int = 8,
    max_age_hours: int = 72,
) -> list[dict]:
    """Pull recent HIGH/CRITICAL monitor alerts relevant to this ticker or macro."""
    try:
        import geo_alerts
    except Exception:
        return []

    geo_alerts.init_db()
    sym = (ticker or "").strip().upper()
    cutoff = int(time.time()) - max_age_hours * 3600
    out: list[dict] = []
    for a in geo_alerts.list_alerts(limit=50, min_severity="HIGH"):
        if a.created_at < cutoff:
            continue
        tickers = [t.upper() for t in (a.affected_tickers or [])]
        scope = (a.scope or "").lower()
        relevant = (
            scope in ("macro", "both", "")
            or (sym and sym in tickers)
            or (sym and sym in (a.title or "").upper())
        )
        if not relevant:
            continue
        out.append(
            {
                "title": a.title,
                "source": a.source or "STOCKIFY monitor",
                "content": a.why_disruptive or a.investor_action or "",
                "url": a.url or "",
                "published_at": a.published_at or "",
                "severity": a.severity,
                "disruption_score": a.disruption_score,
                "from_monitor": True,
            }
        )
        if len(out) >= limit:
            break
    return out


# ── Macro snapshot for geo prompt (one-shot per analysis) ─────────────────────

def build_macro_context() -> dict | None:
    """Live macro snapshot (NIFTY 50 / sector indices / USD-INR) for the geo prompt.

    Returns None when Kite is unauthenticated or no macro instruments resolve.
    """
    if not kite_client.is_authenticated():
        return None
    try:
        macro_tokens = kite_ticker_worker.resolve_macro_tokens()
    except Exception:
        macro_tokens = {}
    if not macro_tokens:
        return None
    try:
        quote_resp = kite_client.quote(list(macro_tokens.values()))
    except Exception:
        return None
    if not quote_resp:
        return None

    indices: dict[str, dict] = {}
    currency: dict[str, dict] = {}
    for label, tok in macro_tokens.items():
        data = quote_resp.get(str(tok)) or quote_resp.get(tok)
        if not data:
            continue
        ltp = float(data.get("last_price") or 0)
        prev = float((data.get("ohlc") or {}).get("close") or 0)
        change_pct = ((ltp - prev) / prev * 100.0) if prev else 0.0
        bucket = currency if label in ("USDINR",) else indices
        bucket[label] = {"ltp": ltp, "change_pct": change_pct, "prev_close": prev}

    ist = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist)
    h, m = now_ist.hour, now_ist.minute
    if (h, m) < (9, 0):
        state = "CLOSED"
    elif (h, m) < (9, 15):
        state = "PRE_OPEN"
    elif (h, m) < (15, 30):
        state = "OPEN"
    else:
        state = "CLOSED"

    if not indices and not currency:
        return None
    return {
        "fetched_at": int(time.time()),
        "indices":    indices,
        "currency":   currency,
        "market_state": state,
    }

"""Always-on geo news monitor — independent of FastAPI uptime.

One poll cycle:
  1. Build macro + holdings queries
  2. Fetch Google News via gnews (fast path, no decode)
  3. Mark new articles; heuristic prefilter
  4. Gemini classify suspects
  5. Persist HIGH/CRITICAL alerts + email
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_a, **_k):  # type: ignore[misc]
        return False

import geo_alerts
from gnews import GoogleNewsClient
from geo_monitor.email_notify import try_send_alert_email
from geo_monitor.gemini_classifier import (
    classify_batch,
    heuristic_suspect,
    resolve_gemini_api_key,
    should_alert,
)
from geo_monitor.queries import build_monitor_queries

log = logging.getLogger("geo_monitor.worker")


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env() -> None:
    load_dotenv(dotenv_path=os.path.join(_repo_root(), ".env"), override=True)


def _interval_sec() -> int:
    try:
        return max(60, int(os.environ.get("GEO_MONITOR_INTERVAL_SEC", "600")))
    except ValueError:
        return 600


def _open_ended() -> str:
    return (os.environ.get("GEO_MONITOR_WHEN", "1h") or "1h").strip()


def _holdings_for_queries() -> tuple[list[dict], list[str]]:
    """Return (holding dicts for queries, ticker list for Gemini)."""
    try:
        import portfolio_sync

        snap = portfolio_sync.cached_snapshot(max_age_seconds=300)
        holdings = []
        tickers = []
        for h in snap.holdings:
            if h.quantity <= 0:
                continue
            holdings.append(
                {
                    "tradingsymbol": h.tradingsymbol,
                    "name": "",  # Kite holdings lack long name; ticker is enough
                }
            )
            tickers.append(h.tradingsymbol)
        if snap.error:
            log.warning("Portfolio snapshot: %s — macro-only this cycle", snap.error)
        return holdings, tickers
    except Exception as e:
        log.warning("Holdings unavailable (%s) — macro-only", e)
        return [], []


def run_once(
    *,
    client: Optional[GoogleNewsClient] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Execute a single monitor cycle. Returns stats dict."""
    load_env()
    geo_alerts.init_db()

    holdings, tickers = _holdings_for_queries()
    queries = build_monitor_queries(holdings, include_macro=True)
    when = _open_ended()
    client = client or GoogleNewsClient(language="en", region="IN")

    stats: dict[str, Any] = {
        "queries": len(queries),
        "fetched": 0,
        "new": 0,
        "suspects": 0,
        "classified": 0,
        "alerts": 0,
        "emailed": 0,
        "errors": [],
    }

    result = client.search_many(
        queries,
        max_items_per_query=15,
        open_ended=when,
        fetch_article_details=False,
        total_max=250,
    )
    stats["fetched"] = result.unique_count
    stats["errors"].extend(result.errors)

    holding_terms = list(tickers)
    new_articles: list[dict[str, Any]] = []
    for art in result.articles:
        guid = art.guid or art.link
        newly = geo_alerts.mark_seen(
            guid=guid,
            title=art.title,
            url=art.loaded_url or art.link,
            source=art.source,
            published_at=art.published_at,
            query=art.query,
        )
        if not newly:
            continue
        stats["new"] += 1
        row = {
            "guid": guid,
            "title": art.title,
            "source": art.source,
            "description": art.description,
            "url": art.loaded_url or art.link,
            "published_at": art.published_at,
            "query": art.query,
        }
        if heuristic_suspect(
            art.title, art.description, holding_terms=holding_terms
        ):
            new_articles.append(row)

    stats["suspects"] = len(new_articles)
    if not new_articles:
        return stats

    if not resolve_gemini_api_key():
        stats["errors"].append("no Gemini API key — suspects not classified")
        return stats

    try:
        # Batch in chunks of 12 to keep prompts bounded
        classified: list[dict[str, Any]] = []
        chunk_size = 12
        for i in range(0, len(new_articles), chunk_size):
            chunk = new_articles[i : i + chunk_size]
            classified.extend(
                classify_batch(chunk, holding_tickers=tickers)
            )
        stats["classified"] = len(classified)
    except Exception as e:
        log.exception("Gemini classification failed")
        stats["errors"].append(str(e))
        return stats

    for c in classified:
        if not should_alert(c):
            continue
        if dry_run:
            stats["alerts"] += 1
            continue
        alert_id = geo_alerts.save_alert(
            severity=c.get("severity", "HIGH"),
            disruption_score=int(c.get("disruption_score") or 0),
            scope=str(c.get("scope") or "macro"),
            title=c.get("title") or "",
            source=c.get("source") or "",
            url=c.get("url") or "",
            published_at=c.get("published_at") or "",
            guid=c.get("guid") or "",
            why_disruptive=c.get("why_disruptive") or "",
            investor_action=c.get("investor_action") or "",
            confidence=float(c.get("confidence") or 0),
            affected_tickers=c.get("affected_tickers") or [],
            article=c,
        )
        if alert_id is None:
            continue
        stats["alerts"] += 1
        payload = geo_alerts.get_alert(alert_id)
        if not payload:
            continue
        err = try_send_alert_email(payload.to_dict())
        if err is None:
            geo_alerts.mark_emailed(alert_id)
            stats["emailed"] += 1
        elif err != "email not configured (skipped)":
            stats["errors"].append(f"email: {err}")
        else:
            stats["errors"].append(err)

    return stats


def run_forever(*, once: bool = False) -> None:
    load_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not resolve_gemini_api_key():
        log.warning("GOOGLE_AI_API_KEY / GEMINI_API_KEY not set — classify will fail")

    log.info(
        "Geo monitor starting (interval=%ss, when=%s)",
        _interval_sec(),
        _open_ended(),
    )
    while True:
        started = time.time()
        try:
            stats = run_once()
            log.info("Cycle done: %s", stats)
        except Exception:
            log.exception("Monitor cycle crashed")
        if once:
            return
        sleep_for = max(5, _interval_sec() - int(time.time() - started))
        time.sleep(sleep_for)

"""High-level Google News client — RSS search with day-slicing and optional decode.

Designed as a pure library for later STOCKIFY geo pipeline + always-on monitor.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

import requests

from gnews.decode import enrich_article_details
from gnews.models import Article, SearchResult
from gnews.rss import build_search_url, build_topic_url, fetch_and_parse
from gnews.slicing import resolve_windows

log = logging.getLogger("gnews.client")


def _dedupe(articles: list[Article]) -> list[Article]:
    seen: set[str] = set()
    out: list[Article] = []
    for a in articles:
        key = (a.guid or "").strip() or a.title.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


class GoogleNewsClient:
    """Apify-style Google News scraper backed by public RSS feeds."""

    def __init__(
        self,
        *,
        language: str = "en",
        region: str = "IN",
        session: Optional[requests.Session] = None,
    ):
        self.language = language
        self.region = region
        self.session = session or requests.Session()

    def search(
        self,
        q: str,
        *,
        max_items: int = 100,
        open_ended: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        fetch_article_details: bool = False,
        fetch_image: bool = True,
        decode_limit: Optional[int] = None,
    ) -> SearchResult:
        """Search Google News.

        Args:
            q: Search query (supports Google operators).
            max_items: Cap on returned articles after dedupe.
            open_ended: Relative window — 1h, 1d, 7d, 30d, 1y.
            date_from / date_to: Inclusive YYYY-MM-DD range.
            fetch_article_details: Resolve publisher URLs via batchexecute.
            fetch_image: When details=True, also pull og:image.
            decode_limit: Max articles to decode (default = all returned).
        """
        result = SearchResult(query=q)
        if not (q or "").strip():
            result.errors.append("empty query")
            return result

        try:
            windows = resolve_windows(
                q,
                max_items=max_items,
                open_ended=open_ended,
                date_from=date_from,
                date_to=date_to,
            )
        except ValueError as e:
            result.errors.append(str(e))
            return result

        collected: list[Article] = []
        for composed_q in windows:
            if len(_dedupe(collected)) >= max_items:
                break
            url = build_search_url(composed_q, language=self.language, region=self.region)
            try:
                batch = fetch_and_parse(url, query=composed_q, session=self.session)
                collected.extend(batch)
                result.windows_fetched += 1
                result.raw_count += len(batch)
            except Exception as e:
                msg = f"window fetch failed for {composed_q!r}: {e}"
                log.warning(msg)
                result.errors.append(msg)

        unique = _dedupe(collected)[: max(0, max_items)]
        result.unique_count = len(unique)

        if fetch_article_details and unique:
            limit = decode_limit if decode_limit is not None else len(unique)
            for art in unique[:limit]:
                try:
                    enrich_article_details(
                        art,
                        session=self.session,
                        fetch_image=fetch_image,
                    )
                except Exception as e:
                    art.decode_error = str(e)

        result.articles = unique
        return result

    def search_many(
        self,
        queries: Iterable[str],
        *,
        max_items_per_query: int = 30,
        open_ended: Optional[str] = "1d",
        fetch_article_details: bool = False,
        fetch_image: bool = False,
        total_max: Optional[int] = 200,
    ) -> SearchResult:
        """Run multiple queries and merge/dedupe — for macro + holdings monitors."""
        merged = SearchResult(query=" | ".join(queries))
        all_arts: list[Article] = []
        for q in queries:
            q = (q or "").strip()
            if not q:
                continue
            partial = self.search(
                q,
                max_items=max_items_per_query,
                open_ended=open_ended,
                fetch_article_details=False,
            )
            merged.windows_fetched += partial.windows_fetched
            merged.raw_count += partial.raw_count
            merged.errors.extend(partial.errors)
            all_arts.extend(partial.articles)

        unique = _dedupe(all_arts)
        if total_max is not None:
            unique = unique[:total_max]
        merged.unique_count = len(unique)

        if fetch_article_details:
            for art in unique:
                try:
                    enrich_article_details(
                        art, session=self.session, fetch_image=fetch_image
                    )
                except Exception as e:
                    art.decode_error = str(e)

        merged.articles = unique
        return merged

    def topic(
        self,
        topic: str,
        *,
        max_items: int = 100,
        fetch_article_details: bool = False,
    ) -> SearchResult:
        """Fetch a named topic feed (BUSINESS, WORLD, TECHNOLOGY, …)."""
        result = SearchResult(query=f"topic:{topic}")
        url = build_topic_url(topic, language=self.language, region=self.region)
        try:
            arts = fetch_and_parse(url, query=result.query, session=self.session)
            result.windows_fetched = 1
            result.raw_count = len(arts)
            unique = _dedupe(arts)[:max_items]
            result.unique_count = len(unique)
            if fetch_article_details:
                for art in unique:
                    enrich_article_details(art, session=self.session, fetch_image=True)
            result.articles = unique
        except Exception as e:
            result.errors.append(str(e))
        return result

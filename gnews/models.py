"""Article models for the Google News scraper.

`Article.to_pipeline_dict()` is the stable contract for later STOCKIFY
geo/analysis pipeline integration (matches fetch_stage2_articles shape).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Article:
    title: str
    link: str  # Google News / RSS wrapper URL
    guid: str
    source: str
    published_at: str  # ISO-8601 when parseable, else raw
    query: str = ""
    source_url: Optional[str] = None
    rss_link: Optional[str] = None
    loaded_url: Optional[str] = None  # decoded publisher URL
    image: Optional[str] = None
    description: str = ""
    decode_error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Full scraper output (Apify-like camelCase field names)."""
        return {
            "title": self.title,
            "link": self.link,
            "guid": self.guid,
            "source": self.source,
            "sourceUrl": self.source_url,
            "publishedAt": self.published_at,
            "loadedUrl": self.loaded_url,
            "rssLink": self.rss_link or self.link,
            "image": self.image,
            "description": self.description,
            "query": self.query,
            "decodeError": self.decode_error,
        }

    def to_pipeline_dict(self) -> dict[str, str]:
        """Stable shape for analysis_pipeline / geo stage consumers."""
        return {
            "title": self.title,
            "source": self.source,
            "content": self.description,
            "url": self.loaded_url or self.link,
            "published_at": self.published_at,
        }


@dataclass
class SearchResult:
    articles: list[Article] = field(default_factory=list)
    query: str = ""
    windows_fetched: int = 0
    raw_count: int = 0
    unique_count: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "articles": [a.to_dict() for a in self.articles],
            "query": self.query,
            "windowsFetched": self.windows_fetched,
            "rawCount": self.raw_count,
            "uniqueCount": self.unique_count,
            "errors": self.errors,
        }

    def to_pipeline_articles(self) -> list[dict[str, str]]:
        return [a.to_pipeline_dict() for a in self.articles]

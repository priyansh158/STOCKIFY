"""STOCKIFY Google News scraper.

Public RSS-based client with day-slicing and optional publisher URL decode.
See gnews/README.md for usage and limitations.
"""
from gnews.client import GoogleNewsClient
from gnews.models import Article, SearchResult

__all__ = ["GoogleNewsClient", "Article", "SearchResult"]
__version__ = "0.1.0"

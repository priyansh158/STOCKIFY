"""RSS URL building, HTTP fetch, and XML parsing for Google News."""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Optional
from urllib.parse import quote_plus, urlparse

import requests

from gnews.models import Article

log = logging.getLogger("gnews.rss")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

_TAG_RE = re.compile(r"<[^>]+>")


def locale_to_params(language: str = "en", region: str = "IN") -> tuple[str, str, str]:
    """Map language/region to Google News hl, gl, ceid."""
    lang = (language or "en").strip().lower()
    reg = (region or "IN").strip().upper()
    # Accept "en-IN" style language
    if "-" in lang:
        parts = lang.split("-", 1)
        lang = parts[0]
        if not region:
            reg = parts[1].upper()
    hl = f"{lang}-{reg}" if reg else lang
    ceid = f"{reg}:{lang}"
    return hl, reg, ceid


def build_search_url(
    q: str,
    *,
    language: str = "en",
    region: str = "IN",
) -> str:
    hl, gl, ceid = locale_to_params(language, region)
    return (
        "https://news.google.com/rss/search?"
        f"q={quote_plus(q)}&hl={quote_plus(hl)}&gl={quote_plus(gl)}&ceid={quote_plus(ceid)}"
    )


def build_topic_url(
    topic: str,
    *,
    language: str = "en",
    region: str = "IN",
) -> str:
    """Named topic feed, e.g. BUSINESS, WORLD, TECHNOLOGY."""
    hl, gl, ceid = locale_to_params(language, region)
    topic_key = topic.strip().upper()
    return (
        f"https://news.google.com/rss/headlines/section/topic/{quote_plus(topic_key)}"
        f"?hl={quote_plus(hl)}&gl={quote_plus(gl)}&ceid={quote_plus(ceid)}"
    )


def _strip_html(text: str) -> str:
    return unescape(_TAG_RE.sub("", text or "")).strip()


def _parse_pub_date(raw: str) -> str:
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, IndexError):
        return raw.strip()


def _source_homepage(source_url: Optional[str], link: str) -> Optional[str]:
    if source_url:
        try:
            p = urlparse(source_url)
            if p.scheme and p.netloc:
                return f"{p.scheme}://{p.netloc}"
        except Exception:
            pass
    return None


def parse_rss_xml(xml_text: str, query: str = "") -> list[Article]:
    """Parse a Google News RSS XML document into Article objects."""
    if not xml_text or not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.warning("RSS XML parse error: %s", e)
        return []

    channel = root.find("channel")
    items = channel.findall("item") if channel is not None else root.findall("item")
    articles: list[Article] = []

    for item in items:
        title = _strip_html((item.findtext("title") or "").strip())
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        description = _strip_html(item.findtext("description") or "")
        pub = _parse_pub_date(item.findtext("pubDate") or "")

        source_el = item.find("source")
        source_name = ""
        source_url = None
        if source_el is not None:
            source_name = (source_el.text or "").strip()
            source_url = (source_el.get("url") or "").strip() or None

        if not title:
            continue

        articles.append(
            Article(
                title=title,
                link=link,
                guid=guid,
                source=source_name or "Unknown",
                source_url=_source_homepage(source_url, link),
                published_at=pub,
                description=description,
                rss_link=link,
                query=query,
            )
        )
    return articles


def fetch_rss(
    url: str,
    *,
    timeout: float = 20.0,
    max_retries: int = 4,
    session: Optional[requests.Session] = None,
) -> str:
    """GET an RSS URL with retries/backoff. Returns response text."""
    sess = session or requests.Session()
    headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml, */*"}
    last_err: Exception | None = None

    for attempt in range(max_retries):
        try:
            resp = sess.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = min(2 ** attempt, 30)
                log.warning("RSS HTTP %s for %s — retry in %ss", resp.status_code, url[:80], wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            last_err = e
            wait = min(2 ** attempt, 30)
            log.warning("RSS fetch error (%s) — retry in %ss", e, wait)
            time.sleep(wait)

    raise RuntimeError(f"Failed to fetch RSS after {max_retries} attempts: {last_err}")


def fetch_and_parse(
    url: str,
    query: str = "",
    *,
    session: Optional[requests.Session] = None,
) -> list[Article]:
    xml_text = fetch_rss(url, session=session)
    return parse_rss_xml(xml_text, query=query)

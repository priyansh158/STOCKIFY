"""Resolve Google News article wrapper URLs to publisher URLs.

Post-2024 Google News links no longer embed the destination in base64.
Resolution uses the public batchexecute `garturlreq` RPC (same approach
as community decoders / Apify actors).
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional
from urllib.parse import quote, urlparse

import requests

from gnews.rss import USER_AGENT

log = logging.getLogger("gnews.decode")

_BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
_SG_RE = re.compile(r'data-n-a-sg="([^"]+)"')
_TS_RE = re.compile(r'data-n-a-ts="([^"]+)"')
_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_OG_IMAGE_RE_ALT = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    re.IGNORECASE,
)


def extract_article_id(url: str) -> Optional[str]:
    """Pull the CBMi… id from a Google News article / rss / read URL."""
    try:
        path = urlparse(url).path.rstrip("/")
    except Exception:
        return None
    parts = path.split("/")
    if not parts:
        return None
    # …/articles/{id} or …/rss/articles/{id} or …/read/{id}
    for i, p in enumerate(parts):
        if p in ("articles", "read") and i + 1 < len(parts):
            return parts[i + 1].split("?")[0]
    return None


def _fetch_decode_params(
    article_id: str,
    session: requests.Session,
    timeout: float,
) -> Optional[tuple[str, str]]:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
    for prefix in ("articles", "rss/articles"):
        url = f"https://news.google.com/{prefix}/{article_id}"
        try:
            resp = session.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 429:
                return None  # signal rate limit to caller
            if resp.status_code >= 400:
                continue
            html = resp.text
            sg = _SG_RE.search(html)
            ts = _TS_RE.search(html)
            if sg and ts and ts.group(1).isdigit():
                return sg.group(1), ts.group(1)
        except requests.RequestException as e:
            log.debug("decode params fetch failed for %s: %s", prefix, e)
    return None


def _batchexecute_resolve(
    article_id: str,
    signature: str,
    timestamp: str,
    session: requests.Session,
    timeout: float,
    *,
    hl: str = "en-IN",
    gl: str = "IN",
    ceid: str = "IN:en",
) -> Optional[str]:
    """POST garturlreq and parse publisher URL from garturlres."""
    rpc_inner = json.dumps(
        [
            "garturlreq",
            [
                ["X", "X", ["X", "X"], None, None, 1, 1, ceid, None, 1, None, None, None, None, None, 0, 1],
                "X",
                "X",
                1,
                [1, 1, 1],
                1,
                1,
                None,
                0,
                0,
                None,
                0,
            ],
            article_id,
            int(timestamp),
            signature,
        ],
        separators=(",", ":"),
    )
    f_req = json.dumps([[["Fbv4je", rpc_inner, None, "generic"]]], separators=(",", ":"))
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Referer": "https://news.google.com/",
    }
    try:
        resp = session.post(
            _BATCH_URL,
            data=f"f.req={quote(f_req)}",
            headers=headers,
            timeout=timeout,
        )
        if resp.status_code == 429:
            return None
        resp.raise_for_status()
        body = resp.text
    except requests.RequestException as e:
        log.debug("batchexecute failed: %s", e)
        return None

    if body.startswith(")]}'"):
        body = body.split("\n", 1)[-1]
    body = body.lstrip()
    # Response often starts with a length prefix line
    lines = body.split("\n")
    if lines and lines[0].strip().isdigit():
        body = "\n".join(lines[1:])

    try:
        rows = json.loads(body)
        # rows[0][2] is a JSON string payload
        payload = json.loads(rows[0][2])
        if isinstance(payload, list) and len(payload) >= 2 and payload[0] == "garturlres":
            url = payload[1]
            if isinstance(url, str) and url.startswith("http"):
                return url
    except (json.JSONDecodeError, IndexError, TypeError, KeyError) as e:
        log.debug("batchexecute parse failed: %s", e)
    return None


def decode_google_news_url(
    url: str,
    *,
    session: Optional[requests.Session] = None,
    timeout: float = 15.0,
    max_retries: int = 3,
    interval: float = 0.4,
) -> tuple[Optional[str], Optional[str]]:
    """Return (loaded_url, error_message)."""
    # Already a publisher URL
    host = urlparse(url).hostname or ""
    if host and "news.google.com" not in host:
        return url, None

    article_id = extract_article_id(url)
    if not article_id:
        return None, "could not extract article id"

    sess = session or requests.Session()
    last_err = "decode failed"

    for attempt in range(max_retries):
        if attempt:
            time.sleep(min(2 ** attempt, 20) + interval)
        params = _fetch_decode_params(article_id, sess, timeout)
        if params is None:
            last_err = "rate limited or missing decode params"
            continue
        signature, timestamp = params
        resolved = _batchexecute_resolve(article_id, signature, timestamp, sess, timeout)
        if resolved:
            if interval:
                time.sleep(interval)
            return resolved, None
        last_err = "batchexecute returned no URL"

    return None, last_err


def fetch_og_image(
    url: str,
    *,
    session: Optional[requests.Session] = None,
    timeout: float = 12.0,
) -> Optional[str]:
    """Best-effort og:image from publisher page."""
    if not url or "news.google.com" in (urlparse(url).hostname or ""):
        return None
    sess = session or requests.Session()
    try:
        resp = sess.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
            timeout=timeout,
            allow_redirects=True,
        )
        if resp.status_code >= 400:
            return None
        html = resp.text[:200_000]
        m = _OG_IMAGE_RE.search(html) or _OG_IMAGE_RE_ALT.search(html)
        return m.group(1).strip() if m else None
    except requests.RequestException:
        return None


def enrich_article_details(
    article,
    *,
    session: Optional[requests.Session] = None,
    fetch_image: bool = True,
    interval: float = 0.4,
) -> None:
    """Mutate Article with loaded_url / image / decode_error."""
    loaded, err = decode_google_news_url(article.link, session=session, interval=interval)
    if loaded:
        article.loaded_url = loaded
        if fetch_image:
            article.image = fetch_og_image(loaded, session=session)
    else:
        article.decode_error = err

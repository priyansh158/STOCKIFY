# Google News scraper (gnews/)

STOCKIFY's own Apify-style Google News client. Backed by **public Google News RSS**
(not an official API), with day-window slicing past the ~100-item cap and optional
publisher URL decode via Google's `batchexecute` RPC.

## Quick start

From the STOCKIFY repo root:

```bash
# Fast metadata only (recommended for polling)
python -m gnews --q "RBI repo rate" --when 1d --max 20

# Resolve publisher URLs (+ og:image)
python -m gnews --q "USD INR" --when 1d --max 5 --decode

# Pipeline-shaped JSON (for geo analysis stage)
python -m gnews --q "TCS" --when 1d --max 10 --pipeline

# Named topic feed
python -m gnews --topic BUSINESS --max 30
```

## Python API

```python
from gnews import GoogleNewsClient

client = GoogleNewsClient(language="en", region="IN")

result = client.search(
    "RBI OR Fed",
    open_ended="1d",          # 1h | 1d | 7d | 30d | 1y
    max_items=50,
    fetch_article_details=False,
)

# Stable contract for analysis_pipeline / geo stage
articles = result.to_pipeline_articles()
# [{title, source, content, url, published_at}, ...]

# Multi-query merge (macro + holdings monitor)
merged = client.search_many(
    ["RBI repo", "crude oil Brent", "TCS OR Infosys"],
    open_ended="1h",
    max_items_per_query=20,
)
```

## Output fields

| Field | Meaning |
|-------|---------|
| `title` | Headline |
| `link` / `rssLink` | Google News wrapper URL |
| `guid` | Stable id for dedupe |
| `source` / `sourceUrl` | Publisher name / homepage |
| `publishedAt` | ISO-8601 UTC when parseable |
| `loadedUrl` | Decoded publisher URL (`--decode`) |
| `image` | `og:image` when decoded |
| `description` | RSS snippet |

## Limitations (read before production use)

- **Unofficial** — Google can change RSS or decode RPC without notice.
- **~100 items per feed request** — deeper history uses day-by-day `after:`/`before:` slicing when `max_items > 100`.
- **Metadata only** from RSS — no full article body.
- **URL decode** can hit HTTP 429; use backoff and prefer decode only for alert candidates.
- **Rate limits** — keep poll intervals polite (≥5–10 min for monitors).
- Personal monitoring use; respect Google / publisher ToS.

## Tests

```bash
cd /path/to/STOCKIFY
python -m unittest tests.gnews.test_gnews -v
```

## Later integration

1. Always-on worker calls `search_many` for macro keywords + Kite holdings.
2. Gemini classifies disruptiveness.
3. Email + STOCKIFY alert store.
4. `stream_geo` swaps NewsAPI for `GoogleNewsClient` via `to_pipeline_articles()`.

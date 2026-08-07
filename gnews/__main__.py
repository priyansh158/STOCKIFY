"""CLI: python -m gnews --q "RBI repo" --when 1d --max 20"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from gnews.client import GoogleNewsClient


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m gnews",
        description="Search Google News (RSS) — India edition by default.",
    )
    p.add_argument("--q", "--query", dest="query", default="", help="Search query")
    p.add_argument("--topic", default="", help="Named topic feed, e.g. BUSINESS")
    p.add_argument("--when", dest="open_ended", default=None, help="1h, 1d, 7d, 30d, 1y")
    p.add_argument("--from", dest="date_from", default=None, help="YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", default=None, help="YYYY-MM-DD")
    p.add_argument("--max", dest="max_items", type=int, default=50)
    p.add_argument("--lang", default="en")
    p.add_argument("--region", default="IN")
    p.add_argument("--decode", action="store_true", help="Resolve publisher URLs")
    p.add_argument("--no-image", action="store_true", help="Skip og:image when decoding")
    p.add_argument("--pipeline", action="store_true", help="Emit pipeline-shaped JSON")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if not args.query and not args.topic:
        p.error("provide --q or --topic")

    client = GoogleNewsClient(language=args.lang, region=args.region)

    if args.topic:
        result = client.topic(
            args.topic,
            max_items=args.max_items,
            fetch_article_details=args.decode,
        )
    else:
        result = client.search(
            args.query,
            max_items=args.max_items,
            open_ended=args.open_ended,
            date_from=args.date_from,
            date_to=args.date_to,
            fetch_article_details=args.decode,
            fetch_image=not args.no_image,
        )

    if args.pipeline:
        payload = {
            "articles": result.to_pipeline_articles(),
            "uniqueCount": result.unique_count,
            "errors": result.errors,
        }
    else:
        payload = result.to_dict()

    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if not result.errors or result.articles else 1


if __name__ == "__main__":
    raise SystemExit(main())

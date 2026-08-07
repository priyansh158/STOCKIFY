"""CLI: python -m geo_monitor [--once] [--dry-run]"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from geo_monitor.worker import load_env, run_forever, run_once


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="STOCKIFY always-on geo news monitor")
    p.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify but do not persist alerts or email",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_env()

    if args.once or args.dry_run:
        stats = run_once(dry_run=args.dry_run)
        json.dump(stats, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0 if not stats.get("errors") else 1

    run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

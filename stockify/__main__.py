"""CLI: python -m stockify <command> [...]

Subcommands are registered now so the surface is stable; each one is
implemented by the milestone named in its help text.
"""
from __future__ import annotations

import argparse
import json
import sys

from stockify import log

# command -> (milestone, help)
_STUBS = {
    "refdata": ("M1", "sync instruments and reference data"),
    "nse": ("M1", "fetch NSE public end-of-day files"),
    "bars": ("M1", "fetch and cache Kite historical candles"),
    "compact": ("M1", "verify and archive landing tick files"),
    "pack": ("M1", "build event packs"),
    "data": ("M1", "data quality report"),
    "oracle": ("M2", "run the Python oracle backtester"),
    "golden": ("M2", "run golden scenarios"),
    "compare-artifacts": ("M3", "compare two runs' deterministic artifacts"),
    "run": ("M3", "run a backtest or replay"),
    "margin": ("M4", "margin calibration tools"),
    "parity": ("M5", "nightly journal parity"),
    "compare": ("M6", "compare runs"),
    "export": ("M6", "export a run"),
    "sweep": ("M6", "parameter sweep"),
    "walkforward": ("M6", "walk-forward analysis"),
    "sensitivity": ("M6", "cost and fill sensitivity"),
    "tools": ("M6", "maintenance tools"),
}


def _cmd_daemon(args) -> int:
    from stockify import daemon
    daemon.run_forever(once=args.once)
    return 0


def _cmd_config(args) -> int:
    from stockify.config import loader
    loader.load_all()
    print("config OK")
    return 0


def _cmd_engine(args) -> int:
    from stockify import engine
    try:
        json.dump(engine.build_info(), sys.stdout, indent=2, sort_keys=True)
    except engine.EngineUnavailable as e:
        print(e, file=sys.stderr)
        return 1
    sys.stdout.write("\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="stockify", description="STOCKIFY: NSE simulation lab")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("daemon", help="market daemon (recorder + paper sessions)")
    d.add_argument("--once", action="store_true", help="log one idle cycle and exit")
    d.set_defaults(func=_cmd_daemon)

    sub.add_parser("config", help="validate stockify/config/").set_defaults(func=_cmd_config)
    sub.add_parser("engine", help="print the engine build info").set_defaults(func=_cmd_engine)

    for name, (milestone, text) in _STUBS.items():
        s = sub.add_parser(name, help=f"{text} ({milestone}, not implemented yet)")
        s.set_defaults(func=None, milestone=milestone)

    args = p.parse_args(argv)
    log.setup(args.verbose)
    if args.func is None:
        print(f"stockify {args.command}: not implemented yet (planned for {args.milestone})",
              file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

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
    "nse": ("M1", "fetch NSE public end-of-day files"),
    "bars": ("M1", "fetch and cache Kite historical candles"),
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
    if args.recorder_only:
        if args.ticker == "live":
            _load_env()
        return daemon.run_recorder(ticker_spec=args.ticker, faults=args.inject)
    if args.once:
        daemon.run_forever(once=True)
        return 0
    _load_env()
    return daemon.run_daemon()


def _cmd_config(args) -> int:
    from stockify.config import loader
    loader.load_all()
    print("config OK")
    return 0


def _cmd_refdata_sync(args) -> int:
    from datetime import date
    from pathlib import Path

    from stockify import timeutil
    from stockify.data import instruments

    day = date.fromisoformat(args.date) if args.date else timeutil.ist_today()
    if args.dir:
        folder = Path(args.dir)
        dumps = {ex: (folder / f"{ex}.csv").read_text(encoding="utf-8")
                 for ex in instruments.DUMP_EXCHANGES if (folder / f"{ex}.csv").exists()}
        if not dumps:
            print(f"no NSE.csv / NFO.csv in {folder}", file=sys.stderr)
            return 1
    else:
        dumps = instruments.download_dumps()
    stats = instruments.sync(day, dumps)
    if not args.dir:
        # Compatibility: search still reads the old instruments.sqlite cache (RECON M1.0).
        import kite_client
        kite_client.refresh_instruments(force=True)
    json.dump(stats.__dict__, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def _load_env() -> None:
    from dotenv import load_dotenv

    import stockify_paths
    load_dotenv(dotenv_path=stockify_paths.env_file(), override=True)


def _cmd_kite_check(args) -> int:
    _load_env()
    from stockify import kitecheck

    checks = kitecheck.run()
    for c in checks:
        print(f"{'PASS' if c.ok else 'FAIL'}  {c.name:<10} {c.detail}")
    ok = kitecheck.passed(checks)
    print("Kite market data: " + ("available" if ok else "NOT available (see FAIL lines)"))
    return 0 if ok else 1


def _cmd_calendar(args) -> int:
    from datetime import date

    from stockify import timeutil
    from stockify.data import calendar as cal

    start = date.fromisoformat(args.start) if args.start else timeutil.ist_today()
    local = cal.owner_tz()

    def loc(t_ns: int, fmt: str) -> str:
        return timeutil.ns_to_ist(t_ns).astimezone(local).strftime(fmt)

    def ist(t_ns: int) -> str:
        return timeutil.ns_to_ist(t_ns).strftime("%H:%M")

    zone = loc(timeutil.ist_ns(start, "09:00"), "%Z")
    covered = cal.holidays_cover_until()
    if (start - covered).days + args.days > 1:
        print(f"WARNING: the NSE holiday list ends {covered}; later weekdays are shown as open "
              "until NSE publishes the next year's list (add it to calendar/nse_holidays.csv).")
    print(f"NSE F&O sessions from {start} (IST trade dates; your time zone {local.key}, {zone})")
    print(f"{'IST date':<15} {'session IST':<13} {'session, your time':<30} {'Kite login window, your time'}")
    for row in cal.upcoming(start, args.days):
        head = f"{row['date']:%Y-%m-%d %a}"
        if not row["open"]:
            print(f"{head:<15} closed: {row['reason']}")
            continue
        session = f"{loc(row['session_start_ns'], '%a %d %b %H:%M')} -> {loc(row['session_end_ns'], '%a %H:%M')}"
        login = f"{loc(row['login_from_ns'], '%a %d %b %H:%M')} - {loc(row['login_until_ns'], '%H:%M')}"
        print(f"{head:<15} {ist(row['session_start_ns'])}-{ist(row['session_end_ns']):<7} {session:<30} {login}")
    return 0


def _cmd_compact(args) -> int:
    from datetime import date

    from stockify.data import compact

    day = date.fromisoformat(args.date)
    res = compact.compact(day, force=args.force)
    if res.status == "verified" and not args.keep_landing:
        # Same policy as the nightly job (D-38): delete only what retention allows.
        from stockify import timeutil
        deleted = compact.apply_retention(timeutil.ist_today())
        if deleted:
            print(f"landing files deleted (verified archives kept): {', '.join(deleted)}", file=sys.stderr)
    json.dump(res.__dict__, sys.stdout, indent=2)
    sys.stdout.write("\n")
    for warning in compact.disk_warnings():
        print(f"WARNING: {warning}", file=sys.stderr)
    return 0 if res.status in ("verified", "no_landing") else 1


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

    d = sub.add_parser("daemon", help="market daemon: record each session, then hibernate (D-32)")
    d.add_argument("--once", action="store_true", help="log one idle cycle and exit")
    d.add_argument("--recorder-only", action="store_true", help="record today's ticks (M1.5)")
    d.add_argument("--ticker", default="live",
                   help="live, or replay:YYYY-MM-DD:<Nx|max> to replay a recorded day")
    d.add_argument("--inject", action="append", default=[],
                   help="replay fault, e.g. disconnect@10:30:00+90s, token_expiry@11:00:00, kill@13:00:00")
    d.set_defaults(func=_cmd_daemon)

    sub.add_parser("config", help="validate stockify/config/").set_defaults(func=_cmd_config)
    sub.add_parser("engine", help="print the engine build info").set_defaults(func=_cmd_engine)

    r = sub.add_parser("refdata", help="reference data (instruments with history)")
    rsub = r.add_subparsers(dest="refdata_command", required=True)
    rs = rsub.add_parser("sync", help="fold today's Kite instrument dumps into refdata.sqlite")
    rs.add_argument("--date", help="trade date YYYY-MM-DD (default: today in IST)")
    rs.add_argument("--dir", help="read NSE.csv / NFO.csv from this folder instead of downloading")
    rs.set_defaults(func=_cmd_refdata_sync)

    cp = sub.add_parser("compact", help="archive a finished day's landing file to verified Parquet (M1.7)")
    cp.add_argument("--date", required=True, help="IST trade date YYYY-MM-DD")
    cp.add_argument("--force", action="store_true", help="rebuild even if the day is already verified")
    cp.add_argument("--keep-landing", action="store_true",
                    help="do not apply the retention policy (landing files stay)")
    cp.set_defaults(func=_cmd_compact)

    c = sub.add_parser("calendar", help="upcoming NSE sessions and Kite login windows in your time zone")
    c.add_argument("--start", help="first IST trade date YYYY-MM-DD (default: today in IST)")
    c.add_argument("--days", type=int, default=14, help="number of calendar days (default 14)")
    c.set_defaults(func=_cmd_calendar)

    k = sub.add_parser("kite", help="Kite Connect checks")
    ksub = k.add_subparsers(dest="kite_command", required=True)
    ksub.add_parser("check", help="confirm the plan gives REST, historical and WebSocket data"
                    ).set_defaults(func=_cmd_kite_check)

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

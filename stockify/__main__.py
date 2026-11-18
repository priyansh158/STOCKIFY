"""CLI: python -m stockify <command> [...]

Subcommands are registered now so the surface is stable; each one is
implemented by the milestone named in its help text.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import kite_client  # noqa: F401  — first: patches the Kite SDK's client class to paper only (LCB 5.7)
from stockify import log

# command -> (milestone, help)
_STUBS = {
}


def _parse_sessions(text: str) -> list[str] | None:
    """`--sessions`: `all` (every armed, running or paused session) is None, `none` is [], else the ids."""
    text = text.strip()
    if text == "all":
        return None
    if text == "none":
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def _redirect_state_for_replay(directory: str) -> str | None:
    """`--replay-sandbox DIR`: run with DIR as the local app-data folder, so a replay's sessions, journals and store
    live there and never in the real lab. Refused when DIR is (or lies inside) the real one. Returns a message on
    refusal."""
    from pathlib import Path
    from stockify.daemon import market_day
    target = Path(directory).resolve()
    for real_state in market_day.real_state_dirs():                # every candidate real lab folder
        real_app = real_state.resolve().parent                       # the real STOCKIFY folder in local app-data
        real_root = real_app.parent                                  # the real local app-data folder
        if target == real_root or real_app == target or real_app in target.parents or target in real_app.parents:
            return f"--replay-sandbox {directory}: that is the real lab's folder; pick an empty directory elsewhere"
    real_lab = next((d / "lab" for d in market_day.real_state_dirs() if (d / "lab").is_dir()), None)
    target.mkdir(parents=True, exist_ok=True)
    os.environ["LOCALAPPDATA"] = str(target)
    if real_lab is not None:
        # The recording is replayed from the real lab, read-only; the sandbox only gets its own copy of the instrument
        # reference data. Every output stays in the sandbox.
        from stockify import paths
        from stockify.daemon import replay_ticker
        replay_ticker.set_source_lab(real_lab)
        replay_ticker.seed_refdata_from(real_lab, paths.refdata_db())
    return None


def _cmd_daemon(args) -> int:
    from stockify import daemon
    from stockify.daemon import exitcodes, singleton
    if args.inject and args.ticker == "live":
        print("--inject needs a replay ticker (--ticker replay:YYYY-MM-DD:<Nx|max>): faults are never injected into "
              "the live ticker, so none would be armed", file=sys.stderr)
        return 2
    if args.once and not args.recorder_only:
        daemon.run_forever(once=True)
        return 0
    if args.replay_sandbox is not None:
        problem = _redirect_state_for_replay(args.replay_sandbox)
        if problem:
            print(problem, file=sys.stderr)
            return 2
    try:
        lock = singleton.acquire()               # held until the process ends; the OS drops it however it ends
    except singleton.AlreadyRunning as e:
        print(e, file=sys.stderr)
        return exitcodes.ALREADY_RUNNING
    except singleton.LockUnavailable as e:
        print(e, file=sys.stderr)
        return exitcodes.LOCK_FAILED
    sessions = [] if args.recorder_only else _parse_sessions(args.sessions)
    if args.recorder_only or args.ticker != "live":
        if args.ticker == "live":
            _load_env()
        from stockify.daemon import market_day
        try:
            return daemon.run_market_day(ticker_spec=args.ticker, faults=args.inject, session_ids=sessions)
        except market_day.ReplayRefused as e:
            print(e, file=sys.stderr)
            return 2
    _load_env()
    return daemon.run_daemon(session_ids=sessions)


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


def _cmd_calendar_audit(args) -> int:
    from datetime import date

    from stockify.tools import holiday_audit

    start, end = date.fromisoformat(args.date_from), date.fromisoformat(args.date_to)
    if end < start:
        print(f"--to {end} is before --from {start}", file=sys.stderr)
        return 1
    found = holiday_audit.audit(start, end, args.segment)
    print(holiday_audit.report(found))
    if found.not_checked:
        print("calendar audit: " + ", ".join(name for name, _ in found.not_checked) + " not checked; run it again "
              "while no recorder or daemon is writing", file=sys.stderr)
        return 1
    return 0


def _cmd_data_report(args) -> int:
    from datetime import date

    from stockify.data import quality

    try:
        result = quality.report(date.fromisoformat(args.date))
    except quality.QualityError as e:
        print(e, file=sys.stderr)
        return 1
    print(quality.summary_markdown(result))
    print(f"(rows written to data_quality; summary saved to {result['summary_path']})", file=sys.stderr)
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


def _nse_datasets(text: str) -> tuple[str, ...]:
    from stockify.data import nse_files
    names = tuple(x.strip() for x in text.split(",") if x.strip())
    bad = [x for x in names if x not in nse_files.DATASETS]
    if bad:
        raise SystemExit(f"unknown dataset(s) {', '.join(bad)}; choose from {', '.join(nse_files.DATASETS)}")
    return names


def _print_nse(res) -> None:  # noqa: ANN001
    print(f"{res.dataset:<10} {res.trade_date}  {res.outcome:<8} {res.detail}")


def _cmd_nse_fetch(args) -> int:
    from datetime import date

    from stockify import timeutil
    from stockify.data import nse_files

    day = date.fromisoformat(args.date) if args.date else timeutil.ist_today()
    try:
        results = nse_files.fetch(day, _nse_datasets(args.datasets), allow_unverified=args.allow_unverified,
                                  force=args.force)
    except nse_files.NseError as e:
        print(e, file=sys.stderr)
        return 1
    for res in results:
        _print_nse(res)
    for s in nse_files.settle(day):
        print(f"settlement {s['underlying']:<12} {s['expiry']}  {s['status']}"
              + (f" fsp={s['fsp_paise'] / 100:.2f} ({s['source']}, cross-check {s['cross_check']})"
                 if s["status"] == "ok" else ""))
    return 0 if all(r.outcome in ("ok", "skipped", "missing") and r.parsed_ok is not False
                    for r in results) else 1


def _cmd_nse_backfill(args) -> int:
    from datetime import date, timedelta

    from stockify import timeutil
    from stockify.data import nse_files

    end = date.fromisoformat(args.to) if args.to else timeutil.ist_today() - timedelta(days=1)
    try:
        totals = nse_files.backfill(date.fromisoformat(args.from_), end, _nse_datasets(args.datasets),
                                    allow_unverified=args.allow_unverified,
                                    progress=_print_nse if args.verbose_rows else None)
    except nse_files.NseError as e:
        print(e, file=sys.stderr)
        return 1
    print(json.dumps(totals, sort_keys=True))
    return 0 if not totals.get("error") else 1


def _iso_day(text: str):  # noqa: ANN202
    from datetime import date
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a date YYYY-MM-DD: {text!r}") from None


def _positive_int(text: str) -> int:
    try:
        n = int(text)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError(f"not a whole number of 1 or more: {text!r}")
    return n


def _fundamentals_session_guard() -> bool:
    """True (with a plain message) while the recorder's session is in progress: the screener's NSE reads, XBRL
    parsing and backtests wait for the close, as the daemon's job does (HELD_DURING_SESSION)."""
    from stockify import timeutil
    from stockify.daemon import scheduler
    if scheduler.session_in_progress(timeutil.now_ns()):
        print("The recorder's session is in progress (subscribe to unsubscribe in schedule.json, IST). Run this "
              "after the close.", file=sys.stderr)
        return True
    return False


_FUNDAMENTALS_BUSY = ("Another fundamentals run is in progress (the daemon's nightly job, a refresh or a backfill). "
                      "Run this again when it has finished.")


def _cmd_fundamentals_backfill(args) -> int:
    from stockify.fundamentals import job

    if _fundamentals_session_guard():
        return 1
    try:
        res = job.backfill(since=args.from_ or job.filings_from(), limit=args.limit)
    except job.Busy:
        print(_FUNDAMENTALS_BUSY, file=sys.stderr)
        return 1
    print(json.dumps(res, sort_keys=True, default=str))
    return 0 if res.get("stopped") is None else 1


def _cmd_fundamentals_refresh(args) -> int:
    from stockify import timeutil
    from stockify.fundamentals import job

    if _fundamentals_session_guard():
        return 1
    try:
        res = job.run(timeutil.ist_today())
    except job.Busy:
        print(_FUNDAMENTALS_BUSY, file=sys.stderr)
        return 1
    print(json.dumps(res, sort_keys=True, default=str))
    return 0 if res.get("complete") else 1


def _cmd_fundamentals_test_record(args) -> int:
    if _fundamentals_session_guard():
        return 1
    _load_env()
    import kite_client
    from stockify.fundamentals import test_record

    if not kite_client.is_authenticated():
        print("The test record needs Kite's daily prices, and Kite is not logged in. Log in to Kite in the app, "
              "then run this again.", file=sys.stderr)
        return 1
    done = [0]

    def fetch(iid, interval, start, end, **kw):  # noqa: ANN001, ANN003, ANN202
        stats = test_record.bars_history.fetch(iid, interval, start, end, **kw)
        done[0] += 1
        print(f"bars {done[0]}: instrument {iid} {stats.outcome}", flush=True)
        return stats
    print("Fetching each ranked stock's daily prices from Kite, then running the walk-forward ...", flush=True)
    try:
        res = test_record.run(fetch=fetch)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"walk-forward {res['job_id']} ({res['family_id']}): {res['start']} to {res['end']}, "
          f"{len(res['symbols'])} stocks, {len(res['missing'])} without prices; benchmark {res['benchmark']['label']}")
    return 0


def _cmd_nse_margin_files(args) -> int:
    from datetime import date

    from stockify.data import nse_margin_files as mf

    if args.elm_window:
        results = mf.elm_window()
        for r in results:
            if r.stored or r.errors or r.rejected:
                _print_margin(r)
        have = sum(1 for r in results if mf.elm_name(r.day) in r.stored + r.unchanged)
        print(f"ELM window: {have} of {len(results)} trading days archived "
              f"({sum(1 for r in results if r.stored)} new)")
        return 0 if not any(r.errors or r.rejected for r in results) else 1
    if args.backfill:
        results = mf.backfill(date.fromisoformat(args.backfill[0]), date.fromisoformat(args.backfill[1]))
    elif args.date:
        results = [mf.fetch_day(date.fromisoformat(args.date))]
    else:
        print("give --date YYYY-MM-DD, --backfill FROM TO or --elm-window", file=sys.stderr)
        return 2
    for r in results:
        _print_margin(r)
    return 0 if not any(r.errors or r.rejected for r in results) else 1


def _print_margin(r) -> None:  # noqa: ANN001
    print(f"{r.day}  stored {len(r.stored)}  already {len(r.unchanged)}  missing {len(r.missing)}  "
          f"rejected {len(r.rejected)}  errors {len(r.errors)}")
    for label, names in (("stored", r.stored), ("missing", r.missing), ("rejected", r.rejected),
                         ("errors", r.errors)):
        for name in names:
            print(f"    {label:<8} {name}")


def _cmd_bars_fetch(args) -> int:
    from datetime import date

    from stockify.data import history, instruments

    _load_env()
    start, end = date.fromisoformat(args.from_), date.fromisoformat(args.to)
    code = 0
    for sym in (s.strip() for s in args.symbols.split(",") if s.strip()):
        try:
            if args.continuous:
                stats = history.fetch_continuous(sym, start, end)
            else:
                exchange, _, name = sym.rpartition(":")
                iid = instruments.find(exchange or "NSE", name)
                if iid is None:
                    print(f"{sym}: not in refdata (run `refdata sync`)", file=sys.stderr)
                    code = 1
                    continue
                stats = history.fetch(iid, args.interval, start, end)
        except history.HistoryError as e:
            print(f"{sym}: {e}", file=sys.stderr)
            code = 1
            continue
        extra = f", adjustment {stats.adjustment}" if stats.adjustment else ""
        print(f"{sym:<20} {stats.interval:<9} {stats.outcome:<9} {stats.rows} bars in {stats.chunks} chunks, "
              f"{stats.requests} requests{extra} {stats.detail}")
    return code


def _cmd_bars_sweep(args) -> int:
    from stockify.data import history

    _load_env()
    out = history.sweep()
    flagged = [s for s in out if s.adjustment]
    print(f"checked {len(out)} cached live instruments; {len(flagged)} adjusted")
    for s in flagged:
        print(f"  instrument {s.instrument_id}: {s.adjustment}")
    return 0


def _cmd_pack_build(args) -> int:
    from pathlib import Path

    from stockify.data import packs

    try:
        sha = packs.build(json.loads(Path(args.spec).read_text(encoding="utf-8")))
    except packs.PackError as e:
        print(e, file=sys.stderr)
        return 1
    print(sha)
    return 0


def _cmd_pack_show(args) -> int:
    from stockify import paths
    from stockify.data import packs

    hits = sorted(p.stem for p in paths.packs_dir().glob(f"{args.sha}*.evt"))
    if len(hits) != 1:
        print(f"{len(hits)} packs match {args.sha!r}", file=sys.stderr)
        return 1
    sha = hits[0]
    print(json.dumps(packs.manifest(sha), indent=2, sort_keys=True))
    _, records = packs.read(packs.pack_path(sha))
    for rec in records[:args.records]:
        print(json.dumps(rec, sort_keys=True))
    return 0


def _with_unlock(config, reason: str | None):  # noqa: ANN001, ANN202
    """`--unlock-holdout "<reason>"`: the config (a path or a dict) with `unlock_holdout` set to the reason. Without
    the flag the config goes on as it is (a reason written in the file still counts)."""
    import json
    from pathlib import Path

    if reason is None:
        return config
    if not isinstance(config, dict):
        try:
            config = json.loads(Path(config).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            from stockify import runconfig
            raise runconfig.RunConfigError(f"run.json could not be read: {e}") from e
    if not isinstance(config, dict):
        from stockify import runconfig
        raise runconfig.RunConfigError("run.json must be a JSON object")
    return {**config, "unlock_holdout": reason}


_UNLOCK_HELP = ("a real written reason (3+ characters) to run over the locked holdout (research.json holdout_from); "
                "every unlock is logged and counted on the family's report")


def _cmd_oracle_run(args) -> int:
    from stockify import bundle, runconfig
    from stockify.oracle import run

    try:
        res = run(_with_unlock(args.config, args.unlock_holdout), run_id=args.run_id)
    except (runconfig.RunConfigError, bundle.BundleError) as e:
        print(e, file=sys.stderr)
        return 1
    m = res.metrics
    print(res.folder)
    print(_summary_line(m, res.labels), file=sys.stderr)
    return 0


def _summary_line(m: dict, labels) -> str:  # noqa: ANN001
    """The one-line result both `oracle run` and `run` print: NAV, trades, rejected orders, costs, labels."""
    rejects = sum((m.get("rejects") or {}).values())
    return (f"NAV {m.get('nav_start_paise', 0) / 100:,.2f} -> {m.get('nav_end_paise', 0) / 100:,.2f}; "
            f"trades {m.get('trades')}; rejects {rejects}; costs {m.get('costs_total_paise', 0) / 100:,.2f}; "
            f"labels {', '.join(labels)}")


def _objectives() -> tuple[str, ...]:
    from stockify.research import registry
    return tuple(registry.OBJECTIVES)


def _run_roles() -> tuple[str, ...]:
    from stockify.research import registry
    return tuple(r for r in registry.ROLES if r != "rerun")      # only runs.rerun sets "rerun"


def _cmd_run(args) -> int:
    """Exit 0 done, 1 run failed (or re-run not reproduced), 2 bad config / usage / a --run-id not queued."""
    import json

    from stockify import paths, runconfig, runs, store

    if args.rerun:
        try:
            paths.run_path(args.rerun)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
        try:
            new_id, same = runs.rerun(args.rerun, unlock_holdout=args.unlock_holdout)
        except runconfig.RunConfigError as e:
            print(e, file=sys.stderr)
            return 2
        except runs.RunFailed as e:
            print(e, file=sys.stderr)
            return 1
        except runs.RunNotQueued as e:
            print(e, file=sys.stderr)
            return 2
        print(paths.run_path(new_id))
        if same:
            print(f"determinism.json reproduced byte for byte ({args.rerun} -> {new_id})", file=sys.stderr)
            return 0
        changes = runs.column_changes(args.rerun, new_id)    # a folder saved before D-64 (final I-3) and/or CH 6.2
        if changes:
            print(f"reproduced ({'; '.join(changes)}) ({args.rerun} -> {new_id}): every other deterministic "
                  "file is byte for byte the same, and only the columns and files named above were added", file=sys.stderr)
            return 0
        print(f"not reproduced ({args.rerun} -> {new_id})", file=sys.stderr)
        for name in runs.determinism_diff(args.rerun, new_id):
            print(f"differs: {name}", file=sys.stderr)
        return 1
    if args.config is None and args.run_id is None:
        print("stockify run: give --config or --rerun", file=sys.stderr)
        return 2
    if args.unlock_holdout is not None and args.run_id is not None:
        print("stockify run: --unlock-holdout goes with --config (a queued run keeps the reason it was queued with)",
              file=sys.stderr)
        return 2
    try:
        config = _with_unlock(args.config, args.unlock_holdout)
        run_id = runs.launch(config, run_id=args.run_id, impl=args.impl, report=not args.no_report,
                             role=args.role, job_id=args.job_id)
    except (runconfig.RunConfigError, runs.RunNotQueued) as e:
        print(e, file=sys.stderr)
        return 2
    except runs.RunFailed as e:
        print(e, file=sys.stderr)
        return 1
    row = store.get_run(run_id)
    print(row["folder"])
    m = json.loads(row["metrics_json"]) if row.get("metrics_json") else {}
    labels = json.loads(row["labels_json"]) if row.get("labels_json") else []
    print(_summary_line(m, labels), file=sys.stderr)
    return 0


def _cmd_margin_calibrate(args) -> int:
    from stockify.fno import calibrate
    if args.refit:                                      # IWB 4.5: offline, from stored samples; never calls Kite
        if not args.date:
            print("--refit needs --date YYYY-MM-DD (the IST trade date of the stored calibration)", file=sys.stderr)
            return 2
        try:
            report, params = calibrate.refit(args.underlying, args.date)
        except calibrate.CalibrationError as e:
            print(e, file=sys.stderr)
            return 1
        print(report)
        print(f"refitted row replaces {args.date}'s pending row in {params} (spanlite_pending; not used until you move "
              "it into spanlite)", file=sys.stderr)
        return 0
    import kite_client
    if not kite_client.is_enabled():
        print("Kite is not set up here (no API key, or kiteconnect is not installed).", file=sys.stderr)
        return 2
    try:
        client = kite_client.get_client()
    except kite_client.KiteAuthError:
        print("Log in to Kite first (the token is missing or from before today's 06:00 IST).", file=sys.stderr)
        return 2
    try:
        report, params = calibrate.calibrate(client, args.underlying, args.samples, seed=args.seed,
                                             skip_expiring=args.skip_expiring)
    except calibrate.CalibrationError as e:
        print(e, file=sys.stderr)
        return 1
    print(report)
    print(f"fitted row added to {params} under spanlite_pending (not used until you move it into spanlite)",
          file=sys.stderr)
    return 0


def _cmd_margin_span_validate(args) -> int:
    """MEZ 4.8: the exact SPAN calculator and the ELM file against the stored Kite answers of one day (offline)."""
    from datetime import date
    from stockify import paths, store, timeutil
    from stockify.fno import span_validate as sv
    try:
        day = date.fromisoformat(args.date)
    except ValueError:
        print(f"--date must be YYYY-MM-DD, got {args.date!r}", file=sys.stderr)
        return 2
    underlyings = args.underlying
    if not underlyings:
        with sv.read_only():
            underlyings = sorted({r["underlying"] for r in store.margin_calibrations()
                                  if timeutil.ns_to_ist(r["wall_ns"]).date() == day})
    rep = sv.validate(day, underlyings)
    if not rep.samples:
        print(f"no stored calibration samples on {day}", file=sys.stderr)
        return 1
    text = sv.report_text(rep)
    out_dir = paths.lab_dir() / "margin_calibration"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / f"span-validate-{day.isoformat()}.md"
    report.write_text(text, encoding="utf-8")
    _print_console(text)
    print(report, file=sys.stderr)
    return 0 if rep.gate()["passed"] else 1             # the report is written either way; 1 = the gate failed


def _print_console(text: str) -> None:
    """Print, with plain ASCII for the symbols a cp1252 console cannot show (₹ -> Rs)."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except UnicodeEncodeError:
        text = (text.replace("₹", "Rs ").replace("—", "-").replace("×", "x").replace("−", "-")
                .replace("·", "*").replace("≤", "<="))
        text = text.encode(encoding, errors="replace").decode(encoding)
    print(text)


def _cmd_research_families(args) -> int:
    """M6.4: the trial families (a table), with `--name` to name one and `--backfill` to give old runs and sessions a family."""
    from stockify import store
    from stockify.research import registry, report
    if args.dry_run and not args.backfill:
        print("--dry-run only goes with --backfill", file=sys.stderr)
        return 2
    if args.name:
        family_id, description = args.name
        if store.get_family(family_id) is None:
            print(f"no such family: {family_id!r} (`research families` lists them)", file=sys.stderr)
            return 1
        store.name_family(family_id, description)
    if args.backfill:
        n = registry.backfill(dry_run=args.dry_run)
        ns = registry.backfill_sessions(dry_run=args.dry_run)
        print(f"would give {n} run(s) and {ns} session(s) a family (dry run, nothing written)" if args.dry_run
              else f"gave {n} run(s) and {ns} session(s) a family")
    fams = report.list_families()
    if not fams:
        print("no families yet")
        return 0
    _print_console(report.families_table(fams))
    return 0


def _cmd_research_show(args) -> int:
    """M6.4/M6.8: the lines the pages show for a run or a family, in plain words."""
    from stockify import store
    from stockify.research import report
    if store.get_run(args.target) is not None:
        _print_console(report.run_text(report.run_research(args.target)))
        return 0
    if store.get_family(args.target) is not None:
        _print_console(report.family_text(report.family_report(args.target)))
        return 0
    print(f"{args.target!r} is neither a run id nor a family id", file=sys.stderr)
    return 2


def _cmd_research_report(args) -> int:
    """M6.12 (WF 6.7): re-render research/<job_id>/report.md of a finished walk-forward job through wf_report.render (its
    only writer) and print the path. Exit 0 written, 1 an internal failure (the job's result file is unreadable or the
    write failed), 2 an unknown job, a job that is not `done` (the status is named) or a bad --sensitivity-run id."""
    from stockify.research import wf_report
    try:
        dropping = args.sensitivity_run is None and wf_report.has_sensitivity_table(args.walkforward)
        path = wf_report.render(args.walkforward, sensitivity_run=args.sensitivity_run)
    except wf_report.WfReportError as e:
        print(e, file=sys.stderr)
        return 1 if e.kind == "missing" else 2
    except Exception as e:  # noqa: BLE001
        print(f"the report could not be written: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if dropping:
        print("note: the cost table of the earlier report.md is dropped, because no --sensitivity-run was given; "
              "run it again with --sensitivity-run RUN_ID to keep the table", file=sys.stderr)
    print(path)
    return 0


def _cmd_sensitivity(args) -> int:
    """M6.7: re-run a finished run with costs, extra slippage ticks, latency and other fill models, one change at a
    time. Exit 0 done (robust, fragile or already losing), 1 when a variant failed or the verdict is inconclusive, 2 for an unknown or unfinished run or a locked holdout."""
    from stockify import runconfig
    from stockify.research import sensitivity
    try:
        res = sensitivity.run_suite(args.run, workers=args.workers, unlock_reason=args.unlock_holdout)
    except sensitivity.SensitivityRunError as e:                  # the job exists and is marked failed
        print(f"the sensitivity job failed: {e}", file=sys.stderr)
        return 1
    except (sensitivity.SensitivityError, runconfig.RunConfigError, ValueError) as e:
        print(e, file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 — a run or the pool broke: the job is already marked failed
        print(f"the sensitivity job failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    _print_console(sensitivity.format_table(res))
    return 1 if res.verdict == "inconclusive" or any(r.status == "failed" for r in res.rows) else 0


def _cmd_sweep(args) -> int:
    """M6.10: run a sweep.yaml in parallel as its own family. Exit 0 all trials ran, 1 a trial failed or none could
    be ranked, 2 for a bad spec, a refused confirmation or a locked holdout."""
    from pathlib import Path
    from stockify import runconfig
    from stockify.research import sweep
    if args.workers is not None and args.workers < 1:
        print(f"--workers must be a whole number of 1 or more, not {args.workers}", file=sys.stderr)
        return 2
    try:
        spec = sweep.load_spec(Path(args.config))
        count = len(sweep.expand(spec))
        print(f"sweep {spec.name}: {count} trials")
        if count > sweep.CONFIRM_ABOVE and not args.yes:
            if not sys.stdin.isatty():
                print(f"{count} trials is more than {sweep.CONFIRM_ABOVE}: pass --yes to run without being asked "
                      "(there is no terminal to ask on)", file=sys.stderr)
                return 2
            try:
                answer = input(f"Run {count} trials? [y/N] ").strip().lower()
            except EOFError:
                print("no answer was given: pass --yes to run without being asked", file=sys.stderr)
                return 2
            if answer not in ("y", "yes"):
                print("not run", file=sys.stderr)
                return 2
        res = sweep.run_sweep(spec, unlock_reason=args.unlock_holdout, workers=args.workers)
    except (sweep.SweepSpecError, runconfig.RunConfigError, ValueError) as e:
        print(e, file=sys.stderr)
        return 2
    _print_console(sweep.format_table(res))
    return 1 if res.best_run_id is None or any(r["status"] == "failed" for r in res.rows) else 0

def _cmd_walkforward(args) -> int:
    """M6.5 (WF 6.6): walk a sweep forward, finish it and write its report. Exit 0 finished, 1 finished but a window
    could not be tested (or a run, the pool or `finish` broke after the job started), 2 bad arguments, an unusable sweep file, a locked holdout or no
    window that fits. Every argument is checked before the first run starts."""
    from datetime import date
    from pathlib import Path
    from stockify import paths, research, runconfig
    from stockify.research import pool, sweep, walkforward, wf_report
    cfg = research.settings()["walk_forward"]
    try:
        train = walkforward.parse_span(args.train or f"{cfg['train_days']}d")
        test = walkforward.parse_span(args.test or f"{cfg['test_days']}d")
        step = walkforward.parse_span(args.step or f"{cfg['step_days']}d")
        start = date.fromisoformat(args.date_from) if args.date_from else None
        end = date.fromisoformat(args.date_to) if args.date_to else None
        if args.workers is not None and args.workers < 1:
            raise ValueError("--workers must be a whole number of 1 or more")
        if step < test:
            raise ValueError(f"--step ({step}d) is smaller than --test ({test}d): the out-of-sample pieces would "
                             "overlap")
        if args.sensitivity_run is not None:
            paths.run_path(args.sensitivity_run)
        spec = sweep.load_spec(Path(args.sweep))
        res = walkforward.run_windows(spec, train=train, test=test, step=step, objective=args.objective or cfg["objective"],
                                      start=start, end=end, unlock_reason=args.unlock_holdout, workers=args.workers,
                                      pool_run=pool.run_all)      # looked up here, so a test can patch it
    except walkforward.WalkforwardRunError as e:                  # the job exists and is marked failed
        print(f"the walk-forward job failed: {e}", file=sys.stderr)
        return 1
    except (sweep.SweepSpecError, walkforward.WalkforwardError, runconfig.RunConfigError, ValueError) as e:
        print(e, file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 — a run or the pool broke: the job is already marked failed
        print(f"the walk-forward job failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    try:
        fin = walkforward.finish(res.job_id)
        report = wf_report.render(res.job_id, sensitivity_run=args.sensitivity_run)
        text = wf_report.summary_text(res.job_id)
    except Exception as e:  # noqa: BLE001
        print(f"the walk-forward job {res.job_id} ran but could not be finished: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 1
    _print_console(text)
    print(report.parent)
    untested = [w["index"] + 1 for w in fin.windows if w["test"] is None]
    if untested:
        print(f"window {', '.join(map(str, untested))} could not be tested; the report says why", file=sys.stderr)
        return 1
    return 0


def _cmd_margin_check(args) -> int:
    """MEZ 4.11: a proposed basket priced by the margin staircase from the archived NSE files (offline)."""
    from datetime import date
    from stockify import timeutil
    from stockify.fno import margin_check as mc
    try:
        legs = [mc.parse_leg(x) for x in args.leg]
        day = date.fromisoformat(args.date) if args.date else None
        if args.at is not None:
            timeutil.parse_hhmm(args.at)
    except mc.CheckError as e:
        print(e, file=sys.stderr)
        return 2
    except ValueError:
        print(f"--date must be YYYY-MM-DD and --at HH:MM (IST); got {args.date!r}, {args.at!r}", file=sys.stderr)
        return 2
    try:
        res = mc.check(legs, day, args.at, live=args.live)
    except mc.CheckInputError as e:                     # the request is wrong
        print(e, file=sys.stderr)
        return 2
    except mc.CheckDataError as e:                      # the archive cannot price it
        print(e, file=sys.stderr)
        return 1
    _print_console(mc.check_text(res))
    return 0


def _cmd_margin_scorecard(args) -> int:
    """MEZ 4.11: every staircase step against every stored Kite answer (offline; writes nothing)."""
    from stockify.fno import margin_check as mc
    card = mc.scorecard()
    if not card.rows and not (card.live_answers or card.live_errors):
        if card.notes:                       # answers are stored but none could be scored: say why
            for note in card.notes:
                print(f"Note: {note}", file=sys.stderr)
        else:
            print("no stored Kite margin answers to score (`margin calibrate` stores them)", file=sys.stderr)
        return 1
    _print_console(mc.scorecard_text(card))
    return 0


def _cmd_margin_live_report(args) -> int:
    """IWB 5.1: where Kite's live margin answers and the staircase disagree (offline; reads the lab, writes nothing)."""
    from datetime import date
    from stockify.fno import margin_live_report as mlr
    try:
        start = date.fromisoformat(args.start) if args.start else None
        end = date.fromisoformat(args.end) if args.end else None
    except ValueError:
        print(f"--from and --to must be YYYY-MM-DD; got {args.start!r}, {args.end!r}", file=sys.stderr)
        return 2
    text = mlr.report(start=start, end=end)
    if text.startswith("no live margin rows"):
        print(text.strip(), file=sys.stderr)
        return 1
    _print_console(text)
    return 0


def _cmd_session_refinalise(args) -> int:
    """C-1: finalise a session-day again, from its journal, without waiting for NSE's files (a day that failed, or was
    left open). Exit 0: finalised; 1: it cannot be finalised (the reason is printed; the day stays `finalised_failed`);
    2: bad arguments, an unknown session or a day the session never opened; 3: busy elsewhere, try again; 4: could not
    run (a locked database, an engine that is not built)."""
    from datetime import date
    from stockify import store
    from stockify.daemon import finalise
    try:
        day = date.fromisoformat(args.date)
    except ValueError:
        print(f"--date must be YYYY-MM-DD; got {args.date!r}", file=sys.stderr)
        return 2
    if store.get_session(args.session) is None:
        print(f"no such session: {args.session!r}", file=sys.stderr)
        return 2
    if store.get_session_day(args.session, args.date) is None:
        print(f"session {args.session!r} has no record of {args.date}", file=sys.stderr)
        return 2
    try:
        result = finalise.refinalise(args.session, day)
    except Exception as e:  # noqa: BLE001 — a locked database or an engine that is not built
        print(f"refinalise: could not run: {type(e).__name__}: {e}".splitlines()[0], file=sys.stderr)
        return 4
    if result.status == "finalised":
        print(f"finalised: {args.session} {args.date} ({result.settle_count} settlement records)")
        return 0
    if result.status == "failed":
        print(f"cannot finalise {args.session} {args.date}: {result.error}", file=sys.stderr)
        return 1
    print(f"{result.status}: {args.session} {args.date}; try again shortly", file=sys.stderr)
    return 3


def _cmd_parity_journal(args) -> int:
    """RD 5.1: replay a finalised session-day's journal and compare it with the store (reads only). Exit 0: identical;
    1: a difference (one `differs:` line); 2: bad arguments or an unknown session; 3: not finalised, nothing checked; 4: could not run (a locked
    database, an engine that is not built)."""
    from datetime import date
    from stockify import parity, store
    try:
        day = date.fromisoformat(args.date)
    except ValueError:
        print(f"--date must be YYYY-MM-DD; got {args.date!r}", file=sys.stderr)
        return 2
    if store.get_session(args.session) is None:
        print(f"no such session: {args.session!r}", file=sys.stderr)
        return 2
    try:
        result = parity.journal_parity(args.session, day)
    except Exception as e:  # noqa: BLE001 — a locked database or an engine that is not built is not a difference
        print(f"parity: could not run: {type(e).__name__}: {e}".splitlines()[0], file=sys.stderr)
        return 4
    if result.outcome == "skipped":
        print(f"skipped: {result.summary['reason']}", file=sys.stderr)
        return 3
    if result.outcome == "fail":
        print(result.difference_line)
        return 1
    print(f"identical: {args.session} {args.date} ({result.summary['records']} journal records)")
    return 0


def _cmd_parity_recorder(args) -> int:
    """RD 5.4: re-run a finalised session-day on the recorder's data and report how far it drifts from the live
    day (reads only; never pauses a session). Exit 0: reported, whatever the drift; 2: bad arguments or an unknown
    session; 3: nothing checked (not finalised, not recorded, or the landing file is busy); 4: could not run."""
    from datetime import date
    from stockify import parity, store
    try:
        day = date.fromisoformat(args.date)
    except ValueError:
        print(f"--date must be YYYY-MM-DD; got {args.date!r}", file=sys.stderr)
        return 2
    if store.get_session(args.session) is None:
        print(f"no such session: {args.session!r}", file=sys.stderr)
        return 2
    try:
        result = parity.recorder_parity(args.session, day)
    except Exception as e:  # noqa: BLE001 — a locked database or an engine that is not built is not a difference
        print(f"parity: could not run: {type(e).__name__}: {e}".splitlines()[0], file=sys.stderr)
        return 4
    if result.outcome == "skipped":
        print(f"skipped: {result.summary['reason']}", file=sys.stderr)
        return 3
    s = result.summary
    print(f"recorder parity {args.session} {args.date}: fills {s['fills_live']} live, {s['fills_recorder']} recorder, "
          f"{s['fills_matched']} matched; NAV differs by {s['pnl_diff_paise']} paise (recorder less live)")
    first = s["first_divergence"]
    if first is not None:
        print(f"first divergence at {s['first_divergence_ns']} ns, fill #{first['index']}: live "
              f"{json.dumps(first['live'], sort_keys=True)} / recorder {json.dumps(first['recorder'], sort_keys=True)}")
    if s["explained_by"]:                           # only differences have causes: an identical day prints nothing here
        print(f"explained by: {', '.join(s['explained_by'])}")
    if s["unexplained"]:
        n = sum(1 for x in s["differences"] if not x["causes"])
        print(f"unexplained: {n} of {s['differences_total']} difference(s) have no earlier dropped frames, journaled "
              "margin answer or pause that covers them")
    return 0


def _cmd_compare_strategies(args) -> int:
    from stockify import runs
    from stockify.tools import compare_strategies as cs
    try:
        report = cs.run(args.config, impl=args.impl)
    except cs.CompareError as e:
        print(e, file=sys.stderr)
        return 2
    except runs.RunFailed as e:
        print(f"a run failed: {e.error}", file=sys.stderr)
        return 1
    print(report)
    return 0


def _cmd_bench(args) -> int:
    from stockify.tools import bench
    bench.run(args.pack, reps=args.reps)
    return 0


def _cmd_tools_measure(args) -> int:
    from stockify.tools import measure
    return measure.main(as_json=args.json)


def _cmd_golden_run(args) -> int:
    from stockify import golden

    only = [x.strip() for x in args.only.split(",")] if args.only else None
    failed = 0
    for _, g in golden.goldens(only, include_capture=True):
        if g.get("impl") == "kite_capture":
            status, notes = golden.capture_status(g)
            print(f"{g['id']:<4} {status:<4}  {g['title']}")
            for p in notes:
                print(f"       {p}")
            failed += status == "FAIL"
            continue
        problems = golden.run_one(g, args.impl)
        print(f"{g['id']:<4} {'PASS' if not problems else 'FAIL'}  {g['title']}")
        for p in problems:
            print(f"       {p}")
        failed += bool(problems)
    return 1 if failed else 0


def _cmd_compare(args) -> int:
    from pathlib import Path

    from stockify import compare, paths

    def folder(x: str) -> Path:
        p = Path(x)
        return p if p.exists() else paths.run_path(x)  # a folder path, or a run id (checked)

    problems = compare.compare(folder(args.run_a), folder(args.run_b), tol_paise=args.tol_paise)
    for p in problems[:200]:
        print(p)
    print("identical" if not problems else f"{len(problems)} differences", file=sys.stderr)
    return 0 if not problems else 1


def _cmd_compare_runs(args) -> int:
    from stockify.reporting import compare as rc
    try:
        c = rc.compare_runs(list(args.runs))
    except KeyError as e:
        print(f"stockify compare: no test run {str(e.args[0])[:80]!r}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"stockify compare: {e}", file=sys.stderr)
        return 2
    print(rc.format_table(c))
    return 0


def _cmd_export(args) -> int:
    from pathlib import Path

    from stockify.reporting import export
    _load_env()      # the secret scan compares against KITE_API_KEY / KITE_API_SECRET, as the API process has them
    try:
        out = export.build(args.run_id, with_packs=args.with_packs, out=Path(args.out) if args.out else None)
    except export.ExportError as e:
        print(f"stockify export: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"stockify export: cannot write the zip: {e.strerror or type(e).__name__}", file=sys.stderr)
        return 1
    print(out)
    return 0


def _cmd_capture_charges(args) -> int:
    from pathlib import Path

    from stockify.tools import capture_charges

    _load_env()
    out = capture_charges.capture(Path(args.cases))
    print(out)
    problems = capture_charges.compare_capture(out)
    for p in problems:
        print(f"  {p}")
    print("all components match" if not problems else f"{len(problems)} differences (fix charges.json)",
          file=sys.stderr)
    return 0


def _cmd_tools_fuzz(args) -> int:
    from stockify.tools import fuzz

    return fuzz.main(fuzz.parse_seeds(args.seeds), args.shrink, args.save)


def _cmd_notify_test(args) -> int:
    """Send one test message through the channel chosen in notify.json (secrets come from the environment). Exit 0:
    sent; 1: not sent (the reason is printed; a secret never is)."""
    _load_env()      # the SMTP settings are in the repo's settings file, which the daemon loads too
    from stockify.daemon import notify
    error = notify.send_test()
    if error is not None:
        print(f"notify test: not sent: {error}", file=sys.stderr)
        return 1
    print("notify test: sent")
    return 0


def _cmd_backup_run(args) -> int:
    """Back the lab's irreplaceable stores up to `--target`, or `storage.json` -> `backup.target` (GK 5.7). Exit 0:
    done; 1: some copy failed or did not verify; 2: no usable target (the reason is printed)."""
    from pathlib import Path

    from stockify import backup, timeutil
    from stockify.config import loader
    target = args.target or loader.load_json("storage.json")["backup"]["target"]
    if not target:
        print("backup: no target: pass --target DIR, or set backup.target in stockify/config/storage.json "
              "(a folder outside the lab and the repo, ideally on another drive)", file=sys.stderr)
        return 2
    try:
        res = backup.run(Path(target), day=timeutil.ist_today())
    except backup.BackupRefused as e:
        print(e, file=sys.stderr)
        return 2
    print(f"backup: {res.copied} copied, {res.skipped} skipped, {res.bytes} bytes -> {target}")
    for note in res.notes:
        print(f"backup: note: {note}")
    for error in res.errors:
        print(f"backup: ERROR: {error}", file=sys.stderr)
    return 1 if res.errors else 0


def _cmd_engine(args) -> int:
    from stockify import engine
    try:
        json.dump(engine.build_info(), sys.stdout, indent=2, sort_keys=True)
    except engine.EngineUnavailable as e:
        print(e, file=sys.stderr)
        return 1
    sys.stdout.write("\n")
    return 0


def _check_environment() -> str | None:
    """A clear message instead of a traceback when run outside the project's .venv."""
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("Asia/Kolkata")
    except Exception:  # noqa: BLE001 — ZoneInfoNotFoundError, or a broken tzdata
        return ("This Python has no time-zone data (the tzdata package). STOCKIFY must run from its virtual "
                "environment:\n    .\\.venv\\Scripts\\python.exe -m stockify " + " ".join(sys.argv[1:]) +
                f"\n(this one is {sys.executable}). Or install the requirements: pip install -r requirements.txt")
    return None


def main(argv: list[str] | None = None) -> int:
    problem = _check_environment()
    if problem:
        print(problem, file=sys.stderr)
        return 2
    p = argparse.ArgumentParser(prog="stockify", description="STOCKIFY: NSE simulation lab")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("daemon", help="market daemon: record each session, then hibernate (D-32)")
    d.add_argument("--once", action="store_true", help="log one idle cycle and exit")
    d.add_argument("--recorder-only", action="store_true", help="record today's ticks (M1.5)")
    d.add_argument("--sessions", default="all",
                   help="paper sessions to run: all (every armed, running or paused one), none, or ids separated "
                        "by commas")
    d.add_argument("--replay-sandbox", default=None, metavar="DIR",
                   help="run a replay with DIR (an empty directory elsewhere) as the lab's home: everything the replay "
                        "writes goes there, never into the real lab, while the recorded day is read from the real lab "
                        "(read-only)")
    d.add_argument("--ticker", default="live",
                   help="live, or replay:YYYY-MM-DD:<Nx|max> to replay a recorded day")
    d.add_argument("--inject", action="append", default=[],
                   help="replay fault, e.g. disconnect@10:30:00+90s, token_expiry@11:00:00, kill@13:00:00")
    d.set_defaults(func=_cmd_daemon)

    sub.add_parser("config", help="validate stockify/config/").set_defaults(func=_cmd_config)
    sub.add_parser("engine", help="print the engine build info").set_defaults(func=_cmd_engine)

    nt = sub.add_parser("notify", help="notifications (M5.8)")
    ntsub = nt.add_subparsers(dest="notify_command", required=True)
    ntsub.add_parser("test", help="send one test message through the channel in notify.json").set_defaults(
        func=_cmd_notify_test)

    bk = sub.add_parser("backup", help="verified backup of the stores that cannot be rebuilt (M5.13)")
    bksub = bk.add_subparsers(dest="backup_command", required=True)
    bkr = bksub.add_parser("run", help="copy refdata, lab, journals, the tick archive, NSE raw files and the ELM "
                                       "archive to the target; reads the lab, changes nothing in it")
    bkr.add_argument("--target", help="backup folder (default: storage.json backup.target)")
    bkr.set_defaults(func=_cmd_backup_run)

    r = sub.add_parser("refdata", help="reference data (instruments with history)")
    rsub = r.add_subparsers(dest="refdata_command", required=True)
    rs = rsub.add_parser("sync", help="fold today's Kite instrument dumps into refdata.sqlite")
    rs.add_argument("--date", help="trade date YYYY-MM-DD (default: today in IST)")
    rs.add_argument("--dir", help="read NSE.csv / NFO.csv from this folder instead of downloading")
    rs.set_defaults(func=_cmd_refdata_sync)

    dq = sub.add_parser("data", help="data tools")
    dqs = dq.add_subparsers(dest="data_command", required=True)
    dr = dqs.add_parser("report", help="data-quality report for a recorded day (M1.9)")
    dr.add_argument("--date", required=True, help="IST trade date YYYY-MM-DD")
    dr.set_defaults(func=_cmd_data_report)

    n = sub.add_parser("nse", help="NSE public end-of-day files (M1.3)")
    nsub = n.add_subparsers(dest="nse_command", required=True)
    nf = nsub.add_parser("fetch", help="fetch and load one trade date (default: today in IST)")
    nf.add_argument("--date", help="IST trade date YYYY-MM-DD")
    nf.add_argument("--datasets", default="fo,cm,idx,qtyfreeze", help="comma list of fo,cm,idx,qtyfreeze")
    nf.add_argument("--force", action="store_true", help="download again even if already fetched")
    nf.add_argument("--allow-unverified", action="store_true", help="use sources.json rows not yet verified")
    nf.set_defaults(func=_cmd_nse_fetch)
    nb = nsub.add_parser("backfill", help="fetch every trading day in a range (resumable)")
    nb.add_argument("--datasets", default="fo,cm,idx", help="comma list of fo,cm,idx")
    nb.add_argument("--from", dest="from_", required=True, help="first date YYYY-MM-DD")
    nb.add_argument("--to", help="last date YYYY-MM-DD (default: yesterday in IST)")
    nb.add_argument("--allow-unverified", action="store_true", help="use sources.json rows not yet verified")
    nb.add_argument("--rows", dest="verbose_rows", action="store_true", help="print one line per file")
    nb.set_defaults(func=_cmd_nse_backfill)

    nm = nsub.add_parser("margin-files", help="archive NSE's SPAN risk files and the contract-wise ELM file")
    nm.add_argument("--date", help="IST trade date YYYY-MM-DD: every SPAN file of the day plus its ELM file")
    nm.add_argument("--backfill", nargs=2, metavar=("FROM", "TO"),
                    help="end-of-day SPAN files for every trading day in the range (resumable)")
    nm.add_argument("--elm-window", action="store_true",
                    help="the ELM file of every trading day back 120 days that NSE still serves")
    nm.set_defaults(func=_cmd_nse_margin_files)

    fu = sub.add_parser("fundamentals", help="the fundamentals screener's NSE filings and daily list (FS 8)")
    fusub = fu.add_subparsers(dest="fundamentals_command", required=True)
    fb = fusub.add_parser("backfill", help="NSE's results filings of every universe company from --from, their "
                                           "XBRL figures and today's list; first NSE's CM files when cm_eod lacks "
                                           "days (resumable; about one request a second)")
    fb.add_argument("--from", dest="from_", metavar="YYYY-MM-DD", type=_iso_day,
                    help="first period end YYYY-MM-DD (default: fundamentals.json filings_from, 2021-04-01)")
    fb.add_argument("--limit", metavar="N", type=_positive_int, help="list at most N companies in this run")
    fb.set_defaults(func=_cmd_fundamentals_backfill)
    fusub.add_parser("refresh", help="one daily run now: universe (Fridays), results calendar, new filings, "
                                     "figures and today's list").set_defaults(func=_cmd_fundamentals_refresh)
    fusub.add_parser("test-record", help="the screener's walk-forward test record (needs a Kite login; fetches "
                                         "daily prices for about 500 stocks)"
                     ).set_defaults(func=_cmd_fundamentals_test_record)

    b = sub.add_parser("bars", help="Kite historical bar cache (M1.4)")
    bsub = b.add_subparsers(dest="bars_command", required=True)
    bf = bsub.add_parser("fetch", help="fetch and cache candles")
    bf.add_argument("--symbols", required=True,
                    help="comma list: RELIANCE (NSE), NFO:NIFTY26NOVFUT, or underlyings with --continuous")
    bf.add_argument("--interval", default="day", help="minute, 3minute, 5minute, 10minute, 15minute, "
                    "30minute, 60minute or day")
    bf.add_argument("--from", dest="from_", required=True, help="first IST date YYYY-MM-DD")
    bf.add_argument("--to", required=True, help="last IST date YYYY-MM-DD")
    bf.add_argument("--continuous", action="store_true", help="continuous day futures for the underlyings (K-5)")
    bf.set_defaults(func=_cmd_bars_fetch)
    bsub.add_parser("sweep", help="corporate-action check for every cached live instrument"
                    ).set_defaults(func=_cmd_bars_sweep)

    o = sub.add_parser("oracle", help="the Python oracle backtester (M2)")
    osub = o.add_subparsers(dest="oracle_command", required=True)
    orun = osub.add_parser("run", help="run a run.json; prints the run folder")
    orun.add_argument("--config", required=True, help="path to run.json")
    orun.add_argument("--run-id", help="run folder name (default: <name>-<bundle hash>)")
    orun.add_argument("--unlock-holdout", metavar="REASON", help=_UNLOCK_HELP)
    orun.set_defaults(func=_cmd_oracle_run)
    rn = sub.add_parser("run", help="run a backtest on the engine and record it in lab.sqlite (M3.11)")
    rmode = rn.add_mutually_exclusive_group()
    rmode.add_argument("--config", help="path to run.json")
    rmode.add_argument("--rerun", metavar="RUN_ID", help="re-run a finished run from its folder; exit 1 unless "
                       "determinism.json is reproduced byte for byte (DT-03; a folder saved before D-64 may differ "
                       "only by margin_daily.csv's step column)")
    rn.add_argument("--run-id", help="run this queued run (its stored config); --config is then optional")
    rn.add_argument("--impl", default="engine", choices=["engine", "oracle"])
    rn.add_argument("--no-report", action="store_true", help="skip report.md and the charts")
    rn.add_argument("--unlock-holdout", metavar="REASON", help=_UNLOCK_HELP)
    rn.add_argument("--role", default="trial", choices=_run_roles(),
                    help="how the run counts (default trial: counts toward N in the deflated Sharpe); the research "
                         "tools set the others for their own runs")
    rn.add_argument("--job-id", help="the research job this run belongs to (set by the research tools)")
    rn.set_defaults(func=_cmd_run)
    g = sub.add_parser("golden", help="golden scenarios (A3)")
    gsub = g.add_subparsers(dest="golden_command", required=True)
    grun = gsub.add_parser("run", help="run every golden on an implementation")
    grun.add_argument("--impl", default="oracle", choices=["oracle", "engine"])
    grun.add_argument("--only", help="comma list of ids, e.g. G01,G09")
    grun.set_defaults(func=_cmd_golden_run)
    cr = sub.add_parser("compare", help="put 2 to 6 finished runs side by side (metrics, equity, config differences)")
    cr.add_argument("runs", nargs="+", metavar="RUN_ID", help="2 to 6 run ids")
    cr.set_defaults(func=_cmd_compare_runs)
    ex = sub.add_parser("export", help="write a finished run as a reproducible zip (never holds a secret)")
    ex.add_argument("run_id", metavar="RUN_ID")
    ex.add_argument("--with-packs", action="store_true", help="also include the event packs the run used")
    ex.add_argument("--out", help="write the zip here instead of the exports folder")
    ex.set_defaults(func=_cmd_export)
    ca = sub.add_parser("compare-artifacts", help="diff two run folders' deterministic artifacts")
    ca.add_argument("run_a")
    ca.add_argument("run_b")
    ca.add_argument("--tol-paise", type=int, default=0, help="allowed difference in money columns (default 0)")
    ca.set_defaults(func=_cmd_compare)

    mg = sub.add_parser("margin", help="margin model tools (M4)")
    mgsub = mg.add_subparsers(dest="margin_command", required=True)
    mc = mgsub.add_parser("calibrate", help="fit SPAN-lite to Kite's basket margins during market hours (M4.9; needs a "
                                           "fresh Kite login; read-only, no orders; the fit waits for your review)")
    mc.add_argument("--underlying", default="NIFTY")
    mc.add_argument("--samples", type=int, default=200, help="portfolios to price (default 200)")
    mc.add_argument("--seed", type=int, default=0, help="which portfolios are drawn (default 0)")
    mc.add_argument("--skip-expiring", action="store_true",
                    help="leave out contracts whose last trading day is today and take the next two expiries (use on an expiry day)")
    mc.add_argument("--refit", action="store_true",
                    help="fit again from the samples stored for --date, offline (no Kite call); replaces that day's "
                         "pending row")
    mc.add_argument("--date", help="with --refit: the IST trade date of the stored calibration, YYYY-MM-DD")
    mc.set_defaults(func=_cmd_margin_calibrate)
    sv = mgsub.add_parser("span-validate", help="check the exact SPAN calculator and NSE's ELM %% against the Kite "
                                                "answers stored for a day (offline; reads the lab, writes one report)")
    sv.add_argument("--date", required=True, help="IST trade date of the stored calibration samples, YYYY-MM-DD")
    sv.add_argument("--underlying", nargs="*", help="default: every underlying sampled that day")
    sv.set_defaults(func=_cmd_margin_span_validate)
    mk = mgsub.add_parser("check", help="price a proposed basket with the margin staircase from the archived NSE "
                                        "files (offline; reads the lab, writes nothing)")
    mk.add_argument("--leg", action="append", required=True,
                    help='"BUY|SELL <units> <tradingsymbol>", e.g. "SELL 65 NIFTY26N1724950CE"; repeat per leg')
    mk.add_argument("--date", help="IST trade date YYYY-MM-DD (default: the latest archived day; after about 20:30 "
                                   "IST that can be tomorrow, whose i1 file NSE creates the evening before)")
    mk.add_argument("--at", help="IST time HH:MM: the SPAN file in force is the last created by then "
                                 "(default 15:30)")
    mk.add_argument("--live", action="store_true",
                    help="also ask Kite for this basket (read-only /margins/basket, consider_positions=false; needs "
                         "a fresh Kite login) and print its answer beside the staircase's")
    mk.set_defaults(func=_cmd_margin_check)
    mgsub.add_parser("scorecard", help="every staircase step's error against every stored Kite answer, by portfolio "
                                       "type (offline; reads the lab, writes nothing)"
                     ).set_defaults(func=_cmd_margin_scorecard)

    lr = mgsub.add_parser("live-report", help="where Kite's live margin answers and the staircase disagree, by portfolio "
                                              "type, underlying, IST hour and SPAN-file age (offline; reads the lab)")
    lr.add_argument("--from", dest="start", help="first IST trade date, YYYY-MM-DD (default: all)")
    lr.add_argument("--to", dest="end", help="last IST trade date, YYYY-MM-DD (default: all)")
    lr.set_defaults(func=_cmd_margin_live_report)

    rs = sub.add_parser("research", help="honest-backtest reports: trial families, deflated Sharpe, bootstrap (M6)")
    rssub = rs.add_subparsers(dest="research_command", required=True)
    rf_ = rssub.add_parser("families", help="list the trial families (exit 0; 1 unknown family for --name; 2 bad arguments)")
    rf_.add_argument("--name", nargs=2, metavar=("FAMILY_ID", "DESCRIPTION"), help="name a family")
    rf_.add_argument("--backfill", action="store_true",
                     help="give every run and paper session without a family its default one (writes to the lab database)")
    rf_.add_argument("--dry-run", action="store_true", help="with --backfill: count what would change, write nothing")
    rf_.set_defaults(func=_cmd_research_families)
    rw = rssub.add_parser("show", help="print the research lines of a run or a family, in plain words")
    rw.add_argument("target", help="a run id or a family id")
    rw.set_defaults(func=_cmd_research_show)
    rr = rssub.add_parser("report", help="write the report.md of a finished walk-forward job again and print its path",
                          description="Re-render research/<job_id>/report.md of a walk-forward job that has finished "
                                      "(status done). The sensitivity run is not stored on the walk-forward job, so "
                                      "give it with --sensitivity-run to have the cost table in the report; without "
                                      "it the report says how to run the suite. Exit 0 written, 1 the job's result "
                                      "file is missing or the write failed, 2 unknown job, a job that is not done, "
                                      "or a bad run id.")
    rr.add_argument("--walkforward", required=True, metavar="JOB_ID", help="a finished walk-forward job's id")
    rr.add_argument("--sensitivity-run", metavar="RUN_ID",
                    help="the run whose sensitivity.json goes into the cost table (not stored on the walk-forward "
                         "job, so name it here)")
    rr.set_defaults(func=_cmd_research_report)

    sn = sub.add_parser("sensitivity", help="re-run a finished run with costs x0/1.5/2, extra slippage ticks, more "
                                            "latency and other fill models, and flag it fragile (M6.7)")
    sn.add_argument("--run", required=True, help="the finished run's id")
    sn.add_argument("--workers", type=int, default=None, help="runs at once (default: the pool's own limit)")
    sn.add_argument("--unlock-holdout", metavar="REASON", help=_UNLOCK_HELP)
    sn.set_defaults(func=_cmd_sensitivity)

    sw = sub.add_parser("sweep", help="run a sweep.yaml (grid or random search) in parallel as its own family and "
                                      "print the results, best first (M6.10)")
    sw.add_argument("--config", required=True, help="the sweep.yaml file")
    sw.add_argument("--workers", type=int, default=None, help="runs at once (default: the pool's own limit)")
    sw.add_argument("--unlock-holdout", metavar="REASON", help=_UNLOCK_HELP)
    sw.add_argument("--yes", action="store_true", help="do not ask before running more than 50 trials")
    sw.set_defaults(func=_cmd_sweep)

    wk = sub.add_parser("walkforward", help="walk a sweep forward: train, pick the best, test on the days after; "
                                            "writes report.md (M6.5)",
                        description="Walk-forward analysis. For each window the sweep's grid runs on the training "
                                    "days, the best by the objective is run on the test days after them, and the "
                                    "test pieces are stitched into one out-of-sample record. All lengths are "
                                    "calendar days (120d). Defaults come from research.json walk_forward. Exit 0 "
                                    "finished, 1 finished but a window could not be tested or a run broke, 2 bad "
                                    "arguments or a locked holdout.")
    wk.add_argument("--sweep", required=True, metavar="FILE", help="the sweep.yaml (its grid is run in every window)")
    wk.add_argument("--train", metavar="Nd", help="training days, e.g. 120d")
    wk.add_argument("--test", metavar="Nd", help="test days, e.g. 30d")
    wk.add_argument("--step", metavar="Nd", help="days between window starts, e.g. 30d (not below --test)")
    wk.add_argument("--objective", choices=_objectives(), help="how the best training run is picked")
    wk.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD", help="first day (default: the base config's)")
    wk.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", help="last day (default: the base config's)")
    wk.add_argument("--workers", type=int, default=None, help="runs at once (default: the sweep's, else the pool's)")
    wk.add_argument("--unlock-holdout", metavar="REASON", help=_UNLOCK_HELP)
    wk.add_argument("--sensitivity-run", metavar="RUN_ID",
                    help="a finished run whose sensitivity.json goes into the report's cost table")
    wk.set_defaults(func=_cmd_walkforward)

    se = sub.add_parser("session", help="paper session maintenance")
    sesub = se.add_subparsers(dest="session_command", required=True)
    rf = sesub.add_parser("refinalise", help="finalise a failed or unfinished session-day again from its journal "
                                             "(exit 0 finalised, 1 cannot, 2 bad arguments, 3 busy, 4 could not run)")
    rf.add_argument("--session", required=True, help="session id")
    rf.add_argument("--date", required=True, help="trade date YYYY-MM-DD")
    rf.set_defaults(func=_cmd_session_refinalise)

    pa = sub.add_parser("parity", help="nightly parity gates (M5.10)")
    pasub = pa.add_subparsers(dest="parity_command", required=True)
    pj = pasub.add_parser("journal", help="replay a finalised session-day's journal and compare it with the store "
                                          "(reads only; exit 0 identical, 1 a difference, 3 not finalised, 4 could not run)")
    pj.add_argument("--session", required=True, help="session id")
    pj.add_argument("--date", required=True, help="trade date YYYY-MM-DD")
    pj.set_defaults(func=_cmd_parity_journal)
    pr = pasub.add_parser("recorder", help="re-run a finalised session-day on the recorder's data and report the drift "
                                           "from the live day (reads only, never pauses; exit 0 reported, 3 nothing "
                                           "checked, 4 could not run)")
    pr.add_argument("--session", required=True, help="session id")
    pr.add_argument("--date", required=True, help="trade date YYYY-MM-DD")
    pr.set_defaults(func=_cmd_parity_recorder)

    tl = sub.add_parser("tools", help="maintenance tools")
    tsub = tl.add_subparsers(dest="tools_command", required=True)
    bn = tsub.add_parser("bench", help="engine speed on a recorded day's pack (M3.13); reads the lab, writes nothing there")
    bn.add_argument("--pack", required=True, help="the pack's SHA-256 (a unique prefix is enough)")
    bn.add_argument("--reps", type=int, default=3, help="runs per case; the median is reported (default 3)")
    bn.set_defaults(func=_cmd_bench)
    cc = tsub.add_parser("capture-charges", help="save Kite's contract-note charges for imaginary orders (G08)")
    cc.add_argument("--cases", default="tests/golden/charges_cases.json")
    cc.set_defaults(func=_cmd_capture_charges)
    fz = tsub.add_parser("fuzz", help="oracle vs engine on random worlds (A3 P-04; the weekly job is 4000-8999)")
    fz.add_argument("--seeds", default="4000-8999", help="e.g. 4000-8999 or 4000,4100-4110 (default 4000-8999)")
    fz.add_argument("--shrink", action="store_true", help="shrink each disagreement to a small world")
    fz.add_argument("--save", action="store_true", help="save each disagreement as tests/golden/F<seed>.yaml")
    fz.set_defaults(func=_cmd_tools_fuzz)
    cs = tsub.add_parser("compare-strategies",
                         help="run several strategies on the same days and write them side by side (M4.13)")
    cs.add_argument("--config", required=True, help="a stockify.compare/1 JSON file")
    cs.add_argument("--impl", default="engine", choices=["engine", "oracle"])
    cs.set_defaults(func=_cmd_compare_strategies)
    ms = tsub.add_parser("measure", help="the numbers for the write-up, each with its sample and source; \"not measured "
                                         "yet\" where there is no sample (reads only; exit 0, 1 if the text fails the "
                                         "privacy scan)")
    ms.add_argument("--json", action="store_true", help="print JSON instead of a table")
    ms.set_defaults(func=_cmd_tools_measure)

    pk = sub.add_parser("pack", help="event packs for the engine (M1.8)")
    pksub = pk.add_subparsers(dest="pack_command", required=True)
    pb = pksub.add_parser("build", help="build a pack from a JSON spec; prints its SHA-256")
    pb.add_argument("--spec", required=True, help="path to the spec JSON")
    pb.set_defaults(func=_cmd_pack_build)
    ps = pksub.add_parser("show", help="print a pack's manifest and first records")
    ps.add_argument("sha", help="the pack's SHA-256 (a unique prefix is enough)")
    ps.add_argument("--records", type=int, default=10, help="how many records to print (default 10)")
    ps.set_defaults(func=_cmd_pack_show)

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
    csub = c.add_subparsers(dest="calendar_command")
    au = csub.add_parser("audit", help="the holiday list against the lab's data: trading days with no data and "
                                       "holidays with data (D-75); reads the lab, writes nothing",
                         description="The holiday list against the lab's data: trading days with no data and "
                                     "holidays with data (D-75). Reads the lab and writes nothing: a database is "
                                     "opened immutable. Run it only while no recorder or daemon is writing: a "
                                     "database with a write in progress is not opened (the report names it "
                                     "\"busy, not checked\" and the exit code is 1), and a read during a write can "
                                     "make the report wrong, never the stores.")
    au.add_argument("--from", dest="date_from", required=True, help="first date YYYY-MM-DD")
    au.add_argument("--to", dest="date_to", required=True, help="last date YYYY-MM-DD")
    au.add_argument("--segment", default="FO", choices=["CM", "FO"], help="calendar segment (default FO)")
    au.set_defaults(func=_cmd_calendar_audit)

    k = sub.add_parser("kite", help="Kite Connect checks")
    ksub = k.add_subparsers(dest="kite_command", required=True)
    ksub.add_parser("check", help="confirm the plan gives REST, historical and WebSocket data"
                    ).set_defaults(func=_cmd_kite_check)

    for name, (milestone, text) in _STUBS.items():
        s = sub.add_parser(name, help=f"{text} ({milestone}, not implemented yet)")
        s.set_defaults(func=None, milestone=milestone)

    args = p.parse_args(argv)
    log.setup(args.verbose, file="stockify-daemon.log" if args.command == "daemon" else None)
    if args.func is None:
        print(f"stockify {args.command}: not implemented yet (planned for {args.milestone})",
              file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

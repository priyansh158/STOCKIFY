"""The daemon's day: which session is next, which jobs are due or missed, and
when the laptop may hibernate and must wake (M1.5 minimal scheduler, D-32).

Job times live only in `schedule.json` (IST, A1 §7.8). Jobs belong to an IST
trade date and are skipped on non-trading days:

| job         | when (IST)                | notes                                        |
|-------------|---------------------------|----------------------------------------------|
| refdata     | 08:30, retry until 09:10  | needs a fresh Kite login                     |
| session     | subscribe 08:58 → 15:45   | the recorder (`run_recorder`)                |
| compaction  | 16:30                     | must finish before sleeping (M1.7)           |
| nse_fetch   | 19:30 → 23:00             | catch-up: runs on the next wake if slept through (M1.3) |
| margin_files | 17:20 → 22:00            | SPAN + ELM files (CAM 4.2); holds sleep until done or 22:00 |
| prune       | after the session         | recorder_status rows older than 7 days       |

Completed jobs are remembered in `$LAB/scheduler_state.json` so a restart or a
resume from hibernate runs what was missed, once.

Sleep after close (`schedule.json` → `sleep_after_close`): allowed once the
session and compaction are done, not before `not_before`, not while the owner
used the laptop in the last `idle_minutes`, and not when `$LAB/stay_awake`
exists. Catch-up jobs never block sleep, with one exception: the day's
margin files (`HOLDS_SLEEP_UNTIL_WINDOW_ENDS`) hold it until they are archived
or their window ends (22:00), so they are fetched the same evening and retried
every `retry_every_min`, not once the next morning (CAM 4.4). The wake time is
`wake_ist` on the next trading day.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta

from stockify import paths, timeutil
from stockify.config import loader
from stockify.data import calendar

SEGMENT = "FO"
CATCH_UP_DAYS = 7
BLOCKS_SLEEP = ("compaction",)          # a missed session must not keep the laptop up forever
CATCH_UP = ("nse_fetch", "margin_files")
HOLDS_SLEEP_UNTIL_WINDOW_ENDS = ("margin_files",)   # fetched the same evening (CAM 4.4), not the next morning


def schedule() -> dict:
    return loader.load_json("schedule.json")


@dataclass(frozen=True)
class DayPlan:
    day: date
    refdata_ns: int
    refdata_until_ns: int
    subscribe_ns: int
    unsubscribe_ns: int
    compaction_ns: int
    nse_fetch_ns: int
    nse_fetch_until_ns: int
    margin_files_ns: int
    margin_files_until_ns: int
    keep_awake_ns: int


def plan(day: date, cfg: dict | None = None) -> DayPlan:
    cfg = cfg or schedule()
    jobs = cfg["jobs"]
    return DayPlan(
        day=day,
        refdata_ns=timeutil.ist_ns(day, jobs["refdata"]),
        refdata_until_ns=timeutil.ist_ns(day, jobs["token_dependent_retry"]["until"]),
        subscribe_ns=timeutil.ist_ns(day, jobs["subscribe"]),
        unsubscribe_ns=timeutil.ist_ns(day, jobs["unsubscribe"]),
        compaction_ns=timeutil.ist_ns(day, jobs["compaction"]),
        nse_fetch_ns=timeutil.ist_ns(day, jobs["nse_fetch"]["start"]),
        nse_fetch_until_ns=timeutil.ist_ns(day, jobs["nse_fetch"]["until"]),
        margin_files_ns=timeutil.ist_ns(day, jobs["margin_files"]["start"]),
        margin_files_until_ns=timeutil.ist_ns(day, jobs["margin_files"]["until"]),
        keep_awake_ns=timeutil.ist_ns(day, cfg["keep_awake"]["from"]),
    )


def session_day(now_ns: int, cfg: dict | None = None) -> date:
    """The trade date whose session is ongoing or next."""
    d = timeutil.ns_to_ist(now_ns).date()
    if calendar.is_trading_day(SEGMENT, d) and now_ns < plan(d, cfg).unsubscribe_ns:
        return d
    return calendar.next_trading_day(SEGMENT, d)


def next_wake_ns(now_ns: int, cfg: dict | None = None) -> int:
    """`wake_ist` on the first trading day whose wake time is still ahead."""
    cfg = cfg or schedule()
    wake = cfg["sleep_after_close"]["wake_ist"]
    d = timeutil.ns_to_ist(now_ns).date()
    if not calendar.is_trading_day(SEGMENT, d):
        d = calendar.next_trading_day(SEGMENT, d)
    while timeutil.ist_ns(d, wake) <= now_ns:
        d = calendar.next_trading_day(SEGMENT, d)
    return timeutil.ist_ns(d, wake)


# ── job state ────────────────────────────────────────────────────────────────

class State:
    """{job: [completed trade dates]} in $LAB/scheduler_state.json."""

    def __init__(self, path=None):
        self.path = path or (paths.lab_dir() / "scheduler_state.json")
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}

    def done(self, job: str, day: date) -> bool:
        return day.isoformat() in self.data.get(job, [])

    def mark(self, job: str, day: date) -> None:
        days = set(self.data.get(job, []))
        days.add(day.isoformat())
        self.data[job] = sorted(days)[-60:]                 # keep the last 60 entries
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


def retry_every_ns(job: str, cfg: dict | None = None) -> int:
    """The gap between two attempts of a catch-up job (`retry_every_min`); 0 when it has none."""
    spec = (cfg or schedule())["jobs"].get(job)
    if not isinstance(spec, dict):
        return 0
    return int(spec.get("retry_every_min", 0)) * 60 * 1_000_000_000


def window_end_ns(job: str, day: date, cfg: dict | None = None) -> int:
    p = plan(day, cfg)
    return {"nse_fetch": p.nse_fetch_until_ns, "margin_files": p.margin_files_until_ns}[job]


def missed_jobs(now_ns: int, state: State, cfg: dict | None = None) -> list[tuple[str, date]]:
    """Catch-up jobs whose start passed on a recent trading day and never completed."""
    out = []
    today = timeutil.ns_to_ist(now_ns).date()
    for i in range(CATCH_UP_DAYS, -1, -1):
        d = today - timedelta(days=i)
        if not calendar.is_trading_day(SEGMENT, d):
            continue
        p = plan(d, cfg)
        if "nse_fetch" in CATCH_UP and p.nse_fetch_ns <= now_ns and not state.done("nse_fetch", d):
            out.append(("nse_fetch", d))
        if "margin_files" in CATCH_UP and p.margin_files_ns <= now_ns and not state.done("margin_files", d):
            out.append(("margin_files", d))
    return out


# ── sleep decision ───────────────────────────────────────────────────────────

def should_sleep(now_ns: int, day: date | None, state: State, *, idle_s: float,
                 stay_awake: bool, cfg: dict | None = None) -> tuple[bool, str]:
    """(may hibernate now?, why).

    `day` is the trade date whose post-session work must be finished first, or
    None when nothing is owed (night before a session, weekend, holiday).
    """
    cfg = cfg or schedule()
    rule = cfg["sleep_after_close"]
    if not rule.get("enabled", False):
        return False, "sleep_after_close disabled"
    if stay_awake:
        return False, "stay_awake flag present"
    if day is not None and calendar.is_trading_day(SEGMENT, day):
        if now_ns < timeutil.ist_ns(day, rule["not_before"]):
            return False, f"before not_before {rule['not_before']} IST"
        for job in BLOCKS_SLEEP:
            if not state.done(job, day):
                return False, f"{job} for {day} not done"
        for job in HOLDS_SLEEP_UNTIL_WINDOW_ENDS:
            until = window_end_ns(job, day, cfg)
            if not state.done(job, day) and now_ns < until:
                return False, (f"{job} for {day} not done (window until "
                               f"{timeutil.ns_to_ist(until):%H:%M} IST)")
    if idle_s < 60 * float(rule["idle_minutes"]):
        return False, f"owner active {idle_s / 60:.0f} min ago"
    return True, "ok"


def stay_awake_flag():
    return paths.lab_dir() / "stay_awake"


def next_wake_file():
    """Written before exit 75; the launcher registers the wake task from it."""
    return paths.lab_dir() / "next_wake.json"


def write_next_wake(wake_ns: int) -> None:
    local = timeutil.ns_to_ist(wake_ns).astimezone(calendar.owner_tz())
    next_wake_file().write_text(json.dumps({
        "wake_ns": wake_ns,
        "wake_unix_s": wake_ns // 1_000_000_000,
        "wake_ist": timeutil.ns_to_ist(wake_ns).isoformat(),
        "wake_owner_local": local.isoformat(),
    }, indent=2), encoding="utf-8")

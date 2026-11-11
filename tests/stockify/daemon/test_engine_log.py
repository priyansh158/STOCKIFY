"""GK 5.1 (carried from Task 7): a paused strategy skips every hook, and the engine logs one INFO line per skip.
Over an unattended day that is millions of lines, so the daemon reports the first skip and then one summary line per
minute of engine time with the counts. The engine's own log (events_log.txt, goldens) is untouched."""
from __future__ import annotations

import itertools
import logging
import unittest

import tests.support as support  # noqa: F401  — must precede project imports
from tests.stockify.daemon import paper_fixture as pf

from stockify import engine, store
from stockify.daemon import session_day
from stockify.daemon.engine_log import EngineLog

SEC = 1_000_000_000


def skip(t_s: int, hook: str = "on_tick", reasons: str = "owner") -> str:
    return f"{t_s * SEC} INFO {hook} skipped: strategy disabled ({reasons})"


def enable_logging(test) -> None:
    """The suite runner switches logging off; `assertLogs` needs it on."""
    was = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    test.addCleanup(logging.disable, was)


class EngineLogTest(unittest.TestCase):
    def setUp(self):
        enable_logging(self)
        self.log = EngineLog("s1")

    def lines(self, ctx):
        return [r.getMessage() for r in ctx.records]

    def test_the_first_skip_is_logged_and_the_rest_of_the_minute_is_counted(self):
        with self.assertLogs("stockify.daemon.engine_log", "INFO") as ctx:
            self.log.feed([skip(0)] + [skip(1 + i % 50) for i in range(100)])
        self.assertEqual(len(ctx.records), 1)
        self.assertIn("on_tick skipped: strategy disabled (owner)", self.lines(ctx)[0])
        self.assertIn("summarised", self.lines(ctx)[0])

    def test_one_summary_per_minute_with_counts_by_hook(self):
        with self.assertLogs("stockify.daemon.engine_log", "INFO") as ctx:
            self.log.feed([skip(0)])
            self.log.feed([skip(10), skip(20, "on_timer"), skip(30), skip(59)])
            self.assertEqual(len(ctx.records), 1)
            self.log.feed([skip(60)])                                  # a minute of engine time after the first
        self.assertEqual(len(ctx.records), 2)
        summary = self.lines(ctx)[1]
        self.assertIn("5 hook calls skipped", summary)
        self.assertIn("on_tick 4", summary)
        self.assertIn("on_timer 1", summary)
        self.assertIn("(owner)", summary)
        with self.assertLogs("stockify.daemon.engine_log", "INFO") as ctx:
            self.log.feed([skip(119)])
            self.log.feed([skip(120)])                                 # the next minute starts at the last summary
        self.assertEqual(len(ctx.records), 1)
        self.assertIn("2 hook calls skipped", self.lines(ctx)[0])

    def test_a_day_of_skips_stays_a_few_lines(self):
        with self.assertLogs("stockify.daemon.engine_log", "INFO") as ctx:
            self.log.feed([skip(s) for s in range(0, 6 * 3600) for _ in range(20)])      # 432,000 skips in 6 hours
            self.log.flush()
        self.assertLessEqual(len(ctx.records), 6 * 60 + 2)
        total = sum(int(m.split(" hook calls")[0].split()[-1]) for m in self.lines(ctx) if "hook calls" in m)
        self.assertEqual(total, 6 * 3600 * 20 - 1)                     # everything but the first, reported once

    def test_enabling_reports_what_is_pending_and_the_next_pause_logs_afresh(self):
        with self.assertLogs("stockify.daemon.engine_log", "INFO") as ctx:
            self.log.feed([skip(0), skip(1), skip(2), f"{3 * SEC} INFO strategy enabled"])
            self.log.feed([skip(10)])
        messages = self.lines(ctx)
        self.assertEqual(len(messages), 4)
        self.assertIn("2 hook calls skipped", messages[1])
        self.assertIn("strategy enabled", messages[2])
        self.assertIn("on_tick skipped: strategy disabled", messages[3])

    def test_a_different_reason_set_is_reported_at_once(self):
        with self.assertLogs("stockify.daemon.engine_log", "INFO") as ctx:
            self.log.feed([skip(0), skip(1), skip(2, reasons="owner, token_expired")])
        messages = self.lines(ctx)
        self.assertEqual(len(messages), 3)
        self.assertIn("1 hook calls skipped", messages[1])
        self.assertIn("(owner, token_expired)", messages[2])

    def test_flush_reports_the_pending_count(self):
        with self.assertLogs("stockify.daemon.engine_log", "INFO") as ctx:
            self.log.feed([skip(0), skip(1), skip(2)])
            self.log.flush()
            self.log.flush()
        self.assertEqual(len(ctx.records), 2)
        self.assertIn("2 hook calls skipped", self.lines(ctx)[1])

    def test_other_engine_lines_are_logged_with_their_level(self):
        with self.assertLogs("stockify.daemon.engine_log", "INFO") as ctx:
            self.log.feed([f"{SEC} INFO order 3 rejected MARGIN", f"{SEC} WARN day volume decreased; treated as 0"])
        self.assertEqual([r.levelno for r in ctx.records], [logging.INFO, logging.WARNING])
        self.assertIn("order 3 rejected MARGIN", self.lines(ctx)[0])


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class PausedSessionDayTest(pf.PaperDayTest):
    def setUp(self):
        super().setUp()
        enable_logging(self)

    def test_a_paused_day_logs_a_handful_of_lines_not_one_per_skipped_hook(self):
        from tests.stockify.daemon.test_session_day import pack_records
        from stockify.data import packs
        self.record(seconds=240)
        sha = packs.build({"dataset": "ticks", "from": pf.EXPIRY, "to": pf.EXPIRY, "clock_cadence_ms": 5000})
        records = pack_records(sha)
        store.create_session("s1", "s1", {}, 1)
        ticks = itertools.count(0, 10)
        sd = session_day.SessionDay("s1", pf.DAY, self.session_cfg(strategy={"kind": "script", "actions": []}),
                                    mono=lambda: next(ticks))
        self.addCleanup(sd.close)
        sd.open(None, ["owner"], self.t("09:14:00"))                       # opens disabled
        with self.assertLogs("stockify.daemon.engine_log", "INFO") as ctx:
            for i in range(0, len(records), 50):
                sd.feed(records[i:i + 50], None)
            sd.close()
        engine_lines = [line for line in sd_log_lines(self, records) if "skipped: strategy disabled" in line]
        shown = [r for r in ctx.records if "skipped: strategy disabled" in r.getMessage()
                 or "hook calls skipped" in r.getMessage()]
        minutes = (int(engine_lines[-1].split()[0]) - int(engine_lines[0].split()[0])) // (60 * SEC)
        self.assertGreater(len(engine_lines), 1000, "the fixture should skip many hooks")
        self.assertLessEqual(len(shown), 1 + minutes + 1, [r.getMessage() for r in shown])      # first, a line per minute, flush
        self.assertLess(len(shown) * 20, len(engine_lines))


def sd_log_lines(test, records) -> list[str]:
    """The engine's own full log for the same input, from a second, never-throttled engine."""
    ticks = itertools.count(0, 10)
    store.create_session("s2", "s2", {}, 2)
    other = session_day.SessionDay("s2", pf.DAY, test.session_cfg(strategy={"kind": "script", "actions": []}),
                                   mono=lambda: next(ticks))
    test.addCleanup(other.close)
    other.open(None, ["owner"], test.t("09:14:00"))
    for i in range(0, len(records), 50):
        other.feed(records[i:i + 50], None)
    return list(other.engine.events_log())


if __name__ == "__main__":
    unittest.main()

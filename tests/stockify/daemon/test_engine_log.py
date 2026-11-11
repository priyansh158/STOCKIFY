"""GK 5.1 (carried from Task 7): a paused strategy skips every hook, and the engine logs one INFO line per skip.
Over an unattended day that is millions of lines, so the daemon reports the first skip and then one summary line per
minute of engine time with the counts. The engine's own log (events_log.txt, goldens) is untouched."""
from __future__ import annotations

import itertools
import logging
import threading
import time
import unittest

import tests.support as support  # noqa: F401  — must precede project imports
from tests.stockify.daemon import paper_fixture as pf

from stockify import engine, store
from stockify.daemon import session_day
from stockify.config import loader
from stockify.daemon.engine_log import EngineLog, LogPump

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


class EngineLogCapTest(unittest.TestCase):
    """GK 5.2 (I2): an unattended day cannot flood the log, however chatty a strategy is."""

    def setUp(self):
        enable_logging(self)

    def test_the_caps_are_bounded_paper_json_keys(self):
        doc = loader.load_json("paper.json")
        self.assertEqual(doc["engine_log"], {"max_lines_per_min": 120, "max_line_chars": 500})
        for key, bad in (("max_lines_per_min", 0), ("max_lines_per_min", 10001), ("max_line_chars", 0),
                         ("max_line_chars", 10001), ("max_line_chars", "5")):
            with self.subTest(key=key, bad=bad):
                broken = {**doc, "engine_log": {**doc["engine_log"], key: bad}}
                with self.assertRaises(loader.ConfigError):
                    loader._check_paper(broken)
        loader._check_paper({**doc, "engine_log": {"max_lines_per_min": 10000, "max_line_chars": 10000}})
        with self.assertRaises(loader.ConfigError):
            loader._check_paper({k: v for k, v in doc.items() if k != "engine_log"})

    def test_a_flood_of_lines_is_capped_per_minute_with_one_summary(self):
        log = EngineLog("s1", max_lines_per_min=120, max_line_chars=500)
        with self.assertLogs("stockify.daemon.engine_log", "INFO") as ctx:
            log.feed([f"{SEC + i} INFO strategy says {i}" for i in range(5000)])          # all inside one minute
            self.assertEqual(len(ctx.records), 120)
            log.feed([f"{61 * SEC} INFO next minute"])                                    # the window rolls
        messages = [r.getMessage() for r in ctx.records]
        self.assertEqual(len(messages), 120 + 1 + 1)
        self.assertIn("4880 engine log lines suppressed", messages[120])
        self.assertIn("next minute", messages[121])

    def test_flush_reports_what_was_suppressed(self):
        log = EngineLog("s1", max_lines_per_min=2, max_line_chars=500)
        with self.assertLogs("stockify.daemon.engine_log", "INFO") as ctx:
            log.feed([f"{SEC} INFO a", f"{SEC} INFO b", f"{SEC} INFO c", f"{SEC} INFO d"])
            log.flush()
        self.assertEqual(len(ctx.records), 3)
        self.assertIn("2 engine log lines suppressed", ctx.records[2].getMessage())

    def test_lines_are_truncated_and_newlines_escaped(self):
        log = EngineLog("s1", max_lines_per_min=120, max_line_chars=50)
        forged = f"{SEC} INFO forged" + chr(10) + "9 INFO fake"
        raw = "no timestamp" + chr(13) + chr(10) + "second line"
        with self.assertLogs("stockify.daemon.engine_log", "INFO") as ctx:
            log.feed([f"{SEC} INFO " + "x" * 5000, forged, raw])
        for record in ctx.records:
            text = record.getMessage()
            self.assertNotIn(chr(10), text)
            self.assertNotIn(chr(13), text)
            self.assertLessEqual(len(text), len("session s1: ") + 50 + 40)
        self.assertIn("truncated", ctx.records[0].getMessage())
        self.assertIn("forged" + chr(92) + "n9 INFO fake", ctx.records[1].getMessage())
        self.assertIn("no timestamp" + chr(92) + "r" + chr(92) + "nsecond line", ctx.records[2].getMessage())

    def test_a_slow_sink_never_blocks_the_engine_thread(self):
        release = threading.Event()
        got = []

        def slow(level, text):
            release.wait(10)
            got.append(text)

        pump = LogPump(slow, maxsize=100)
        log = EngineLog("s1", max_lines_per_min=10_000, max_line_chars=100, sink=pump)
        started = time.monotonic()
        log.feed([f"{SEC + i} INFO line {i}" for i in range(5000)])
        self.assertLess(time.monotonic() - started, 2.0)                        # it did not wait for the sink
        self.assertGreater(pump.dropped, 4000)                                   # the queue was bounded: drops counted
        release.set()
        pump.close()
        self.assertGreaterEqual(len(got), 100)
        self.assertTrue(any("dropped" in line for line in got), got[-3:])        # and said so once


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

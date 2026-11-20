"""CW 3: the live 52-week alerts (`stockify.watchlist.live.HighLowWatch`): the tap, the judging, the once-only record,
the digests and their cap, a removed stock, and an evaluator crash beside a recorder."""
from __future__ import annotations

from datetime import date, timedelta
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.stockify.daemon import packets_fixture as pk
from tests.stockify.daemon.test_engine_log import enable_logging
from tests.stockify.watchlist.test_ranges import flat
from tests.stockify.watchlist.test_store import DAY, StoreCase

from stockify import timeutil
from stockify.config import loader
from stockify.daemon import kite_packets as kp
from stockify.daemon.clock import ManualClock
from stockify.daemon.landing import LandingDB
from stockify.daemon.recorder import Recorder
from stockify.watchlist import live, store

SEC = 1_000_000_000
TOKENS = {"INFY": 1000, "TCS": 1001, "RELIANCE": 1002}
HIGH, LOW, CLOSE = 11_000, 9_000, 10_000          # the prior range of every fixture stock, in paise


def packet(token: int, *, high: int = 10_500, low: int = 9_500, close: int = CLOSE, full: bool = False) -> bytes:
    q = pk.quote(token, 10_000, 1, 10_000, 100, 1, 1, 10_000, high, low, close)
    return q + bytes(140) if full else q


def item(packets: list[bytes], at: str = "10:00:00", frame_no: int = 1) -> tuple:
    return (frame_no, timeutil.ist_ns(DAY, at), 0, kp.build_frame(packets), True)


class Post:
    """notify.post stand-in: remembers, and says at once how the delivery went."""

    def __init__(self, ok: bool = True, error=None):
        self.calls: list[dict] = []
        self.ok, self.error = ok, error

    def __call__(self, event, subject, body, *, key, on_done):
        self.calls.append({"event": event, "subject": subject, "body": body, "key": key})
        if self.ok:
            on_done(self.error)
        return self.ok


class WatchCase(StoreCase):
    def setUp(self):
        super().setUp()
        enable_logging(self)
        live.state_file().unlink(missing_ok=True)
        self.addCleanup(lambda: live.state_file().unlink(missing_ok=True))
        self.cfg = dict(loader.load_json("watchlist.json"))
        self.clock = ManualClock(timeutil.ist_ns(DAY, "09:00:00"), mono_ns=10**12)
        self.post = Post()
        self.tokens = {TOKENS[s]: self.ids[s] for s in TOKENS}
        self.bars = {iid: flat(DAY - timedelta(days=400), 400, high=HIGH, low=LOW, close=CLOSE)
                     for iid in self.ids.values()}

    def build(self, **cfg) -> live.HighLowWatch | None:
        return live.HighLowWatch.build(DAY, self.tokens, clock=self.clock, cfg={**self.cfg, **cfg},
                                       bars_of=lambda iid: self.bars.get(iid, []), post=self.post)

    def feed(self, w: live.HighLowWatch, *packets: bytes, at: str = "10:00:00") -> None:
        w.put_nowait(item(list(packets), at))
        w.step()

    def flush(self, w: live.HighLowWatch) -> None:
        self.clock.advance(int(self.cfg["alert_batch_s"]) * SEC)
        w.step()

    def rows(self, kind: str | None = None) -> list[dict]:
        return [a for a in store.alerts() if kind is None or a["kind"] == kind]


class TapTests(WatchCase):
    def test_the_tap_never_raises_even_when_full_and_is_a_recorder_tap(self):
        store.add("RELIANCE")
        w = self.build()
        rec = Recorder(mock.MagicMock(max_seq=lambda: 0, max_frame_no=lambda: 0), lambda t: None, self.clock)
        rec.add_tap(w)                                                   # accepted: it has put_nowait
        for n in range(10 * int(self.cfg["tap_frames"])):
            w.put_nowait(item([packet(TOKENS["RELIANCE"])], frame_no=n))
        self.assertEqual(len(w.frames), int(self.cfg["tap_frames"]))     # the oldest fell off
        self.assertEqual(w.frames[-1][0], 10 * int(self.cfg["tap_frames"]) - 1)


class JudgeTests(WatchCase):
    def setUp(self):
        super().setUp()
        store.add("RELIANCE")
        store.add("INFY")
        self.w = self.build()

    def test_one_alert_per_side_per_day_and_strictly_beyond_the_range(self):
        rel = TOKENS["RELIANCE"]
        self.feed(self.w, packet(rel, high=HIGH, low=LOW))               # equal is not new
        self.assertEqual(self.rows(), [])
        self.feed(self.w, packet(rel, high=HIGH + 5))
        self.feed(self.w, packet(rel, high=HIGH + 50), packet(rel, high=HIGH + 60, full=True))
        self.feed(self.w, packet(rel, high=HIGH + 60, low=LOW - 1))
        [high] = self.rows("high52")
        [low] = self.rows("low52")
        self.assertEqual((high["symbol"], high["ref"], high["level_paise"], high["prior_paise"]),
                         ("RELIANCE", DAY.isoformat(), HIGH + 5, HIGH))
        self.assertEqual(high["prior_date"], (DAY - timedelta(days=1)).isoformat())
        self.assertEqual((low["level_paise"], low["prior_paise"]), (LOW - 1, LOW))
        self.assertEqual(len(self.w.batch), 2)

    def test_a_restart_does_not_tell_again(self):
        self.feed(self.w, packet(TOKENS["RELIANCE"], high=HIGH + 5))
        self.flush(self.w)
        self.assertEqual(len(self.post.calls), 1)
        again = self.build()                                             # the daemon restarted the same day
        self.feed(again, packet(TOKENS["RELIANCE"], high=HIGH + 9))
        self.flush(again)
        self.assertEqual(len(self.post.calls), 1)
        self.assertEqual(len(self.rows("high52")), 1)

    def test_pre_open_and_after_the_close_are_ignored(self):
        self.feed(self.w, packet(TOKENS["RELIANCE"], high=HIGH + 5), at="09:05:00")
        self.feed(self.w, packet(TOKENS["RELIANCE"], high=HIGH + 5), at="15:45:00")
        self.assertEqual(self.rows(), [])
        self.feed(self.w, packet(TOKENS["RELIANCE"], high=HIGH + 5), at="09:15:30")
        self.assertEqual(len(self.rows()), 1)

    def test_zero_values_are_never_a_low(self):
        self.feed(self.w, packet(TOKENS["RELIANCE"], low=0))
        self.assertEqual(self.rows(), [])

    def test_unwatched_tokens_and_other_packets_are_skipped(self):
        frame = [pk.ltp(TOKENS["TCS"], 1), pk.index_quote(pk.NIFTY_INDEX, 1, 2, 3, 4, 5, 6),
                 packet(TOKENS["TCS"], high=HIGH + 5)]
        with mock.patch.object(kp, "parse_packet", wraps=kp.parse_packet) as parse:
            self.feed(self.w, *frame)
        self.assertEqual(parse.call_count, 0)                            # TCS is not watched: nothing decoded
        self.assertEqual(self.rows(), [])

    def test_a_basis_mismatch_turns_the_stock_off_for_the_day(self):
        rel = TOKENS["RELIANCE"]
        with self.assertLogs("stockify.watchlist.live", "WARNING") as cm:
            self.feed(self.w, packet(rel, high=HIGH * 3, close=CLOSE // 2))    # a 1:2 split: yesterday's close halves
        self.assertIn("split or bonus", "\n".join(cm.output))
        self.feed(self.w, packet(rel, high=HIGH * 3, close=CLOSE))
        self.assertEqual(self.rows(), [])
        self.feed(self.w, packet(TOKENS["INFY"], high=HIGH + 1, close=CLOSE + CLOSE * 2 // 100))   # within 2 %
        self.assertEqual([a["symbol"] for a in self.rows()], ["INFY"])

    def test_a_removed_stock_stops_alerting(self):
        store.remove("RELIANCE")
        self.clock.advance(live.VERSION_EVERY_S * SEC)
        self.feed(self.w, packet(TOKENS["RELIANCE"], high=HIGH + 5), packet(TOKENS["INFY"], high=HIGH + 5))
        self.assertEqual([a["symbol"] for a in self.rows()], ["INFY"])


class BuildTests(WatchCase):
    def test_stale_candles_turn_the_stock_off_for_the_day(self):
        store.add("RELIANCE")
        store.add("INFY")
        rel = self.ids["RELIANCE"]
        self.bars[rel] = [b for b in self.bars[rel] if timeutil.ns_to_ist(b["ts_ns"]).date() < DAY - timedelta(days=3)]
        with self.assertLogs("stockify.watchlist.live", "WARNING") as cm:
            w = self.build()
        self.assertIn("RELIANCE", "\n".join(cm.output))
        self.assertEqual([x.symbol for x in w.watched.values()], ["INFY"])

    def test_nothing_watched_or_nothing_judgeable_is_no_evaluator(self):
        self.assertIsNone(self.build())
        store.add("TCS")
        self.bars[self.ids["TCS"]] = self.bars[self.ids["TCS"]][-100:]  # less than a year
        self.assertIsNone(self.build())

    def test_the_range_ends_the_trading_day_before_the_session(self):
        store.add("RELIANCE")
        rel = self.ids["RELIANCE"]
        self.bars[rel][-1] = {**self.bars[rel][-1], "high_paise": HIGH + 100}   # yesterday's high counts
        self.bars[rel].append({**self.bars[rel][-1], "ts_ns": timeutil.ist_ns(DAY, "00:00"), "high_paise": 99_999})
        [w] = self.build().watched.values()                               # today's own candle does not
        self.assertEqual(w.high_paise, HIGH + 100)


class DeliveryTests(WatchCase):
    def setUp(self):
        super().setUp()
        for s in TOKENS:
            store.add(s)

    def test_alerts_within_the_batch_time_go_in_one_email(self):
        w = self.build()
        self.feed(w, packet(TOKENS["RELIANCE"], high=HIGH + 5))
        self.clock.advance(10 * SEC)
        self.feed(w, packet(TOKENS["INFY"], low=LOW - 5))
        self.assertEqual(self.post.calls, [])
        self.flush(w)
        [mail] = self.post.calls
        self.assertEqual((mail["event"], mail["key"]), ("watch_high_low", f"52w:{DAY.isoformat()}:1"))
        self.assertIn("RELIANCE, INFY", mail["subject"])
        self.assertIn("RELIANCE: a new 52-week high of ₹110.05", mail["body"])
        self.assertIn("INFY: a new 52-week low of ₹89.95", mail["body"])
        self.assertEqual({a["delivery"] for a in self.rows()}, {"sent"})

    def test_after_the_daily_cap_alerts_are_capped_and_the_last_email_says_so(self):
        w = self.build(max_alert_emails_per_day=2)
        for s in ("RELIANCE", "INFY"):
            self.feed(w, packet(TOKENS[s], high=HIGH + 5))
            self.flush(w)
        self.assertEqual(len(self.post.calls), 2)
        self.assertNotIn("last of today's", self.post.calls[0]["body"])
        self.assertIn("last of today's 2 alert emails", self.post.calls[1]["body"])
        self.assertIn("/watchlist", self.post.calls[1]["body"])
        self.feed(w, packet(TOKENS["TCS"], high=HIGH + 5))
        self.flush(w)
        self.assertEqual(len(self.post.calls), 2)
        self.assertEqual({a["symbol"]: a["delivery"] for a in self.rows()},
                         {"RELIANCE": "sent", "INFY": "sent", "TCS": "capped"})

    def test_the_cap_holds_across_a_restart(self):
        w = self.build(max_alert_emails_per_day=1)
        self.feed(w, packet(TOKENS["RELIANCE"], high=HIGH + 5))
        self.flush(w)
        again = self.build(max_alert_emails_per_day=1)
        self.feed(again, packet(TOKENS["INFY"], high=HIGH + 5))
        self.flush(again)
        self.assertEqual(len(self.post.calls), 1)
        self.assertEqual(self.rows("high52")[0]["delivery"], "capped")    # newest first: INFY

    def test_a_failed_delivery_is_marked_failed_and_stop_sends_what_waits(self):
        self.post.error = "SMTPAuthenticationError"
        w = self.build()
        self.feed(w, packet(TOKENS["RELIANCE"], high=HIGH + 5))
        w.start()
        w.stop()                                                         # the thread's last pass sends the batch
        self.assertFalse(w._thread.is_alive())
        self.assertEqual(len(self.post.calls), 1)
        self.assertEqual(self.rows()[0]["delivery"], "failed")

    def test_a_message_kept_in_notifys_outbox_counts_as_sent(self):
        self.post.error = "gaierror"
        w = self.build()
        self.feed(w, packet(TOKENS["RELIANCE"], high=HIGH + 5))
        with mock.patch("stockify.daemon.notify.queued", return_value=True) as queued:
            self.flush(w)
        queued.assert_called_once_with("watch_high_low", f"52w:{DAY.isoformat()}:1")
        self.assertEqual(self.rows()[0]["delivery"], "sent")

    def test_a_pending_alert_left_by_a_crash_goes_in_the_next_runs_first_digest(self):
        w = self.build()
        self.feed(w, packet(TOKENS["RELIANCE"], high=HIGH + 5))         # recorded, then the process died
        self.assertEqual(self.post.calls, [])
        again = self.build()
        self.feed(again, packet(TOKENS["RELIANCE"], high=HIGH + 9))     # already told today: not a second row
        self.flush(again)
        [mail] = self.post.calls
        self.assertIn("RELIANCE: a new 52-week high of ₹110.05", mail["body"])
        self.assertEqual([a["delivery"] for a in self.rows()], ["sent"])

    def test_a_replay_post_logs_and_never_emails(self):
        with mock.patch("stockify.daemon.notify.post") as post, \
                self.assertLogs("stockify.watchlist.live", "INFO") as cm:
            self.assertTrue(live.replay_post("watch_high_low", "[STOCKIFY] s", "b", key="k", on_done=None))
        post.assert_not_called()
        self.assertIn("NOTIFY watch_high_low (replay, not sent)", "\n".join(cm.output))


class ContainmentTests(WatchCase):
    def test_an_evaluator_crash_never_touches_the_recorder_fed_alongside_it(self):
        store.add("RELIANCE")
        w = self.build()
        landing = LandingDB(DAY)
        self.addCleanup(landing.close)
        rec = Recorder(landing, {TOKENS["RELIANCE"]: self.ids["RELIANCE"]}.get, self.clock)
        rec.begin()
        rec.add_tap(w)
        with mock.patch.object(w, "_judge", side_effect=RuntimeError("evaluator bug")), \
                self.assertLogs("stockify.watchlist.live", "ERROR") as cm:
            for n in range(live.MAX_ERRORS + 3):
                self.clock.set(timeutil.ist_ns(DAY, "10:00:00") + n * SEC)
                rec.on_message(None, kp.build_frame([packet(TOKENS["RELIANCE"], high=HIGH + 5)]), True)
                rec.process(rec.queue.get_nowait())
                w.step()
        rec.flush()
        self.assertTrue(w.disabled)
        self.assertEqual(w.errors, live.MAX_ERRORS)
        self.assertIn("off for the rest of", "\n".join(cm.output))
        self.assertEqual((rec.stats.tap_errors, rec.stats.packets), (0, live.MAX_ERRORS + 3))
        self.assertEqual(landing.packet_count(), live.MAX_ERRORS + 3)
        self.assertEqual(self.rows(), [])

    def test_a_broken_store_mid_day_is_contained_and_the_alert_is_tried_again(self):
        store.add("RELIANCE")
        w = self.build()
        with mock.patch.object(store, "record_alert", side_effect=OSError("disk")):
            self.feed(w, packet(TOKENS["RELIANCE"], high=HIGH + 5))
        self.assertEqual(w.errors, 1)
        self.assertFalse(w.disabled)
        self.feed(w, packet(TOKENS["RELIANCE"], high=HIGH + 7))          # the write works again
        self.assertEqual([a["level_paise"] for a in self.rows()], [HIGH + 7])
        self.assertEqual(w.errors, 0)                                    # a clean pass resets the budget

    def test_only_failures_in_a_row_switch_the_evaluator_off(self):
        store.add("RELIANCE")
        w = self.build()
        for _ in range(3 * live.MAX_ERRORS):
            with mock.patch.object(w, "_frame", side_effect=RuntimeError("one bad frame")):
                for _ in range(live.MAX_ERRORS - 1):
                    w.put_nowait(item([packet(TOKENS["RELIANCE"])]))
                    w.step()
            w.step()                                                     # a clean pass
        self.assertFalse(w.disabled)

    def test_nothing_on_the_main_thread_reads_the_store(self):
        """The constructor (before the connect) and stop() (before the shutdown) never touch watchlist.sqlite: a
        store that hangs or breaks costs only the evaluator's own thread."""
        store.add("RELIANCE")
        with mock.patch.object(store, "rows", side_effect=AssertionError("store read")), \
                mock.patch.object(store, "version", side_effect=AssertionError("store read")):
            w = live.HighLowWatch(DAY, self.tokens, clock=self.clock, cfg=self.cfg, post=self.post,
                                  bars_of=lambda iid: self.bars.get(iid, []))
            w.put_nowait(item([packet(TOKENS["RELIANCE"])]))
            w.stop()
        self.assertFalse(w.ready)

    def test_a_store_that_cannot_be_read_turns_the_evaluator_off_on_its_thread(self):
        store.add("RELIANCE")
        w = live.HighLowWatch(DAY, self.tokens, clock=self.clock, cfg=self.cfg, post=self.post,
                              bars_of=lambda iid: self.bars.get(iid, []))
        with mock.patch.object(store, "rows", side_effect=OSError("locked")), \
                self.assertLogs("stockify.watchlist.live", "ERROR") as cm:
            w.start()
            w._thread.join(5)
        self.assertTrue(w.disabled)
        self.assertIn("recording is not affected", "\n".join(cm.output))
        self.assertEqual(len(w.frames), 0)

    def test_frames_tapped_before_the_evaluator_is_ready_are_dropped(self):
        store.add("RELIANCE")
        w = live.HighLowWatch(DAY, self.tokens, clock=self.clock, cfg=self.cfg, post=self.post,
                              bars_of=lambda iid: self.bars.get(iid, []))
        w.put_nowait(item([packet(TOKENS["RELIANCE"], high=HIGH + 5)]))
        self.assertTrue(w.prepare())
        w.step()
        self.assertEqual(self.rows(), [])

    def test_the_thread_is_named_and_stops_quickly(self):
        store.add("RELIANCE")
        w = self.build()
        w.start()
        self.assertEqual(w._thread.name, "watch-alerts")
        w.stop()
        self.assertFalse(w._thread.is_alive())

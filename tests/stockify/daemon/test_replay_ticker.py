"""stockify.daemon.replay_ticker: a recorded day replays byte for byte (M1.6)."""
from __future__ import annotations

import unittest
import unittest.mock
from datetime import date

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import paths, timeutil
from stockify.daemon import kite_packets as kp
from stockify.daemon.clock import ManualClock
from stockify.daemon.landing import LandingDB
from stockify.daemon.recorder import Recorder
from stockify.daemon.replay_ticker import (MODE_FULL, MODE_QUOTE, ReplayClock, ReplayTicker,
                                           parse_fault, parse_spec)
from tests.stockify.daemon import packets_fixture as pf

DAY = date(2026, 11, 9)
OPT, IDX = pf.NIFTY_CE, pf.NIFTY_INDEX
TOKENS = {OPT: 501, IDX: 1}
SEC = 1_000_000_000


def t(hhmmss: str) -> int:
    return timeutil.ist_ns(DAY, hhmmss)


def record_fixture_day(landing: LandingDB) -> None:
    """Three minutes of frames with heartbeats, through the real recorder."""
    clock = ManualClock(t("09:15:00"), 5_000)
    rec = Recorder(landing, TOKENS.get, clock)
    rec.begin()

    def send(payload: bytes) -> None:
        rec.on_message(None, payload, True)
        rec.process(rec.queue.get_nowait())

    price = 12_000
    for second in range(0, 180, 2):
        clock.set(t("09:15:00") + second * SEC + 137_000)
        price += 5 if second % 4 else -3
        packets = [pf.full(OPT, price, vol=1_000 + second)]
        if second % 6 == 0:
            packets.append(pf.index_full(IDX, 2_400_000 + second, 1, 1, 1, 1, second - 90, 1_762_660_000 + second))
        send(kp.build_frame(packets))
        if second % 10 == 0:
            clock.advance(400_000_000)
            send(b"\x00")
        rec.maybe_flush()
    rec.flush()


def table(landing: LandingDB, sql: str) -> list[tuple]:
    return [tuple(r) for r in landing.conn.execute(sql)]


PACKETS_SQL = ("SELECT seq, frame_no, recv_ns, mono_ns, instrument_id, instrument_token, mode, pkt_len, "
               "ltp, volume, exch_ts_s, raw FROM packets ORDER BY seq")


class ReplayTest(unittest.TestCase):
    def setUp(self):
        support.fresh_cache_dir(self.id().rsplit(".", 1)[-1])
        self.source = LandingDB(DAY)
        record_fixture_day(self.source)
        self.target_path = paths.lab_dir() / "replay_target.sqlite"

    def tearDown(self):
        self.source.close()

    def replay_into_recorder(self, *, faults=None, modes=None, **ticker_kw):
        clock = ReplayClock(t("09:14:59"))
        ticker = ReplayTicker(LandingDB(DAY, readonly=True), clock, faults=faults, **ticker_kw)
        target = LandingDB(DAY, self.target_path)
        rec = Recorder(target, TOKENS.get, clock)
        rec.begin()
        events = []

        def on_connect(ws, response):
            events.append(("connect", clock.now_ns()))
            for token, mode in (modes or {OPT: MODE_FULL, IDX: MODE_FULL}).items():
                ws.subscribe([token])
                ws.set_mode(mode, [token])

        def on_message(ws, payload, is_binary):
            rec.on_message(ws, payload, is_binary)
            rec.process(rec.queue.get_nowait())
            rec.maybe_flush()

        ticker.on_connect = on_connect
        ticker.on_message = on_message
        ticker.on_close = lambda ws, code, reason: events.append(("close", code))
        ticker.on_error = lambda ws, code, reason: events.append(("error", code))
        ticker.on_noreconnect = lambda ws: events.append(("noreconnect",))
        ticker.connect(threaded=False)
        rec.flush()
        return target, ticker, events


class ByteForByteTests(ReplayTest):
    def test_packets_frames_and_heartbeats_are_reproduced(self):
        target, ticker, events = self.replay_into_recorder()
        self.assertEqual(table(target, PACKETS_SQL), table(self.source, PACKETS_SQL))
        hb = "SELECT minute_ns, count FROM heartbeats ORDER BY minute_ns"
        self.assertEqual(table(target, hb), table(self.source, hb))
        self.assertEqual(events[0][0], "connect")
        self.assertEqual(events[-1], ("close", 1000))
        target.close()

    def test_frames_rebuilt_exactly(self):
        frames = {}
        for row in self.source.conn.execute("SELECT frame_no, raw FROM packets ORDER BY seq"):
            frames.setdefault(row["frame_no"], []).append(bytes(row["raw"]))
        seen = []
        ticker = ReplayTicker(LandingDB(DAY, readonly=True), ReplayClock(0), heartbeats=False)
        ticker.subscribe([OPT, IDX])
        ticker.set_mode(MODE_FULL, [OPT, IDX])
        ticker.on_message = lambda ws, payload, binary: seen.append(payload)
        ticker.connect()
        self.assertEqual(seen, [kp.build_frame(p) for p in frames.values()])


class ArchiveSourceTests(ReplayTest):
    def test_replay_from_the_archive_matches_replay_from_landing(self):
        from stockify.data import compact, refdata
        from stockify.daemon.replay_ticker import ArchiveSource, LandingSource, source_for
        support.reset_sqlite_singleton(refdata)
        self.addCleanup(support.reset_sqlite_singleton, refdata)
        self.source.close()
        with unittest.mock.patch.object(compact, "ROW_GROUP", 40), \
                unittest.mock.patch.object(compact, "session_over", return_value=True):   # 9 Nov is in the future
            res = compact.compact(DAY)
        self.assertEqual(res.status, "verified", res.error)
        chosen = source_for(DAY)
        self.assertIsInstance(chosen, ArchiveSource)
        landing = LandingSource(DAY)
        self.assertEqual(list(chosen.frames()), list(landing.frames()))
        self.assertEqual(chosen.heartbeats(), landing.heartbeats())
        self.source = LandingDB(DAY)          # reopened for tearDown

    def test_without_an_archive_the_landing_file_is_used(self):
        from stockify.daemon.replay_ticker import LandingSource, source_for
        self.assertIsInstance(source_for(DAY), LandingSource)


class SubscriptionTests(ReplayTest):
    def test_only_subscribed_tokens_and_quote_mode_truncates(self):
        target, _, _ = self.replay_into_recorder(modes={OPT: MODE_QUOTE})
        lens = {r[0] for r in table(target, "SELECT DISTINCT pkt_len FROM packets")}
        tokens = {r[0] for r in table(target, "SELECT DISTINCT instrument_token FROM packets")}
        self.assertEqual((lens, tokens), ({44}, {OPT}))
        target.close()


class FaultTests(ReplayTest):
    def test_disconnect_drops_frames_and_reconnects(self):
        fault = parse_fault("disconnect@09:16:00+30s", DAY)
        target, _, events = self.replay_into_recorder(faults=[fault])
        times = [r[0] for r in table(target, "SELECT recv_ns FROM packets")]
        self.assertFalse(any(t("09:16:00") <= x < t("09:16:30") for x in times))
        self.assertIn(("close", 1006), events)
        self.assertEqual([e for e in events if e[0] == "connect"][1], ("connect", t("09:16:30")))
        target.close()

    def test_token_expiry_stops_with_error_and_noreconnect(self):
        target, _, events = self.replay_into_recorder(faults=[parse_fault("token_expiry@09:16:00", DAY)])
        self.assertIn(("error", 403), events)
        self.assertIn(("noreconnect",), events)
        self.assertTrue(all(x < t("09:16:00") for (x,) in table(target, "SELECT recv_ns FROM packets")))
        target.close()

    def test_kill_stops_without_a_close(self):
        target, ticker, events = self.replay_into_recorder(faults=[parse_fault("kill@09:16:00", DAY)])
        self.assertTrue(ticker.killed)
        self.assertNotIn("close", [e[0] for e in events])
        target.close()


class ResumeTests(ReplayTest):
    """GK 5.3 (D-71): a restarted replay goes on after the last frame the old process dispatched."""

    def recv_times(self, target) -> list[int]:
        return [r[0] for r in table(target, "SELECT DISTINCT recv_ns FROM packets ORDER BY recv_ns")]

    def test_resume_skips_frames_at_or_below_the_mark(self):
        mark = t("09:16:00") + 137_000                       # a recorded stamp
        target, ticker, _ = self.replay_into_recorder(resume_after=(0, mark))
        times = self.recv_times(target)
        self.assertTrue(times and min(times) > mark)
        every = [r[0] for r in table(self.source, "SELECT DISTINCT recv_ns FROM packets ORDER BY recv_ns")]
        self.assertEqual(times, [x for x in every if x > mark])
        beats = [r[0] for r in table(target, "SELECT minute_ns FROM heartbeats")]
        self.assertTrue(all(b + 60 * SEC > mark for b in beats))
        target.close()

    def test_the_mark_compares_stamps_not_frame_numbers(self):
        target, _, _ = self.replay_into_recorder(resume_after=(10 ** 9, t("09:16:00") + 137_000))
        self.assertTrue(self.recv_times(target))               # a huge frame_no does not skip anything by itself
        target.close()

    def test_a_fault_duration_restart_resumes_at_t_plus_n(self):
        target, ticker, events = self.replay_into_recorder(resume_not_before_ns=t("09:16:30"))
        self.assertTrue(min(self.recv_times(target)) >= t("09:16:30"))
        target.close()

    def test_a_token_expiry_with_a_duration_says_where_the_restart_resumes(self):
        target, ticker, _ = self.replay_into_recorder(faults=[parse_fault("token_expiry@09:16:00+120s", DAY)])
        self.assertEqual(ticker.resume_at_ns, t("09:18:00"))
        target.close()

    def test_a_restart_given_the_same_faults_skips_the_ones_already_passed(self):      # XI 5.12, found by E-03
        faults = [parse_fault("disconnect@09:16:00+30s", DAY), parse_fault("token_expiry@09:16:50+30s", DAY),
                  parse_fault("kill@09:17:30", DAY)]
        target, ticker, events = self.replay_into_recorder(faults=faults, resume_not_before_ns=t("09:17:20"),
                                                           resume_after=(0, t("09:16:10")))
        self.assertNotIn(("close", 1006), events)
        self.assertNotIn(("error", 403), events)
        self.assertTrue(ticker.killed)                      # the kill is after the resume point, so it stays armed
        target.close()

    def test_a_disconnect_with_a_duration_still_reconnects_in_process(self):
        target, ticker, _ = self.replay_into_recorder(faults=[parse_fault("disconnect@09:16:00+30s", DAY)])
        self.assertIsNone(ticker.resume_at_ns)
        target.close()

    def test_kill_runs_the_hook_then_terminates_before_the_first_frame_at_or_after_the_time(self):
        order = []
        target = LandingDB(DAY, self.target_path)
        rec = Recorder(target, TOKENS.get, ReplayClock(t("09:14:59")))
        rec.begin()
        ticker = ReplayTicker(LandingDB(DAY, readonly=True), ReplayClock(t("09:14:59")),
                              faults=[parse_fault("kill@09:16:00", DAY)], heartbeats=False)
        ticker.subscribe([OPT, IDX])
        ticker.set_mode(MODE_FULL, [OPT, IDX])
        ticker.on_message = lambda ws, payload, binary: order.append(("frame", ticker.clock.now_ns()))
        ticker.on_kill = lambda: order.append(("on_kill",))
        ticker.terminate = lambda code: order.append(("terminate", code))
        ticker.connect()
        self.assertEqual(order[-2:], [("on_kill",), ("terminate", 137)])
        frames = [x[1] for x in order if x[0] == "frame"]
        self.assertTrue(frames and max(frames) < t("09:16:00"))
        target.close()


class SpecTests(unittest.TestCase):
    def test_parse_spec(self):
        self.assertEqual(parse_spec("replay:2026-10-28:10x"), (date(2026, 10, 28), 10.0))
        self.assertEqual(parse_spec("replay:2026-10-28:max"), (date(2026, 10, 28), None))
        for bad in ("live", "replay:2026-10-28", "replay:2026-10-28:0x", "replay:x:1x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_spec(bad)

    def test_parse_fault(self):
        f = parse_fault("disconnect@10:30:00+90s", DAY)
        self.assertEqual((f.kind, f.at_ns, f.duration_ns), ("disconnect", t("10:30:00"), 90 * SEC))


if __name__ == "__main__":
    unittest.main()

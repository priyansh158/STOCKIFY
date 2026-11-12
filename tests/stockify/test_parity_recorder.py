"""RD 5.4 (M5.10 report; E-01): the recorder parity report. A finalised session-day is re-run on a pack built from what
the recorder kept (the same day bundle, the same strategy, a 1 s CLOCK) and the fills and NAV are compared with the
live session's. It only reports: a difference never pauses a session, and the landing and archive files are only read."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sqlite3
import unittest
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.stockify.daemon import paper_fixture as pf
from tests.stockify.test_parity_journal import SID, _Base

from stockify import __main__ as cli, bundle, engine, paths, parity, store
from stockify.daemon import finalise, lifecycle, session_day
from stockify.daemon.landing import LandingDB
from stockify.data import packs, ticks

SEC = 1_000_000_000


def sha_of(path) -> str:  # noqa: ANN001
    return hashlib.sha256(path.read_bytes()).hexdigest()


def margin_answer(units: int, instrument: int, span_paise: int) -> dict:
    """A journaled Kite answer for a book of one leg (negative units: short)."""
    from stockify.fno import live_margin
    legs = [[instrument, units]]
    return live_margin.check_payload({
        "portfolio_sha256": live_margin.portfolio_sha256([(instrument, units)]), "legs": legs,
        "span_paise": span_paise, "exposure_paise": 0, "additional_paise": 0, "option_premium_paise": 0,
        "requested_ns": 1, "source": live_margin.SOURCE})


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class _RecorderBase(_Base):
    def setUp(self):
        super().setUp()
        # the live session sees the same 1 s CLOCK the report's pack carries (SETTLE comes from finalising)
        sha = packs.build({"dataset": "ticks", "from": pf.EXPIRY, "to": pf.EXPIRY, "clock_cadence_ms": 1000})
        from tests.stockify.daemon.test_session_day import pack_records
        self.records = [r for r in pack_records(sha) if r[1] != packs.RANK[packs.SETTLE_K]]
        packs.pack_path(sha).unlink()               # only a feed for the live day: the report builds its own pack
        packs.manifest_path(sha).unlink()

    def live_day(self, extra=(), cfg=None, control_at=None):
        """The fixture day finalised live; `extra` are (t, rank, record) the journal holds and a pack would not."""
        cfg = cfg or self.cfg()
        store.create_session(SID, "s", cfg, 1)
        lifecycle.arm(SID, wall_ns=2)
        lifecycle.start_continuous(SID, wall_ns=3, engine_ns=3)
        sd = session_day.SessionDay(SID, pf.DAY, cfg, mono=lambda: next(self.ticks))
        self.addCleanup(sd.close)
        sd.open(None, [], self.t("09:14:00"))
        sd.feed(sorted([*self.records, *extra], key=lambda r: (r[0], r[1])), None)
        sd.close()
        res = finalise.finalise_day(SID, pf.DAY, files_complete=True, cutoff_passed=False)
        self.assertEqual((res.status, res.error), ("finalised", None))
        return cfg

    def journal_only_records(self):
        """What the journal can hold and no pack can: a journaled Kite margin answer, and an owner's pause that
        reached the engine before its order (the live day placed nothing; the recorder's day would have)."""
        answer = margin_answer(65, self.ces[4], 9_000_000_000)
        return [(self.t("09:15:01"), packs.RANK[packs.REFDATA_K],
                 packs.enc_refdata(0, self.t("09:15:01"), "margin_requirement", answer)),
                (self.t("09:15:01"), session_day.CONTROL_RANK,
                 packs.enc_refdata(0, self.t("09:15:01"), "strategy_control",
                                   {"enabled": False, "reasons": ["owner"]}))]

    def stored_result(self):
        return store.get_parity_result(SID, pf.EXPIRY, "recorder")


class RecorderParityTest(_RecorderBase):
    def test_identical_inputs_give_zero_diff(self):
        self.live_day()
        landing = paths.ticks_landing_file(pf.DAY)
        before = sha_of(landing)
        res = parity.recorder_parity(SID, pf.DAY)
        self.assertEqual((res.outcome, res.kind, res.session_id, res.day), ("pass", "recorder", SID, pf.DAY))
        s = res.summary
        self.assertGreater(s["fills_live"], 0)
        self.assertEqual((s["fills_recorder"], s["fills_matched"]), (s["fills_live"], s["fills_live"]))
        self.assertEqual(s["pnl_diff_paise"], 0)
        self.assertIsNone(s["first_divergence_ns"])
        self.assertIsNone(s["first_divergence"])
        self.assertEqual(s["known_differences"]["queue_full"], {"spans": [], "frames": 0})
        self.assertFalse(s["unexplained"])
        stored = self.stored_result()
        self.assertEqual((stored["outcome"], stored["summary"]), ("pass", s))
        self.assertIsNone(store.get_session_day(SID, pf.EXPIRY)["parity"], "only the journal result is the day's parity")
        self.assertEqual(sha_of(landing), before, "the recorder's file is only read")
        self.assertEqual([r["detail"]["kind"] for r in store.timeline(SID) if r["kind"] == "parity"], ["recorder"])

    def test_journal_only_margin_records_explain_divergence(self):
        """A journaled Kite margin answer (and a pause) is in the journal and in no pack: the live day placed an order
        fewer than the recorder's day did, and the report says what the journal held that the recorder could not."""
        self.live_day(extra=self.journal_only_records())
        res = parity.recorder_parity(SID, pf.DAY)
        s = res.summary
        self.assertEqual(res.outcome, "pass", "a report never fails")
        self.assertEqual((s["fills_live"], s["fills_recorder"], s["fills_matched"]), (0, 1, 0))
        self.assertNotEqual(s["pnl_diff_paise"], 0)
        self.assertEqual(s["first_divergence"]["live"], None)
        self.assertEqual(s["first_divergence"]["recorder"]["side"], "BUY")
        self.assertEqual(s["first_divergence_ns"], s["first_divergence"]["recorder"]["t_ns"])
        journal_only = s["known_differences"]["journal_only"]
        self.assertEqual(journal_only["margin_requirement"], 1)
        self.assertEqual(journal_only["strategy_control"], 1, "the opening enable is not counted, the pause is")
        self.assertFalse(s["unexplained"])
        self.assertEqual(s["explained_by"], ["margin_requirement", "strategy_control"])

    def test_a_difference_with_nothing_known_is_called_unexplained(self):
        self.live_day()
        self.tamper_row("session_fills", "fill_id", "price_paise", 1)       # the store disagrees; the files do not say why
        res = parity.recorder_parity(SID, pf.DAY)
        s = res.summary
        self.assertEqual(res.outcome, "pass")
        self.assertEqual((s["fills_live"], s["fills_recorder"], s["fills_matched"]), (1, 1, 0))
        self.assertEqual(s["first_divergence"]["live"]["price_paise"], 1)
        self.assertEqual(s["known_differences"]["journal_only"], {"margin_requirement": 0, "strategy_control": 0})
        self.assertTrue(s["unexplained"])
        self.assertEqual(s["explained_by"], [])

    def test_never_pauses(self):
        self.live_day(extra=self.journal_only_records())
        status = store.get_session(SID)["status"]
        res = parity.recorder_parity(SID, pf.DAY)
        self.assertNotEqual(res.summary["fills_live"], res.summary["fills_recorder"])
        self.assertEqual(store.get_session(SID)["pause_reasons"], [])
        self.assertEqual(store.get_session(SID)["status"], status)
        self.assertEqual(store.pending_commands(), [])
        self.assertNotIn("parity_failure", json.dumps(store.get_session(SID)))
        self.assertEqual([r for r in store.timeline(SID) if r["kind"] == "parity_still_open"], [])

    def test_unfinalised_unknown_and_unrecorded_days_are_skipped(self):
        self.assertEqual(parity.recorder_parity("nosuch", pf.DAY).outcome, "skipped")
        store.create_session("open1", "s", self.cfg(), 1)
        res = parity.recorder_parity("open1", pf.DAY)
        self.assertEqual(res.outcome, "skipped")
        self.assertIn("not finalised", res.summary["reason"])
        self.live_day()
        paths.ticks_landing_file(pf.DAY).unlink()
        res = parity.recorder_parity(SID, pf.DAY)
        self.assertEqual(res.outcome, "skipped")
        self.assertIn("not recorded", res.summary["reason"])
        self.assertIsNone(self.stored_result())

    def test_a_landing_file_being_written_is_refused_as_busy(self):
        self.live_day()
        landing = paths.ticks_landing_file(pf.DAY)
        wal = landing.with_name(landing.name + "-wal")
        wal.write_bytes(b"rows the recorder has not checkpointed")
        before = sha_of(landing)
        res = parity.recorder_parity(SID, pf.DAY)
        self.assertEqual(res.outcome, "skipped")
        self.assertIn("busy", res.summary["reason"])
        self.assertIsNone(self.stored_result())
        self.assertEqual(sha_of(landing), before)
        self.assertEqual(wal.read_bytes(), b"rows the recorder has not checkpointed")

    def test_the_landing_file_is_opened_immutable_and_nothing_appears_beside_it(self):
        self.live_day()
        landing = paths.ticks_landing_file(pf.DAY)
        names = sorted(p.name for p in landing.parent.iterdir())
        seen = []
        real = sqlite3.connect

        def spy(target, *a, **kw):
            seen.append(str(target))
            return real(target, *a, **kw)
        with mock.patch.object(sqlite3, "connect", spy):
            parity.recorder_parity(SID, pf.DAY)
        self.assertTrue([t for t in seen if "ticks_" in t and t.endswith("?mode=ro&immutable=1")], seen)
        self.assertFalse([t for t in seen if "ticks_" in t and "immutable=1" not in t], seen)
        self.assertEqual(sorted(p.name for p in landing.parent.iterdir()), names)

    # ── queue_full: known, not unexplained ───────────────────────────────────────────────────────────────────

    def drop(self, runs):
        landing = LandingDB(pf.DAY)
        landing.add_dropped_frames(runs)
        landing.close()

    def test_queue_full_rows_are_known_differences(self):
        self.live_day()
        self.drop([(3, 4, self.t("09:15:02"), self.t("09:15:03")), (9, 9, self.t("09:15:08"), self.t("09:15:08"))])
        res = parity.recorder_parity(SID, pf.DAY)
        known = res.summary["known_differences"]["queue_full"]
        self.assertEqual(known["frames"], 3)
        self.assertEqual([(g["first_frame_no"], g["last_frame_no"], g["frames"]) for g in known["spans"]],
                         [(3, 4, 2), (9, 9, 1)])
        self.assertEqual(known["spans"][0]["start_ns"], self.t("09:15:02"))
        self.assertFalse(res.summary["unexplained"])
        self.assertIn("queue_full", res.summary["explained_by"])

    def test_a_landing_file_from_before_migration_0002_reads_without_the_frame_columns(self):
        rows = [{"gap_id": 1, "start_ns": 5, "end_ns": 9, "reason": "queue_full", "scope": "all"},
                {"gap_id": 2, "start_ns": 1, "end_ns": 2, "reason": "disconnect", "scope": "all"}]
        known = parity._queue_full(rows)
        self.assertEqual(known["frames"], 0)
        self.assertEqual(known["spans"], [{"start_ns": 5, "end_ns": 9, "first_frame_no": None, "last_frame_no": None,
                                           "frames": None}])

    def test_the_archive_gaps_are_read_too(self):
        self.live_day()
        self.drop([(3, 4, self.t("09:15:02"), self.t("09:15:03"))])
        gaps = ticks.open_day(pf.DAY).gaps()

        class FakeArchive:
            source = "archive"

            def gaps(self):
                return gaps

            def close(self):
                pass
        with mock.patch.object(parity, "_open_ticks", return_value=FakeArchive()):
            res = parity.recorder_parity(SID, pf.DAY)
        self.assertEqual(res.summary["known_differences"]["queue_full"]["frames"], 2)

    # ── the pack manifest lists the dropped frames, the pack itself does not change ────────────────────────────

    def test_the_manifest_lists_queue_full_spans_and_the_pack_is_unchanged(self):
        spec = {"dataset": "ticks", "from": pf.EXPIRY, "to": pf.EXPIRY, "clock_cadence_ms": 1000}
        clean = packs.build(spec)
        self.assertNotIn("recorder_gaps", packs.manifest(clean))
        self.drop([(3, 4, self.t("09:15:02"), self.t("09:15:03"))])
        again = packs.build(spec)
        self.assertEqual(again, clean, "no new record kind: the evt bytes are the same")
        m = packs.manifest(again)
        self.assertEqual(m["recorder_gaps"], {"frames": 2, "spans": [
            {"day": pf.EXPIRY, "reason": "queue_full", "start_ns": self.t("09:15:02"), "end_ns": self.t("09:15:03"),
             "first_frame_no": 3, "last_frame_no": 4, "frames": 2}]})
        self.assertEqual(m["gaps"], {"count": 0, "days": {}}, "instruments with no events: a different thing")

    def test_a_backtest_over_such_a_day_is_labelled(self):
        spec = {"dataset": "ticks", "from": pf.EXPIRY, "to": pf.EXPIRY, "clock_cadence_ms": 1000}
        self.drop([(3, 4, self.t("09:15:02"), self.t("09:15:03")), (9, 9, self.t("09:15:08"), self.t("09:15:08"))])
        rows = bundle.resolve_packs({"data": {"packs": [{"spec": spec}]}})
        self.assertEqual(rows[0]["recorder_gaps"]["frames"], 3)
        self.assertEqual(bundle.dropped_frames_labels(rows), ["data:dropped_frames=3"])
        self.assertEqual(bundle.dropped_frames_labels([{"sha256": "x"}]), [])

    # ── the CLI ─────────────────────────────────────────────────────────────────────────────────────────────

    def test_cli_exit_codes(self):
        self.live_day()
        argv = ["parity", "recorder", "--session", SID, "--date", pf.EXPIRY]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(argv), 0)
        self.assertIn("fills 1 live, 1 recorder, 1 matched", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["parity", "recorder", "--session", SID, "--date", "2026-13-40"]), 2)
            self.assertEqual(cli.main(["parity", "recorder", "--session", "nosuch", "--date", pf.EXPIRY]), 2)
        err = io.StringIO()
        with mock.patch.object(parity, "recorder_parity", side_effect=sqlite3.OperationalError("database is locked")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(argv), 4)
        self.assertIn("database is locked", err.getvalue())
        paths.ticks_landing_file(pf.DAY).unlink()
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(argv), 3)
        self.assertIn("not recorded", err.getvalue())

    def test_cli_names_a_difference_without_failing(self):
        self.live_day(extra=self.journal_only_records())
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["parity", "recorder", "--session", SID, "--date", pf.EXPIRY]), 0)
        text = out.getvalue()
        self.assertIn("first divergence", text)
        self.assertIn("explained by: margin_requirement", text)


if __name__ == "__main__":
    unittest.main()

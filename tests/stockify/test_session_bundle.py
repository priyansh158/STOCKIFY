"""REG 5.1 (M5.2, M5.3, D-67): a paper session's config and its day bundles, resolved without packs."""
from __future__ import annotations

import copy
import hashlib
import json
import unittest
from datetime import date

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import bundle, runconfig, sessionconfig
from stockify.oracle.margin import FlatMargin
from tests.stockify.data.test_packs import DAY, PackTest
from tests.stockify.test_bundle_span import archive_span

CHAIN = [{"chain": {"underlying": "NIFTY", "expiry": "nearest", "strikes": 2}}]
STOCK_FUT = "RELIANCE26NOVFUT"        # expires Mon 2026-11-23: Tue 24th is an NSE holiday (Guru Nanak Jayanti)
STOCK_EXPIRY = "2026-11-23"


def raw(**kw) -> dict:
    base = {"schema": "stockify.session/1", "name": "s1",
            "strategy": {"kind": "python", "ref": "stockify.runconfig:RunConfigError"},
            "capital_paise": 10_000_000, "costs": {"allow_unverified": True}, "universe": CHAIN}
    base.update(kw)
    return base


def cfg(**kw) -> dict:
    return sessionconfig.load(raw(**kw))


class SessionConfigTests(unittest.TestCase):
    def test_defaults_that_differ_from_a_run(self):
        c = cfg()
        self.assertEqual(c["schema"], "stockify.session/1")
        self.assertEqual(c["fill_model"]["kind"], "snapshot_quote")
        self.assertEqual((c["margin"]["model"], c["margin"]["live_fresh_s"]), ("staircase", 360))
        self.assertEqual((c["seed_mode"], c["price_basis"]), ("per_day", "raw"))
        self.assertNotIn("data", c)
        self.assertEqual(c["universe"], CHAIN)

    def test_it_is_a_run_config_without_data_otherwise(self):
        c = cfg(fill_model={"participation": "0.25", "slippage": {"kind": "bps", "x": "0.0005"}}, seed=7)
        self.assertEqual((c["fill_model"]["kind"], c["fill_model"]["participation_ppm"],
                          c["fill_model"]["slippage"]["bp"], c["seed"]), ("snapshot_quote", 250_000, 5, 7))

    def test_a_normalised_config_validates_again_to_itself(self):
        c = cfg(margin={"live_fresh_s": 90})
        self.assertEqual(sessionconfig.validate(copy.deepcopy(c)), c)
        self.assertEqual(c["margin"]["live_fresh_s"], 90)

    def test_session_config_refuses_data_packs(self):
        data = {"packs": [{"sha256": "a" * 64}]}
        with self.assertRaisesRegex(sessionconfig.SessionConfigError, "^session: a session has no `data`"):
            cfg(data=data)

    def test_an_unknown_key_is_refused_naming_it_and_its_section(self):
        cases = [({"capitl_paise": 1}, r"^session: unknown key 'capitl_paise'.*capital_paise"),
                 ({"margin": {"model": "flat", "live_fresh": 5}}, r"^margin: unknown key 'live_fresh'.*live_fresh_s"),
                 ({"fill_model": {"kind": "bar", "slipage": {}}}, r"^fill_model: unknown key 'slipage'"),
                 ({"fill_model": {"slippage": {"kind": "fixed_ticks", "k": 1, "z": 1}}},
                  r"^fill_model.slippage: unknown key 'z'"),
                 ({"costs": {"broker": "zerodha", "x": 1}}, r"^costs: unknown key 'x'"),
                 ({"policies": {"preopn": "queue"}}, r"^policies: unknown key 'preopn'"),
                 ({"strategy": {"kind": "python", "ref": "a:B", "param": {}}}, r"^strategy: unknown key 'param'"),
                 ({"universe": [{"symbol": ["NSE:X"]}]}, r"^universe\[0\]: unknown key 'symbol'"),
                 ({"universe": [{"chain": {"underlying": "NIFTY", "strike": 2}}]},
                  r"^universe\[0\].chain: unknown key 'strike'"),
                 ({"universe": [{"ids": [1], "symbols": ["NSE:X"]}]}, r"^universe\[0\]: needs exactly one")]
        for change, pattern in cases:
            with self.subTest(change=change), self.assertRaisesRegex(sessionconfig.SessionConfigError, pattern):
                cfg(**change)

    def test_the_rest_is_refused_by_field(self):
        cases = [({"schema": "stockify.run/1"}, "^schema"), ({"universe": []}, "^universe"),
                 ({"universe": "NSE:X"}, "^universe"), ({"universe": [3]}, r"^universe\[0\]"),
                 ({"price_basis": "adjusted"}, "^price_basis"), ({"seed_mode": "run"}, "^seed_mode"),
                 ({"margin": {"live_fresh_s": 0}}, "^margin.live_fresh_s"),
                 ({"margin": {"live_fresh_s": True}}, "^margin.live_fresh_s"),
                 ({"margin": {"model": "magic"}}, "^margin.model"), ({"capital_paise": 0}, "^capital_paise"),
                 ({"name": "../x"}, "^name")]
        for change, pattern in cases:
            with self.subTest(change=change), self.assertRaisesRegex(sessionconfig.SessionConfigError, pattern):
                cfg(**change)

    def test_a_catalogue_strategy_is_checked_as_for_a_run(self):
        bad = {"kind": "python", "ref": "stockify.strategies.sma_cross:SmaCross", "params": {"fast": "x"}}
        with self.assertRaises(sessionconfig.SessionConfigError) as ctx:
            cfg(strategy=bad)
        self.assertTrue(ctx.exception.problems)

    def test_a_run_config_is_unchanged(self):
        with self.assertRaisesRegex(runconfig.RunConfigError, "^data.packs"):
            runconfig.validate({"schema": "stockify.run/1", "name": "t", "strategy": {"kind": "python", "ref": "a:B"},
                                "capital_paise": 1, "fill_model": {"kind": "bar"}, "price_basis": "raw"})
        with self.assertRaisesRegex(runconfig.RunConfigError, "^run: unknown key 'universe'"):
            runconfig.validate({"schema": "stockify.run/1", "name": "t", "universe": []})

    def test_load_reads_a_file(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "session.json"
            f.write_text(json.dumps(raw()), encoding="utf-8")
            self.assertEqual(sessionconfig.load(f), cfg())
            f.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(sessionconfig.SessionConfigError, "not valid JSON"):
                sessionconfig.load(f)


class SessionBundleTests(unittest.TestCase):
    setUp = PackTest.setUp          # the NIFTY chain and future of the pack tests, DAY 2026-11-17

    def test_day_bundle_holds_every_strike_of_the_selected_expiry(self):
        b = bundle.resolve_session_day("s1", cfg(universe=CHAIN), DAY, None)
        ces = [i for i in b["instruments"] if i["kind"] == "CE"]
        # the fixture lists eleven strikes (23500..24500; its 24000 call is listed once), a `strikes: 2` window five
        self.assertEqual(len(ces), 11)
        self.assertEqual(len({i["strike_paise"] for i in ces}), len(ces))
        self.assertEqual(b["packs"], [])
        self.assertEqual([d["date"] for d in b["days"]], [DAY.isoformat()])
        self.assertEqual(b["days"][0]["seed"], bundle.day_seed(0, DAY))
        self.assertEqual(b["schema"], bundle.SCHEMA)

    def test_the_underlying_and_every_listed_future_come_along(self):
        b = bundle.resolve_session_day("s1", cfg(), DAY, None)
        by_kind = {k: [i for i in b["instruments"] if i["kind"] == k] for k in ("FUT", "INDEX")}
        self.assertEqual([i["id"] for i in by_kind["FUT"]], [self.fut])
        self.assertEqual([i["id"] for i in by_kind["INDEX"]], [self.idx])

    def test_session_universe_widens_a_chain_and_leaves_the_rest(self):
        wide = bundle.session_universe(CHAIN, DAY)
        narrow = bundle.session_universe([{"chain": {"underlying": "NIFTY", "expiry": "nearest"}}], DAY)
        self.assertEqual(wide, narrow | {self.fut})
        self.assertEqual(len(wide), 12)                # eleven calls and the future
        self.assertEqual(bundle.session_universe(["NSE:NIFTY 50", {"ids": [self.ce]}], DAY), {self.idx, self.ce})
        # a chain's own `futures` flag adds nothing a widened chain does not already hold
        self.assertEqual(bundle.session_universe([{"chain": {"underlying": "NIFTY", "futures": True}}], DAY), wide)

    def test_bundle_hash_is_stable(self):
        c = cfg()
        a = bundle.dumps(bundle.resolve_session_day("s1", c, DAY, None))
        b = bundle.dumps(bundle.resolve_session_day("s1", c, DAY, None))
        self.assertEqual(hashlib.sha256(a.encode()).hexdigest(), hashlib.sha256(b.encode()).hexdigest())
        self.assertNotEqual(a, bundle.dumps(bundle.resolve_session_day("s2", c, DAY, None)))

    def test_sod_state_is_embedded_verbatim(self):
        state = {"schema": "stockify.sod_state/1", "trade_date": "2026-11-16", "x": [1, {"b": 2, "a": 3}]}
        before = copy.deepcopy(state)
        b = bundle.resolve_session_day("s1", cfg(), DAY, state)
        self.assertEqual(b["sod_state"], before)
        self.assertEqual(state, before)
        self.assertIsNone(bundle.resolve_session_day("s1", cfg(), DAY, None)["sod_state"])

    def test_labels(self):
        b = bundle.resolve_session_day("s1", cfg(), DAY, None)
        self.assertIn("session:s1", b["labels"])
        self.assertIn("fill:snapshot_quote", b["labels"])
        self.assertIn("margin:staircase", b["labels"])
        self.assertIn("margin_span:sod_files_only", b["labels"])
        self.assertNotIn("refdata_stale", b["labels"])
        self.assertEqual(b["labels"], sorted(set(b["labels"])))
        stale = bundle.resolve_session_day("s1", cfg(), DAY, None, refdata_stale=True)
        self.assertEqual(set(stale["labels"]) - set(b["labels"]), {"refdata_stale"})
        flat = bundle.resolve_session_day("s1", cfg(margin={"model": "flat"}), DAY, None)
        self.assertNotIn("margin_span:sod_files_only", flat["labels"])
        self.assertNotIn("span", flat)

    def test_only_the_span_files_archived_by_the_refdata_job_are_in(self):
        archive_span(date(2026, 11, 16), "s", "202611162000")           # the previous session's end-of-day file
        archive_span(DAY, "i1", "202611170800")                          # before 08:30 IST
        archive_span(DAY, "i2", "202611171100")                          # intraday: not known at resolve time
        b = bundle.resolve_session_day("s1", cfg(), DAY, None)
        created = [f["created_ns"] for f in b["span"][DAY.isoformat()]]
        self.assertEqual(len(created), 2)
        self.assertLessEqual(max(created), bundle.timeutil.ist_ns(DAY, "08:30"))
        self.assertIn("margin_span:sod_files_only", b["labels"])

    def test_holiday_and_unsafe_id_raise(self):
        for day in (date(2026, 11, 24), date(2026, 11, 21), date(2026, 11, 22)):      # holiday, Saturday, Sunday
            with self.subTest(day=day), self.assertRaisesRegex(bundle.BundleError, "not an NSE trading day"):
                bundle.resolve_session_day("s1", cfg(), day, None)
        for bad in ("", "../x", "a/b", "a\\b", "a" * 65, "CON", ".x", "x.", " x", "x\n", None, 5):
            with self.subTest(session_id=bad), self.assertRaises(bundle.BundleError):
                bundle.resolve_session_day(bad, cfg(), DAY, None)

    def test_calendar_days_reach_expiry_week(self):
        b = bundle.resolve_session_day("s1", cfg(), DAY, None)
        self.assertEqual([d["date"] for d in b["prev_days"]], ["2026-11-16"])
        self.assertEqual([d["date"] for d in b["next_days"]], ["2026-11-18", "2026-11-19", "2026-11-20", "2026-11-23"])
        for d in (*b["prev_days"], *b["next_days"]):
            self.assertEqual(set(d), {"date", "groups", "mis_cutoff_ns", "day_end_ns"})
        self.assertEqual(b["prev_days"][0], next(d for d in bundle.resolve_days(
            date(2026, 11, 16), date(2026, 11, 16), b["broker_policy"])))

    def test_the_trading_day_count_matches_a_whole_run(self):
        """D-60: a stock future's offset from expiry is the same counted in a day bundle as in the days around it,
        with the holiday on 24 November between a Friday and the next Monday's expiry."""
        self.assertFalse(bundle._is_trading_day(date(2026, 11, 24)))
        for day, offset in ((date(2026, 11, 17), -4), (date(2026, 11, 20), -1), (date(2026, 11, 23), 0)):
            b = bundle.resolve_session_day("s1", cfg(universe=["NSE:NIFTY 50"]), day, None)
            whole = [d["date"] for d in bundle.resolve_days(date(2026, 11, 13), date(2026, 11, 30), b["broker_policy"])]
            row = {"settlement": "physical", "expiry": STOCK_EXPIRY, "actual_expiry": None}
            got = FlatMargin(b)._offset(row, day.isoformat())
            self.assertEqual((got, whole.index(day.isoformat()) - whole.index(STOCK_EXPIRY)), (offset, offset), day)

    def test_a_cash_day_still_has_a_next_day(self):
        b = bundle.resolve_session_day("s1", cfg(universe=["NSE:NIFTY 50"], margin={"model": "flat"}),
                                       date(2026, 11, 20), None)
        self.assertEqual(b["prev_days"][0]["date"], "2026-11-19")
        self.assertEqual(b["next_days"][0]["date"], "2026-11-23")

    def test_positions_left_open_stay_in_the_bundle(self):
        # the universe names no future, but yesterday's state holds a position in one and an order on a call
        state = {"ledger": {"positions": [{"instrument_id": self.fut, "q": 65}, {"instrument_id": self.idx, "q": 0}]},
                 "pending_orders": [{"instrument_id": self.ce}]}
        b = bundle.resolve_session_day("s1", cfg(universe=["NSE:NIFTY 50"]), DAY, state)
        self.assertEqual(sorted(i["id"] for i in b["instruments"]), sorted([self.idx, self.fut, self.ce]))
        self.assertEqual([i["id"] for i in bundle.resolve_session_day(
            "s1", cfg(universe=["NSE:NIFTY 50"]), DAY, None)["instruments"]], [self.idx])

    def test_both_runners_take_the_day_bundle(self):
        """No pack, an empty day: the oracle's constructor and the engine's both accept the bundle whole."""
        from stockify import engine
        from stockify.oracle import runner
        from stockify.strategy import Strategy
        b = json.loads(bundle.dumps(bundle.resolve_session_day("s1", cfg(), DAY, None)))
        runner.Engine(b, [], Strategy())
        if engine.available():
            import tempfile
            from pathlib import Path
            with tempfile.TemporaryDirectory() as d:
                bundle.write(b, Path(d))
                engine.load().Engine(str(Path(d) / "bundle.json"))

    def test_existing_run_bundles_unchanged(self):
        import inspect
        sig = inspect.signature(bundle.resolve_bundle)
        self.assertEqual(list(sig.parameters), ["cfg"])
        self.assertEqual(list(inspect.signature(bundle.span_section).parameters)[:3],
                         ["instruments_rows", "days", "skipped"])
        self.assertIsNone(inspect.signature(bundle.span_section).parameters["until_ns"].default)


if __name__ == "__main__":
    unittest.main()

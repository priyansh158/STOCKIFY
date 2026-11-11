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
    record = PackTest.record

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

    # The content hashes (sha256 of bundle.json as `bundle.write` writes it) of a run bundle over the pack tests' tick
    # pack, resolved before and after REG 5.1 / 5.2: measured on 69c320d (before the session code) and on this code.
    PINNED_RUN_BUNDLE = {"flat": "de43313c91ec3a4f2b41278184192bd378455f1889c411e344a77c44c634d2ab",
                         "staircase": "62f700a5e00bd4068146df1279eabf99e396c65071b78f12bfb788588444fd66"}

    def test_existing_run_bundles_unchanged(self):
        self.record()
        for model, pinned in self.PINNED_RUN_BUNDLE.items():
            with self.subTest(model=model):
                run_cfg = runconfig.load({
                    "schema": "stockify.run/1", "name": "pin",
                    "strategy": {"kind": "python", "ref": "stockify.strategy:Strategy", "params": {}},
                    "data": {"packs": [{"spec": {"dataset": "ticks", "from": DAY.isoformat(), "to": DAY.isoformat(),
                                                 "instruments": [{"ids": [self.ce, self.idx, self.fut]}]}}]},
                    "price_basis": "raw", "capital_paise": 10_000_000, "fill_model": {"kind": "snapshot_quote"},
                    "costs": {"allow_unverified": True}, "margin": {"model": model}})
                b = bundle.resolve_bundle(run_cfg)
                self.assertNotIn("prev_days", b)
                self.assertNotIn("next_days", b)
                self.assertEqual(hashlib.sha256((bundle.dumps(b) + chr(10)).encode()).hexdigest(), pinned)




# -- Fix round 1 (REG 5.2): a day's bundle loads the day before's state, whatever the state references -----------

DAY2 = date(2026, 11, 18)
EXPIRED_EXPIRY = "2026-11-17"          # the fixture's weekly calls expire on DAY; the next weekly is 24 November
NEXT_EXPIRY = "2026-11-24"


class AcrossExpiryTests(unittest.TestCase):
    def setUp(self):
        PackTest.setUp(self)
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.add_next_expiry()

    def add_next_expiry(self):
        from stockify.data import instruments
        from tests.stockify.data.test_packs import HEADER
        from tests.stockify.daemon import packets_fixture as pf
        rows = "".join(f"{95000 + i},{50 + i},NIFTY26N24{s}CE,NIFTY,0,{NEXT_EXPIRY},{s},0.05,65,CE,NFO-OPT,NFO\n"
                       for i, s in enumerate(range(23500, 24600, 100)))
        instruments.sync(date(2026, 11, 17), {
            "NSE": f"{HEADER}\n{pf.NIFTY_INDEX},1,NIFTY 50,NIFTY 50,0,,0,0,0,EQ,INDICES,NSE\n",
            "NFO": (f"{HEADER}\n{pf.NIFTY_CE},2,NIFTY26N1724000CE,NIFTY,0,{EXPIRED_EXPIRY},24000,0.05,65,CE,NFO-OPT,NFO\n"
                    f"9001,3,NIFTY26NOVFUT,NIFTY,0,2026-11-24,0,0.1,65,FUT,NFO-FUT,NFO\n" + rows),
        }, underlyings={"NIFTY": "NSE:NIFTY 50"})

    def session_cfg(self, actions=(), **kw):
        return cfg(strategy={"kind": "script", "actions": list(actions)}, margin={"model": "flat"},
                   fill_model={"kind": "snapshot_ltp"}, **kw)

    def day_one_state(self, *, buy_and_sell: bool = True) -> str:
        """Day 1 (the day the weekly calls expire) run on the oracle: a call bought and sold again (a flat position
        with market data and bars), the future and the index quoted. Returns day 1's eod_state()."""
        from stockify import golden, timeutil
        from stockify.oracle import runner
        from stockify.strategies._script import Script
        ce = self.ce

        def at(hhmm):
            return timeutil.ist_ns(DAY, hhmm)
        acts = []
        if buy_and_sell:
            acts = [{"at_ns": at("09:16:30"), "place": {"instrument": ce, "side": "BUY", "qty": 65, "product": "NRML",
                                                        "type": "LIMIT", "price": 13_000}},
                    {"at_ns": at("09:20:30"), "place": {"instrument": ce, "side": "SELL", "qty": 65, "product": "NRML",
                                                        "type": "LIMIT", "price": 11_000}}]
        events = []
        for n in range(30):
            hhmm = f"09:{15 + n:02d}"
            events.append({"kind": "TICK_QUOTE", "instrument": ce, "t": f"2026-11-17T{hhmm}:10+05:30",
                           "ltp": 12_000 + n, "volume": 1000 * (n + 1)})
            events.append({"kind": "BAR", "instrument": self.fut, "interval_s": 60, "start": f"2026-11-17T{hhmm}:00+05:30",
                           "o": 12_000, "h": 12_050, "l": 11_950, "c": 12_000 + n, "v": 100})
            events.append({"kind": "INDEX", "instrument": self.idx, "t": f"2026-11-17T{hhmm}:30+05:30",
                           "value": 2_400_000 + n})
        pack = golden.build_pack({"id": "day1", "events": events}, self.folder)
        c = self.session_cfg(acts)
        b = json.loads(bundle.dumps(bundle.resolve_session_day("s1", c, DAY, None)))
        e = runner.Engine(b, runner.load_records([pack]), Script(b["strategy"]["actions"]))
        e.run()
        return e.eod_state()

    def day_two_bundle(self, state: str, *, universe=None) -> dict:
        c = self.session_cfg(universe=universe or CHAIN)
        return json.loads(bundle.dumps(bundle.resolve_session_day("s1", c, DAY2, json.loads(state))))

    def load_on_both(self, b: dict):
        """The day's bundle, state inside: the oracle's constructor applies it, and so does the C++ engine's."""
        from stockify import engine
        from stockify.oracle import runner
        from stockify.strategy import Strategy
        runner.Engine(copy.deepcopy(b), [], Strategy())
        if engine.available():
            bundle.write(copy.deepcopy(b), self.folder)
            engine.load().Engine(str(self.folder / "bundle.json"))

    def test_the_state_after_a_flat_expired_call_loads_the_next_day(self):
        state = self.day_one_state()
        refs = json.loads(state)
        self.assertEqual([p["q"] for p in refs["ledger"]["positions"]], [0])          # flat, not open
        self.assertIn(str(self.ce), refs["market"])
        self.assertTrue(any(k.startswith(f"{self.fut}:") for k in refs["bar_history"]))
        b2 = self.day_two_bundle(state)
        ids = {i["id"] for i in b2["instruments"]}
        self.assertIn(self.ce, ids)                                                  # expired on day 1, still listed
        self.assertEqual(sum(1 for i in b2["instruments"] if i["kind"] == "CE" and i["expiry"] == NEXT_EXPIRY), 11)
        self.assertLessEqual(bundle._sod_instruments(refs), ids)                     # all that the state names
        self.load_on_both(b2)

    def test_the_expired_call_keeps_its_dated_row(self):
        b2 = self.day_two_bundle(self.day_one_state())
        row = next(i for i in b2["instruments"] if i["id"] == self.ce)
        self.assertEqual((row["kind"], row["expiry"]), ("CE", EXPIRED_EXPIRY))
        self.assertTrue(row["specs"] and row["last_trading_ns"])

    def test_every_instrument_a_state_names_is_in_the_bundle(self):
        """Each field the loader checks, one at a time: its instrument (expired on day 1) must come along."""
        base = json.loads(self.day_one_state())
        ce = self.ce
        puts = {
            "ledger position": lambda s: s["ledger"]["positions"].append(
                {**base["ledger"]["positions"][0], "instrument_id": ce}),
            "market": lambda s: s["market"].setdefault(str(ce), next(iter(base["market"].values()))),
            "bar_history": lambda s: s["bar_history"].setdefault(f"{ce}:60", [[1, 1, 1, 1, 1, 1, 0]]),
            "dp_done": lambda s: s["dp_done"].append([ce, "2026-11-17"]),
            "built_bars": lambda s: s["built_bars"].append([ce, 60, 1, 1, 1, 1, 1, 1, 0]),
            "subscriptions.ticks": lambda s: s["subscriptions"]["ticks"].append(ce),
            "subscriptions.bars": lambda s: s["subscriptions"]["bars"].setdefault(str(ce), [60]),
            "fsp_seen": lambda s: s["fsp_seen"].append([ce, "2026-11-17", 100]),
        }
        for name, put in puts.items():
            s = copy.deepcopy(base)
            s["ledger"]["positions"] = []
            s["market"], s["bar_history"] = {}, {}
            s["dp_done"], s["built_bars"], s["fsp_seen"] = [], [], []
            s["subscriptions"] = {"ticks": [], "bars": {}}
            put(s)
            with self.subTest(field=name):
                b2 = bundle.resolve_session_day("s1", self.session_cfg(universe=["NSE:NIFTY 50"]), DAY2, s)
                self.assertIn(ce, {i["id"] for i in b2["instruments"]})

    def test_an_instrument_with_no_row_fails_the_build_naming_it(self):
        s = json.loads(self.day_one_state())
        s["subscriptions"]["ticks"].append(987654)
        with self.assertRaisesRegex(bundle.BundleError, r"987654.*2026-11-18|2026-11-18.*987654"):
            bundle.resolve_session_day("s1", self.session_cfg(), DAY2, s)


class ReplayEqualsLiveTests(unittest.TestCase):
    """REG 5.2 (review minor 2): what a day's bundle holds is what was archived at the refdata job's time, so a
    resolve at 08:30 and a replay of the same day months later give the same bundle."""

    setUp = PackTest.setUp

    ELM = "OPTIDX,NIFTY,17-NOV-2026,24000,CE,0,2\nFUTIDX,NIFTY,24-NOV-2026,-1,XX,0,3\n"

    def test_a_live_resolve_and_a_later_replay_give_the_same_bundle(self):
        from tests.stockify.test_bundle_span import archive_elm
        archive_span(date(2026, 11, 16), "s", "202611162000")
        archive_span(DAY, "i1", "202611170800")
        live = bundle.dumps(bundle.resolve_session_day("s1", cfg(), DAY, None))
        archive_elm(DAY, self.ELM)                       # published for the day later: not there at 08:30
        archive_span(DAY, "i2", "202611171100")
        replay = bundle.dumps(bundle.resolve_session_day("s1", cfg(), DAY, None))
        self.assertEqual(live, replay)
        self.assertEqual(json.loads(live)["elm"], {})

    def test_the_label_says_which_files_were_left_out(self):
        b = bundle.resolve_session_day("s1", cfg(), DAY, None)
        self.assertIn("margin_span:sod_files_only", b["labels"])
        self.assertIn("margin_elm:sod_files_only", b["labels"])
        flat = bundle.resolve_session_day("s1", cfg(margin={"model": "flat"}), DAY, None)
        self.assertNotIn("margin_elm:sod_files_only", flat["labels"])


if __name__ == "__main__":
    unittest.main()

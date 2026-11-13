"""MEZ 5.1 (D-69, M5.6): a live margin answer from Kite prices the book, only through a journaled record.

* `live_margin`: the portfolio hash, the payload check, `LiveAnswers` (latest per hash, freshness at the record's time);
* `LiveWithFallbackMargin` (oracle): L1 prices the current book when a fresh answer matches its hash, the staircase
  otherwise; the pre-trade increment (`pair`) never uses L1; the L1 row of margin_daily.csv;
* oracle and engine on one world: byte-identical folders; L1 changes the available funds, deterministically;
* a bad payload is a run error on both sides; `Engine.margin_now()` names the hash the oracle computes;
* 100 random worlds with random answers agree.
"""
from __future__ import annotations

import copy
import csv
import gc
import hashlib
import json
import random
import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine, golden, numerics, runconfig, sessionconfig
from stockify.fno import live_margin
from stockify.fno.live_margin import LiveAnswers, LiveWithFallbackMargin, portfolio_sha256
from stockify.fno.staircase import StaircaseMargin
from stockify.oracle import run_bundle as oracle_run
from tests.stockify.test_engine_parity import assert_same_artifacts
from tests.stockify.test_fo_mis_margin import CALL, DAY, FUT, LOT, SHARE, WING, flat_world
from tests.stockify.test_staircase import DAY as SDAY
from tests.stockify.test_staircase import ROWS, FakeEngine, nrml, price

NS = 10**9


def payload(legs, span=3_000_000, exposure=1_000_000, additional=0, premium=0, requested_ns=0, source="kite_basket"):
    legs = sorted([list(x) for x in legs])
    return {"portfolio_sha256": portfolio_sha256([tuple(x) for x in legs]), "legs": legs, "span_paise": span,
            "exposure_paise": exposure, "additional_paise": additional, "option_premium_paise": premium,
            "requested_ns": requested_ns, "source": source}


class PayloadTests(unittest.TestCase):
    def test_hash_is_sha256_of_the_canonical_sorted_legs(self):
        want = hashlib.sha256(b"[[2,-65],[3,65]]").hexdigest()
        self.assertEqual(portfolio_sha256([(3, 65), (2, -65)]), want)
        self.assertEqual(portfolio_sha256([]), hashlib.sha256(b"[]").hexdigest())

    def test_good_payload_passes_and_bad_ones_are_named(self):
        good = payload([(3, 65)])
        self.assertEqual(live_margin.check_payload(good), good)
        bad = {
            "not an object": [],
            "a missing key": {k: v for k, v in good.items() if k != "source"},
            "an extra key": {**good, "x": 1},
            "a float amount": {**good, "span_paise": 1.0},
            "a bool amount": {**good, "span_paise": True},
            "a negative amount": {**good, "exposure_paise": -1},
            "a huge amount": {**good, "additional_paise": 10**18 + 1},
            "another source": {**good, "source": "kite_order"},
            "an upper-case hash": {**good, "portfolio_sha256": good["portfolio_sha256"].upper()},
            "a hash of other legs": {**good, "portfolio_sha256": portfolio_sha256([(3, 66)])},
            "unsorted legs": payload([(3, 65), (2, 65)]) | {"legs": [[3, 65], [2, 65]]},
            "a zero leg": payload([(3, 0)]),
            "a repeated instrument": payload([(3, 65), (3, 65)]),
            "a leg with three items": {**good, "legs": [[3, 65, 1]]},
            "a negative time": {**good, "requested_ns": -1},
        }
        for what, p in bad.items():
            with self.subTest(what), self.assertRaises(live_margin.PayloadError):
                live_margin.check_payload(p)


class FreshSBoundTests(unittest.TestCase):
    RUN = {"schema": "stockify.run/1", "name": "t", "strategy": {"kind": "script", "actions": []},
           "data": {"packs": [{"sha256": "0" * 64}]}, "capital_paise": 10**7, "fill_model": {"kind": "bar"},
           "price_basis": "raw"}
    SESSION = {"schema": "stockify.session/1", "name": "s", "strategy": {"kind": "script", "actions": []},
               "universe": ["NSE:RELIANCE"], "capital_paise": 10**8}

    def test_live_fresh_s_is_one_to_a_day_and_only_for_the_live_model(self):
        live = {"model": "live_with_fallback"}
        for fresh in (1, 360, 86_400):
            self.assertEqual(runconfig.validate(self.RUN | {"margin": live | {"live_fresh_s": fresh}})["margin"]
                             ["live_fresh_s"], fresh)
            self.assertEqual(sessionconfig.validate(self.SESSION | {"margin": {"live_fresh_s": fresh}})["margin"]
                             ["live_fresh_s"], fresh)
        for fresh in (0, -1, 86_401, 10**10, True, 1.5, "60"):
            with self.subTest(fresh=fresh):
                with self.assertRaises(runconfig.RunConfigError) as r:
                    runconfig.validate(self.RUN | {"margin": live | {"live_fresh_s": fresh}})
                self.assertIn("margin.live_fresh_s", str(r.exception))
                with self.assertRaises(sessionconfig.SessionConfigError) as r:
                    sessionconfig.validate(self.SESSION | {"margin": {"live_fresh_s": fresh}})
                self.assertIn("margin.live_fresh_s", str(r.exception))
        for model in ("flat", "spanlite", "span_exact", "staircase"):
            with self.subTest(model=model):
                with self.assertRaises(runconfig.RunConfigError) as r:
                    runconfig.validate(self.RUN | {"margin": {"model": model, "live_fresh_s": 60}})
                self.assertIn("margin.live_fresh_s", str(r.exception))
                self.assertIn("live_with_fallback", str(r.exception))
                with self.assertRaises(sessionconfig.SessionConfigError) as r:
                    sessionconfig.validate(self.SESSION | {"margin": {"model": model, "live_fresh_s": 60}})
                self.assertIn("margin.live_fresh_s", str(r.exception))
        with self.assertRaises(runconfig.RunConfigError):                  # no model given: a run's default is staircase
            runconfig.validate(self.RUN | {"margin": {"live_fresh_s": 60}})


class AnswersTests(unittest.TestCase):
    def test_latest_answer_per_hash_and_freshness_from_its_record_time(self):
        a = LiveAnswers()
        p1, p2 = payload([(3, 65)], span=1), payload([(3, 65)], span=2)
        a.put(p1, 100)
        self.assertEqual(a.fresh(p1["portfolio_sha256"], 100, 50), p1)
        self.assertEqual(a.fresh(p1["portfolio_sha256"], 150, 50), p1)          # exactly as old as allowed
        self.assertIsNone(a.fresh(p1["portfolio_sha256"], 151, 50))             # one ns older
        self.assertIsNone(a.fresh(p1["portfolio_sha256"], 99, 50))              # from before it was known
        self.assertIsNone(a.fresh(portfolio_sha256([(3, 66)]), 100, 50))        # another portfolio
        a.put(p2, 120)
        self.assertEqual(a.fresh(p1["portfolio_sha256"], 130, 50)["span_paise"], 2)


def _bundle(fresh_s: int | None = 360) -> dict:
    margin = {"model": "live_with_fallback"} | ({} if fresh_s is None else {"live_fresh_s": fresh_s})
    return FakeEngine().bundle | {"run": {"margin": margin}}


class ModelTests(unittest.TestCase):
    LEGS = [(10, -65), (11, -65)]

    def setUp(self):
        self.e = FakeEngine()
        self.answers = LiveAnswers()
        self.m = LiveWithFallbackMargin(self.e.bundle, self.e, self.answers)
        self.plain = StaircaseMargin(self.e.bundle, self.e)
        self.sha = portfolio_sha256(self.LEGS)

    def req(self, m, legs=None):
        return m.requirement(nrml(legs or self.LEGS), ROWS.__getitem__, price, SDAY)

    def test_without_an_answer_it_is_the_staircase(self):
        self.assertEqual(self.req(self.m), self.req(self.plain))
        self.assertEqual(self.m.breakdown(nrml(self.LEGS), ROWS.__getitem__, price, SDAY),
                         self.plain.breakdown(nrml(self.LEGS), ROWS.__getitem__, price, SDAY))
        self.assertIn("margin_live:L1_answers=0", self.m.labels)

    def test_fresh_matching_answer_is_used_and_counted(self):
        self.answers.put(payload(self.LEGS, span=5_000_000, exposure=700_000, additional=30_000, premium=999),
                         self.e.now)
        self.assertEqual(self.req(self.m), 5_730_000)                         # span + exposure + additional, no premium
        self.assertEqual(self.req(self.m), 5_730_000)
        self.assertEqual(self.m.l1_answers, 1)                                # one answer, however often it is used
        self.assertIn("margin_live:L1_answers=1", self.m.labels)
        [row] = self.m.breakdown(nrml(self.LEGS), ROWS.__getitem__, price, SDAY)
        self.assertEqual((row.underlying_id, row.step, row.scan, row.somc, row.elm, row.addons, row.total),
                         (0, "L1_kite", 5_000_000, 0, 700_000, 30_000, 5_730_000))
        self.assertEqual(self.m.l1_answers, 1)                                # the row is the same answer
        self.answers.put(payload(self.LEGS, span=6_000_000, exposure=0), self.e.now)
        self.assertEqual(self.req(self.m), 6_000_000)
        self.assertEqual(self.m.l1_answers, 2)

    def test_answer_for_another_portfolio_is_ignored(self):
        self.answers.put(payload([(10, -65)]), self.e.now)
        self.assertEqual(self.req(self.m), self.req(self.plain))
        self.assertEqual(self.m.l1_answers, 0)

    def test_stale_answer_falls_back_to_staircase(self):
        self.answers.put(payload(self.LEGS), self.e.now)
        self.e.now += 361 * NS
        self.assertEqual(self.req(self.m), self.req(self.plain))
        self.assertEqual(self.m.l1_answers, 0)

    def test_pre_trade_increment_never_uses_l1(self):
        before, after = nrml([(10, -65)]), nrml(self.LEGS)
        want = self.plain.pair(before, after, ROWS.__getitem__, price, price, SDAY)
        self.answers.put(payload([(10, -65)], span=1, exposure=0), self.e.now)
        self.answers.put(payload(self.LEGS, span=1, exposure=0), self.e.now)
        self.assertEqual(self.m.pair(before, after, ROWS.__getitem__, price, price, SDAY), want)
        self.assertEqual(self.m.l1_answers, 0)

    def test_an_empty_book_is_zero_and_never_l1(self):
        self.answers.put(payload([]), self.e.now)
        self.assertEqual(self.m.requirement([], ROWS.__getitem__, price, SDAY), 0)
        self.assertEqual(self.m.l1_answers, 0)

    def test_only_the_staircase_group_legs_are_hashed(self):
        """MIS shares, a zero leg and an unmarginable kind are not in the hash: only NRML FUT/CE/PE with an underlying."""
        legs = [(10, "NRML", -65), (11, "NRML", 0), (2, "MIS", 0)]
        self.answers.put(payload([(10, -65)]), self.e.now)
        self.assertEqual(self.m.requirement(legs, ROWS.__getitem__, price, SDAY), 4_000_000)


class SessionDefaultTests(unittest.TestCase):
    SESSION = {"schema": "stockify.session/1", "name": "s", "strategy": {"kind": "script", "actions": []},
               "universe": ["NSE:RELIANCE"], "capital_paise": 10**8}

    def test_model_is_listed_and_the_session_default(self):
        self.assertEqual(runconfig.MARGIN_MODELS[-1], "live_with_fallback")
        self.assertEqual(runconfig.DEFAULTS["margin"]["model"], "staircase")     # a run keeps the staircase
        cfg = sessionconfig.validate(copy.deepcopy(self.SESSION))
        self.assertEqual(cfg["margin"], {"model": "live_with_fallback", "live_fresh_s": 360})
        s = copy.deepcopy(self.SESSION) | {"margin": {"model": "staircase"}}
        self.assertEqual(sessionconfig.validate(s)["margin"], {"model": "staircase"})   # no live_fresh_s to carry


# ── Both engines on one world ────────────────────────────────────────────────────────────────────────────────────

def at(hhmm: str) -> str:
    return f"{DAY}T{hhmm}+05:30"


def order(at_hhmm: str, iid: int, side: str, qty: int, product: str = "NRML") -> dict:
    return {"at_ns": golden.ts(at(at_hhmm)), "place": {"instrument": iid, "side": side, "qty": qty,
                                                         "product": product, "type": "MARKET"}}


def answer(hhmm: str, p: dict) -> dict:
    return {"kind": "MARGIN_REQUIREMENT", "instrument": 0, "t": at(hhmm), "payload": p}


def world(actions, answers, *, capital=30_000_000, fresh_s=360, model="live_with_fallback") -> dict:
    g = flat_world()
    g["run"] = {**g["run"], "capital_paise": capital,
                "margin": {"model": model} | ({"live_fresh_s": fresh_s} if model == "live_with_fallback" else {})}
    g["strategy"] = {"actions": actions}
    g["events"] = g["events"] + answers
    return g


def run_both(g: dict, folder: Path) -> tuple[Path, Path]:
    pack = golden.build_pack(g, folder)
    bundle = golden.build_bundle(g)
    oracle_run(copy.deepcopy(bundle), [pack], folder / "oracle", report=False)
    engine.run_bundle(copy.deepcopy(bundle), [pack], folder / "engine", report=False)
    return folder / "oracle", folder / "engine"


def rows(run: Path, name: str) -> list[dict]:
    with (run / name).open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def labels(run: Path) -> list[str]:
    return json.loads((run / "manifest.json").read_text(encoding="utf-8"))["labels"]


def l1_answers(run: Path) -> int:
    for x in labels(run):
        if x.startswith("margin_live:L1_answers="):
            return int(x.split("=")[1])
    raise AssertionError("no margin_live label")


FUT_ORDER = order("09:17:00", FUT, "BUY", LOT)                       # fills at the 09:17 bar's open
SHARE_ORDER = order("09:25:00", SHARE, "BUY", 150, "CNC")            # ₹1,50,000: needs ₹1,50,000 of available funds
BOOK = [(FUT, LOT)]
LOW = payload(BOOK, span=3_000_000, exposure=1_000_000)            # ₹40,000 against the flat model's ₹1,95,000


class BothEngines(unittest.TestCase):
    def check(self, g: dict) -> tuple[Path, Path, tempfile.TemporaryDirectory]:
        tmp = tempfile.TemporaryDirectory(prefix="live-margin-")
        self.addCleanup(tmp.cleanup)
        o, e = run_both(g, Path(tmp.name))
        assert_same_artifacts(self, o, e)
        self.assertEqual(labels(o), labels(e))
        return o, e, tmp

    def rejects(self, g: dict) -> list[str]:
        o, _, _ = self.check(g)
        return [r["reason"] for r in rows(o, "orders.csv") if r["status"] == "REJECTED"]

    def test_fresh_matching_answer_is_used_and_counted(self):
        o, e, _ = self.check(world([FUT_ORDER], [answer("09:20:00", LOW)], fresh_s=86_400))
        self.assertGreater(l1_answers(o), 0)
        self.assertEqual(l1_answers(o), l1_answers(e))
        self.assertEqual(rows(o, "margin_daily.csv")[0]["step"], "L1_kite")

    def test_answer_for_another_portfolio_is_ignored(self):
        other = payload([(FUT, 2 * LOT)])
        o, _, _ = self.check(world([FUT_ORDER], [answer("09:20:00", other)], fresh_s=86_400))
        self.assertEqual(l1_answers(o), 0)
        self.assertEqual([r["step"] for r in rows(o, "margin_daily.csv")], ["B5_flat"])

    def test_stale_answer_falls_back_to_staircase(self):
        o, _, _ = self.check(world([FUT_ORDER, SHARE_ORDER], [answer("09:20:00", LOW)], fresh_s=360))
        self.assertEqual([r["step"] for r in rows(o, "margin_daily.csv")], ["B5_flat"])   # DAY_END is hours later
        self.assertGreater(l1_answers(o), 0)                                              # but it priced the 09:25 check

    def test_l1_changes_available_funds_deterministically(self):
        g = world([FUT_ORDER, SHARE_ORDER], [], fresh_s=360)
        with_answer = world([FUT_ORDER, SHARE_ORDER], [answer("09:24:00", LOW)], fresh_s=360)
        self.assertEqual(self.rejects(g), ["R_MARGIN"])
        self.assertEqual(self.rejects(with_answer), [])
        with tempfile.TemporaryDirectory(prefix="live-margin-twice-") as a, \
                tempfile.TemporaryDirectory(prefix="live-margin-twice-") as b:
            first, _ = run_both(copy.deepcopy(with_answer), Path(a))
            second, _ = run_both(copy.deepcopy(with_answer), Path(b))
            for name in ("orders.csv", "fills.csv", "equity.csv", "margin_daily.csv", "events_log.txt"):
                self.assertEqual((first / name).read_bytes(), (second / name).read_bytes(), name)

    def test_an_answer_that_arrives_too_late_changes_nothing(self):
        late = world([FUT_ORDER, SHARE_ORDER], [answer("09:26:00", LOW)], fresh_s=360)
        self.assertEqual(self.rejects(late), ["R_MARGIN"])

    def test_l1_row_rule_at_day_end(self):
        fresh = world([FUT_ORDER], [answer("09:20:00", LOW)], fresh_s=86_400)
        o, _, _ = self.check(fresh)
        self.assertEqual([(r["underlying_id"], r["model"], r["step"], r["scan"], r["somc"], r["elm"], r["addons"],
                           r["total"]) for r in rows(o, "margin_daily.csv")],
                         [("0", "live_with_fallback", "L1_kite", "3000000", "0", "1000000", "0", "4000000")])
        o, _, _ = self.check(world([FUT_ORDER], [answer("09:20:00", LOW)], fresh_s=360))
        [row] = rows(o, "margin_daily.csv")
        self.assertEqual((row["underlying_id"], row["model"], row["step"]), ("1", "live_with_fallback", "B5_flat"))

    def test_staircase_world_with_answers_is_unchanged_by_them(self):
        """Under another model a margin_requirement record is read, checked and ignored."""
        with_a = world([FUT_ORDER, SHARE_ORDER], [answer("09:24:00", LOW)], model="staircase")
        self.assertEqual(self.rejects(with_a), ["R_MARGIN"])

    def test_a_bad_payload_is_a_run_error_on_both_sides(self):
        for bad in ({**LOW, "span_paise": 1.5}, {**LOW, "source": "x"}, {**LOW, "portfolio_sha256": "0" * 64}, [1]):
            with self.subTest(bad), tempfile.TemporaryDirectory(prefix="live-margin-bad-") as tmp:
                g = world([FUT_ORDER], [answer("09:20:00", bad)])
                pack = golden.build_pack(g, Path(tmp))
                bundle = golden.build_bundle(g)
                with self.assertRaises(Exception) as o:  # noqa: PT027
                    oracle_run(copy.deepcopy(bundle), [pack], Path(tmp) / "oracle", report=False)
                with self.assertRaises(Exception) as e:  # noqa: PT027
                    engine.run_bundle(copy.deepcopy(bundle), [pack], Path(tmp) / "engine", report=False)
                self.assertIn("margin_requirement: ", str(o.exception))
                self.assertIn("margin_requirement: ", str(e.exception))

    def refusal(self, g: dict) -> tuple[str, str]:
        """The exact words of the oracle's and the engine's refusal of one world."""
        with tempfile.TemporaryDirectory(prefix="live-margin-bad-") as tmp:
            pack = golden.build_pack(g, Path(tmp))
            bundle = golden.build_bundle(g)
            with self.assertRaises(Exception) as o:  # noqa: PT027
                oracle_run(copy.deepcopy(bundle), [pack], Path(tmp) / "oracle", report=False)
            with self.assertRaises(Exception) as e:  # noqa: PT027
                engine.run_bundle(copy.deepcopy(bundle), [pack], Path(tmp) / "engine", report=False)
            return str(o.exception), str(e.exception)

    def test_both_runners_refuse_in_the_same_words(self):
        legs = "margin_requirement: legs must be a list of [instrument, units] pairs of whole numbers"
        amount = "margin_requirement: span_paise must be a whole number of paise from 0 to 1000000000000000000"
        zero = "0" * 64
        cases = {
            "units 2**64": ({**LOW, "legs": [[FUT, 2**64]], "portfolio_sha256": zero}, legs),
            "units -2**63-1": ({**LOW, "legs": [[FUT, -2**63 - 1]], "portfolio_sha256": zero}, legs),
            "a string amount": ({**LOW, "span_paise": "5"}, amount),
            "legs not a list": ({**LOW, "legs": 5}, legs),
            "a float units value": ({**LOW, "legs": [[FUT, 65.0]], "portfolio_sha256": zero}, legs),
        }
        for what, (bad, text) in cases.items():
            with self.subTest(what):
                o, e = self.refusal(world([FUT_ORDER], [answer("09:20:00", bad)]))
                self.assertIn(text, o)
                self.assertIn(text, e)
                self.assertEqual(o.split("margin_requirement: ")[1], e.split("margin_requirement: ")[1])

    def test_the_record_must_be_on_instrument_zero(self):
        ev = answer("09:20:00", LOW) | {"instrument": FUT}
        o, e = self.refusal(world([FUT_ORDER], [ev]))
        for text in (o, e):
            self.assertIn("margin_requirement: the record's instrument must be 0", text)

    def test_margin_now_changes_no_label_and_no_state(self):
        g = world([FUT_ORDER], [answer("09:20:00", LOW)], fresh_s=86_400)
        with tempfile.TemporaryDirectory(prefix="live-margin-now-") as tmp:
            folder = Path(tmp)
            pack = golden.build_pack(g, folder)
            (folder / "bundle.json").write_text(numerics.dumps_canonical(golden.build_bundle(g)) + "\n",
                                                encoding="utf-8")
            e = engine.load().Engine(str(folder / "bundle.json"))
            e.add_pack(str(pack))
            from stockify.strategies._script import Script
            e.set_strategy_py(Script(copy.deepcopy(g["strategy"]["actions"])))
            e.run()
            before = (e.labels(), e.margin_now())
            for _ in range(3):
                e.margin_now()
            self.assertEqual((e.labels(), e.margin_now()), before)
            del e
            gc.collect()

    def test_margin_now_names_the_hash_and_the_rows(self):
        g = world([FUT_ORDER], [answer("09:20:00", LOW)], fresh_s=86_400)
        with tempfile.TemporaryDirectory(prefix="live-margin-now-") as tmp:
            folder = Path(tmp)
            pack = golden.build_pack(g, folder)
            bundle = golden.build_bundle(g)
            (folder / "bundle.json").write_text(numerics.dumps_canonical(bundle) + "\n", encoding="utf-8")
            mod = engine.load()
            e = mod.Engine(str(folder / "bundle.json"))
            e.add_pack(str(pack))
            from stockify.strategies._script import Script
            e.set_strategy_py(Script(copy.deepcopy(g["strategy"]["actions"])))
            e.run()
            now = e.margin_now()
            self.assertEqual(now["legs"], [[FUT, LOT]])
            self.assertEqual(now["portfolio_sha256"], portfolio_sha256([(FUT, LOT)]))
            self.assertTrue(now["l1"])
            [row] = now["fallback"]
            self.assertEqual((row["underlying_id"], row["step"]), (1, "B5_flat"))
            self.assertEqual(row["total"], 19_500_000)
            del e
            gc.collect()                                  # the engine holds the pack file open


# ── Random worlds with random answers ────────────────────────────────────────────────────────────────────────────

def random_world(seed: int) -> dict:
    rng = random.Random(seed)
    contracts = {CALL: LOT, FUT: LOT, WING: LOT}
    minutes = sorted(rng.sample(range(16, 29), rng.randint(2, 5)))
    actions, answers, book = [], [], {}
    for m in minutes:
        iid = rng.choice([FUT, FUT, CALL, WING, SHARE])
        side = rng.choice(["BUY", "SELL"])
        if iid == SHARE:
            actions.append(order(f"09:{m}:00", SHARE, "BUY", rng.choice([10, 150, 400]), "CNC"))
            continue
        qty = contracts[iid] * rng.choice([1, 1, 2])
        actions.append(order(f"09:{m}:00", iid, side, qty, rng.choice(["NRML", "NRML", "MIS"])))
        book[iid] = book.get(iid, 0) + (qty if side == "BUY" else -qty)
        legs = sorted((i, u) for i, u in book.items() if u)
        for _ in range(rng.randint(0, 2)):                           # answers for the book as nominally filled
            t = min(m * 60 + 60 + rng.randint(0, 200), 30 * 60 + 59)
            hhmm = f"09:{t // 60}:{t % 60:02d}"
            amounts = [rng.choice([0, 1_000, 500_000, 3_000_000, 25_000_000]) for _ in range(3)]
            answers.append(answer(hhmm, payload(legs, *amounts, premium=rng.choice([0, 5_000]),
                                                requested_ns=golden.ts(at(hhmm)) - NS)))
    if rng.random() < 0.5:                                           # a decoy for a book that never exists
        answers.append(answer("09:21:00", payload([(FUT, 7 * LOT)])))
    return world(actions, answers, capital=rng.choice([20_000_000, 30_000_000, 100_000_000]),
                 fresh_s=rng.choice([30, 360, 86_400]))


class FuzzTests(unittest.TestCase):
    def test_fuzz_worlds_with_random_answers_agree(self):
        used = 0
        for seed in range(100):
            with self.subTest(seed=seed), tempfile.TemporaryDirectory(prefix=f"live-fuzz-{seed}-") as tmp:
                o, e = run_both(random_world(seed), Path(tmp))
                assert_same_artifacts(self, o, e)
                self.assertEqual(labels(o), labels(e))
                used += l1_answers(o) > 0
        self.assertGreaterEqual(used, 30, "the worlds must actually use L1")


if __name__ == "__main__":
    unittest.main()

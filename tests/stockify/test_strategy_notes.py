"""CH 6.6: the shipped Python strategies say why they trade (`note=`) and draw their own lines (`ctx.plot`).

Every sentence here is also the contract of the C++ twins (orb_cpp, sma_cross_cpp; C7): they must match it byte for
byte, so each one is built only from `fmt_price`, `fmt_qty`, whole numbers and plain text. The expected strings are
written out by hand from the fixture prices (paise): 10050 is 100.50.
"""
from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import engine, golden, strategy as S
from stockify.oracle import run_bundle as oracle_run
from stockify.oracle.notes import NOTE_MAX, clean_note
from stockify.strategies.buy_and_hold import BuyAndHold
from stockify.strategies.eod_short_straddle import EodShortStraddle
from stockify.strategies.opening_range_breakout import OpeningRangeBreakout
from stockify.strategies.sma_cross import SmaCross
from stockify.strategy import fmt_price, fmt_qty
from tests.stockify import test_option_strategies as options
from tests.stockify.test_engine_parity import assert_same_artifacts
from tests.stockify.test_strategies import DAYS, daily, minute, walk, world

SMA_CLOSES = [10000, 10000, 10000, 10100, 10200, 10300, 10150, 10000, 9900, 9850]      # up, then down
FUT = {"id": 1, "kind": "FUT", "tradingsymbol": "TESTFUT", "underlying": "TESTU", "expiry": "2026-03-26",
       "specs": [{"from": "2026-01-01", "lot": 50, "tick_paise": 5}]}
SHIPPED = ("buy_and_hold", "sma_cross", "sma_cross_short", "orb", "orb_short", "eod_short_straddle",
           "short_straddle_stop", "iron_fly")


def _rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _by_tag(folder: Path) -> dict[str, dict]:
    return {o["tag"]: o for o in _rows(folder / "orders.csv")}


def _run(g: dict, strat, params: dict, folder: Path, impl: str = "oracle") -> Path:  # noqa: ANN001
    pack = golden.build_pack(g, folder)
    b = golden.build_bundle(g)
    b["strategy"]["params"] = params
    out = folder / impl
    (engine.run_bundle if impl == "engine" else oracle_run)(b, [pack], out, strategy=strat, report=False)
    return out


def _orb_world(short: bool = False) -> dict:
    day = "2026-03-02"
    if short:
        evs = [minute(day, "09:15", 10000, 10050, 9950, 10000), minute(day, "09:16", 10000, 10060, 9980, 10040),
               minute(day, "09:17", 10040, 10040, 9900, 9900),               # closes below the 99.50 low
               minute(day, "09:18", 9900, 9910, 9890, 9900), minute(day, "15:10", 9900, 9910, 9890, 9900),
               minute(day, "15:11", 9900, 9910, 9890, 9900)]
    else:
        evs = [minute(day, "09:15", 10000, 10050, 9950, 10000), minute(day, "09:16", 10000, 10060, 9980, 10040),
               minute(day, "09:17", 10040, 10120, 10040, 10100),             # closes above the 100.60 high
               minute(day, "09:18", 10100, 10150, 10090, 10140), minute(day, "15:10", 10200, 10210, 10190, 10200),
               minute(day, "15:11", 10200, 10210, 10190, 10200)]
    return world(evs, mis_cutoff={"CM": "15:20"})


def _eod_world() -> dict:
    days = ["2026-03-02", "2026-03-03", "2026-03-04"]
    spec = [{"from": "2026-01-01", "lot": 65, "tick_paise": 5}]
    inst = [{"id": 1, "kind": "INDEX", "tradingsymbol": "NIFTY 50"},
            {"id": 2, "kind": "CE", "tradingsymbol": "NC", "underlying": "NIFTY", "underlying_id": 1,
             "expiry": "2026-03-04", "strike_paise": 2400000, "specs": spec},
            {"id": 3, "kind": "PE", "tradingsymbol": "NP", "underlying": "NIFTY", "underlying_id": 1,
             "expiry": "2026-03-04", "strike_paise": 2400000, "specs": spec}]

    def eod(iid: int, day: str, c: int) -> dict:
        return {"kind": "BAR", "instrument": iid, "interval_s": 86400, "start": f"{day}T00:00:00+05:30",
                "t": f"{day}T19:00:00+05:30", "o": c, "h": c, "l": c, "c": c, "v": 100_000}
    evs = []
    # the legs close at 100.00 + 100.00 on day 1 and at 160.00 + 160.00 on day 2: past 1.5 x the credit
    for day, spot, ce, pe in zip(days, (2400000, 2402000, 2410000), (10000, 16000, 10000), (10000, 16000, 0)):
        evs += [eod(2, day, ce), eod(3, day, pe), eod(1, day, spot)]
    evs.append({"kind": "SETTLE", "instrument": 1, "t": "2026-03-04T16:00:00+05:30", "date": "2026-03-04",
                "price": 2410000, "subtype": "final"})
    return world(evs, instruments=inst, dates=days,
                 run={"fill_model": {"kind": "eod_close", "half_spread_ticks": 1}, "capital_paise": 100_000_000})


class Scenarios(unittest.TestCase):
    """Runs every shipped strategy once on a small fixture world (setUpClass); the tests read the run folders."""

    runs: dict[str, Path] = {}
    _tmp: tempfile.TemporaryDirectory

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(dir=str(support.SANDBOX_ROOT))
        root = Path(cls._tmp.name)
        cls.runs = {}

        def go(name: str, g: dict, strat, params: dict, impl: str = "oracle") -> None:  # noqa: ANN001
            folder = root / name
            folder.mkdir(exist_ok=True)
            cls.runs[name if impl == "oracle" else name + "/engine"] = _run(g, strat, params, folder, impl)

        for impl in ("oracle", "engine") if engine.available() else ("oracle",):
            go("buy_and_hold", world([daily(d, c) for d, c in zip(DAYS[:4], (10000, 10100, 10200, 10300))],
                                     dates=DAYS[:4]), BuyAndHold(), {"symbol": "NSE:TEST"}, impl)
            go("sma_cross", world(walk(SMA_CLOSES), dates=DAYS), SmaCross(),
               {"symbol": "NSE:TEST", "fast": 2, "slow": 3}, impl)
            go("orb", _orb_world(), OpeningRangeBreakout(), {"symbol": "NSE:TEST", "range_minutes": 2, "qty": 10}, impl)
        go("sma_cross_short", world(walk(SMA_CLOSES), instruments=(FUT,), dates=DAYS), SmaCross(),
           {"symbol": "TESTFUT", "fast": 2, "slow": 3, "long_short": True, "product": "NRML"})
        go("orb_short", _orb_world(short=True), OpeningRangeBreakout(),
           {"symbol": "NSE:TEST", "range_minutes": 2, "qty": 10, "short": True})
        go("eod_short_straddle", _eod_world(), EodShortStraddle(), {"min_days": 1})
        for key, name in (("straddle", "short_straddle_stop"), ("fly", "iron_fly")):
            params = {"entry_hhmm": "09:30", "exit_hhmm": "11:00", "stop_pct": "0.07", "lots": 1}
            if key == "fly":
                params["wing_strikes"] = 2
            for seed in range(6100, 6160):                         # the first world that enters
                folder = root / f"{name}-{seed}"
                folder.mkdir()
                out = options._run(options._world(seed), params, options.REFS[key], folder, "oracle")
                if any(o["tag"] == "entry" and o["status"] == "COMPLETE" for o in _rows(out / "orders.csv")):
                    break
            cls.runs[name] = out

    @classmethod
    def tearDownClass(cls):
        cls.runs = {}
        cls._tmp.cleanup()


class NoteTests(Scenarios):
    def test_buy_and_hold_notes_name_quantity_and_the_deciding_number(self):
        # 95 % of the ₹1,00,000 capital over the first close 100.00 plus the 3 % band (103.00): 9,500,000 // 10300 = 922
        orders = _rows(self.runs["buy_and_hold"] / "orders.csv")
        self.assertEqual([o["note"] for o in orders], ["Bought 922 NSE:TEST on the first bar and will hold"])

    def test_sma_cross_notes_name_quantity_and_both_averages(self):
        orders = _rows(self.runs["sma_cross"] / "orders.csv")
        # entry on the 4th bar: SMA2 (10000 + 10100) / 2 = 10050 over SMA3 (10000 + 10000 + 10100) / 3 = 10033;
        # exit on the 8th: SMA2 (10150 + 10000) / 2 = 10075 under SMA3 (10300 + 10150 + 10000) / 3 = 10150.
        # 9,500,000 // (10100 * 103 // 100) = 913
        self.assertEqual([(o["tag"], o["note"]) for o in orders], [
            ("entry", "Bought 913 NSE:TEST: the 2-bar average 100.50 rose above the 3-bar average 100.33"),
            ("exit", "Sold 913 NSE:TEST: the 2-bar average 100.75 fell below the 3-bar average 101.50")])

    def test_sma_cross_long_short_notes_say_sold_short_or_flipped(self):
        orders = _rows(self.runs["sma_cross_short"] / "orders.csv")
        # one lot is 50: flat and level averages (10000, 10000) sells short; the cross up buys 100 (closes 50 short,
        # opens 50 long: the note names the new position, 50); the cross down sells 100 (new short of 50)
        self.assertEqual([(o["side"], o["qty"], o["status"], o["note"]) for o in orders], [
            ("SELL", "50", "COMPLETE",
             "Sold short 50 TESTFUT: the 2-bar average 100.00 is level with the 3-bar average 100.00"),
            ("BUY", "100", "COMPLETE",
             "Flipped to long 50 TESTFUT: the 2-bar average 100.50 rose above the 3-bar average 100.33"),
            ("SELL", "100", "COMPLETE",
             "Flipped to short 50 TESTFUT: the 2-bar average 100.75 fell below the 3-bar average 101.50")])

    def test_orb_long_notes(self):
        by = _by_tag(self.runs["orb"])
        # range of the first two bars: high 100.60, low 99.50 (tick 0.05); the 09:17 bar closes at 101.00
        self.assertEqual(by["orb_long"]["note"],
                         "Bought 10 NSE:TEST: price 101.00 broke above the 2-minute opening range high of 100.60")
        self.assertEqual(by["orb_stop"]["note"], "Stop-loss for the 10 bought: sells if price falls to 99.50")
        self.assertEqual(by["orb_exit"]["note"], "Closed 10 NSE:TEST at the 15:10 time exit")

    def test_orb_short_notes(self):
        by = _by_tag(self.runs["orb_short"])
        # the 09:17 bar closes at 99.00 below the 99.50 low; the stop sits at the 100.60 high (a multiple of the tick)
        self.assertEqual(by["orb_short"]["note"],
                         "Sold short 10 NSE:TEST: price 99.00 broke below the 2-minute opening range low of 99.50")
        self.assertEqual(by["orb_stop"]["note"], "Stop-loss for the 10 sold short: buys back if price rises to 100.60")

    def test_eod_short_straddle_notes(self):
        orders = _rows(self.runs["eod_short_straddle"] / "orders.csv")
        entries = [o["note"] for o in orders if o["tag"] == "entry"]
        # the strike nearest the 24,000.00 close; each leg is one lot of 65
        self.assertEqual(entries, ["Sold 65 NC: short straddle at the 24,000.00 strike, expires 2026-03-04",
                                   "Sold 65 NP: short straddle at the 24,000.00 strike, expires 2026-03-04"])
        stops = [o["note"] for o in orders if o["tag"] == "stop"]
        # credit 65 x (100.00 + 100.00) = 13,000.00; the legs close at 160.00 each: 65 x 320.00 = 20,800.00 > 1.5 x 13,000.00
        self.assertEqual(stops, [f"Bought back 65 {leg}: the options cost 20,800.00 to close, over 1.5x the 13,000.00 received"
                                 for leg in ("NC", "NP")])

    def test_option_legs_notes(self):
        for name in ("short_straddle_stop", "iron_fly"):
            orders = _rows(self.runs[name] / "orders.csv")
            with self.subTest(strategy=name):
                entries = [o for o in orders if o["tag"] == "entry"]
                self.assertEqual(len(entries), 2)
                for o in entries:
                    self.assertRegex(o["note"], rf"^Sold {fmt_qty(int(o['qty']))} \S+: short (call|put) at the "
                                                r"[\d,]+\.\d\d strike, the one nearest the forward price$")
                stops = [o for o in orders if o["tag"] == "stop"]
                self.assertTrue(stops)
                for stop in stops:
                    sale = next(o for o in entries if o["instrument_id"] == stop["instrument_id"])
                    leg = sale["note"].split(": ")[0].split(" ")[-1]                  # the tradingsymbol of the sale
                    self.assertEqual(stop["note"], f"Stop-loss for the {fmt_qty(int(stop['qty']))} {leg} sold: buys back "
                                     f"if price rises to {fmt_price(int(stop['trigger_paise']))}, 7% above the sale "
                                     f"at {fmt_price(int(sale['avg_price_paise']))}")
                for o in orders:
                    if o["tag"] == "wing":
                        self.assertRegex(o["note"], rf"^Bought {fmt_qty(int(o['qty']))} \S+: protective (call|put) "
                                                    r"at the [\d,]+\.\d\d strike, to cap the loss$")
                    if o["tag"] == "time_exit":
                        self.assertRegex(o["note"], f"^Closed {fmt_qty(int(o['qty']))} NIFTY[0-9]+(CE|PE) at the 11:00 time exit$")
        self.assertTrue(any(o["tag"] == "wing" for o in _rows(self.runs["iron_fly"] / "orders.csv")))

    def test_every_order_of_a_shipped_strategy_has_a_note(self):
        for name in SHIPPED:
            for o in _rows(self.runs[name] / "orders.csv"):
                with self.subTest(strategy=name, order=o["order_id"], tag=o["tag"]):
                    self.assertTrue(o["note"], "empty note")
                    self.assertLessEqual(len(o["note"]), NOTE_MAX)
                    self.assertLessEqual(len(o["note"]), 120)

    def test_every_note_passes_clean_note_unchanged(self):
        seen = 0
        for key, folder in self.runs.items():
            for o in _rows(folder / "orders.csv"):
                with self.subTest(run=key, order=o["order_id"]):
                    self.assertEqual(clean_note(o["note"]), o["note"])
                    seen += 1
        self.assertGreater(seen, 20)

    def test_a_note_changes_no_fill(self):
        """The same run with every note stripped (a strategy that passes none) has the same fills."""
        class Quiet(SmaCross):
            def on_bar(self, ctx, bar):  # noqa: ANN001
                class Mute:
                    def __getattr__(self, name):  # noqa: ANN001, ANN204
                        return getattr(ctx, name)

                    def place(self, *a, **kw):  # noqa: ANN001, ANN002, ANN003
                        kw.pop("note", None)
                        return ctx.place(*a, **kw)
                SmaCross.on_bar(self, Mute(), bar)
        with tempfile.TemporaryDirectory(dir=str(support.SANDBOX_ROOT)) as tmp:
            out = _run(world(walk(SMA_CLOSES), dates=DAYS), Quiet(), {"symbol": "NSE:TEST", "fast": 2, "slow": 3},
                       Path(tmp))
            loud = self.runs["sma_cross"]
            for name in ("fills.csv", "equity.csv"):
                self.assertEqual((out / name).read_bytes(), (loud / name).read_bytes(), name)
            self.assertEqual([o["note"] for o in _rows(out / "orders.csv")], ["", ""])

    @unittest.skipUnless(engine.available(), "stockify._engine is not built")
    def test_the_engine_writes_the_same_notes_and_plots_for_the_shipped_strategies(self):
        for name in ("buy_and_hold", "sma_cross", "orb"):
            with self.subTest(strategy=name):
                assert_same_artifacts(self, self.runs[name], self.runs[name + "/engine"])


class PlotTests(Scenarios):
    def test_sma_cross_plots_two_series_with_one_point_per_bar(self):
        plots = _rows(self.runs["sma_cross"] / "plots.csv")
        self.assertEqual({p["name"] for p in plots}, {"fast", "slow"})
        # ten daily bars; the 3-bar average exists from the third on: eight points per series
        for name in ("fast", "slow"):
            series = [p for p in plots if p["name"] == name]
            self.assertEqual(len(series), 8, name)
            self.assertEqual({(p["kind"], p["instrument_id"]) for p in series}, {("price", "1")})
            self.assertEqual(len({p["t_ns"] for p in series}), 8)
        fast = [int(p["value"]) for p in plots if p["name"] == "fast"]
        slow = [int(p["value"]) for p in plots if p["name"] == "slow"]
        # SMA2 and SMA3 of 10000 10000 10000 10100 10200 10300 10150 10000 9900 9850, truncated to whole paise
        self.assertEqual(fast, [10000, 10050, 10150, 10250, 10225, 10075, 9950, 9875])
        self.assertEqual(slow, [10000, 10033, 10100, 10200, 10216, 10150, 10016, 9916])

    def test_orb_plots_range_levels_from_the_first_breakout_bar(self):
        plots = _rows(self.runs["orb"] / "plots.csv")
        # the bars from 09:17 on carry the range (high 100.60, low 99.50) until the 15:10 time exit closes the day:
        # 09:17 and 09:18 plot, the two bars after the exit do not (D-95: no plot once the position is closed)
        self.assertEqual([p["value"] for p in plots if p["name"] == "range_high"], ["10060"] * 2)
        self.assertEqual([p["value"] for p in plots if p["name"] == "range_low"], ["9950"] * 2)
        self.assertEqual({(p["kind"], p["instrument_id"]) for p in plots}, {("price", "1")})
        self.assertEqual(len({p["t_ns"] for p in plots}), 2)

    def test_orb_plots_stop_when_the_day_is_closed_and_stay_under_the_point_cap(self):
        """Ten full minute sessions: half the days stop out early, half hold to the 15:10 exit. No point is drawn
        after the position is closed; the worst day times 250 sessions stays under the 200,000-point cap."""
        from stockify import timeutil
        events, stop_days = [], set(DAYS[1::2])
        for d in DAYS:
            for m in range(376):                                   # 09:15 .. 15:30
                hh, mm = divmod(9 * 60 + 15 + m, 60)
                hhmm = f"{hh:02d}:{mm:02d}"
                if m < 15:
                    events.append(minute(d, hhmm, 10000, 10050, 9950, 10000))
                elif m == 15:
                    events.append(minute(d, hhmm, 10040, 10120, 10040, 10100))          # breakout above 100.50
                elif d in stop_days and m == 45:
                    events.append(minute(d, hhmm, 10100, 10100, 9900, 9900))            # through the 99.50 stop
                elif d in stop_days and m > 45:
                    events.append(minute(d, hhmm, 9900, 9900, 9900, 9900))
                else:
                    events.append(minute(d, hhmm, 10100, 10100, 10100, 10100))
        g = world(events, dates=DAYS, mis_cutoff={"CM": "15:20"})
        with tempfile.TemporaryDirectory(dir=str(support.SANDBOX_ROOT)) as tmp:
            out = _run(g, OpeningRangeBreakout(), {"symbol": "NSE:TEST", "range_minutes": 15, "qty": 10}, Path(tmp))
            plots, fills = _rows(out / "plots.csv"), _rows(out / "fills.csv")
        stop_fills = [f for f in fills if f["side"] == "SELL" and f["price_paise"] != "10100"]
        self.assertEqual(len(stop_fills), 5)
        per_day: dict[str, list[int]] = {}
        for p in plots:
            day = str(timeutil.ns_to_ist(int(p["t_ns"])).date())
            per_day.setdefault(day, []).append(int(p["t_ns"]))
        self.assertEqual(sorted(per_day), sorted(DAYS))
        stop_t = {str(timeutil.ns_to_ist(int(f["t_ns"])).date()): int(f["t_ns"]) for f in stop_fills}
        for d in DAYS:
            exit_ns = timeutil.ist_ns(date.fromisoformat(d), "15:10")
            self.assertLessEqual(max(per_day[d]), exit_ns, d)
            if d in stop_days:
                self.assertLessEqual(max(per_day[d]), stop_t[d], d)
        longest = max(len(v) for v in per_day.values())                # points (both series) on the longest day
        shortest = min(len(v) for v in per_day.values())
        naive = 2 * 361 * len(DAYS)                                    # every post-range bar, both series
        self.assertLess(len(plots), naive)
        self.assertLess(shortest, longest // 2)                        # a stopped-out day draws far fewer points
        self.assertLessEqual(longest, 2 * 340)                         # 09:30 to the exit: 340 bars, two series
        self.assertLess(longest * 250, 200_000)                        # a year of days that all hold to the exit

    def test_buy_and_hold_plots_nothing(self):
        self.assertEqual((self.runs["buy_and_hold"] / "plots.csv").read_text(encoding="utf-8").strip(),
                         "t_ns,instrument_id,name,kind,value")


class NearTieTests(unittest.TestCase):
    """The words follow the exact averages; when the printed (truncated) paise are equal they say `just`."""

    def test_near_ties_say_just_above_and_just_below(self):
        # SMA2 / SMA3 on 10000 10001 10002: 10001.5 over 10001.0, both print 100.01; then 10001 10001 gives
        # SMA2 10001.0 under SMA3 10001.33, both print 100.01
        closes = [10000, 10001, 10002, 10001, 10001]
        with tempfile.TemporaryDirectory(dir=str(support.SANDBOX_ROOT)) as tmp:
            out = _run(world(walk(closes), dates=DAYS[:5]), SmaCross(), {"symbol": "NSE:TEST", "fast": 2, "slow": 3},
                       Path(tmp))
            notes = [o["note"] for o in _rows(out / "orders.csv")]
        # 9,500,000 // (10002 * 103 // 100) = 922
        self.assertEqual(notes, [
            "Bought 922 NSE:TEST: the 2-bar average 100.01 rose just above the 3-bar average 100.01",
            "Sold 922 NSE:TEST: the 2-bar average 100.01 fell just below the 3-bar average 100.01"])

    def test_a_wider_gap_and_an_exact_tie_keep_their_words(self):
        # the existing fixtures: 100.50 over 100.33 is "rose above"; the long-short run starts on an exact tie
        with tempfile.TemporaryDirectory(dir=str(support.SANDBOX_ROOT)) as tmp:
            out = _run(world(walk(SMA_CLOSES), dates=DAYS), SmaCross(), {"symbol": "NSE:TEST", "fast": 2, "slow": 3},
                       Path(tmp))
            notes = [o["note"] for o in _rows(out / "orders.csv")]
        self.assertIn("rose above the 3-bar average", notes[0])
        self.assertNotIn("just", notes[0] + notes[1])


class ForwardingTests(unittest.TestCase):
    def test_script_strategy_forwards_note(self):
        from stockify.strategies._script import Script
        g = world([daily(d, c) for d, c in zip(DAYS[:2], (10000, 10100))], dates=DAYS[:2])
        actions = [{"on_event": 1, "place": {"instrument": 1, "side": "BUY", "qty": 1, "product": "CNC",
                                             "type": "MARKET", "note": "Bought on purpose"}}]
        with tempfile.TemporaryDirectory(dir=str(support.SANDBOX_ROOT)) as tmp:
            out = _run(g, Script(actions), {}, Path(tmp))
            self.assertEqual([o["note"] for o in _rows(out / "orders.csv")], ["Bought on purpose"])

    def test_strategy_guard_accepts_the_shipped_strategies(self):
        for ref in ("stockify.strategies.buy_and_hold:BuyAndHold", "stockify.strategies.sma_cross:SmaCross",
                    "stockify.strategies.opening_range_breakout:OpeningRangeBreakout",
                    "stockify.strategies.eod_short_straddle:EodShortStraddle",
                    "stockify.strategies.short_straddle_stop:ShortStraddleStop",
                    "stockify.strategies.iron_fly:IronFly"):
            with self.subTest(ref=ref):
                self.assertIsInstance(S.load(ref), S.Strategy)


if __name__ == "__main__":
    unittest.main()

"""M4.7: the T0 flat margin model completed (A4 §6.5, X-9, X-10, Z-4), on the oracle and the engine alike.

A probe strategy trades, then logs ctx.funds() at fixed times; both implementations must log the same, and
the blocked margin must be the hand-computed value. Skipped when the engine is not built.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine, golden, numerics
from stockify.oracle import run_bundle as oracle_run
from stockify.strategy import Strategy

SPOT = 2_450_000                                     # the index at 24,500.00
IDX = {"id": 1, "kind": "INDEX", "tradingsymbol": "NIFTY 50"}
CE = {"id": 2, "kind": "CE", "tradingsymbol": "NIFTYCE", "underlying": "NIFTY", "underlying_id": 1,
      "expiry": "2026-03-03", "strike_paise": 2_500_000, "specs": [{"from": "2026-01-01", "lot": 65, "tick_paise": 5}]}


class Probe(Strategy):
    """Places `orders` on the first bar, then logs funds at each `at` time."""

    def __init__(self, orders: list[tuple], at: list[str]):
        self.orders, self.at, self.done = orders, at, False

    def on_start(self, ctx):  # noqa: ANN001
        ctx.subscribe_bars(2, 60)
        for t in self.at:
            ctx.schedule_at(golden.ts(t))

    def on_bar(self, ctx, bar):  # noqa: ANN001
        if not self.done:
            self.done = True
            for iid, side, qty, product, price in self.orders:
                ctx.place(iid, side, qty, product, "LIMIT", price=price)

    def on_timer(self, ctx, timer_id):  # noqa: ANN001
        f = ctx.funds()
        ctx.log("info", f"funds blocked={f.blocked} available={f.available}")


def _world(addon: str | None) -> dict:
    ev = []
    for day in ("2026-03-02", "2026-03-03"):
        ev.append({"kind": "INDEX", "instrument": 1, "t": f"{day}T09:15:30+05:30", "value": SPOT})
        for hhmm in ("09:15", "09:16"):
            ev.append({"kind": "BAR", "instrument": 2, "interval_s": 60, "start": f"{day}T{hhmm}:00+05:30",
                       "o": 10000, "h": 10000, "l": 10000, "c": 10000, "v": 10_000})
    ev.append({"kind": "SETTLE", "instrument": 1, "t": "2026-03-03T16:00:00+05:30", "date": "2026-03-03",
               "price": SPOT, "subtype": "final"})
    b = {"dates": ["2026-03-02", "2026-03-03"], "instruments": [IDX, CE]}
    if addon is not None:
        b["expiry_addon"] = addon
    return {"id": "M47", "bundle": b, "run": {"fill_model": {"kind": "bar"}, "capital_paise": 100_000_000,
                                              "price_basis": "raw"}, "events": ev, "strategy": {"actions": []}}


def _blocked(g: dict, orders: list[tuple], test: unittest.TestCase) -> list[int]:
    at = ["2026-03-02T11:00:00+05:30", "2026-03-03T11:00:00+05:30"]
    logs = []
    with tempfile.TemporaryDirectory(prefix="margin-") as tmp:
        folder = Path(tmp)
        pack, b = golden.build_pack(g, folder), golden.build_bundle(g)
        for impl, runner in (("oracle", oracle_run), ("engine", engine.run_bundle)):
            runner(b, [pack], folder / impl, strategy=Probe(orders, at), report=False)
            logs.append([x.split(" ", 2)[2] for x in (folder / impl / "events_log.txt").read_text().splitlines()
                         if " INFO funds " in x])
    test.assertEqual(logs[0], logs[1])
    return [int(x.split("blocked=")[1].split(" ")[0]) for x in logs[0]]


def _rupee_up(raw_ppb: int) -> int:
    return numerics.ceil_to(numerics.ceil_div(raw_ppb, 1_000_000_000), 100)


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class ExpiryDayElmTests(unittest.TestCase):
    SHORT = [(2, "SELL", 65, "NRML", 10000)]

    def test_a_short_index_option_adds_two_percent_on_its_expiry_day(self):
        flat = 65 * SPOT * 120_000_000                 # 12 % of 65 × 24,500.00 (index flat rate)
        addon = 65 * SPOT * 20_000_000                 # + 2 % of underlying price × qty, expiry day only (X-9)
        self.assertEqual(_blocked(_world("0.02"), self.SHORT, self), [_rupee_up(flat), _rupee_up(flat + addon)])

    def test_without_the_add_on_row_the_expiry_day_is_like_any_other(self):
        flat = _rupee_up(65 * SPOT * 120_000_000)
        self.assertEqual(_blocked(_world(None), self.SHORT, self), [flat, flat])

    def test_long_options_are_premium_only(self):
        """X-10: a bought option blocks no margin, on its expiry day either; the premium was paid upfront."""
        self.assertEqual(_blocked(_world("0.02"), [(2, "BUY", 65, "NRML", 10000)], self), [0, 0])

    def test_the_shipped_row_starts_on_2024_11_20(self):
        from stockify.config import loader
        rows = loader.load_json("margin_params.json")["expiry_day_short_index_option_addon"]
        self.assertEqual([(r["effective_from"], r["pct"], r["basis"]) for r in rows],
                         [("2024-11-20", "0.02", "underlying_price_x_qty")])



# ── the stock F&O delivery-margin schedule (Z-4, D-60) and the shortfall rule (A4 §6.5) ─────────────────

WEEK = ["2026-03-25", "2026-03-26", "2026-03-27", "2026-03-30", "2026-03-31"]   # Wed … Tue, expiry Tuesday
STOCK = {"id": 1, "kind": "EQ", "tradingsymbol": "ACME", "specs": [{"from": "2026-01-01", "lot": 1, "tick_paise": 5}]}


def _stock_world(dates: list[str], contract: dict, stock_prices: bool = True, capital: int = 100_000_000) -> dict:
    ev = []
    for day in dates:
        if stock_prices:
            ev.append({"kind": "BAR", "instrument": 1, "interval_s": 60, "start": f"{day}T09:14:00+05:30",
                       "o": 100000, "h": 100000, "l": 100000, "c": 100000, "v": 10_000})
        for hhmm in ("09:15", "09:16", "11:06"):
            ev.append({"kind": "BAR", "instrument": 2, "interval_s": 60, "start": f"{day}T{hhmm}:00+05:30",
                       "o": contract["px"], "h": contract["px"], "l": contract["px"], "c": contract["px"],
                       "v": 10_000})
    ev.append({"kind": "SETTLE", "instrument": 1, "t": f"{dates[-1]}T16:00:00+05:30", "date": dates[-1],
               "price": 100000, "subtype": "final"})
    row = {"id": 2, "kind": contract["kind"], "tradingsymbol": "ACMEX", "underlying": "ACME", "underlying_id": 1,
           "expiry": contract.get("expiry", dates[-1]), "underlying_class": "STOCK", "settlement": "physical",
           "specs": [{"from": "2026-01-01", "lot": 100, "tick_paise": 5}]}
    if "strike" in contract:
        row["strike_paise"] = contract["strike"]
    return {"id": "Z4", "bundle": {"dates": dates, "instruments": [STOCK, row], "delivery_schedule": True},
            "run": {"fill_model": {"kind": "bar"}, "capital_paise": capital, "price_basis": "raw"},
            "events": ev, "strategy": {"actions": []}}


def _week_blocked(g: dict, orders: list[tuple], test: unittest.TestCase) -> tuple[list[int], list[str]]:
    at = [f"{d}T11:00:00+05:30" for d in g["bundle"]["dates"]]
    logs, labels = [], []
    with tempfile.TemporaryDirectory(prefix="margin-") as tmp:
        folder = Path(tmp)
        pack, b = golden.build_pack(g, folder), golden.build_bundle(g)
        for impl, runner in (("oracle", oracle_run), ("engine", engine.run_bundle)):
            runner(b, [pack], folder / impl, strategy=Probe(orders, at), report=False)
            logs.append([x.split(" ", 2)[2] for x in (folder / impl / "events_log.txt").read_text().splitlines()
                         if " INFO funds " in x])
            import json
            labels.append(json.loads((folder / impl / "manifest.json").read_text())["labels"])
    test.assertEqual(logs[0], logs[1])
    test.assertEqual(labels[0], labels[1])
    return [int(x.split("blocked=")[1].split(" ")[0]) for x in logs[0]], labels[0]


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class DeliveryMarginTests(unittest.TestCase):
    CV = 100 * 100000                                  # 100 units at the stock's 1,000.00

    def test_a_long_itm_option_through_expiry_week(self):
        g = _stock_world(WEEK, {"kind": "CE", "strike": 95000, "px": 6000})
        blocked, labels = _week_blocked(g, [(2, "BUY", 100, "NRML", 6000)], self)
        # E-4..E-2: 10 / 25 / 45 % of the stand-in VaR + ELM (the flat 20 % of contract value); E-1 25 %, E 50 %
        self.assertEqual(blocked, [self.CV * 2 // 100, self.CV * 5 // 100, self.CV * 9 // 100,
                                   self.CV * 25 // 100, self.CV * 50 // 100])
        self.assertIn("delivery_margin_approx", labels)

    def test_a_holiday_moves_the_schedule_a_day_earlier(self):
        week = [d for d in WEEK if d != "2026-03-27"]   # Friday a holiday: Wed is now E-3, Thu E-2
        blocked, _ = _week_blocked(_stock_world(week, {"kind": "CE", "strike": 95000, "px": 6000}),
                                   [(2, "BUY", 100, "NRML", 6000)], self)
        self.assertEqual(blocked, [self.CV * 5 // 100, self.CV * 9 // 100, self.CV * 25 // 100, self.CV * 50 // 100])

    def test_a_short_future_on_expiry_day_pays_the_smaller_of_half_or_one_and_a_half_nrml(self):
        blocked, labels = _week_blocked(_stock_world(WEEK, {"kind": "FUT", "px": 100000}),
                                        [(2, "SELL", 100, "NRML", 100000)], self)
        nrml = self.CV * 20 // 100                      # the flat stock rate
        self.assertEqual(blocked, [nrml, nrml, nrml, nrml, min(self.CV // 2, nrml * 3 // 2)])
        self.assertNotIn("delivery_margin_approx", labels)

    def test_without_a_stock_price_or_inside_the_bundle_the_normal_margin_stays(self):
        no_spot = _stock_world(WEEK, {"kind": "CE", "strike": 95000, "px": 6000}, stock_prices=False)
        self.assertEqual(_week_blocked(no_spot, [(2, "BUY", 100, "NRML", 6000)], self)[0], [0] * 5)
        later = _stock_world(WEEK[:3], {"kind": "CE", "strike": 95000, "px": 6000, "expiry": "2026-04-07"})
        self.assertEqual(_week_blocked(later, [(2, "BUY", 100, "NRML", 6000)], self)[0], [0] * 3)

    def test_a_shortfall_account_can_close_a_position_but_not_add_one(self):
        """E-1 asks 25 % of the contract value (2,500,000) of an account with 400,000 cash: a shortfall. A sell
        that closes the calls is accepted; a new buy is refused R_MARGIN (A4 §6.5)."""
        g = _stock_world(WEEK, {"kind": "CE", "strike": 95000, "px": 6000}, capital=1_000_000)
        with tempfile.TemporaryDirectory(prefix="margin-") as tmp:
            folder = Path(tmp)
            pack, b = golden.build_pack(g, folder), golden.build_bundle(g)
            for impl, runner in (("oracle", oracle_run), ("engine", engine.run_bundle)):
                runner(b, [pack], folder / impl, strategy=Closer(), report=False)
            rows = [golden._read_csv(folder / impl / "orders.csv") for impl in ("oracle", "engine")]
            self.assertEqual(rows[0], rows[1])
            got = [(r["side"], r["instrument_id"], r["status"], r["reason"]) for r in rows[0]]
            self.assertEqual(got, [("BUY", "2", "COMPLETE", ""), ("BUY", "1", "REJECTED", "R_MARGIN"),
                                   ("SELL", "2", "COMPLETE", "")])
            log = (folder / "oracle" / "events_log.txt").read_text()
            self.assertIn("shortfall available=-", log)


class Closer(Strategy):
    """Buys 100 calls on the first day; on E-1 (in a shortfall) tries to add a share, then sells the calls."""

    def on_start(self, ctx):  # noqa: ANN001
        ctx.subscribe_bars(2, 60)
        self.bought = False
        ctx.schedule_at(golden.ts("2026-03-30T11:05:00+05:30"))

    def on_bar(self, ctx, bar):  # noqa: ANN001
        if not self.bought:
            self.bought = True
            ctx.place(2, "BUY", 100, "NRML", "LIMIT", price=6000)

    def on_timer(self, ctx, timer_id):  # noqa: ANN001
        ctx.log("info", f"shortfall available={ctx.funds().available}")
        ctx.place(1, "BUY", 1, "CNC", "LIMIT", price=100000)        # risk-increasing: refused
        ctx.place(2, "SELL", 100, "NRML", "LIMIT", price=6000)      # only reduces: accepted

if __name__ == "__main__":
    unittest.main()

"""M4.6: physical delivery goes through the ordinary CNC path (A4 §7.3, D-58).

A delivered SELL is a CNC sale: with the shipped policy (80 % of a CNC sale is usable the same day), a fifth of
its proceeds is held back until the next trading day, on the oracle and the engine alike. A give with no shares
held leaves a short CNC holding, labelled `short_delivery`. Skipped when the engine is not built.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine, golden, numerics
from stockify.oracle import run_bundle as oracle_run
from stockify.strategy import Strategy

EQ = {"id": 1, "kind": "EQ", "tradingsymbol": "ACME", "specs": [{"from": "2026-01-01", "lot": 1, "tick_paise": 5}]}
PE = {"id": 4, "kind": "PE", "tradingsymbol": "ACME26MAR1050PE", "underlying": "ACME", "underlying_id": 1,
      "expiry": "2026-03-30", "strike_paise": 105000, "underlying_class": "STOCK", "settlement": "physical",
      "specs": [{"from": "2026-01-01", "lot": 100, "tick_paise": 5}]}


def _bar(iid: int, at: str, px: int) -> dict:
    return {"kind": "BAR", "instrument": iid, "interval_s": 60, "start": at, "o": px, "h": px, "l": px, "c": px,
            "v": 10_000}


class Probe(Strategy):
    """Buys `stock` shares (CNC) and a lot of the put, then logs funds after the settlement and next morning."""

    def __init__(self, stock: int):
        self.stock, self.done = stock, False

    def on_start(self, ctx):  # noqa: ANN001
        ctx.subscribe_bars(1, 60)
        ctx.subscribe_bars(4, 60)
        ctx.schedule_at(golden.ts("2026-03-30T16:30:00+05:30"))
        ctx.schedule_at(golden.ts("2026-03-31T10:00:00+05:30"))

    def on_bar(self, ctx, bar):  # noqa: ANN001
        if not self.done:
            self.done = True
            if self.stock:
                ctx.place(1, "BUY", self.stock, "CNC", "LIMIT", price=100000)
            ctx.place(4, "BUY", 100, "NRML", "LIMIT", price=7000)

    def on_timer(self, ctx, timer_id):  # noqa: ANN001
        f = ctx.funds()
        ctx.log("info", f"funds available={f.available} cash={f.cash} positions={ctx.positions()!r}")


def _world(credit: str | None) -> dict:
    events = [_bar(1, "2026-03-30T09:15:00+05:30", 100000), _bar(4, "2026-03-30T09:15:00+05:30", 7000),
              _bar(1, "2026-03-30T09:16:00+05:30", 100000), _bar(4, "2026-03-30T09:16:00+05:30", 7000),
              {"kind": "SETTLE", "instrument": 1, "t": "2026-03-30T16:00:00+05:30", "date": "2026-03-30",
               "price": 102000, "subtype": "final"},
              _bar(1, "2026-03-31T09:15:00+05:30", 102000)]
    return {"id": "PD", "bundle": {"dates": ["2026-03-30", "2026-03-31"], "instruments": [EQ, PE],
                                   "physical_delivery": {"brokerage_pct": "0.0025", "netted_brokerage_pct": "0.001",
                                                         "stt_each_side_pct_of_value": "0.001"}},
            "run": {"fill_model": {"kind": "bar"}, "capital_paise": 50_000_000, "price_basis": "raw"},
            "events": events, "strategy": {"actions": []}, "credit": credit}


def _run_both(g: dict, stock: int, folder: Path) -> list[list[str]]:
    pack, b = golden.build_pack(g, folder), golden.build_bundle(g)
    if g["credit"] is not None:
        b["broker_policy"]["cnc_sell_credit_same_day"] = [
            {"applies": {"segment": "CM"}, "effective_from": "2015-01-01", "fraction": g["credit"],
             "fraction_ppb": numerics.to_ppb(g["credit"])}]
    logs = []
    for impl, runner in (("oracle", oracle_run), ("engine", engine.run_bundle)):
        runner(b, [pack], folder / impl, strategy=Probe(stock), report=False)
        logs.append([x.split(" ", 2)[2] for x in (folder / impl / "events_log.txt").read_text().splitlines()
                     if " INFO funds " in x])
    return logs


def _num(line: str, key: str) -> int:
    return int(line.split(f"{key}=")[1].split(" ")[0])


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class PhysicalDeliveryCashTests(unittest.TestCase):
    def test_a_delivered_sale_follows_the_cnc_credit_policy(self):
        with tempfile.TemporaryDirectory(prefix="delivery-") as tmp:
            oracle, eng = _run_both(_world("0.8"), 100, Path(tmp))
            self.assertEqual(oracle, eng)
            same_day, next_day = oracle
            # the put gives the 100 shares held at its strike 1,050.00: 10,500,000 paise, 20 % held back today
            self.assertEqual(_num(same_day, "cash") - _num(same_day, "available"), 10_500_000 * 2 // 10)
            self.assertEqual(_num(next_day, "available"), _num(next_day, "cash"))
            self.assertIn("positions=[]", same_day)                      # the give delivered the holding
            labels = engine_labels(Path(tmp) / "engine")
            self.assertNotIn("short_delivery", labels)

    def test_without_a_same_day_credit_row_the_proceeds_are_usable_at_once(self):
        with tempfile.TemporaryDirectory(prefix="delivery-") as tmp:
            oracle, eng = _run_both(_world(None), 100, Path(tmp))
            self.assertEqual(oracle, eng)
            self.assertEqual(_num(oracle[0], "available"), _num(oracle[0], "cash"))

    def test_a_give_without_shares_leaves_a_labelled_short_holding(self):
        with tempfile.TemporaryDirectory(prefix="delivery-") as tmp:
            oracle, eng = _run_both(_world("0.8"), 0, Path(tmp))
            self.assertEqual(oracle, eng)
            self.assertIn("PositionView(instrument=1, product='CNC', qty=-100", oracle[0])
            for impl in ("oracle", "engine"):
                self.assertIn("short_delivery", engine_labels(Path(tmp) / impl), impl)


def engine_labels(run: Path) -> list[str]:
    import json
    return json.loads((run / "manifest.json").read_text(encoding="utf-8")).get("labels", [])


if __name__ == "__main__":
    unittest.main()

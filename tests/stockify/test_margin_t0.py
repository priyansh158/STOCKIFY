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


if __name__ == "__main__":
    unittest.main()

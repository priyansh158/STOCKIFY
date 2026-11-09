"""M4.8: SPAN-lite on the oracle and the engine, with exact parity (one platform; A4 §9.3).

Chain worlds (test_chain_view) over two days, the second being the options' expiry day, traded by a strategy that
sells straddles, adds wings, trades the future and closes legs, drawing only from ctx.rng() so both
implementations make the same choices. Every order is followed by a ctx.funds() line in the log, so the margin is
compared all day long; positions open at DAY_END give margin_daily.csv rows.
"""
from __future__ import annotations

import copy
import csv
import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine, golden, numerics
from stockify.oracle import run_bundle as oracle_run
from stockify.strategy import Strategy
from tests.stockify.test_chain_view import EXPIRY, STRIKES, chain_bundle, chain_world
from tests.stockify.test_engine_parity import assert_same_artifacts

DAY_NS = 86_400 * 10**9


def spanlite_world(seed: int, with_future: bool) -> dict:
    g = chain_world(seed, with_future)
    g["run"].update(capital_paise=1_000_000_000, margin={"model": "spanlite"})
    day2 = [dict(e, t=e["t"] + DAY_NS) for e in g["events"]]   # the same moments on the expiry day
    g["events"] = g["events"] + day2
    return g


def spanlite_bundle(g: dict, **over) -> dict:
    b = chain_bundle(g)
    row = {"applies": {"underlying_class": "INDEX"}, "effective_from": "2015-01-01", "psr_pct": "0.06",
           "vsr_pct": "0.25", "extreme_multiple": 3, "extreme_weight": "0.35", "somc_per_lot_paise": 200_000,
           "elm_pct": "0.02", "default_iv": "0.15", "calibration_id": "test"}
    row.update(over)
    for f in ("psr_pct", "vsr_pct", "extreme_weight", "elm_pct", "default_iv"):
        row[f"{f}_ppb"] = numerics.to_ppb(row[f])
    b["margin_params"]["spanlite"] = [row]
    b["margin_params"]["expiry_day_short_index_option_addon"] = [
        {"applies": {}, "effective_from": "2024-11-20", "pct": "0.02", "pct_ppb": 20_000_000}]
    return b


class OptionTrader(Strategy):
    """Sells straddles, buys wings, trades the future, closes legs; logs funds after every order."""

    def on_start(self, ctx):  # noqa: ANN001
        ctx.subscribe_ticks(1)
        self.n = 0

    def on_tick(self, ctx, tick):  # noqa: ANN001
        if tick.instrument != 1:
            return
        self.n += 1
        rng = ctx.rng()
        c = ctx.chain("NIFTY", EXPIRY)
        if not c or c.atm_strike_paise is None or rng.random() < 0.3:
            ctx.log("info", f"funds {ctx.funds()!r}")
            return
        by = {(r["strike_paise"], r["kind"]): r["instrument_id"] for r in c}
        atm = c.atm_strike_paise
        wing = STRIKES[min(len(STRIKES) - 1, STRIKES.index(atm) + 2)], STRIKES[max(0, STRIKES.index(atm) - 2)]
        what = rng.below(5)
        if what == 0:                                   # short straddle
            orders = [(by[(atm, "CE")], "SELL"), (by[(atm, "PE")], "SELL")]
        elif what == 1:                                 # wings: turns a straddle into an iron fly
            orders = [(by[(wing[0], "CE")], "BUY"), (by[(wing[1], "PE")], "BUY")]
        elif what == 2 and any(r["kind"] == "FUT" for r in ctx._rows()):
            orders = [(2, rng.below(2) and "BUY" or "SELL")]
        elif what == 3:                                 # close something
            pos = ctx.positions()
            if pos:
                p = pos[rng.below(len(pos))]
                orders = [(p.instrument, "SELL" if p.qty > 0 else "BUY", abs(p.qty))]
            else:
                orders = []
        else:
            orders = [(by[(STRIKES[rng.below(len(STRIKES))], "CE" if rng.below(2) else "PE")], "SELL")]
        for o in orders:
            iid, side = o[0], o[1]
            qty = o[2] if len(o) > 2 else 75 * (1 + rng.below(2))
            oid = ctx.place(iid, side, qty, "NRML", "MARKET", protection=5000)
            ctx.log("info", f"order {oid} {iid} {side} {qty} {ctx.order(oid).status} funds {ctx.funds()!r}")


def run_both(g: dict, folder: Path, bundle: dict) -> None:
    pack = golden.build_pack(g, folder)
    oracle_run(bundle, [pack], folder / "oracle", strategy=OptionTrader(), report=False)
    engine.run_bundle(bundle, [pack], folder / "engine", strategy=OptionTrader(), report=False)


def margin_rows(folder: Path) -> list[dict]:
    with (folder / "margin_daily.csv").open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class SpanLiteParityTests(unittest.TestCase):
    def test_worlds_agree_exactly(self):
        import json
        totals = {"rows": 0, "addons": 0, "somc_wins": 0, "orders": 0, "fallback": 0}
        for seed, with_future in ((4810, False), (4811, True), (4812, True), (4813, False), (4814, True)):
            with self.subTest(seed=seed), tempfile.TemporaryDirectory(prefix="spanlite-") as tmp:
                folder = Path(tmp)
                g = spanlite_world(seed, with_future)
                run_both(g, folder, spanlite_bundle(g))
                assert_same_artifacts(self, folder / "oracle", folder / "engine")
                self.assertEqual((folder / "oracle" / "margin_daily.csv").read_bytes(),
                                 (folder / "engine" / "margin_daily.csv").read_bytes())
                lo = json.loads((folder / "oracle" / "manifest.json").read_text())
                le = json.loads((folder / "engine" / "manifest.json").read_text())
                self.assertEqual(sorted(lo["labels"]), sorted(le["labels"]))
                rows = margin_rows(folder / "oracle")
                totals["rows"] += len(rows)
                totals["addons"] += sum(int(r["addons"]) > 0 for r in rows)
                totals["somc_wins"] += sum(int(r["somc"]) > int(r["scan"]) for r in rows)
                totals["fallback"] += "margin_spanlite:iv_fallback" in lo["labels"]
                totals["orders"] += sum(1 for _ in (folder / "oracle" / "orders.csv").open()) - 1
                for r in rows:
                    self.assertEqual(r["model"], "spanlite")
                    self.assertEqual(int(r["total"]), max(int(r["scan"]), int(r["somc"])) + int(r["elm"]) + int(r["addons"]))
        self.assertGreater(totals["rows"], 4)
        self.assertGreater(totals["orders"], 50)
        # (expiring options settle before DAY_END, so the add-on shows in the funds lines, which match, not in the
        # day rows; the oracle-run test below checks it was charged)

    def test_a_futures_price_move_agrees_exactly_and_changes_only_futures(self):
        """D-62: `psr_futures_pct` moves futures by their own range; oracle and engine agree byte for byte, and a
        world with no future gives the same margins with or without it."""
        from stockify import numerics
        fut_only = {"psr_futures_pct": "0.0886", "psr_futures_pct_ppb": numerics.to_ppb("0.0886")}
        for seed, with_future in ((4816, True), (4817, True), (4818, False)):
            with self.subTest(seed=seed), tempfile.TemporaryDirectory(prefix="spanlite-fut-") as tmp:
                folder = Path(tmp)
                g = spanlite_world(seed, with_future)
                run_both(g, folder, spanlite_bundle(g, **fut_only))
                assert_same_artifacts(self, folder / "oracle", folder / "engine")
                self.assertEqual((folder / "oracle" / "margin_daily.csv").read_bytes(),
                                 (folder / "engine" / "margin_daily.csv").read_bytes())
                (folder / "plain").mkdir()
                run_both(g, folder / "plain", spanlite_bundle(g))
                same = (folder / "oracle" / "events_log.txt").read_bytes() == \
                    (folder / "plain" / "oracle" / "events_log.txt").read_bytes()
                self.assertEqual(same, not with_future)       # the funds lines move only when a future is held

    def test_net_option_value_agrees_exactly(self):
        """D-63: with `net_option_value`, oracle and engine still agree byte for byte, and the margins change."""
        from stockify import numerics
        nov = {"net_option_value": True, "psr_futures_pct": "0.0886", "psr_futures_pct_ppb": numerics.to_ppb("0.0886")}
        for seed, with_future in ((4819, True), (4820, False), (4821, True)):
            with self.subTest(seed=seed), tempfile.TemporaryDirectory(prefix="spanlite-nov-") as tmp:
                folder = Path(tmp)
                g = spanlite_world(seed, with_future)
                run_both(g, folder, spanlite_bundle(g, **nov))
                assert_same_artifacts(self, folder / "oracle", folder / "engine")
                self.assertEqual((folder / "oracle" / "margin_daily.csv").read_bytes(),
                                 (folder / "engine" / "margin_daily.csv").read_bytes())
                (folder / "plain").mkdir()
                run_both(g, folder / "plain", spanlite_bundle(g))
                self.assertNotEqual((folder / "oracle" / "events_log.txt").read_bytes(),
                                    (folder / "plain" / "oracle" / "events_log.txt").read_bytes())

    def test_flat_runs_write_no_margin_rows(self):
        with tempfile.TemporaryDirectory(prefix="spanlite-flat-") as tmp:
            folder = Path(tmp)
            g = spanlite_world(4815, True)
            g["run"]["margin"] = {"model": "flat"}
            b = chain_bundle(g)
            run_both(g, folder, b)
            assert_same_artifacts(self, folder / "oracle", folder / "engine")
            self.assertEqual(margin_rows(folder / "oracle"), [])


class SpanLiteOracleRunTests(unittest.TestCase):
    def test_a_run_writes_margin_rows_and_the_requirement_matches_them(self):
        from unittest import mock
        from stockify.fno import spanlite
        seen: list = []
        real = spanlite.SpanLiteMargin.breakdown

        def watch(self_, legs, inst, price, date):  # noqa: ANN001, ANN202
            out = real(self_, legs, inst, price, date)
            seen.extend((date, b) for b in out)
            return out

        with tempfile.TemporaryDirectory(prefix="spanlite-oracle-") as tmp,                 mock.patch.object(spanlite.SpanLiteMargin, "breakdown", watch):
            folder = Path(tmp)
            g = spanlite_world(4816, True)
            pack = golden.build_pack(g, folder)
            oracle_run(spanlite_bundle(g), [pack], folder / "run", strategy=OptionTrader(), report=False)
            on_expiry = [b for d, b in seen if d == EXPIRY]
            self.assertTrue(any(b.addons > 0 for b in on_expiry))   # X-9 charged during the expiry day
            self.assertFalse(any(b.addons > 0 for d, b in seen if d != EXPIRY))
            self.assertTrue(any(b.somc > 0 for _, b in seen) and any(b.scan > 0 for _, b in seen))
            rows = margin_rows(folder / "run")
            self.assertTrue(rows)
            with (folder / "run" / "equity.csv").open(encoding="utf-8", newline="") as f:
                equity = {r["trade_date"]: int(r["margin_blocked_paise"]) for r in csv.DictReader(f)}
            per_day: dict[str, int] = {}
            for r in rows:
                per_day[r["trade_date"]] = per_day.get(r["trade_date"], 0) + int(r["total"])
            for d, total in per_day.items():
                self.assertLessEqual(total, equity[d])            # blocked = SPAN-lite NRML + any MIS
            self.assertTrue(all(int(r[k]) % 100 == 0 for r in rows for k in ("scan", "somc", "elm", "addons")))


if __name__ == "__main__":
    unittest.main()

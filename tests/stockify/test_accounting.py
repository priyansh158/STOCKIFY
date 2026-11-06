"""A3 P-06: the accounting identity, checked from a run folder's artifacts alone (A4 §6).

`accounting_problems(run_dir)` rebuilds every position from `fills.csv` plus the expiry settlements logged in
`events_log.txt` ("settled <iid> <product> at FSP <price> (<source>)"), using A4 §6.1's average-cost rule, and
checks, for every trade date d in `equity.csv`:

* charges_d = Σ `charges.csv` totals with trade_date ≤ d;
* `positions_eod.csv` on d lists exactly the rebuilt open positions, with the same qty, average price and
  realised P&L, and unrealised = direction · (units · mark − cost of the open units);
* NAV_d = capital − charges_d + Σ realised P&L of every position + Σ unrealised P&L of the open ones;
* gross_pnl_d = NAV_d − capital + charges_d;
* cash_d = capital − charges_d + Σ premium-style traded value (−side · qty · price, settlements included)
  + Σ realised P&L of variation-style positions, on every day no variation-style position is open
  (futures and MIS move cash at marks that no artifact records; on those days the NAV check covers them).

It also checks that each order's filled_qty is the sum of its fills. Nothing here calls either implementation,
so it holds the oracle and the engine to A4 independently. Checked on every golden world, the 200 P-04 fuzz
seeds and the M3.10 strategy worlds, on both implementations.
"""
from __future__ import annotations

import csv
import json
import tempfile
import unittest
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine, golden
from stockify.oracle import run_bundle as oracle_run


@dataclass
class _Pos:
    style: str
    q: int = 0
    d: int = 1
    c: int = 0
    realised: int = 0


def _rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _apply(p: _Pos, sign: int, qty: int, price: int) -> None:
    """A4 §6.1: reducing removes floor(c·k / q) of the cost (all of it when the position closes)."""
    remaining = qty
    if p.q and p.d != sign:
        k = min(remaining, p.q)
        removed = p.c if k == p.q else (p.c * k) // p.q
        p.realised += p.d * (k * price - removed)
        p.c -= removed
        p.q -= k
        remaining -= k
    if remaining:
        if not p.q:
            p.d, p.c = sign, 0
        p.q += remaining
        p.c += remaining * price


def accounting_problems(run_dir: Path, allow_late: bool = False) -> list[str]:
    """Every way the run folder breaks P-06. `allow_late` skips fills after the last DAY_END: a fill on the last
    day's daily bar, which is only knowable at the next midnight IST (A4 §1.1, D-44), is in no equity row on either
    implementation (tracked in tests/test_known_defects.py)."""
    bundle = json.loads((run_dir / "bundle.json").read_text(encoding="utf-8"))
    capital = bundle["run"]["capital_paise"]
    inst = {r["id"]: r for r in bundle["instruments"]}
    day_end = {d["date"]: d["day_end_ns"] for d in bundle["days"]}
    orders = {int(o["order_id"]): o for o in _rows(run_dir / "orders.csv")}
    fills = _rows(run_dir / "fills.csv")
    problems: list[str] = []

    def style(iid: int, product: str) -> str:
        kind = inst[iid]["kind"]
        return "variation" if kind == "FUT" or (product == "MIS" and kind not in ("CE", "PE")) else "premium"

    # every cash-relevant event: (t, order, iid, product, sign, qty, price)
    events = []
    filled = defaultdict(int)
    for n, f in enumerate(fills):
        oid = int(f["order_id"])
        iid, qty = int(f["instrument_id"]), int(f["qty"])
        filled[oid] += qty
        events.append((int(f["t_ns"]), 0, n, iid, orders[oid]["product"], 1 if f["side"] == "BUY" else -1, qty,
                       int(f["price_paise"])))
    for oid, o in orders.items():
        if filled[oid] != int(o["filled_qty"]):
            problems.append(f"order {oid}: filled_qty {o['filled_qty']} but its fills add up to {filled[oid]}")
    for n, line in enumerate((run_dir / "events_log.txt").read_text(encoding="utf-8").splitlines()):
        t, level, msg = line.split(" ", 2)
        if level == "INFO" and msg.startswith("settled "):
            _, iid, product, _, _, fsp, _ = msg.split(" ")
            events.append((int(t), 1, n, int(iid), product, 0, 0, int(fsp)))   # sign/qty: from the position

    events.sort()
    pos: dict[tuple[int, str], _Pos] = {}
    flows = 0                                  # premium-style traded value, settlements included
    charges = sorted((r["trade_date"], int(r["total"])) for r in _rows(run_dir / "charges.csv"))
    eod = defaultdict(dict)
    for r in _rows(run_dir / "positions_eod.csv"):
        eod[r["trade_date"]][(int(r["instrument_id"]), r["product"])] = r
    i = 0
    for row in _rows(run_dir / "equity.csv"):
        d = row["trade_date"]
        while i < len(events) and events[i][0] <= day_end[d]:
            t, is_settle, _, iid, product, sign, qty, price = events[i]
            i += 1
            p = pos.setdefault((iid, product), _Pos(style(iid, product)))
            if is_settle:
                if not p.q:
                    problems.append(f"{d}: {iid} {product} settled while flat")
                    continue
                r = inst[iid]
                if r["kind"] != "FUT":
                    k = r["strike_paise"]
                    price = max(0, price - k) if r["kind"] == "CE" else max(0, k - price)
                sign, qty = -p.d, p.q
            _apply(p, sign, qty, price)
            if p.style == "premium":
                flows -= sign * qty * price
        charged = sum(x for td, x in charges if td <= d)
        where = f"{run_dir.name} {d}"
        if int(row["charges_paise"]) != charged:
            problems.append(f"{where}: charges {row['charges_paise']} but charges.csv adds up to {charged}")
        open_now = {k: p for k, p in pos.items() if p.q}
        if set(open_now) != set(eod[d]):
            problems.append(f"{where}: open positions {sorted(open_now)} but positions_eod lists {sorted(eod[d])}")
            continue
        unrealised = 0
        for k, p in open_now.items():
            r = eod[d][k]
            mark = int(r["mark_paise"])
            want = {"qty": p.d * p.q, "avg_price_paise": p.c // p.q, "realised_paise": p.realised,
                    "unrealised_paise": p.d * (p.q * mark - p.c)}
            for col, v in want.items():
                if int(r[col]) != v:
                    problems.append(f"{where}: {k} {col} {r[col]}, expected {v}")
            unrealised += want["unrealised_paise"]
        realised = sum(p.realised for p in pos.values())
        nav = int(row["nav_paise"])
        if nav != capital - charged + realised + unrealised:
            problems.append(f"{where}: NAV {nav} but capital − charges + realised + unrealised = "
                            f"{capital - charged + realised + unrealised}")
        if int(row["gross_pnl_paise"]) != nav - capital + charged:
            problems.append(f"{where}: gross P&L {row['gross_pnl_paise']}, expected {nav - capital + charged}")
        if not any(p.style == "variation" for p in open_now.values()):
            cash = capital - charged + flows + sum(p.realised for p in pos.values() if p.style == "variation")
            if int(row["cash_paise"]) != cash:
                problems.append(f"{where}: cash {row['cash_paise']}, expected {cash}")
    if i < len(events) and not allow_late:
        problems.append(f"{run_dir.name}: {len(events) - i} fills or settlements after the last trading day")
    return problems


def _both(test: unittest.TestCase, folder: Path, pack: Path, bundle: dict, strategy) -> int:  # noqa: ANN001
    """Run on both implementations (a fresh strategy each) and check each run folder. Returns the fill count."""
    for impl, runner in (("oracle", oracle_run), ("engine", engine.run_bundle)):
        runner(bundle, [pack], folder / impl, strategy=strategy(), report=False)
        test.assertEqual(accounting_problems(folder / impl, allow_late=True), [], impl)
    return len(_rows(folder / "oracle" / "fills.csv"))


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class AccountingIdentityTests(unittest.TestCase):
    def test_golden_worlds(self):
        from stockify.engine import strategy_for
        from tests.stockify.test_engine_parity import _cases
        fills = 0
        for name, g in _cases():
            with self.subTest(golden=name), tempfile.TemporaryDirectory(prefix="p06-") as tmp:
                folder = Path(tmp)
                pack, bundle = golden.build_pack(g, folder), golden.build_bundle(g)
                fills += _both(self, folder, pack, bundle, lambda b=bundle: strategy_for(b))
        self.assertGreater(fills, 30)

    def test_fuzz_seeds(self):
        from tests.stockify.test_engine_fuzz import SEEDS, run_both
        checked = 0
        for seed in SEEDS:
            with self.subTest(seed=seed), tempfile.TemporaryDirectory(prefix="p06-fuzz-") as tmp:
                folder = Path(tmp)
                oracle_error, engine_error = run_both(seed, folder)
                if oracle_error or engine_error:
                    continue                       # both failed alike (P-04 checks that); nothing to reconcile
                for impl in ("oracle", "engine"):
                    self.assertEqual(accounting_problems(folder / impl, allow_late=True), [], f"seed {seed} {impl}")
                checked += 1
        self.assertGreater(checked, 150)

    def test_strategy_worlds(self):
        from tests.stockify.test_engine_strategies import (REFS, _load, _materialise, daily_world, minute_world,
                                                           straddle_world)
        cases = [(daily_world(s, future=s % 2 == 0), REFS["sma_cross"]) for s in range(5200, 5212)]
        cases += [(minute_world(s), REFS["orb"]) for s in range(5300, 5310)]
        cases += [(straddle_world(s), REFS["straddle"]) for s in range(5400, 5410)]
        fills = 0
        for n, ((g, params), ref) in enumerate(cases):
            with self.subTest(case=n), tempfile.TemporaryDirectory(prefix="p06-strat-") as tmp:
                folder = Path(tmp)
                pack, bundle = _materialise(g, params, folder)
                fills += _both(self, folder, pack, bundle, lambda r=ref: _load(r))
        self.assertGreater(fills, 50)

    def test_a_wrong_number_is_caught(self):
        """The check is not vacuous: one paisa off in a fill price, a NAV or a charge is reported."""
        from tests.stockify.test_engine_strategies import REFS, _load, _materialise, daily_world
        g, params = daily_world(5201)
        with tempfile.TemporaryDirectory(prefix="p06-mut-") as tmp:
            folder = Path(tmp)
            pack, bundle = _materialise(g, params, folder)
            oracle_run(bundle, [pack], folder / "run", strategy=_load(REFS["sma_cross"]), report=False)
            run = folder / "run"
            self.assertEqual(accounting_problems(run), [])
            for name, col in (("fills.csv", "price_paise"), ("equity.csv", "nav_paise"), ("charges.csv", "total")):
                original = (run / name).read_bytes()
                rows = _rows(run / name)
                self.assertTrue(rows, name)
                rows[-1][col] = str(int(rows[-1][col]) + 1)
                with (run / name).open("w", encoding="utf-8", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
                    w.writeheader()
                    w.writerows(rows)
                self.assertNotEqual(accounting_problems(run), [], f"{name} {col}")
                (run / name).write_bytes(original)


if __name__ == "__main__":
    unittest.main()

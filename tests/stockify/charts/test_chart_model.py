"""CH 6.10: the chart model of a run, `stockify.reporting.chart.run_chart`.

Run folders are written by hand (CSVs, a log, a bundle and a real pack in the sandboxed packs folder) so each rule
has an exact, hand-computed expectation; one test runs the G10 golden (index options held to expiry) through the
oracle. Property tests are seeded stdlib loops (no `hypothesis`).
"""
from __future__ import annotations

import csv
import hashlib
import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tests.support  # noqa: F401  — must precede project imports

from stockify import golden, paths
from stockify.data import packs
from stockify.oracle import metrics
from stockify.oracle import run_bundle as oracle_run

NS = 1_000_000_000
MIN = 60 * NS
DAY = "2026-03-02"

ORDER_COLS = ("order_id,parent_id,instrument_id,side,product,type,validity,qty,price_paise,trigger_paise,"
              "protection_bp,tag,t_submit_ns,t_activate_ns,status,filled_qty,avg_price_paise,reason,note").split(",")
FILL_COLS = "fill_id,order_id,t_ns,instrument_id,side,qty,price_paise,evidence".split(",")
CHARGE_COLS = ("row_no,kind,order_id,instrument_id,trade_date,turnover_paise,brokerage,stt,exchange,sebi,stamp,gst,"
               "dp,other,total").split(",")
SHIPPED_WORDS = "Settled at expiry at {price}"


def T(h: int, m: int, s: int = 0, ns: int = 0, day: str = DAY) -> int:
    return golden.ts(f"{day}T{h:02d}:{m:02d}:{s:02d}+05:30") + ns


def order(oid: int, *, iid: int = 1, side: str = "BUY", qty: int = 10, note: str = "", tag: str = "",
          parent: int | None = None, product: str = "MIS") -> dict:
    return {"order_id": oid, "parent_id": parent, "instrument_id": iid, "side": side, "product": product,
            "type": "MARKET", "validity": "DAY", "qty": qty, "tag": tag, "status": "COMPLETE", "filled_qty": qty,
            "note": note}


def fill(fid: int, oid: int, t: int, side: str, qty: int, price: int, iid: int = 1) -> dict:
    return {"fill_id": fid, "order_id": oid, "t_ns": t, "instrument_id": iid, "side": side, "qty": qty,
            "price_paise": price, "evidence": "x"}


def charge(row: int, oid: int, total: int, *, kind: str = "order", iid: int = 1, date: str = DAY) -> dict:
    return {"row_no": row, "kind": kind, "order_id": oid, "instrument_id": iid, "trade_date": date, "total": total}


def _csv(path: Path, cols: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def bar_world(n: int = 5, *, first: int | None = None, interval_s: int = 60, iid: int = 1, extra=()) -> dict:
    """`n` BAR records of one instrument, one per interval from `first` (09:15 IST by default)."""
    first = T(9, 15) if first is None else first
    ev = [{"kind": "BAR", "instrument": iid, "interval_s": interval_s, "start": first + k * interval_s * NS,
           "o": 10000 + 10 * k, "h": 10005 + 10 * k, "l": 9995 + 10 * k, "c": 10002 + 10 * k, "v": 100}
          for k in range(n)]
    return {"id": f"W{n}", "events": [*ev, *extra]}


def install_pack(world: dict, *, basis: int = 0) -> str:
    """Build the world's pack into the sandboxed packs folder under its own sha256; returns the sha."""
    with tempfile.TemporaryDirectory(prefix="c10-pack-") as tmp:
        data = golden.build_pack(world, Path(tmp)).read_bytes()
    if basis:
        head = list(packs.HEADER.unpack_from(data, 0))
        head[-1] = basis
        data = packs.HEADER.pack(*head) + data[64:]
    sha = hashlib.sha256(data).hexdigest()
    (paths.packs_dir() / f"{sha}.evt").write_bytes(data)
    return sha


_serial = [0]


def make_run(*, orders=(), fills=(), charges=(), plots=None, sha: str | None = "bars", world: dict | None = None,
             log: str | None = None, bundle: dict | None = None, no_note_column: bool = False,
             instruments=None, with_bundle: bool = True) -> str:
    """Write a run folder under the sandboxed runs folder and return its id."""
    _serial[0] += 1
    run_id = f"c10-{_serial[0]}"
    folder = paths.run_path(run_id)
    folder.mkdir(parents=True)
    cols = [c for c in ORDER_COLS if not (no_note_column and c == "note")]
    _csv(folder / "orders.csv", cols, list(orders))
    _csv(folder / "fills.csv", FILL_COLS, list(fills))
    _csv(folder / "charges.csv", CHARGE_COLS, list(charges))
    if plots is not None:
        _csv(folder / "plots.csv", ["t_ns", "instrument_id", "name", "kind", "value"], plots)
    if log is not None:
        (folder / "events_log.txt").write_text(log, encoding="utf-8")
    if with_bundle:
        if sha == "bars":
            sha = install_pack(world or bar_world())
        doc = {"instruments": instruments or [{"id": 1, "tradingsymbol": "TEST", "kind": "EQ", "session_group": "CM",
                                               "cas_dates": []}],
               "packs": [{"sha256": sha}] if sha else [], "run": {"price_basis": "raw"},
               "system_notes": {"SETTLEMENT": SHIPPED_WORDS}}
        doc.update(bundle or {})
        (folder / "bundle.json").write_text(json.dumps(doc), encoding="utf-8")
    return run_id


FUT = [{"id": 1, "tradingsymbol": "TESTFUT", "kind": "FUT", "session_group": "FO_FUT", "cas_dates": []}]


def chart(run_id: str, *a, **kw) -> dict:
    from stockify.reporting.chart import run_chart
    return run_chart(run_id, *a, **kw)


class MarkerTests(unittest.TestCase):
    def test_marker_snaps_to_the_bar_containing_the_fill(self):
        rid = make_run(orders=[order(1), order(2), order(3, side="SELL")],
                       fills=[fill(1, 1, T(9, 16), "BUY", 10, 10010),                  # on a boundary: the later bar
                              fill(2, 2, T(9, 16, ns=-1), "BUY", 10, 10005),           # a nanosecond before it
                              fill(3, 3, T(15, 30), "SELL", 20, 10050)])               # eod_close, after the last bar
        out = chart(rid)
        got = [(m["fill_id"], m["bar_start_ns"]) for m in out["markers"]]
        self.assertEqual(got, [(2, T(9, 15)), (1, T(9, 16)), (3, T(9, 19))])
        self.assertEqual(out["omitted_markers"], 0)
        self.assertEqual(out["data_state"], "ok")
        self.assertEqual(out["interval_s"], 60)
        self.assertEqual([b["start_ns"] for b in out["bars"]], [T(9, 15 + k) for k in range(5)])

    def test_a_minute_with_no_bar_takes_the_last_bar_before_it(self):
        world = bar_world(5)
        world["events"] = [e for e in world["events"] if e["start"] != T(9, 17)]       # no 09:17 bar
        rid = make_run(orders=[order(1)], fills=[fill(1, 1, T(9, 17, 30), "BUY", 10, 10020)], world=world)
        self.assertEqual(chart(rid)["markers"][0]["bar_start_ns"], T(9, 16))

    def test_two_fills_in_one_bar_are_both_marked_in_fill_order(self):
        rid = make_run(orders=[order(1, qty=20)],
                       fills=[fill(4, 1, T(9, 16, 10), "BUY", 10, 10010), fill(3, 1, T(9, 16, 10), "BUY", 10, 10011),
                              fill(5, 1, T(9, 16, 40), "BUY", 5, 10012)])
        m = chart(rid)["markers"]
        self.assertEqual([x["fill_id"] for x in m], [3, 4, 5])
        self.assertEqual({x["bar_start_ns"] for x in m}, {T(9, 16)})

    def test_note_falls_back_to_the_parent_then_to_the_tag(self):
        markup = '<img src=x onerror=alert(1)> {price} %s [b]'
        rid = make_run(orders=[order(1, note=markup, tag="entry"), order(2, parent=1, tag="child"),
                               order(3, tag="AUTO_SQOFF"), order(4, tag="mine"), order(5)],
                       fills=[fill(n, n, T(9, 15 + n % 4, n), "BUY", 10, 10000 + n) for n in range(1, 6)])
        m = {x["order_id"]: x for x in chart(rid)["markers"]}
        self.assertEqual(m[1]["note"], markup)                       # exactly as stored, never interpreted
        self.assertEqual((m[1]["tag"], m[1]["tag_is_code"]), ("entry", False))
        self.assertEqual((m[2]["note"], m[2]["tag"], m[2]["tag_is_code"]), (markup, "child", False))   # the parent's
        self.assertEqual((m[3]["note"], m[3]["tag"], m[3]["tag_is_code"]), ("", "AUTO_SQOFF", True))   # a system code
        self.assertEqual((m[4]["note"], m[4]["tag"], m[4]["tag_is_code"]), ("", "mine", False))
        self.assertEqual((m[5]["note"], m[5]["tag"], m[5]["tag_is_code"]), ("", "", False))

    def test_charges_shown_once_on_the_last_fill_of_a_multi_fill_order(self):
        rid = make_run(orders=[order(1, qty=30), order(2), order(3)],
                       fills=[fill(1, 1, T(9, 15, 1), "BUY", 10, 10000), fill(2, 1, T(9, 15, 2), "BUY", 10, 10001),
                              fill(3, 1, T(9, 15, 3), "BUY", 10, 10002), fill(4, 2, T(9, 16), "BUY", 10, 10010),
                              fill(5, 3, T(9, 17), "BUY", 10, 10020)],
                       charges=[charge(1, 1, 120), charge(2, 1, 30), charge(3, 2, 77),
                                charge(4, 2, 999, kind="settlement")])
        m = {x["fill_id"]: x["charges_paise"] for x in chart(rid)["markers"]}
        self.assertEqual(m, {1: None, 2: None, 3: 150, 4: 77, 5: None})      # no row: None, not 0

    def test_trade_pnl_for_a_partial_exit_flip_and_full_exit(self):
        orders = [order(n) for n in range(1, 6)]
        fills = [fill(1, 1, T(9, 15), "BUY", 10, 10000), fill(2, 2, T(9, 16), "BUY", 5, 10100),
                 fill(3, 3, T(9, 17), "SELL", 12, 10200),      # 10 @ 10000 (+2000), 2 @ 10100 (+200); 3 left
                 fill(4, 4, T(9, 18), "SELL", 8, 10300),       # closes 3 @ 10100 (+600), opens short 5 @ 10300
                 fill(5, 5, T(9, 19), "BUY", 5, 10250)]        # closes the short: 5 * (10300 - 10250) = +250
        m = {x["fill_id"]: x for x in chart(make_run(orders=orders, fills=fills))["markers"]}
        self.assertEqual([m[n]["trade_pnl_paise"] for n in range(1, 6)], [None, None, 2200, 600, 250])
        self.assertEqual(m[3]["chunks"], [{"qty": 10, "open_price_paise": 10000, "pnl_paise": 2000},
                                          {"qty": 2, "open_price_paise": 10100, "pnl_paise": 200}])
        self.assertEqual(m[4]["chunks"], [{"qty": 3, "open_price_paise": 10100, "pnl_paise": 600}])
        self.assertEqual(m[5]["chunks"], [{"qty": 5, "open_price_paise": 10300, "pnl_paise": 250}])
        self.assertEqual(m[1]["chunks"], [])

    def test_pnl_uses_all_fills_even_when_the_range_hides_the_opening_fill(self):
        rid = make_run(orders=[order(1), order(2, side="SELL")],
                       fills=[fill(1, 1, T(9, 15, 5), "BUY", 10, 10000), fill(2, 2, T(9, 18, 5), "SELL", 10, 10400)])
        out = chart(rid, 1, T(9, 17), None)
        self.assertEqual([m["fill_id"] for m in out["markers"]], [2])
        self.assertEqual(out["markers"][0]["trade_pnl_paise"], 4000)
        self.assertEqual(out["omitted_markers"], 1)
        self.assertEqual(out["bars"][0]["start_ns"], T(9, 17))

    def test_a_fill_before_the_first_bar_is_counted_not_returned(self):
        rid = make_run(orders=[order(1), order(2)],
                       fills=[fill(1, 1, T(9, 0), "BUY", 10, 10000), fill(2, 2, T(9, 16), "BUY", 10, 10010)])
        out = chart(rid)
        self.assertEqual(([m["fill_id"] for m in out["markers"]], out["omitted_markers"]), ([2], 1))


class SettlementTests(unittest.TestCase):
    def test_settlement_with_nothing_open_is_skipped(self):
        log = f"{T(15, 30)} INFO settled 1 MIS at FSP 10100 (settle)\n"
        rid = make_run(orders=[order(1), order(2, side="SELL")],
                       fills=[fill(1, 1, T(9, 15), "BUY", 10, 10000), fill(2, 2, T(9, 16), "SELL", 10, 10010)], log=log,
                       instruments=FUT)
        out = chart(rid)
        self.assertEqual((out["skipped_settlements"], out["pnl_complete"], out["notice"]), (0, True, None))
        self.assertEqual([m["kind"] for m in out["markers"]], ["fill", "fill"])

    def test_settlement_of_an_open_position_closes_it_in_the_fifo_loop(self):
        log = f"{T(15, 30)} INFO settled 1 MIS at FSP 10100 (settle)\n"
        for side, opening, want in (("BUY", 10000, 10 * (10100 - 10000)), ("SELL", 10200, 10 * (10200 - 10100))):
            rid = make_run(orders=[order(1, side=side)], fills=[fill(1, 1, T(9, 15), side, 10, opening)], log=log,
                           charges=[charge(1, 1, 5), charge(2, 0, 40, kind="settlement")], instruments=FUT)
            out = chart(rid)
            self.assertEqual(out["markers"][0]["trade_pnl_paise"], None)
            s = out["markers"][1]
            self.assertEqual((s["kind"], s["side"], s["qty"], s["price_paise"]),
                             ("settlement", "SELL" if side == "BUY" else "BUY", 10, 10100))
            self.assertEqual((s["fill_id"], s["order_id"], s["tag"], s["tag_is_code"]), (None, None, "", False))
            self.assertEqual((s["trade_pnl_paise"], s["charges_paise"]), (want, 40))
            self.assertEqual(s["bar_start_ns"], T(9, 19))                    # after the last bar: the last bar
            self.assertEqual(s["note"], "Settled at expiry at 101.00")

    def test_settlement_words_are_filled_exactly_and_default_to_the_shipped_ones(self):
        log = f"{T(9, 16, 30)} INFO settled 1 MIS at FSP 123456789 (settle)\n"
        kw = dict(orders=[order(1)], fills=[fill(1, 1, T(9, 15), "BUY", 10, 10000)], log=log, instruments=FUT)
        text = "{0} {price} {x} {price} <b>%s</b>"
        out = chart(make_run(bundle={"system_notes": {"SETTLEMENT": text}}, **kw))
        self.assertEqual(out["markers"][1]["note"], "{0} 12,34,567.89 {x} 12,34,567.89 <b>%s</b>")
        no_notes = make_run(**kw)
        path = paths.run_path(no_notes) / "bundle.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        del doc["system_notes"]                                              # an older bundle
        path.write_text(json.dumps(doc), encoding="utf-8")
        self.assertEqual(chart(no_notes)["markers"][1]["note"], "Settled at expiry at 12,34,567.89")
        doc["system_notes"] = {}
        path.write_text(json.dumps(doc), encoding="utf-8")
        self.assertEqual(chart(no_notes)["markers"][1]["note"], "")

    def test_read_settlements_reads_both_patterns_and_a_missing_log_as_none(self):
        from stockify.reporting.settlements import Settlement, read_settlements
        rid = make_run(log=(f"5 INFO settled 7 NRML at FSP 24500 (settle)\nnoise\n"
                            f"9 INFO delivered 3 CNC BUY 500 at 1234 (fallback)\n"
                            f"10 WARN settled 7 NRML at FSP 1 (settle)\n"))
        self.assertEqual(read_settlements(paths.run_path(rid)),
                         [Settlement(5, 7, "NRML", 24500, "cash", None, None, "settle"),
                          Settlement(9, 3, "CNC", 1234, "delivery", "BUY", 500, "fallback")])
        self.assertEqual(read_settlements(paths.run_path(make_run())), [])

    def test_physical_delivery_is_a_marker_without_a_profit(self):
        log = f"{T(15, 30)} INFO delivered 1 CNC BUY 40 at 10100 (settle)\n"
        out = chart(make_run(orders=[order(1)], fills=[fill(1, 1, T(9, 15), "BUY", 10, 10000)], log=log))
        d = out["markers"][1]
        self.assertEqual((d["kind"], d["side"], d["qty"], d["price_paise"], d["trade_pnl_paise"], d["chunks"]),
                         ("settlement", "BUY", 40, 10100, None, []))

    def test_physical_delivery_says_the_profit_is_not_complete(self):
        log = f"{T(15, 30)} INFO delivered 1 CNC BUY 40 at 10100 (settle)\n"
        out = chart(make_run(orders=[order(1)], fills=[fill(1, 1, T(9, 15), "BUY", 10, 10000)], log=log))
        self.assertFalse(out["pnl_complete"])
        self.assertEqual(out["notice"], "Profit for contracts settled by delivery isn't shown per trade; the run's "
                                        "total in its metrics includes it.")
        self.assertEqual(out["markers"][1]["fsp_paise"], 10100)

    def test_an_option_without_a_kind_or_strike_is_skipped_and_counted_not_guessed(self):
        log = f"{T(15, 30)} INFO settled 1 NRML at FSP 2450000 (settle)\n"
        for row in ({"id": 1, "tradingsymbol": "X", "kind": "CE"},                      # no strike
                    {"id": 1, "tradingsymbol": "X", "strike_paise": 2400000},            # no kind
                    {"id": 1, "tradingsymbol": "X", "kind": "PE", "strike_paise": None}):
            rid = make_run(orders=[order(1, product="NRML")], fills=[fill(1, 1, T(9, 15), "BUY", 65, 10000)], log=log,
                           instruments=[row])
            out = chart(rid)
            self.assertEqual([m["kind"] for m in out["markers"]], ["fill"])
            self.assertEqual((out["skipped_settlements"], out["pnl_complete"]), (1, False))
            self.assertIn("1 expiry settlement", out["notice"])
        ok = make_run(orders=[order(1, product="NRML")], fills=[fill(1, 1, T(9, 15), "BUY", 65, 10000)], log=log,
                      instruments=[{"id": 1, "tradingsymbol": "X", "kind": "CE", "strike_paise": 2400000}])
        s = chart(ok)["markers"][1]
        self.assertEqual((s["price_paise"], s["fsp_paise"], s["trade_pnl_paise"]), (50000, 2450000, 65 * 40000))

    def test_a_corrupt_pack_is_a_data_state_not_an_error(self):
        sha = "ab" * 32
        (paths.packs_dir() / f"{sha}.evt").write_bytes(b"not a pack at all" * 10)
        out = chart(make_run(sha=sha, orders=[order(1)], fills=[fill(1, 1, T(9, 16, 5), "BUY", 10, 10000)]))
        self.assertEqual((out["data_state"], out["bars"]), ("pack unreadable", []))
        self.assertEqual(out["markers"][0]["bar_start_ns"], T(9, 16))


class G10GoldenTests(unittest.TestCase):
    """Index options held to expiry: a long ITM call, a long OTM call and a short ITM put (G10)."""

    @classmethod
    def setUpClass(cls):
        g = golden.load(golden.GOLDEN_DIR / "G10.yaml")
        g.pop("variants")
        with tempfile.TemporaryDirectory(prefix="c10-g10-") as tmp:
            pack = golden.build_pack(g, Path(tmp))
            sha = install_pack(g)
            bundle = golden.build_bundle(g)
            bundle["packs"] = [{"sha256": sha, "path": f"packs/{sha}.evt"}]
            folder = paths.run_path("c10-g10")
            cls.result = oracle_run(bundle, [pack], folder, report=False)
        cls.folder = folder

    def test_expiry_settlement_gets_a_settlement_marker_with_note_pnl_and_charges(self):
        out = chart("c10-g10", 2)
        fill_m, settle_m = out["markers"]
        self.assertEqual((fill_m["kind"], fill_m["side"], fill_m["qty"], fill_m["price_paise"]), ("fill", "BUY", 65, 10000))
        self.assertEqual((settle_m["kind"], settle_m["side"], settle_m["qty"]), ("settlement", "SELL", 65))
        self.assertEqual(settle_m["price_paise"], 50000)                     # intrinsic: FSP 24,500.00 - strike 24,000.00
        self.assertEqual(settle_m["note"], "Settled at expiry at 500.00")
        self.assertEqual(settle_m["trade_pnl_paise"], 65 * (50000 - 10000))
        self.assertEqual(settle_m["chunks"], [{"qty": 65, "open_price_paise": 10000, "pnl_paise": 2_600_000}])
        self.assertEqual(settle_m["charges_paise"], 6423)                    # the settlement row of G10's notes
        self.assertEqual(settle_m["bar_start_ns"], T(9, 16, day="2026-03-03"))   # after the last bar: the last bar
        self.assertIsNone(fill_m["trade_pnl_paise"])
        self.assertEqual(settle_m["fsp_paise"], 2_450_000)                   # the raw log value beside the close used
        self.assertEqual((out["pnl_complete"], out["skipped_settlements"], out["notice"]), (True, 0, None))

    def test_every_chunk_sums_to_the_runs_own_total_profit(self):
        total = 0
        for iid in (2, 3, 4):
            total += sum(m["trade_pnl_paise"] or 0 for m in chart("c10-g10", iid)["markers"])
        self.assertEqual(total, 2_600_000 - 32_500 + 0)
        metrics_json = json.loads((self.folder / "metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(total, metrics_json["gross_pnl_paise"])             # every position is settled: flat
        self.assertEqual(total, round(metrics_json["avg_trade_paise"] * metrics_json["trades"]))

    def test_instrument_picker_lists_every_traded_instrument_including_fo_legs(self):
        out = chart("c10-g10")
        self.assertEqual(out["instruments"], [{"id": 2, "symbol": "NIFTY2630324000CE"},
                                              {"id": 3, "symbol": "NIFTY2630325000CE"},
                                              {"id": 4, "symbol": "NIFTY2630325000PE"}])
        self.assertEqual(out["instrument_id"], 2)                            # the lowest traded id
        self.assertEqual(chart("c10-g10", 4)["markers"][1]["side"], "BUY")   # the short put is closed by a buy
        with self.assertRaises(LookupError):
            chart("c10-g10", 99)


class DataStateTests(unittest.TestCase):
    def test_no_trades_run_still_has_bars(self):
        out = chart(make_run())
        self.assertEqual((len(out["bars"]), out["markers"], out["plots"], out["omitted_markers"]), (5, [], [], 0))
        self.assertEqual(out["instruments"], [{"id": 1, "symbol": "TEST"}])
        self.assertEqual(out["instrument_id"], 1)

    def test_old_orders_csv_without_note_reads_as_empty_notes(self):
        rid = make_run(orders=[order(1, note="never written", tag="AUTO_SQOFF")], no_note_column=True,
                       fills=[fill(1, 1, T(9, 15), "BUY", 10, 10000)])
        out = chart(rid)
        self.assertEqual((out["markers"][0]["note"], out["markers"][0]["tag_is_code"]), ("", True))
        self.assertEqual(out["plots"], [])                                   # and no plots.csv at all

    def test_a_folder_with_no_csvs_at_all_does_not_raise(self):
        rid = make_run()
        for name in ("orders.csv", "fills.csv", "charges.csv"):
            (paths.run_path(rid) / name).unlink()
        self.assertEqual(chart(rid)["markers"], [])

    def test_pruned_pack_gives_markers_on_an_empty_axis(self):
        rid = make_run(sha="0" * 64, orders=[order(1)], fills=[fill(1, 1, T(9, 16, 37, 5), "BUY", 10, 10000)])
        out = chart(rid)
        self.assertEqual((out["data_state"], out["bars"], out["truncated"], out["next_from_ns"]), ("pruned", [], False, None))
        self.assertEqual(out["markers"][0]["bar_start_ns"], T(9, 16))        # rounded down to the minute
        self.assertEqual(out["omitted_markers"], 0)
        self.assertEqual(out["basis"], "raw")

    def test_a_pack_hash_that_is_not_a_hash_is_pruned_not_a_path(self):
        rid = make_run(sha="../../x", orders=[order(1)], fills=[fill(1, 1, T(9, 16), "BUY", 10, 10000)])
        self.assertEqual(chart(rid)["data_state"], "pruned")

    def test_bundle_less_and_pack_less_folders_are_no_pack(self):
        for rid in (make_run(with_bundle=False, orders=[order(1)], fills=[fill(1, 1, T(9, 16, 5), "BUY", 10, 10000)]),
                    make_run(sha=None, orders=[order(1)], fills=[fill(1, 1, T(9, 16, 5), "BUY", 10, 10000)])):
            out = chart(rid)
            self.assertEqual((out["data_state"], out["bars"]), ("no_pack", []))
            self.assertEqual(out["markers"][0]["bar_start_ns"], T(9, 16))

    def test_basis_comes_from_the_pack_not_from_a_refetch(self):
        sha = install_pack(bar_world(), basis=1)
        rid = make_run(sha=sha, orders=[order(1)], fills=[fill(1, 1, T(9, 16), "BUY", 10, 10000)])
        import sqlite3
        boom = AssertionError("a chart read must not open a database")
        with patch("sqlite3.connect", side_effect=boom), patch("stockify.store._connect", side_effect=boom):
            out = chart(rid)
        self.assertEqual((out["basis"], len(out["bars"])), ("adjusted", 5))   # the bundle says "raw"; the pack is truth
        self.assertTrue(sqlite3.connect)

    def test_unknown_run_raises_a_lookup_error(self):
        with self.assertRaises(LookupError):
            chart("no-such-run")
        with self.assertRaises(ValueError):
            chart("../escape")

    def test_interval_must_be_a_positive_whole_number(self):
        rid = make_run()
        for bad in (0, -60, 1.5, "60", True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                chart(rid, interval_s=bad)
        from stockify.reporting.chartbars import bars_from_records
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                bars_from_records([], 1, interval_s=bad, anchors={})
        for kw in ({"instrument_id": "1"}, {"from_ns": 1.5}, {"to_ns": True}):
            with self.assertRaises(ValueError, msg=repr(kw)):
                chart(rid, **kw)

    def test_only_daily_bars_say_so_when_another_interval_was_asked_for(self):
        day = {"id": "D", "events": [{"kind": "BAR", "instrument": 1, "interval_s": 86400, "start": T(9, 15) + k * 86400 * NS,
                                      "o": 1, "h": 2, "l": 1, "c": 2, "v": 3} for k in range(3)]}
        rid = make_run(world=day)
        out = chart(rid, interval_s=60)
        self.assertEqual((out["interval_s"], len(out["bars"])), (86400, 3))
        self.assertIn("86400", out["notice"])
        asked = chart(rid, interval_s=86400)
        plain = chart(rid)
        self.assertEqual((asked["notice"], plain["notice"], plain["interval_s"]), (None, None, 86400))

    def test_tick_runs_are_built_at_the_asked_interval(self):
        ticks = {"id": "TK", "events": [{"kind": "TICK_QUOTE", "instrument": 1, "t": T(9, 15) + s * NS, "ltp": 10000 + s,
                                         "volume": 10 * s} for s in range(0, 600, 20)]}
        spec = [{"from": "2026-01-01", "lot": 1, "tick_paise": 5}]
        bundle = golden.build_bundle({"id": "TK", "bundle": {"dates": [DAY], "instruments": [
            {"id": 1, "kind": "EQ", "tradingsymbol": "TEST", "specs": spec}]}, "events": ticks["events"],
            "strategy": {"actions": []}})
        rid = make_run(world=ticks, bundle={k: bundle[k] for k in ("days", "prev_days", "next_days") if k in bundle})
        self.assertEqual([chart(rid)["interval_s"], len(chart(rid)["bars"])], [60, 10])
        five = chart(rid, interval_s=300)
        self.assertEqual((five["interval_s"], len(five["bars"])), (300, 2))
        self.assertIsNone(five["notice"])

    def test_plots_come_with_their_series_inside_the_window(self):
        plots = [{"t_ns": T(9, 15, 5), "instrument_id": 1, "name": "sma", "kind": "price", "value": 10001},
                 {"t_ns": T(9, 17, 5), "instrument_id": 1, "name": "sma", "kind": "price", "value": 10020},
                 {"t_ns": T(9, 17, 5), "instrument_id": 0, "name": "rsi", "kind": "level", "value": 543217},
                 {"t_ns": T(9, 17, 5), "instrument_id": 2, "name": "other", "kind": "price", "value": 5}]
        rid = make_run(plots=plots)
        out = chart(rid)
        self.assertEqual(out["plots"], [
            {"name": "sma", "kind": "price", "instrument_id": 1, "points": [[T(9, 15, 5), 10001], [T(9, 17, 5), 10020]]},
            {"name": "rsi", "kind": "level", "instrument_id": 0, "points": [[T(9, 17, 5), 543217]]}])
        windowed = chart(rid, 1, T(9, 16), None)
        self.assertEqual([(p["name"], p["points"]) for p in windowed["plots"]],
                         [("sma", [[T(9, 17, 5), 10020]]), ("rsi", [[T(9, 17, 5), 543217]])])


def _minute_bars(n: int) -> tuple[dict, int]:
    first = T(9, 15)
    return bar_world(n, first=first), first


class PagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        world, cls.first = _minute_bars(60_000)
        cls.sha = install_pack(world)

    def _run(self, fill_bar: int | None = None) -> str:
        kw = {}
        if fill_bar is not None:
            kw = dict(orders=[order(1)], fills=[fill(1, 1, self.first + fill_bar * MIN + 5 * NS, "BUY", 10, 10000)])
        return make_run(sha=self.sha, **kw)

    def test_pages_join_without_a_gap_or_a_duplicate_bar(self):
        rid = self._run()
        one = chart(rid, 1, self.first, None)
        self.assertEqual((len(one["bars"]), one["truncated"]), (50_000, True))
        self.assertEqual(one["next_from_ns"], self.first + 50_000 * MIN)
        two = chart(rid, 1, one["next_from_ns"], None)
        self.assertEqual((len(two["bars"]), two["truncated"], two["next_from_ns"]), (10_000, False, None))
        starts = [b["start_ns"] for b in one["bars"] + two["bars"]]
        self.assertEqual(starts, [self.first + k * MIN for k in range(60_000)])
        capped = chart(rid, 1, self.first, self.first + 70_000 * MIN)         # a range past the data
        self.assertEqual((len(capped["bars"]), capped["truncated"]), (50_000, True))
        exact = chart(rid, 1, self.first, self.first + 50_000 * MIN)          # a range of exactly one page
        self.assertEqual((len(exact["bars"]), exact["truncated"], exact["next_from_ns"]), (50_000, False, None))
        small = chart(rid, 1, self.first + 10 * MIN, self.first + 13 * MIN)
        self.assertEqual([b["start_ns"] for b in small["bars"]], [self.first + k * MIN for k in (10, 11, 12)])

    def test_default_window_is_around_the_first_trade(self):
        late = chart(self._run(55_000))                                        # the window ends with the data
        self.assertEqual((len(late["bars"]), late["bars"][0]["start_ns"], late["truncated"]),
                         (50_000, self.first + 10_000 * MIN, False))
        self.assertEqual(late["markers"][0]["bar_start_ns"], self.first + 55_000 * MIN)
        early = chart(self._run(100))
        self.assertEqual((len(early["bars"]), early["bars"][0]["start_ns"], early["truncated"], early["next_from_ns"]),
                         (50_000, self.first, True, self.first + 50_000 * MIN))
        mid = chart(self._run(30_000))                                         # centred on it
        self.assertEqual(mid["bars"][0]["start_ns"], self.first + 5_000 * MIN)
        self.assertEqual(mid["markers"][0]["bar_start_ns"], self.first + 30_000 * MIN)
        self.assertEqual(mid["omitted_markers"], 0)

    def test_without_a_trade_or_a_range_the_last_five_thousand_bars(self):
        out = chart(self._run())
        self.assertEqual((len(out["bars"]), out["bars"][0]["start_ns"], out["truncated"]),
                         (5_000, self.first + 55_000 * MIN, False))

    def test_a_fill_outside_the_returned_page_is_counted(self):
        rid = self._run(59_000)
        out = chart(rid, 1, self.first, None)                                  # the first page holds no fill
        self.assertEqual((out["markers"], out["omitted_markers"], out["truncated"]), ([], 1, True))


class ModelPropertyTests(unittest.TestCase):
    def _random_run(self, rng: random.Random):
        n_bars = rng.randint(3, 40)
        first = T(9, 15)
        iids = (1, 2)
        sha = install_pack({"id": "P", "events": [e for iid in iids for e in bar_world(n_bars, iid=iid)["events"]]})
        orders, fills, charges, fid = [], [], [], 0
        end = first + n_bars * MIN
        for iid in iids:
            net = 0
            for _ in range(rng.randint(0, 12)):
                oid = len(orders) + 1
                side = rng.choice(("BUY", "SELL"))
                orders.append(order(oid, iid=iid, side=side, product="MIS", note=rng.choice(("", "n%d" % oid))))
                for _ in range(rng.randint(1, 3)):
                    fid += 1
                    qty = rng.randint(1, 20)
                    t = rng.choice((first - 5 * MIN, first + rng.randrange(n_bars * MIN), end + MIN))
                    fills.append(fill(fid, oid, t, side, qty, rng.randint(9000, 11000), iid))
                if rng.random() < 0.5:
                    charges.append(charge(len(charges) + 1, oid, rng.randint(0, 99), iid=iid))
        return sha, orders, fills, charges, first, n_bars

    def test_markers_are_a_subset_of_fills_and_sorted(self):
        rng = random.Random(610)
        rich = 0
        for _ in range(60):
            sha, orders, fills, charges, first, n_bars = self._random_run(rng)
            rid = make_run(sha=sha, orders=orders, fills=fills, charges=charges, instruments=[
                {"id": 1, "tradingsymbol": "A", "kind": "EQ", "session_group": "CM", "cas_dates": []},
                {"id": 2, "tradingsymbol": "B", "kind": "EQ", "session_group": "CM", "cas_dates": []}])
            for iid in (1, 2):
                a = first + rng.randrange(n_bars) * MIN if rng.random() < 0.5 else None
                b = first + rng.randrange(n_bars + 1) * MIN if rng.random() < 0.5 else None
                out = chart(rid, iid, a, b)
                mine = {f["fill_id"]: f for f in fills if f["instrument_id"] == iid}
                got = out["markers"]
                self.assertEqual([m["fill_id"] for m in got].__len__() + out["omitted_markers"], len(mine))
                self.assertEqual([(m["t_ns"], m["fill_id"]) for m in got], sorted((m["t_ns"], m["fill_id"]) for m in got))
                starts = [bar["start_ns"] for bar in out["bars"]]
                for m in got:
                    src = mine[m["fill_id"]]
                    self.assertEqual((m["t_ns"], m["qty"], m["price_paise"], m["side"]),
                                     (src["t_ns"], src["qty"], src["price_paise"], src["side"]))
                    self.assertIn(m["bar_start_ns"], starts)
                    self.assertLessEqual(m["bar_start_ns"], m["t_ns"])
                rich += bool(got) and bool(out["omitted_markers"])
        self.assertGreater(rich, 5)                   # the generator reached both a shown and an omitted fill

    def test_marker_trade_pnl_sums_to_the_cash_flow_of_a_flat_run(self):
        rng = random.Random(611)
        checked = 0
        for _ in range(60):
            sha, orders, fills, charges, first, n_bars = self._random_run(rng)
            for iid in (1, 2):                                                # end flat, inside the bars
                net = sum(f["qty"] * (1 if f["side"] == "BUY" else -1) for f in fills if f["instrument_id"] == iid)
                fills = [f for f in fills if not (f["instrument_id"] == iid and f["t_ns"] < first)]
            fills = [{**f, "t_ns": min(max(f["t_ns"], first), first + n_bars * MIN + MIN)} for f in fills]
            for iid in (1, 2):
                net = sum(f["qty"] * (1 if f["side"] == "BUY" else -1) for f in fills if f["instrument_id"] == iid)
                if net:
                    oid = len(orders) + 1
                    orders.append(order(oid, iid=iid, side="SELL" if net > 0 else "BUY"))
                    fills.append(fill(len(fills) + 1000, oid, first + n_bars * MIN + 2 * MIN, "SELL" if net > 0 else "BUY",
                                      abs(net), 10500, iid))
            rid = make_run(sha=sha, orders=orders, fills=fills, instruments=[
                {"id": 1, "tradingsymbol": "A", "kind": "EQ", "session_group": "CM", "cas_dates": []},
                {"id": 2, "tradingsymbol": "B", "kind": "EQ", "session_group": "CM", "cas_dates": []}])
            for iid in (1, 2):
                mine = [f for f in fills if f["instrument_id"] == iid]
                if not mine:
                    continue
                out = chart(rid, iid)
                self.assertEqual(out["omitted_markers"], 0)
                cash = sum(f["qty"] * f["price_paise"] * (1 if f["side"] == "SELL" else -1) for f in mine)
                self.assertEqual(sum(m["trade_pnl_paise"] or 0 for m in out["markers"]), cash)
                self.assertEqual(sum(c["pnl_paise"] for m in out["markers"] for c in m["chunks"]), cash)
                checked += 1
        self.assertGreater(checked, 30)


if __name__ == "__main__":
    unittest.main()

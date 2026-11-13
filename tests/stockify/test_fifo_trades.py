"""metrics.fifo_trades, the one FIFO loop the metrics and the chart's trade markers share (CH 6.5)."""
from __future__ import annotations

import hashlib
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import golden, numerics
from stockify.oracle import metrics


def _scenarios():
    """(label, golden dict) for every simulated golden and each of its variants, as golden.run_one runs them."""
    for _, g in golden.goldens():
        if not g.get("variants"):
            yield g["id"], g
            continue
        for v in g["variants"]:
            h = {k: x for k, x in g.items() if k != "variants"}
            for part in ("bundle", "run", "strategy"):
                if part in v:
                    h[part] = golden._merge(h.get(part, {}), v[part])
            if "drop_events" in v:
                h["events"] = [e for n, e in enumerate(g["events"], start=1) if n not in v["drop_events"]]
            yield f"{g['id']}/{v['name']}", h


def metrics_hashes() -> dict[str, str]:
    """sha256 of the canonical metrics.json text of each golden scenario that runs to completion."""
    from stockify.oracle import run_bundle
    out = {}
    for label, g in _scenarios():
        if g.get("expect", {}).get("error"):
            continue
        with tempfile.TemporaryDirectory(prefix="fifo-") as tmp:
            folder = Path(tmp)
            pack = golden.build_pack(g, folder)
            res = run_bundle(golden.build_bundle(g), [pack], folder / "run", report=False)
            text = numerics.dumps_canonical(res.metrics) + "\n"
            out[label] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return out


# Captured with the loop still inside metrics.compute, before it moved (C5 step 2).
PRE_CHANGE_HASHES: dict[str, str] = {
    "G01": "59040baff6bcaba5f432f392575c0eacc80c18f5757ceb34baaf711c2dab452d",
    "G02/through": "5422f9b73d8b1fa172767d53e232d6e32ad1b1aee3c0ab4f345a2670ced83010",
    "G02/touch": "fc61e4f583e452413484c2988ae857ee101b5b6d97d3a7ba73b31c7ce0e7f8f4",
    "G03": "f46bbff6fc5b31195547875dde135c2913680cd9bf78c7bb6a3fdc573d2b5e12",
    "G04": "119aa58fd002fe0523215094aa93f057d18bc3032f53c7f86a3477a6ed2e48a8",
    "G05": "9f2aad0fddc50fbea2ae6399cf948d7afc54e34c5de6ae1c27cfa3d9690b1064",
    "G06": "f7ba208c2405b33b0e4fa2ac4b9007a7469887954627fca25ca34acffe9905fa",
    "G07": "1c3786cd9b2d8edbea4f5f9e475eff8dd24630f526e63533a45625d3c43ec32c",
    "G09": "6904ad3515a69991052615687a6caf2039f2fe9a2c208a7261a74f65f94f8660",
    "G10/fallback": "200770730dc6cd0496060b30b3580fc99f35fed8cfe0a5ca860ccb7a5bd3ed4f",
    "G10/fsp": "200770730dc6cd0496060b30b3580fc99f35fed8cfe0a5ca860ccb7a5bd3ed4f",
    "G11/fallback": "105db3b873297dc85225130e0d7c18fd21d037b19755987a07f769d4e05d79bc",
    "G11/fsp": "105db3b873297dc85225130e0d7c18fd21d037b19755987a07f769d4e05d79bc",
    "G12": "ccb3b25955f7a4471cd9e26b5cb9a4ed585157b4e99a1d450bb0b25d6e7a16dd",
    "G14": "dde529c1d652834d7c507c2be472491312d608ce5066c788c2b4d3a2f939d335",
    "G16/freeze_1800": "999b8cb0317f9c9abf9c0dc7d7b89583506bdecc190f0fca406ffcd9eb1e301c",
    "G16/freeze_3510": "33fbb71cf1b6d28dd0d4a9cbfb1db778653132f39ba96b68cb1f7dd95b68af2b",
    "G19": "c3dfd28c918dcadff457e69cf4646808e816fb39bd00c614a7b22fe88ebbcb28",
    "G20/queue": "e01b5366466348d249e8e776f11d5210b904b2e8c279b9e2b7b0cb1105b66ab4",
    "G20/reject": "5a262fbff13c64fb923353030a83cd896c24f87a6636f82387c2fbea2a8cb578",
    "G21": "d32817cbfe7af0b4a00a072c0df8f3508d0c3ecd7baed536555dd340647920d3",
}


def F(fill_id, order_id, t, iid, side, qty, price):
    return NS(fill_id=fill_id, order_id=order_id, t_ns=t, instrument_id=iid, side=side, qty=qty, price=price)


PRODUCTS = {1: "MIS", 2: "NRML"}


class StoredMetricsTests(unittest.TestCase):
    def test_compute_is_byte_identical_to_the_stored_metrics(self):
        self.assertEqual(metrics_hashes(), PRE_CHANGE_HASHES)


class FifoTradesTests(unittest.TestCase):
    def test_partial_exit_is_a_trade_for_the_closed_quantity(self):
        chunks, _ = metrics.fifo_trades([F(1, 1, 10, 7, "BUY", 10, 100), F(2, 1, 20, 7, "SELL", 4, 110)], None, PRODUCTS)
        self.assertEqual(len(chunks), 1)
        c = chunks[0]
        self.assertEqual((c.qty, c.open_side, c.open_price, c.close_price, c.pnl), (4, "BUY", 100, 110, 40))
        self.assertEqual((c.close_t_ns, c.close_fill_id, c.close_order_id, c.instrument_id, c.product),
                         (20, 2, 1, 7, "MIS"))

    def test_flip_closes_the_old_position_and_opens_a_new_one(self):
        fills = [F(1, 2, 10, 7, "BUY", 5, 100), F(2, 2, 20, 7, "SELL", 8, 90), F(3, 2, 30, 7, "BUY", 3, 80)]
        chunks, _ = metrics.fifo_trades(fills, None, PRODUCTS)
        self.assertEqual([(c.qty, c.open_side, c.pnl, c.close_fill_id, c.close_order_id) for c in chunks],
                         [(5, "BUY", -50, 2, 2), (3, "SELL", 30, 3, 2)])

    def test_settlement_close_has_no_fill_id(self):
        chunks, _ = metrics.fifo_trades([F(1, 1, 10, 7, "SELL", 2, 100)], [(99, 7, "MIS", "BUY", 2, 95)], PRODUCTS)
        self.assertEqual(len(chunks), 1)
        c = chunks[0]
        self.assertEqual((c.close_fill_id, c.close_order_id, c.close_t_ns, c.open_side, c.pnl),
                         (None, None, 99, "SELL", 10))

    def test_fifo_order_across_two_opens(self):
        fills = [F(1, 1, 10, 7, "BUY", 2, 100), F(2, 1, 11, 7, "BUY", 3, 120), F(3, 1, 20, 7, "SELL", 4, 130)]
        chunks, _ = metrics.fifo_trades(fills, None, PRODUCTS)
        self.assertEqual([(c.qty, c.open_price, c.pnl) for c in chunks], [(2, 100, 60), (2, 120, 20)])

    def test_traded_value_ignores_settlements(self):
        fills = [F(1, 1, 10, 7, "BUY", 2, 100), F(2, 1, 20, 7, "SELL", 1, 110)]
        _, tv = metrics.fifo_trades(fills, [(99, 7, "MIS", "SELL", 1, 500)], PRODUCTS)
        self.assertEqual(tv, 2 * 100 + 1 * 110)

    def test_settlement_sorts_after_a_fill_at_the_same_time(self):
        fills = [F(1, 1, 10, 7, "BUY", 1, 100), F(2, 1, 50, 7, "SELL", 1, 101)]
        chunks, _ = metrics.fifo_trades(fills, [(50, 7, "MIS", "SELL", 1, 999)], PRODUCTS)
        self.assertEqual([(c.close_fill_id, c.pnl) for c in chunks], [(2, 1)])


class FifoPropertyTests(unittest.TestCase):
    def test_chunks_sum_to_metrics_total_pnl(self):
        """Per sequence, a final flattening settlement leaves no open lot, so the chunks' P&L must equal the plain
        cash flow (sells minus buys): an oracle that never matches lots. Seeded; flips, partial exits and
        settlements all occur."""
        saw = {"flip": 0, "partial": 0, "settle": 0, "chunks": 0}
        for seed in range(1000):
            rng = random.Random(seed)
            fills, closes, cash, net = [], [], 0, {}
            t = 0
            for n in range(rng.randint(1, 25)):
                t += rng.choice([0, 0, 1, 5])
                iid = rng.choice([1, 2, 3])
                oid = rng.choice([1, 2])
                side = rng.choice(["BUY", "SELL"])
                qty, price = rng.randint(1, 6), rng.randint(90, 110)
                if rng.random() < 0.15:
                    closes.append((t, iid, PRODUCTS[oid], side, qty, price))
                else:
                    fills.append(F(n + 1, oid, t, iid, side, qty, price))
                s = 1 if side == "BUY" else -1
                cash -= s * qty * price
                net[(iid, PRODUCTS[oid])] = net.get((iid, PRODUCTS[oid]), 0) + s * qty
            for (iid, prod), n_ in sorted(net.items()):
                if n_:
                    p = rng.randint(90, 110)
                    closes.append((t + 1, iid, prod, "SELL" if n_ > 0 else "BUY", abs(n_), p))
                    cash += n_ * p
            chunks, tv = metrics.fifo_trades(fills, closes, PRODUCTS)
            self.assertEqual(sum(c.pnl for c in chunks), cash, seed)
            self.assertTrue(all(c.qty > 0 for c in chunks), seed)
            self.assertEqual(tv, sum(f.qty * f.price for f in fills), seed)
            self.assertTrue(all(c.close_fill_id is None for c in chunks if c.close_order_id is None), seed)
            saw["chunks"] += len(chunks)
            saw["settle"] += any(c.close_fill_id is None for c in chunks)
            saw["partial"] += any(c.qty < f.qty for c in chunks for f in fills if f.fill_id == c.close_fill_id)
            saw["flip"] += any(c.close_fill_id is not None and
                               sum(k.qty for k in chunks if k.close_fill_id == c.close_fill_id) <
                               next(f.qty for f in fills if f.fill_id == c.close_fill_id) for c in chunks)
        self.assertTrue(all(saw.values()), saw)       # the generator really reaches each case


if __name__ == "__main__":
    unittest.main()

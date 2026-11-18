"""HD 7.1: option ideas for an F&O holding, priced from the live chain."""
from __future__ import annotations

import unittest

import tests.support as support  # noqa: F401  — must precede project imports

from stockify.desk import ideas as I
from stockify.desk.thesis import Verdict

SPOT = 153_000                     # ₹1,530.00
LOT = 400
NEAR, NEXT = "2026-11-24", "2026-12-29"
STRIKES = [140_000, 145_000, 150_000, 155_000, 160_000, 165_000]
# (bid, ask) per share in paise, per (strike, kind), for both expiries.
QUOTES = {
    (140_000, "PE"): (400, 420), (145_000, "PE"): (900, 940), (150_000, "PE"): (2_000, 2_050),
    (155_000, "PE"): (3_800, 3_900), (160_000, "PE"): (7_000, 7_200), (165_000, "PE"): (12_000, 12_400),
    (140_000, "CE"): (13_500, 13_900), (145_000, "CE"): (9_500, 9_800), (150_000, "CE"): (5_600, 5_800),
    (155_000, "CE"): (3_000, 3_100), (160_000, "CE"): (1_300, 1_350), (165_000, "CE"): (500, 520),
}
GAUGE = {"expiry": NEAR, "pcr_oi": 0.9, "call_walls": [160_000, 165_000], "put_walls": [145_000, 140_000],
         "max_pain_paise": 150_000, "atm_iv": 0.25, "iv_percentile": 50.0, "sentences": []}


def snap(trade_date="2026-11-17", drop=()):  # noqa: ANN001
    rows = []
    for exp, tag in ((NEAR, "26NOV"), (NEXT, "26DEC")):
        for k in STRIKES:
            for kind in ("CE", "PE"):
                bid, ask = QUOTES[(k, kind)]
                if (exp, k, kind) in drop:
                    bid = ask = None
                rows.append({"instrument_id": len(rows) + 1, "tradingsymbol": f"INFY{tag}{k // 100}{kind}",
                             "kind": kind, "strike_paise": k, "expiry": exp, "bid_paise": bid, "ask_paise": ask,
                             "ltp_paise": ask, "oi": 1000, "iv": 0.25, "delta": 0.5})
    return {"underlying": "INFY", "trade_date": trade_date, "spot_paise": SPOT, "lot_size": LOT,
            "expiries": [NEAR, NEXT], "rows": rows}


def verdict(label="Hold", share=0.2, nearest=None):  # noqa: ANN001
    return Verdict(label, share, nearest, [], [], [], "")


def by_name(out):  # noqa: ANN001
    return {i.name: i for i in out}


class FakeMargins:
    def __init__(self, margin_rupees=50_000.0, cash_rupees=100_000.0):
        self.margin, self.cash, self.calls = margin_rupees, cash_rupees, []

    def basket_order_margins(self, orders, consider_positions=False):  # noqa: ANN001
        self.calls.append((orders, consider_positions))
        return {"final": {"span": self.margin, "exposure": 0, "additional": 0, "option_premium": 0}}

    def margins(self):  # noqa: ANN201
        return {"equity": {"net": self.cash, "available": {"cash": self.cash}}}


class IdeasTests(unittest.TestCase):
    def test_covered_call_needs_a_full_lot(self):
        few = by_name(I.ideas(verdict(), snap(), GAUGE, SPOT, LOT, 399))
        for name in ("Covered call", "Protective put", "Collar"):
            self.assertFalse(few[name].possible)
            self.assertEqual(few[name].not_possible_reason, "You hold fewer shares than one lot (400)")
        two = by_name(I.ideas(verdict(), snap(), GAUGE, SPOT, LOT, 850))
        cc = two["Covered call"]
        self.assertTrue(cc.possible)
        self.assertEqual(cc.legs, [{"side": "SELL", "tradingsymbol": "INFY26NOV1600CE", "lots": 2,
                                    "price_paise": 1_300}])          # nearest call wall, at the bid
        self.assertEqual(cc.premium_paise, -1_300 * 800)
        self.assertEqual(cc.max_gain_paise, (160_000 - SPOT + 1_300) * 800)
        self.assertEqual(cc.max_loss_paise, (SPOT - 1_300) * 800)
        self.assertEqual(cc.breakeven_paise, [SPOT - 1_300])

    def test_protective_put_and_collar_strikes(self):
        out = by_name(I.ideas(verdict(nearest=146_000), snap(), GAUGE, SPOT, LOT, 400))
        pp = out["Protective put"]
        self.assertEqual(pp.legs, [{"side": "BUY", "tradingsymbol": "INFY26NOV1450PE", "lots": 1,
                                    "price_paise": 940}])            # strike nearest the exit level, at the ask
        self.assertEqual(pp.premium_paise, 940 * 400)
        self.assertEqual(pp.max_loss_paise, (SPOT - 145_000 + 940) * 400)
        self.assertIsNone(pp.max_gain_paise)
        self.assertEqual(pp.breakeven_paise, [SPOT + 940])
        col = out["Collar"]
        self.assertEqual([(l["side"], l["tradingsymbol"]) for l in col.legs],
                         [("BUY", "INFY26NOV1450PE"), ("SELL", "INFY26NOV1600CE")])
        net = 940 - 1_300
        self.assertEqual(col.premium_paise, net * 400)
        self.assertEqual(col.max_loss_paise, (SPOT - 145_000 + net) * 400)
        self.assertEqual(col.max_gain_paise, (160_000 - SPOT - net) * 400)
        # without an exit level: one strike below the one nearest the price
        no_level = by_name(I.ideas(verdict(), snap(), GAUGE, SPOT, LOT, 400))
        self.assertEqual(no_level["Protective put"].legs[0]["tradingsymbol"], "INFY26NOV1500PE")

    def test_exit_verdict_offers_put_ideas_and_hold_offers_call_ideas(self):
        for label in ("Exit", "Trim"):
            names = [i.name for i in I.ideas(verdict(label, 0.7), snap(), GAUGE, SPOT, LOT, 400)]
            self.assertEqual(names, ["Protective put", "Collar", "Covered call", "Bear put spread", "Long put"])
        names = [i.name for i in I.ideas(verdict("Hold", 0.2), snap(), GAUGE, SPOT, LOT, 400)]
        self.assertEqual(names, ["Protective put", "Collar", "Covered call", "Bull call spread", "Long call"])
        for v in (verdict("Tighten stop", 0.3), verdict("No reliable signal", 0.0)):
            names = [i.name for i in I.ideas(v, snap(), GAUGE, SPOT, LOT, 400)]
            self.assertEqual(names, ["Protective put", "Collar", "Covered call"])

    def test_spread_max_loss_and_gain(self):
        up = by_name(I.ideas(verdict("Hold", 0.1), snap(), GAUGE, SPOT, LOT, 0))
        bull = up["Bull call spread"]
        self.assertEqual([(l["side"], l["tradingsymbol"], l["lots"], l["price_paise"]) for l in bull.legs],
                         [("BUY", "INFY26NOV1550CE", 1, 3_100), ("SELL", "INFY26NOV1600CE", 1, 1_300)])
        debit = 3_100 - 1_300
        self.assertEqual(bull.premium_paise, debit * LOT)
        self.assertEqual(bull.max_loss_paise, debit * LOT)
        self.assertEqual(bull.max_gain_paise, (160_000 - 155_000 - debit) * LOT)
        self.assertEqual(bull.breakeven_paise, [155_000 + debit])
        self.assertTrue(bull.possible)                       # directional ideas need no shares
        call = up["Long call"]
        self.assertEqual(call.max_loss_paise, 3_100 * LOT)
        self.assertIsNone(call.max_gain_paise)
        self.assertEqual(call.breakeven_paise, [155_000 + 3_100])

        down = by_name(I.ideas(verdict("Exit", 0.8), snap(), GAUGE, SPOT, LOT, 0))
        bear = down["Bear put spread"]
        self.assertEqual([(l["side"], l["tradingsymbol"]) for l in bear.legs],
                         [("BUY", "INFY26NOV1550PE"), ("SELL", "INFY26NOV1450PE")])   # ATM, wall below
        debit = 3_900 - 900
        self.assertEqual(bear.max_loss_paise, debit * LOT)
        self.assertEqual(bear.max_gain_paise, (155_000 - 145_000 - debit) * LOT)
        self.assertEqual(bear.breakeven_paise, [155_000 - debit])
        put = down["Long put"]
        self.assertEqual(put.max_loss_paise, 3_900 * LOT)
        self.assertEqual(put.max_gain_paise, (155_000 - 3_900) * LOT)

    def test_missing_quote_makes_the_idea_not_possible(self):
        out = by_name(I.ideas(verdict("Hold", 0.1), snap(drop={(NEAR, 160_000, "CE")}), GAUGE, SPOT, LOT, 400))
        for name in ("Collar", "Covered call", "Bull call spread"):
            self.assertFalse(out[name].possible, name)
            self.assertEqual(out[name].not_possible_reason, "No live quotes for ₹1,600.00 CE")
        self.assertTrue(out["Long call"].possible)
        self.assertTrue(out["Protective put"].possible)

    def test_near_expiry_too_close_uses_the_next(self):
        out = by_name(I.ideas(verdict(), snap(trade_date="2026-11-20"), GAUGE, SPOT, LOT, 400))
        self.assertEqual(out["Protective put"].legs[0]["tradingsymbol"], "INFY26DEC1500PE")
        self.assertIn("next expiry", out["Protective put"].why)
        near = by_name(I.ideas(verdict(), snap(), GAUGE, SPOT, LOT, 400))
        self.assertNotIn("next expiry", near["Protective put"].why)

    def test_margin_above_cash_is_flagged(self):
        none = by_name(I.ideas(verdict(), snap(), GAUGE, SPOT, LOT, 400))
        self.assertIsNone(none["Collar"].margin_paise)
        self.assertIsNone(none["Collar"].margin_ok)
        rich = FakeMargins(margin_rupees=50_000.0, cash_rupees=100_000.0)
        out = by_name(I.ideas(verdict(), snap(), GAUGE, SPOT, LOT, 400, margin_client=rich))
        self.assertEqual(out["Collar"].margin_paise, 5_000_000)
        self.assertTrue(out["Collar"].margin_ok)
        orders, consider = rich.calls[0]
        self.assertFalse(consider)
        self.assertEqual(orders[0]["exchange"], "NFO")
        self.assertEqual(orders[0]["quantity"], 400)
        poor = FakeMargins(margin_rupees=150_000.0, cash_rupees=100_000.0)
        out = by_name(I.ideas(verdict(), snap(), GAUGE, SPOT, LOT, 400, margin_client=poor))
        self.assertEqual(out["Collar"].margin_paise, 15_000_000)
        self.assertFalse(out["Collar"].margin_ok)

    def test_margin_errors_leave_none(self):
        class Broken(FakeMargins):
            def basket_order_margins(self, orders, consider_positions=False):  # noqa: ANN001
                raise RuntimeError("down")
        out = by_name(I.ideas(verdict(), snap(), GAUGE, SPOT, LOT, 400, margin_client=Broken()))
        self.assertIsNone(out["Collar"].margin_paise)
        self.assertIsNone(out["Collar"].margin_ok)

    def test_put_strike_never_above_the_price(self):
        # exit level ₹1,528 rounds to ₹1,550 by nearness, but a put above the price is not a floor
        out = by_name(I.ideas(verdict(nearest=152_800), snap(), GAUGE, SPOT, LOT, 400))
        self.assertEqual(out["Protective put"].legs[0]["tradingsymbol"], "INFY26NOV1500PE")
        self.assertEqual([l["tradingsymbol"] for l in out["Collar"].legs], ["INFY26NOV1500PE", "INFY26NOV1600CE"])
        self.assertTrue(out["Collar"].possible)
        self.assertGreater(out["Collar"].max_gain_paise, 0)
        # exit level above the price (cannot come from decide, but stay safe): capped at the price
        out = by_name(I.ideas(verdict(nearest=158_000), snap(), GAUGE, SPOT, LOT, 400))
        self.assertEqual(out["Protective put"].legs[0]["tradingsymbol"], "INFY26NOV1500PE")

    def test_collar_with_no_room_for_a_gain_is_not_possible(self):
        spot = 159_900                       # call wall ₹1,600 sits ₹1 above; the put costs more than the call brings
        out = by_name(I.ideas(verdict(), snap(), GAUGE, spot, LOT, 400))
        col = out["Collar"]
        self.assertFalse(col.possible)
        self.assertEqual(col.not_possible_reason,
                         "The call's strike is too close to the price for this to pay at these prices")
        self.assertIsNone(col.margin_paise)
        self.assertTrue(out["Covered call"].possible)

    def test_cost_against_typical_move_caution(self):
        small = by_name(I.ideas(verdict("Hold", 0.1), snap(), GAUGE, SPOT, LOT, 400, atr_paise=500.0))
        # 5 trading days left: typical move ≈ ₹5 × √5 ≈ ₹11.18; long call breakeven is ₹51 away
        self.assertIn("typical move", small["Long call"].caution)
        self.assertIn("₹11.18", small["Long call"].caution)
        self.assertNotIn("typical move", small["Covered call"].caution)      # nothing bought
        big = by_name(I.ideas(verdict("Hold", 0.1), snap(), GAUGE, SPOT, LOT, 400, atr_paise=10_000.0))
        self.assertNotIn("typical move", big["Long call"].caution)
        none = by_name(I.ideas(verdict("Hold", 0.1), snap(), GAUGE, SPOT, LOT, 400))
        self.assertNotIn("typical move", none["Long call"].caution)

    def test_two_leg_basket_and_one_cash_read(self):
        fake = FakeMargins()
        fake.cash_reads = 0
        orig = fake.margins

        def counted():  # noqa: ANN202
            fake.cash_reads += 1
            return orig()
        fake.margins = counted
        I.ideas(verdict("Hold", 0.1), snap(), GAUGE, SPOT, LOT, 400, margin_client=fake)
        collar_orders = fake.calls[1][0]
        self.assertEqual([(o["tradingsymbol"], o["transaction_type"], o["quantity"]) for o in collar_orders],
                         [("INFY26NOV1500PE", "BUY", 400), ("INFY26NOV1600CE", "SELL", 400)])
        self.assertEqual(fake.cash_reads, 1)

    def test_high_iv_caution_on_bought_options(self):
        high = dict(GAUGE, iv_percentile=85.0)
        out = by_name(I.ideas(verdict("Hold", 0.1), snap(), high, SPOT, LOT, 400))
        self.assertIn("priced high", out["Long call"].caution)
        self.assertIn("priced high", out["Protective put"].caution)
        self.assertNotIn("priced high", out["Covered call"].caution)
        low = dict(GAUGE, iv_percentile=20.0)
        out = by_name(I.ideas(verdict("Hold", 0.1), snap(), low, SPOT, LOT, 400))
        self.assertIn("priced low", out["Covered call"].caution)
        self.assertNotIn("priced low", out["Long call"].caution)
        mid = by_name(I.ideas(verdict("Hold", 0.1), snap(), GAUGE, SPOT, LOT, 400))
        self.assertNotIn("priced", mid["Long call"].caution)

    def test_plain_copy(self):
        out = I.ideas(verdict("Exit", 0.8), snap(), dict(GAUGE, iv_percentile=90.0), SPOT, LOT, 100)
        out += I.ideas(verdict("Hold", 0.1), snap(trade_date="2026-11-20"), GAUGE, SPOT, LOT, 400)
        for i in out:
            for text in (i.name, i.why, i.caution, i.not_possible_reason):
                self.assertNotIn("—", text)
                for word in ("cost basis", "average", "profit", "P&L"):
                    self.assertNotIn(word, text)
            self.assertTrue(i.why)
            self.assertTrue(i.caution)


if __name__ == "__main__":
    unittest.main()

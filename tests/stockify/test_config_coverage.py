"""Every shipped config loads, and charges resolve to exactly one row (A4 §5.2).

For each order profile and sample date, every charge component that applies
must match exactly one dated row. The one known hole is recorded, not hidden
(decision D-33): NSE's F&O transaction charge has no sourced row before
2024-10-01, so an F&O run on an earlier date must fail with "no row" rather
than borrow a later rate.
"""
from __future__ import annotations

import unittest

import tests.support  # noqa: F401  — must precede project imports

from stockify.config import loader

# segment, product, kind, side
PROFILES = {
    "CM CNC buy": ("CM", "CNC", "EQ", "BUY"),
    "CM CNC sell": ("CM", "CNC", "EQ", "SELL"),
    "CM MIS sell": ("CM", "MIS", "EQ", "SELL"),
    "FO FUT sell": ("FO", "NRML", "FUT", "SELL"),
    "FO CE buy": ("FO", "NRML", "CE", "BUY"),
}
# Components charged on every order (others apply only to exercise, delivery or square-off).
ORDER_COMPONENTS = ("brokerage", "stt", "exchange_txn", "sebi", "stamp", "gst")
SAMPLE_DATES = ("2024-10-15", "2025-06-02", "2026-05-04", "2026-10-30")


def applies(rule: dict, segment, product, kind, side) -> bool:
    for key, want in rule.items():
        have = {"segment": segment, "product": product, "kind": kind, "side": side}[key]
        if isinstance(want, list):
            if have not in want:
                return False
        elif have != want:
            return False
    return True


def matching(rows, day, profile) -> list[dict]:
    """Rows in force on `day` for this order: per applies-key, the latest effective_from <= day."""
    by_key: dict[str, dict] = {}
    for r in rows:
        if r["effective_from"] <= day and applies(r.get("applies", {}), *profile):
            key = repr(sorted(r.get("applies", {}).items()))
            if key not in by_key or r["effective_from"] > by_key[key]["effective_from"]:
                by_key[key] = r
    return list(by_key.values())


class ConfigCoverageTests(unittest.TestCase):
    def setUp(self):
        self.charges = loader.load_json("charges.json")["components"]

    def test_all_configs_load(self):
        docs = loader.load_all()
        self.assertGreater(len(docs["charges.json"]["components"]["stt"]), 0)
        self.assertGreater(len(docs["sessions.json"]["versions"]), 0)

    def test_watchlist_config_values(self):
        """CW 2: every key the later phases read is in the shipped watchlist.json."""
        doc = loader.load_json("watchlist.json")
        self.assertEqual(doc["max_stocks"], 50)
        self.assertEqual((doc["lookback_days"], doc["min_history_days"]), (365, 365))
        self.assertEqual(doc["basis_tolerance"], "0.02")
        self.assertEqual((doc["alert_batch_s"], doc["max_alert_emails_per_day"]), (60, 6))
        self.assertEqual((doc["results_lead_trading_days"], doc["tap_frames"]), (2, 64))
        self.assertEqual(doc["desk_watch_new_grades_per_night"], 10)

    def test_each_component_resolves_to_exactly_one_row(self):
        for name, profile in PROFILES.items():
            for day in SAMPLE_DATES:
                for comp in ORDER_COMPONENTS:
                    with self.subTest(order=name, day=day, component=comp):
                        rows = matching(self.charges[comp], day, profile)
                        if comp in ("stt", "stamp") and not rows:
                            continue          # e.g. no stamp duty on a sell; no STT on an F&O buy
                        self.assertEqual(len(rows), 1, f"{len(rows)} rows: {rows}")

    def test_fo_exchange_charge_before_2024_10_01_is_a_known_hole_d33(self):
        rows = matching(self.charges["exchange_txn"], "2024-01-15", PROFILES["FO FUT sell"])
        self.assertEqual(rows, [])

    def test_rates_are_exact_ppb(self):
        stt = matching(self.charges["stt"], "2026-05-04", PROFILES["FO CE buy"])
        self.assertEqual(stt[0]["sell_ppb"], 1_500_000)      # 0.15% on the sell side

    def test_unverified_rows_are_marked(self):
        exch = matching(self.charges["exchange_txn"], "2026-10-30", PROFILES["FO FUT sell"])
        self.assertFalse(exch[0]["verified"])
        self.assertTrue(exch[0].get("effective_from_resolved"))


class CandlesConfigTests(unittest.TestCase):
    def test_the_shipped_candle_settings(self):
        """CW 1 (spec "Shared config"): six years of daily candles at 2 requests a second, backing off 1, 2 and 4 s
        on a 429; below Kite's historical limit (K-8, kite_limits.json historical_rps)."""
        from decimal import Decimal
        cfg = loader.load_json("candles.json")
        self.assertEqual((cfg["history_years"], cfg["rps"], cfg["rate_limit_backoff_s"]), (6, "2", [1, 2, 4]))
        self.assertTrue(cfg["note"])
        self.assertLessEqual(Decimal(cfg["rps"]), Decimal(str(loader.load_json("kite_limits.json")["historical_rps"])))

if __name__ == "__main__":
    unittest.main()

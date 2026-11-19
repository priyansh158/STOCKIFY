"""Unit tests for portfolio_sync: holding arithmetic, snapshot aggregates,
fetching and caching.

No network: kite_client is never authenticated in the sandbox, and the cache is
driven directly.
"""
from __future__ import annotations

import time
import unittest
from unittest.mock import patch

import tests.support  # noqa: F401  — must precede project imports

import portfolio_sync
from portfolio_sync import Holding, PortfolioSnapshot


# kiteconnect's typed error, by class name and module (the SDK itself is absent in tests)
SDK_TOKEN_ERROR = type("TokenException", (Exception,), {"__module__": "kiteconnect.exceptions"})


def holding(**kw) -> Holding:
    base = dict(
        tradingsymbol="RELIANCE",
        exchange="NSE",
        quantity=100,
        average_price=2800.0,
        last_price=2900.0,
        pnl=10_000.0,
        day_change_pct=1.2,
        instrument_token=738561,
        isin="INE002A01018",
    )
    base.update(kw)
    return Holding(**base)


class PortfolioSyncTest(unittest.TestCase):
    def setUp(self):
        portfolio_sync.invalidate_cache()
        self.addCleanup(portfolio_sync.invalidate_cache)


# ── Holding arithmetic ───────────────────────────────────────────────────────

class HoldingMathTests(PortfolioSyncTest):
    def test_invested_is_quantity_times_average(self):
        self.assertEqual(holding(quantity=100, average_price=2800.0).invested, 280_000.0)

    def test_current_value_is_quantity_times_last(self):
        self.assertEqual(holding(quantity=100, last_price=2900.0).current_value, 290_000.0)

    def test_pnl_pct_is_relative_to_cost_basis(self):
        h = holding(quantity=100, average_price=2800.0, pnl=28_000.0)
        self.assertAlmostEqual(h.pnl_pct, 10.0)

    def test_negative_pnl_pct(self):
        h = holding(quantity=100, average_price=2800.0, pnl=-28_000.0)
        self.assertAlmostEqual(h.pnl_pct, -10.0)

    def test_zero_quantity_does_not_divide_by_zero(self):
        self.assertEqual(holding(quantity=0).pnl_pct, 0.0)
        self.assertEqual(holding(quantity=0).invested, 0.0)

    def test_zero_average_price_does_not_divide_by_zero(self):
        self.assertEqual(holding(average_price=0.0).pnl_pct, 0.0)

    def test_fractional_quantities_are_handled(self):
        h = holding(quantity=3, average_price=1000.5)
        self.assertAlmostEqual(h.invested, 3001.5)

    def test_to_dict_adds_the_derived_fields(self):
        d = holding().to_dict()
        for key in ("invested", "current_value", "pnl_pct"):
            self.assertIn(key, d)
        self.assertEqual(d["tradingsymbol"], "RELIANCE")
        self.assertEqual(d["invested"], 280_000.0)

    def test_to_dict_is_json_shaped(self):
        # Consumed by AnalysisState.position_context, which is JSON-serialised
        # into the history store — so every value must be a primitive.
        for value in holding().to_dict().values():
            self.assertIsInstance(value, (str, int, float, bool))


class SnapshotAggregateTests(PortfolioSyncTest):
    def test_totals_sum_across_holdings(self):
        snap = PortfolioSnapshot(holdings=[
            holding(quantity=100, average_price=100.0, last_price=110.0, pnl=1000.0),
            holding(quantity=50, average_price=200.0, last_price=190.0, pnl=-500.0),
        ])
        self.assertEqual(snap.total_invested, 100 * 100.0 + 50 * 200.0)
        self.assertEqual(snap.total_current, 100 * 110.0 + 50 * 190.0)
        self.assertEqual(snap.total_pnl, 500.0)

    def test_total_pnl_pct(self):
        snap = PortfolioSnapshot(holdings=[
            holding(quantity=100, average_price=100.0, pnl=1000.0),
        ])
        self.assertAlmostEqual(snap.total_pnl_pct, 10.0)

    def test_empty_snapshot_totals_are_zero_not_errors(self):
        snap = PortfolioSnapshot()
        self.assertEqual(snap.total_invested, 0)
        self.assertEqual(snap.total_current, 0)
        self.assertEqual(snap.total_pnl, 0)
        self.assertEqual(snap.total_pnl_pct, 0.0)

    def test_default_collections_are_not_shared_between_instances(self):
        a, b = PortfolioSnapshot(), PortfolioSnapshot()
        a.holdings.append(holding())
        self.assertEqual(b.holdings, [])


# ── Snapshot fetching / caching ──────────────────────────────────────────────

class FetchSnapshotTests(PortfolioSyncTest):
    def test_unauthenticated_returns_an_errored_but_valid_snapshot(self):
        with patch.object(portfolio_sync.kite_client, "is_authenticated", return_value=False):
            snap = portfolio_sync.fetch_snapshot()
        self.assertIsInstance(snap, PortfolioSnapshot)
        self.assertEqual(snap.error, "Kite not authenticated")
        self.assertEqual(snap.holdings, [])

    def test_maps_raw_kite_rows_into_holdings(self):
        raw = [{
            "tradingsymbol": "reliance", "exchange": "nse", "quantity": 10,
            "average_price": 2800.0, "last_price": 2900.0, "pnl": 1000.0,
            "day_change_percentage": 1.5, "instrument_token": 738561,
            "isin": "INE002A01018",
        }]
        with patch.object(portfolio_sync.kite_client, "is_authenticated", return_value=True), \
             patch.object(portfolio_sync.kite_client, "holdings", return_value=raw), \
             patch.object(portfolio_sync.kite_client, "positions", return_value={"net": []}):
            snap = portfolio_sync.fetch_snapshot()
        self.assertEqual(len(snap.holdings), 1)
        h = snap.holdings[0]
        self.assertEqual(h.tradingsymbol, "RELIANCE")   # upper-cased
        self.assertEqual(h.exchange, "NSE")
        self.assertIsNone(snap.error)

    def test_missing_fields_default_rather_than_crash(self):
        with patch.object(portfolio_sync.kite_client, "is_authenticated", return_value=True), \
             patch.object(portfolio_sync.kite_client, "holdings", return_value=[{}]), \
             patch.object(portfolio_sync.kite_client, "positions", return_value={"net": []}):
            snap = portfolio_sync.fetch_snapshot()
        self.assertEqual(len(snap.holdings), 1)
        self.assertEqual(snap.holdings[0].quantity, 0)
        self.assertEqual(snap.holdings[0].tradingsymbol, "")

    def test_one_malformed_row_does_not_drop_the_others(self):
        raw = [
            {"tradingsymbol": "GOOD", "quantity": 1, "average_price": 1.0,
             "last_price": 1.0, "pnl": 0.0, "instrument_token": 1},
            {"tradingsymbol": "BAD", "quantity": "not-a-number"},
        ]
        with patch.object(portfolio_sync.kite_client, "is_authenticated", return_value=True), \
             patch.object(portfolio_sync.kite_client, "holdings", return_value=raw), \
             patch.object(portfolio_sync.kite_client, "positions", return_value={"net": []}):
            snap = portfolio_sync.fetch_snapshot()
        self.assertEqual([h.tradingsymbol for h in snap.holdings], ["GOOD"])

    def test_positions_failure_leaves_holdings_intact(self):
        with patch.object(portfolio_sync.kite_client, "is_authenticated", return_value=True), \
             patch.object(portfolio_sync.kite_client, "holdings", return_value=[]), \
             patch.object(portfolio_sync.kite_client, "positions",
                          side_effect=RuntimeError("positions 500")):
            snap = portfolio_sync.fetch_snapshot()
        self.assertEqual(snap.positions_net, [])
        self.assertIsNone(snap.error)

    def test_holdings_failure_is_recorded_on_the_snapshot(self):
        with patch.object(portfolio_sync.kite_client, "is_authenticated", return_value=True), \
             patch.object(portfolio_sync.kite_client, "holdings",
                          side_effect=RuntimeError("holdings 500")):
            snap = portfolio_sync.fetch_snapshot()
        self.assertEqual(snap.error, portfolio_sync.UNAVAILABLE)      # a plain sentence, not the raw error text
        self.assertFalse(snap.expired)
        self.assertEqual(snap.holdings, [])

    def test_holdings_are_asked_for_strictly(self):
        """The non-strict wrapper turns a network failure into [], which would read as "no holdings"."""
        with patch.object(portfolio_sync.kite_client, "is_authenticated", return_value=True), \
             patch.object(portfolio_sync.kite_client, "holdings", return_value=[]) as h, \
             patch.object(portfolio_sync.kite_client, "positions", return_value={"net": []}):
            portfolio_sync.fetch_snapshot()
        h.assert_called_once_with(strict=True)


class RejectedLoginTests(PortfolioSyncTest):
    """2026-11-18: Kite refused the stored token and the page showed "No holdings." instead of "log in again"."""

    def fetch_raising(self, exc):  # noqa: ANN001
        with patch.object(portfolio_sync.kite_client, "is_authenticated", return_value=True), \
             patch.object(portfolio_sync.kite_client, "holdings", side_effect=exc), \
             patch.object(portfolio_sync.kite_client, "positions", return_value={"net": []}):
            return portfolio_sync.fetch_snapshot()

    def test_an_sdk_token_exception_is_an_expired_login_not_empty_holdings(self):
        snap = self.fetch_raising(SDK_TOKEN_ERROR("Incorrect `api_key` or `access_token`."))
        self.assertTrue(snap.expired)
        self.assertEqual(snap.error, portfolio_sync.EXPIRED)
        self.assertIn("Log in again", snap.error)
        self.assertEqual(snap.holdings, [])

    def test_the_clients_own_auth_errors_are_an_expired_login(self):
        for exc in (portfolio_sync.kite_client.KiteTokenRejected("Kite refused the stored login"),
                    portfolio_sync.kite_client.KiteTokenExpired("past 06:00")):
            with self.subTest(type(exc).__name__):
                snap = self.fetch_raising(exc)
                self.assertTrue(snap.expired)
                self.assertEqual(snap.error, portfolio_sync.EXPIRED)

    def test_a_403_is_an_expired_login(self):
        exc = RuntimeError("Forbidden")
        exc.code = 403
        self.assertTrue(self.fetch_raising(exc).expired)

    def test_the_error_never_carries_the_raw_text(self):
        snap = self.fetch_raising(SDK_TOKEN_ERROR("access_token=SECRET123tok rejected"))
        self.assertNotIn("SECRET123tok", snap.error)

    def test_a_new_login_does_not_reuse_the_rejected_snapshot(self):
        with patch.object(portfolio_sync.kite_client, "token_fingerprint", return_value="old"):
            self.fetch_raising(SDK_TOKEN_ERROR("Incorrect `api_key` or `access_token`."))
        fresh = PortfolioSnapshot(fetched_at=int(time.time()))
        with patch.object(portfolio_sync.kite_client, "token_fingerprint", return_value="new"), \
             patch.object(portfolio_sync, "fetch_snapshot", return_value=fresh) as m:
            self.assertIs(portfolio_sync.cached_snapshot(60), fresh)
        m.assert_called_once()

    def test_the_same_login_reuses_its_cached_snapshot(self):
        with patch.object(portfolio_sync.kite_client, "token_fingerprint", return_value="same"):
            first = self.fetch_raising(SDK_TOKEN_ERROR("Incorrect `api_key` or `access_token`."))
            with patch.object(portfolio_sync, "fetch_snapshot") as m:
                self.assertIs(portfolio_sync.cached_snapshot(60), first)
            m.assert_not_called()


class CacheTests(PortfolioSyncTest):
    def test_fresh_cache_is_reused(self):
        cached = PortfolioSnapshot(fetched_at=int(time.time()))
        portfolio_sync._cache = cached
        with patch.object(portfolio_sync, "fetch_snapshot") as m:
            got = portfolio_sync.cached_snapshot(max_age_seconds=60)
        self.assertIs(got, cached)
        m.assert_not_called()

    def test_stale_cache_triggers_a_refetch(self):
        portfolio_sync._cache = PortfolioSnapshot(fetched_at=int(time.time()) - 3600)
        fresh = PortfolioSnapshot(fetched_at=int(time.time()))
        with patch.object(portfolio_sync, "fetch_snapshot", return_value=fresh) as m:
            got = portfolio_sync.cached_snapshot(max_age_seconds=60)
        self.assertIs(got, fresh)
        m.assert_called_once()

    def test_cold_cache_fetches(self):
        portfolio_sync._cache = None
        with patch.object(portfolio_sync, "fetch_snapshot",
                          return_value=PortfolioSnapshot()) as m:
            portfolio_sync.cached_snapshot()
        m.assert_called_once()

    def test_invalidate_forces_refetch(self):
        portfolio_sync._cache = PortfolioSnapshot(fetched_at=int(time.time()))
        portfolio_sync.invalidate_cache()
        self.assertIsNone(portfolio_sync._cache)

    def test_cached_snapshot_never_returns_none(self):
        with patch.object(portfolio_sync.kite_client, "is_authenticated", return_value=False):
            self.assertIsInstance(portfolio_sync.cached_snapshot(), PortfolioSnapshot)


if __name__ == "__main__":
    unittest.main()

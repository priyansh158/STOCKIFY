"""Unit tests for portfolio_sync and server.services._position.

The headline test class here is `AnonymisationRuleTests`. The module docstring
calls the anonymisation rule "load-bearing": if position text reaches the LLM
with owner attribution, the analysis is biased toward confirming the user's
existing trade. That rule is invisible to type checkers and easy to break with
an innocent copy-edit, so it gets explicit character-level coverage.

No network: kite_client is never authenticated in the sandbox, and the cache is
driven directly.
"""
from __future__ import annotations

import re
import time
import unittest
from unittest.mock import patch

import tests.support  # noqa: F401  — must precede project imports

import portfolio_sync
from portfolio_sync import Holding, PortfolioSnapshot
from server.services import _position


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


# ── The anonymisation rule ───────────────────────────────────────────────────

# Words that must never appear in text destined for a prompt. Matched with
# word boundaries so "IndiaMART" doesn't trip on "I" and "Reliance" doesn't
# trip on "I" either.
_OWNER_WORDS = re.compile(
    r"\b(you|your|yours|yourself|i|me|my|mine|we|us|our|ours|user|client|customer|holder|owner)\b",
    re.IGNORECASE,
)


class AnonymisationRuleTests(PortfolioSyncTest):
    """Guards the rule in portfolio_sync's module docstring.

    Any prompt-bound position text must read as neutral market context.
    """

    def test_blurb_contains_no_owner_attributing_pronouns(self):
        blurb = portfolio_sync.anonymous_position_blurb(holding())
        found = _OWNER_WORDS.findall(blurb)
        self.assertEqual(found, [], f"owner-attributing words leaked into prompt text: {found}")

    def test_blurb_is_anonymous_across_pnl_signs_and_sizes(self):
        cases = [
            holding(pnl=10_000.0),
            holding(pnl=-10_000.0),
            holding(pnl=0.0),
            holding(quantity=1, average_price=0.05, last_price=0.06, pnl=0.01),
            holding(quantity=10_000_000, average_price=1e6, last_price=2e6, pnl=1e13),
        ]
        for h in cases:
            with self.subTest(pnl=h.pnl, qty=h.quantity):
                found = _OWNER_WORDS.findall(portfolio_sync.anonymous_position_blurb(h))
                self.assertEqual(found, [])

    def test_blurb_uses_observed_position_framing(self):
        blurb = portfolio_sync.anonymous_position_blurb(holding())
        self.assertIn("OBSERVED POSITION FOR CONTEXT", blurb)
        self.assertIn("Anonymized", blurb)

    def test_blurb_carries_the_numbers_the_model_needs(self):
        blurb = portfolio_sync.anonymous_position_blurb(
            holding(quantity=100, average_price=2800.0, last_price=2900.0)
        )
        self.assertIn("100", blurb)
        self.assertIn("2,800.00", blurb)
        self.assertIn("2,900.00", blurb)

    def test_blurb_signs_positive_pnl(self):
        self.assertIn("+", portfolio_sync.anonymous_position_blurb(holding(pnl=10_000.0)))

    def test_blurb_shows_negative_pnl_without_a_plus(self):
        blurb = portfolio_sync.anonymous_position_blurb(holding(pnl=-10_000.0))
        self.assertIn("-", blurb)
        self.assertNotIn("+", blurb)

    def test_zero_quantity_yields_no_blurb(self):
        self.assertEqual(portfolio_sync.anonymous_position_blurb(holding(quantity=0)), "")

    def test_negative_quantity_yields_no_blurb(self):
        self.assertEqual(portfolio_sync.anonymous_position_blurb(holding(quantity=-5)), "")

    def test_none_holding_yields_no_blurb(self):
        self.assertEqual(portfolio_sync.anonymous_position_blurb(None), "")

    # The UI caption is the deliberate counterpart — it *is* owner-attributed
    # and must never be routed to a prompt. These two tests together catch an
    # accidental swap of the two functions.

    def test_ui_caption_is_owner_attributed_by_design(self):
        caption = portfolio_sync.ui_caption_for(holding())
        self.assertIn("You hold", caption)

    def test_ui_caption_and_blurb_are_not_the_same_text(self):
        h = holding()
        self.assertNotEqual(
            portfolio_sync.ui_caption_for(h),
            portfolio_sync.anonymous_position_blurb(h),
        )

    def test_ui_caption_empty_for_zero_quantity_and_none(self):
        self.assertEqual(portfolio_sync.ui_caption_for(holding(quantity=0)), "")
        self.assertEqual(portfolio_sync.ui_caption_for(None), "")


class PositionHelperTests(PortfolioSyncTest):
    """server.services._position — the lazy indirection used by the pipeline."""

    def test_empty_ticker_returns_empty_string(self):
        self.assertEqual(_position.anonymous_position_block_for(""), "")
        self.assertEqual(_position.anonymous_position_block_for("   "), "")

    def test_no_holding_returns_empty_string(self):
        with patch.object(portfolio_sync, "holding_for", return_value=None):
            self.assertEqual(_position.anonymous_position_block_for("RELIANCE"), "")

    def test_delegates_to_the_anonymous_blurb(self):
        with patch.object(portfolio_sync, "holding_for", return_value=holding()):
            block = _position.anonymous_position_block_for("RELIANCE")
        self.assertIn("OBSERVED POSITION FOR CONTEXT", block)
        self.assertEqual(_OWNER_WORDS.findall(block), [])

    def test_ticker_is_normalised_before_lookup(self):
        with patch.object(portfolio_sync, "holding_for", return_value=None) as m:
            _position.anonymous_position_block_for("  reliance  ")
        m.assert_called_once_with("RELIANCE")

    def test_upstream_failure_is_swallowed(self):
        # The pipeline must not abort an analysis because Kite is down.
        with patch.object(portfolio_sync, "holding_for", side_effect=RuntimeError("kite down")):
            self.assertEqual(_position.anonymous_position_block_for("RELIANCE"), "")


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
        self.assertIn("holdings 500", snap.error)


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


class HoldingForTests(PortfolioSyncTest):
    def _seed(self, *holdings):
        portfolio_sync._cache = PortfolioSnapshot(
            holdings=list(holdings), fetched_at=int(time.time())
        )

    def test_finds_by_exact_symbol(self):
        self._seed(holding(tradingsymbol="RELIANCE"), holding(tradingsymbol="TCS"))
        self.assertEqual(portfolio_sync.holding_for("RELIANCE").tradingsymbol, "RELIANCE")

    def test_lookup_is_case_insensitive(self):
        self._seed(holding(tradingsymbol="RELIANCE"))
        self.assertIsNotNone(portfolio_sync.holding_for("reliance"))

    def test_unknown_symbol_returns_none(self):
        self._seed(holding(tradingsymbol="RELIANCE"))
        self.assertIsNone(portfolio_sync.holding_for("TCS"))

    def test_empty_symbol_returns_none(self):
        self._seed(holding())
        self.assertIsNone(portfolio_sync.holding_for(""))
        self.assertIsNone(portfolio_sync.holding_for(None))

    def test_empty_portfolio_returns_none(self):
        self._seed()
        self.assertIsNone(portfolio_sync.holding_for("RELIANCE"))


if __name__ == "__main__":
    unittest.main()

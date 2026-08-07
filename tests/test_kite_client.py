"""Unit tests for kite_client — auth/token lifecycle and symbol resolution.

Nothing here touches the network or the real token cache: `tests.support`
repoints LOCALAPPDATA at a temp dir, `kiteconnect` is intentionally left
uninstalled, and the instruments SQLite cache is seeded by hand.

The token-freshness tests pin the daily 06:00 IST expiry boundary from both
sides, including the "before 06:00, so yesterday's boundary applies" case that
is easy to get backwards.
"""
from __future__ import annotations

import json
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import tests.support as support  # noqa: F401  — must precede project imports

import kite_client

IST = timezone(timedelta(hours=5, minutes=30))


def ist(y, mo, d, h, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=IST)


class _FrozenDatetime(datetime):
    """datetime subclass with a pinned now(); fromtimestamp etc. inherited."""
    _frozen: datetime = None  # type: ignore[assignment]

    @classmethod
    def now(cls, tz=None):
        return cls._frozen.astimezone(tz) if tz is not None else cls._frozen


def freeze_now(when: datetime):
    """Patch kite_client's `datetime` so `datetime.now(_IST)` returns `when`."""
    frozen = type("Frozen", (_FrozenDatetime,), {"_frozen": when})
    return patch.object(kite_client, "datetime", frozen)


class KiteClientTest(unittest.TestCase):
    """Fresh sandboxed cache dir per test."""

    API_KEY = "test_api_key"
    API_SECRET = "test_api_secret"

    def setUp(self):
        super().setUp()
        support.fresh_cache_dir(type(self).__name__)
        support.assert_sandboxed(kite_client._cache_dir())
        self._env = patch.dict(
            os.environ,
            {"KITE_API_KEY": self.API_KEY, "KITE_API_SECRET": self.API_SECRET},
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def enable_kite(self):
        """Pretend the kiteconnect library is installed."""
        p = patch.object(kite_client, "KITECONNECT_AVAILABLE", True)
        p.start()
        self.addCleanup(p.stop)


# ── Config / enablement ──────────────────────────────────────────────────────

class EnablementTests(KiteClientTest):
    def test_disabled_without_the_library(self):
        with patch.object(kite_client, "KITECONNECT_AVAILABLE", False):
            self.assertFalse(kite_client.is_enabled())

    def test_disabled_without_api_key(self):
        self.enable_kite()
        with patch.dict(os.environ, {"KITE_API_KEY": ""}):
            self.assertFalse(kite_client.is_enabled())

    def test_disabled_without_api_secret(self):
        self.enable_kite()
        with patch.dict(os.environ, {"KITE_API_SECRET": ""}):
            self.assertFalse(kite_client.is_enabled())

    def test_enabled_with_library_and_both_creds(self):
        self.enable_kite()
        self.assertTrue(kite_client.is_enabled())

    def test_credentials_are_whitespace_stripped(self):
        with patch.dict(os.environ, {"KITE_API_KEY": "  padded  "}):
            self.assertEqual(kite_client._api_key(), "padded")

    def test_login_url_embeds_the_api_key(self):
        url = kite_client.login_url()
        self.assertIn(f"api_key={self.API_KEY}", url)
        self.assertIn("kite.zerodha.com/connect/login", url)

    def test_login_url_empty_without_api_key(self):
        with patch.dict(os.environ, {"KITE_API_KEY": ""}):
            self.assertEqual(kite_client.login_url(), "")


class FingerprintTests(KiteClientTest):
    def test_fingerprint_is_stable_and_prefixed(self):
        fp = kite_client._api_key_fingerprint()
        self.assertTrue(fp.startswith("sha256:"))
        self.assertEqual(fp, kite_client._api_key_fingerprint())

    def test_fingerprint_changes_with_the_key(self):
        first = kite_client._api_key_fingerprint()
        with patch.dict(os.environ, {"KITE_API_KEY": "different"}):
            self.assertNotEqual(first, kite_client._api_key_fingerprint())

    def test_fingerprint_does_not_leak_the_raw_key(self):
        self.assertNotIn(self.API_KEY, kite_client._api_key_fingerprint())

    def test_empty_key_yields_empty_fingerprint(self):
        with patch.dict(os.environ, {"KITE_API_KEY": ""}):
            self.assertEqual(kite_client._api_key_fingerprint(), "")


# ── Token persistence ────────────────────────────────────────────────────────

class TokenPersistenceTests(KiteClientTest):
    def test_round_trip(self):
        payload = {
            "access_token": "abc123",
            "user_name": "Test User",
            "obtained_at": int(time.time()),
            "api_key_fingerprint": kite_client._api_key_fingerprint(),
        }
        kite_client.save_token(payload)
        self.assertEqual(kite_client.load_cached_token(), payload)

    def test_load_returns_none_when_absent(self):
        self.assertIsNone(kite_client.load_cached_token())

    def test_load_returns_none_on_corrupt_json(self):
        kite_client._token_file().write_text("{not json", encoding="utf-8")
        self.assertIsNone(kite_client.load_cached_token())

    def test_token_from_a_different_api_key_is_discarded(self):
        kite_client.save_token({
            "access_token": "abc123",
            "obtained_at": int(time.time()),
            "api_key_fingerprint": "sha256:staleandwrong",
        })
        self.assertIsNone(kite_client.load_cached_token())

    def test_token_without_a_fingerprint_is_accepted(self):
        # Backwards compatibility with tokens saved before fingerprinting.
        kite_client.save_token({"access_token": "abc", "obtained_at": int(time.time())})
        self.assertIsNotNone(kite_client.load_cached_token())

    def test_save_does_not_leave_a_temp_file_behind(self):
        kite_client.save_token({"access_token": "abc"})
        leftovers = list(kite_client._cache_dir().glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_save_overwrites_previous_token(self):
        kite_client.save_token({"access_token": "first"})
        kite_client.save_token({"access_token": "second"})
        self.assertEqual(kite_client.load_cached_token()["access_token"], "second")

    def test_saved_file_is_valid_json(self):
        kite_client.save_token({"access_token": "abc", "obtained_at": 1})
        json.loads(kite_client._token_file().read_text(encoding="utf-8"))

    def test_non_serialisable_values_do_not_break_save(self):
        kite_client.save_token({"access_token": "abc", "login_time": datetime(2025, 8, 6)})
        self.assertIsNotNone(kite_client.load_cached_token())

    def test_clear_token_removes_the_file(self):
        kite_client.save_token({"access_token": "abc"})
        kite_client.clear_token()
        self.assertFalse(kite_client._token_file().exists())
        self.assertIsNone(kite_client.load_cached_token())

    def test_clear_token_is_idempotent(self):
        kite_client.clear_token()
        kite_client.clear_token()  # must not raise


# ── The daily 06:00 IST expiry boundary ──────────────────────────────────────

class TokenFreshnessTests(KiteClientTest):
    def fresh_at(self, obtained: datetime, now: datetime) -> bool:
        payload = {"access_token": "abc", "obtained_at": int(obtained.timestamp())}
        with freeze_now(now):
            return kite_client.token_is_fresh(payload)

    def test_none_payload_is_stale(self):
        self.assertFalse(kite_client.token_is_fresh(None))
        self.assertFalse(kite_client.token_is_fresh({}))

    def test_payload_without_access_token_is_stale(self):
        self.assertFalse(kite_client.token_is_fresh({"obtained_at": int(time.time())}))

    def test_payload_without_obtained_at_is_stale(self):
        self.assertFalse(kite_client.token_is_fresh({"access_token": "abc"}))

    # now = 07:00 IST → boundary is today 06:00 IST

    def test_obtained_after_this_mornings_boundary_is_fresh(self):
        self.assertTrue(self.fresh_at(ist(2025, 8, 6, 6, 30), ist(2025, 8, 6, 7, 0)))

    def test_obtained_exactly_on_the_boundary_is_fresh(self):
        self.assertTrue(self.fresh_at(ist(2025, 8, 6, 6, 0), ist(2025, 8, 6, 7, 0)))

    def test_obtained_one_minute_before_the_boundary_is_stale(self):
        self.assertFalse(self.fresh_at(ist(2025, 8, 6, 5, 59), ist(2025, 8, 6, 7, 0)))

    def test_yesterdays_token_is_stale_after_this_mornings_boundary(self):
        self.assertFalse(self.fresh_at(ist(2025, 8, 5, 23, 0), ist(2025, 8, 6, 7, 0)))

    # now = 05:00 IST (before today's boundary) → boundary is yesterday 06:00 IST

    def test_before_the_boundary_last_nights_token_is_still_fresh(self):
        self.assertTrue(self.fresh_at(ist(2025, 8, 5, 20, 0), ist(2025, 8, 6, 5, 0)))

    def test_before_the_boundary_a_token_from_two_days_ago_is_stale(self):
        self.assertFalse(self.fresh_at(ist(2025, 8, 4, 20, 0), ist(2025, 8, 6, 5, 0)))

    def test_before_the_boundary_yesterdays_pre_dawn_token_is_stale(self):
        self.assertFalse(self.fresh_at(ist(2025, 8, 5, 5, 0), ist(2025, 8, 6, 5, 0)))

    def test_token_issued_seconds_ago_is_fresh_at_any_hour(self):
        for hour in (0, 5, 6, 12, 23):
            with self.subTest(hour=hour):
                now = ist(2025, 8, 6, hour, 30)
                self.assertTrue(self.fresh_at(now - timedelta(seconds=30), now))

    def test_boundary_is_evaluated_in_ist_not_utc(self):
        # 01:00 UTC on 6 Aug == 06:30 IST, i.e. already past the boundary.
        # A token obtained at 00:30 UTC (06:00 IST) is exactly on it.
        obtained = datetime(2025, 8, 6, 0, 30, tzinfo=timezone.utc)
        now = datetime(2025, 8, 6, 1, 0, tzinfo=timezone.utc)
        self.assertTrue(self.fresh_at(obtained, now))


class IsAuthenticatedTests(KiteClientTest):
    def test_false_when_kite_is_disabled(self):
        with patch.object(kite_client, "KITECONNECT_AVAILABLE", False):
            self.assertFalse(kite_client.is_authenticated())

    def test_false_without_a_cached_token(self):
        self.enable_kite()
        self.assertFalse(kite_client.is_authenticated())

    def test_true_with_a_fresh_token(self):
        self.enable_kite()
        kite_client.save_token({
            "access_token": "abc",
            "obtained_at": int(time.time()),
            "api_key_fingerprint": kite_client._api_key_fingerprint(),
        })
        self.assertTrue(kite_client.is_authenticated())

    def test_false_with_a_stale_token(self):
        self.enable_kite()
        kite_client.save_token({
            "access_token": "abc",
            "obtained_at": int(time.time()) - 5 * 86400,
            "api_key_fingerprint": kite_client._api_key_fingerprint(),
        })
        self.assertFalse(kite_client.is_authenticated())

    def test_makes_no_network_call(self):
        # is_authenticated() is called on hot paths (every fetch_candles), so it
        # must stay purely local.
        self.enable_kite()
        with patch.object(kite_client, "get_client",
                          side_effect=AssertionError("must not build a client")):
            kite_client.is_authenticated()


class GetClientTests(KiteClientTest):
    def test_raises_when_not_configured(self):
        with patch.object(kite_client, "KITECONNECT_AVAILABLE", False):
            with self.assertRaises(kite_client.KiteNotAuthenticated):
                kite_client.get_client()

    def test_raises_when_no_token_cached(self):
        self.enable_kite()
        with self.assertRaises(kite_client.KiteNotAuthenticated):
            kite_client.get_client()

    def test_raises_token_expired_for_a_stale_token(self):
        self.enable_kite()
        kite_client.save_token({
            "access_token": "abc",
            "obtained_at": int(time.time()) - 5 * 86400,
            "api_key_fingerprint": kite_client._api_key_fingerprint(),
        })
        with self.assertRaises(kite_client.KiteTokenExpired):
            kite_client.get_client()

    def test_error_types_share_a_base(self):
        self.assertTrue(issubclass(kite_client.KiteNotAuthenticated, kite_client.KiteAuthError))
        self.assertTrue(issubclass(kite_client.KiteTokenExpired, kite_client.KiteAuthError))


class ExchangeRequestTokenTests(KiteClientTest):
    def test_requires_the_library(self):
        with patch.object(kite_client, "KITECONNECT_AVAILABLE", False):
            with self.assertRaises(kite_client.KiteAuthError):
                kite_client.exchange_request_token("rt")

    def test_requires_credentials(self):
        self.enable_kite()
        with patch.dict(os.environ, {"KITE_API_SECRET": ""}):
            with self.assertRaises(kite_client.KiteAuthError):
                kite_client.exchange_request_token("rt")

    def test_requires_a_request_token(self):
        self.enable_kite()
        with self.assertRaises(kite_client.KiteAuthError):
            kite_client.exchange_request_token("")


# ── Instruments cache + symbol resolution ────────────────────────────────────

INSTRUMENT_COLUMNS = (
    "instrument_token, exchange_token, tradingsymbol, name, instrument_type, "
    "segment, exchange, tick_size, lot_size, fetched_at"
)


class InstrumentsTest(KiteClientTest):
    def setUp(self):
        super().setUp()
        self.enable_kite()

    def seed(self, rows, fetched_at=None):
        """rows: (token, tradingsymbol, name, exchange, segment, instrument_type)"""
        ts = int(time.time()) if fetched_at is None else fetched_at
        with kite_client._connect_db() as conn:
            conn.executemany(
                f"INSERT OR REPLACE INTO instruments ({INSTRUMENT_COLUMNS}) "
                f"VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(t, None, sym, name, itype, seg, exch, 0.05, 1, ts)
                 for (t, sym, name, exch, seg, itype) in rows],
            )
            conn.commit()

    def seed_default(self, fetched_at=None):
        self.seed(fetched_at=fetched_at, rows=[
            (738561, "RELIANCE", "RELIANCE INDUSTRIES", "NSE", "NSE", "EQ"),
            (128083204, "RELIANCE", "RELIANCE INDUSTRIES", "BSE", "BSE", "EQ"),
            (738562, "RELIANCEPP", "RELIANCE INDUSTRIES PARTLY PAID", "NSE", "NSE", "EQ"),
            (2953217, "TCS", "TATA CONSULTANCY SERVICES", "NSE", "NSE", "EQ"),
            (884737, "TATAMOTORS", "TATA MOTORS", "NSE", "NSE", "EQ"),
            (256265, "NIFTY 50", "NIFTY 50", "NSE", "INDICES", "EQ"),
            (260105, "NIFTY BANK", "NIFTY BANK", "NSE", "INDICES", "EQ"),
            (999999, "SOMEFUT", "SOME FUTURE", "NFO", "NFO-FUT", "FUT"),
        ])


class InstrumentsCacheTests(InstrumentsTest):
    def test_row_count_zero_before_seeding(self):
        self.assertEqual(kite_client.instruments_row_count(), 0)

    def test_row_count_after_seeding(self):
        self.seed_default()
        self.assertEqual(kite_client.instruments_row_count(), 8)

    def test_age_is_huge_when_cache_is_missing(self):
        self.assertGreaterEqual(kite_client.instruments_age_seconds(), 10**9)

    def test_age_is_small_after_a_fresh_seed(self):
        self.seed_default()
        self.assertLess(kite_client.instruments_age_seconds(), 60)

    def test_age_reflects_an_old_fetched_at(self):
        self.seed_default(fetched_at=int(time.time()) - 7200)
        self.assertGreaterEqual(kite_client.instruments_age_seconds(), 7200)

    def test_refresh_needed_when_empty(self):
        self.assertTrue(kite_client.needs_instruments_refresh())

    def test_refresh_not_needed_when_fresh(self):
        self.seed_default()
        self.assertFalse(kite_client.needs_instruments_refresh(max_age_seconds=24 * 3600))

    def test_refresh_needed_when_stale(self):
        self.seed_default(fetched_at=int(time.time()) - 48 * 3600)
        self.assertTrue(kite_client.needs_instruments_refresh(max_age_seconds=24 * 3600))

    def test_async_refresh_is_a_no_op_when_unauthenticated(self):
        self.seed_default(fetched_at=int(time.time()) - 48 * 3600)
        with patch.object(kite_client, "is_authenticated", return_value=False), \
             patch.object(kite_client, "refresh_instruments",
                          side_effect=AssertionError("must not refresh")):
            kite_client.refresh_instruments_async()

    def test_refresh_skipped_when_cache_is_fresh(self):
        self.seed_default()
        with patch.object(kite_client, "get_client",
                          side_effect=AssertionError("must not hit the API")):
            self.assertEqual(kite_client.refresh_instruments(), 8)


class ResolveSymbolTests(InstrumentsTest):
    def test_empty_when_kite_is_disabled(self):
        self.seed_default()
        with patch.object(kite_client, "KITECONNECT_AVAILABLE", False):
            self.assertEqual(kite_client.resolve_symbol("RELIANCE"), [])

    def test_empty_when_cache_is_empty(self):
        self.assertEqual(kite_client.resolve_symbol("RELIANCE"), [])

    def test_single_character_query_is_rejected(self):
        self.seed_default()
        self.assertEqual(kite_client.resolve_symbol("R"), [])

    def test_empty_and_whitespace_queries_are_rejected(self):
        self.seed_default()
        self.assertEqual(kite_client.resolve_symbol(""), [])
        self.assertEqual(kite_client.resolve_symbol("   "), [])
        self.assertEqual(kite_client.resolve_symbol(None), [])

    def test_exact_match_ranks_first(self):
        self.seed_default()
        self.assertEqual(kite_client.resolve_symbol("RELIANCE")[0]["tradingsymbol"], "RELIANCE")

    def test_query_is_case_insensitive(self):
        self.seed_default()
        self.assertEqual(kite_client.resolve_symbol("reliance")[0]["tradingsymbol"], "RELIANCE")

    def test_query_is_whitespace_trimmed(self):
        self.seed_default()
        self.assertEqual(kite_client.resolve_symbol("  reliance  ")[0]["tradingsymbol"], "RELIANCE")

    def test_nse_is_preferred_over_bse_for_a_dual_listing(self):
        self.seed_default()
        self.assertEqual(kite_client.resolve_symbol("RELIANCE")[0]["exchange"], "NSE")

    def test_symbols_are_deduplicated_across_exchanges(self):
        self.seed_default()
        symbols = [r["tradingsymbol"] for r in kite_client.resolve_symbol("RELIANCE")]
        self.assertEqual(len(symbols), len(set(symbols)))

    def test_prefix_matches_follow_the_exact_match(self):
        self.seed_default()
        symbols = [r["tradingsymbol"] for r in kite_client.resolve_symbol("RELIANCE")]
        self.assertEqual(symbols[0], "RELIANCE")
        self.assertIn("RELIANCEPP", symbols)

    def test_name_substring_match_when_symbol_does_not_match(self):
        self.seed_default()
        symbols = [r["tradingsymbol"] for r in kite_client.resolve_symbol("CONSULTANCY")]
        self.assertIn("TCS", symbols)

    def test_derivatives_are_excluded(self):
        self.seed_default()
        symbols = [r["tradingsymbol"] for r in kite_client.resolve_symbol("SOMEFUT")]
        self.assertNotIn("SOMEFUT", symbols)

    def test_indices_are_excluded_from_equity_search(self):
        self.seed_default()
        symbols = [r["tradingsymbol"] for r in kite_client.resolve_symbol("NIFTY")]
        self.assertNotIn("NIFTY 50", symbols)

    def test_limit_is_honoured(self):
        self.seed_default()
        self.assertEqual(len(kite_client.resolve_symbol("RELIANCE", limit=1)), 1)

    def test_no_match_returns_empty_list(self):
        self.seed_default()
        self.assertEqual(kite_client.resolve_symbol("ZZZZZZZZ"), [])

    def test_result_rows_carry_the_fields_the_search_router_needs(self):
        self.seed_default()
        row = kite_client.resolve_symbol("RELIANCE")[0]
        self.assertEqual(
            set(row),
            {"instrument_token", "tradingsymbol", "name", "exchange", "segment",
             "instrument_type"},
        )
        self.assertIsInstance(row["instrument_token"], int)

    def test_like_wildcards_in_the_query_are_not_escaped(self):
        # "R%" reaches the LIKE pattern verbatim, so it behaves as a wildcard
        # search rather than a literal prefix. Locking in current behaviour —
        # a future escaping fix should update this test deliberately.
        self.seed_default()
        symbols = [r["tradingsymbol"] for r in kite_client.resolve_symbol("R%")]
        self.assertIn("RELIANCE", symbols)

    def test_hostile_query_does_not_damage_the_cache(self):
        self.seed_default()
        kite_client.resolve_symbol("'; DROP TABLE instruments;--")
        self.assertEqual(kite_client.instruments_row_count(), 8)


class InstrumentTokenLookupTests(InstrumentsTest):
    def test_returns_token_for_a_known_nse_symbol(self):
        self.seed_default()
        self.assertEqual(kite_client.get_instrument_token("RELIANCE"), 738561)

    def test_exchange_argument_selects_the_listing(self):
        self.seed_default()
        self.assertEqual(kite_client.get_instrument_token("RELIANCE", "BSE"), 128083204)

    def test_lookup_is_case_insensitive_and_trimmed(self):
        self.seed_default()
        self.assertEqual(kite_client.get_instrument_token("  reliance "), 738561)

    def test_unknown_symbol_returns_none(self):
        self.seed_default()
        self.assertIsNone(kite_client.get_instrument_token("NOSUCH"))

    def test_empty_symbol_returns_none(self):
        self.seed_default()
        self.assertIsNone(kite_client.get_instrument_token(""))

    def test_returns_none_when_disabled(self):
        self.seed_default()
        with patch.object(kite_client, "KITECONNECT_AVAILABLE", False):
            self.assertIsNone(kite_client.get_instrument_token("RELIANCE"))

    def test_index_lookup_by_tradingsymbol(self):
        self.seed_default()
        self.assertEqual(kite_client.get_instrument_token_for_index("NIFTY 50"), 256265)

    def test_index_lookup_is_case_insensitive(self):
        self.seed_default()
        self.assertEqual(kite_client.get_instrument_token_for_index("nifty bank"), 260105)

    def test_index_lookup_does_not_return_equities(self):
        self.seed_default()
        self.assertIsNone(kite_client.get_instrument_token_for_index("RELIANCE"))

    def test_index_lookup_unknown_returns_none(self):
        self.seed_default()
        self.assertIsNone(kite_client.get_instrument_token_for_index("NIFTY NOSUCH"))

    def test_index_lookup_empty_returns_none(self):
        self.seed_default()
        self.assertIsNone(kite_client.get_instrument_token_for_index(""))


class StatusSnapshotTests(InstrumentsTest):
    def test_shape_is_stable(self):
        snap = kite_client.status_snapshot()
        self.assertEqual(
            set(snap),
            {"enabled", "authenticated", "user_name", "user_id",
             "instruments_rows", "instruments_age_s", "refresh_pending"},
        )

    def test_unauthenticated_snapshot(self):
        snap = kite_client.status_snapshot()
        self.assertTrue(snap["enabled"])
        self.assertFalse(snap["authenticated"])
        self.assertEqual(snap["user_name"], "")

    def test_authenticated_snapshot_surfaces_user_fields(self):
        kite_client.save_token({
            "access_token": "abc",
            "user_name": "Test User",
            "user_id": "AB1234",
            "obtained_at": int(time.time()),
            "api_key_fingerprint": kite_client._api_key_fingerprint(),
        })
        snap = kite_client.status_snapshot()
        self.assertTrue(snap["authenticated"])
        self.assertEqual(snap["user_name"], "Test User")
        self.assertEqual(snap["user_id"], "AB1234")

    def test_snapshot_reports_instrument_rows(self):
        self.seed_default()
        self.assertEqual(kite_client.status_snapshot()["instruments_rows"], 8)

    def test_snapshot_makes_no_network_call(self):
        with patch.object(kite_client, "get_client",
                          side_effect=AssertionError("must not build a client")):
            kite_client.status_snapshot()


class RestWrapperTests(KiteClientTest):
    """The thin wrappers must degrade to empty values, not raise, on failure —
    except for auth errors, which are re-raised so the UI can prompt re-login."""

    def test_empty_symbol_lists_short_circuit(self):
        self.assertEqual(kite_client.ltp([]), {})
        self.assertEqual(kite_client.quote([]), {})

    def test_zero_instrument_token_short_circuits(self):
        self.assertEqual(kite_client.historical(0, datetime.now(), datetime.now(), "day"), [])

    def test_ltp_returns_empty_on_generic_failure(self):
        with patch.object(kite_client, "get_client", side_effect=RuntimeError("network")):
            self.assertEqual(kite_client.ltp(["NSE:RELIANCE"]), {})

    def test_ltp_reraises_auth_errors(self):
        with patch.object(kite_client, "get_client",
                          side_effect=kite_client.KiteTokenExpired("expired")):
            with self.assertRaises(kite_client.KiteAuthError):
                kite_client.ltp(["NSE:RELIANCE"])

    def test_quote_reraises_auth_errors(self):
        with patch.object(kite_client, "get_client",
                          side_effect=kite_client.KiteNotAuthenticated("nope")):
            with self.assertRaises(kite_client.KiteAuthError):
                kite_client.quote(["NSE:RELIANCE"])

    def test_historical_returns_empty_on_generic_failure(self):
        with patch.object(kite_client, "get_client", side_effect=RuntimeError("boom")):
            self.assertEqual(
                kite_client.historical(738561, datetime.now(), datetime.now(), "day"), []
            )

    def test_holdings_returns_empty_list_on_failure(self):
        with patch.object(kite_client, "get_client", side_effect=RuntimeError("boom")):
            self.assertEqual(kite_client.holdings(), [])

    def test_positions_returns_the_empty_shape_on_failure(self):
        with patch.object(kite_client, "get_client", side_effect=RuntimeError("boom")):
            self.assertEqual(kite_client.positions(), {"day": [], "net": []})

    def test_holdings_reraises_auth_errors(self):
        with patch.object(kite_client, "get_client",
                          side_effect=kite_client.KiteTokenExpired("expired")):
            with self.assertRaises(kite_client.KiteAuthError):
                kite_client.holdings()


if __name__ == "__main__":
    unittest.main()

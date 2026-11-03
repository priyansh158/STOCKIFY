"""Unit tests for tick_buffer — the SQLite ringbuffer behind live Kite ticks.

Every test runs against a throwaway DB inside the tests sandbox (see
tests/support), so the developer's real ticks.sqlite is never opened.

Focus areas:
  * the idempotency contract the module docstring promises for reconnect storms
  * timestamp coercion across the three shapes Kite emits (datetime / int / absent)
  * retention trimming boundaries
  * subscription bookkeeping
  * reader behaviour on empty / partial / hostile input
"""
from __future__ import annotations

import time
import unittest
from datetime import datetime, timezone

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import tick_buffer


def tick(token=1, ts=1_700_000_000, ltp=100.0, volume=None, oi=None, **extra):
    """Build a kiteconnect-shaped tick dict."""
    d = {"instrument_token": token, "exchange_timestamp": ts, "last_price": ltp}
    if volume is not None:
        d["volume_traded"] = volume
    if oi is not None:
        d["oi"] = oi
    d.update(extra)
    return d


class TickBufferTest(SandboxedDBTest):
    db_modules = (tick_buffer,)


class InitTests(TickBufferTest):
    def test_init_db_is_idempotent(self):
        tick_buffer.init_db()
        tick_buffer.init_db()
        self.assertEqual(tick_buffer.total_rows(), 0)

    def test_db_file_lands_in_sandbox(self):
        support.assert_sandboxed(tick_buffer._db_path())
        self.assertEqual(tick_buffer._db_path().name, "ticks.sqlite")


class WriteTicksTests(TickBufferTest):
    def test_writes_and_reads_back(self):
        self.assertEqual(tick_buffer.write_ticks([tick(ltp=101.5, volume=42)]), 1)
        got = tick_buffer.latest_tick(1)
        self.assertEqual(got["ltp"], 101.5)
        self.assertEqual(got["ts"], 1_700_000_000)
        self.assertEqual(got["volume"], 42)

    def test_empty_input_writes_nothing(self):
        self.assertEqual(tick_buffer.write_ticks([]), 0)
        self.assertEqual(tick_buffer.total_rows(), 0)

    def test_bulk_write(self):
        ticks = [tick(token=1, ts=1000 + i, ltp=100.0 + i) for i in range(50)]
        self.assertEqual(tick_buffer.write_ticks(ticks), 50)
        self.assertEqual(tick_buffer.total_rows(), 50)

    # ── Idempotency: the load-bearing promise for reconnect storms ────────────

    def test_duplicate_token_ts_does_not_create_a_second_row(self):
        t = tick(token=7, ts=555, ltp=10.0)
        tick_buffer.write_ticks([t])
        tick_buffer.write_ticks([t])
        tick_buffer.write_ticks([t])
        self.assertEqual(tick_buffer.total_rows(), 1)

    def test_replaying_a_whole_batch_is_a_no_op_on_row_count(self):
        batch = [tick(token=1, ts=1000 + i, ltp=float(i)) for i in range(10)]
        tick_buffer.write_ticks(batch)
        before = tick_buffer.total_rows()
        tick_buffer.write_ticks(batch)
        self.assertEqual(tick_buffer.total_rows(), before)

    def test_same_token_ts_with_new_price_overwrites(self):
        # INSERT OR REPLACE — a corrected tick for the same instant wins.
        tick_buffer.write_ticks([tick(token=3, ts=900, ltp=10.0)])
        tick_buffer.write_ticks([tick(token=3, ts=900, ltp=99.0)])
        self.assertEqual(tick_buffer.total_rows(), 1)
        self.assertEqual(tick_buffer.latest_tick(3)["ltp"], 99.0)

    def test_return_value_counts_rows_submitted_not_rows_added(self):
        # Documented as "number of rows written"; upserts count too.
        t = tick(token=4, ts=1, ltp=5.0)
        self.assertEqual(tick_buffer.write_ticks([t, t]), 2)
        self.assertEqual(tick_buffer.total_rows(), 1)

    def test_different_tokens_same_ts_are_distinct_rows(self):
        tick_buffer.write_ticks([tick(token=1, ts=100), tick(token=2, ts=100)])
        self.assertEqual(tick_buffer.total_rows(), 2)

    # ── Timestamp coercion ───────────────────────────────────────────────────

    def test_datetime_exchange_timestamp_is_converted_to_epoch(self):
        dt = datetime(2025, 8, 6, 10, 30, tzinfo=timezone.utc)
        tick_buffer.write_ticks([tick(token=9, ts=dt, ltp=1.0)])
        self.assertEqual(tick_buffer.latest_tick(9)["ts"], int(dt.timestamp()))

    def test_missing_exchange_timestamp_falls_back_to_wall_clock(self):
        before = int(time.time())
        tick_buffer.write_ticks([{"instrument_token": 11, "last_price": 5.0}])
        after = int(time.time())
        ts = tick_buffer.latest_tick(11)["ts"]
        self.assertGreaterEqual(ts, before)
        self.assertLessEqual(ts, after)

    def test_uncoercible_timestamp_falls_back_to_wall_clock(self):
        before = int(time.time())
        tick_buffer.write_ticks([tick(token=12, ts="not-a-timestamp", ltp=5.0)])
        ts = tick_buffer.latest_tick(12)["ts"]
        self.assertGreaterEqual(ts, before)

    # ── Field aliases and skip rules ─────────────────────────────────────────

    def test_ltp_alias_accepted_when_last_price_absent(self):
        tick_buffer.write_ticks([{"instrument_token": 20, "exchange_timestamp": 5, "ltp": 77.0}])
        self.assertEqual(tick_buffer.latest_tick(20)["ltp"], 77.0)

    def test_volume_alias_accepted(self):
        tick_buffer.write_ticks(
            [{"instrument_token": 21, "exchange_timestamp": 5, "last_price": 1.0, "volume": 9}]
        )
        self.assertEqual(tick_buffer.latest_tick(21)["volume"], 9)

    def test_tick_without_price_is_skipped(self):
        tick_buffer.write_ticks([{"instrument_token": 30, "exchange_timestamp": 5}])
        self.assertEqual(tick_buffer.total_rows(), 0)

    def test_tick_without_token_is_skipped(self):
        tick_buffer.write_ticks([{"exchange_timestamp": 5, "last_price": 1.0}])
        self.assertEqual(tick_buffer.total_rows(), 0)

    def test_token_zero_is_skipped(self):
        tick_buffer.write_ticks([tick(token=0, ltp=1.0)])
        self.assertEqual(tick_buffer.total_rows(), 0)

    def test_good_ticks_survive_a_bad_neighbour_in_the_same_batch(self):
        tick_buffer.write_ticks([
            {"instrument_token": 40, "exchange_timestamp": 1, "last_price": 1.0},
            {"instrument_token": None, "last_price": 2.0},          # skipped
            {"instrument_token": 41, "exchange_timestamp": 1},      # skipped: no price
            {"instrument_token": 42, "exchange_timestamp": 1, "last_price": 3.0},
        ])
        self.assertEqual(tick_buffer.total_rows(), 2)
        self.assertIsNotNone(tick_buffer.latest_tick(40))
        self.assertIsNotNone(tick_buffer.latest_tick(42))
        self.assertIsNone(tick_buffer.latest_tick(41))

    def test_zero_volume_is_stored_as_null(self):
        # `int(v) or None` maps a real 0 to NULL rather than 0.
        tick_buffer.write_ticks([tick(token=50, ts=1, ltp=1.0, volume=0)])
        self.assertIsNone(tick_buffer.latest_tick(50)["volume"])

    def test_string_numerics_are_coerced(self):
        tick_buffer.write_ticks(
            [{"instrument_token": "60", "exchange_timestamp": 5, "last_price": "12.5"}]
        )
        self.assertEqual(tick_buffer.latest_tick(60)["ltp"], 12.5)


class LatestTickTests(TickBufferTest):
    def test_returns_none_for_unknown_token(self):
        self.assertIsNone(tick_buffer.latest_tick(999))

    def test_returns_none_for_falsy_token(self):
        self.assertIsNone(tick_buffer.latest_tick(0))
        self.assertIsNone(tick_buffer.latest_tick(None))

    def test_picks_the_highest_ts_not_the_last_inserted(self):
        tick_buffer.write_ticks([tick(token=1, ts=3000, ltp=30.0)])
        tick_buffer.write_ticks([tick(token=1, ts=1000, ltp=10.0)])  # late-arriving old tick
        self.assertEqual(tick_buffer.latest_tick(1)["ltp"], 30.0)


class LatestTicksBulkTests(TickBufferTest):
    def setUp(self):
        super().setUp()
        tick_buffer.write_ticks([
            tick(token=1, ts=100, ltp=1.0),
            tick(token=1, ts=200, ltp=2.0),
            tick(token=2, ts=150, ltp=15.0),
            tick(token=3, ts=50, ltp=5.0),
        ])

    def test_empty_token_list_returns_empty_dict(self):
        self.assertEqual(tick_buffer.latest_ticks_bulk([]), {})

    def test_returns_latest_per_token(self):
        got = tick_buffer.latest_ticks_bulk([1, 2, 3])
        self.assertEqual(got[1]["ltp"], 2.0)
        self.assertEqual(got[1]["ts"], 200)
        self.assertEqual(got[2]["ltp"], 15.0)
        self.assertEqual(got[3]["ltp"], 5.0)

    def test_unknown_tokens_are_omitted_not_nulled(self):
        got = tick_buffer.latest_ticks_bulk([1, 424242])
        self.assertIn(1, got)
        self.assertNotIn(424242, got)

    def test_all_unknown_returns_empty_dict(self):
        self.assertEqual(tick_buffer.latest_ticks_bulk([9998, 9999]), {})

    def test_duplicate_tokens_in_request_are_tolerated(self):
        got = tick_buffer.latest_ticks_bulk([1, 1, 1])
        self.assertEqual(set(got), {1})

    def test_agrees_with_latest_tick_singular(self):
        bulk = tick_buffer.latest_ticks_bulk([1, 2, 3])
        for token in (1, 2, 3):
            with self.subTest(token=token):
                self.assertEqual(bulk[token], tick_buffer.latest_tick(token))

    def test_large_token_list(self):
        tokens = list(range(1000, 1200))
        tick_buffer.write_ticks([tick(token=t, ts=1, ltp=float(t)) for t in tokens])
        got = tick_buffer.latest_ticks_bulk(tokens)
        self.assertEqual(len(got), len(tokens))


class TrimOldTests(TickBufferTest):
    def test_removes_only_rows_older_than_retention(self):
        now = int(time.time())
        tick_buffer.write_ticks([
            tick(token=1, ts=now - 100, ltp=1.0),        # fresh
            tick(token=1, ts=now - 10_000, ltp=2.0),     # stale vs 3600s retention
        ])
        removed = tick_buffer.trim_old(retention_seconds=3600)
        self.assertEqual(removed, 1)
        self.assertEqual(tick_buffer.total_rows(), 1)
        self.assertEqual(tick_buffer.latest_tick(1)["ltp"], 1.0)

    def test_no_op_when_everything_is_fresh(self):
        tick_buffer.write_ticks([tick(token=1, ts=int(time.time()), ltp=1.0)])
        self.assertEqual(tick_buffer.trim_old(retention_seconds=3600), 0)
        self.assertEqual(tick_buffer.total_rows(), 1)

    def test_zero_retention_clears_the_buffer(self):
        tick_buffer.write_ticks([tick(token=1, ts=int(time.time()) - 5, ltp=1.0)])
        tick_buffer.trim_old(retention_seconds=0)
        self.assertEqual(tick_buffer.total_rows(), 0)

    def test_trim_on_empty_db_is_safe(self):
        self.assertEqual(tick_buffer.trim_old(), 0)

    def test_default_retention_is_eight_hours(self):
        self.assertEqual(tick_buffer._RETENTION_SECONDS, 8 * 3600)


class CheckpointTests(TickBufferTest):
    def test_checkpoint_does_not_raise_and_preserves_rows(self):
        tick_buffer.write_ticks([tick(token=1, ts=1, ltp=1.0)])
        tick_buffer.checkpoint()
        self.assertEqual(tick_buffer.total_rows(), 1)


class SubscriptionTests(TickBufferTest):
    def test_add_and_list(self):
        tick_buffer.add_subscription(101, "macro")
        tick_buffer.add_subscription(102, "analysis")
        self.assertEqual(tick_buffer.subscribed_tokens(), {101, 102})

    def test_filter_by_kind(self):
        tick_buffer.add_subscription(101, "macro")
        tick_buffer.add_subscription(102, "analysis")
        tick_buffer.add_subscription(103, "macro")
        self.assertEqual(tick_buffer.subscribed_tokens("macro"), {101, 103})
        self.assertEqual(tick_buffer.subscribed_tokens("analysis"), {102})

    def test_unknown_kind_returns_empty_set(self):
        tick_buffer.add_subscription(101, "macro")
        self.assertEqual(tick_buffer.subscribed_tokens("portfolio"), set())

    def test_default_kind_is_analysis(self):
        tick_buffer.add_subscription(200)
        self.assertEqual(tick_buffer.subscribed_tokens("analysis"), {200})

    def test_re_adding_updates_kind_rather_than_duplicating(self):
        tick_buffer.add_subscription(101, "analysis")
        tick_buffer.add_subscription(101, "macro")
        self.assertEqual(tick_buffer.subscribed_tokens(), {101})
        self.assertEqual(tick_buffer.subscribed_tokens("macro"), {101})
        self.assertEqual(tick_buffer.subscribed_tokens("analysis"), set())

    def test_remove(self):
        tick_buffer.add_subscription(101, "macro")
        tick_buffer.remove_subscription(101)
        self.assertEqual(tick_buffer.subscribed_tokens(), set())

    def test_remove_unknown_token_is_safe(self):
        tick_buffer.remove_subscription(4242)
        self.assertEqual(tick_buffer.subscribed_tokens(), set())

    def test_remove_by_kind_leaves_other_kinds(self):
        tick_buffer.add_subscription(101, "macro")
        tick_buffer.add_subscription(102, "analysis")
        tick_buffer.remove_subscriptions_of_kind("macro")
        self.assertEqual(tick_buffer.subscribed_tokens(), {102})

    def test_falsy_token_is_ignored_on_add_and_remove(self):
        tick_buffer.add_subscription(0, "macro")
        tick_buffer.add_subscription(None, "macro")
        self.assertEqual(tick_buffer.subscribed_tokens(), set())
        tick_buffer.remove_subscription(0)  # must not raise

    def test_subscriptions_are_independent_of_ticks(self):
        tick_buffer.add_subscription(101, "macro")
        tick_buffer.write_ticks([tick(token=999, ts=1, ltp=1.0)])
        self.assertEqual(tick_buffer.subscribed_tokens(), {101})
        self.assertEqual(tick_buffer.total_rows(), 1)


class TotalRowsTests(TickBufferTest):
    def test_zero_on_fresh_db(self):
        self.assertEqual(tick_buffer.total_rows(), 0)

    def test_counts_across_tokens(self):
        tick_buffer.write_ticks([tick(token=1, ts=1), tick(token=2, ts=1), tick(token=2, ts=2)])
        self.assertEqual(tick_buffer.total_rows(), 3)


if __name__ == "__main__":
    unittest.main()

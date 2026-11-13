"""LCM 7.1: /api/data/recorder marks an old status row stale itself and never calls it connected."""
from __future__ import annotations

import unittest

import tests.support  # noqa: F401  — must precede project imports

from server.routers import data as data_router
from stockify import timeutil


def row(age_s: float, connected: int = 1) -> dict:
    return {"wall_ns": timeutil.now_ns() - int(age_s * 1e9), "connected": connected, "subs_full": 1, "subs_quote": 0,
            "packets_per_s": 204.3, "queue_depth": 0, "writer_lag_ms": None,
            "last_packet_age_ms_json": '{"full": 94}', "bytes_today": 0}


class RecorderStalenessTest(unittest.TestCase):
    def test_a_fresh_row_is_not_stale_and_keeps_its_connection(self):
        dto = data_router._recorder_dto(row(3))
        self.assertFalse(dto.stale)
        self.assertTrue(dto.connected)

    def test_an_old_row_is_stale_and_not_connected(self):
        dto = data_router._recorder_dto(row(8 * 86400))
        self.assertTrue(dto.stale)
        self.assertFalse(dto.connected)

    def test_the_limit_is_a_multiple_of_the_recorders_report_interval(self):
        from stockify.daemon import recorder_health as rh
        self.assertEqual(rh.STALE_AFTER_S, rh.HEALTH_EVERY_S * rh.MISSED_REPORTS_FOR_STALE)
        lim = rh.STALE_AFTER_S
        self.assertFalse(data_router._recorder_dto(row(lim - 5)).stale)
        self.assertTrue(data_router._recorder_dto(row(lim + 5)).stale)

    def test_the_ticker_writes_at_the_shared_interval(self):
        import inspect
        from stockify.daemon import recorder_health as rh
        from stockify.daemon.ticker import RecorderSession
        default = inspect.signature(RecorderSession.__init__).parameters["health_every_s"].default
        self.assertEqual(default, rh.HEALTH_EVERY_S)

    def test_market_open_follows_the_session_rules_not_a_clock_of_ours(self):
        from unittest import mock
        with mock.patch("stockify.daemon.market_day.inside_market_hours", return_value=True):
            self.assertTrue(data_router._recorder_dto(row(3)).market_open)
        with mock.patch("stockify.daemon.market_day.inside_market_hours", return_value=False):
            self.assertFalse(data_router._recorder_dto(row(3)).market_open)
        with mock.patch("stockify.daemon.market_day.inside_market_hours", side_effect=ValueError("no rule")):
            self.assertFalse(data_router._recorder_dto(row(3)).market_open)


if __name__ == "__main__":
    unittest.main()

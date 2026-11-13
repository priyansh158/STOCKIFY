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

    def test_the_limit_is_the_servers_constant(self):
        lim = data_router.RECORDER_STALE_S
        self.assertFalse(data_router._recorder_dto(row(lim - 5)).stale)
        self.assertTrue(data_router._recorder_dto(row(lim + 5)).stale)


if __name__ == "__main__":
    unittest.main()

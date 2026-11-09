"""At most two live KiteTicker connections out of Kite's three (M5.14, K-1, D-20; LCB 5.1)."""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import paths, proc
from stockify.daemon import conn_budget


class ConnBudgetTest(unittest.TestCase):
    def setUp(self):
        support.fresh_cache_dir(type(self).__name__)
        self.folder = paths.lab_dir() / "ticker_connections"

    def test_third_live_connection_is_refused(self):
        api = conn_budget.register("api", limit=3)
        daemon = conn_budget.register("daemon", limit=3)
        with self.assertRaises(conn_budget.BudgetExceeded):
            conn_budget.register("dev", limit=3)
        self.assertFalse((self.folder / f"dev-{os.getpid()}.json").exists())
        daemon.release()
        conn_budget.register("dev", limit=3).release()
        api.release()
        self.assertEqual(sorted(p.name for p in self.folder.glob("*.json")), [])

    def test_ticket_file_holds_pid_created_and_role(self):
        t = conn_budget.register("daemon", limit=3)
        self.addCleanup(t.release)
        body = json.loads((self.folder / f"daemon-{os.getpid()}.json").read_text(encoding="utf-8"))
        self.assertEqual(body, {"pid": os.getpid(), "created": proc.creation_time(os.getpid()), "role": "daemon"})

    def test_dead_ticket_is_cleaned(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        dead = self.folder / "dev-424242.json"
        dead.write_text(json.dumps({"pid": os.getpid(), "created": 1, "role": "dev"}), encoding="utf-8")
        broken = self.folder / "api-1.json"
        broken.write_text("{not json", encoding="utf-8")
        a = conn_budget.register("api", limit=3)
        b = conn_budget.register("daemon", limit=3)
        self.assertFalse(dead.exists())
        self.assertFalse(broken.exists())
        a.release()
        b.release()

    def test_same_role_in_the_same_process_does_not_count_itself(self):
        a = conn_budget.register("api", limit=3)
        b = conn_budget.register("daemon", limit=3)
        again = conn_budget.register("api", limit=3)              # a reconnect after the thread died
        for t in (a, b, again):
            t.release()

    def test_release_is_idempotent(self):
        t = conn_budget.register("api", limit=3)
        t.release()
        t.release()

    def test_live_tickets_are_counted(self):
        with mock.patch.object(proc, "is_alive", return_value=True):
            self.folder.mkdir(parents=True, exist_ok=True)
            (self.folder / "dev-777.json").write_text(json.dumps({"pid": 777, "created": 5, "role": "dev"}),
                                                     encoding="utf-8")
            a = conn_budget.register("api", limit=3)
            with self.assertRaises(conn_budget.BudgetExceeded):
                conn_budget.register("daemon", limit=3)
            a.release()

    def test_roles_must_be_safe_segments(self):
        for bad in ("", "../x", "a/b", "con"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                conn_budget.register(bad, limit=3)

    def test_limit_comes_from_kite_limits(self):
        self.assertEqual(conn_budget.limit(), 3)


class _FakeTicker:
    MODE_LTP, MODE_QUOTE, MODE_FULL = "ltp", "quote", "full"

    def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.closed = False

    def connect(self, threaded=False):  # noqa: ANN001
        pass

    def close(self):
        self.closed = True


class CallersTakeTicketsTest(unittest.TestCase):
    """The daemon (role daemon), the API's worker (role api) and kite check (role dev) all go through the budget."""

    def setUp(self):
        support.fresh_cache_dir(type(self).__name__)
        self.folder = paths.lab_dir() / "ticker_connections"

    def _fill(self, *roles):  # noqa: ANN002
        tickets = [conn_budget.register(r, limit=3) for r in roles]
        for t in tickets:
            self.addCleanup(t.release)

    def test_daemon_ticker_takes_and_gives_back_a_ticket(self):
        import kiteconnect  # noqa: F401  — make_live_ticker imports KiteTicker from it
        from stockify.daemon import _release_ticket
        from stockify.daemon.ticker import make_live_ticker
        with mock.patch("kiteconnect.KiteTicker", _FakeTicker):
            ticker = make_live_ticker("key", "tok")
            self.assertTrue((self.folder / f"daemon-{os.getpid()}.json").exists())
            _release_ticket(ticker)
            self.assertFalse((self.folder / f"daemon-{os.getpid()}.json").exists())
            self._fill("api", "dev")
            with self.assertRaises(conn_budget.BudgetExceeded):
                make_live_ticker("key", "tok")

    def test_api_worker_refuses_a_third_connection(self):
        import kite_client
        import kite_ticker_worker
        self._fill("daemon", "dev")
        sup = kite_ticker_worker._Supervisor()
        with mock.patch.object(kite_ticker_worker, "KITETICKER_AVAILABLE", True), \
                mock.patch.object(kite_ticker_worker, "KiteTicker", _FakeTicker), \
                mock.patch.object(kite_client, "is_authenticated", return_value=True), \
                mock.patch.object(kite_client, "load_cached_token", return_value={"access_token": "tok"}):
            self.assertFalse(sup.ensure_running())
        self.assertIsNone(sup.ticker)
        self.assertIn("not connecting", sup.status()["errors"][-1])

    def test_api_worker_takes_role_api_and_releases_on_shutdown(self):
        import kite_client
        import kite_ticker_worker
        sup = kite_ticker_worker._Supervisor()
        with mock.patch.object(kite_ticker_worker, "KITETICKER_AVAILABLE", True), \
                mock.patch.object(kite_ticker_worker, "KiteTicker", _FakeTicker), \
                mock.patch.object(kite_client, "is_authenticated", return_value=True), \
                mock.patch.object(kite_client, "load_cached_token", return_value={"access_token": "tok"}):
            self.assertTrue(sup.ensure_running())
            sup.thread.join(5)
            self.assertTrue((self.folder / f"api-{os.getpid()}.json").exists())
            sup.shutdown()
        self.assertFalse((self.folder / f"api-{os.getpid()}.json").exists())

    def test_kite_check_websocket_is_refused_when_two_are_live(self):
        from stockify import kitecheck
        self._fill("api", "daemon")
        with mock.patch.object(kitecheck, "_websocket_check", side_effect=AssertionError("must not connect")):
            check = kitecheck._websocket("key", "tok", 256265)
        self.assertFalse(check.ok)
        self.assertEqual(check.name, "websocket")


if __name__ == "__main__":
    unittest.main()

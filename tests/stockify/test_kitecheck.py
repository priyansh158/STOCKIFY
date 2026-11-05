"""stockify.kitecheck: the offline failure paths (the live path is owner-run)."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import tests.support as support  # noqa: F401  — must precede project imports

import kite_client
from stockify import kitecheck


class KiteCheckTests(unittest.TestCase):
    def test_unconfigured_stops_at_config(self):
        with support.isolated_env({}):
            checks = kitecheck.run()
        self.assertEqual([(c.name, c.ok) for c in checks], [("config", False)])
        self.assertFalse(kitecheck.passed(checks))

    def test_missing_token_stops_at_token(self):
        with patch.object(kite_client, "is_enabled", return_value=True), \
             patch.object(kite_client, "load_cached_token", return_value=None):
            checks = kitecheck.run()
        self.assertEqual([(c.name, c.ok) for c in checks], [("config", True), ("token", False)])
        self.assertIn("log in", checks[-1].detail)

    def test_stale_token_names_the_06_00_rule(self):
        stale = {"access_token": "x", "obtained_at": 0}
        with patch.object(kite_client, "is_enabled", return_value=True), \
             patch.object(kite_client, "load_cached_token", return_value=stale):
            checks = kitecheck.run()
        self.assertFalse(checks[-1].ok)
        self.assertIn("06:00 IST", checks[-1].detail)

    def test_output_never_contains_the_token(self):
        secret = {"access_token": "SECRET-TOKEN-VALUE", "obtained_at": 0}
        with patch.object(kite_client, "is_enabled", return_value=True), \
             patch.object(kite_client, "load_cached_token", return_value=secret):
            checks = kitecheck.run()
        self.assertTrue(all("SECRET-TOKEN-VALUE" not in c.detail for c in checks))

    def test_messages_are_ascii_for_the_windows_console(self):
        with patch.object(kite_client, "is_enabled", return_value=True), \
             patch.object(kite_client, "load_cached_token", return_value=None):
            for c in kitecheck.run():
                c.detail.encode("ascii")


if __name__ == "__main__":
    unittest.main()

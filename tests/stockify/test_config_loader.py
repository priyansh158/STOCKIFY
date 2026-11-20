"""stockify.config.loader: shipped configs are valid, and the I-1 rules bite."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports

from stockify.config import loader


def row(**kw) -> dict:
    base = {"applies": {"segment": "FO"}, "effective_from": "2024-10-01",
            "pct": "0.0003503", "source": "Z-3", "verified": False}
    base.update(kw)
    return base


class ShippedConfigTests(unittest.TestCase):
    def test_every_shipped_file_loads(self):
        with support.isolated_env():
            docs = loader.load_all()
        self.assertEqual(set(docs), set(loader.SCHEMAS) | set(loader.CSV_HEADERS))


class LoaderRuleTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=str(support.SANDBOX_ROOT)))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def charges(self, rows: list) -> dict:
        doc = {"schema": "stockify.charges/1", "broker": "zerodha",
               "components": {"exchange_txn": rows}}
        (self.dir / "charges.json").write_text(json.dumps(doc), encoding="utf-8")
        return loader.load_json("charges.json", self.dir)

    def test_rates_convert_exactly_to_ppb(self):
        out = self.charges([row()])
        self.assertEqual(out["components"]["exchange_txn"][0]["pct_ppb"], 350_300)

    def test_inexact_rate_is_rejected(self):
        with self.assertRaises(loader.ConfigError):
            self.charges([row(pct="0.00000000001")])

    def test_verify_row_after_an_earlier_row_starts_on_observed_on(self):
        out = self.charges([
            row(),
            row(effective_from="VERIFY", observed_on="2026-10-29", pct="0.0003553"),
        ])
        dates = [r["effective_from"] for r in out["components"]["exchange_txn"]]
        self.assertEqual(dates, ["2024-10-01", "2026-10-29"])

    def test_lone_verify_row_starts_on_the_floor(self):
        out = self.charges([row(effective_from="VERIFY", observed_on="2026-10-29")])
        self.assertEqual(out["components"]["exchange_txn"][0]["effective_from"],
                         loader.FLOOR_DATE)

    def test_verify_row_must_stay_unverified(self):
        with self.assertRaises(loader.ConfigError):
            self.charges([row(effective_from="VERIFY", observed_on="2026-10-29",
                              verified=True, verified_on="2026-10-29")])

    def test_verified_row_needs_verified_on(self):
        with self.assertRaises(loader.ConfigError):
            self.charges([row(verified=True)])

    def test_row_without_source_is_rejected(self):
        bad = row()
        del bad["source"]
        with self.assertRaises(loader.ConfigError):
            self.charges([bad])

    def test_a_dated_row_without_effective_from_is_refused_not_skipped(self):
        late = row(effective_from="2025-01-01")
        gone = row(applies={"segment": "CM"})
        del gone["effective_from"]
        for rows in ([row(), gone], [gone, late], [row(), "text"]):
            with self.subTest(rows=len(rows)):
                with self.assertRaisesRegex(loader.ConfigError, r"effective_from"):
                    self.charges(rows)
        self.charges([row(), late])                                   # all rows dated: still fine

    def test_a_list_of_objects_with_no_dates_at_all_is_not_a_dated_list(self):
        doc = {"schema": "stockify.sessions/1", "versions": [{"group": "CM"}, {"group": "FO_OPT"}]}
        out = loader._walk_dated(doc, "sessions", allow_verify=False)
        self.assertEqual(out["versions"], [{"group": "CM"}, {"group": "FO_OPT"}])

    def test_duplicate_date_for_one_key_is_rejected(self):
        with self.assertRaises(loader.ConfigError):
            self.charges([row(), row(pct="0.0001")])

    def test_rows_come_back_sorted(self):
        out = self.charges([row(effective_from="2026-04-01"), row()])
        dates = [r["effective_from"] for r in out["components"]["exchange_txn"]]
        self.assertEqual(dates, ["2024-10-01", "2026-04-01"])

    def test_verify_outside_charges_is_rejected(self):
        doc = {"schema": "stockify.sessions/1", "versions": [
            {"group": "CM", "effective_from": "VERIFY", "observed_on": "2026-10-29",
             "phases": [], "source": "X-7", "verified": False}]}
        (self.dir / "sessions.json").write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaises(loader.ConfigError):
            loader.load_json("sessions.json", self.dir)

    def test_wrong_schema_tag_is_rejected(self):
        (self.dir / "storage.json").write_text('{"schema": "x/1"}', encoding="utf-8")
        with self.assertRaises(loader.ConfigError):
            loader.load_json("storage.json", self.dir)

    def test_log_caps_in_storage_json_are_bounded(self):
        good = json.loads((loader.CONFIG_DIR / "storage.json").read_text(encoding="utf-8"))
        self.assertEqual((good["log_max_mb"], good["log_backups"]), (20, 5))
        for key, bad in (("log_max_mb", 0), ("log_max_mb", 1001), ("log_max_mb", True), ("log_max_mb", "20"),
                         ("log_backups", -1), ("log_backups", 101), ("log_backups", 1.5)):
            with self.subTest(key=key, bad=bad):
                (self.dir / "storage.json").write_text(json.dumps({**good, key: bad}), encoding="utf-8")
                with self.assertRaises(loader.ConfigError):
                    loader.load_json("storage.json", self.dir)
        (self.dir / "storage.json").write_text(json.dumps({**good, "log_backups": 0}), encoding="utf-8")
        self.assertEqual(loader.load_json("storage.json", self.dir)["log_backups"], 0)

    def test_fundamentals_json_is_checked(self):
        """FS 1: NSE hosts only, bounded whole numbers, a real date, api entries {path, warmup}; unknown keys
        refused."""
        good = json.loads((loader.CONFIG_DIR / "fundamentals.json").read_text(encoding="utf-8"))
        bad_cases = (
            {"liquidity_floor_paise": "8000000000"}, {"liquidity_floor_paise": True}, {"liquidity_days": 0},
            {"basket_size": 501}, {"top_kept": 2.5}, {"filings_from": "20210401"}, {"filings_from": "2021-13-01"},
            {"nifty500_list_url": "http://nsearchives.nseindia.com/x.csv"},
            {"nifty500_list_fallback_url": "https://example.com/x.csv"},
            {"api": {**good["api"], "base": "https://example.com"}},
            {"api": {**good["api"], "results": {"path": "/api/quote-equity?symbol=X",
                                                     "warmup": "https://www.nseindia.com/"}}},
            {"api": {**good["api"], "results": {"path": "/api/x", "warmup": "https://evil.example/"}}},
            {"api": {**good["api"], "results": {"path": "/api/x"}}},
            {"surprise": 1},
            {"corporate_action_ratios": [2]}, {"corporate_action_ratios": ["1"]}, {"corporate_action_ratios": []},
            {"corporate_action_ratios": ["3", "2"]}, {"corporate_action_ratios": ["2", "2.1"]},
            {"corporate_action_tolerance": 0.04}, {"corporate_action_tolerance": "0.5"},
            {"corporate_action_tolerance": "0"}, {"corporate_action_hold_fall": "1"},
            {"corporate_action_hold_fall": "1.95"}, {"corporate_action_hold_fall": 1.3},
            {"balances": []}, {"balances": [0.5]}, {"balances": ["1.5"]}, {"balances": ["0.5", "0.5"]},
            {"balances": ["-0.1"]}, {"balances": "0.5"},
            {"basket_sizes": []}, {"basket_sizes": [0]}, {"basket_sizes": [10, 10]}, {"basket_sizes": ["10"]},
            {"basket_sizes": [51]}, {"basket_sizes": [60], "top_kept": 50},
        )
        for change in bad_cases:
            with self.subTest(change=change):
                (self.dir / "fundamentals.json").write_text(json.dumps({**good, **change}), encoding="utf-8")
                with self.assertRaises(loader.ConfigError):
                    loader.load_json("fundamentals.json", self.dir)
        missing = dict(good)
        del missing["liquidity_days"]
        (self.dir / "fundamentals.json").write_text(json.dumps(missing), encoding="utf-8")
        with self.assertRaisesRegex(loader.ConfigError, "liquidity_days is missing"):
            loader.load_json("fundamentals.json", self.dir)
        extra_api = {**good, "api": {**good["api"], "results": {"path": "/api/corporates-financial-results",
                                                                "warmup": "https://www.nseindia.com/companies"}}}
        (self.dir / "fundamentals.json").write_text(json.dumps(extra_api), encoding="utf-8")
        self.assertIn("results", loader.load_json("fundamentals.json", self.dir)["api"])

    def test_candles_json_is_checked(self):
        """CW 1: candles.json: history_years a whole number of years, rps a decimal string above 0, the 429 backoff a
        non-empty list of whole seconds that never shrinks; unknown or missing keys refused, naming them."""
        good = json.loads((loader.CONFIG_DIR / "candles.json").read_text(encoding="utf-8"))
        bad_cases = (
            {"history_years": 0}, {"history_years": 21}, {"history_years": "6"}, {"history_years": True},
            {"history_years": 6.5}, {"rps": 2}, {"rps": "0"}, {"rps": "-1"}, {"rps": "abc"}, {"rps": "11"},
            {"rate_limit_backoff_s": []}, {"rate_limit_backoff_s": [0]}, {"rate_limit_backoff_s": [4, 2]},
            {"rate_limit_backoff_s": ["1"]}, {"rate_limit_backoff_s": [1, 601]}, {"rate_limit_backoff_s": 1},
            {"note": 5}, {"surprise": 1},
        )
        for change in bad_cases:
            with self.subTest(change=change):
                (self.dir / "candles.json").write_text(json.dumps({**good, **change}), encoding="utf-8")
                with self.assertRaises(loader.ConfigError):
                    loader.load_json("candles.json", self.dir)
        for key in ("history_years", "rps", "rate_limit_backoff_s"):
            missing = {k: v for k, v in good.items() if k != key}
            (self.dir / "candles.json").write_text(json.dumps(missing), encoding="utf-8")
            with self.subTest(missing=key), self.assertRaisesRegex(loader.ConfigError, f"{key} is missing"):
                loader.load_json("candles.json", self.dir)
        (self.dir / "candles.json").write_text(json.dumps({**good, "rps": "0.5", "rate_limit_backoff_s": [2, 2]}),
                                               encoding="utf-8")
        self.assertEqual(loader.load_json("candles.json", self.dir)["rps"], "0.5")

    def test_candles_rps_never_exceeds_kites_historical_limit(self):
        """Fix round 1 (5): candles.json rps at most kite_limits.json historical_rps (K-8), read from the same folder
        when it has one, else the shipped one."""
        good = json.loads((loader.CONFIG_DIR / "candles.json").read_text(encoding="utf-8"))
        for rps, ok in (("3", True), ("3.5", False), ("2.99", True)):
            with self.subTest(rps=rps):
                (self.dir / "candles.json").write_text(json.dumps({**good, "rps": rps}), encoding="utf-8")
                if ok:
                    loader.load_json("candles.json", self.dir)
                else:
                    with self.assertRaisesRegex(loader.ConfigError, "historical_rps"):
                        loader.load_json("candles.json", self.dir)
        limits = json.loads((loader.CONFIG_DIR / "kite_limits.json").read_text(encoding="utf-8"))
        (self.dir / "kite_limits.json").write_text(json.dumps({**limits, "historical_rps": 1}), encoding="utf-8")
        (self.dir / "candles.json").write_text(json.dumps({**good, "rps": "2"}), encoding="utf-8")
        with self.assertRaisesRegex(loader.ConfigError, "historical_rps"):
            loader.load_json("candles.json", self.dir)

    def test_watchlist_json_is_checked(self):
        """CW 2: whole numbers within bounds, decimal strings for the tolerance, no unknown or missing keys."""
        good = json.loads((loader.CONFIG_DIR / "watchlist.json").read_text(encoding="utf-8"))
        bad_cases = (
            {"max_stocks": 0}, {"max_stocks": 501}, {"max_stocks": "50"}, {"max_stocks": True},
            {"lookback_days": 0}, {"lookback_days": 3651}, {"min_history_days": 0},
            {"min_history_days": 400, "lookback_days": 365},
            {"basis_tolerance": 0.02}, {"basis_tolerance": "0"}, {"basis_tolerance": "0.5"}, {"basis_tolerance": "-0.01"},
            {"alert_batch_s": 0}, {"alert_batch_s": 3601}, {"max_alert_emails_per_day": 0},
            {"max_alert_emails_per_day": 101}, {"results_lead_trading_days": 0}, {"results_lead_trading_days": 11},
            {"tap_frames": 7}, {"tap_frames": 100_001}, {"surprise": 1},
            {"desk_watch_new_grades_per_night": -1}, {"desk_watch_new_grades_per_night": 501},
            {"desk_watch_new_grades_per_night": "10"}, {"desk_watch_new_grades_per_night": True},
        )
        for change in bad_cases:
            with self.subTest(change=change):
                (self.dir / "watchlist.json").write_text(json.dumps({**good, **change}), encoding="utf-8")
                with self.assertRaises(loader.ConfigError):
                    loader.load_json("watchlist.json", self.dir)
        for key in good:
            if key in ("schema", "note"):
                continue
            with self.subTest(missing=key):
                (self.dir / "watchlist.json").write_text(
                    json.dumps({k: v for k, v in good.items() if k != key}), encoding="utf-8")
                with self.assertRaisesRegex(loader.ConfigError, f"{key} is missing"):
                    loader.load_json("watchlist.json", self.dir)

    def test_schedule_json_is_checked(self):
        """CW 6: every job's keys and times are checked, candles and watch_results included, so a typo fails
        `stockify config` and not the daemon's loop."""
        import copy
        good = json.loads((loader.CONFIG_DIR / "schedule.json").read_text(encoding="utf-8"))

        def with_(path, value):
            doc = copy.deepcopy(good)
            node = doc
            for k in path[:-1]:
                node = node[k]
            if value is KeyError:
                del node[path[-1]]
            else:
                node[path[-1]] = value
            return doc

        bad = [
            (("jobs", "candles", "start"), "16.40"), (("jobs", "candles", "start"), "25:00"),
            (("jobs", "candles", "until"), "6:30pm"), (("jobs", "candles", "until"), KeyError),
            (("jobs", "candles", "retry_every_min"), 0), (("jobs", "candles", "retry_every_min"), "15"),
            (("jobs", "candles", "retry_every_mn"), 15), (("jobs", "candles"), "16:40"),
            (("jobs", "watch_results", "start"), "21:0"), (("jobs", "watch_results", "retry_every_min"), True),
            (("jobs", "watch_results"), {"begin": "21:00"}), (("jobs", "watch_results"), KeyError),
            (("jobs", "desk", "until"), "24:00"), (("jobs", "refdata"), "8:30"), (("jobs", "refdata"), {"start": "08:30"}),
            (("jobs", "backup"), 545), (("jobs", "day_end"), "23:59"), (("jobs", "day_endd"), "23:59:59"),
            (("jobs", "token_dependent_retry", "every_min"), 0), (("jobs", "token_dependent_retry", "until"), "9am"),
            (("keep_awake", "from"), "8:45"), (("sleep_after_close", "enabled"), "yes"),
            (("sleep_after_close", "idle_minutes"), 0), (("sleep_after_close", "wake_ist"), "6"),
            (("owner_tz",), "Nowhere/Land"), (("kite_login_window_ist", "from"), "06:00:00"),
            (("login_reminders_ist",), "06:05"), (("login_reminders_ist",), ["06:05", "8"]),
            (("surprise",), 1),
        ]
        for path, value in bad:
            with self.subTest(path=path, value=value):
                (self.dir / "schedule.json").write_text(json.dumps(with_(path, value)), encoding="utf-8")
                with self.assertRaisesRegex(loader.ConfigError, "schedule.json"):
                    loader.load_json("schedule.json", self.dir)
        (self.dir / "schedule.json").write_text(json.dumps(good), encoding="utf-8")
        self.assertEqual(loader.load_json("schedule.json", self.dir)["jobs"]["candles"]["start"], "16:40")

    def test_csv_header_is_checked(self):
        (self.dir / "calendar").mkdir()
        (self.dir / "calendar" / "nse_holidays.csv").write_text("date,desc\n", encoding="utf-8")
        with self.assertRaises(loader.ConfigError):
            loader.load_csv("calendar/nse_holidays.csv", self.dir)


if __name__ == "__main__":
    unittest.main()

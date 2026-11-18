"""stockify CLI, engine gateway and the moved Kite candle helpers."""
from __future__ import annotations

import io
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime
from unittest.mock import patch

import tests.support  # noqa: F401  — must precede project imports

import stockify
from stockify import __main__ as cli
from stockify import engine
from stockify.data import history

_MISSING = object()


@contextmanager
def engine_module(value):
    """Make `from stockify import _engine` see `value` (None = not built), even when another test has already
    imported the real module (import finds it as an attribute of the `stockify` package, not in sys.modules)."""
    saved = stockify.__dict__.get("_engine", _MISSING)
    engine._module = None
    with patch.dict("sys.modules", {"stockify._engine": value}):
        if value is None:
            stockify.__dict__.pop("_engine", None)
        else:
            stockify._engine = value
        try:
            yield
        finally:
            if saved is _MISSING:
                stockify.__dict__.pop("_engine", None)
            else:
                stockify._engine = saved
            engine._module = None


class CliTests(unittest.TestCase):
    def run_cli(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_config_command_validates_shipped_configs(self):
        code, out, _ = self.run_cli("config")
        self.assertEqual(code, 0)
        self.assertIn("config OK", out)

    def test_no_command_is_a_stub_any_more(self):
        # M6 made `export`, `sweep` and `walkforward` real, the last two stubs: a command that is still planned
        # goes back into cli._STUBS and gets its own "not implemented" test here.
        self.assertEqual(cli._STUBS, {})
        for name in ("sweep", "walkforward"):
            with self.subTest(command=name):
                out = io.StringIO()
                with redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
                    cli.main([name, "--help"])
                self.assertEqual(ctx.exception.code, 0)
                self.assertIn(f"stockify {name}", out.getvalue())
                self.assertNotIn("not implemented", out.getvalue())

    def test_fundamentals_status_is_read_only_and_plain(self):
        from stockify.fundamentals import job
        s = {"universe": 500, "liquid": 380, "with_parsed": 370, "clean": {"non-financial": (300, 320),
             "financial": (40, 60)}, "gate": dict(job.GATE), "held": {"the filing is in a format this parser has no "
             "map for": 12}, "latest_list": "2026-11-16", "budget": {"day": "2026-11-16", "spent": 410, "budget": 900}}
        with patch.object(job, "status", return_value=s):
            code, out, _ = self.run_cli("fundamentals", "status")
        self.assertEqual(code, 0)
        self.assertIn("non-financial: 300 of 320 (93.8 %; the gate is 90 %): meets the gate", out)
        self.assertIn("financial: 40 of 60 (66.7 %; the gate is 80 %): below the gate", out)
        self.assertIn("12  the filing is in a format this parser has no map for", out)
        self.assertIn("Latest list: 2026-11-16.", out)
        self.assertIn("410 of 900 requests used", out)

    def test_fundamentals_reparse_needs_a_symbol_or_all_held(self):
        from stockify.fundamentals import xbrl
        with patch.object(xbrl, "reparse", side_effect=AssertionError("reparsed")):
            code, _, err = self.run_cli("fundamentals", "reparse")
        self.assertEqual(code, 2)
        self.assertIn("--all-held", err)
        with patch.object(xbrl, "reparse", return_value={"reset": 3, "no_file": 1}) as rp:
            code, out, _ = self.run_cli("fundamentals", "reparse", "--symbol", "sbilife")
        self.assertEqual(code, 0)
        rp.assert_called_once_with(symbol="sbilife")
        self.assertIn("3 of SBILIFE's held filings", out)
        self.assertIn("1 held filings have no XBRL file kept", out)
        with patch.object(xbrl, "reparse", return_value={"reset": 7, "no_file": 0}) as rp:
            code, out, _ = self.run_cli("fundamentals", "reparse", "--all-held")
        self.assertEqual(code, 0)
        rp.assert_called_once_with(symbol=None)

    def test_daemon_once_returns(self):
        code, _, _ = self.run_cli("daemon", "--once")
        self.assertEqual(code, 0)


class EngineGatewayTests(unittest.TestCase):
    def test_missing_module_raises_engine_unavailable_with_a_fix(self):
        with engine_module(None), self.assertRaises(engine.EngineUnavailable) as ctx:
            engine.load()
        self.assertIn("-BuildEngine", str(ctx.exception))

    def test_python_version_mismatch_is_detected(self):
        class Fake:
            @staticmethod
            def build_info():
                return {"python_version": "2.7"}
        with engine_module(Fake), self.assertRaises(engine.EngineUnavailable) as ctx:
            engine.load()
        self.assertIn("2.7", str(ctx.exception))


class HistoryHelperTests(unittest.TestCase):
    def test_rows_are_normalised_and_bad_rows_dropped(self):
        dt = datetime(2026, 10, 1, 9, 15)
        rows = history._kite_rows([
            {"date": dt, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": None},
            {"date": None, "open": 1, "high": 1, "low": 1, "close": 1},
            {"date": dt, "open": "x", "high": 1, "low": 1, "close": 1},
        ])
        self.assertEqual(rows, [{"date": dt, "open": 1.0, "high": 2.0, "low": 0.5,
                                 "close": 1.5, "volume": 0}])

    def test_unauthenticated_fetch_returns_empty(self):
        self.assertEqual(
            history._fetch_candles_kite("RELIANCE", datetime(2026, 1, 1),
                                        datetime(2026, 2, 1), "1d"), [])

    def test_unknown_interval_returns_empty(self):
        with patch.object(history.kite_client, "is_authenticated", return_value=True):
            self.assertEqual(
                history._fetch_candles_kite("RELIANCE", datetime(2026, 1, 1),
                                            datetime(2026, 2, 1), "7m"), [])


if __name__ == "__main__":
    unittest.main()

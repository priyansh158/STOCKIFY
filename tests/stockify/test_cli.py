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

    def test_unimplemented_command_names_its_milestone(self):
        code, _, err = self.run_cli("compare")             # `margin` became real in M4.9, `parity` in M5.10
        self.assertEqual(code, 2)
        self.assertIn("M6", err)

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

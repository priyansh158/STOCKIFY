"""D-135: one rule for which headline figures are shown (stockify.reporting.figures), on the run page data, the
compare table and report.md."""
from __future__ import annotations

import json
import unittest

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest
from tests.stockify.research.fixtures import make_run

import stockify.store as store
from stockify import research
from stockify.config import loader
from stockify.reporting import compare as rc
from stockify.reporting import figures

LIMIT = 240


def metrics(days: int, **kw) -> dict:  # noqa: ANN003
    m = {"days": days, "cagr": -0.0438, "volatility": 0.1, "sharpe": 1.2, "sortino": 1.5, "calmar": -246.46,
         "turnover": 9.0, "max_drawdown": 0.01, "trades": 1, "costs_total_paise": 500, "gross_pnl_paise": 2000,
         "cost_share_of_gross": 0.25}
    m.update(kw)
    return m


class PresentTests(unittest.TestCase):
    def test_the_threshold_is_dated_config_not_code(self):
        self.assertEqual(figures.min_days_yearly(), research.settings()["reporting"]["min_days_yearly"])
        self.assertEqual(figures.min_days_yearly(), LIMIT)

    def test_a_short_run_loses_every_yearly_figure_with_one_reason(self):
        shown, why = figures.present(metrics(1))
        for k in figures.YEARLY_KEYS:
            self.assertIsNone(shown[k], k)
            self.assertEqual(why[k], figures.TOO_SHORT)
        self.assertEqual(shown["max_drawdown"], 0.01)          # not a yearly figure
        self.assertEqual(shown["trades"], 1)

    def test_the_boundary_is_the_configured_day_count(self):
        self.assertIsNone(figures.present(metrics(LIMIT - 1))[0]["cagr"])
        shown, why = figures.present(metrics(LIMIT))
        self.assertEqual(shown["cagr"], -0.0438)
        self.assertEqual(why, {})
        self.assertIsNone(figures.present(metrics(5), min_days=6)[0]["cagr"])
        self.assertEqual(figures.present(metrics(5), min_days=5)[0]["cagr"], -0.0438)

    def test_a_figure_already_missing_is_not_explained_by_length(self):
        shown, why = figures.present(metrics(1, sharpe=None))
        self.assertNotIn("sharpe", why)
        self.assertIsNone(shown["sharpe"])

    def test_missing_or_bad_days_counts_as_too_short(self):
        for days in (None, "x", True):
            self.assertIsNone(figures.present(metrics(days))[0]["cagr"], days)

    def test_costs_over_a_loss_before_costs_is_not_a_ratio(self):
        m = metrics(LIMIT, gross_pnl_paise=-154, costs_total_paise=498, cost_share_of_gross=-3.2357)
        shown, why = figures.present(m)
        self.assertIsNone(shown["cost_share_of_gross"])
        self.assertEqual(why["cost_share_of_gross"], figures.NO_GROSS)
        zero, why0 = figures.present(metrics(LIMIT, gross_pnl_paise=0, cost_share_of_gross=None))
        self.assertIsNone(zero["cost_share_of_gross"])

    def test_costs_over_a_profit_is_the_exact_ratio(self):
        shown, why = figures.present(metrics(LIMIT, gross_pnl_paise=2000, costs_total_paise=500))
        self.assertEqual(shown["cost_share_of_gross"], 0.25)
        self.assertNotIn("cost_share_of_gross", why)

    def test_an_old_stored_negative_ratio_without_paise_is_still_withheld(self):
        m = {"days": LIMIT, "cost_share_of_gross": -3.2}
        self.assertIsNone(figures.present(m)[0]["cost_share_of_gross"])

    def test_the_input_is_not_changed(self):
        m = metrics(1)
        figures.present(m)
        self.assertEqual(m, metrics(1))


class ConfigTests(unittest.TestCase):
    def test_bad_values_are_refused(self):
        shipped = json.loads((loader.CONFIG_DIR / "research.json").read_text(encoding="utf-8"))
        import tempfile
        from pathlib import Path
        for bad in (0, 1, True, "240", 1.5, 100_001):
            doc = json.loads(json.dumps(shipped))
            doc["reporting"]["min_days_yearly"] = bad
            with tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "research.json").write_text(json.dumps(doc), encoding="utf-8")
                with self.assertRaisesRegex(loader.ConfigError, "reporting.min_days_yearly"):
                    loader.load_json("research.json", Path(tmp))


class ReportMdTests(unittest.TestCase):
    def render(self, m: dict) -> str:
        import tempfile
        from pathlib import Path
        from stockify.oracle import report
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp)
            full = dict(m, nav_start_paise=100_000_000, nav_end_paise=100_000_000, max_drawdown_days=0,
                        profit_factor=None, avg_trade_paise=None, exposure=0.1, max_participation=0.0,
                        win_rate=None, costs_paise={}, rejects={})
            (f / "metrics.json").write_text(json.dumps(full), encoding="utf-8")
            (f / "manifest.json").write_text(json.dumps({"run_id": "x", "bundle_sha256": "0" * 64, "packs": [],
                                                         "labels": []}), encoding="utf-8")
            (f / "run.json").write_text(json.dumps({"name": "n", "strategy": {"kind": "python"}, "seed": 1,
                                                    "fill_model": {"kind": "bar"}, "price_basis": "close"}),
                                        encoding="utf-8")
            (f / "equity.csv").write_text("trade_date,nav_paise" + chr(10), encoding="utf-8")
            return report.write(f).read_text(encoding="utf-8")

    def test_report_md_uses_the_same_words_as_the_other_pages(self):
        text = self.render(metrics(1, gross_pnl_paise=-154, costs_total_paise=498, cost_share_of_gross=-3.2357))
        self.assertIn(f"| CAGR | {figures.TOO_SHORT} |", text)
        self.assertIn(f"| Calmar | {figures.TOO_SHORT} |", text)
        self.assertIn(figures.NO_GROSS, text)
        self.assertNotIn("-4.38", text)
        self.assertNotIn("-323", text)

    def test_report_md_of_a_long_run_shows_the_figures(self):
        text = self.render(metrics(LIMIT))
        self.assertIn("| CAGR | -4.38% |", text)
        self.assertIn("| share of gross P&L", text)
        self.assertIn("25.00%", text)


class CompareTests(SandboxedDBTest):
    db_modules = (store,)

    def setUp(self):
        super().setUp()
        make_run("short", None, navs=[100_100_000, 100_300_000, 100_200_000])
        make_run("long", None, daily_returns=[0.0005, -0.0003] * 130)

    def set_metrics(self, run_id: str, **kw) -> None:  # noqa: ANN003
        m = json.loads(store.get_run(run_id)["metrics_json"])
        m.update(kw)
        store.update_run(run_id, metrics_json=json.dumps(m))

    def rows(self, *ids):  # noqa: ANN002, ANN201
        return {m.key: m for m in rc.compare_runs(list(ids)).metrics}

    def test_yearly_rows_of_a_short_run_are_null_with_the_reason_and_a_long_run_keeps_them(self):
        rows = self.rows("short", "long")
        for key in ("cagr", "sharpe", "sortino", "calmar"):
            self.assertIsNone(rows[key].values[0], key)
            self.assertEqual(rows[key].reasons[0], figures.TOO_SHORT, key)
            self.assertIsNone(rows[key].reasons[1], key)
        self.assertIsNotNone(rows["cagr"].values[1])
        self.assertEqual(rows["net_pnl"].reasons, [None, None])

    def test_cost_share_of_a_loss_before_costs_is_n_a_with_a_reason(self):
        self.set_metrics("short", gross_pnl_paise=-154, costs_total_paise=498, cost_share_of_gross=-3.2357)
        self.set_metrics("long", gross_pnl_paise=2000, costs_total_paise=500, cost_share_of_gross=0.25)
        row = self.rows("short", "long")["cost_share"]
        self.assertEqual(row.values, [None, 0.25])
        self.assertEqual(row.reasons, [figures.NO_GROSS, None])

    def test_the_text_table_says_why_instead_of_a_dash(self):
        text = rc.format_table(rc.compare_runs(["short", "long"]))
        cagr_line = next(line for line in text.splitlines() if line.startswith("cagr"))
        self.assertIn(figures.TOO_SHORT, cagr_line)

    def test_two_short_runs_agree_on_a_withheld_row(self):
        make_run("short2", None, navs=[100_000_000, 100_900_000, 100_100_000])
        c = rc.compare_runs(["short", "short2"])
        self.assertEqual({m.key: m.values for m in c.metrics}["cagr"], [None, None])


@support.requires_fastapi
class ApiTests(CompareTests):
    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from server.main import app
        self.client = TestClient(app, base_url=support.API_BASE_URL)

    def test_run_detail_withholds_the_yearly_figures_and_says_why(self):
        body = self.client.get("/api/tests/short").json()
        self.assertIsNone(body["metrics"]["cagr"])
        self.assertEqual(body["withheld"]["cagr"], figures.TOO_SHORT)
        self.assertEqual(body["metrics"]["days"], 3)
        long = self.client.get("/api/tests/long").json()
        self.assertIsNotNone(long["metrics"]["cagr"])
        self.assertNotIn("cagr", long["withheld"])

    def test_compare_endpoint_carries_the_reasons(self):
        body = self.client.get("/api/tests/compare", params=[("ids", "short"), ("ids", "long")]).json()
        cagr = next(m for m in body["metrics"] if m["key"] == "cagr")
        self.assertEqual(cagr["reasons"], [figures.TOO_SHORT, None])
        self.assertIsNone(cagr["values"][0])


if __name__ == "__main__":
    unittest.main()

"""The web shell (D-141): no site footer, the chart keeps TradingView's own attribution, the NOTICE file carries the
copyright, and the Next.js dev badge is off."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

REPO = Path(__file__).resolve().parents[2]
WEB = REPO / "web"


def _text(*parts: str) -> str:
    return REPO.joinpath(*parts).read_text(encoding="utf-8")


class WebShellTests(unittest.TestCase):
    def test_no_site_footer(self):
        layout = _text("web", "src", "app", "layout.tsx")
        self.assertNotIn("<footer", layout)
        self.assertNotIn("Simulation only", layout)
        self.assertNotIn("stk-foot", _text("web", "src", "app", "globals.css"))

    def test_every_chart_keeps_the_attribution_logo(self):
        creators = [p for p in (WEB / "src").rglob("*.ts*") if "createChart(" in p.read_text(encoding="utf-8")]
        self.assertTrue(creators)
        for p in creators:
            with self.subTest(file=p.name):
                self.assertEqual(len(re.findall(r"createChart\(", p.read_text(encoding="utf-8"))), 1)
                self.assertIn("attributionLogo: true", p.read_text(encoding="utf-8"))
        self.assertNotIn("attributionLogo: false", "".join(p.read_text(encoding="utf-8") for p in (WEB / "src").rglob("*.ts*")))

    def test_notice_file_carries_the_tradingview_copyright(self):
        notice = _text("NOTICE")
        self.assertIn("TradingView, Inc.", notice)
        self.assertIn("Apache", notice)
        self.assertIn("https://www.tradingview.com/", notice)

    def test_chart_options_that_keep_the_scale_labels_clear(self):
        chart = _text("web", "src", "components", "PriceChart.tsx")
        self.assertIn("entireTextOnly: true", chart)
        # The last-price label and line follow live polling: off for a finished run.
        self.assertIn("lastValueVisible: false, priceLineVisible: false", chart)       # at creation
        self.assertRegex(chart, r"const on = !!pollMs;\s*candlesRef\.current\?\.applyOptions\(\{ lastValueVisible: on, priceLineVisible: on \}\)")

    def test_next_dev_indicator_is_hidden(self):
        self.assertRegex(_text("web", "next.config.ts"), r"devIndicators:\s*false")


if __name__ == "__main__":
    unittest.main()

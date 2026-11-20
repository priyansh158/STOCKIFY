"""LCM 7.4: the top bar and the home page list read the same destinations (web/src/lib/nav.ts), and every page in it exists."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

APP = Path(__file__).resolve().parents[2] / "web" / "src"


class NavSingleSourceTest(unittest.TestCase):
    def test_both_places_read_the_shared_list_and_keep_no_list_of_their_own(self):
        for f in ("components/AppNav.tsx", "app/page.tsx"):
            text = (APP / f).read_text(encoding="utf-8")
            self.assertIn("DESTINATIONS", text, f)
            self.assertNotRegex(text, r'href:\s*"/', f)

    def test_each_destination_has_a_label_a_note_and_a_page(self):
        text = (APP / "lib" / "nav.ts").read_text(encoding="utf-8")
        rows = re.findall(r'\{ href: "(/[a-z]+)", label: "([^"]+)", note: "([^"]+)" \}', text)
        self.assertEqual([r[0] for r in rows], ["/portfolio", "/watchlist", "/alerts", "/tests", "/trade", "/research", "/data"])
        for href, label, note in rows:
            self.assertTrue(label and note)
            self.assertTrue((APP / "app" / href.lstrip("/") / "page.tsx").is_file(), href)


if __name__ == "__main__":
    unittest.main()

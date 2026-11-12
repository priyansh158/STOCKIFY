"""research.json: the shipped file validates and every bad value is refused, naming its key."""
from __future__ import annotations

import copy
import json
import random
import tempfile
import unittest
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import research
from stockify.config import loader

SHIPPED = json.loads((loader.CONFIG_DIR / "research.json").read_text(encoding="utf-8"))


def load(doc: dict) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "research.json").write_text(json.dumps(doc), encoding="utf-8")
        return loader.load_json("research.json", Path(tmp))


def changed(*path, value):
    doc = copy.deepcopy(SHIPPED)
    node = doc
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return doc


class ResearchConfigTests(unittest.TestCase):
    def refused(self, doc: dict, key: str) -> None:
        with self.assertRaises(loader.ConfigError) as cm:
            load(doc)
        self.assertIn(key, str(cm.exception))

    def test_shipped_research_json_validates(self):
        doc = load(SHIPPED)
        self.assertEqual(doc["schema"], "stockify.research/1")
        self.assertIsNone(doc["holdout_from"])
        self.assertEqual(research.settings()["pbo"], {"slices": 16, "min_trials": 2})

    def test_holdout_from_must_be_an_iso_date_or_null(self):
        self.assertEqual(load(changed("holdout_from", value="2026-08-01"))["holdout_from"], "2026-08-01")
        for bad in ("2026-13-40", 20260801, "", "20260801", "2026-W31-6", " 2026-08-01", "2026-8-1"):
            with self.subTest(bad=bad):
                self.refused(changed("holdout_from", value=bad), "holdout_from")

    def test_bootstrap_ci_must_be_in_the_open_unit_interval(self):
        for bad in ("0", "1", "0.9x", "1.5", "-0.5", ""):
            with self.subTest(bad=bad):
                self.refused(changed("bootstrap", "ci", value=bad), "bootstrap.ci")
        load(changed("bootstrap", "ci", value="0.95"))
        self.refused(changed("bootstrap", "resamples", value=99), "bootstrap.resamples")
        self.refused(changed("bootstrap", "resamples", value=True), "bootstrap.resamples")
        self.refused(changed("bootstrap", "mean_block", value="0.5"), "bootstrap.mean_block")
        self.refused(changed("bootstrap", "min_days", value=0), "bootstrap.min_days")

    def test_decimal_fields_are_strings(self):
        self.refused(changed("sensitivity", "cost_scale", value=["0", 1, "1.5"]), "sensitivity.cost_scale")
        self.refused(changed("sensitivity", "fragile_cost_scale", value=1.5), "sensitivity.fragile_cost_scale")
        self.refused(changed("coverage", "ok_pct", value=99), "coverage.ok_pct")
        self.refused(changed("bootstrap", "ci", value=0.9), "bootstrap.ci")
        self.refused(changed("regimes", "vix_edges", value=[13, "16"]), "regimes.vix_edges")

    def test_edges_must_be_strictly_increasing(self):
        self.refused(changed("regimes", "vix_edges", value=["13", "13", "20"]), "regimes.vix_edges")
        self.refused(changed("regimes", "gap_edges_pct", value=["0.5", "0.1"]), "regimes.gap_edges_pct")
        self.refused(changed("regimes", "vix_edges", value=[]), "regimes.vix_edges")
        self.refused(changed("sensitivity", "cost_scale", value=["1", "0"]), "sensitivity.cost_scale")
        self.refused(changed("sensitivity", "extra_ticks", value=[0, 2, 1]), "sensitivity.extra_ticks")
        self.refused(changed("sensitivity", "latency_ms", value=[0, 0]), "sensitivity.latency_ms")

    def test_unknown_key_refused_naming_it(self):
        for path in ((), ("bootstrap",), ("coverage",), ("sweep",), ("regimes",), ("walk_forward",)):
            with self.subTest(path=path):
                doc = copy.deepcopy(SHIPPED)
                node = doc
                for key in path:
                    node = node[key]
                node["mystery_key"] = 1
                self.refused(doc, "mystery_key")

    def test_missing_key_refused(self):
        doc = copy.deepcopy(SHIPPED)
        del doc["pbo"]["slices"]
        self.refused(doc, "pbo.slices")

    def test_pbo_slices_must_be_even_and_at_least_4(self):
        for bad in (2, 3, 5, 0, -4, "16"):
            with self.subTest(bad=bad):
                self.refused(changed("pbo", "slices", value=bad), "pbo.slices")
        load(changed("pbo", "slices", value=4))
        self.refused(changed("pbo", "min_trials", value=1), "pbo.min_trials")

    def test_coverage_days_limits(self):
        self.refused(changed("coverage", "default_days", value=500), "coverage")
        self.refused(changed("coverage", "default_days", value=0), "coverage.default_days")
        self.refused(changed("coverage", "max_days", value=0), "coverage.max_days")
        for bad in ("0", "100.5", "-1", "abc"):
            with self.subTest(bad=bad):
                self.refused(changed("coverage", "ok_pct", value=bad), "coverage.ok_pct")
        load(changed("coverage", "ok_pct", value="100"))

    def test_sweep_max_workers_null_or_positive_int(self):
        load(changed("sweep", "max_workers", value=4))
        for bad in (0, -1, "4", 1.5):
            with self.subTest(bad=bad):
                self.refused(changed("sweep", "max_workers", value=bad), "sweep.max_workers")

    def test_walk_forward_and_regimes_basics(self):
        self.refused(changed("walk_forward", "objective", value="luck"), "walk_forward.objective")
        self.refused(changed("walk_forward", "train_days", value=0), "walk_forward.train_days")
        self.refused(changed("regimes", "thin_days", value=0), "regimes.thin_days")
        self.refused(changed("regimes", "vix_symbol", value=""), "regimes.vix_symbol")

    def test_property_random_edge_lists(self):
        rng = random.Random(20261112)
        for _ in range(300):
            ints = [rng.randint(-50, 50) for _ in range(rng.randint(1, 6))]
            edges = [str(x) for x in ints]
            doc = changed("regimes", "gap_edges_pct", value=edges)
            if all(a < b for a, b in zip(ints, ints[1:])):
                self.assertEqual(load(doc)["regimes"]["gap_edges_pct"], edges)
            else:
                self.refused(doc, "regimes.gap_edges_pct")

    def test_property_random_ci_values(self):
        rng = random.Random(7)
        for _ in range(200):
            x = rng.randint(-20, 120) / 100
            doc = changed("bootstrap", "ci", value=f"{x:.2f}")
            if 0 < x < 1:
                load(doc)
            else:
                self.refused(doc, "bootstrap.ci")


if __name__ == "__main__":
    unittest.main()

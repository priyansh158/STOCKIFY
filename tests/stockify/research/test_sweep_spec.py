"""M6.10 (WF 6.1): sweep.yaml, loaded, validated and expanded into run configs."""
from __future__ import annotations

import copy
import itertools
import json
import math
import tempfile
import unittest
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import paths, runconfig
from stockify.research import seeding, sweep

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "examples" / "sweeps" / "orb_grid.yaml"
BASE = json.loads((ROOT / "examples" / "runs" / "orb_reliance_minute.json").read_text(encoding="utf-8"))

HEAD = "schema: stockify.sweep/1\nname: g\nobjective: sharpe\n"


def spec_from(text: str, name: str = "sweep.yaml") -> sweep.SweepSpec:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / name
        p.write_text(text, encoding="utf-8")
        return sweep.load_spec(p)


def yaml_text(axes: str, search: str = "{kind: grid}", extra: str = "", name: str = "g") -> str:
    base = json.dumps(BASE)
    return (f"schema: stockify.sweep/1\nname: {name}\nbase: {base}\nobjective: sharpe\n"
            f"search: {search}\nworkers: null\n{extra}axes:\n{axes}")


AXES = "  strategy.params.range_minutes: [5, 10, 15]\n  strategy.params.exit_hhmm: ['14:30', '15:00']\n"


def key(p: sweep.GridPoint) -> str:
    return json.dumps(p.params, sort_keys=True)


class SweepSpecTest(unittest.TestCase):
    def test_grid_is_cartesian_in_axis_order(self):
        pts = sweep.expand(spec_from(yaml_text(AXES)))
        got = [(p.params["strategy.params.range_minutes"], p.params["strategy.params.exit_hhmm"]) for p in pts]
        self.assertEqual(got, [(r, e) for r in (5, 10, 15) for e in ("14:30", "15:00")])
        self.assertEqual([p.index for p in pts], list(range(6)))
        self.assertEqual(pts[3].config["strategy"]["params"]["range_minutes"], 10)
        self.assertEqual(pts[3].config["strategy"]["params"]["exit_hhmm"], "15:00")

    def test_random_search_is_seeded_and_without_replacement(self):
        text = yaml_text(AXES, "{kind: random, trials: 4, seed: 7}")
        a, b = sweep.expand(spec_from(text)), sweep.expand(spec_from(text))
        self.assertEqual([p.params for p in a], [p.params for p in b])
        self.assertEqual(len(a), 4)
        self.assertEqual(len({key(p) for p in a}), 4)
        other = sweep.expand(spec_from(yaml_text(AXES, "{kind: random, trials: 4, seed: 8}")))
        self.assertNotEqual([p.params for p in a], [p.params for p in other])
        grid = [key(p) for p in sweep.expand(spec_from(yaml_text(AXES)))]
        pos = [grid.index(key(p)) for p in a]
        self.assertEqual(pos, sorted(pos))               # returned in grid order
        self.assertEqual([p.index for p in a], [0, 1, 2, 3])

    def test_random_trials_at_or_above_grid_size_give_the_full_grid(self):
        full = sweep.expand(spec_from(yaml_text(AXES)))
        for n in (6, 50):
            got = sweep.expand(spec_from(yaml_text(AXES, "{kind: random, trials: %d, seed: 1}" % n)))
            self.assertEqual([p.params for p in got], [p.params for p in full])

    def test_unknown_key_in_sweep_yaml_refused_naming_it(self):
        with self.assertRaises(sweep.SweepSpecError) as cm:
            spec_from(yaml_text(AXES, extra="objectiv: sharpe\n"))
        self.assertIn("objectiv", str(cm.exception))
        self.assertIn("objective", str(cm.exception))   # the closest known key
        with self.assertRaises(sweep.SweepSpecError) as cm:
            spec_from(yaml_text(AXES, "{kind: grid, trials: 3}"))
        self.assertIn("trials", str(cm.exception))

    def test_family_id_key_is_refused_with_a_hint(self):
        with self.assertRaises(sweep.SweepSpecError) as cm:
            spec_from(yaml_text(AXES, extra="family_id: a/b\n"))
        self.assertIn("own family", str(cm.exception))

    def test_name_longer_than_40_refused(self):
        spec_from(yaml_text(AXES, name="a" * 40))
        for bad in ("a" * 41, "a b", "CON", "../x"):
            with self.subTest(bad=bad), self.assertRaises(sweep.SweepSpecError):
                spec_from(yaml_text(AXES, name=json.dumps(bad)))

    def test_set_path_typo_refused(self):
        with self.assertRaises(sweep.SweepSpecError) as cm:
            sweep.set_path(BASE, "strategy.parms.range_minutes", 5)
        self.assertIn("parms", str(cm.exception))
        with self.assertRaises(sweep.SweepSpecError):
            sweep.set_path(BASE, "capital_paisa", 5)
        with self.assertRaises(sweep.SweepSpecError):
            sweep.set_path(BASE, "seed.x", 5)            # through a scalar
        before = copy.deepcopy(BASE)
        out = sweep.set_path(BASE, "strategy.params.range_minutes", 5)
        self.assertEqual(BASE, before)                    # a deep copy
        self.assertEqual(out["strategy"]["params"]["range_minutes"], 5)
        self.assertEqual(sweep.set_path(BASE, "strategy.params.brand_new", 1)["strategy"]["params"]["brand_new"], 1)
        self.assertEqual(sweep.set_path(BASE, "seed", 9)["seed"], 9)

    def test_every_expanded_config_passes_runconfig_validate(self):
        for p in sweep.expand(spec_from(yaml_text(AXES))):
            self.assertEqual(runconfig.validate(copy.deepcopy(p.config)), p.config)
            self.assertIsNone(p.config["family_id"])   # the runner sets the family

    def test_a_trial_that_runconfig_refuses_is_refused_naming_the_axis(self):
        with self.assertRaises(sweep.SweepSpecError) as cm:
            spec_from(yaml_text("  capital_paise: [100, -5]\n"))
        self.assertIn("capital_paise", str(cm.exception))

    def test_point_names_are_safe_unique_segments(self):
        pts = sweep.expand(spec_from(yaml_text(AXES)))
        names = [p.config["name"] for p in pts]
        self.assertEqual(names[0], "g-t000")
        self.assertEqual(len(set(names)), len(names))
        for n in names:
            self.assertTrue(paths.safe_segment(n, 64))

    def test_base_path_is_relative_to_the_yaml_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "sweeps"
            (Path(tmp) / "runs").mkdir()
            sub.mkdir()
            (Path(tmp) / "runs" / "b.json").write_text(json.dumps(BASE), encoding="utf-8")
            (sub / "s.yaml").write_text(HEAD + "base: ../runs/b.json\nsearch: {kind: grid}\naxes:\n" + AXES,
                                        encoding="utf-8")
            spec = sweep.load_spec(sub / "s.yaml")
            self.assertEqual(spec.base["name"], BASE["name"])
            self.assertEqual(spec.source, sub / "s.yaml")
            (sub / "t.yaml").write_text(HEAD + "base: ../runs/missing.json\naxes:\n" + AXES, encoding="utf-8")
            with self.assertRaises(sweep.SweepSpecError):
                sweep.load_spec(sub / "t.yaml")

    def test_example_orb_grid_yaml_loads_and_expands_to_15(self):
        spec = sweep.load_spec(EXAMPLE)
        pts = sweep.expand(spec)
        self.assertEqual(len(pts), 15)
        self.assertEqual(spec.objective, "sharpe")
        for p in pts:
            self.assertIsNone(p.config["unlock_holdout"])
            for pack in p.config["data"]["packs"]:
                self.assertLess(pack["spec"]["to"], "2026-08-01")

    # -- sharp edges ---------------------------------------------------------------------------------------------
    def test_yaml_aliases_duplicate_keys_and_python_tags_are_refused(self):
        for text in (yaml_text(AXES) + "x: &a [1]\ny: *a\n",
                     yaml_text("  seed: [1]\n  seed: [2]\n"),
                     yaml_text("  seed: !!python/object/apply:os.getcwd []\n")):
            with self.subTest(text=text[-40:]), self.assertRaises(sweep.SweepSpecError):
                spec_from(text)

    def test_floats_empty_axes_and_duplicate_values_are_refused(self):
        for axes in ("  strategy.params.range_minutes: [1.5]\n", "  strategy.params.range_minutes: []\n",
                     "  strategy.params.range_minutes: [5, 5]\n", "  strategy.params.range_minutes: 5\n", "  {}\n"):
            with self.subTest(axes=axes), self.assertRaises(sweep.SweepSpecError):
                spec_from(yaml_text(axes))

    def test_a_grid_past_the_settings_cap_is_refused_before_it_is_built(self):
        cap = sweep.max_trials()
        axes = "  seed: [%s]\n  capital_paise: [1, 2]\n" % ", ".join(str(i) for i in range(cap + 1))
        with self.assertRaises(sweep.SweepSpecError) as cm:
            spec_from(yaml_text(axes))
        self.assertIn("max_trials", str(cm.exception))
        names = ("seed", "latency_ms", "capital_paise", "strategy.params.range_minutes", "strategy.params.qty",
                 "policies.max_slices")
        big = "".join(f"  {n}: [{', '.join(str(j + 1) for j in range(10))}]\n" for n in names)
        got = sweep.expand(spec_from(yaml_text(big, "{kind: random, trials: 5, seed: 3}")))
        self.assertEqual(len(got), 5)
        with self.assertRaises(sweep.SweepSpecError):
            spec_from(yaml_text(big, "{kind: random, trials: %d, seed: 3}" % (cap + 1)))

    def test_unquoted_date_in_an_axis_or_the_base_is_refused_telling_to_quote(self):
        with self.assertRaises(sweep.SweepSpecError) as cm:
            spec_from(yaml_text("  seed: [2026-06-01]\n"))
        self.assertIn("quote", str(cm.exception))
        self.assertIn("seed", str(cm.exception))
        text = yaml_text(AXES).replace(json.dumps(BASE), "{schema: stockify.run/1, name: x, when: 2026-06-01}")
        with self.assertRaises(sweep.SweepSpecError) as cm:
            spec_from(text)
        self.assertIn("quote", str(cm.exception))

    def test_unquoted_time_stays_the_string_the_user_wrote(self):
        spec = spec_from(yaml_text("  strategy.params.exit_hhmm: [14:30, '15:00']\n"))
        self.assertEqual(spec.axes["strategy.params.exit_hhmm"], ["14:30", "15:00"])
        self.assertEqual(spec_from(yaml_text("  seed: [0x10, 11, 12, 3]\n")).axes["seed"], [16, 11, 12, 3])

    def test_name_family_schema_and_unlock_holdout_cannot_be_axes(self):
        for path in ("name", "family_id", "schema", "unlock_holdout"):
            with self.subTest(path=path), self.assertRaises(sweep.SweepSpecError) as cm:
                spec_from(yaml_text(f"  {path}: ['a', 'b']\n"))
            self.assertIn(path, str(cm.exception))

    def test_deep_nesting_and_bad_utf8_are_spec_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "s.yaml"
            p.write_bytes(b"schema: \xff\xfe\n")
            with self.assertRaises(sweep.SweepSpecError):
                sweep.load_spec(p)
            p.write_text("a: " + "[" * 5000 + "]" * 5000 + "\n", encoding="utf-8")
            with self.assertRaises(sweep.SweepSpecError):
                sweep.load_spec(p)

    def test_workers_must_be_null_or_a_positive_int(self):
        self.assertIsNone(spec_from(yaml_text(AXES)).workers)
        with self.assertRaises(sweep.SweepSpecError):
            spec_from(yaml_text(AXES).replace("workers: null", "workers: 0"))
        self.assertEqual(spec_from(yaml_text(AXES).replace("workers: null", "workers: 3")).workers, 3)

    def test_objective_must_be_a_registry_objective(self):
        with self.assertRaises(sweep.SweepSpecError):
            spec_from(yaml_text(AXES).replace("objective: sharpe", "objective: luck"))


class SweepPropertiesTest(unittest.TestCase):
    """Seeded loops over random grids (stdlib property testing)."""

    def random_axes(self, rng) -> tuple[str, list[int]]:
        pool = ["strategy.params.exit_hhmm", "strategy.params.range_minutes", "strategy.params.qty", "seed", "latency_ms", "capital_paise"]
        chosen = list(dict.fromkeys(pool[rng.below(len(pool))] for _ in range(1 + rng.below(3))))
        sizes, lines = [], []
        for path in chosen:
            n = 1 + rng.below(4)
            vals = [1 + 10 * i + rng.below(9) for i in range(n)]      # distinct, positive
            if path.endswith("exit_hhmm"):
                vals = [f"1{3 + i}:{10 + rng.below(40)}" for i in range(n)]    # strings, quoted in the file
            sizes.append(n)
            lines.append(f"  {path}: {vals}\n")
        return "".join(lines), sizes

    def test_grid_count_is_the_product_all_validate_none_equal_and_order_is_stable(self):
        rng = seeding.stream(5, "random_search")
        for _ in range(40):
            axes, sizes = self.random_axes(rng)
            text = yaml_text(axes)
            pts = sweep.expand(spec_from(text))
            self.assertEqual(len(pts), math.prod(sizes))
            self.assertEqual(len({json.dumps(p.config, sort_keys=True) for p in pts}), len(pts))
            for p in pts:
                runconfig.validate(copy.deepcopy(p.config))
            self.assertEqual([p.config for p in pts], [p.config for p in sweep.expand(spec_from(text))])
            # Stable order: the last axis fastest, i.e. the points are sorted by their axis positions.
            spec = spec_from(text)
            pos = [tuple(spec.axes[a].index(p.params[a]) for a in spec.axes) for p in pts]
            self.assertEqual(pos, sorted(pos))
            self.assertEqual(pos, list(itertools.product(*(range(len(v)) for v in spec.axes.values()))))

    def test_random_search_same_seed_same_trials_and_a_subset_of_the_grid(self):
        rng = seeding.stream(6, "random_search")
        for _ in range(30):
            axes, sizes = self.random_axes(rng)
            n, seed = 1 + rng.below(8), rng.below(1000)
            text = yaml_text(axes, "{kind: random, trials: %d, seed: %d}" % (n, seed))
            a, b = sweep.expand(spec_from(text)), sweep.expand(spec_from(text))
            self.assertEqual([p.params for p in a], [p.params for p in b])
            self.assertEqual(len(a), min(n, math.prod(sizes)))
            grid = {key(p) for p in sweep.expand(spec_from(yaml_text(axes)))}
            keys = [key(p) for p in a]
            self.assertEqual(len(set(keys)), len(keys))
            self.assertLessEqual(set(keys), grid)


if __name__ == "__main__":
    unittest.main()

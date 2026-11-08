"""M3.15: the strategy how-to's examples stay valid (docs/plan/HOWTO_strategy.md)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import runconfig, strategy as S

REPO = Path(__file__).resolve().parents[2]
HOWTO = REPO / "docs" / "plan" / "HOWTO_strategy.md"


class HowtoTests(unittest.TestCase):
    def test_every_example_config_validates(self):
        found = sorted((REPO / "examples" / "runs").glob("*.json"))
        self.assertGreaterEqual(len(found), 4)
        for path in found:
            with self.subTest(config=path.name):
                doc = json.loads(path.read_text(encoding="utf-8"))
                if doc.get("schema") == "stockify.compare/1":     # a comparison of runs (M4.13): each one checked
                    from stockify.tools import compare_strategies
                    for run in compare_strategies.load_config(doc)["runs"]:
                        runconfig.load(run["config"])
                else:
                    runconfig.load(path)

    def test_the_cpp_example_is_the_python_one_with_another_strategy(self):
        py = json.loads((REPO / "examples" / "runs" / "orb_reliance_minute.json").read_text(encoding="utf-8"))
        cpp = json.loads((REPO / "examples" / "runs" / "orb_reliance_minute_cpp.json").read_text(encoding="utf-8"))
        self.assertEqual(cpp["strategy"], {"kind": "cpp", "ref": "orb_cpp", "params": py["strategy"]["params"]})
        rest = lambda c: {k: v for k, v in c.items() if k not in ("name", "strategy")}  # noqa: E731
        self.assertEqual(rest(cpp), rest(py))

    def test_the_files_it_names_exist_and_the_strategy_passes_the_load_check(self):
        text = HOWTO.read_text(encoding="utf-8")
        for rel in ("stockify/strategies/opening_range_breakout.py", "engine/strategies/orb_cpp.cpp",
                    "examples/runs/orb_reliance_minute.json", "examples/runs/orb_reliance_minute_cpp.json",
                    "tests/stockify/test_engine_strategies.py"):
            self.assertIn(rel, text)
            self.assertTrue((REPO / rel).is_file(), rel)
        self.assertIn("test_opening_range_breakout_and_orb_cpp", text)
        S.load("stockify.strategies.opening_range_breakout:OpeningRangeBreakout")

    def test_the_code_shown_is_the_reference_strategy(self):
        """The listing in section 1 is the module's class, comments aside, so the doc cannot drift from it."""
        text = HOWTO.read_text(encoding="utf-8")
        shown = text.split("```python\n", 1)[1].split("```", 1)[0]
        source = (REPO / "stockify" / "strategies" / "opening_range_breakout.py").read_text(encoding="utf-8")

        def code(s: str) -> list[str]:
            out = []
            for line in s.splitlines():
                line = line.split("  #")[0].rstrip()
                if line.strip() and not line.lstrip().startswith(("#", '"""', "from __future__")):
                    out.append(line.split("# noqa")[0].rstrip())
            return out
        body = source[source.index("from stockify import timeutil"):]
        self.assertEqual(code(shown), code(body))


if __name__ == "__main__":
    unittest.main()

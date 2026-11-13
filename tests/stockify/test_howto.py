"""M3.15: the strategy how-to's examples stay valid (docs/plan/HOWTO_strategy.md)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports

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

    def test_the_say_why_examples_pass_the_load_check_and_run(self):
        """Section 1's two examples (`note=` and `ctx.plot`) are checked, run on a small world, and show their effect."""
        import csv
        import tempfile
        from stockify import golden
        from stockify.oracle import run_bundle
        from tests.stockify.test_strategies import DAYS, daily
        text = HOWTO.read_text(encoding="utf-8")
        section = text.split("### Say why:", 1)[1].split("\n## ", 1)[0]
        blocks = [b.split("```", 1)[0] for b in section.split("```python\n")[1:]]
        self.assertEqual(len(blocks), 2)
        g = {"id": "T", "bundle": {"dates": DAYS[:4], "instruments": [
            {"id": 1, "kind": "EQ", "tradingsymbol": "TEST", "specs": [{"from": "2026-01-01", "lot": 1, "tick_paise": 5}]}]},
             "run": {"fill_model": {"kind": "bar"}, "capital_paise": 10_000_000, "price_basis": "raw"},
             "events": [daily(d, c) for d, c in zip(DAYS[:4], (10000, 10100, 10200, 10300))], "strategy": {"actions": []}}
        out = {}
        for block, params in zip(blocks, ({"symbol": "NSE:TEST", "below": 10100}, {"symbol": "NSE:TEST"})):
            self.assertEqual(S.check_source(block), [])
            namespace: dict = {}
            exec(compile(block, "<howto>", "exec"), namespace)                                  # noqa: S102
            cls = next(v for v in namespace.values() if isinstance(v, type) and issubclass(v, S.Strategy) and v is not S.Strategy)
            with tempfile.TemporaryDirectory(dir=str(support.SANDBOX_ROOT)) as tmp:
                pack = golden.build_pack(g, Path(tmp))
                b = golden.build_bundle(g)
                b["strategy"]["params"] = params
                run_bundle(b, [pack], Path(tmp) / "run", strategy=cls(), report=False)
                for name in ("orders.csv", "plots.csv"):
                    with (Path(tmp) / "run" / name).open(encoding="utf-8", newline="") as fh:
                        out[(cls.__name__, name)] = list(csv.DictReader(fh))
        self.assertEqual([o["note"] for o in out[("BuyBelow", "orders.csv")]], ["Bought 1: the close 100.00 fell under 101.00"])
        self.assertEqual([p["value"] for p in out[("DrawAverage", "plots.csv")]], ["10100", "10200"])
        self.assertEqual({p["name"] for p in out[("DrawAverage", "plots.csv")]}, {"sma3"})


if __name__ == "__main__":
    unittest.main()

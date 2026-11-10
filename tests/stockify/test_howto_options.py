"""ENG 4.x: the options how-to (docs/plan/HOWTO_options.md) stays true to the code it describes."""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import engine, golden, numerics, runconfig
from stockify.config import loader
from stockify.fno import calibrate as CAL

REPO = Path(__file__).resolve().parents[2]
HOWTO = REPO / "docs" / "plan" / "HOWTO_options.md"
BASICS = REPO / "docs" / "plan" / "HOWTO_strategy.md"


def text() -> str:
    return HOWTO.read_text(encoding="utf-8")


class HowtoOptionsTests(unittest.TestCase):
    def test_the_code_shown_is_the_strategys_own(self):
        source = (REPO / "stockify" / "strategies" / "_option_legs.py").read_text(encoding="utf-8")
        blocks = re.findall(r"```python\n(.*?)```", text(), flags=re.S)
        self.assertEqual(len(blocks), 2)
        for block in blocks:
            self.assertIn(block, source)

    def test_the_files_and_configs_it_names_exist_and_validate(self):
        for rel in re.findall(r"`((?:stockify|examples|docs|tests|engine)/[\w/.]+\.(?:py|json|md|csv))`", text()):
            self.assertTrue((REPO / rel).is_file(), rel)
        runconfig.load(REPO / "examples" / "runs" / "short_straddle_nifty_recorded.json")
        from stockify.tools import compare_strategies
        for name in ("eod_straddle_flat_vs_spanlite.json", "straddle_vs_ironfly_recorded.json"):
            doc = compare_strategies.load_config(REPO / "examples" / "runs" / name)
            for run in doc["runs"]:
                runconfig.load(run["config"])
        flat, span = (r["config"] for r in compare_strategies.load_config(
            REPO / "examples" / "runs" / "eod_straddle_flat_vs_spanlite.json")["runs"])
        self.assertEqual((flat["margin"], span["margin"]), ({"model": "flat"}, {"model": "spanlite"}))
        rest = lambda c: {k: v for k, v in c.items() if k not in ("name", "margin")}  # noqa: E731
        self.assertEqual(rest(flat), rest(span))                   # the margin model is the only difference
        self.assertIn("HOWTO_options.md", BASICS.read_text(encoding="utf-8"))

    def test_every_chain_view_key_is_explained(self):
        src = (REPO / "stockify" / "oracle" / "context.py").read_text(encoding="utf-8")
        body = src[src.index('item = {"instrument_id"'):src.index('"theta": None}') + 15]
        keys = set(re.findall(r'"(\w+)":', body))
        self.assertGreaterEqual(len(keys), 17)
        doc = text()
        for key in keys:
            self.assertIn(f"`{key}`", doc, key)
        for attr in ("forward_paise", "forward_source", "atm_strike_paise", "future", "synthetic",
                     "forward_from_index"):
            self.assertIn(f"`{attr}`", doc)
        for reason in ("BELOW_INTRINSIC", "ABOVE_FORWARD", "ABOVE_STRIKE", "BELOW_TICK", "ABOVE_MAX_VOL", "LOW_VEGA"):
            self.assertIn(reason, doc)

    def test_every_label_the_ui_explains_is_explained_here_or_in_the_basics(self):
        ui = (REPO / "web" / "src" / "app" / "tests" / "ui.tsx").read_text(encoding="utf-8")
        meaning = ui[ui.index("export function labelMeaning"):]
        meaning = meaning[:meaning.index("if (fixed[label])")]
        labels = re.findall(r'^\s*"?([\w:()]+)"?: "', meaning, flags=re.M)
        self.assertGreaterEqual(len(labels), 10)
        both = text() + BASICS.read_text(encoding="utf-8")
        for label in labels:
            self.assertIn(f"`{label}`", both, label)
        self.assertIn("`margin_spanlite:placeholder`", text())

    def test_the_config_numbers_quoted_are_the_configs(self):
        margin = json.loads((loader.CONFIG_DIR / "margin_params.json").read_text(encoding="utf-8"))
        index, stock = sorted((r for r in margin["spanlite"] if r["calibration_id"] == "placeholder"), key=lambda r: r["applies"]["underlying_class"])
        self.assertEqual((margin["flat"][-1]["index_pct"], margin["flat"][-1]["stock_pct"]), ("0.12", "0.20"))
        self.assertEqual((index["elm_pct"], stock["elm_pct"], index["default_iv"], stock["default_iv"]),
                         ("0.02", "0.035", "0.15", "0.30"))
        self.assertEqual((index["extreme_multiple"], index["extreme_weight"]), (3, "0.35"))
        charges = json.loads((loader.CONFIG_DIR / "charges.json").read_text(encoding="utf-8"))["components"]
        self.assertEqual(charges["exercise_brokerage"][-1]["flat_paise"], 2000)        # ₹20 (+ GST = ₹23.60)
        self.assertEqual([r["pct_of_intrinsic"] for r in charges["exercise_stt"]], ["0.00125", "0.0015"])
        doc = text()
        for s in ("12 %", "20 %", "3.5 %", "₹20", "₹23.60", "0.15 %", "0.125 %"):
            self.assertIn(s, doc)

    def test_adopting_a_pending_row_as_described_passes_the_config_check(self):
        tmp = Path(tempfile.mkdtemp(dir=str(support.SANDBOX_ROOT)))
        shutil.copytree(loader.CONFIG_DIR, tmp, dirs_exist_ok=True)
        path = tmp / "margin_params.json"
        row = {"applies": {"underlying": "NIFTY"}, "effective_from": "2026-11-11", "psr_pct": "0.08",
               "vsr_pct": "0.3", "extreme_multiple": 3, "extreme_weight": "0.35", "somc_per_lot_paise": 300000,
               "elm_pct": "0.02", "default_iv": "0.15", "calibration_id": "calibration-2026-11-11",
               "source": "calibration 2026-11-11", "samples": 200, "mean_relative_error": "0.0210",
               "verified": False, "pending_review": True}
        CAL.write_pending(row, path)
        doc = json.loads(path.read_text(encoding="utf-8"))
        moved = doc["spanlite_pending"].pop()                                       # step 2: cut …
        del moved["pending_review"]
        doc["spanlite"].append(moved)                                               # … and paste
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        loaded = loader.load_json("margin_params.json", tmp)                        # step 4: `stockify config`
        self.assertIn("calibration-2026-11-11", {r["calibration_id"] for r in loaded["spanlite"]})


def _world_with(rows: list[dict]):  # noqa: ANN202
    from tests.stockify.test_spanlite_parity import spanlite_bundle, spanlite_world
    g = spanlite_world(4902, True)
    b = spanlite_bundle(g)
    for r in rows:
        for f in ("psr_pct", "vsr_pct", "extreme_weight", "elm_pct", "default_iv"):
            r[f"{f}_ppb"] = numerics.to_ppb(r[f])
    b["margin_params"]["spanlite"] = b["margin_params"]["spanlite"] + rows
    return g, b


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class AdoptedRowTests(unittest.TestCase):
    """Section 6, step 3: a row naming the underlying wins over the class placeholder, from its date on."""

    def margin_daily(self, rows: list[dict]) -> list[str]:
        """The margin after each fill (the comparison tool's margin watch: the straddle is flat at day end)."""
        from stockify.strategies._margin_watch import LOG_PREFIX, MarginWatch
        g, b = _world_with(rows)
        b["strategy"]["params"] = {"inner_ref": "stockify.strategies.short_straddle_stop:ShortStraddleStop",
                                   "underlying": "NIFTY", "entry_hhmm": "09:30", "exit_hhmm": "11:00", "lots": 1}
        with tempfile.TemporaryDirectory(prefix="howto-adopt-") as tmp:
            pack = golden.build_pack(g, Path(tmp))
            engine.run_bundle(b, [pack], Path(tmp) / "run", strategy=MarginWatch(), report=False)
            log = (Path(tmp) / "run" / "events_log.txt").read_text(encoding="utf-8")
            return [x.split(LOG_PREFIX)[1] for x in log.splitlines() if LOG_PREFIX in x]

    def test_the_adopted_row_applies_from_its_date(self):
        def nifty(day: str) -> dict:
            return {"applies": {"underlying": "NIFTY"}, "effective_from": day, "psr_pct": "0.09", "vsr_pct": "0.25",
                    "extreme_multiple": 3, "extreme_weight": "0.35", "somc_per_lot_paise": 200_000,
                    "elm_pct": "0.02", "default_iv": "0.15", "calibration_id": "calibration-x"}
        placeholder = self.margin_daily([])
        self.assertTrue(any(int(x) > 0 for x in placeholder))
        self.assertNotEqual(self.margin_daily([nifty("2026-01-01")]), placeholder)   # in force: used
        self.assertEqual(self.margin_daily([nifty("2027-01-01")]), placeholder)      # not yet in force: ignored


if __name__ == "__main__":
    unittest.main()

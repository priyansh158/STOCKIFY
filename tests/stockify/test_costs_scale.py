"""`costs.scale` (FW 6.1, D-90): one run-config key multiplies every brokerage, tax and fee of the bundle's
`charges` section exactly, so the sensitivity suite can ask "what if costs were 0x, 1.5x or 2x?".

Leaf classes found in `stockify/config/charges.json` (components -> rows):
  * rate strings, each with a derived `<field>_ppb` integer sibling: pct, buy, sell, rate, pct_of_intrinsic,
    brokerage_pct, netted_brokerage_pct, stt_each_side_pct_of_value (loader.CHARGE_RATE_FIELDS). Scaled in
    Decimal, then the `_ppb` is re-derived from the new string (never scaled on its own).
  * integer paise amounts: flat_paise, per_order_paise, per_scrip_per_day_paise. Multiplied by the scale and rounded
    once, half-up, to whole paise.
  * text, flags and lists that are never money: effective_from, verified_on, observed_on, kind, source, side,
    product, segment, `on` (a list of component names), `applies` (a dict of the same), verified, includes_gst,
    plus_gst (booleans). Left alone.
Any other numeric leaf, and a `_ppb` with no rate string beside it, makes `scale_charges` raise.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import random
import tempfile
import unittest
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import bundle, engine, golden, numerics, runconfig
from stockify.config import loader
from stockify.oracle import run_bundle as oracle_run
from tests.stockify import test_bundle as tb


def real_charges() -> dict:
    return copy.deepcopy(loader.load_json("charges.json"))


def leaves(node, path=()):  # noqa: ANN001, ANN201
    if isinstance(node, dict):
        for k, v in node.items():
            yield from leaves(v, (*path, k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from leaves(v, (*path, i))
    else:
        yield path, node


def row_doc(**fields) -> dict:  # noqa: ANN003
    row = {"applies": {}, "effective_from": "2015-01-01", "verified": True, "source": "t", **fields}
    for f in loader.CHARGE_RATE_FIELDS:
        if isinstance(row.get(f), str):
            row[f"{f}_ppb"] = numerics.to_ppb(row[f])
    return {"components": {"x": [row]}}


def _row(doc: dict) -> dict:
    return doc["components"]["x"][0]


def golden_run(scale, *, impl="oracle", gid="G09") -> dict[str, bytes]:  # noqa: ANN001
    """The golden's scenario run with `costs.scale` = `scale` (None: no key); the artifacts it wrote."""
    g = golden.load(golden.GOLDEN_DIR / f"{gid}.yaml")
    if scale is not None:
        g["run"] = {**g.get("run", {}), "costs": {"scale": scale}}
    run = oracle_run if impl == "oracle" else engine.run_bundle
    with tempfile.TemporaryDirectory(prefix="costs-scale-") as tmp:
        folder = Path(tmp)
        pack = golden.build_pack(g, folder)
        b = golden.build_bundle(g)
        b["charges"] = bundle.apply_cost_scale(b["run"], b["charges"])
        run(b, [pack], folder / "run", report=False)
        return {p.name: p.read_bytes() for p in (folder / "run").iterdir() if p.is_file()}


def totals(files: dict[str, bytes]) -> list[int]:
    return [int(r["total"]) for r in csv.DictReader(io.StringIO(files["charges.csv"].decode("utf-8")))]


class ScaleKeyTests(unittest.TestCase):
    def cfg(self, **costs) -> dict:  # noqa: ANN003
        return runconfig.load(tb.run(costs={"allow_unverified": True, **costs}))

    def test_scale_one_left_out_of_the_normalised_config(self):
        base = self.cfg()
        for given in ("1", "1.0", "1.0000", "01"):
            with self.subTest(given=given):
                cfg = self.cfg(scale=given)
                self.assertNotIn("scale", cfg["costs"])
                self.assertEqual(cfg, base)
        self.assertEqual(self.cfg(scale="1.50")["costs"]["scale"], "1.5")
        self.assertEqual(self.cfg(scale="0.0")["costs"]["scale"], "0")
        self.assertEqual(self.cfg(scale="10.0000")["costs"]["scale"], "10")
        again = runconfig.load(self.cfg(scale="2"))          # a stored normalised config validates again
        self.assertEqual(again["costs"]["scale"], "2")

    def test_scale_refused_when_negative_above_ten_or_not_a_string(self):
        for bad in ("-0.5", "10.5", "10.0001", 1.5, 2, True, None, "1e0", "", " 1", "1 ", "NaN", "inf", "1.23456",
                    ".5", "1,5"):
            with self.subTest(bad=bad):
                with self.assertRaises(runconfig.RunConfigError) as cm:
                    self.cfg(scale=bad)
                self.assertIn("costs.scale", str(cm.exception))


class ScaleChargesTests(unittest.TestCase):
    def test_percentage_strings_scale_exactly(self):
        out = bundle.scale_charges(row_doc(pct="0.00297"), Decimal("1.5"))
        self.assertEqual((_row(out)["pct"], _row(out)["pct_ppb"]), ("0.004455", 4_455_000))

    def test_scale_one_and_a_half_rounds_each_paise_leaf_once_half_up(self):
        for before, after in ((2000, 3000), (1999, 2999), (1, 2), (3, 5), (0, 0)):
            with self.subTest(paise=before):
                out = bundle.scale_charges(row_doc(flat_paise=before), Decimal("1.5"))
                self.assertEqual(_row(out)["flat_paise"], after)     # 1999 x 1.5 = 2998.5 -> 2999, not 2998

    def test_scale_applies_to_flat_amounts_too(self):
        doc = {"components": {"x": [{"applies": {}, "effective_from": "2015-01-01", "kind": "flat", "flat_paise": 2000,
                                     "verified": True, "source": "t"}],
                              "auto_square_off_fee": [{"applies": {}, "effective_from": "2015-01-01",
                                                       "per_order_paise": 5000, "plus_gst": True}],
                              "dp": [{"applies": {}, "effective_from": "2015-01-01", "per_scrip_per_day_paise": 1500}]}}
        out = bundle.scale_charges(doc, Decimal("0.25"))
        comps = out["components"]
        self.assertEqual((comps["x"][0]["flat_paise"], comps["auto_square_off_fee"][0]["per_order_paise"],
                          comps["dp"][0]["per_scrip_per_day_paise"]), (500, 1250, 375))
        self.assertIs(comps["auto_square_off_fee"][0]["plus_gst"], True)

    def test_scale_two_doubles_every_charge_leaf_in_the_bundle(self):
        before = real_charges()
        after = bundle.scale_charges(before, Decimal("2"))
        self.assertEqual(before, real_charges())                       # the input is not touched
        old, new = dict(leaves(before)), dict(leaves(after))
        self.assertEqual(old.keys(), new.keys())
        doubled = 0
        for path, v in old.items():
            name = str(path[-1])
            if name in loader.CHARGE_RATE_FIELDS:
                self.assertEqual(Decimal(new[path]), Decimal(v) * 2, path)
                doubled += 1
            elif name.endswith("_ppb"):
                self.assertEqual(new[path], 2 * v, path)
                self.assertEqual(new[path], numerics.to_ppb(new[path[:-1] + (name[:-4],)]), path)
            elif name.endswith("_paise"):
                self.assertEqual(new[path], 2 * v, path)
                doubled += 1
            else:
                self.assertEqual(new[path], v, path)                    # dates, ids, flags, source text
        self.assertGreater(doubled, 20)

    def test_the_shipped_rates_scale_by_one_and_a_half_and_two(self):
        for scale in ("1.5", "2"):
            out = dict(leaves(bundle.scale_charges(real_charges(), Decimal(scale))))
            for path, v in out.items():
                if str(path[-1]) in loader.CHARGE_RATE_FIELDS:
                    self.assertEqual(numerics.to_ppb(v), out[path[:-1] + (f"{path[-1]}_ppb",)])

    def test_a_rate_that_ppb_cannot_hold_is_refused_not_rounded(self):
        doc = {"components": {"x": [{"applies": {}, "effective_from": "2015-01-01", "sell": "0.000000001",
                                     "sell_ppb": 1}]}}                         # one ppb; x 1.5 is half a ppb over
        with self.assertRaises(runconfig.RunConfigError) as cm:
            bundle.scale_charges(doc, Decimal("1.5"))
        text = str(cm.exception)
        self.assertIn("sell", text)
        self.assertIn("1.5", text)

    def test_unknown_charge_leaf_raises(self):
        for field, value in (("mystery", 7), ("ratio", 0.5), ("flat", 12)):
            with self.subTest(field=field):
                doc = row_doc()
                _row(doc)[field] = value
                with self.assertRaises(runconfig.RunConfigError) as cm:
                    bundle.scale_charges(doc, Decimal("2"))
                self.assertIn(field, str(cm.exception))
        doc = row_doc(sell="0.001")
        del _row(doc)["sell"]                                          # a _ppb with no string beside it
        with self.assertRaises(runconfig.RunConfigError) as cm:
            bundle.scale_charges(doc, Decimal("2"))
        self.assertIn("sell_ppb", str(cm.exception))

    def test_scale_one_returns_the_same_charges(self):
        self.assertEqual(bundle.scale_charges(real_charges(), Decimal("1")), real_charges())

    def test_scaling_is_exact_over_random_scales_and_rates(self):
        rng = random.Random(1306)
        refused = scaled = 0
        for _ in range(400):
            places = rng.randint(0, 4)
            scale = min(Decimal(rng.randint(0, 10 * 10 ** places)).scaleb(-places), Decimal(10))
            rate = format(Decimal(rng.randint(0, 10 ** 7)).scaleb(-rng.randint(0, 7)), "f")
            paise = rng.randint(0, 10 ** 7)
            doc = row_doc(sell=rate, flat_paise=paise)
            wide = Decimal(rate) * scale                                # exact: far below 28 digits
            try:
                out = bundle.scale_charges(doc, scale)
            except runconfig.RunConfigError:
                self.assertGreater(-wide.normalize().as_tuple().exponent, 9, (rate, scale))   # only for lost digits
                refused += 1
                continue
            scaled += 1
            row = _row(out)
            self.assertEqual(Decimal(row["sell"]), wide)
            self.assertEqual(row["sell_ppb"], numerics.to_ppb(row["sell"]))
            if scale != 0:
                self.assertEqual(Decimal(row["sell"]) / scale, Decimal(rate))            # round trip
            exact = Fraction(paise) * Fraction(scale)
            self.assertEqual(row["flat_paise"], (2 * exact.numerator + exact.denominator) // (2 * exact.denominator))
        self.assertGreater(scaled, 100)
        self.assertGreater(refused, 10)


class ScaledBundleTests(tb.BundleTest):
    def scaled(self, **costs) -> dict:  # noqa: ANN003
        return self.resolve(costs={"allow_unverified": True, **costs})

    def test_scale_one_is_byte_identical_to_no_key(self):
        plain = bundle.dumps(self.scaled())
        for given in ("1", "1.0"):
            self.assertEqual(bundle.dumps(self.scaled(scale=given)), plain)
        a, b = golden_run(None), golden_run("1")
        for name in ("determinism.json", "bundle.json", "charges.csv"):
            self.assertEqual(a[name], b[name], name)

    def test_scale_changes_the_bundle_hash(self):
        def digest(b: dict) -> str:
            return hashlib.sha256(bundle.dumps(b).encode()).hexdigest()
        base, half, double = digest(self.scaled()), digest(self.scaled(scale="0.5")), digest(self.scaled(scale="2"))
        self.assertEqual(len({base, half, double}), 3)
        self.assertEqual(digest(self.scaled(scale="2")), double)       # and it is deterministic

    def test_bundle_charges_are_the_scaled_dated_rows(self):
        base, two = self.scaled(), self.scaled(scale="2")
        self.assertEqual(two["charges"], bundle.scale_charges(base["charges"], Decimal(2)))
        self.assertEqual(two["run"]["costs"]["scale"], "2")

    def test_manifest_carries_the_label(self):
        self.assertNotIn("costs:scaled", self.scaled()["labels"])
        self.assertNotIn("costs:scaled", self.scaled(scale="1.0")["labels"])
        for given in ("0", "1.5"):
            self.assertIn("costs:scaled", self.scaled(scale=given)["labels"])
        from stockify.oracle import report
        self.assertIn("costs:scaled", report.LABEL_TEXT)


class ScaledRunTests(unittest.TestCase):
    def test_scale_zero_gives_zero_charges(self):
        self.assertGreater(sum(totals(golden_run(None))), 0)           # the scenario does cost something
        zero = totals(golden_run("0"))
        self.assertTrue(zero)
        self.assertEqual(set(zero), {0})

    @unittest.skipUnless(engine.available(), "the C++ engine is not built (.\\run.ps1 -BuildEngine)")
    def test_engine_and_oracle_agree_with_scaled_costs(self):
        for scale in ("1.5", "0", "2.5"):
            with self.subTest(scale=scale):
                a, b = golden_run(scale, impl="oracle"), golden_run(scale, impl="engine")
                for name in ("charges.csv", "fills.csv", "equity.csv", "positions_eod.csv"):
                    self.assertEqual(a[name], b[name], name)
        self.assertNotEqual(totals(golden_run("1.5")), totals(golden_run(None)))


if __name__ == "__main__":
    unittest.main()

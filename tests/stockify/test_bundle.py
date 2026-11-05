"""Run config and bundle resolver (M2.1)."""
from __future__ import annotations

import hashlib
import unittest
from datetime import date

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import bundle, paths, runconfig, timeutil
from stockify.data import history, instruments, packs, refdata

HEADER = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,"
          "tick_size,lot_size,instrument_type,segment,exchange")
SEC = 1_000_000_000


def run(**kw) -> dict:
    base = {"schema": "stockify.run/1", "name": "t",
            "strategy": {"kind": "python", "ref": "stockify.runconfig:RunConfigError"},
            "data": {"packs": [{"spec": {"dataset": "bars", "interval": "day", "instruments": ["NSE:RELIANCE"],
                                         "from": "2025-12-22", "to": "2025-12-31"}}]},
            "price_basis": "adjusted", "capital_paise": 10_000_000, "fill_model": {"kind": "bar"},
            "costs": {"allow_unverified": True}}
    base.update(kw)
    return base


class RunConfigTests(unittest.TestCase):
    def test_defaults_and_exact_conversions(self):
        cfg = runconfig.load(run(fill_model={"kind": "bar", "participation": "0.25",
                                             "slippage": {"kind": "bps", "x": "0.0005"}}))
        self.assertEqual((cfg["fill_model"]["participation_ppm"], cfg["fill_model"]["slippage"]["bp"]), (250_000, 5))
        self.assertEqual(cfg["policies"], {"preopen": "queue", "cas": "none", "freeze": "slice", "max_slices": 10,
                                           "rms_square_off": False})
        self.assertEqual((cfg["mark"], cfg["latency_ms"], cfg["seed"]), ("ltp", 0, 0))

    def test_errors_name_the_field(self):
        cases = [({"schema": "x"}, "schema"), ({"fill_model": {"kind": "magic"}}, "fill_model.kind"),
                 ({"fill_model": {"kind": "bar", "participation": "0.1234567"}}, "fill_model.participation"),
                 ({"data": {"packs": [{"spec": {}, "sha256": "a" * 64}]}}, "data.packs[0]"),
                 ({"strategy": {"kind": "python", "ref": "no_colon"}}, "strategy.ref"),
                 ({"capital_paise": 0}, "capital_paise"), ({"price_basis": "mixed"}, "price_basis"),
                 ({"policies": {"preopen": "maybe"}}, "policies.preopen"),
                 ({"fill_model": {"kind": "eod_close"}}, "price_basis")]
        for change, field in cases:
            with self.subTest(field=field), self.assertRaisesRegex(runconfig.RunConfigError, f"^{field.replace('[', '.').replace(']', '.')}"):
                runconfig.load(run(**change))


class BundleTest(unittest.TestCase):
    def setUp(self):
        support.fresh_cache_dir(self.id().rsplit(".", 1)[-1])
        for mod in (refdata, history):
            support.reset_sqlite_singleton(mod)
            self.addCleanup(support.reset_sqlite_singleton, mod)
        instruments.sync(date(2025, 12, 22), {
            "NSE": (f"{HEADER}\n256265,1,NIFTY 50,NIFTY 50,0,,0,0,0,EQ,INDICES,NSE\n"
                    f"738561,2,RELIANCE,RELIANCE,0,,0,0.1,1,EQ,NSE,NSE\n"),
            "NFO": f"{HEADER}\n9001,3,NIFTY25DECFUT,NIFTY,0,2025-12-30,0,0.1,75,FUT,NFO-FUT,NFO\n",
        }, underlyings={"NIFTY": "NSE:NIFTY 50"})
        self.rel = instruments.find("NSE", "RELIANCE")
        self.fut = instruments.find("NFO", "NIFTY25DECFUT")
        with history._Tx() as conn:
            for d in (22, 23, 24, 26, 29, 30, 31):
                ts = timeutil.ist_ns(date(2025, 12, d), "00:00")
                conn.execute("INSERT INTO bars VALUES (?, 'day', ?, 100, 110, 90, 105, 1000, NULL, '2026-01-01')",
                             (self.rel, ts))
                conn.execute("INSERT INTO bars VALUES (?, 'day', ?, 100, 110, 90, 105, 1000, 5, '2026-01-01')",
                             (self.fut, ts))

    def resolve(self, **kw) -> dict:
        return bundle.resolve_bundle(runconfig.load(run(**kw)))


class BundleTests(BundleTest):
    def test_days_phases_and_cutoffs(self):
        b = self.resolve()
        dates = [d["date"] for d in b["days"]]
        self.assertEqual(dates, ["2025-12-22", "2025-12-23", "2025-12-24", "2025-12-26", "2025-12-29",
                                 "2025-12-30", "2025-12-31"])                        # 25th: Christmas
        d22, d26 = b["days"][0], b["days"][3]
        self.assertEqual(d22["mis_cutoff_ns"]["CM"], timeutil.ist_ns(date(2025, 12, 22), "15:20"))
        self.assertEqual(d26["mis_cutoff_ns"]["CM"], timeutil.ist_ns(date(2025, 12, 26), "15:25"))   # Z-5 change
        self.assertEqual(d26["mis_cutoff_ns"]["FO"], timeutil.ist_ns(date(2025, 12, 26), "15:26"))
        cont = [p for p in d22["groups"]["CM"] if p["phase"] == "CONTINUOUS"][0]
        self.assertEqual(cont["start_ns"], timeutil.ist_ns(date(2025, 12, 22), "09:15"))
        self.assertEqual(d22["day_end_ns"], timeutil.ist_ns(date(2025, 12, 23), "00:00") - 1)

    def test_instruments_and_assumed_equity_spec(self):
        self.assertNotIn("specs:equity_tick_assumed", self.resolve()["labels"])      # spec in force from day one
        with refdata.transaction() as conn:                                           # first known spec mid-range
            conn.execute("UPDATE contract_specs SET effective_from='2025-12-24' WHERE instrument_id=?", (self.rel,))
        b = self.resolve()
        rel = {i["id"]: i for i in b["instruments"]}[self.rel]
        self.assertEqual((rel["session_group"], rel["specs"][0]["tick_paise"], rel["specs"][0]["assumed"]), ("CM", 1, True))
        self.assertEqual((rel["specs"][1]["from"], rel["specs"][1]["tick_paise"]), ("2025-12-24", 10))
        self.assertIn("specs:equity_tick_assumed", b["labels"])

    def test_derivative_fields(self):
        b = self.resolve(data={"packs": [{"spec": {"dataset": "bars", "interval": "day", "instruments": [{"ids": [self.fut]}],
                                                   "from": "2025-12-22", "to": "2025-12-31"}}]})
        fut = {i["id"]: i for i in b["instruments"]}[self.fut]
        self.assertEqual((fut["session_group"], fut["underlying_class"], fut["settlement"]), ("FO_FUT", "INDEX", "cash"))
        self.assertEqual(fut["last_trading_ns"], timeutil.ist_ns(date(2025, 12, 30), "15:30"))
        self.assertEqual(fut["specs"], [{"from": "2025-12-22", "lot": 75, "tick_paise": 10, "freeze_qty": None}])
        self.assertIn(instruments.find("NSE", "NIFTY 50"), {i["id"] for i in b["instruments"]})   # underlying included

    def test_derivative_without_spec_fails(self):
        with refdata.transaction() as conn:
            conn.execute("DELETE FROM contract_specs WHERE instrument_id=?", (self.fut,))
        with self.assertRaisesRegex(bundle.BundleError, "no contract spec"):
            self.resolve(data={"packs": [{"spec": {"dataset": "bars", "interval": "day", "instruments": [{"ids": [self.fut]}],
                                                   "from": "2025-12-22", "to": "2025-12-31"}}]})

    def test_unverified_charges_refused_or_labelled(self):
        with self.assertRaisesRegex(bundle.BundleError, "not verified"):
            self.resolve(costs={"allow_unverified": False})
        self.assertIn("charges:unverified_rows", self.resolve()["labels"])

    def test_charge_rows_in_range_only(self):
        rows = self.resolve()["charges"]["components"]["stt"]
        fut = [r for r in rows if r.get("applies", {}).get("kind") == ["FUT"]]
        self.assertEqual([r["effective_from"] for r in fut], ["2024-10-01"])           # 2026-04-01 row not yet
        self.assertEqual(fut[0]["sell_ppb"], 200_000)

    def test_pack_problems(self):
        sha = packs.build(run()["data"]["packs"][0]["spec"])
        with self.assertRaisesRegex(bundle.BundleError, "mix price bases"):
            self.resolve(data={"packs": [{"sha256": sha}, {"spec": {"dataset": "bhav_eod", "instruments": ["NSE:RELIANCE"],
                                                                    "from": "2025-12-22", "to": "2025-12-31"}}]})
        with self.assertRaisesRegex(bundle.BundleError, "price_basis"):
            self.resolve(data={"packs": [{"sha256": sha}]}, price_basis="raw")
        path = packs.pack_path(sha)
        path.write_bytes(path.read_bytes() + b"\0")
        with self.assertRaisesRegex(bundle.BundleError, "hash mismatch"):
            self.resolve(data={"packs": [{"sha256": sha}]})

    def test_strategy_source_hash(self):
        b = self.resolve()
        import stockify.runconfig as rc
        self.assertEqual(b["strategy"]["source_sha256"], hashlib.sha256(open(rc.__file__, "rb").read()).hexdigest())
        with self.assertRaisesRegex(bundle.BundleError, "strategy"):
            self.resolve(strategy={"kind": "python", "ref": "no.such.module:X"})

    def test_written_bundle_hash_is_stable(self):
        folder = paths.lab_dir() / "runs" / "x"
        folder.mkdir(parents=True)
        b = self.resolve()
        h1 = bundle.write(b, folder)
        self.assertEqual(bundle.write(self.resolve(), folder), h1)
        self.assertEqual(hashlib.sha256((folder / "bundle.json").read_bytes()).hexdigest(), h1)


if __name__ == "__main__":
    unittest.main()

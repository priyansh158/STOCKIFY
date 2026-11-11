"""Run config and bundle resolver (M2.1)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import unittest
from unittest import mock
from datetime import date

import tests.support as support  # noqa: F401  — must precede project imports

from stockify.strategies.buy_and_hold import BuyAndHold as strategy_buy_and_hold
from stockify import bundle, paths, runconfig, strategy, timeutil
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

    # Anything but 64 lower-case hex digits could become a path outside the packs folder (LCB 5.10).
    BAD_PACK_HASHES = (
        "../x" + "0" * 61, "../" + "0" * 61, "..\\" + "0" * 61, "/" + "0" * 63, "\\" + "0" * 63,
        "0" * 31 + "/" + "0" * 32, "0" * 31 + "\\" + "0" * 32, "0" * 31 + ".." + "0" * 31, "C:" + "0" * 62,
        "A" * 64, "0" * 63 + "F", "0" * 63, "0" * 65, "", "0" * 63 + "g", "0" * 63 + " ", "0" * 64 + "\n",
        "0" * 63 + "\n", "\uff10" * 64, "0" * 63 + "\u0660", "0" * 63 + "\x00", 0, None, ["0" * 64], b"0" * 64,
    )

    def test_a_pack_hash_is_64_lower_case_hex_digits_and_nothing_else(self):
        for bad in self.BAD_PACK_HASHES:
            with self.subTest(bad=bad), self.assertRaisesRegex(
                    runconfig.RunConfigError, r"^data\.packs\[0\]\.sha256: must be exactly 64 lower-case hex digits"):
                runconfig.load(run(data={"packs": [{"sha256": bad}]}))
        with self.assertRaisesRegex(runconfig.RunConfigError, r"^data\.packs\[1\]\.sha256"):
            runconfig.load(run(data={"packs": [{"sha256": "0" * 64}, {"sha256": "../" + "0" * 61}]}))
        for good in ("0" * 64, "0123456789abcdef" * 4):
            self.assertEqual(runconfig.load(run(data={"packs": [{"sha256": good}]}))["data"]["packs"], [{"sha256": good}])


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

    GAP_SPEC = {"dataset": "bars", "interval": "day", "from": "2025-12-22", "to": "2025-12-31"}

    def _gap_spec(self):
        return {**self.GAP_SPEC, "instruments": [{"ids": [self.rel, self.fut]}]}

    def _drop_future_bars(self):
        with history._Tx() as conn:
            for d in (24, 29):
                ts = timeutil.ist_ns(date(2025, 12, d), "00:00")
                conn.execute("DELETE FROM bars WHERE instrument_id=? AND ts_ns=?", (self.fut, ts))

    def test_a_pack_records_the_instruments_with_no_events_each_day(self):
        self._drop_future_bars()
        m = packs.manifest(packs.build(self._gap_spec()))
        self.assertEqual(m["gaps"], {"count": 2, "days": {"2025-12-24": [self.fut], "2025-12-29": [self.fut]}})
        self.assertEqual(m["volume_unit"], "units")
        # Nothing is filled in (I-7): the records are the rows there are.
        self.assertEqual(m["counts_by_kind"]["BAR"], 12)

    def test_a_pack_without_gaps_says_so(self):
        m = packs.manifest(packs.build(self._gap_spec()))
        self.assertEqual(m["gaps"], {"count": 0, "days": {}})

    def test_a_run_over_a_pack_with_gaps_is_labelled_and_the_report_lists_them(self):
        self._drop_future_bars()
        b = self.resolve(data={"packs": [{"spec": self._gap_spec()}]})
        self.assertIn("data:gaps=2", b["labels"])
        self.assertEqual(b["packs"][0]["gaps"], {"count": 2, "days": {"2025-12-24": [self.fut], "2025-12-29": [self.fut]}})
        b["strategy"]["params"] = {"symbol": "NSE:RELIANCE"}
        import tempfile
        from pathlib import Path
        from stockify.oracle import run_bundle
        folder = Path(tempfile.mkdtemp(dir=str(support.SANDBOX_ROOT)))
        run_bundle(b, [paths.packs_dir() / f"{b['packs'][0]['sha256']}.evt"], folder / "run",
                   strategy=strategy_buy_and_hold())
        text = (folder / "run" / "report.md").read_text(encoding="utf-8")
        self.assertIn("data:gaps=2", text)
        self.assertIn("2025-12-24", text)
        self.assertIn(f"{self.fut}", text)
        # No gaps: no label and no key in the bundle.
        clean = self.resolve()
        self.assertFalse([x for x in clean["labels"] if x.startswith("data:gaps")])
        self.assertNotIn("gaps", clean["packs"][0])

    def _stale_manifest(self, dataset: str) -> str:
        sha = packs.build(run()["data"]["packs"][0]["spec"])
        path = packs.manifest_path(sha)
        m = json.loads(path.read_text(encoding="utf-8"))
        del m["volume_unit"]
        m["spec"] = {**m["spec"], "dataset": dataset}
        path.write_text(json.dumps(m), encoding="utf-8")
        return sha

    def test_a_bhav_eod_pack_without_volume_unit_must_be_rebuilt(self):
        sha = self._stale_manifest("bhav_eod")
        with self.assertRaisesRegex(bundle.BundleError, r"D-79.*rebuild the pack"):
            self.resolve(data={"packs": [{"sha256": sha}]})

    def test_other_pack_kinds_without_volume_unit_still_load(self):
        sha = self._stale_manifest("bars")
        self.assertEqual(self.resolve(data={"packs": [{"sha256": sha}]})["packs"][0]["sha256"], sha)

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

    def test_a_config_that_skipped_validation_still_cannot_name_a_path(self):
        outside = paths.packs_dir().parent / ("0" * 61 + ".evt")
        outside.write_bytes(b"not a pack")
        self.addCleanup(outside.unlink)
        for bad in ("../" + "0" * 61, "..\\" + "0" * 61):
            with self.subTest(bad=bad), self.assertRaisesRegex(packs.PackError, "64 lower-case hex digits"):
                bundle.resolve_packs({"data": {"packs": [{"sha256": bad}]}})

    def test_editing_a_helper_module_changes_the_strategy_hash(self):
        real = bundle._file_sha

        def edited(path):  # as if _option_legs.py had been changed on disk
            return "edited" if path.name == "_option_legs.py" else real(path)

        for ref in ("short_straddle_stop:ShortStraddleStop", "iron_fly:IronFly"):
            cfg = {"strategy": {"kind": "python", "ref": f"stockify.strategies.{ref}", "params": {}}}
            with self.subTest(ref=ref):
                before = bundle.strategy_ref(cfg)["source_sha256"]
                with mock.patch.object(bundle, "_file_sha", edited):
                    after = bundle.strategy_ref(cfg)["source_sha256"]
                self.assertNotEqual(before, after)
        # A strategy that imports no helper is not affected by that helper's file.
        cfg = {"strategy": {"kind": "python", "ref": "stockify.strategies.buy_and_hold:BuyAndHold", "params": {}}}
        with mock.patch.object(bundle, "_file_sha", edited):
            self.assertEqual(bundle.strategy_ref(cfg)["source_sha256"], bundle.strategy_ref(cfg)["source_sha256"])
            self.assertNotIn("stockify.strategies._option_legs", strategy.module_closure("stockify.strategies.buy_and_hold"))

    def test_strategy_source_hash(self):
        b = self.resolve()
        import stockify.runconfig as rc
        # The hash covers the strategy file and every own-package helper it imports, by module name, in sorted order.
        files = [(name, importlib.util.find_spec(name).origin) for name in strategy.module_closure("stockify.runconfig")]
        h = hashlib.sha256()
        for name, origin in sorted(files):
            h.update(name.encode() + b":" + hashlib.sha256(open(origin, "rb").read()).hexdigest().encode() + b";")
        self.assertEqual(b["strategy"]["source_sha256"], h.hexdigest())
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

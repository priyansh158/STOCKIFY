"""CAM 4.3: the NSE SPAN files in force each day and the ELM % go into the bundle, without lookahead.

The archive is built in the sandbox from the trimmed real 2026-11-09 i1 file (created 2026-11-08 20:26 IST): copies
with a rewritten `<created>` stand in for the day's intraday and end-of-day files.
"""
from __future__ import annotations

import copy
import gzip
import hashlib
import io
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

from stockify import bundle, engine, golden, paths, runconfig, timeutil
from stockify.data import history, instruments, refdata

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "span" / "nsccl.20261109.i1.trim.spn"
DAY = date(2026, 11, 9)
PREV = date(2026, 11, 6)          # the trading day before (7-8 Nov: weekend)
HEADER = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,"
          "instrument_type,segment,exchange")


def archive_span(day: date, suffix: str, created: str) -> Path:
    """The fixture as `nsccl.<day>.<suffix>.zip` with header `<created>` = `created` (YYYYMMDDHHMM, IST)."""
    body = FIXTURE.read_bytes().replace(b"<created>202611082026</created>", f"<created>{created}</created>".encode(), 1)
    folder = paths.nse_margin_dir("span") / f"{day:%Y}"
    folder.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"nsccl.{day:%Y%m%d}.{suffix}.spn", body)
    path = folder / f"nsccl.{day:%Y%m%d}.{suffix}.zip"
    path.write_bytes(buf.getvalue())
    return path


def archive_elm(day: date, text: str) -> None:
    folder = paths.nse_margin_dir("elm") / f"{day:%Y}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"F_AEL_OPT_CONTRACTS_{day:%d%m%Y}.csv.gz").write_bytes(gzip.compress(text.encode()))


def ns(day: date, hhmm: str) -> int:
    return timeutil.ist_ns(day, hhmm)


def inst(kind: str, underlying: str, expiry: str, strike_paise: int | None = None) -> dict:
    return {"kind": kind, "underlying": underlying, "expiry": expiry, "strike_paise": strike_paise}


NIFTY_CE = inst("CE", "NIFTY", "2026-11-23", 2500000)
NIFTY_PE = inst("PE", "NIFTY", "2026-11-23", 2500000)
NIFTY_FUT = inst("FUT", "NIFTY", "2026-11-23")
INDEX = {"kind": "INDEX", "underlying": None, "expiry": None, "strike_paise": None}
ROWS = [INDEX, NIFTY_CE, NIFTY_PE, NIFTY_FUT]
DAYS = [{"date": DAY.isoformat()}]


class SpanSectionTests(SandboxedDBTest):
    db_modules = (refdata,)

    def test_span_day_uses_i1_then_intraday_never_s(self):
        # Review Focus 1: i1 (created 20:26 the evening before), i2 11:03, i3 12:32 and D's own .s at 16:55.
        files = {s: archive_span(DAY, s, c) for s, c in
                 (("s", "202611091655"), ("i3", "202611091232"), ("i1", "202611082026"), ("i2", "202611091103"))}
        day = bundle.span_section(ROWS, DAYS)[DAY.isoformat()]
        self.assertEqual([f["created_ns"] for f in day],
                         [ns(date(2026, 11, 8), "20:26"), ns(DAY, "11:03"), ns(DAY, "12:32")])
        self.assertEqual([f["file"] for f in day], [f"span/2026/nsccl.20261109.{s}.zip" for s in ("i1", "i2", "i3")])
        self.assertEqual([f["sha256"] for f in day],
                         [hashlib.sha256(files[s].read_bytes()).hexdigest() for s in ("i1", "i2", "i3")])
        self.assertNotIn("nsccl.20261109.s.zip", str(day))

    def test_without_i1_the_previous_eod_opens_the_day(self):
        archive_span(PREV, "s", "202611061655")
        archive_span(PREV, "i4", "202611061400")             # the previous day's intraday files are never used
        archive_span(DAY, "i2", "202611091103")
        archive_span(DAY, "s", "202611091655")
        day = bundle.span_section(ROWS, DAYS)[DAY.isoformat()]
        self.assertEqual([f["file"] for f in day], ["span/2026/nsccl.20261106.s.zip", "span/2026/nsccl.20261109.i2.zip"])
        self.assertEqual([f["created_ns"] for f in day], [ns(PREV, "16:55"), ns(DAY, "11:03")])

    def test_no_day_files_falls_back_to_previous_eod(self):
        archive_span(PREV, "s", "202611061655")
        day = bundle.span_section(ROWS, DAYS)[DAY.isoformat()]
        self.assertEqual([(f["file"], f["created_ns"]) for f in day],
                         [("span/2026/nsccl.20261106.s.zip", ns(PREV, "16:55"))])

    def test_nothing_archived_gives_an_empty_day(self):
        self.assertEqual(bundle.span_section(ROWS, DAYS), {DAY.isoformat(): []})

    def test_only_bundle_instruments_included(self):
        archive_span(DAY, "i1", "202611082026")
        f = bundle.span_section([NIFTY_CE, NIFTY_FUT, inst("CE", "NIFTY", "2030-01-01", 100)], DAYS)[DAY.isoformat()][0]
        self.assertEqual(set(f["commodities"]), {"NIFTY"})                       # BANKNIFTY is in the file, not here
        cc = f["commodities"]["NIFTY"]
        self.assertEqual([(c["kind"], c["expiry"], c["strike_paise"]) for c in cc["contracts"]],
                         [("CE", "2026-11-23", 2500000), ("FUT", "2026-11-23", None)])   # absent contract: left out
        ce = cc["contracts"][0]
        self.assertEqual(len(ce["risk_array"]), 16)
        self.assertTrue(all(isinstance(v, str) for v in ce["risk_array"] + [ce["price"], ce["delta"]]))
        # every parameter block of the commodity, whatever the bundle trades
        self.assertEqual([(s["type"], s["expiry"]) for s in cc["series"]],
                         [("FUT", "2026-11-23"), ("FUT", "2026-12-29"), ("OOP", "2026-11-17"), ("OOP", "2026-11-23")])
        self.assertTrue(all(isinstance(s["price_scan"], str) for s in cc["series"]))
        self.assertEqual((cc["spot"], cc["som_method"], cc["cvf"]["OOP"]), ("24986.400000", "GROSS", "1.00"))
        self.assertEqual(len(cc["calendar_spreads"]), 3)
        self.assertTrue(cc["scan_tiers"] and cc["som_tiers"])
        self.assertEqual([p["point"] for p in f["scan_points"]], list(range(1, 17)))
        self.assertIsInstance(f["scan_points"][0]["price_mult"], str)

    def test_elm_section_from_archived_file(self):
        archive_elm(DAY, "OPTIDX,NIFTY,23-NOV-2026,25000,CE,0,4.5\n"
                         "OPTIDX,NIFTY,23-NOV-2026,25000,PE,0,2.00\n"
                         "OPTIDX,NIFTY,23-NOV-2026,26000,CE,0,2\n"            # not in the bundle
                         "OPTIDX,BANKNIFTY,23-NOV-2026,25000,CE,0,3\n"        # same contract, other underlying
                         "FUTIDX,NIFTY,23-NOV-2026,-1,XX,0,2\n")
        bnf = inst("CE", "BANKNIFTY", "2026-11-23", 2500000)
        days = DAYS + [{"date": "2026-11-11"}]                                  # no ELM file archived
        self.assertEqual(bundle.elm_section(ROWS + [bnf], days), {DAY.isoformat(): {
            "NIFTY": {"CE|2026-11-23|2500000": "4.5", "PE|2026-11-23|2500000": "2.00", "FUT|2026-11-23|": "2"},
            "BANKNIFTY": {"CE|2026-11-23|2500000": "3"}}})


class RunConfigModelTests(unittest.TestCase):
    def run_cfg(self, **kw) -> dict:
        base = {"schema": "stockify.run/1", "name": "t", "strategy": {"kind": "script", "actions": []},
                "data": {"packs": [{"sha256": "0" * 64}]}, "price_basis": "raw", "capital_paise": 1,
                "fill_model": {"kind": "bar"}}
        return runconfig.load({**base, **kw})

    def test_default_model_is_flat_until_engine_supports_staircase(self):
        self.assertEqual(runconfig.DEFAULTS["margin"]["model"], "flat")
        self.assertEqual(self.run_cfg()["margin"]["model"], "flat")
        for model in ("flat", "spanlite", "span_exact", "staircase"):
            self.assertEqual(self.run_cfg(margin={"model": model})["margin"]["model"], model)
        with self.assertRaisesRegex(runconfig.RunConfigError, "^margin.model"):
            self.run_cfg(margin={"model": "span"})


def g01() -> dict:
    return copy.deepcopy(next(g for _, g in golden.goldens(["G01"])))


class GoldenPinTests(unittest.TestCase):
    def test_goldens_pin_flat(self):
        g = g01()
        before = golden.build_bundle(g)
        self.assertEqual(before["run"]["margin"]["model"], "flat")
        self.assertNotIn("span", before)
        with mock.patch.dict(runconfig.DEFAULTS, {"margin": {"model": "staircase"}}):
            self.assertEqual(golden.build_bundle(g), before)                     # a staircase default changes nothing
        g["run"] = {**g.get("run", {}), "margin": {"model": "spanlite"}}
        self.assertEqual(golden.build_bundle(g)["run"]["margin"]["model"], "spanlite")   # a named model is kept

    def refused(self, impl: str, model: str) -> list[str]:
        g = g01()
        g["run"] = {**g.get("run", {}), "margin": {"model": model}}
        g["expect"] = {"error": f"margin model {model!r} is not supported yet"}
        return golden.run_one(g, impl)

    def test_oracle_refuses_span_models_until_they_exist(self):
        for model in ("span_exact", "staircase"):
            with self.subTest(model=model):
                self.assertEqual(self.refused("oracle", model), [])

    @unittest.skipUnless(engine.available(), "stockify._engine is not built")
    def test_engine_refuses_span_models_until_they_exist(self):
        for model in ("span_exact", "staircase"):
            with self.subTest(model=model):
                self.assertEqual(self.refused("engine", model), [])


class ResolveBundleSpanTests(SandboxedDBTest):
    db_modules = (refdata, history)

    def setUp(self):
        super().setUp()
        instruments.sync(DAY, {
            "NSE": f"{HEADER}\n256265,1,NIFTY 50,NIFTY 50,0,,0,0,0,EQ,INDICES,NSE\n",
            "NFO": f"{HEADER}\n9001,3,NIFTY26NOVFUT,NIFTY,0,2026-11-23,0,0.1,65,FUT,NFO-FUT,NFO\n",
        }, underlyings={"NIFTY": "NSE:NIFTY 50"})
        self.fut = instruments.find("NFO", "NIFTY26NOVFUT")
        with history._Tx() as conn:
            conn.execute("INSERT INTO bars VALUES (?, 'day', ?, 100, 110, 90, 105, 1000, 5, '2026-11-10')",
                         (self.fut, ns(DAY, "00:00")))
        archive_span(DAY, "i1", "202611082026")
        archive_span(DAY, "s", "202611091655")
        archive_elm(DAY, "FUTIDX,NIFTY,23-NOV-2026,-1,XX,0,2\n")

    def resolve(self, model: str) -> dict:
        return bundle.resolve_bundle(runconfig.load({
            "schema": "stockify.run/1", "name": "t", "strategy": {"kind": "script", "actions": []},
            "data": {"packs": [{"spec": {"dataset": "bars", "interval": "day", "instruments": [{"ids": [self.fut]}],
                                         "from": "2026-11-09", "to": "2026-11-09"}}]},
            "price_basis": "adjusted", "capital_paise": 10_000_000, "fill_model": {"kind": "bar"},
            "costs": {"allow_unverified": True}, "margin": {"model": model}}))

    def test_span_models_resolve_the_sections_and_others_do_not(self):
        for model in ("span_exact", "staircase"):
            with self.subTest(model=model):
                b = self.resolve(model)
                self.assertEqual([f["file"] for f in b["span"]["2026-11-09"]], ["span/2026/nsccl.20261109.i1.zip"])
                self.assertEqual(b["span"]["2026-11-09"][0]["commodities"]["NIFTY"]["contracts"][0]["expiry"],
                                 "2026-11-23")
                self.assertEqual(b["elm"], {"2026-11-09": {"NIFTY": {"FUT|2026-11-23|": "2"}}})
        for model in ("flat", "spanlite"):
            with self.subTest(model=model):
                b = self.resolve(model)
                self.assertNotIn("span", b)
                self.assertNotIn("elm", b)


if __name__ == "__main__":
    unittest.main()

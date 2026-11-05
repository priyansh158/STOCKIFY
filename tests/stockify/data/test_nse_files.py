"""NSE end-of-day files (M1.3): parsers, fetcher, loading, settlement prices."""
from __future__ import annotations

import io
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import paths, timeutil
from stockify.daemon.clock import ManualClock
from stockify.data import instruments, nse_files, nse_parsers as P, quality, refdata

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "nse"
OLD = date(2024, 7, 5)
EXP = date(2026, 11, 17)
HEADER = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,"
          "tick_size,lot_size,instrument_type,segment,exchange")


def fixture(name: str) -> bytes:
    return (FIX / name).read_bytes()


def zipped(name: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, data)
    return buf.getvalue()


def udiff_fo(day: date, rows: list[dict]) -> bytes:
    """A UDiFF F&O file in the real column order (S-1)."""
    lines = [",".join(P.UDIFF_COLUMNS)]
    for r in rows:
        cells = dict.fromkeys(P.UDIFF_COLUMNS, "")
        cells.update(TradDt=day.isoformat(), BizDt=day.isoformat(), Sgmt="FO", Src="NSE", SsnId="F1",
                     XpryDt=r["exp"], FininstrmActlXpryDt=r["exp"], FinInstrmTp=r["tp"], TckrSymb=r["sym"],
                     StrkPric=r.get("strike", ""), OptnTp=r.get("opt", ""), OpnPric="1.00", HghPric="1.00",
                     LwPric="1.00", ClsPric=r.get("close", "1.00"), LastPric="1.00", PrvsClsgPric="1.00",
                     UndrlygPric="1.00", SttlmPric=r.get("settle", "1.00"), OpnIntrst="100", ChngInOpnIntrst="0",
                     TtlTradgVol="2", TtlTrfVal="200.00", TtlNbOfTxsExctd="1", NewBrdLotQty=str(r["lot"]))
        lines.append(",".join(cells[c] for c in P.UDIFF_COLUMNS))
    return ("\n".join(lines) + "\n").encode()


def udiff_cm(day: date, rows: list[tuple[str, str, str]]) -> bytes:
    lines = [",".join(P.UDIFF_COLUMNS)]
    for sym, series, close in rows:
        cells = dict.fromkeys(P.UDIFF_COLUMNS, "")
        cells.update(TradDt=day.isoformat(), BizDt=day.isoformat(), Sgmt="CM", Src="NSE", FinInstrmTp="STK",
                     TckrSymb=sym, SctySrs=series, OpnPric=close, HghPric=close, LwPric=close, ClsPric=close,
                     LastPric=close, PrvsClsgPric=close, TtlTradgVol="10", TtlTrfVal="100.00",
                     TtlNbOfTxsExctd="3", SsnId="F1", NewBrdLotQty="1")
        lines.append(",".join(cells[c] for c in P.UDIFF_COLUMNS))
    return ("\n".join(lines) + "\n").encode()


def ind_close(day: date, rows: list[tuple[str, str]]) -> bytes:
    lines = [",".join(P.IDX_COLUMNS)]
    lines += [f"{name},{day:%d-%m-%Y},{close},{close},{close},{close},0,0,-,-,-,-,-" for name, close in rows]
    return ("\n".join(lines) + "\n").encode()


class FakeResponse:
    def __init__(self, status: int, content: bytes):
        self.status_code, self.content = status, content


class FakeSession:
    """Answers by URL suffix; each answer list is consumed in order, the last repeats."""

    def __init__(self, routes: dict[str, list] | None = None, clock: ManualClock | None = None):
        self.headers: dict = {}
        self.routes = routes or {}
        self.calls: list[tuple[str, int]] = []
        self.clock = clock

    def get(self, url, timeout=None):  # noqa: ANN001
        self.calls.append((url, self.clock.mono_ns() if self.clock else 0))
        for suffix, answers in self.routes.items():
            if url.endswith(suffix):
                answer = answers.pop(0) if len(answers) > 1 else answers[0]
                if isinstance(answer, Exception):
                    raise answer
                return FakeResponse(*answer)
        return FakeResponse(404, b"<html>not found</html>")


class NseTest(unittest.TestCase):
    def setUp(self):
        support.fresh_cache_dir(self.id().rsplit(".", 1)[-1])
        support.reset_sqlite_singleton(refdata)
        self.addCleanup(support.reset_sqlite_singleton, refdata)
        self.clock = ManualClock(timeutil.ist_ns(EXP, "20:00"))

    def http(self, routes: dict) -> tuple[nse_files.Http, FakeSession]:
        session = FakeSession(routes, self.clock)
        return nse_files.Http(session=session, clock=self.clock), session

    def kite_refdata(self):
        instruments.sync(date(2026, 11, 16), {
            "NSE": (f"{HEADER}\n256265,1,NIFTY 50,NIFTY 50,0,,0,0,0,EQ,INDICES,NSE\n"
                    f"738561,2,RELIANCE,RELIANCE INDUSTRIES,0,,0,0.1,1,EQ,NSE,NSE\n"
                    f"408065,3,INFY,INFOSYS,0,,0,0.1,1,EQ,NSE,NSE\n"),
            "NFO": (f"{HEADER}\n9001,11,NIFTY26NOVFUT,NIFTY,0,2026-11-17,0,0.1,65,FUT,NFO-FUT,NFO\n"
                    f"9002,12,NIFTY2611724000CE,NIFTY,0,2026-11-17,24000,0.05,65,CE,NFO-OPT,NFO\n"
                    f"9003,13,RELIANCE26NOVFUT,RELIANCE,0,2026-11-17,0,0.1,500,FUT,NFO-FUT,NFO\n"),
        }, underlyings={"NIFTY": "NSE:NIFTY 50"})


# ── Parsers ──────────────────────────────────────────────────────────────────

class ParserTests(unittest.TestCase):
    def test_udiff_fields_by_header_name(self):
        rows = P.fo_udiff(fixture("fo_udiff_20240705.csv"), OLD)
        fut = next(r for r in rows if r["kind"] == "FUT")
        self.assertEqual((fut["underlying"], fut["expiry"], fut["strike_paise"], fut["index_derivative"]),
                         ("BANKNIFTY", "2024-07-31", None, True))
        self.assertEqual((fut["close_paise"], fut["settle_paise"], fut["oi"], fut["volume"]),
                         (5272410, 5272410, 2607810, 177911))
        call = next(r for r in rows if r["kind"] == "CE" and r["strike_paise"] == 62000)
        self.assertEqual((call["lot_size"], call["turnover_paise"], call["settle_paise"]), (1000, 72900000, 10560))

    def test_legacy_has_no_lot_size_and_reads_lakhs(self):
        rows = P.fo_legacy(fixture("fo_legacy_20240705.csv"), OLD)
        self.assertTrue(all(r["lot_size"] is None for r in rows))
        call = next(r for r in rows if r["kind"] == "CE" and r["strike_paise"] == 62000)
        self.assertEqual(call["turnover_paise"], 72900000)               # 7.29 lakh

    def test_legacy_and_udiff_same_day_identical_schema(self):
        """U-03: the two formats for 2024-07-05 give the same rows."""
        def by_key(rows):
            return {(r["underlying"], r["kind"], r["expiry"], r["strike_paise"]): r for r in rows}
        legacy = by_key(P.fo_legacy(fixture("fo_legacy_20240705.csv"), OLD))
        udiff = by_key(P.fo_udiff(fixture("fo_udiff_20240705.csv"), OLD))
        self.assertEqual(set(legacy), set(udiff))
        self.assertEqual(len(legacy), 4)
        for k in legacy:
            self.assertEqual(set(legacy[k]), set(P.FO_KEYS))
            self.assertEqual(set(udiff[k]), set(P.FO_KEYS))
            for f in P.FO_KEYS:
                if f in ("last_paise", "prev_close_paise", "underlying_paise", "lot_size"):
                    self.assertIsNone(legacy[k][f])                     # legacy does not carry these
                elif f == "turnover_paise":                             # lakhs to 2 decimals = ₹1,000 steps
                    self.assertLess(abs(legacy[k][f] - udiff[k][f]), 100_000)
                else:
                    self.assertEqual(legacy[k][f], udiff[k][f], (k, f))
        cm_l = {r["symbol"]: r for r in P.cm_legacy(fixture("cm_legacy_20240705.csv"), OLD)}
        cm_u = {r["symbol"]: r for r in P.cm_udiff(fixture("cm_udiff_20240705.csv"), OLD)}
        self.assertEqual(cm_l, cm_u)

    def test_unknown_or_missing_column_raises(self):
        text = fixture("fo_udiff_20240705.csv").decode()
        with self.assertRaisesRegex(P.ParseError, "unknown \\['NewCol'\\]"):
            P.fo_udiff(text.replace("Rsvd4", "NewCol").encode(), OLD)
        with self.assertRaisesRegex(P.ParseError, "missing \\['Rsvd4'\\]"):
            P.fo_udiff(text.replace(",Rsvd4", "").encode(), OLD)

    def test_wrong_day_and_bad_numbers_raise(self):
        with self.assertRaisesRegex(P.ParseError, "expected 2024-07-08"):
            P.fo_udiff(fixture("fo_udiff_20240705.csv"), date(2024, 7, 8))
        bad = fixture("fo_udiff_20240705.csv").replace(b"54.45", b"54.455")
        with self.assertRaisesRegex(P.ParseError, "whole number of paise"):
            P.fo_udiff(bad, OLD)

    def test_zip_is_read(self):
        raw = zipped("BhavCopy.csv", fixture("fo_udiff_20240705.csv"))
        self.assertEqual(P.fo_udiff(raw, OLD), P.fo_udiff(fixture("fo_udiff_20240705.csv"), OLD))

    def test_index_closes_and_vix_rounding(self):
        rows = {r["name"]: r for r in P.ind_close_all(fixture("ind_close_all_20261104.csv"), date(2026, 11, 4))}
        self.assertEqual(rows["Nifty 50"]["close_paise"], 2396060)
        self.assertEqual(rows["India VIX"]["open_paise"], 1572)           # 15.7225 -> 15.72
        self.assertEqual(rows["India VIX"]["high_paise"], 1615)           # 16.145 -> 16.15 (half up)

    def test_qtyfreeze_padded_columns(self):
        rows = {r["symbol"]: r["freeze_qty"] for r in P.qtyfreeze(fixture("qtyfreeze_20261104.csv"), date(2026, 11, 4))}
        self.assertEqual((rows["NIFTY"], rows["BANKNIFTY"], rows["RELIANCE"]), (1800, 900, 20000))


# ── Sources ──────────────────────────────────────────────────────────────────

class SourceTests(unittest.TestCase):
    def test_format_by_date(self):
        self.assertEqual(nse_files.source_for("fo", date(2023, 12, 29)).parser, "fo_legacy")
        self.assertEqual(nse_files.source_for("fo", date(2024, 1, 1)).parser, "fo_udiff")
        self.assertEqual(nse_files.source_for("cm", date(2026, 11, 4)).parser, "cm_udiff")
        self.assertIsNone(nse_files.source_for("fo", date(2014, 12, 31)))

    def test_urls(self):
        src = nse_files.source_for("fo", date(2023, 12, 29))
        self.assertTrue(nse_files.url_for(src, date(2023, 12, 29)).endswith(
            "/content/historical/DERIVATIVES/2023/DEC/fo29DEC2023bhav.csv.zip"))
        src = nse_files.source_for("idx", date(2026, 11, 4))
        self.assertEqual(nse_files.url_for(src, date(2026, 11, 4)),
                         "https://nsearchives.nseindia.com/content/indices/ind_close_all_04112026.csv")
        src = nse_files.source_for("fo", date(2026, 11, 4))
        self.assertTrue(nse_files.url_for(src, date(2026, 11, 4)).endswith("BhavCopy_NSE_FO_0_0_0_20261104_F_0000.csv.zip"))


# ── Fetcher ──────────────────────────────────────────────────────────────────

class FetchTests(NseTest):
    def test_throttle_headers_and_404(self):
        http, session = self.http({})
        res = nse_files.fetch_one("idx", EXP, http)
        self.assertEqual(res.outcome, "missing")
        self.assertEqual(session.headers["Referer"], "https://www.nseindia.com/")
        self.assertIn("Mozilla", session.headers["User-Agent"])
        nse_files.fetch_one("cm", EXP, http)
        times = [t for _, t in session.calls]
        self.assertEqual(len(times), 3)                                   # warm-up + two files
        self.assertTrue(all(b - a >= 1_000_000_000 for a, b in zip(times, times[1:])))
        row = nse_files.logged("idx", EXP)
        self.assertEqual((row["outcome"], row["http_status"], row["parsed_ok"]), ("missing", 404, None))

    def test_retries_with_backoff_then_raw_kept(self):
        body = ind_close(EXP, [("Nifty 50", "24000.00")])
        http, session = self.http({"ind_close_all_17112026.csv": [(503, b""), ConnectionError("reset"), (200, body)]})
        res = nse_files.fetch_one("idx", EXP, http)
        self.assertEqual(res.outcome, "ok")
        t = [t for _, t in session.calls[1:]]
        self.assertGreaterEqual(t[1] - t[0], 2_000_000_000)              # backoff 2 s, then 4 s
        self.assertGreaterEqual(t[2] - t[1], 4_000_000_000)
        raw = paths.nse_raw_dir("idx") / "20261117.csv"
        self.assertEqual(raw.read_bytes(), body)
        self.assertEqual(raw.with_name("20261117.csv.sha256").read_text().split()[0], res.sha256)

    def test_resumable_and_raw_immutable(self):
        first = ind_close(EXP, [("Nifty 50", "24000.00")])
        http, session = self.http({"ind_close_all_17112026.csv": [(200, first)]})
        nse_files.fetch_one("idx", EXP, http)
        n = len(session.calls)
        self.assertEqual(nse_files.fetch_one("idx", EXP, http).outcome, "skipped")
        self.assertEqual(len(session.calls), n)                           # no request at all
        second = ind_close(EXP, [("Nifty 50", "24001.00")])
        http, _ = self.http({"ind_close_all_17112026.csv": [(200, second)]})
        res = nse_files.fetch_one("idx", EXP, http, force=True)
        self.assertEqual(res.raw_file, "20261117_r2.csv")
        self.assertEqual((paths.nse_raw_dir("idx") / "20261117.csv").read_bytes(), first)

    def test_parse_failure_is_logged_and_raw_kept(self):
        http, _ = self.http({"ind_close_all_17112026.csv": [(200, b"<html>maintenance</html>")]})
        res = nse_files.fetch_one("idx", EXP, http)
        self.assertEqual((res.outcome, res.parsed_ok), ("ok", False))
        self.assertEqual(nse_files.logged("idx", EXP)["parsed_ok"], 0)
        self.assertTrue((paths.nse_raw_dir("idx") / "20261117.csv").exists())
        self.assertFalse(nse_files.complete([res]))

    def test_unverified_source_refused(self):
        cfg = nse_files.config()
        for row in cfg["datasets"]["idx"]:
            row["verified"] = False
        http, _ = self.http({})
        with self.assertRaisesRegex(nse_files.NseError, "not verified"):
            nse_files.fetch_one("idx", EXP, http, cfg=cfg)
        self.assertEqual(nse_files.fetch_one("idx", EXP, http, cfg=cfg, allow_unverified=True).outcome, "missing")


# ── Loading and settlement ───────────────────────────────────────────────────

FO_ROWS = [                                   # on expiry day every expiring contract settles at the FSP
    {"tp": "IDF", "sym": "NIFTY", "exp": "2026-11-17", "settle": "24010.50", "lot": 65},
    {"tp": "IDO", "sym": "NIFTY", "exp": "2026-11-17", "strike": "24000.00", "opt": "CE", "lot": 75, "settle": "24010.50"},
    {"tp": "IDO", "sym": "NIFTY", "exp": "2026-11-17", "strike": "25000.00", "opt": "CE", "lot": 75, "settle": "24010.50"},
    {"tp": "IDO", "sym": "NIFTY", "exp": "2026-11-17", "strike": "23000.00", "opt": "PE", "lot": 75, "settle": "24010.50"},
    {"tp": "STF", "sym": "RELIANCE", "exp": "2026-11-17", "settle": "1500.15", "lot": 500},
    {"tp": "STO", "sym": "INFY", "exp": "2026-11-17", "strike": "1600.00", "opt": "CE", "lot": 400, "settle": "1600.00"},
    {"tp": "STO", "sym": "TCS", "exp": "2026-11-17", "strike": "3000.00", "opt": "CE", "lot": 175, "settle": "3001.00"},
]

class LoadTests(NseTest):
    def setUp(self):
        super().setUp()
        self.kite_refdata()
        self.routes = {
            "BhavCopy_NSE_FO_0_0_0_20261117_F_0000.csv.zip": [(200, zipped("fo.csv", udiff_fo(EXP, FO_ROWS)))],
            "BhavCopy_NSE_CM_0_0_0_20261117_F_0000.csv.zip": [(200, zipped("cm.csv", udiff_cm(EXP, [
                ("RELIANCE", "EQ", "1500.00"), ("RELIANCE", "BE", "1499.00"), ("INFY", "EQ", "1600.00"),
                ("NEWCO", "EQ", "10.00")])))],
            "ind_close_all_17112026.csv": [(200, ind_close(EXP, [("Nifty 50", "24010.50"), ("India VIX", "15.1925")]))],
            "qtyfreeze.csv": [(200, fixture("qtyfreeze_20261104.csv"))],
        }

    def fetch(self):
        http, _ = self.http(self.routes)
        return nse_files.fetch(EXP, http=http, today=EXP)

    def test_fo_rows_map_to_instruments(self):
        results = {r.dataset: r for r in self.fetch()}
        self.assertTrue(nse_files.complete(list(results.values())), [r.detail for r in results.values()])
        fut = instruments.derivative("NIFTY", "FUT", "2026-11-17")
        self.assertEqual(fut, instruments.find("NFO", "NIFTY26NOVFUT"))  # existing contract kept its id
        put = instruments.derivative("NIFTY", "PE", "2026-11-17", 2_300_000)
        info = instruments.get(put)
        self.assertEqual((info["tradingsymbol"], info["source"], info["first_seen"]), (None, "bhavcopy", "2026-11-17"))
        self.assertEqual(info["underlying_id"], instruments.find("NSE", "NIFTY 50"))
        conn = refdata._connect()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM fo_eod WHERE trade_date='2026-11-17'").fetchone()[0], 7)
        self.assertEqual(conn.execute("SELECT settle_paise FROM fo_eod WHERE instrument_id=?", (fut,)).fetchone()[0],
                         2401050)

    def test_lot_size_changes_become_specs(self):
        self.fetch()
        call = instruments.derivative("NIFTY", "CE", "2026-11-17", 2_400_000)
        self.assertEqual(instruments.spec_at(call, "2026-11-16")["lot_size"], 65)
        spec = instruments.spec_at(call, "2026-11-17")
        self.assertEqual((spec["lot_size"], spec["tick_paise"], spec["source"]), (75, 5, "bhavcopy"))
        fut = instruments.derivative("NIFTY", "FUT", "2026-11-17")
        self.assertEqual(instruments.spec_at(fut, "2026-11-17")["source"], "kite_dump")      # unchanged: no row
        new_call = instruments.derivative("NIFTY", "CE", "2026-11-17", 2_500_000)
        self.assertEqual(instruments.spec_at(new_call, "2026-11-17")["tick_paise"], 5)       # tick from a sibling
        put = instruments.derivative("NIFTY", "PE", "2026-11-17", 2_300_000)
        self.assertIsNone(instruments.spec_at(put, "2026-11-17"))                            # no PE tick known

    def test_cm_and_index(self):
        results = {r.dataset: r for r in self.fetch()}
        conn = refdata._connect()
        rel = instruments.find("NSE", "RELIANCE")
        self.assertEqual([tuple(r) for r in conn.execute(
            "SELECT series, close_paise FROM cm_eod WHERE instrument_id=?", (rel,))], [("EQ", 150000)])
        newco = instruments.get(instruments.find("NSE", "NEWCO"))
        self.assertEqual((newco["kind"], newco["source"]), ("EQ", "bhavcopy"))
        nifty = instruments.find("NSE", "NIFTY 50")
        self.assertEqual(conn.execute("SELECT close_paise FROM index_eod WHERE instrument_id=?", (nifty,)).fetchone()[0],
                         2401050)
        self.assertIn("1 index names not in refdata", results["idx"].detail)

    def test_settlement_prices_and_cross_check(self):
        self.fetch()
        conn = refdata._connect()
        rows = {r["underlying_id"]: dict(r) for r in conn.execute("SELECT * FROM settlement_prices")}
        nifty, rel, infy = (instruments.find("NSE", s) for s in ("NIFTY 50", "RELIANCE", "INFY"))
        self.assertEqual((rows[nifty]["fsp_paise"], rows[nifty]["source"], rows[nifty]["cross_check"]),
                         (2401050, "fo_eod", "ok"))                      # index close agrees
        # NSE settled RELIANCE at 1500.15; the official close says 1500.00 (closing auction): FSP stays NSE's.
        self.assertEqual((rows[rel]["fsp_paise"], rows[rel]["cross_check"]), (150015, "mismatch"))
        self.assertEqual((rows[infy]["fsp_paise"], rows[infy]["cross_check"]), (160000, "ok"))   # options only
        dq = conn.execute("SELECT value, detail FROM data_quality WHERE instrument_id=? AND metric=?",
                          (rel, nse_files.FSP_METRIC)).fetchone()
        self.assertEqual(dq[0], 15)
        self.assertIn('"close_source": "cm_eod"', dq[1])
        tcs = instruments.find("NSE", "TCS")
        self.assertIsNone(tcs)                                            # no CM row, no Kite row: unknown
        self.assertNotIn(None, rows)

    def test_inconsistent_settle_prices_are_reported(self):
        rows = [dict(r) for r in FO_ROWS]
        rows[2]["settle"] = "24011.00"                                    # one NIFTY call disagrees
        self.routes["BhavCopy_NSE_FO_0_0_0_20261117_F_0000.csv.zip"] = [(200, zipped("fo.csv", udiff_fo(EXP, rows)))]
        self.fetch()
        nifty = instruments.find("NSE", "NIFTY 50")
        conn = refdata._connect()
        self.assertIsNone(conn.execute("SELECT * FROM settlement_prices WHERE underlying_id=?", (nifty,)).fetchone())
        self.assertEqual(conn.execute("SELECT value FROM data_quality WHERE instrument_id=? AND metric=?",
                                      (nifty, nse_files.FSP_SPREAD_METRIC)).fetchone()[0], 50)

    def test_settle_without_close_is_na(self):
        del self.routes["ind_close_all_17112026.csv"]
        self.fetch()
        nifty = instruments.find("NSE", "NIFTY 50")
        row = refdata._connect().execute("SELECT fsp_paise, cross_check FROM settlement_prices WHERE underlying_id=?",
                                         (nifty,)).fetchone()
        self.assertEqual(tuple(row), (2401050, "n/a"))

    def test_quality_report_keeps_fsp_rows(self):
        self.fetch()
        with refdata.transaction() as conn:                               # what quality.report() deletes
            conn.execute("DELETE FROM data_quality WHERE trade_date=? AND metric NOT IN (%s)"
                         % ",".join("?" * len(quality.FOREIGN_METRICS)), ("2026-11-17", *quality.FOREIGN_METRICS))
        self.assertEqual(refdata._connect().execute(
            "SELECT COUNT(*) FROM data_quality WHERE metric=?", (nse_files.FSP_METRIC,)).fetchone()[0], 1)
        self.assertIn(nse_files.FSP_METRIC, quality.FOREIGN_METRICS)

    def test_freeze_limits_only_on_change(self):
        self.fetch()
        conn = refdata._connect()
        self.assertEqual(conn.execute("SELECT freeze_qty, effective_from, source FROM freeze_limits "
                                      "WHERE underlying='NIFTY'").fetchall()[0][:], (1800, "2026-11-17", "qtyfreeze.csv"))
        later = date(2026, 11, 18)
        changed = fixture("qtyfreeze_20261104.csv").replace(b"NIFTY     ,1800", b"NIFTY     ,3510")
        http, _ = self.http({"qtyfreeze.csv": [(200, changed)]})
        res = nse_files.fetch_one("qtyfreeze", EXP, http, today=later)
        self.assertEqual(res.trade_date, "2026-11-18")                    # current-only: stored under fetch day
        rows = conn.execute("SELECT underlying, effective_from, freeze_qty FROM freeze_limits "
                            "WHERE effective_from='2026-11-18'").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("NIFTY", "2026-11-18", 3510)])

    def test_fo_universe_view(self):
        self.fetch()
        rows = refdata._connect().execute("SELECT underlying FROM fo_universe WHERE trade_date='2026-11-17' "
                                          "ORDER BY underlying").fetchall()
        self.assertEqual([r[0] for r in rows], ["RELIANCE"])              # indices excluded; INFY has no future

    def test_reload_replaces_day(self):
        self.fetch()
        http, _ = self.http(self.routes)
        nse_files.fetch(EXP, http=http, force=True, today=EXP)
        self.assertEqual(refdata._connect().execute("SELECT COUNT(*) FROM fo_eod").fetchone()[0], 7)


class BackfillTests(NseTest):
    def test_backfill_is_resumable_and_skips_qtyfreeze(self):
        routes = {"ind_close_all_16112026.csv": [(200, ind_close(date(2026, 11, 16), [("Nifty 50", "1.00")]))],
                  "ind_close_all_17112026.csv": [(200, ind_close(EXP, [("Nifty 50", "2.00")]))]}
        http, session = self.http(routes)
        totals = nse_files.backfill(date(2026, 11, 14), EXP, ("idx", "qtyfreeze"), http=http)
        self.assertEqual(totals, {"ok": 2})                               # 14th and 15th are a weekend
        self.assertFalse(any("qtyfreeze" in url for url, _ in session.calls))
        self.assertEqual(nse_files.backfill(date(2026, 11, 14), EXP, ("idx",), http=http), {"skipped": 2})


class DaemonJobTests(NseTest):
    def setUp(self):
        super().setUp()
        from stockify.daemon import scheduler
        self.state = scheduler.State()

    def run_job(self, results, now_ist: str):
        from stockify import daemon
        with patch.object(nse_files, "fetch", return_value=results), \
                patch.object(timeutil, "now_ns", return_value=timeutil.ist_ns(EXP, now_ist)):
            daemon._run_job("nse_fetch", EXP, self.state)

    def test_complete_marks_done(self):
        self.run_job([nse_files.FetchResult("fo", "2026-11-17", "ok", parsed_ok=True)], "20:00")
        self.assertTrue(self.state.done("nse_fetch", EXP))

    def test_missing_retries_until_window_ends(self):
        missing = [nse_files.FetchResult("fo", "2026-11-17", "missing")]
        self.run_job(missing, "20:00")
        self.assertFalse(self.state.done("nse_fetch", EXP))
        self.run_job(missing, "23:30")
        self.assertTrue(self.state.done("nse_fetch", EXP))

    def test_parse_failure_never_given_up(self):
        bad = [nse_files.FetchResult("fo", "2026-11-17", "ok", parsed_ok=False)]
        self.run_job(bad, "23:30")
        self.assertFalse(self.state.done("nse_fetch", EXP))


if __name__ == "__main__":
    unittest.main()

"""CH 6.9: display bars read back from a pack or journal, with the runner's own bucketing (A4 §2.2).

`stockify.oracle.bars` holds the two pure helpers the runner's `_build_bars` calls; `stockify.reporting.chartbars`
rebuilds the bars a run saw from the pack's records. The proof against the runner is the strategy's own `on_bar`
deliveries on a tick world, on both runners. The property tests are seeded stdlib loops (no `hypothesis`).
"""
from __future__ import annotations

import hashlib
import random
import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine, golden
from stockify.data import packs
from stockify.oracle import run_bundle as oracle_run
from stockify.strategy import Strategy

NS = 1_000_000_000
D1, D2 = "2026-03-02", "2026-03-03"

# sha256 of every file of the oracle run folder of `_tick_world()`, captured BEFORE the runner was changed to call
# the shared helpers (CH 6.9: no behaviour change). manifest.json is left out: it differs between two identical runs.
GOLDEN_HASHES: dict[str, str] = {
    "bundle.json": "4807b9797d5547537c6fdf0015c7e95c21bbcb07fe562985e7aa6e1c77a9a554",
    "charges.csv": "f36833dfe8a7a5e2c2d60dcac8884b0dd149c5e4a7cc57150d4c8c507b1b0878",
    "determinism.json": "313dbe0481f542d517a03c42eabb22beb8e1ace87a1fc76105d4d63f13bc6264",
    "equity.csv": "ebd3a5cab66aa7396b426d00078363624294294d9e49dcfa7d1f85ab00e94b96",
    "events_log.txt": "9f518b820c143177b6980b91a0916f4387c96ad9f4dc39cbacf90ea696d1ac34",
    "fills.csv": "a067431535f5caeb2431b64dc4cea6b984b2f7dd883e9b1c4f5dde4695cb9c3e",
    "margin_daily.csv": "3231eebbffe07bb38909ffabd7412a451b5063472ce0ccd2fb12ca0519807e7c",
    "metrics.json": "dde529c1d652834d7c507c2be472491312d608ce5066c788c2b4d3a2f939d335",
    "order_events.csv": "20609a692f16302e7fa1183a8f9b2f7041f69d6b1dc95cbe162e3696b8d50131",
    "orders.csv": "624a34e27ed39d91f149c284315c59108a54c9aca836e6ddf68b2d741ba923a8",
    "plots.csv": "e36b9d1a705298478e4166fa4383d6b4e44b7fbb278b17c4a19fd8bfd5947632",
    "positions_eod.csv": "3eddba2c22d9b1ce1889a582a4760c709709f2f16fa3294db691db1cfe4b9247",
    "run.json": "5aa59c15b508d056d2ce491d47adffa7270c3e8911d8c0037f97f158ba46c017",
}


def _ts(d: str, hms: str) -> int:
    return golden.ts(f"{d}T{hms}+05:30")


def _tick_world(seed: int = 7) -> dict:
    """Two days, an equity (id 1) with quote and full ticks and an index (id 2); a pre-open tick, a volume
    decrease, quiet minutes, a GAP in the middle of day 1."""
    rng = random.Random(seed)
    events = []
    for d in (D1, D2):
        vol, ltp = 0, 10000
        t = _ts(d, "09:05:00")                       # before the continuous session: never bucketed
        while t < _ts(d, "15:29:30"):
            ltp = max(5, ltp + rng.randint(-20, 20) * 5)
            vol = max(0, vol + rng.randint(-30, 400))          # a decrease contributes 0
            k = rng.choice(("TICK_QUOTE", "TICK_FULL"))
            ev = {"kind": k, "instrument": 1, "t": t, "ltp": ltp, "volume": vol, "high": ltp + 10, "low": ltp - 10}
            if k == "TICK_FULL":
                ev["bids"], ev["asks"] = [[ltp - 5, 10, 1]], [[ltp + 5, 10, 1]]
            events.append(ev)
            if rng.random() < 0.5:
                events.append({"kind": "INDEX", "instrument": 2, "t": t + 1000, "value": 2450000 + rng.randint(-500, 500)})
            t += rng.choice((7, 23, 41, 95, 130, 400)) * NS      # some gaps leave whole minutes empty
        if d == D1:
            events.append({"kind": "GAP", "instrument": 0, "t": _ts(d, "11:00:00"), "phase": "start"})
            events.append({"kind": "GAP", "instrument": 0, "t": _ts(d, "11:00:10"), "phase": "end"})
    spec = [{"from": "2026-01-01", "lot": 1, "tick_paise": 5}]
    return {"id": "CB9", "bundle": {"dates": [D1, D2], "instruments": [
                {"id": 1, "kind": "EQ", "tradingsymbol": "TEST", "specs": spec},
                {"id": 2, "kind": "INDEX", "tradingsymbol": "NIFTY 50"}]},
            "run": {"fill_model": {"kind": "snapshot_ltp"}, "capital_paise": 10_000_000, "price_basis": "raw"},
            "events": events, "strategy": {"actions": []}}


class _Watcher(Strategy):
    """Subscribes bars at 60 s and 300 s on both instruments and keeps what on_bar delivered."""

    def __init__(self):
        self.seen: dict[tuple[int, int], list[tuple]] = {}

    def on_start(self, ctx):  # noqa: ANN001
        for iid in (1, 2):
            for isec in (60, 300):
                ctx.subscribe_bars(iid, isec)

    def on_bar(self, ctx, bar):  # noqa: ANN001
        self.seen.setdefault((bar.instrument, bar.interval_s), []).append(
            (bar.start_ns, bar.open, bar.high, bar.low, bar.close, bar.volume))
        ctx.log("info", f"bar {bar.instrument} {bar.interval_s} {bar.start_ns} {bar.open} {bar.high} {bar.low} "
                        f"{bar.close} {bar.volume}")          # puts every built bar into events_log.txt


def _hashes(folder: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir()) if p.is_file() and p.name != "manifest.json"}


def _run(runner, world: dict, tmp: Path):  # noqa: ANN001, ANN202
    pack, bundle = golden.build_pack(world, tmp), golden.build_bundle(world)
    w = _Watcher()
    runner(bundle, [pack], tmp / "run", strategy=w, report=False)
    return pack, bundle, w


class BucketingHelperTests(unittest.TestCase):
    def test_bucket_start_hand_cases(self):
        from stockify.oracle.bars import bucket_start
        a = _ts(D1, "09:15:00")
        self.assertEqual(bucket_start(a, a, 60), a)                                    # on the anchor
        self.assertEqual(bucket_start(a, _ts(D1, "09:15:59"), 60), a)
        self.assertEqual(bucket_start(a, _ts(D1, "09:16:00"), 60), a + 60 * NS)        # on a boundary
        self.assertEqual(bucket_start(a, _ts(D1, "09:19:59"), 300), a)
        self.assertEqual(bucket_start(a, _ts(D1, "09:20:00"), 300), a + 300 * NS)
        self.assertEqual(bucket_start(a, _ts(D1, "09:37:12") + 5, 300), _ts(D1, "09:35:00"))
        self.assertEqual(bucket_start(a, a + 3_600 * NS - 1, 3600), a)

    def test_volume_delta_hand_cases(self):
        from stockify.oracle.bars import volume_delta
        self.assertEqual(volume_delta(500, None), 500)       # the first snapshot brings its whole day volume
        self.assertEqual(volume_delta(0, None), 0)
        self.assertEqual(volume_delta(700, 500), 200)
        self.assertEqual(volume_delta(500, 500), 0)
        self.assertEqual(volume_delta(400, 500), 0)          # a decrease contributes 0
        self.assertEqual(volume_delta(None, None), 0)        # an index has no volume
        self.assertEqual(volume_delta(None, 500), 0)

    def test_property_helpers_equal_the_runners_expressions(self):
        from stockify.oracle.bars import bucket_start, volume_delta
        rng = random.Random(9_001)
        for _ in range(5000):
            anchor = rng.randint(1, 2_000_000_000) * NS + rng.randint(0, NS - 1)
            t = anchor + rng.randint(0, 30_000) * NS + rng.randint(0, NS - 1)
            isec = rng.choice((1, 5, 60, 180, 300, 900, 3600, 86400))
            step = isec * NS
            got = bucket_start(anchor, t, isec)
            self.assertEqual(got, anchor + (t - anchor) // step * step)                 # the runner's expression
            self.assertTrue(anchor <= got <= t < got + step)
            self.assertEqual((got - anchor) % step, 0)
            self.assertEqual(bucket_start(anchor, got, isec), got)
            vol = rng.choice((None, rng.randint(0, 10**9)))
            prev = rng.choice((None, rng.randint(0, 10**9)))
            runner_dv = vol if prev is None else max(0, vol - prev) if vol is not None else 0
            self.assertEqual(volume_delta(vol, prev), runner_dv or 0)
            self.assertGreaterEqual(volume_delta(vol, prev), 0)


class ChartBarsTests(unittest.TestCase):
    def test_runner_refactor_changes_no_bytes(self):
        self.assertTrue(GOLDEN_HASHES, "the pre-refactor hashes were not captured")
        with tempfile.TemporaryDirectory(prefix="cb9-") as tmp:
            _run(oracle_run, _tick_world(), Path(tmp))
            self.assertEqual(_hashes(Path(tmp) / "run"), GOLDEN_HASHES)

    def _check_against_runner(self, runner) -> None:  # noqa: ANN001
        from stockify.reporting.chartbars import anchors_from_bundle, bars_from_records
        with tempfile.TemporaryDirectory(prefix="cb9-") as tmp:
            pack, bundle, w = _run(runner, _tick_world(), Path(tmp))
            recs = records_of_pack_path(pack)
            for iid in (1, 2):
                anchors = anchors_from_bundle(bundle, iid)
                self.assertEqual(sorted(anchors), [D1, D2])
                for isec in (60, 300):
                    got, interval = bars_from_records(recs, iid, interval_s=isec, anchors=anchors)
                    self.assertIsNone(interval)
                    want = w.seen[(iid, isec)]
                    self.assertGreater(len(want), 50)
                    self.assertEqual([tuple(b) for b in got], want, (iid, isec))

    def test_display_bars_equal_the_runners_built_bars_oracle(self):
        self._check_against_runner(oracle_run)

    @unittest.skipUnless(engine.available(), "stockify._engine is not built")
    def test_display_bars_equal_the_runners_built_bars_engine(self):
        self._check_against_runner(lambda *a, **k: engine.run_bundle(*a, **k))

    def test_real_bars_win_over_snapshots(self):
        from stockify.reporting.chartbars import Bar, bars_from_records
        a = _ts(D1, "09:15:00")
        recs = [_tick(a + 5 * NS, 100, 10), _bar(a, 60, 1, 5, 0, 3, 77), _tick(a + 70 * NS, 120, 30)]
        got, interval = bars_from_records(recs, 1, interval_s=60, anchors={D1: a})
        self.assertEqual(got, [Bar(a, 1, 5, 0, 3, 77)])
        self.assertEqual(interval, 60)

    def test_bar_pack_returns_the_exact_bars_and_their_interval(self):
        from stockify.reporting.chartbars import Bar, bars_from_records
        a = _ts(D1, "09:15:00")
        recs = [_bar(a, 300, 10, 20, 5, 15, 100), _bar(a, 60, 10, 12, 9, 11, 40), _bar(a + 60 * NS, 60, 11, 13, 10, 12, 50),
                _bar(a + 300 * NS, 300, 15, 25, 14, 22, 120), _bar(a, 60, 1, 1, 1, 1, 1, iid=2)]
        got, interval = bars_from_records(recs, 1, interval_s=None, anchors={})
        self.assertEqual(interval, 60)                                    # the smallest interval present
        self.assertEqual(got, [Bar(a, 10, 12, 9, 11, 40), Bar(a + 60 * NS, 11, 13, 10, 12, 50)])
        got, interval = bars_from_records(recs, 1, interval_s=300, anchors={})
        self.assertEqual((interval, len(got)), (300, 2))
        self.assertEqual(got[1], Bar(a + 300 * NS, 15, 25, 14, 22, 120))

    def test_daily_only_run_gives_daily_bars(self):
        from stockify.reporting.chartbars import Bar, bars_from_records
        a = _ts(D1, "00:00:00")
        recs = [_bar(a, 86400, 100, 110, 90, 105, 1000), _bar(a + 86400 * NS, 86400, 105, 120, 100, 118, 900)]
        got, interval = bars_from_records(recs, 1, interval_s=None, anchors={})
        self.assertEqual(interval, 86400)
        self.assertEqual([b.close for b in got], [105, 118])
        self.assertIsInstance(got[0], Bar)

    def test_empty_bucket_makes_no_bar(self):
        from stockify.reporting.chartbars import bars_from_records
        a = _ts(D1, "09:15:00")
        recs = [_tick(a + 10 * NS, 100, 5), _tick(a + 5 * 60 * NS + 10 * NS, 110, 9)]       # minutes 1..4 are empty
        got, _ = bars_from_records(recs, 1, interval_s=60, anchors={D1: a})
        self.assertEqual([b.start_ns for b in got], [a, a + 300 * NS])
        self.assertEqual([b.volume for b in got], [5, 4])
        self.assertEqual(bars_from_records(recs, 1, interval_s=60, anchors={})[0], [])      # no anchor, no bars
        self.assertEqual(bars_from_records([_tick(a - NS, 90, 1)], 1, interval_s=60, anchors={D1: a})[0], [])

    def test_a_gap_and_a_new_day_restart_the_volume_baseline(self):
        from stockify.reporting.chartbars import bars_from_records
        a, b = _ts(D1, "09:15:00"), _ts(D2, "09:15:00")
        gap = {"kind": "GAP", "instrument_id": 0, "t_event": a + 100 * NS, "seq": 0, "phase": 0, "reason": "disconnect",
               "scope": 0}
        recs = [_tick(a + 10 * NS, 100, 50), _tick(a + 20 * NS, 100, 80), gap, _tick(a + 130 * NS, 100, 90),
                _tick(b + 10 * NS, 100, 20)]
        got, _ = bars_from_records(recs, 1, interval_s=60, anchors={D1: a, D2: b})
        self.assertEqual([x.volume for x in got], [80, 90, 20])            # 50+30 | whole 90 after the gap | whole 20

    def test_property_bars_are_ordered_and_ohlc_consistent(self):
        from stockify.oracle.bars import volume_delta
        from stockify.reporting.chartbars import bars_from_records
        a = _ts(D1, "09:15:00")
        for seed in range(200):
            rng = random.Random(seed)
            recs, t, vol, want_vol = [], a, 0, 0
            prev = None
            for _ in range(rng.randint(1, 120)):
                t += rng.randint(1, 400) * NS
                vol = rng.choice((vol + rng.randint(0, 50), max(0, vol - rng.randint(0, 20))))
                recs.append(_tick(t, rng.randint(1, 10**6), vol))
                want_vol += volume_delta(vol, prev)
                prev = vol
            isec = rng.choice((60, 120, 300, 900))
            got, _ = bars_from_records(recs, 1, interval_s=isec, anchors={D1: a})
            self.assertTrue(got)
            for bar in got:
                self.assertLessEqual(bar.low, min(bar.open, bar.close), seed)
                self.assertGreaterEqual(bar.high, max(bar.open, bar.close), seed)
                self.assertGreaterEqual(bar.volume, 0)
                self.assertEqual((bar.start_ns - a) % (isec * NS), 0)
            starts = [b.start_ns for b in got]
            self.assertTrue(all(x < y for x, y in zip(starts, starts[1:])), seed)       # strictly rising
            self.assertEqual(sum(b.volume for b in got), want_vol, seed)

    def test_records_of_pack_and_journal_read_whole_records(self):
        from stockify import paths
        from stockify.daemon import journal
        from stockify.reporting.chartbars import records_of_journal, records_of_pack
        with tempfile.TemporaryDirectory(prefix="cb9-") as tmp:
            pack = golden.build_pack(_tick_world(), Path(tmp))
            _, want = packs.read(pack)
            data = pack.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            (paths.packs_dir() / f"{sha}.evt").write_bytes(data)
            self.assertEqual(records_of_pack(sha), want)
            head = packs.HEADER.unpack_from(data, 0)                     # the same records under a journal header
            jdata = packs.HEADER.pack(head[0], head[1], journal.FLAG_JOURNAL | journal.FLAG_FINAL, *head[3:]) + data[64:]
            jpath = Path(tmp) / "day.journal"
            jpath.write_bytes(jdata)
            self.assertEqual(records_of_journal(jpath), want)
            before = jpath.read_bytes()
            records_of_journal(jpath)
            self.assertEqual(jpath.read_bytes(), before)                 # reading writes nothing
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir() if p.name.startswith("day")), ["day.journal"])
            with self.assertRaises(packs.PackError):
                records_of_pack("../x")                                  # not a pack hash

    def test_a_torn_journal_tail_is_ignored_and_left_alone(self):
        import itertools
        from stockify.daemon import journal
        from stockify.reporting.chartbars import records_of_journal
        ticks = itertools.count(0, 10)
        with tempfile.TemporaryDirectory(prefix="cb10-") as tmp:
            path = Path(tmp) / "day.journal"
            w = journal.JournalWriter(path, fsync_every_mono_ns=1_000, mono=lambda: next(ticks))
            w.append([(1_000 + i, packs.RANK[packs.CLOCK], packs.enc_clock(0)) for i in range(10)])
            w.close(finalise=False)
            whole = path.read_bytes()
            want = records_of_journal(path)
            self.assertEqual(len(want), 10)
            path.write_bytes(whole[:-7])                                 # the last record is cut short
            got = records_of_journal(path)
            self.assertEqual(got, want[:9])                              # only whole records
            self.assertEqual(path.read_bytes(), whole[:-7])              # and nothing was repaired or removed


def records_of_pack_path(path: Path) -> list[dict]:
    return packs.read(path)[1]


def _tick(t: int, ltp: int, vol: int, iid: int = 1) -> dict:
    return {"kind": "TICK_QUOTE", "instrument_id": iid, "t_event": t, "seq": 0, "ltp": ltp, "volume": vol}


def _bar(start: int, isec: int, o: int, h: int, lo: int, c: int, v: int, iid: int = 1) -> dict:
    return {"kind": "BAR", "instrument_id": iid, "t_event": start + isec * NS, "seq": 0, "interval_s": isec,
            "start_ns": start, "open": o, "high": h, "low": lo, "close": c, "volume": v, "oi": 0}


if __name__ == "__main__":
    unittest.main()

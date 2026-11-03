# M1 — Data layer and recorder

> **Goal**: every piece of market data STOCKIFY will ever use, stored correctly, with history: reference data with dated specs, NSE end-of-day files, Kite historical bars, and a live tick recorder that runs every trading day. Plus the tools that turn stored data into engine input (event packs) and a replay ticker for daytime development.
> **Depends on**: M0. **Unblocks**: M2, M3, M5. **Size**: L.
> **Priority inside M1**: M1.0 → M1.1 → M1.2 → **M1.5 (go live)** → the rest. Every trading day without the recorder is option-chain history lost for good (K-5).
> **Demo**: replay a recorded day through the recorder, compact it, build a pack, print the quality report.

Concepts: C-1, C-5, C-6, C-7. Schemas: `A1_schemas.md` §1–§5, §7. Numbers: all money parsing goes through `simlab.numerics` (exact decimal → paise, A4 §0).

---

## M1.0 Recon (S)

- [ ] 🛑 Confirm with the owner that the **paid Kite Connect plan** (₹500/month, K-11) is active on the API key. Without it there is no WebSocket data and no historical data.
- [ ] Record the installed `kiteconnect` version. Confirm `KiteTicker` exposes a raw `on_message(ws, payload, is_binary)` callback. The recorder parses raw bytes itself (D-08): this avoids any dependence on how the library converts timestamps to `datetime` objects (I-4). 🛑 if raw frames are not available.
- [ ] Inspect the existing instruments cache: table schema, who reads it (search, portfolio, ticker), and how `refresh_instruments` is scheduled.
- [ ] Inspect `kite_ticker_worker._Supervisor`: subscription bookkeeping, reconnect backoff, threading model. The daemon's supervisor (M1.5) reuses this logic.
- [ ] Append findings to `RECON.md`.

## M1.1 Reference data with history (M)

Files: `simlab/data/refdata.py`, `simlab/data/instruments.py`. Tables: `A1_schemas.md` §1.1.

- [ ] Create `refdata.sqlite` with migrations (`simlab/data/migrations/refdata/0001_init.sql`, applied in order, recorded in `schema_version`), including `index_underlyings` (populated from `contract_rules.json`). Same migration pattern for every SQLite store from now on (I-13).
- [ ] `instruments.sync(on_date)`:
  1. Download the Kite instrument dumps for `NSE` and `NFO`; save each as `$SIMLAB/nse/raw/kite_instruments/{YYYYMMDD}_{EXCHANGE}.csv.gz` with a `.sha256` sidecar (cheap, and it makes the whole table rebuildable).
  2. Classify each row: `EQ`, `INDEX` (segment `INDICES`), `FUT`, `CE`, `PE` (K-9).
  3. Resolve the natural key (D-06): derivatives by `(exchange, underlying, kind, expiry, strike_paise)`, equities and indices by `(exchange, tradingsymbol)`. Parse strikes and prices with `simlab.numerics` (exact decimal), never `round(x * 100)` on a float.
  4. Upsert `instruments` (`first_seen`, `last_seen`); close or open `instrument_tokens` intervals when a token appears, moves to another instrument, or disappears.
  5. Append a `contract_specs` row (`source = 'kite_dump'`) only when lot size or tick size differs from the latest row for that instrument.
- [ ] Public API (used by everything else): `resolve_token(token, on_date) -> instrument_id`, `get(instrument_id)`, `find(exchange, tradingsymbol)`, `spec_at(instrument_id, on_date)`, `expiries(underlying, kind, on_date)`, `option_chain(underlying, expiry, on_date)`, `underlying_of(instrument_id)`.
- [ ] Underlying links: index derivatives name an underlying like `NIFTY`; the index itself is `NIFTY 50`. Keep that map in `contract_rules.json` → `underlyings` (⚠ verify each Kite tradingsymbol in the dump).
- [ ] One source of truth: make `kite_client.refresh_instruments` call `instruments.sync` and keep populating whatever table search/portfolio read today (a compatibility copy or view), so existing features keep working. If that is invasive, keep both for M1 and log a follow-up in `RECON.md`.
- [ ] Tests (`tests/simlab/data/test_instruments.py`, fixture dumps under `tests/fixtures/kite_instruments/`): token reuse resolves by date (A3 G13); a mid-life lot change produces two `contract_specs` rows and `spec_at` returns the right one on each side (the data half of G12); syncing the same dump twice changes nothing.

## M1.2 Calendar, sessions and rule configs (M)

Files: `simlab/config/*`, `simlab/data/calendar.py`, `simlab/timeutil.py`. Schemas: `A1_schemas.md` §7.

- [ ] `calendar/nse_holidays.csv`: trading holidays for 2024, 2025 and 2026 per segment, with the NSE circular as `source` (⚠ fetch NSE's published lists; the owner checks them). Add 2027 when NSE publishes it (usually December).
- [ ] `calendar/special_sessions.csv`: e.g. Muhurat trading (2026: reported for 8 November ⚠ date and times).
- [ ] `sessions.json` exactly as A1 §7.3 (X-6, X-7), with the ⚠ rows checked against the NSE circulars where possible.
- [ ] `contract_rules.json` (A1 §7.4), `broker_policy.json` (A1 §7.5), `schedule.json` (A1 §7.8), `storage.json` (A1 §7.9, `retention_days: null`).
- [ ] `charges.json` with every row of A1 §7.2 (sources Z-1, Z-2, Z-3, Z-4), `pricing.json` (A1 §7.7) and `margin_params.json` (A1 §7.6, the T0 assumption rows). The M0.2 loader must accept them; a test loads every config and checks that each component resolves to exactly one row for sample dates in 2024, 2025 and 2026 (A4 §5.2).
- [ ] `calendar.py`: `is_trading_day(segment, d)`, `trading_days(segment, start, end)`, `phases(group, d) -> [(phase, start_utc_ns, end_utc_ns)]`, `phase_at(group, t_ns)`, `generated_expiries(underlying, start, end)`, `validate_expiries(underlying, actual_dates)` (rules as validators, I-6).
- [ ] All IST↔UTC conversion goes through `simlab/timeutil.py` using `zoneinfo("Asia/Kolkata")`. A test greps the package: no `datetime.now()`, `datetime.fromtimestamp()` without `tz=`, `time.time()` or `time.localtime` outside `timeutil.py` and `daemon/clock.py` (I-4).
- [ ] Tests: phase boundaries on 2025-12-05, 2025-12-08, 2026-07-31 and 2026-08-03 (U-02); the Thursday→Tuesday switch around 2025-08-28/2025-09-02 (G15); a holiday-shifted expiry.

## M1.3 NSE end-of-day files (L)

Files: `simlab/data/nse_files.py` (fetcher), `simlab/data/nse_parsers.py`, `simlab/config/sources.json`. Tables: `A1_schemas.md` §1.2.

- [ ] Fetcher: browser-like `User-Agent`, `Referer: https://www.nseindia.com/`, at most 1 request/second, 3 retries with exponential backoff, 404 ⇒ "not published / holiday" (not an error). Every download is saved unchanged under `$SIMLAB/nse/raw/<dataset>/<date>.<ext>` with a `.sha256` sidecar and a row in `nse_fetch_log`. Raw files are never modified or deleted.
- [ ] `sources.json` holds URL templates with `valid_from`/`valid_to` and a `verified` flag (S-1…S-6). For each dataset, download one real file, run its parser, and only then set `verified: true` with the evidence (URL, date, header line) recorded in the row and in `01_facts.md`. The fetcher refuses unverified datasets unless `--allow-unverified` is passed.
- [ ] Parsers, each selected by date: `fo_udiff` (2024-07-08 →), `fo_legacy` (before), `cm_udiff`, `cm_legacy`, `ind_close_all`, `qtyfreeze`. Map columns **by header name** from a real file; unknown or missing columns raise (S-6 note). Decimal strings → paise via `simlab.numerics`.
- [ ] Map F&O rows to instruments by the derivative natural key. Contracts never seen in a Kite dump (history before recording began) are created with `tradingsymbol = NULL`, `source = 'bhavcopy'`. When the file carries a lot-size column, append `contract_specs` rows with `source = 'bhavcopy'` on change. 🛑 If legacy files lack lot sizes and the owner wants pre-2024-07 F&O backtests, stop and agree on a source.
- [ ] `settlement_prices`: for each expiry date and underlying, the final settlement price: index → index close on expiry day (`ind_close_all`); stock → official close (`cm_eod`). Cross-check against the expiring future's settlement price when one exists; mismatches beyond one tick go to `data_quality` (D-13).
- [ ] `qtyfreeze.csv` is current-only (S-6): fetch it every trading day (M1.5 schedule) and store each day's copy; changes become `freeze_limits` rows (cross-checked against circulars in M4.1).
- [ ] View `fo_universe(trade_date, underlying)` (A1 §1.2): the point-in-time F&O stock universe (C-4).
- [ ] CLI: `python -m simlab nse backfill --datasets fo,cm,idx --from 2024-01-01 --to <yesterday>` (resumable: skips dates already fetched with a matching hash) and `python -m simlab nse fetch --date <d>`.
- [ ] Tests with trimmed real-format fixtures (`tests/fixtures/nse/`): header mapping, paise parsing, mapping to instruments, the settlement-price cross-check, a legacy and a UDiFF file for adjacent dates producing identical schemas (U-03).

## M1.4 Kite historical bar cache (M)

Files: `simlab/data/history.py` (extends the helpers moved in M0.1), `simlab/config/kite_limits.json`. Tables: `A1_schemas.md` §2.

- [ ] `bars.sqlite` keyed by `(instrument_id, interval, ts_ns)` — **not** by token (D-06). Every row stores `fetched_on`. Basis = `adjusted` (K-6).
- [ ] Throttle: one process-wide token bucket at 3 requests/second (K-8), with an injectable clock for tests.
- [ ] Chunking: per-interval maximum range from `kite_limits.json` (community-reported defaults ⚠: minute 60 days, 3/5/10-minute 100, 15/30-minute 200, 60-minute 400, day 2000). On a range-limit `InputException`, halve the chunk, retry, and persist the learned limit.
- [ ] Continuous futures (K-5): `continuous=1` only with `interval=day` on a live futures contract. Store the result as the synthetic instrument `CONT:<underlying>:FUT` (kind `FUT_CONT`), never mixed into per-contract rows. Expired options have no Kite history at all; use bhavcopy (M1.3).
- [ ] Corporate-action detector (C-5, D-18): every fetch for an instrument with cached data also re-fetches the last 5 cached daily bars. If closes differ by a consistent ratio (> 0.1%), write `adjustments_detected`. For **live** instruments, drop that instrument's cached bars and refetch (`action = refetched`). For **expired** instruments, whose minute data Kite can no longer provide, keep the rows and only flag them (`action = flagged_only`) — never delete what cannot be fetched again. A nightly sweep runs the same check for every cached live instrument.
- [ ] CLI: `python -m simlab bars fetch --symbols RELIANCE,INFY --interval minute --from 2026-01-01 --to 2026-10-28`.
- [ ] Tests with a fake Kite client (U-04): chunk boundaries, throttle timing, limit learning, adjustment detection, expired instruments only flagged, timestamps stored as bar **start** in UTC ns.

## M1.5 The recorder (L) — go live as early as possible

Files: `simlab/daemon/ticker.py` (supervisor, raw frames), `simlab/daemon/subscriptions.py`, `simlab/daemon/recorder.py`, `simlab/daemon/kite_packets.py` (binary parser), `simlab/daemon/scheduler.py` (minimal), `simlab/daemon/clock.py` (from M1.6, created here first), `simlab/config/universe.json`. Tables: `A1_schemas.md` §3.

- [ ] **Universe** (`universe.json`, A1 §7.1): tiers per D-08.
  - FULL: `NIFTY 50`, `NIFTY BANK`, `INDIA VIX`; NIFTY and BANKNIFTY futures (near + next); option chains within ±`n_full` strikes of ATM for configured expiries; the equity and stock-future list you intend to trade.
  - QUOTE: chain strikes from `n_full+1` to `n_quote`; broader equity context.
  - Starting defaults: NIFTY nearest weekly `n_full=10, n_quote=20`; NIFTY next weekly `n_full=6`; NIFTY and BANKNIFTY current monthly `n_full=6`; BANKNIFTY next monthly QUOTE `n_quote=6`. Roughly 250–300 instruments.
- [ ] **Subscription manager**: builds the day's set from the universe, refdata and an ATM reference — the same-expiry future's LTP when it exists, otherwise the index LTP (the synthetic forward of C-13 replaces this fallback once M4.4 exists). Re-centres when ATM moves ≥ `recenter_threshold` strikes; adds strikes immediately; removes strikes only beyond `n_quote + hysteresis`. Never removes **pinned** instruments (`pin(instrument_ids, owner)`; M5 pins open paper positions). Enforces ≤ 3,000 per connection (K-1), warns at 80%, drops the farthest QUOTE strikes first. Every change is written to `subscription_log`: a missing packet only means something if you know the instrument was subscribed.
- [ ] **Clock**: every component takes a `Clock` (`now_ns()`, `mono_ns()`, `sleep_until()`); live uses `WallClock`, replay uses `ReplayClock` (M1.6). Nothing in `simlab/daemon/` calls `time.*` directly (I-2, I-4).
- [ ] **Ticker supervisor**: reuse the `_Supervisor` reconnect/re-subscribe logic. The `on_message` listener does only `queue.put_nowait((frame_no, clock.now_ns(), clock.mono_ns(), payload))` with `frame_no` a per-day counter ("listeners must be cheap"). Text frames (broker messages, errors) are logged. A full queue increments a counter and raises an alarm; it should never happen.
- [ ] **Packet parser** (`kite_packets.py`): a frame is a packet count followed by (length, packet) pairs; dispatch on length 8/28/32/44/184 per K-2/K-3. A 1-byte frame is a heartbeat: not a packet, counted per minute in `heartbeats` for liveness. Unit-tested against hand-built binary frames for every case (U-01).
- [ ] **Writer thread**: parses frames, resolves token → `instrument_id` with that day's map, assigns `seq` (strictly increasing per day; after a restart continue from `max(seq) + 1`), and inserts in batches every 250 ms or 2,000 rows into `ticks/landing/ticks_YYYYMMDD.sqlite` (WAL, `synchronous=NORMAL`). Stores parsed core fields, `frame_no`, **and the raw packet bytes**; depth is parsed later during compaction.
- [ ] **Gaps** (I-7): disconnect → reconnect, process stop → start, token expiry, and stalls (a FULL index silent for > 30 s during a continuous phase) each produce a `gaps` row with reason, start and end.
- [ ] **Minimal scheduler** (times from `schedule.json`, IST; skipped on holidays): refdata snapshot (instruments sync; price bands for FULL-tier instruments via the quote endpoint at ≤ 1 request/second and ≤ 500 instruments per call (K-8) into `daily_limits`, refreshed hourly for options; token-dependent steps retry every 5 minutes until 09:10); subscribe; unsubscribe after the F&O close (X-7); compaction (M1.7); NSE fetch including `qtyfreeze.csv` (M1.3), retried until 23:00; daily pruning of `recorder_status` rows older than 7 days. M5.9 later extends this scheduler; it does not replace it.
- [ ] **Health**: every 5 s write a `recorder_status` row to `simlab.sqlite` (connected, subscriptions by tier, packets/s, queue depth, writer lag, last-packet age per tier, bytes today). `GET /api/data/recorder` returns the latest row.
- [ ] Tests (U-13): batching thresholds, `seq` continuation after restart, one gap row per reason, re-centring with hysteresis, pinning, the 3,000 cap, `subscription_log` contents, the queue-full alarm — all driven by the replay ticker and a fake clock.
- [ ] Run standalone: `python -m simlab daemon --recorder-only` (and via `run.ps1 -Daemon`). Log an expected bytes/day estimate at start; after the first real day, record the actuals in `RECON.md`.
- [ ] 🛑 Before the first live day, show the owner the subscription list and the estimate. After the first day, show the quality report (M1.9).

## M1.6 Replay ticker (M)

Files: `simlab/daemon/replay_ticker.py`, `simlab/daemon/clock.py`.

- [ ] Same interface as the live supervisor (subscribe, set_mode, start, stop, raw `on_message`). Source: the archive (preferred) or the landing file of a recorded date.
- [ ] Rebuild the original frames exactly from `frame_no` (count + lengths + raw packets, in `seq` order) and deliver them with the original inter-frame gaps divided by the speed factor (`1x`, `10x`, `max`).
- [ ] Honour subscriptions and modes: deliver only subscribed tokens; when a FULL-recorded packet is subscribed in QUOTE mode, truncate it to the quote prefix (44 bytes tradable, 28 bytes index).
- [ ] `ReplayClock` follows the replayed receive times, so components stamping `clock.now_ns()` reproduce the original timeline.
- [ ] Fault injection for tests: `--inject disconnect@10:30:00+90s`, `--inject token_expiry@11:00:00`, `--inject kill@13:00:00`.
- [ ] Select with `--ticker live` or `--ticker replay:2026-10-28:10x`.
- [ ] Test: the recorder fed by the replay ticker over a fixture day reproduces the fixture's packets and frames byte for byte.

## M1.7 Compaction and archive (M)

Files: `simlab/data/compact.py`. Layout: `A1_schemas.md` §3.2.

- [ ] For a finished date: `PRAGMA quick_check` on the landing file; parse depth from raw FULL packets; write `full.parquet`, `quote.parquet`, `index.parquet`, `gaps.parquet`, `subscriptions.parquet`, `heartbeats.parquet` and `meta.json` under `ticks/archive/date=YYYY-MM-DD/` (zstd, row groups ≈ 1M rows, sorted by `seq`).
- [ ] Verify: re-read the Parquet files; row counts and a SHA-256 over `(seq, frame_no, instrument_id, raw)` in `seq` order must equal the same digest computed from the landing file. Record the result in `tick_days`.
- [ ] Retention: `storage.json` → `retention_days` is **null (never delete)** until the owner has backups (M5.13). 🛑 The owner sets a number explicitly; after that, a landing file is deleted only when its day is `verified` **and** older than `retention_days`. Nothing else ever deletes tick data (I-13).
- [ ] Disk check: warn when free space falls below the `storage.json` thresholds.
- [ ] CLI `python -m simlab compact --date 2026-10-28`; scheduled by the M1.5 scheduler.

## M1.8 Event packs (M)

Files: `simlab/data/packs.py` (builder, encoder, numpy reader). Format: `A1_schemas.md` §5. The C++ reader comes in M3.2.

- [ ] Pack spec (JSON): `dataset` (`ticks | bars | bhav_eod`), date range, instruments (ids or selectors such as "NIFTY options of the nearest weekly expiry within ±10 strikes"), tiers, bar interval, `clock_cadence_ms` (default 0 = none; CLOCK events never change results, A4 §1.6), `include_gaps` (default true).
- [ ] Builder: load rows and map them to records: FULL → `TICK_FULL`; QUOTE → `TICK_QUOTE`; index → `INDEX`; Kite bars → `BAR` with `t_event = start + interval`; `bhav_eod` rows → `BAR` (interval 86400) with `t_event` = the trade date's EOD publication time from `pricing.json` (A4 §1.1, §4.9); gaps → `GAP` (tier gaps expanded per instrument); optional `CLOCK`; `REFDATA` for instruments added mid-day and for daily limits; `SETTLE` from `settlement_prices` (final) and `fo_eod.settle_paise` (daily), with `t_event` = publication times from `pricing.json`.
- [ ] Ordering and `seq` exactly per A1 §5: sort by `(t_event, rank, source seq, instrument_id)` with the ranks of A4 §1.3, then number 1…N. Name the file by its SHA-256.
- [ ] Deterministic: the same spec over the same sources produces identical bytes (P-05).
- [ ] Manifest (`<sha>.json`): spec, source files and hashes, counts by kind, time range, `price_basis` (`raw` for ticks and bhavcopy, `adjusted` for Kite bars).
- [ ] CLI `python -m simlab pack build --spec spec.json` prints the hash; `python -m simlab pack show <sha>` prints the manifest and the first records.

## M1.9 Data-quality report (S)

- [ ] `python -m simlab data report --date D` computes per instrument: packets, coverage (% of continuous-phase seconds with ≥ 1 packet while subscribed), longest gap, crossed books (bid ≥ ask), day-volume decreases, out-of-order exchange timestamps, receive-minus-exchange latency percentiles, heartbeat gaps. Writes `data_quality` rows and a short Markdown summary; `GET /api/data/quality?date=` returns them.

## M1.10 Tracked defect #1 (S)

- [ ] Re-test `candles_since`. Fix it, or document why the recorder makes it irrelevant, and close it.

---

## Verification

```
.\.venv\Scripts\python.exe tests\run_tests.py              # all offline tests, including tests/simlab/data/*
python -m simlab daemon --recorder-only --ticker replay:<fixture-date>:max
python -m simlab compact --date <fixture-date>
python -m simlab pack build --spec tests/fixtures/packs/nifty_chain_spec.json
python -m simlab data report --date <fixture-date>
python -m simlab nse backfill --datasets fo,cm,idx --from 2026-10-01 --to 2026-10-28   # network; owner runs once
```

## Definition of done

The recorder has run unattended for at least three live trading days with daily compaction verified; NSE backfill covers the agreed range and every NSE dataset in use is `verified`; bar fetches pass the adjustment check; packs build deterministically; the replay ticker reproduces a recorded day byte for byte; all boxes ticked.

# RECON — what the code actually looks like

Append one dated section per milestone. Line numbers refer to the tree at the time of writing.

## 2026-11-02 — M0 recon (pre-deletion tree, commit `b019983` + uncommitted work)

### Items the plan relies on

| Plan reference | Actual | Notes |
|---|---|---|
| `kite_client.refresh_instruments` `:279` | `kite_client.py:279` | Keeps only segments `{"NSE","BSE","INDICES"}` (`:303`), so **NFO is dropped**. Table keyed by `instrument_token`, fully replaced on each refresh (`:220-232`, `:321`). `refresh_instruments_async` (`:336`) has no callers, so nothing schedules a refresh. → M1.1 |
| `get_instrument_token` `:445`, `quote` `:500`, `historical` `:513` | same lines | `historical` swallows every non-auth error and returns `[]` (`:513-535`). M1.4's "halve the chunk on a range-limit `InputException`" can't see that error through it. → M1.4 should call `kite.historical_data` directly or the wrapper must re-raise. |
| `is_authenticated`, `get_client`, `KiteAuthError` | `:179`, `:191`, `:60-68` | as described |
| Kite token store + login callback | `stockify_paths.state_dir()/kite_token.json` (`kite_client.py:50`); `GET /api/kite/callback` → `kite_client.exchange_request_token` (`server/routers/kite.py:51`) | The daemon reads it through `kite_client.load_cached_token()` / `get_client()` (I-10). |
| `kite_ticker_worker._Supervisor` `:67-225` | `:67` | **No reconnect or backoff logic to reuse.** `_on_reconnect` only logs (KiteTicker reconnects internally); the `_on_close` comment says auth errors stop reconnecting, but no code does that. It uses parsed `on_ticks`, not raw `on_message`. Re-subscribe on connect exists; unknown tokens default to QUOTE, which contradicts the class comment. → M1.5 writes its own supervisor. |
| `geo_alerts.py:56-78` store template | `:56-78` | singleton, WAL, `check_same_thread=False`, `Row` factory: as described |
| `geo_monitor/worker.py` `run_forever`, `load_env`; `__main__.py` | present | Logs to stdout only; the PowerShell launcher tees to `%LOCALAPPDATA%\STOCKIFY\logs\geo-monitor.log`. |
| `run.ps1` child supervision `:164-188` | `:164-190` | **No restart or backoff:** any child exit stops everything. → D-27 adds restart for the daemon only. |
| `tests/support.py` | `tests/support/__init__.py` (package) | `SandboxedDBTest` `:247`, `isolated_env` `:127` |
| `tests/run_tests.py` | present | Discovers subpackages that have `__init__.py`; exit 2 means a tracked defect started passing. |
| Tracked defect #1 `candles_since` | `tick_buffer.py:200-228` | Had no callers; **removed in M0.1** together with its expected-failure tests. |
| `stockify_paths.state_dir()` | `stockify_paths.py` | `local_appdata()` raises when `LOCALAPPDATA` is unset. Fine on Windows only (D-26). |

### Deletion import graph (M0.1)

- `geo_monitor/` imports nothing from `server/services/data.py`. No kept module (geo_monitor, gnews, alerts, search, kite, portfolio, ticks) imports `anthropic` or `playwright`. Neither 🛑 triggered.
- Importers outside the deleted set: `server/main.py:40` (`analysis_history` in lifespan); tests `test_known_defects`, `test_portfolio_sync` (`_position`), `test_module_health`, and `tests/support` (anthropic stub). All edited, not deleted.
- Web links into deleted routes: `web/src/app/page.tsx` (`/history`), `HoldingsList.tsx` (`/analyze/…`), `SearchBox.tsx` (`/analyze/…`).
- Dependencies no longer used: `anthropic`, `playwright`, `pypdf`, `sse-starlette` (removed). Kept: `yfinance` (search fallback), `requests` (gnews, geo monitor).

### Owner decisions at the M0.0 🛑

- D-03 stands: Python strategies by default, C++ optional.
- **Windows only, permanently (D-26)**: no Linux presets, CI, systemd or cloud host.
- Interim UI: STOCKIFY landing page; the search box shows instrument details without navigating; the HoldingsList analyze link is removed.
- Daemon restart with backoff (D-27). One commit for the whole overhaul (D-28). `CLAUDE.md` is never committed (D-29).
- Also removed as unused: `assets/favicon.png` (Streamlit era), `web/public/*.svg` (Next starter), `tests/geo_monitor/` (5 tests duplicated in `tests/test_geo_monitor.py`), dead helpers `portfolio_sync.{holding_for, anonymous_position_blurb, ui_caption_for}`, `india_utils.fetch_history_with_fallback`, `tick_buffer.{candles_since, macro_snapshot}`, `kite_ticker_worker.{resolve_macro_tokens, ensure_macro_subscriptions}`.

### Toolchain on the dev machine (resolved 2026-11-04)

- Python 3.12.10 venv; `pybind11 3.1.0` installed.
- **Visual Studio Build Tools 2026 (v18.4) is installed without the C++ workload**; no `cl`, no CMake, no Ninja. `run.ps1 -BuildEngine` stops with the fix. To install (admin, large download):
  ```powershell
  & "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\setup.exe" modify `
    --installPath "${env:ProgramFiles(x86)}\Microsoft Visual Studio\18\BuildTools" `
    --add Microsoft.VisualStudio.Workload.VCTools `
    --add Microsoft.VisualStudio.Component.VC.CMake.Project --includeRecommended --passive
  ```
  This brings `cl`, CMake and Ninja; `run.ps1 -BuildEngine` enters the VS developer shell itself. The presets use the Ninja generator, so they don't depend on the VS 2022 vs 2026 generator name.

### Open issues found in the plan docs (not blocking M0)

1. A1 §7.2 "VERIFY" rows vs M1.2's "exactly one row per component for 2024 sample dates": FO `exchange_txn` has no row before 2024-10-01, so a 2024-01 date fails. C-11's figures (said to equal G09) don't hold against the shipped config, because the current exchange rates resolve from `observed_on` = 2026-10-29.
2. `market_protection_auto_pct` = `"2"` meaning 2% (A1 §7.5) vs the fraction convention, and A4 §3.3's `market_protection_auto_bp`.
3. `run.json` policy keys `preopen` / `cas` / `freeze` (A1 §6.1) vs A4's `preopen_policy` / `cas_policy` / `freeze_policy`. `max_slices` appears in both `run.json` and `broker_policy.json`; its source R-1 says nothing about slices.
4. `DAY_END`: A4 §1.2 says 23:59:59.999999999 IST; `schedule.json` says `"23:59:59"`.
5. F&O pre-open scope: A1 §7.3 and A4 §4.7 put all `FO_FUT` in PREOPEN; X-6 says near month only (plus next month in the last 5 days).
6. G10 ownership: written in M2.5 and required on the engine by the M3 definition of done, but exercise STT and brokerage arrive only in M4.5.
7. M3 demo "byte-identical artifacts" vs A4 §9.3's 1e-12 / 100-paise tolerances for oracle vs engine.
8. `margin_flat.cpp` at `src/` (M3.8) vs `src/margin/` (A2 §1, M4.8). A2 §5 lacks `underlying_of` (M1.1). A2 §6 lists `/api/research/families`, which M6.4 never mentions. G12 asserts the reduce-only lot exception that A4 §3.2 marks ⚠.
9. M6.1 (six runs side by side) and the M6.10 heatmap don't say how they fit a ≤390px screen.

### Facts that are due now

- **T-1:** US daylight saving ended 2026-11-01. The Kite login window is now 16:30–19:00 PST; NSE trades 19:30–02:10 PST (pre-open included). Update T-1 and the PDT wording in M5 before M5.
- **Muhurat trading on 2026-11-08** (M1.2 ⚠) is unverified, and the recorder (M1.5) can't be ready by then.
- **Python 3.11 vs 3.12:** float `sum()` changed in 3.12 (A4 §0.3), and `run.ps1` accepts both. The engine records the Python it was built for, and `stockify.engine` refuses a mismatch. M2 must enforce the no-`sum()` rule.

## 2026-11-04 — M0 closed

- Installed the VS Build Tools 2026 C++ workload (`Microsoft.VisualStudio.Workload.VCTools` + `Microsoft.VisualStudio.Component.VC.CMake.Project`, recommended components) with the command above: MSVC 14.50 (`cl` 19.50.35727), CMake and Ninja from the VS install.
- `.\run.ps1 -BuildEngine` on the first try: configure + build of `win-msvc-release` with `/W4 /WX` clean, ctest 2/2, `_engine.cp312-win_amd64.pyd` copied into `stockify/`, `python -c "import stockify._engine as e; print(e.build_info())"` works.
- M0 verification: Python suite green (481 passed + 4 tracked defects), `import server.main`, web lint and build, no references to the removed modules.
- Note for M3: `engine_build_id` embeds the git SHA at configure time; `-BuildEngine` always reconfigures, but a plain `cmake --build` after new commits keeps the old SHA.

## 2026-11-04 — M1.0 recon

| Item | Finding | Consequence |
|---|---|---|
| `kiteconnect` version | 5.2.2 | Record it in `recorder_meta` (A1 §3.1). |
| Raw frames (🛑 M1.0) | `KiteTicker._on_message(ws, payload, is_binary)` calls `self.on_message(self, payload, is_binary)` with the raw frame **before** parsing (`ticker.py`). | The recorder stores exact packet bytes (D-08). Gate cleared. |
| Twisted reactor | `connect(threaded=True)` runs `reactor.run` in a thread; `stop()` calls `reactor.stop()`, which cannot be restarted in the same process. | Token expiry and sleep end the daemon process (exit codes 76 / 75); the launcher starts a fresh process (D-32). No in-process reconnect with a new token. |
| Reconnect | Library-internal `ReconnectingClientFactory`: `RECONNECT_MAX_DELAY` 60 s (min 5), `RECONNECT_MAX_TRIES` 50 (max 300). `kite_ticker_worker._Supervisor` only logs `on_reconnect` / `on_noreconnect`. | M1.5 supervisor: log every reconnect as a `disconnect` gap (start at close, end at the first packet after reconnect); on `on_noreconnect`, exit for a launcher restart. Raise `reconnect_max_tries` to 300. |
| Instruments cache readers | `kite_client._instruments_db()` → `state_dir()/instruments.sqlite`. Read by `resolve_symbol` (`server/routers/search.py`) and `get_instrument_token` (`stockify/data/history.py`). `get_instrument_token_for_index` has no callers since M0. `refresh_instruments` / `_async` have no callers. | M1.1 keeps `instruments.sqlite` as a compatibility copy filled by `instruments.sync`; search keeps working unchanged. |
| Existing `tick_buffer` / UI ticker | `kite_ticker_worker` (API process) writes to `ticks.sqlite` and fans out to `/api/ws/ticks`. | Stays as connection #1 for the UI; the recorder is connection #2 in the daemon process (D-20). |
| Paid Kite plan (🛑 M1.0) | **Cleared 2026-11-05** by `python -m stockify kite check` (token obtained 08:38 IST): profile ok; quote NSE:NIFTY 50 = 23960.6, token 256265; historical 8 day + 1,125 minute candles (last bar 2026-11-04 15:29 IST); WebSocket full mode delivered a 36-byte raw frame (2-byte count + 2-byte length + 32-byte index packet, matching K-2/K-3). | The recorder and bar cache can run live. |

### 2026-11-04 — M1.1 notes

- The token interval `valid_to` is **exclusive**: the first day the token no longer maps to the instrument (`valid_from <= d < valid_to`).
- Dumps must be synced in date order per exchange; an older dump after a newer one raises `SyncError`. To backfill old dumps, sync them oldest first.
- A re-download of the same day with a different hash (the dump's `last_price` column changes intraday) is kept as `<day>_<EX>_r2.csv.gz`; it never overwrites.
- Indices are kept without `contract_specs` rows (their dump lot and tick sizes are 0).
- **Follow-up:** `kite_client.instruments.sqlite` (search) is still filled by the old `refresh_instruments`, called after each live `refdata sync`. Moving search onto `refdata.sqlite` is a later CM task. The `underlyings` map in `contract_rules.json` is filled in M1.2 (⚠ verify the Kite tradingsymbols).
- Uses the SDK's private `KiteConnect._get("market.instruments", …)`, because it returns the raw CSV bytes, so strikes stay exact decimals. Re-check this on a `kiteconnect` upgrade.

### 2026-11-05 — follow-ups found while running the app

- **`run.ps1` stops everything when uvicorn reloads.** `--reload` makes WatchFiles send a console Ctrl+C on Windows. The web server (`npm.cmd`, sharing the console via `-NoNewWindow`) receives it and exits ("Terminate batch job"), and the supervisor loop then stops all children. Trigger: editing any file under the repo while `.\run.ps1` runs. Fix (GK): start the API without `--reload` by default (add `-Reload` for development), or give each child its own process group.
- **The uvicorn access log prints the one-time `request_token`** of `/api/kite/callback`. The token is single-use and already exchanged, but I-10 says tokens never reach logs. Fix (GK): filter the query string of that route out of the access log.
- **Both fixed 2026-11-05 (GK 1.1).** `run.ps1` starts the API without `--reload` by default; `-Reload` turns it on and gives the API its own console window, so the reloader's CTRL_C_EVENT no longer reaches the web server. `server/log_redaction.py` filters the `uvicorn.access` logger so `request_token`, `access_token`, `api_secret` and `checksum` values print as `***`. Verified live: editing a repo file left the API and web up, and a request carrying `request_token=FAKESECRET42` logged as `request_token=***`.

## 2026-11-05 — M1.2 notes

- **Holidays.** NSE's JSON API `https://www.nseindia.com/api/holiday-master?type=trading&year=YYYY` (needs a browser User-Agent, the NSE home-page cookie and a Referer) returns per-segment lists; CM and F&O were identical for 2024 (22), 2025 (18) and 2026 (20). Raw responses with SHA-256 are kept in `$LAB/nse/raw/nse_holidays/`. The 2024 list includes **2024-03-02 "Special Live Trading"**, a special Saturday session (disaster-recovery drill), not a holiday; it is left out of `nse_holidays.csv`. Weekend-dated entries (e.g. 2026-02-15, 2026-11-08) are kept; they change nothing.
- **Muhurat 2026-11-08** is flagged `Diwali Laxmi Pujan*` (a Sunday). The API gives no session times, so `special_sessions.csv` stays empty until the NSE circular is checked.
- **Index tradingsymbols** in `contract_rules.json` (NIFTY 50, NIFTY BANK, NIFTY FIN SERVICE, NIFTY MID SELECT, NIFTY NEXT 50) and INDIA VIX were all found in the live Kite NSE dump on 2026-11-05. Current futures lot sizes there (NIFTY 65, BANKNIFTY 30, FINNIFTY 60, MIDCPNIFTY 120, NIFTYNXT50 25) match X-3.
- **Open issues 1, 2, 4 and 5 from M0 recon are resolved** by D-33 (charge hole before 2024-10-01), D-34 (protection in bp), D-36 (DAY_END) and D-35 (pre-open scope).
- **No sourced expiry rule exists before 2024-11-20**, so `generated_expiries` returns nothing earlier (I-1). Validating older expiries needs sourced weekday rules per index.
- `broker_policy.json` values not in Z-1…Z-5 (`max_slices` 10, same-day CNC sell credit 80%, F&O MIS = full NRML) are marked `verified: false` assumptions.

## 2026-11-05 — M1.5 / M1.6 notes

- **Frame numbers count data frames only.** Heartbeats are stored as per-minute counts and text frames are only logged, so numbering them would leave holes a replay cannot rebuild. With this, the replay ticker reproduces `seq`, `frame_no`, `recv_ns`, `mono_ns` and raw bytes of a recorded day exactly (test).
- **A replay writes to `ticks/replay/ticks_YYYYMMDD.sqlite`**, never to the real landing file it reads.
- **Chains start once a reference price arrives.** The session subscribes the static set (indices, futures, equities) first; each chain is centred when the first packet of its reference instrument (same-expiry future, else the index) reaches the writer. No REST quote call is needed at start.
- **Subscription changes from the health loop go through `reactor.callFromThread`**, because the KiteTicker reactor is not thread-safe. The landing file's connection is guarded by a lock: the writer and the session threads share it.
- **The library reconnect budget** is raised to its maximum (300 tries, 60 s cap). When it gives up, the daemon exits 78 for a launcher restart; a 403 or token error exits 76 (wait for a fresh login).
- **Size estimate for the default universe** (202 instruments: 133 FULL, 69 QUOTE; 06:40 h from pre-open to F&O close): about **1.44 GB/day** of landing data at one packet per instrument per second, an upper bound. Replace with the first live day's actual figure.
- Still to build for live: the scheduler (refdata sync, session start/stop, NSE fetch, pruning), keep-awake and sleep after close (D-32), the login reminder, and the launcher's handling of exit codes 75/76/78.

## 2026-11-05 — storage plan (owner question)

- The owner can give STOCKIFY about **300 GB** (416 GB free). The ~1.44 GB/day estimate is the uncompressed landing file (raw + parsed columns + indexes), an upper bound.
- Plan: M1.7 compacts each verified day to zstd Parquet (expected 5–10× smaller, so ~0.15–0.3 GB/day; to be measured on the first live day), then landing files can be deleted after `retention_days`. At those rates, 300 GB of archives is roughly 4–8 years of trading days.
- **Owner decision 2026-11-05: backup target deferred.** `storage.json` → `retention_days` stays `null` (never delete) until a backup exists. Options discussed: a cloud bucket with an upload-only key and object lock (recommended, off-site), a USB drive (same location as the laptop), or both (3-2-1).
- Dropped the extra landing index `ix_packets_frame` (not in A1; replay reads in `seq` order). Edited in `0001_init.sql` because no real landing file existed yet.

## 2026-11-05 — first live recording (infrastructure test)

A 5-minute live run of `run_recorder` (10:03–10:08 IST, laptop on battery) with the owner-approved default universe:

- **61,286 packets in 299 s (205/s)** in 1,294 frames, 0 dropped, 99 heartbeats; all **202** instruments recorded (131 FULL, 71 QUOTE), 0 unresolved tokens, **no gaps**. Packet lengths: 42,506 × 184 (full), 16,105 × 44 (quote), 2,675 × 32 (index full).
- Rates: NIFTY 50 ≈ 4 packets/s, NIFTY BANK ≈ 3, INDIA VIX ≈ 2, liquid equities ≈ 2, chain strikes ≈ 0.6. Receive time minus exchange timestamp ≈ +0.7 to +0.9 s (includes the laptop's clock offset; worth checking against NTP in M1.9).
- Two text frames on connect (`instruments_meta`, `app_code`) were logged, not stored.
- The on-battery alarm fired once a minute as designed.
- **Size, measured:** 260 bytes per packet in the landing file → **~1.28 GB per full day** (pre-open to the F&O close). The same rows as zstd-9 Parquet: **5.1× smaller → ~0.25 GB/day**; raw bytes plus keys only would be ~0.16 GB/day. With 300 GB, compressed archives hold roughly 1,190 trading days (~4.8 years).
- Today's real `refdata.sqlite` sync: 53,937 instruments (NSE + NFO). NIFTY's weekly expiry in the dump is 2026-11-09 and the November monthly 2026-11-23, matching the calendar's holiday shifts.

## 2026-11-05 — M1.7 compaction notes

- `index.parquet` carries `mono_ns` and `instrument_token` in addition to the A1 §3.2 columns, so a replay from the archive can rebuild frames exactly.
- The archive is written to `date=YYYY-MM-DD.tmp` and renamed only after verification (row counts, a SHA-256 over `(seq, frame_no, instrument_id, raw)` in `seq` order matching the landing file, and every file's SHA-256 in `meta.json`).
- A failed compaction never blocks the post-close sleep: the job is marked done, `tick_days.status` stays `landing`, the landing file is kept, and `compact --date` retries.
- **First real day (2026-11-05, 5-minute test):** 61,286 packets, 15.9 MB landing → 4.8 MB archive (3.3×), verified in 2.2 s. The 30 parsed depth columns cost about a third of `full.parquet`; grouping rows by instrument inside each row group and delta-encoding integers would cut it about 23%, and dropping the depth columns (raw keeps them) about 51% (owner decision pending).
- **Owner chose the instrument-grouped, delta-encoded layout (D-37).** Re-compacted 2026-11-05: 15.9 MB → 3.6 MB (4.4×), verified.

## 2026-11-05 — M1.9 quality report notes

- First real report (2026-11-05, 5-minute test, from the archive): 202 instruments, 61,084 packets in the continuous phase, 0 silent minutes, 0 crossed books, 0 volume decreases, 0 out-of-order exchange timestamps; median receive-minus-exchange latency about 796 ms.
- **Coverage measures feed activity, not only recording health.** Kite sends a packet only when something changes, so illiquid strikes (e.g. far BANKNIFTY December options: about one packet every 30 s) show 3–5% coverage on a perfect recording. Recording problems show up as day-level silent minutes, gap rows and gaps on liquid instruments (indices, near futures, ATM strikes).
- The latency includes the laptop clock's offset from the exchange; add an NTP offset check before trusting sub-second latency numbers.
- Equities use the CM continuous phase (09:15–15:30); F&O-eligible stocks stop continuous trading at 15:15 from 2026-08-03 (closing auction), so their last 15 minutes count as uncovered until CAS eligibility is modelled.

## 2026-11-04 — M1.3 NSE end-of-day files (LWB 1.1)

- Every source downloaded and parsed for real before `verified: true` (evidence rows in `sources.json`, facts S-1…S-6).
- UDiFF exists from 2024-01-01 (D-40); legacy only before. Legacy F&O has no lot size: those rows load with `lot_size` NULL.
- Units as published: F&O volume = contracts (both formats), OI = units, turnover → paise. Legacy turnover is ₹ lakh to 2 dp (< ₹1,000 off UDiFF).
- FSP (D-39): NSE's settlement price of the expiring contracts. On 2026-10-27, 18 of 213 stocks differ from the CM close by > 1 tick (closing auction); indices all match. 2023-12-28 (legacy) shows the same pattern for 16 stocks.
- `ind_close_all` names ≠ Kite names: 66 of 167 map (aliases in `sources.json`); the rest are indices Kite does not list or names not aliased yet. India VIX O/H/L have 4 decimals and are rounded half-up to points × 100.
- `qtyfreeze.csv` lags circulars (NIFTY 1,800 vs 3,510): stored as observed (`source = qtyfreeze.csv`); M4.1 cross-checks circulars.
- Contracts first seen in a bhavcopy get `tradingsymbol` NULL, `source = 'bhavcopy'`. Their lot size becomes a `contract_specs` row only when a tick size is known from the same contract or a sibling (same underlying and kind); otherwise skipped (73 on 2026-10-27).
- Speed: one trading day (fo + cm + idx) ≈ 7 s, so a 2024-01-01 → today backfill ≈ 1.5 h. A `kind='EQ'` lookup could not use the partial unique index and took 107 s per CM file until it used the index's own predicate.
- Loaded into the real refdata so far: 2023-12-28, 2023-12-29, 2026-10-27, 2026-10-28, 2026-10-29, 2026-11-04, plus qtyfreeze 2026-11-05.
- `data_quality` is shared: the quality report now deletes only its own metrics for a day (`quality.FOREIGN_METRICS`).

## 2026-11-05 — M1.4 Kite bar cache (RWB 1.1)

- The cache lives in `stockify/data/history.py` next to the M0.1 helpers; `bars.sqlite` from migration `bars/0001_init.sql`.
- `kite_client.historical` swallows exceptions, so the cache calls the SDK's `historical_data` directly (it must see `InputException` to learn range limits).
- Throttle: capacity-1 token bucket at `historical_rps` (3/s): calls are spaced 1/3 s apart. A burst-of-3 bucket allowed 4 calls inside one second.
- Chunk defaults (`kite_limits.json` → `historical_max_days`) are the community-reported values; learned limits go to `$LAB/kite_limits_learned.json` (runtime state, not config).
- Kite prices arrive as floats: shortest repr, rounded half-up to paise.
- Corporate-action check: last 5 cached day bars (else the requested interval) re-fetched; a consistent ratio (spread ≤ 0.05%, |ratio − 1| > 0.1%) → `adjustments_detected`; live → whole cache of that instrument dropped and the requested range refetched.
- First real fetch (2026-11-05): RELIANCE/INFY day 2026-01-01 → 2026-11-04 (208 bars each, 1 request); Kite day closes equal the bhavcopy CM closes on 10-27, 10-28 and 11-04. NIFTY continuous futures 2024-01-01 → 2026-11-04: 706 day bars with OI.
- **Observed:** RELIANCE minute bars end at 15:14 IST (360 per day) while NIFTY 50 runs to 15:29 (375). F&O stocks stop continuous trading at 15:15 for the closing auction (D-13, `CM_CAS`); Kite has no minute bars for the auction window.

## 2026-11-05 — M1.8 event packs (CAM 1.1)

- `stockify/data/packs.py`: encoder, builder, `read(path) -> (header, records)` (records as dicts; checks sizes, order and count) and `read_numpy(path, kind)` for one fixed-size kind. A2 listed `read(path) -> numpy records`; variable-size REFDATA makes one array impossible, hence the split.
- Ticks are read through `stockify.data.ticks` from their raw bytes (`kite_packets.parse_packet` / `parse_depth` / `index_change`), so a pack is byte-identical whether the day comes from the landing file or the archive (test).
- One day's records are held in memory at a time; ticks are merged with the other events because receive order is already time order (a day with a backwards clock step is sorted).
- Selectors: ids, symbols, chain (`expiry: nearest` or a date, `strikes: N` around the ATM from the underlying's previous-day close in `index_eod` / `cm_eod`, optional nearest future). Ticks with no selector take every instrument in the day's subscription log.
- LTP-mode (8-byte) packets have no record kind and raise; the recorder never subscribes in LTP mode.
- `GAP` reason 5 `not_subscribed` is encodable but not generated yet; tier gaps are expanded per subscribed pack instrument.
- `REFDATA price_band` comes from `daily_limits`, which the M1.5 scheduler does not fill yet (quote-endpoint snapshot still open).
- Real packs built 2026-11-05: the recorded day (61,488 records, 18.3 MB, all 61,286 packets), a NIFTY nearest-expiry ±10 chain with 1 s CLOCK (17,588 records), RELIANCE/INFY day bars (adjusted) and a bhav_eod pack. Each rebuilt to the same SHA-256.

## 2026-11-05 — backfill findings (LWB 1.2)

- The first backfill (2024-01-01 →) refused 124 F&O and 114 CM days, by design (unknown columns / unexpected values raise). Two real causes:
  - UDiFF files back-published for 2024-01-01 … 2024-06-20 call the reserved columns `Rsvd01…Rsvd04`; later files `Rsvd1…Rsvd4`. Accepted as synonyms.
  - Early expiry on corporate actions (D-41): IDFC 2024-10-09, ITC 2025-01-03, SIEMENS 2025-04-04, ABFRL 2025-05-21 (scheduled expiries up to 3 months later). Stored in `instruments.actual_expiry`.
- Refused days kept their raw files; re-running the backfill re-parses them (only `parsed_ok = 1` days are skipped).

## 2026-11-05 — M2.0 recon (LIB 2.1)

Branch `stockify/m2-oracle` from `stockify/m1-data` (pushed through GK 1.3).

- Present and usable: `stockify.numerics` (all A4 §0.2 helpers, `format_float`, `dumps_canonical`), `stockify.data.calendar.phases(group, d)` for CM, CM_CAS, FO_FUT, FO_OPT, `packs.read(path) -> (header, records)`, `instruments` lookups (`spec_at`, `expiries`, `option_chain`, `derivative`, `actual_expiry`), `fo_universe` view, `freeze_limits` (keyed by underlying symbol).
- Config: `charges.json` rows carry `<field>_ppb`; a row is in force from `effective_from` until the next row of the same `applies` key (the loader sorts per key). `broker_policy.json` has `mis_cutoff` (CM 15:20 → 15:25 from 2025-12-26; FO 15:25 → 15:26), `mis_margin_pct` (CM 20%, FO 100%), `market_protection_auto_bp` 200 (from 2026-04-01 only: earlier dates need a row; see M2.1), `max_slices` 10. `margin_params.json` flat: index 12%, stock 20%. `pricing.json`: `stale_after_ms` 5000, EOD 19:00, FSP 16:00, risk-free 6.5% placeholder.
- Missing (to build): `stockify/runconfig.py`, `stockify/bundle.py`, `stockify/oracle/`, `stockify/strategy.py`, `stockify/strategies/`, `tests/golden/`, `examples/runs/`.
- The plan's "numpy reader" for packs is `packs.read` (records as dicts, M1.8 note); the oracle iterates those records directly, which is also the readable choice.
- Libraries: Python 3.12.10, pandas 3.0.6, matplotlib 3.11.2, PyYAML 6.0.3.

## 2026-11-05 — M2 build notes

- Oracle package `stockify/oracle/`: rng, orders, ledger, costs, margin, matcher, runner (event loop), context, metrics, artifacts, report. Strategy API `stockify/strategy.py`; strategies in `stockify/strategies/`; goldens `stockify/golden.py` + `tests/golden/`.
- Ledger: futures and MIS are variation-settled (`pnl(x) − vm` moves to cash at marks and when flat); a fill never moves cash at its own price, which keeps every paisa exact. CNC and options move premium at the fill.
- Bar model: two simulated paths on copies (ledger, the instrument's live orders, slippage RNG); positions along a path are exact fractions, so fill order never depends on floating point.
- First real runs (2026-11-05): `sma_reliance_daily` (1,197 days, Kite daily bars 2022-01-03 → 2026-09-30): ₹10.00 L → ₹10.45 L, 12 trades, costs ₹24,610 (36 % of gross; STT ₹21,963); `eod_straddle_nifty` (451 days of bhavcopy, 2025-01 → 2026-10): ₹10.00 L → ₹10.83 L, 157 trades (incl. expiry settlements, D-45), win rate 62 %. Both labelled; two runs of the same config give identical artifact hashes.
- Found while running: D-44 (daily-bar DAY orders were cancelled before their bar arrived), an `eod_close` fill on expiry day landing after the FSP (now settled at once), Sharpe ≈ −2e15 on a flat curve (all-equal returns now give sd = 0), and pack building at 60 ms per symbol lookup once refdata held 781,118 instruments (refdata migration 0003 adds an index; 100 lookups now 1 ms).
- Golden status: G01–G07, G09, G10, G12, G14, G16, G19, G20 pass; G08 waits for the owner's charge capture (🛑).
- The straddle's 44 cancelled entries are real: a leg whose next close fell more than 50 % below the protection floor never filled (mostly options one or two days before expiry).

## 2026-11-05 — M3.0 recon

Branch `stockify/m3-engine` from `stockify/m2-oracle` (pushed through XI 2.3, `cff7cde`).

- M2 is complete: `golden run --impl oracle` passes all 15 goldens (G01–G10, G12, G14, G16, G19, G20; G08 against the owner's capture) in about 2 s. `oracle run --config examples/runs/sma_reliance_daily.json` resolves the bundle, builds the packs and runs in 4.6 s with the M2 result (₹10.00 L → ₹10.45 L).
- The engine builds: `run.ps1 -BuildEngine` with MSVC 19.50.35727, C++20, Ninja, Release; ctest 2/2 (`stocksim_tests`, `stocksim_run --version`); build id `cc9f819fad50` (git `cff7cdeebbbe`, clean). Flags `/W4 /permissive- /utf-8 /fp:precise /EHsc /WX`. pybind11 comes from the venv (`find_package(pybind11 CONFIG)`).
- What exists is the M0 skeleton only: `build_info.{hpp,cpp}`, `src/run_main.cpp` (`--version`), `python/engine_module.cpp` (`build_info()`), `tests/test_build_info.cpp`. Vendored: doctest 2.4.12, nlohmann/json 3.12.0, PicoSHA2 (pins and hashes in `third_party/VERSIONS.md`). `stockify/engine.py` has `load()`, `build_info()`, `available()`. `golden run --impl engine` raises "only the oracle exists before M3" (`golden._run_single`).

### A2 §1–§5 against the repo

| A2 says | Repo | Decision |
|---|---|---|
| `engine/bindings/module.cpp`, `engine/tools/stocksim_run.cpp` | `engine/python/engine_module.cpp`, `engine/src/run_main.cpp` | Move to the A2 paths (`git mv`) when M3.10 / M3.11 change them. |
| §2 C++ types: `Date` is `int32` YYYYMMDD; enums for side, product, type, validity, status, reason | The oracle (M2.3) speaks strings: `"BUY"`, `"CNC"`, `"SL-M"`, `"TRIGGER_PENDING"`, `"R_TICK"`; `ctx.today()` returns `datetime.date`; `depth()` a list of levels; `spec()` a dict; `expiries()` ISO strings; `chain()` a list of dicts | Enums inside C++; the Python trampoline converts at the boundary so a strategy sees exactly the oracle's values (the oracle is the reference, M3 rule). |
| §2 `Reason { …, Eod, Ioc, User, Rms }` | The oracle's cancel reasons are `"EOD"`, `"IOC"`, `"USER"`; system orders are tagged `AUTO_SQOFF` / `RMS_SQOFF` | Keep the enum; strings map one to one. |
| §2 `Event.src` | Pack records carry no source index | `src` is the pack's position in the bundle's pack list, assigned by `PackSource` (A4 §1.3). |
| §3 `stockify/engine.py` wraps the module with bundle/pack helpers and `run_folder(config)` | Only the loader and version check | Added in M3.11 with `stockify/runs.py`. |
| §5 `stockify.runs` (`launch`, `import_run`, `rerun`) | Missing | Built in M3.11. |

No header in A2 conflicts with code that exists; everything else in A2 §1–§2 is still to be written.

## 2026-11-05 — M3.1 foundations (BPD 3.1)

- Headers `types`, `errors`, `int128`, `decimal`, `pcg32`, `sha256`, `format` (float format + CSV), `json_out`, `json_bundle`; doctest 21 cases / 333 assertions. `stockify._engine.testing` exposes them so `tests/stockify/test_engine_foundations.py` checks C++ against the oracle's Python on seeded random inputs: 20,000 cases per integer helper and per decimal unit, P-01 (20 seeds × 2 streams × 10,000 draws, plus `below`, `random`, `next_u64`), U-11 (10⁶ doubles against `repr`), canonical JSON, CSV cells, SHA-256.
- Floats: MSVC's `std::to_chars(…, scientific)` gives the shortest round-trip digits; laying them out with Python's rule (`-4 < decpt <= 16` → fixed) matched `repr` on all 10⁶.
- Found and fixed in the oracle: `numerics._scaled` multiplied inside `Decimal`'s 28-digit context, so a value with more than 28 significant digits was rounded and could pass as whole (`"1.0000000000000000000000000001"` became exactly 10⁹ ppb). It now works on the exact digits (A4 §0.1 already required this; no D-entry). Real configs never come close to 28 digits, so no result changes.
- Found in C++ while cross-checking: `floor_to(INT64_MIN, -1)` overflowed in its intermediate `floor_div`; `floor_to` / `ceil_to` are now `x ∓ remainder`, so only a final result outside int64 is an error, as in Python.
- `json_bundle` reads the envelope (schema, seed, packs, strategy, labels) and keeps the document; pack paths stay as written (relative to `$LAB`), because C++ never resolves lab paths — Python passes absolute paths to `add_pack`.

## 2026-11-05 — M3.2 event packs and sources (DLF 3.1)

- `event.hpp` (key, ranks, record view, decoders, `encode_record`), `pack_reader` (`MapViewOfFile`; the whole pack is validated on open with `packs.read`'s checks, every error naming its byte offset), `source` (`PackSource` merging by `(t, rank, pack index, seq)`, `VectorSource`, `LiveBatchSource`). An unknown GAP reason or REFDATA subtype is a malformed record, as it is for the oracle (`decode` raises there).
- `test_engine_packs`: every golden's pack plus synthetic packs of all eight kinds (453 records) decode to exactly `packs.read`'s records and re-encode byte for byte; merges of 1–4 packs give `runner.load_records`' order; random batch splits through `LiveBatchSource` give the same stream; malformed packs fail on both readers.
- A real pack from the lab (80,921 records: 37,159 BAR, 36,804 SETTLE, 6,958 REFDATA) also decodes identically and round-trips. Python `packs.read` 0.15 s; C++ reading it into the same Python dicts 0.08 s; re-encoding 0.02 s; merging the keys 0.01 s.
- The GAP reasons, REFDATA subtypes and event ranks in `event.hpp` are a third copy of the A1/A4 lists, tied to them by blockwatch (`flat` name comparison for the ranks, whose C++ spelling is `kRankBarBuilt`).

## 2026-11-05 — M3.4 instruments and specs (CB 3.1), M3.3 event loop (DLP 3.1)

- `session` (IST dates: fixed UTC+05:30, the only offset Asia/Kolkata has had since 1945; the bundle's phases, `phase_at`, `continuous_start`, `next_continuous_start`), `instruments` (bundle rows, dated specs pre-resolved per bundle day, `group_of` for CM_CAS stocks). The bundle is read as `nlohmann::ordered_json`, so anything iterated in dict order is iterated in the file's order.
- `engine` mirrors `oracle.runner.Engine` handler by handler (each C++ function names the method it mirrors). Orders, fills, costs and the ledger are no-op steps until M3.5–M3.8, which is exactly the oracle's behaviour for a strategy that places no orders.
- **Order of work**: the Python strategy trampoline (`stockify._engine.Engine`, `set_strategy_py`, and the context methods that exist so far) came forward from M3.10, so every section from here on is checked against the oracle as it lands, not only at M3.12. M3.10 still completes the API, the C++ registry and failure handling.
- `test_engine_parity.Recorder` places no orders and logs every callback and context answer. Oracle and engine logs are identical on all 22 golden worlds (variants included; 811 lines) and on 10 random two-day tick worlds (TICK_FULL / TICK_QUOTE / INDEX, gaps, price bands, settlement prices, built bars, a CAS day, inserted CLOCKs). P-02: pushing the records one per batch, or in random pieces, gives the same log as `run()`. P-03: the same world with and without CLOCK records logs the same.
- Found by the tick worlds: with a snapshot fill model the oracle computes A4 §2.1 evidence on every tick, orders or not, and logs `WARN day volume decreased`; the engine now does the same.
- Found while wiring the engine: the oracle ran from the in-memory bundle dict while `bundle.json` (the engine's input, and a re-run's) has canonical sorted keys, so `ctx.params` or a spec dict could iterate in a different order. `run_bundle` now runs from the `bundle.json` it wrote (I-8). Goldens and both example runs are unchanged.
- `Int128` (MSVC has no `__int128`) for the interval VWAP value now, charges and margin later: cross-checked with Python ints on 20,000 random cases per operation and `to_double` against `float(int)`; the check found subtraction overflowing at `x − INT128_MIN` (fixed).
- Still open: G14 (needs orders, M3.5) and the G12 golden on the engine (spec lookups are unit-tested now).

## 2026-11-05 — M3.5 orders (BWM 3.1), M3.6 fills (POA 3.1), M3.7 costs (NCB 3.1), M3.8 ledger (VOL 3.1), M3.9 artifacts (RW 3.1), M3.12 parity (RD 3.1)

- Ported from the oracle module by module: `costs` (D-46 exact components in 10⁻¹⁸ paisa, `Int128`), `ledger` (positions in insertion order, because the oracle iterates its position dict that way when it settles expiries), `margin` (`MarginModel` with `FlatMargin`), `orders` (Order, Fill, Book and its fork for the two bar paths), `matcher` (bar paths with exact fractions), `engine_trading.cpp` (place, validate, accept, activate, modify, cancel, fill, charge, the three fill models, IOC, D-44, settlement, end of day), `metrics` and `artifacts`. Orders keep the side / product / type / validity text as the strategy passed it: an invalid string is rejected (`R_INSTRUMENT` / `R_PRODUCT`) but written to `orders.csv` as given, as the oracle does.
- `stockify.engine.run_bundle` is the engine's twin of `oracle.run_bundle` (same bundle.json, run.json, artifacts, a manifest with labels); `golden run --impl engine` uses it. **All 15 goldens pass on the engine**, G12 and G14 included, and on all 22 golden worlds the oracle's and the engine's CSVs and `events_log.txt` are byte-identical (`metrics.json` too, though A4 §9.3 allows 1e-12).
- G08 on the engine (`test_engine_costs`): for every captured Kite case the C++ charge parts equal the oracle's and the C++ total equals Kite's; 6,000 random order / DP / square-off / exercise charges on the shipped rows across five dates (both sides of the 2026-04-01 STT change) match the oracle.
- P-04 (`test_engine_fuzz`, 200 seeds, ~40 s): random three-day worlds (minute bars, daily bars or ticks; an equity, a CAS stock, an index with futures and options expiring mid-run with a mid-life lot change and a freeze limit, a stock future; gaps, price bands, settlement prices that sometimes never arrive) with random fill model, slippage, latency (up to 90 s), policies, mark, capital and the shipped Zerodha rows, and a random trader drawing only from `ctx.rng()` (valid and invalid sides, products, types, quantities, prices, validities, protections; modifies, cancels, cancels inside callbacks). Over the 200 seeds: ~25,000 orders, ~3,000 fills, all 11 reject codes, every cancel reason, every fill evidence kind, all four charge kinds, 424 expiry settlements. Oracle and engine agree on every seed. This is how M3.5's "unit tests for each reject code, each type conversion and latency races" and M3.6's per-model edge cases are covered: the engine's behaviour equals the oracle's, whose own M2 unit tests define it, on every code and case the fuzz reaches.
- P-07 (causality, A4 §4.0), checked against the pack rather than either implementation: 2,990 of 2,999 fills checked (fills before a later MODIFY are skipped, because `orders.csv` keeps the final `t_activate`). DT-01: the same engine run twice gives identical `determinism.json`. DT-02 is the parity above.
- Found while porting: `stockify.engine.run_bundle` kept the engine (and its memory-mapped packs) alive through an exception's traceback, so Windows could not delete the temp folder; the wrapper now drops it in `finally`.
- Still open in these tasks: DT-03 (re-run from a folder, with the M3.11 runner); P-06 (accounting identity) is not written yet; the fuzz has no shrinker yet (no failure to shrink so far) and the weekly 5,000-seed run is a manual job (A3); `cas_policy = close_price` behaves as in the oracle, which does not yet fill resting orders at the official close (A4 §4.8).

## 2026-11-05 — M3.10 strategy API, registry and trampoline (B2B 3.1)

- Recon: the C++ `Strategy` base and the `Context` already existed (M3.3, brought forward); `Context` lives in `engine.hpp` / `engine.cpp` rather than A2's `context.hpp/.cpp`, because it is a thin view over the engine's private state (a friend). The binding sources moved to the A2 path, as decided at M3.0: `engine/bindings/module.cpp` (the module and `testing`), `engine/bindings/engine.cpp` (Engine, Context, trampoline), `engine/bindings/testing_packs.cpp`.
- Indicators (`indicators.hpp/.cpp`): `sma`, `ema`, `rsi`, `atr` exactly as `stockify.strategy` computes them (exact integer sums, then one rounding to `double`, in the same expression order). U-05: on 3,000 random price series × 8 window lengths (penny stocks to 10¹⁵ paise, flat stretches, jumps, sums beyond 2⁶³) every result has the same float64 bit pattern as the oracle's, NaN during warm-up included. A step Python would compute with a bigger int (a change wider than int64) raises `OverflowError` instead of wrapping. The context methods `sma/ema/rsi/atr/session_vwap`, `expiries`, `futures_chain`, `chain` mirror `oracle.context`; the Recorder parity strategy now logs them on every bar and phase change (134 non-empty chains, 75 VWAP values across the golden worlds), and the logs stay identical.
- `session_vwap` is bars only on both implementations, as the oracle's docstring says; A2 §4.3's tick variant (interval VWAP values, falling back to `ltp`) is still to come, in the oracle first.
- Registry: `STOCKSIM_STRATEGY(Class, "name")`, written inside the class's namespace. The strategies are an OBJECT library (`stocksim_strategies`) linked into the module, `stocksim_run` and the tests, so the linker cannot drop a registration. `sma_cross_cpp` and `orb_cpp` are line-for-line ports; `param_int` / `param_bool` / `param_string` read params the way the Python strategies do (`int("3")`, `int(3.0)`, Python truthiness), so a config spelling `"fast": "3"` behaves the same on both.
- Binding: `set_strategy_cpp(name)` (a run with a C++ strategy releases the GIL), `strategies()`, `drain_outputs()` (new orders as they are now; order events, fills, charges and log lines since the last call, as dicts keyed by the CSV column names, built by the same row functions as the CSVs), `load_sod_state` / `eod_state` (D-47 closed by D-66: they carry a session's day-to-day state, A1 §6.4; the state file is untrusted input, checked whole before anything is applied, the same refusals as the oracle's, ST 5.1). `stockify.engine.run_bundle` runs a `kind: cpp` bundle with its registered strategy.
- Failure: a Python exception in a hook (or an engine error) stops the run. The binding writes `ERROR the strategy raised an exception; the run stops` and the traceback, one ERROR line each, into the log at the failing moment; `run_bundle` writes `events_log.txt`, writes no other artifact, and re-raises the original exception. Marking the run `failed` in `lab.sqlite` is the M3.11 runner's job (the `runs` table arrives there).
- Tests (`test_engine_strategies`): every M2 reference strategy unchanged on oracle and engine on seeded random worlds, with identical run folders: buy_and_hold (20 daily worlds), sma_cross (60, a third of them on an index future traded NRML long/short in lots, with flips both ways), opening_range_breakout (40 minute-bar worlds, longs and shorts, stops triggered and cancelled, MIS square-offs, margin rejects), eod_short_straddle (25 option-chain worlds with two expiries, stops, settlements). `sma_cross_cpp` and `orb_cpp` give the same run folders as the Python originals on the same worlds. Half the worlds use the shipped Zerodha charge rows. A mutation check (a different strategy on one side) fails on the first differing `orders.csv` line, as it should. Drain: pushing the records in random batches and draining after each one adds up to exactly the final CSVs and log.
- Found while porting ORB: `timeutil.parse_hhmm` is `int()` per `:`-separated part, so `" 9:15"`, `"9:15"` and `"+09:015"` are all 09:15. The C++ `ist_ns` now parses the same way (`decimal.hpp` `python_int`, shared with the param readers), checked against the Python values in `test_session.cpp`.
- Not run: the two example runs (`sma_reliance_daily`, `eod_straddle_nifty`) need the lab's reference data and bars, which are not there yet (`unknown symbol 'NSE:RELIANCE'`: no instrument sync has run). They come with going live.

## 2026-11-06 — M3.11 runners and import (TM 3.1–3.8)

- Recon: `runs`, `run_orders`, `run_fills`, `run_equity` and `run_charges` existed in `store.py` since M0 with no functions; `python -m stockify run` was a stub (exit 2, "M3"); `stocksim_run` only printed `--version`; `oracle.run(config)` named folders `<name>-<bundle sha12>` and overwrote an existing one.
- Contract (the API, M3.14, codes against it): `store.create_run / update_run(only_if_status=…) / get_run / list_runs / replace_run_rows / run_rows`; `runs.queue / launch / import_run / rerun / determinism_diff`, `RunFailed` (`.run_id`, `.error`) and `RunNotQueued` (a `--run-id` that is unknown or not queued). Status flow `queued → running → done | failed`; every final update is conditional on `running`, so a row the API cancels stays `cancelled` (tested by cancelling while the run finishes). Bundle-resolution errors are failed runs too. `error` is `"<ExceptionType>: <message>"`; the traceback is in `events_log.txt` (M3.10).
- Ids: `queue` gives `<name>-<UTC YYYYmmddTHHMMSS>-<6 hex>`; `launch` without one gives the oracle's `<name>-<bundle sha12>`, then `-2`, `-3` … : a run folder or row is never reused, so launching the same config twice leaves the first folder untouched (its `determinism.json` equals the second's). The row keeps the runner's wall times; `import_run` takes them from `manifest.json`. All wall-clock reads go through `timeutil.now_ns()` (I-4).
- `import_run(folder)` rebuilds the row and child rows from the folder (typed: integer columns int, empty cells None; `run_charges.components_json` = the nine component columns; `run_equity.margin_paise` = `margin_blocked_paise`) in one transaction, so re-importing is harmless. These are derived rows; no tick table is touched.
- CLI: `python -m stockify run --config PATH [--run-id ID] [--impl engine|oracle] [--no-report]` prints the folder on stdout and a one-line summary on stderr; exit 0 done, 1 failed, 2 bad config, usage or a run id that is not queued. Tested in-process and as a real `sys.executable -m stockify run` subprocess (I-9), on the M2.1 offline bundle fixture (`test_bundle.BundleTest`).
- DT-03: `run --rerun <id>` runs the archived `bundle.json` as it is (never re-resolved: the test changes the bar cache after the run and the re-run still reproduces), with the packs it names and the run's own impl, into `<id>-rerun`, `-rerun-2` …; the new `bundle.json` must hash the same. Exit 0 when `determinism.json` is byte-identical, otherwise 1 with one `differs: <file>` line per file. Passes on both impls.
- `stocksim_run` (moved to `engine/tools/stocksim_run.cpp`): `--bundle <run>/bundle.json --out <dir> [--lab <dir>]`, `strategy.kind: cpp` only (exit 2 otherwise), packs relative to the lab (default: the bundle folder's grandparent). It writes the same nine deterministic files and `determinism.json` as `stockify.engine.run_bundle`, byte for byte (tested on three worlds), plus a manifest with `engine: "stocksim_run"` and a steady-clock `duration_ms`. A failure writes `events_log.txt` ending in `ERROR the run stops: …` and exits 1. No Python, SQLite or network.
- `kind: cpp` configs (TM 3.6): `bundle.strategy_ref` no longer refuses them. The ref must be a name in `_engine.strategies()` (a `BundleError` names the known ones, or says the engine is not built). Its `source_sha256` is SHA-256 of `"<engine_build_id>:<name>"`: the code that runs is the engine build, so the build id plus the name identifies it, and a rebuilt engine changes the bundle hash as a changed Python file does. Tested end to end (`runs.launch` and the CLI): `sma_cross_cpp` gives the same run folder as `SmaCross` on the same data, and its run re-runs (DT-03).
- Pack-build race (TM 3.7, found by the M3.14 e2e test): two runs needing the same pack at once both built it, and the second `replace` failed with `WinError 5` because the first run's engine had the pack memory-mapped. `packs.build` now uses a tmp name unique per builder (pid + random), and when the replace is refused it accepts the existing pack only if its SHA-256 matches (packs are content-addressed); a different file under that name re-raises and is never overwritten. The manifest is written the same way. Tested with three child processes racing while the pack is mapped; the old code fails that test with the same error.
- Manifest half of the race (TM 3.8, about 1 in 100 concurrent builds while something reads manifests): the manifest has no wall-clock field (racing builders write identical text), but when our replace was refused, our read of the existing manifest could itself be refused because another builder was replacing it at that moment, so the race was taken for a conflict. Publishing now retries a refused replace or read (8 tries, 20 ms doubling, capped at 0.5 s) and compares manifests by content, ignoring any wall-clock field should one be added; a complete manifest with other content still re-raises and is never overwritten. The pack's own hash check retries the same way. Tested deterministically (a refused read, an open manifest with the same or other content) and by 5 processes x 20 rounds with a reader running; the stress reproduced 1 failure in 120 builds before the fix and none in 240 after.
- Found on the way: the shared pre-commit hook ran `git init` for its blockwatch snapshot with a worktree's `GIT_DIR` still exported, which re-initialised the shared git dir (and set `core.bare`); fixed in the hook (not tracked).

## 2026-11-06 — M3.14 test-run API and UI (WP 3.1–3.4)

- Recon: no `/api/tests` or `/tests` existed; the app had `/portfolio`, `/alerts` and `/kite/callback`, server-rendered
  pages styled inline with the `globals.css` tokens, and `api.ts` as the only fetch wrapper. `package.json` has no
  charting library, so the charts are hand-drawn SVG (nothing added).
- **Schema format** (WP 3.1): each reference strategy has a data-only `PARAMS` list of `{name, type, default, required,
  help}`, `type` in `int | bool | str | decimal | symbol | hhmm | underlying`. A test checks that it lists exactly the
  `ctx.params` keys the source reads. `stockify/strategies/_catalogue.py` discovers Python strategies through
  `strategy.load` (A2 §4.4 check included) and lists the engine's C++ ones with the schema of the Python original they
  port; `validate_params` reports unknown keys, missing required ones and values the strategy could not read
  (`int()`, bool, `Decimal`, HH:MM, a symbol). `web/src/lib/params.ts` mirrors it with the same messages.
- **Endpoints** (WP 3.3, `server/routers/tests.py`): `GET /api/strategies`; `GET /api/tests` (status, family, offset,
  limit; newest first); `POST /api/tests` (runconfig.load, then the strategy's schema; 422 with every problem as a
  list; then `runs.queue`; 201 `{run_id}`); `GET /api/tests/compare` is a 501 placeholder declared before `/{id}`, so
  M6's path is never read as a run id; `GET /api/tests/{id}` (run card, config, metrics, manifest) and
  `/orders`, `/fills`, `/equity`, `/charges`, `/logs` (the end of `events_log.txt`, or a page with offset/limit, and
  the end of `runner.log`); `POST /api/tests/{id}/cancel`. Money is paise; times are ns plus an IST ISO string.
- **Queue rules** (`server/services/test_queue.py`): each queued run is `python -m stockify run --config
  <runs>/<id>/request.json --run-id <id>` (I-9), cwd the repo, no console window, output in `<id>/runner.log`; at most
  `max(1, cpu_count − 1)` at once, the rest in arrival order. The runner owns its status from `running` on. Cancel
  removes a waiting run or kills a running one, then writes `cancelled` only if the run is still queued or running
  (a run that finished first keeps its status, and the API answers 409). On API start, `queued` rows are re-queued
  oldest first and `running` ones left by a dead API become `failed` (`interrupted: the API restarted`). A launcher
  that cannot start fails that run only. Tests use a fake launcher, plus one real runner subprocess in the sandbox.
- **UI** (WP 3.2/3.4): `/tests` cards (status, strategy, dates, money start → end in ₹, gain or loss, worst fall from a
  high, trades, costs) with the run labels as plain-word chips (`fill:approximate`, `eod_approx`, `margin:flat(approx)`,
  `charges:unverified_rows`, `specs:equity_tick_assumed`, `settlement_fallback`; "adjusted prices" from the config's
  `price_basis`); a New test form from the schema (`/tests?new=1` opens it). `/tests/[id]` polls every 2 s while
  waiting or running, has cancel, shows the error and log tail when failed, and when done: the numbers, an SVG money
  curve with the drawdown under it, fills as cards, costs by kind in plain names, and the reproduction details. The
  yearly rate is shown only for runs of 240+ trading days ("too short to say" otherwise).
- **End to end** (WP 3.4): own API (8791) and web (3791) with `LOCALAPPDATA` in the scratchpad, so no real lab and no
  Kite token. Seeded with three engine runs built from golden worlds and imported with `runs.import_run`. From the
  web origin: a submitted test queued and failed cleanly (`PackError: unknown symbol 'NSE:RELIANCE'`: the sandbox has
  no market data), a second was cancelled, and bad params came back as a 422 list. Screenshots at 390 px and 1280 px:
  `C:\Users\priya\AppData\Local\Temp\claude\ca98620bf8fa9330\1e0a9619-1c55-43b7-bb61-9f4914ae6a7c\scratchpad\shots\`
  (`e2e_*.png` on real runs, `fx_*.png` on fixtures).
- Found: `npm run build` fails inside a worktree because Turbopack refuses the `web/node_modules` junction ("points
  out of the filesystem root"). Builds were checked with a temporary, uncommitted `turbopack.root` and pass; the
  main checkout is unaffected. Imported golden runs have no pack specs or strategy ref, so their cards show no dates;
  the detail page falls back to the equity rows' first and last day.
- WP 3.5/3.6: plain `label`s in every params schema, fill rows carry the tradingsymbol (from the run's bundle.json),
  registered C++ strategies without a schema (the benchmark's `noop_cpp`) are not offered, and `sma_cross_cpp` /
  `orb_cpp` launch from the API end to end through the real runner subprocess on the M2.1 offline data
  (`test_api_tests.CppFromTheFormTests`; screenshots `wp35_*.png`).
- **Found: two runs that build the same pack at the same moment race in `packs.build`.** Both see no pack, both build it,
  and the second `tmp.replace(target)` fails with `PermissionError [WinError 5]` because the first run already has the
  target open (memory-mapped by its engine). Seen with two tests submitted together. Fixed by TM 3.7; the C++ e2e test
  submits its two tests together again (WP 3.7): the runs overlap by about a second and share one pack.

## 2026-11-06 — P-06 accounting identity, the fuzz shrinker and the weekly job, the final close-out, the strategy how-to (RD 3.2–3.4, ENG 3.1)

- P-06 (`tests/stockify/test_accounting.py`) works from a run folder alone: it rebuilds every position from `fills.csv` plus the expiry settlements logged in `events_log.txt`, with A4 §6.1's average-cost rule, and checks each trade date: charges add up to `charges.csv`; `positions_eod` lists exactly the open positions with the same qty, average, realised and unrealised (at its own mark); NAV = capital − charges + Σ realised + Σ unrealised; gross P&L; and cash, on days with no futures or MIS position open (their marks move cash at prices no artifact records; NAV covers those days). It calls neither implementation. Clean on every golden world, the 200 P-04 seeds and the M3.10 strategy worlds, on both; a one-paisa change to a fill price, a NAV or a charge is reported.
- Found by P-06, the same on both sides: a daily bar is knowable only at the next midnight IST (A4 §1.1), so a fill on the last bundle day's bar comes after the run's final DAY_END and is in no equity or positions row. Tracked as DEFECT 2 in `tests/test_known_defects.py`; the owner chose a fix (D-52, RD 3.4). RD 3.4 does it: A4 §1.2 and §1.10 now end a run with one more `DAY_END` when its last input came after the last trading day's `DAY_END`, at that input's time and dated by its own IST date (so the row carries the next calendar date, as the owner accepted). Oracle first (the end of `runner.run`), then the engine (`Engine::close_out`, called once at the end of `run()` and from `finish()`, so live and backtest runs stay identical, P-02). D-44 is untouched: the waiting DAY order fills or is cancelled when that bar is matched, and the close-out records the result. It fires only in worlds whose last daily candle is knowable after the last DAY_END: none of the 22 golden worlds and none of the 200 fuzz seeds, so no golden's expected values changed; it does in the daily strategy worlds. DEFECT 2 left `test_known_defects` and is now `test_accounting.test_a_fill_on_the_last_days_daily_bar_is_booked`; P-06 no longer skips late fills.
- The fuzz world and the random trader moved from the test into `stockify/tools/fuzz.py` (one copy; `test_engine_fuzz` imports them). A saved failure has to fit the golden format, whose strategy is data, so the shrinker first records the trader: every context call it makes (place, modify, cancel, subscriptions, timers), tagged with the number of the callback it came from. `stockify/tools/replay.py:Replay` makes the same calls at the same callback numbers, which reproduces the run exactly (tested: the replayed CSVs equal the live trader's on both implementations). `golden.py` now also loads `F*.yaml` and a strategy given by `ref` + `params`.
- `shrink` is greedy delta debugging in a fixed order: instruments (with their derivatives), days, events (chunks, then single ones), trader calls, run settings back to plain values (no charges, no square-off fee, zero latency, ltp mark, 200 bp), then events again; each step is kept only if oracle and engine still differ in the same file; at most 2,000 runs. `save_golden` writes `tests/golden/F<seed>.yaml` with the oracle's orders, fills, charges, equity and positions as the expected values (or the oracle's error) and a title naming the seed, the file and the first differing line. Tested with a planted disagreement (one paisa added to the engine's fills of one instrument, in the comparison only): a daily-bar world shrinks to a few events, one instrument and a couple of orders that still fail; the saved golden is found by `golden.goldens()`, passes on both implementations and fails when an expected price is changed.
- **Weekly job (manual, D-26: no scheduler, no CI):** `.\.venv\Scripts\python.exe -m stockify tools fuzz --seeds 4000-8999 --shrink --save`. It prints each disagreement, shrinks it, saves it as a golden, and exits 1 if there was any. First run, 2026-11-06: **5,000 seeds, 0 disagreements, 15.5 minutes** on the owner's laptop.

## 2026-11-06 — M3.13 benchmark (PF 3.1)

- `python -m stockify tools bench --pack <sha> [--reps 3]` (`stockify/tools/bench.py`, from Z's draft) resolves the pack's bundle read-only, then times `Engine.run()` only, 3 runs each, median. It writes nothing in the lab (runs go to a temporary folder). `noop_cpp` (`engine/strategies/noop_cpp.cpp`) subscribes to every instrument's ticks and does nothing.
- Data: the existing pack `6d0a1f93dd86…` already was the recorded 2026-11-05 day, so no pack was built. It turned out to be **six minutes of market** (10:03–10:08 IST): 202 instruments, 61,488 records, about 12,000 ticks a minute. A full session is about 62 times longer, so the full-day figures below are estimates (D-48).
- Machine: Intel Core Ultra 9 285H, 16 logical cores, Windows 11, Python 3.12.10, engine `a7f53dcb947a`.
- Results, in plain words:
  - (a) C++ no-op on every tick: the six minutes replay in 12 ms, about **5.1 million events a second** (target: well over 1 million). A full session would take under a second.
  - (a) through `stocksim_run` (no Python at all): 45 ms for the whole process (start, mapping the pack, the run, writing the artifacts); the run itself 20 ms, about 3 million events a second (the tool's own timer also covers opening the pack).
  - (b) Python no-op on every tick: 57 ms, about **1.1 million events a second**, i.e. about one microsecond per Python call (61,286 ticks delivered). A full session would take about 4 seconds. Not optimised, as the plan says.
  - (c) Python SMA crossover on minute bars for the 10 busiest instruments: 11 ms. It placed no orders, because six minutes give only six bars per instrument and the slow average needs 20; a full day of it would take well under a second (target: under a minute).
- Re-run it on the first full recorded day and replace these numbers (D-48).
- `docs/plan/HOWTO_strategy.md` (ENG 3.1) walks one strategy end to end: the reference opening-range breakout (its listing is checked against the module by `test_howto`, so the doc cannot drift), the run config `examples/runs/orb_reliance_minute.json` (RELIANCE minute bars, 2026-11-04: the only day of minute bars in the lab), the CLI run, the `/tests` form, reading the result and the labels in plain words, `--rerun`, and the C++ port with `examples/runs/orb_reliance_minute_cpp.json`, `compare-artifacts` and `stocksim_run`. Every command was run once, on a scratch copy of the lab's refdata and bars (`LOCALAPPDATA` pointed at a scratch folder, so the real lab got no runs or rows): one trade (a breakout at 14:44, bought at ₹1,315.56, the 15:10 exit sold at ₹1,315.14), ₹1,00,000 → ₹99,982.21 after ₹13.59 of charges; the re-run reproduced `determinism.json`; the C++ port's run folder is identical; `stocksim_run` gave the same `determinism.json`. `test_howto` also checks that every `examples/runs/*.json` validates.

## 2026-11-07 — M3 closed: integration and the final checks (RW 3.2)

- M3.11–M3.15 were built in parallel on three branches and integrated onto `stockify/m3-engine` one gated merge at a time; the full suite ran on every merged state.
- The M3.9 determinism box closes here: DT-01 (`test_engine_fuzz`), DT-03 (`test_runs_rerun`, TM 3.3) and DT-02 (the parity tests) all pass.
- Final checks on the merged branch, run by hand on real lab data: engine build `/W4 /WX` and ctest; 31 doctest cases; 15 goldens on both implementations; `npm run lint` and `npm run build`; blockwatch. Then `examples/runs/orb_reliance_minute.json` (RELIANCE minute bars, 2026-11-04) through `python -m stockify run`: twice gives byte-identical `determinism.json`; `--rerun` reproduces it; the C++ port (`orb_reliance_minute_cpp.json`) and the oracle (`--impl oracle`) give identical artifacts under `compare-artifacts --tol-paise 0`.
- Found and fixed during integration:
  - the pre-commit hook re-initialised the shared git dir when run in a worktree (it set `core.bare`; restored, and the hook now unsets `GIT_DIR` / `GIT_INDEX_FILE` for its snapshot);
  - two runs building one pack at once (TM 3.7, TM 3.8).
- Still open, for the owner: going live (no full trading day has been recorded yet; the benchmark is re-measured on the first one, D-48), and the backup target.

## 2026-11-07 — M4.3 Black-76 and implied volatility (TQ 4.1, TQ 4.2)

- Recon: nothing priced options yet; `stockify/fno/` did not exist. `pricing.json` has one dated `risk_free` row (6.5 %,
  a placeholder until the owner sets it from T-bill yields) and the bundle carries it as `rate_ppb`, read the same way
  as the metrics' risk-free rate.
- Python (oracle) `stockify/fno/black76.py` and C++ `engine/include/stocksim/pricing/black76.hpp` / `src/pricing/
  black76.cpp`, each written from the C-13 formulas (C-10): price, delta, gamma, vega (per 1.00 vol), theta (per year),
  rho (= −t·V, since with F given only the discount factor depends on r); N from `erfc`.
- Implied volatility: bounds first (`BELOW_INTRINSIC` with a 1e-10 allowance, since a model price can round a hair
  under intrinsic; `ABOVE_FORWARD`; `ABOVE_STRIKE`), then `BELOW_TICK`, then Brent on σ ∈ [0.001, 5.0] to 1e-10 in
  price with a bisection fallback. A price needing σ < 0.001 (no time value to read) is `LOW_VEGA`; one needing
  σ > 5.0 is the new `ABOVE_MAX_VOL` (D-56). After solving, `LOW_VEGA` when half a tick of price error would move σ
  by more than 1.00 (vega < tick/2). Time: ACT/365 to the last trading moment, floored at one second.
- **G17/G18 are test vectors, not runner goldens**: they are pure maths with no engine world, so the golden runner
  does not fit. G17 lives in `tests/fixtures/black76_vectors.json` (each vector cites its source: Hull's Black's-model
  example and the A3 table), read by both `tests/stockify/test_black76.py` and `engine/tests/test_black76.cpp`.
  Expected values are strings, so a check is "1e-6 absolute for prices / 1e-9 relative for Greeks, or half a unit in
  the last printed digit, whichever is wider": the A3 table prints the row-3 put delta to 10 decimals, coarser than
  1e-9 relative. Every value matches the plan's to all printed digits; Hull's put is 1.1166.
- G18 over 3,366 grid points (K/F 0.8–1.2 in 17 steps, 11 maturities 1–90 days, 9 vols 5–80 %, calls and puts):
  2,170 round-trip within 1e-8 relative (worst 9e-10); 668 out-of-the-money points under a tick are `BELOW_TICK`; 528
  deep in-the-money points are `LOW_VEGA`, all with under ₹0.0005 of time value. G18's "model price below one tick" is
  therefore read as the time value, which for an out-of-the-money option is its price (D-56).
- C++ vs Python on 20,000 seeded random cases (A3 §3, ≤ 1e-12 relative): price, delta and rho bit-equal; gamma and
  vega within ~5e-16 (the normal density is written e^{−x²/2}·(1/√2π) in one and e^{−x²/2}/√2π in the other); theta
  within ~4e-14 (its two terms cancel); every implied volatility and every reason identical, all seven outcomes seen.

## 2026-11-07 — M4.5–M4.7 settlement and T0 margin (VOL 4.1–4.4)

- Recon (M4.5): M3 already settles index derivatives in cash at the FSP with dated exercise STT on long ITM options (0.125% from 2016-06-01, 0.15% from 2026-04-01), a ₹20 + GST exercise brokerage row and the `settlement_fallback` label; G10 passes on both. Open was who pays the expiry brokerage. VOL 4.1 (D-59, owner delegated to the supervisor): ₹20 + GST on every option leaving the books at expiry without a delivery leg (exercised, assigned or expired; ITM stock options pay the §7.3 delivery brokerage instead, never both); exercise STT only on a long ITM option's intrinsic value. Oracle and engine; the only golden that moves is G10 (+₹47.20: the expiring OTM call and the assigned put pay ₹23.60 each), which passes on both.
- M4.6 physical settlement (VOL 4.2, D-58, A4 §7.3 made exact): at the stock's FSP (or the §7.2 fallback) each expiring stock future or option closes (futures marked to the FSP, options at 0) and becomes a delivery leg in the stock: futures at the FSP, ITM options at the strike; long future / long call / short put take, the others give. N = min(take, give) units net per stock and expiry, claimed in `(instrument_id, product)` order. Each leg is booked through the ordinary CNC path (`apply_fill` with the CNC credit policy), so a delivered sale's proceeds are held back as any CNC sale's are; a give beyond the holding leaves a short CNC holding labelled `short_delivery`. Charges come from the existing `physical_delivery` component (A1, `charges.json`, unverified): 0.1% STT on each leg's value plus exercise STT on a long ITM option's intrinsic value, brokerage 0.25% delivered / 0.1% netted, GST on brokerage. Oracle first (`runner._settle_physical`, `costs.delivery`), then the engine (`Engine::settle_physical`, `Costs::delivery`; positions are all closed before the stock is booked, since a new CNC position can grow the ledger).
- G11 (new, A3): a stock future and two ITM options (one call taking, one put giving) on one stock; 100 units netted, 100 delivered; every number hand-computed in its notes; passes on both implementations, with a fallback variant. `test_physical_delivery`: with the shipped 80% same-day credit, a delivered sale holds back 20% until the next day; with no credit row it is usable at once; a give with no shares leaves the labelled short holding. Identical on both.
- The fuzz world (`stockify/tools/fuzz.py`) now lets the stock future expire mid-run in a third of the worlds, beside a stock call and put, with a stock FSP that sometimes never arrives (fallback). The variant is drawn from its own RNG, so the other worlds are unchanged. Of the 200 P-04 seeds, 36 deliver stock (69 legs, both sides, 13 at the fallback, 24 short deliveries); oracle and engine agree on all, and P-06 (which now reads `delivered` lines as CNC trades and closes physical options at 0) holds on both.
- M4.7, VOL 4.3: the expiry-day ELM (X-9) in the flat model, oracle and engine: a short index option on its expiry day adds |units| × the reference price (the underlying's, else the strike) × the `expiry_day_short_index_option_addon` rate. The shipped row was corrected to start 2024-11-20 (X-9) with basis `underlying_price_x_qty` (Zerodha's formula, the supervisor's decision). Long options stay premium only (X-10), now pinned by a test. `test_margin_t0` checks the hand-computed blocked margin on both implementations (12% the day before, 14% on the expiry day); half the fuzz worlds carry the add-on, and P-04, P-06 stay green.
- M4.7, VOL 4.4 (D-60): the Z-4 delivery-margin schedule in the flat model, oracle and engine. Offsets count trading days in the bundle calendar back from a physically settled contract's expiry (a holiday moves the schedule a day earlier); contract value = units × the stock's price. Long ITM stock options pay 10 / 25 / 45 % of the stand-in VaR + ELM (the flat stock rate × contract value, labelled `delivery_margin_approx`) on E-4..E-2, then 25 % and 50 % of contract value; a long OTM option within 1 % of spot (₹1 below ₹100) pays 25 % on expiry day; futures and short options pay min(50 % of contract value, 1.5 × NRML) on expiry day. No stock price, or an expiry outside the bundle's days: the normal margin. Inside, the flat model now sums in paise × 10¹⁸ so that min() is exact; its rounding to the rupee is unchanged. The R_MARGIN check no longer refuses an order that only reduces a position (A4 §6.5), so a shortfall can be traded out of; `RMS_SQOFF` stays unmodelled (no policy time). `test_margin_t0` covers the schedule, the holiday shift, both fallbacks and a shortfall account closing a position, on both implementations; the stock-delivery fuzz worlds carry the schedule. No golden changed.
- M3 review fixes, C++ hardening (RD 4.14, RD 4.15). A lot or tick of 0 reached a raw `%` in order validation, a hardware divide-by-zero (0xC0000094) that killed the Python process, while the oracle stopped with a ZeroDivisionError mid-run. Both now check every instrument once when the bundle loads (id and underlying_id within uint32, lot and tick positive), with the same message. `stocksim_run` wrote its artifacts and manifest outside its `try`, so a file it could not write ended in `std::terminate` (0xC0000409). Every failure after argument parsing now exits 1 with `events_log.txt` written. `test_engine_hardening` runs each case in a subprocess, and each failed before its fix. RD 4.16: one call at a time, and nothing after a failure. A guard around `run`, `push` and `finish` refuses a second call while one is in progress (another thread while a C++ strategy runs with the GIL released, or a strategy calling back into its own engine), and an exception leaving one marks the engine failed. After that, every call and `write_artifacts` refuse, and only the log stays readable, for the traceback. `fill()` does its checked arithmetic before it records anything. The tests (a re-entrant call, a second thread polling during a 300,000-tick C++ run, every call after a failed run) failed before the fix. RD 4.17: `LiveBatchSource::push` lost records and could refuse good batches. With two batches queued, a third push dropped the oldest even though nobody had read it, and a batch rejected part-way had already moved the ordering mark, so the next valid batch was refused. It now checks a batch whole against a local mark, commits the mark only on success, and drops only batches read to the end. A new `testing.live_steps` scripts pushes and reads; its three tests failed before the fix. RD 4.18: `stocksim_run` now refuses a pack that doesn't hash to the SHA-256 its bundle names, as Python's `resolve_packs` does. It exits 1 with the reason in `events_log.txt` and writes no artifacts. The test changes one byte of a well-formed pack; before the fix the run completed. RD 4.19: checked arithmetic where the review found plain arithmetic. `ctx.bars(iid, isec, n)` negated n, which overflows for n = −2⁶³ and gave a negative start index: an access violation (0xC0000005), now Python's `rows[2**63:]`, an empty list. The mid price's bid + ask and the slippage's price ± move now overflow as errors instead of wrapping. `VectorSource` can no longer be copied (its events point into its own strings). The bars test ran in a subprocess and crashed before the fix.
- VOL 4.5, the CAS-group MIS cut-off (closes the open item in the M4.0 section). `broker_policy.json` → `mis_cutoff` gains a `CM_CAS` row: 15:12 from 2026-08-03, when CAS began (Z-5; Zerodha's page shows no other date). The bundle fills `mis_cutoff_ns.CM_CAS` from it, falling back to the CM time before it. Both runners now schedule `MIS_SQOFF` for CM, CM_CAS and FO, and map each instrument to its group's cut-off for the square-off and for `R_SESSION`: FO for derivatives, CM_CAS for a CAS stock on its CAS day, CM otherwise. A CAS stock on a non-CAS day is in CM and keeps the CM time. Oracle first, then the engine (`cutoff_key` in `session.hpp`, the twin of `runner._cutoff_key`). New golden G21 (CAS stock at 15:12, plain stock at 15:20, a late MIS order refused only on the CAS stock) passes on both. No existing golden changed; P-04, P-06 and parity stay green.
- RD 4.20, P-04 for M4. The fuzz world (`stockify/tools/fuzz.py`) now also draws, from its own RNG: SPAN-lite with the shipped placeholder rows (35% of worlds; the golden builder's `spanlite_placeholder: true`), the 15:12 CM_CAS cut-off (50%), and a ₹15,000 account (25%) whose margin runs short. The random trader reads `ctx.chain` (forward, ATM, IVs and Greeks all logged, so both sides must agree to the bit) and trades its legs, and when funds are below zero it closes a position, which A4 §6.5 must never refuse. Stock delivery with netting, the expiry-day ELM, the delivery schedule and expiry brokerage come from the M4.6–4.7 variants. `test_p04` now asserts that every M4 behaviour is reached; on the 200 regular seeds the counts are chain trade 182, chain read with an IV 88, expiry brokerage without exercise STT 81, CAS stock squared off at 15:12 51, SPAN-lite margin 43, short index option on its expiry day 38, stock delivery 31, shortfall close accepted 23, delivery netting 7. All 200 agree; the run takes about 73 s (was ~40 s). Weekly job, 2026-11-08: **5,000 seeds (4000–8999), 0 disagreements, 19.5 minutes.**

## 2026-11-07 — M4.0 recon and fact check (SK 4.1, SK 4.2)

**What the code has for M4 today (recon):**
- **Contract fields.** `bundle.resolve_instruments` already writes, per derivative: `kind`, `underlying`, `underlying_id`, `underlying_class` (INDEX / STOCK from `contract_rules.json` → `underlyings`), `expiry`, `actual_expiry` (D-41), `strike_paise`, `settlement` (dated `contract_rules.json` → `settlement`: index cash, stock physical since 2019-10-01, X-8) and `last_trading_ns` (the session group's close on the last day, from `sessions.json`). The oracle (`runner.py`) and the engine (`instruments.hpp`) read them; expiry settlement is cash only, and a physical-settlement contract raises "out of scope until M4.6".
- **Sessions.** `sessions.json` has dated FO_FUT / FO_OPT groups (FO_FUT with its pre-open from 2025-12-08, X-6; the CAS-era rows from 2026-08-03 marked `verified: false`, X-7).
- **Freeze limits.** Plumbed end to end: refdata table `freeze_limits` (A1 §1.2) → `bundle._freeze` → spec `freeze_qty` → slicing by `floor_to(freeze, lot)` (oracle `runner.py`, engine `engine_trading.cpp`; G16). But the real lab's table holds **one snapshot only**, `qtyfreeze.csv` on 2026-11-05 (224 underlyings). A backtest before that date therefore gets no freeze limit and never slices. M4.1 adds the dated history from the circulars (X-5).
- **Goldens.** G10 (index option expiry at the FSP, the fallback label), G12 (mid-life lot change), G16 (freeze slicing with its own limits) and G20 (pre-open: future in PREOPEN, option CLOSED) exist and pass on both implementations. **Missing:** G11 (physical delivery of stock F&O, M4.6), G17 (Black-76 reference values, M4.3) and G18 (price → IV → price round trip, M4.3).

**Fact check** (evidence and sources in `01_facts.md`; checked 2026-11-07). NSE's own sites (`nsearchives.nseindia.com`, `nseclearing.in`, `nseindia.com`) refuse automated downloads (an HTML block page, or timeouts), so no NSE PDF was read directly. Circular numbers and dates come from exchange notices (BSE), Zerodha's own pages and posts, and regulatory-news summaries; each row says which.
- **X-10 confirmed.** Option premium is collected upfront, effective 2025-02-10 (NSE Clearing deferred it from 02-01). Zerodha always collected it, so M4.7's "long options = premium only" holds on every date.
- **X-9 confirmed, with two changes.** The +2% expiry-day ELM on short index options (from 2024-11-20, hedged or not) is computed on **underlying price × quantity**: Zerodha's own formula and examples. M4.7's note said strike × quantity, which came from a forum post. This was the one rule the fact check changed; **decided 2026-11-07** (the owner, through the supervisor): underlying price × quantity. Second, the 2026 single-stock item was backwards: NCL/CMPT/72906 **removes** the expiry-day calendar-spread benefit for stock derivatives (effective 2026-05-04). That matters only to SPAN (M4.8 / M4.10).
- **X-4 confirmed.** The 2024 lot values, from 2024-11-20: NIFTY 75, BANKNIFTY 30, FINNIFTY 65, MIDCPNIFTY 120, NIFTYNXT50 25.
- **X-5 changed (data, no rule change).** There were two more revisions: 2026-10-05 (NSE/FAOP/76693: NIFTY 3,510 and about twice the others) and 2026-11-02 (NSE/FAOP/77189: back to the earlier values, except BANKNIFTY 900). A 2026-09-01 change took FINNIFTY to 1,800. The old "2026-04-01: BANKNIFTY 900" row had no 2026 source and is withdrawn. M4.1's slicing rule, `floor(limit / lot) × lot`, is confirmed by Zerodha's lot-wise table.
- **X-12, X-13 confirmed as published.** NSE's public All Reports → Derivatives page lists the daily SPAN files and the F&O contract-wise ELM file. **S-7 stays 🛑**: exact file names and URLs need a browser check before M4.8.
- **Z-3 partly confirmed.** The 2024-10-01 rates were 0.00297% equity, 0.00173% futures and 0.03503% options. `charges.json` lacks the 0.00297% CM row. Today's rates are proven in force by K-10a, but when they started could not be found. That stays ⚠.
- **Z-5 confirmed.** Square-off is 15:25 equity and 15:26 F&O from 2025-12-26. Since CAS began, Zerodha's policy page gives **CAS stocks 15:12**, but `broker_policy.json` has no CM_CAS cut-off row, so CAS stocks are squared off at the general 15:25. That is broker-policy data outside M4, reported to the supervisor. The current MIS leverage figure was not found and stays ⚠.

- C++ vs Python on 20,000 seeded random cases (A3 §3 allows 1e-12 relative): at first price, delta and rho were
  bit-equal and the rest within 4e-14, because the normal density was written e^{−x²/2}·(1/√2π) in one and
  e^{−x²/2}/√2π in the other. The chain view (M4.4) logs these numbers and the parity logs must match exactly, so
  both now use the same operation order (TQ 4.3), and `year_fraction` divides the same two doubles on both sides
  (Python's int / int rounds once, and differs from the engine's for spans over 2^53 ns ≈ 104 days). Now every
  price, Greek and implied volatility is bit-equal, and every reason identical, all seven outcomes seen.

**Follow-ups (2026-11-07):**
- **CB 4.1 (D-53):** `charges.json` now has three dated CM exchange rows: 0.00322% from 2015 (unverified, labelled), 0.00297% from 2024-10-01 and 0.00307% from 2026-10-29 (VERIFY). Every golden is unchanged.
- **CB 4.2 (M4.1):** the contract fields were already in place (recon above; the FO last-trading moment is 15:30 before 2026-08-03 and 15:40 from then on, now tested). Index freeze limits are dated from the circulars in `freeze_limits.json` (D-54): 2026-03-02, 09-01, 10-05 (NSE/FAOP/76693) and 11-02 (NSE/FAOP/77189). Before 2026-03-02 no index limit is known, so no slicing. `bundle.freeze_cross_check` lists the `qtyfreeze.csv` snapshots that disagree; the real lab's 2026-11-05 snapshot agrees.
- **CB 4.3 (M4.11):** G20's variants re-run with the real `sessions.json` FO phases for 2026-03-02 (FO_FUT PREOPEN 09:00–09:15, X-6), and G16's `freeze_1800` variant re-run with the limit taken from `freeze_limits.json` (NIFTY 1,800 on 2026-03-02). Both still give the goldens' expected values on the oracle and the engine (`test_m4_real_config`); the goldens themselves are unchanged. G16's `freeze_3510` value is the real limit only from 2026-10-05.
- **WB 4.1 (M4.2):** `ctx.expiries` and `ctx.futures_chain` (from M3.10) checked against M4.2 on both implementations: sorted by expiry, per kind (`OPT` = calls and puts). `stockify/fno/roll.py`: `days_to_expiry`, `trading_days_to_expiry`, `roll_due(contract, trading_days_before, today)` and `next_contract(contract, contracts)`. These are helpers, not engine rules; they take `today` (`ctx.today()`), never the clock, and count FO trading days with the shipped calendar. A contract's last day is its `actual_expiry` when NSE expired it early (D-41).
- **Open: a CAS-group MIS cut-off per session group** (A4 §6.3). Zerodha squares off CAS stocks at 15:12 (Z-5), but the bundle's `CM_CAS` cut-off is filled from the CM rows, and both runners schedule `MIS_SQOFF` and the `R_SESSION` cut-off per segment. It needs a per-group `MIS_SQOFF` and `R_SESSION`, oracle first, and is scheduled after the engine-hardening work.
- **Open: FO exchange rows start on 2024-10-01.** There is no FO exchange-transaction row before that date, so an F&O run before it fails with a CostError. No verified earlier rate was found.

## 2026-11-07 — M4.4 option-chain view (AP 4.1, AP 4.2)

- Recon: `ctx.chain(underlying, expiry)` returned `{instrument_id, kind, strike_paise}` rows, used by EodShortStraddle and
  logged by the Recorder parity strategy. Market state had bid/ask/LTP/time per instrument but no open interest,
  though TICK_FULL and bar records carry it.
- `ctx.chain` now returns `stockify.strategy.ChainView`: still a list of row dicts, so existing code is unchanged (the M2
  strategies and every parity test pass), with the original keys first and then `bid_paise`, `ask_paise`,
  `mid_paise` (float, (bid + ask)/2), `ltp_paise`, `stale`, `oi`, `iv_bid`, `iv_ask`, `iv_mid`, `iv_reason` (why
  there is no mid IV), and `delta`, `gamma`, `vega`, `theta` at the mid IV (rupees; vega per 1.00 vol, theta per
  year). On the view: `forward_paise`, `forward_source` (`future` | `synthetic` | `forward_from_index` | None) and
  `atm_strike_paise` (nearest the forward, the lower strike on a tie). Market state gains `oi` (from TICK_FULL and
  incoming bars, not built bars), on both sides.
- Forward (C-13): the same-expiry future's mid; else the median of `K + (C_mid − P_mid)/D` over up to 5 strikes with
  both sides quoted and fresh, ranked by |C_mid − P_mid| (nearest the money by parity), when at least 2 qualify;
  else the index value; else None, and then no IVs. A non-positive forward (absurd quotes) also gives None. Time to
  expiry from the options' `last_trading_ns` (15:40 on and after 2026-08-03, X-7), rate from the bundle.
- Cached per engine step (the event key) on both sides; every call returns fresh objects, so a strategy that edits a
  row cannot change what the next call sees.
- Tests (`tests/stockify/test_chain_view.py`): chain worlds of 45 "moments" each (most of 18 options quoted within
  2 s, sometimes one-sided or quote-only, sometimes a future, gaps, long pauses that leave quotes stale, then the
  index tick where the chain is read), with a 6.5 % rate (golden bundles carry none). Oracle checks: row order and
  keys, mid IV reprices the mid within 1e-10, Greeks equal Black-76's at that IV, ATM nearest the forward, all three
  forward sources seen, plus a hand-built `_forward` table (future wins; synthetic needs two fresh two-sided strikes;
  index; none). Parity: on 5 chain worlds the engine's log equals the oracle's byte for byte, IVs and Greeks
  included (about 1,900 mid IVs and 2,100 bid IVs per 3 worlds); Black-76 is bit-equal on both sides (TQ 4.3).

## 2026-11-07 — M4.12 replaying recorded chains (CAM 4.1)

- **Recon.** Chain selectors already existed from M1.8 (`{"chain": {underlying, expiry: nearest|next|date, strikes, kinds, futures}}`, centred on the previous close). Neither runner acts on REFDATA `instrument_add`; what a strategy sees comes from the bundle and the ticks. A strike with no data yet has no `last_tick`, and one whose ticks stop goes stale after `stale_after_ms`. So I-7 already held. The gap: every `instrument_add` was stamped at 00:00 IST, even for a strike the recorder only subscribed when it centred or re-centred a chain.
- **Change.** In a ticks pack, `instrument_add` is now at the instrument's first subscription when that subscription's reason is not `start…` (a chain centre or re-centre). That is when the daemon learned of the strike (A4 §1.1). Instruments of the start batch keep 00:00. Same bytes from landing and archive (P-05).
- **On the real recording** (the 5-minute 2026-11-05 test, read-only; one derived pack built): a NIFTY nearest-expiry chain selector (3 strikes each side, with the future) gives 14 options added at 10:03:29, when the recorder centred the chain, and the future at 00:00. A strategy watching the chain sees the strikes without quotes (stale) before 10:03. Afterwards it sees only recorded prices, stale once the recording stops. Oracle and engine logs are identical (2,879 lines).
- **Tests** (`test_chain_replay`, a synthetic day with one strike subscribed by a re-centre at 09:15:10 and dropped at 09:15:15): its add time and order, the start batch at 00:00, landing = archive bytes, and the strategy's view on both engines (no quote before its first tick, only recorded prices, stale after the drop).
- **Waits for going live:** a full recorded day with real mid-session re-centres (chains moving with the index) has never been recorded. Validating replay on one is part of the first live days.

## 2026-11-08 — M4.8 SPAN-lite margin (MEZ 4.1, MEZ 4.2)

- Recon: the oracle and the engine had only the T0 flat model (with Z's X-9 expiry-day ELM since VOL 4.3);
  `margin_params.json` had an empty `spanlite: {}`; `margin_daily.csv` was written as a header only; the engine
  held `FlatMargin` by value behind an abstract `MarginModel` with `requirement` and `mis`.
- `margin.model: spanlite` (run config) selects `stockify/fno/spanlite.py` in the oracle and
  `engine/src/margin/spanlite.cpp` in the engine (behind `MarginModel`; the engine now holds a
  `std::unique_ptr<MarginModel>`, and engine_trading.cpp only changed `margin_.` to `margin_->`). MIS keeps the
  flat rule. The definitions are D-57: 14 scenarios plus 2 weighted extremes, relative VSR, per-leg forwards,
  ELM on futures and short options, each part rounded up to the rupee per underlying; total =
  max(scan, SOMC) + ELM + add-on, summed over underlyings.
- Each leg's IV: its chain mid IV (M4.4); else its last valid IV today; else the ATM IV of its expiry; else
  `default_iv`; either fallback adds `margin_spanlite:iv_fallback`. The bundle adds
  `margin_spanlite:<calibration_id>` (today `margin_spanlite:placeholder`) beside `margin:spanlite(approx)`.
- **Placeholders, explicit and labelled** (`margin_params.json` → `spanlite`, effective 2015-01-01, `verified:
  false`, `calibration_id: placeholder`, source "placeholder until M4.9 calibration against Kite … not NSE's risk
  parameters"): INDEX PSR 6 %, VSR 25 % (relative), extremes 3 × PSR at 0.35, SOMC ₹2,000 per short lot, ELM 2 %,
  default IV 15 %; STOCK PSR 12 %, VSR 25 %, SOMC ₹3,000 per lot, ELM 3.5 %, default IV 30 %. A row naming an
  underlying wins over its class's row. The expiry-day add-on reuses Z's `expiry_day_short_index_option_addon` rows
  (2 % of underlying × quantity, from 2024-11-20).
- `margin_daily.csv`: one row per underlying with NRML F&O positions at DAY_END (`model` = spanlite); flat runs
  still write none. Expiring options settle before DAY_END, so the expiry day's add-on shows in that day's funds
  (blocked margin), not in its rows.
- Found and fixed before merging: the label code first reused the name `ids` in `resolve_bundle`, replacing the
  run's instrument ids with the calibration ids (`instrument placeholder is in a pack but not in refdata`).
- Tests: `test_spanlite.py` (hand-computed short future and short option scans, SOMC floor, the X-9 add-on on
  expiry day only, the IV fallbacks and label, grouping by underlying, U-07 iron fly < naked straddle, the bundle
  label and rows); `test_spanlite_parity.py`: on 5 two-day chain worlds traded by a seeded option trader that
  logs funds after every order, the engine's run folder, margin_daily.csv and labels equal the oracle's byte for
  byte; the oracle run sees the add-on charged on expiry day and never before.

## 2026-11-08 — M4.13 reference option strategies (IF 4.1)

- `stockify/strategies/short_straddle_stop.py` and `iron_fly.py`, sharing `_option_legs.py`, a private module the catalogue does not list. Each strategy reads its own params, so the catalogue's schema check sees exactly what it reads.
  - **Entry.** From `entry_hhmm`, take the nearest expiry at least `min_days` away and read the chain view (M4.4). Take the ATM strike nearest its forward. At the first moment every leg has a fresh two-sided quote (checked at the entry time, then on each tick), buy the wings first (iron fly: `wing_strikes` out), then sell the ATM call and put.
  - **Stops.** When a sell completes, a BUY SL-M is placed for the filled quantity, its trigger at the average fill × (1 + `stop_pct`), rounded up to the tick.
  - **Exit.** At `exit_hhmm`, working stops are cancelled and every leg still open is closed. If no fresh quotes come before the exit time, the day is skipped, with one logged reason.
- **Waiting for fresh quotes.** Entering only at an exact moment skipped every day on the test chains, whose quotes arrive in bursts. A strategy should not lose its day to one stale moment, so it now waits for the first moment with fresh quotes.
- **Margin and fills.** Margin is the run's `margin.model` (flat T0 for now; SPAN-lite, M4.8, is landing separately and nothing here depends on it). Fills are the run's fill model (`snapshot_quote` in the tests).
- **Tests** (`test_option_strategies`, on test_chain_view's chain worlds). On 30 seeds × 2 strategies with random params, oracle and engine give identical run folders (DT-02). The worlds reach every path: entries, stops that fire and stops cancelled at the exit, time exits, wings, skipped days. The mechanics are also unit-tested: the stop trigger arithmetic, wings before the short legs, no entry without fresh two-sided quotes, and bad params refused. The catalogue's reference set (Y's test) now lists the two new strategies.
- **Left for later**, as agreed: the comparison report against `eod_short_straddle` (M4.13 box 3) and the 5-recorded-day demo. They need SPAN-lite and real recorded days.

## 2026-11-08 — M4.13 comparison report (RW 4.1)

- Recon: X's `short_straddle_stop` and `iron_fly` (IF 4.1) and M2's `eod_short_straddle` existed; the run report
  (`stockify/oracle/report.py`) covers one run; `equity.csv` records margin only at each day's close, when an
  intraday option strategy is already flat, so no artifact held the intraday margin.
- **Tool, not a report-module extension**: `python -m stockify tools compare-strategies --config FILE`
  (`stockify/tools/compare_strategies.py`). Config `stockify.compare/1`: a name and two or more runs, each a title,
  an optional role (`eod` | `straddle` | `iron_fly`) and a normal run.json. Each run goes through `runs.launch`
  (ordinary run folders and lab.sqlite rows); the report goes to `runs/compare-<name>/compare.md`. Exit 0 done,
  1 a run failed, 2 a bad config. `compare()` works on finished run folders, so tests need no lab data.
- **Intraday margin**: the tool wraps each Python strategy in `stockify/strategies/_margin_watch.py`, which
  forwards every hook unchanged and logs `margin_watch blocked=<paise>` after each fill and at each day's end; the
  report takes the peak over those lines and the day-close margins. Wrapping changes only the log (a test compares
  orders, fills, charges, equity and positions with the unwrapped run). One caveat: SPAN-lite remembers each
  option's last valid IV of the day when margin is computed, so asking for funds more often can, in a fallback
  case, give a slightly more recent IV. C++ strategies are not wrapped (their peak is the day-close margin).
- The report, in rupees and plain words: per run, the strategy, money start → end ("made ₹… / lost ₹…"),
  charges, trades, the most margin tied up at any moment, the worst fall, and the caveats (labels in words); a
  day-by-day table; and closing sentences: end-of-day vs intraday on the same days (the distortion M4.13 asks
  for) and iron fly vs straddle margin and result.
- Tested on the two-day chain worlds of the SPAN-lite tests, with an end-of-day world built from the same
  quotes (each option's close = its last mid that day, published at 19:00, next-day-close fills). There, the iron
  fly tied up about 28 % less margin than the straddle.
- **Pending going live**: `examples/runs/straddle_vs_ironfly_recorded.json` is ready (bhav_eod for the end-of-day
  run; recorded ticks with the nearest NIFTY chain for the two intraday runs; SPAN-lite everywhere). Its dates,
  2026-11-09 to 2026-11-13, are placeholders: set them to five or more recorded days, then run the tool.

## 2026-11-08 — M4.14 UI additions (WP 4.1, WP 4.2)

- **API (WP 4.1).** `GET /api/tests/{id}/margin` returns `margin_daily.csv` (SPAN-lite runs only; a flat-margin run writes no rows). `GET /api/tests/{id}/settlements` returns the expiry settlements in cash and the physical deliveries. They are read from the engines' own log lines, which are identical on both engines: "settled … at FSP … (source)" and "delivered …". Each comes with its source (NSE's settlement price, or the A4 §7.2 fallback) and each contract's settlement charges. DTOs are mirrored in `types.ts` (`dto-fields`).
- **Page (WP 4.2), `/tests/[id]`.**
  - **Margin by day:** per day and underlying, the price-and-volatility risk, short-option minimum, extreme-loss margin, expiry-day and other add-ons, and the total. For a flat-margin run, a plain explanation plus the margin held at each day's end, from `equity.csv`.
  - **Expiry settlements:** what settled or was delivered, at what price, and whether that price was NSE's or estimated.
  - **What the labels mean:** one plain sentence per label, including the M4 ones (SPAN-like margin, placeholder calibration, assumed volatility, delivery margin approximate, short stock delivered, settlement price estimated).
  - **Fixtures:** a sample iron-fly run lets the page render without the API.
- **Checks.** Lint and `tsc` are clean. At an emulated 390 px width the page measures exactly 390 px wide, with no element past the edge; desktop was checked too. `next build` cannot run inside a worktree, because Turbopack rejects the `node_modules` junction; it has to run in the main checkout.
- **Greeks of open positions at day end (WP 4.3, WP 4.4; D-55, option C).** `positions_eod.csv` gained `underlying_mark_paise` in both engines (byte-identical; goldens unchanged apart from the column). `GET /api/tests/{id}/greeks` takes the option's mark and its underlying's mark as the forward, strike/expiry/last trading moment and the dated risk-free rate from bundle.json, and Black-76 (`stockify.fno.black76`). Position Greeks: delta in units, gamma per ₹1, vega in ₹ per volatility point, theta in ₹ per day; a future has delta = its quantity. No IV → `iv_reason` and no numbers (the card says why in words). The "Greeks at day end" card totals per underlying and leaves out, with a note, positions without an IV.
- **Expiry after the run's last day.** Such an option has `last_trading_ns = null` in the bundle (both `bundle.py` and `golden.py` only fill it when the expiry date is a run day). The API then estimates the close as the expiry date at the IST time of day the instrument's group closes on that trade date (IST has no daylight saving: close + whole days) and marks the row `expiry_close_estimated`.
- **Finding, not fixed here (reported to the supervisor):** the strategy chain view (`oracle/context.py` `_compute_chain`) and SPAN-lite (`fno/spanlite.py`) fall back to `t = now` when `last_trading_ns` is null, i.e. they value every option expiring after the run's last day as if it expired this instant (`MIN_T_NS`). That bends IVs, Greeks and SPAN-lite scans for such runs, in both engines alike (so parity does not catch it). **Fixed in CB 4.5 (D-61):** `bundle.py` and `golden.py` fill `last_trading_ns` for every derivative (the expiry date's dated FO close, `calendar.session_close_ns`), both engines refuse a bundle without it, and the two fallbacks in each engine are gone (C++: `engine.cpp` chain view and `margin/spanlite.cpp`; one ctest fixture gained the field). Bundles of G09, G12, G14, G16, G20 change hash; no expected values change. The API's close estimate remains only for run folders made before D-61.

## 2026-11-08 — M4.9 SPAN-lite calibration tool (IWB 4.1)

- Recon: `margin` was a CLI stub; `margin_calibration` existed in lab.sqlite (A1 §4) with no writer; Kite is reached
  only through `kite_client.get_client()` (token store in `%LOCALAPPDATA%\STOCKIFY\kite`), as the G08 charge
  capture does; kiteconnect is absent in tests.
- `python -m stockify margin calibrate --underlying NIFTY --samples 200 [--seed N]` (`stockify/fno/calibrate.py`).
  Refuses (exit 2) when Kite is not set up or the login is stale, before any call. During market hours it:
  reads the index, nearest future and the two nearest expiries' chains (ATM ± 8 strikes) from refdata and prices
  them with `quote` (≤ 1 request/s); samples seeded portfolios of 8 types (single legs, straddles, strangles,
  verticals, iron flies, iron condors, futures, hedged futures); asks `basket_order_margins(…,
  consider_positions=False)` for each (≤ 10 requests/s); stores each sample (portfolio, the legs' quotes, Kite's
  answer, our T0 and T1) via `store.add_margin_calibration`; fits PSR, VSR and somc_per_lot (golden-section
  coordinate search, 3 rounds, on the mean relative error against Kite's `final`); writes a plain-words report
  to `$LAB/margin_calibration/<underlying>-<date>.md` (median and 90th-percentile error per portfolio type, for
  the flat model, SPAN-lite now and SPAN-lite fitted).
- 🛑 **Never adopted automatically**: the fit is appended to `margin_params.json` → `spanlite_pending` (dated,
  `source: "calibration <date>"`, `calibration_id: calibration-<date>`, `pending_review: true`, `verified: false`,
  with the sample count and mean error). No margin model reads that key; the owner adopts a row by moving it
  into `spanlite`. History is kept: samples append to lab.sqlite, pending rows append to the list.
- Only read-only endpoints are used (quotes, basket margins); no order method exists in the tool. Nothing about
  the login is logged or stored (a test plants a fake access token and checks the database, the report and the
  config file).
- Our margins on the snapshot reuse the oracle's own code: a small snapshot engine feeds the real Context (chain
  view, forwards and mid IVs) and the real FlatMargin / SpanLiteMargin, so calibration fits the model the runs use.
- Tests (`test_calibrate.py`): a fake Kite answers quotes with Black-76 prices on a smile and basket margins with
  SPAN-lite under known settings (PSR 8 %, VSR 30 %, ₹3,000 per lot); from the placeholders, the fit lands within
  0.01 of the true PSR and within 3 % mean error. Also: the snapshot (strikes, quotes in paise, rate limits),
  every portfolio type, NRML/MARKET orders, pending rows never read, and the CLI refusing without Kite.
- Found: 10 and 24 November 2026 are NSE holidays in the calendar, so a contract dated on a holiday has no last
  trading moment from the calendar; real contracts carry refdata's `actual_expiry`, which the snapshot uses first.
- Pending: the first real calibration (owner, market hours, from Monday 9 November IST), then the owner's review
  of the report and adoption of the pending row.


## Options how-to (ENG 4.1–4.2)

- `docs/plan/HOWTO_options.md`, linked from HOWTO_strategy.md: the chain view with X's short_straddle_stop, flat and
  SPAN-lite margin with every label's meaning, expiry (cash at the FSP, stock delivery, the ₹23.60 expiry fee), the
  comparison tool, the calibration session and how to adopt a pending row. `test_howto_options.py` ties the doc to
  the code: the quoted code is the module's, every chain-view key and every label the UI explains is covered, the
  quoted config numbers are the config's, and adopting a row as described passes the config check and changes
  the engine's margin from its date on, not before.
- New examples: `eod_straddle_flat_vs_spanlite.json` (runs offline on October 2026's NSE files; flat blocks at most
  ₹3,59,629, SPAN-lite with placeholders ₹1,54,342, same trades and money) and `short_straddle_nifty_recorded.json`
  (needs recorded ticks for 11 Nov 2026).
- Found and fixed (ENG 4.1): a second calibration on the same day appended a second pending row with the same
  `applies` and `effective_from`, which makes the config loader refuse `margin_params.json` and so every run.
- Found: with 10 Nov a holiday, NIFTY's weekly expiry moves to Mon 9 Nov, so the first good calibration day is
  Wed 11 Nov IST, not Mon 9 Nov. The recorded comparison's dates 9–13 Nov were only four trading days; now 9–16 Nov.
- Found: the `margin_spanlite:` label names every SPAN-lite row in the file, so after an adoption it still reads
  `…+placeholder` (and the UI still warns "not yet calibrated") even for runs that only used the fitted row.
  Documented as is; changing it changes labels in goldens and both implementations.

## SPAN-lite label names only the rows a run can use (MEZ 4.3)

- `bundle._spanlite_calibrations`: for each derivative's underlying and each run day, the row SpanLiteMargin.params
  picks (a row naming the underlying wins over its class); the label joins their calibration ids. A spanlite run
  with no futures or options is labelled `margin_spanlite:none` (before: `placeholder`). Labels are sorted at the end
  of resolve_bundle, so where the label is appended does not matter.
- Expectations changed: only test_spanlite's bundle test, which resolved a RELIANCE-only run and expected
  `placeholder`; it now resolves the NIFTY future. No golden carries this label (goldens build their own bundles),
  and the web fixture's `margin_spanlite:placeholder` stays valid. The /tests page explains `none`.

## 2026-11-09 — first live SPAN-lite calibration against Kite (IWB 4.3)

- Run by the supervisor during the 2026-11-09 IST session (owner authorised, owner logged in to Kite; read-only endpoints only: quotes and basket margins; no order method exists in the tool). `refdata sync` first (51,670 rows, 2,464 new). NIFTY with `--skip-expiring` (9 Nov was NIFTY's weekly expiry: priced 17 and 23 Nov), BANKNIFTY without (priced 23 Nov and 29 Dec); 200 portfolios each, seed 0, at about 09:50 IST. Reports: `$LAB/margin_calibration/NIFTY-2026-11-09.md`, `BANKNIFTY-2026-11-09.md`.
- Average error against Kite's `final`: NIFTY 13.1 % with the placeholder rows → 4.4 % fitted (price move 0.0797, volatility move 0.1473, ₹60 minimum per short lot); BANKNIFTY 9.2 % → 4.7 % (0.0735, 0.4558, ₹95). Option portfolios (single legs, straddles, strangles, spreads, iron flies, condors) fit within about 1 % at the median and 3 % (NIFTY) / 13 % (BANKNIFTY straddles, strangles) for 9 in 10. The flat T0 model is off by 100–500 % on the same option portfolios.
- Weak spot, open: futures stay 8–14 % off even fitted, and futures hedged with options 26 % (NIFTY), so one price-move setting cannot serve both options and futures. A model gap, not a settings gap: likely a separate futures scan/ELM treatment. To look at before adopting for futures strategies.
- The volatility move differs a lot between the two indices (0.15 vs 0.46), which suggests one day does not pin it down. A second calibration on another day (the next good one: Wed 11 Nov IST, needs the owner's Kite login Tue 16:30–19:30 PST) would show whether the settings are stable.
- The two fitted rows are committed under `spanlite_pending` (verified: false). No model reads them. 🛑 Adoption waits for the owner's review of the reports.

## SPAN-lite refit from the first live calibration (IWB 4.5, D-62)

- Target: `calibrate.kite_target` = Kite's final span + exposure + additional (paise). `final.total` adds
  `option_premium`, the premium of bought legs, which a run debits as cash; long single options therefore have
  target 0 and drop out of the fit (13 of 200 samples per index on 2026-11-09). Each new margin_calibration row's
  market_json `snapshot` holds `target`, `target_paise` and every quote of the snapshot (`quotes`).
- `margin calibrate --refit --date D` (`calibrate.refit`): no Kite client at all. It rebuilds the snapshot from
  refdata and the stored rows (`snapshot_from_rows`), fits again, writes `<und>-<D>-refit.md` beside the live report
  (the live one is kept) and replaces that day's pending row. Rows from before IWB 4.5 carry only their own legs'
  quotes (27 instruments per index on 2026-11-09); rebuilt from those, SPAN-lite at the live settings equals the
  stored T1 on 140/200 (NIFTY) and 159/200 (BANKNIFTY) samples and is within 0.09 % on the rest (the 17-Nov
  synthetic forward uses fewer strikes).
- `psr_futures_pct` (D-62), oracle `spanlite.py` and engine `spanlite.cpp`: futures legs move by it, options by
  `psr_pct`; absent = `psr_pct`, so placeholders, goldens and fuzz are unchanged. Byte parity checked with it set
  (test_spanlite_parity). Fitted on plain futures after PSR/VSR/SOMC (`FUTURE_KINDS`).
- Tried and dropped: `extreme_multiple` 2 or 2.5 instead of 3 (options need a bigger PSR, futures get worse: 17–19 %).
- Refit 2026-11-09 (pending, not adopted): NIFTY psr 0.0951, futures 0.0886, vsr 0.3222, ₹3,157/lot, mean error 3.9 %;
  BANKNIFTY psr 0.0963, futures 0.0881, vsr 0.5646, ₹3,033/lot, 7.4 %. The minimum per lot binds on ~60 samples
  per index, so it is determined.
- **Open item: hedged futures stay off (2026-11-09: BANKNIFTY ~52 %, NIFTY 7.5 % too high). Kite credits bought options' value in SPAN (net option value / long option credit); not modelled; candidate for a future SPAN-lite improvement or M4.10.** Detail: hedged futures (NIFTY 7.5 %, BANKNIFTY 51.8 % off). Kite's SPAN credits the bought option's value; one
  BANKNIFTY long future + long ITM put has SPAN 0. SPAN-lite has no such credit (exact SPAN, M4.10, would).

## Bought options need no SPAN-lite (MEZ 4.5, bug fix)

- SPAN-lite charged a book of bought options a scan of almost their whole value (the −1 × PSR move wipes most of
  it out), on top of the premium a run already pays in cash: an option-buying run with SPAN-lite blocked the
  premium twice and could be refused for lack of funds. Kite's SPAN is 0 for every bought single (24 of 24 stored
  samples on 2026-11-09). Fix, in `spanlite.py` and `spanlite.cpp` alike: when every leg of an underlying is a bought
  option, its scan is 0 (SOMC, ELM and the expiry add-on were already 0 for them). A loss could never exceed the
  model's own value of the options anyway, so this only removes the double charge; mixed books are unchanged.
- Under the net-option-value form tried for MEZ 4.4, 11 of 12 NIFTY bought singles still came to ₹1: the scan is
  rounded up to the rupee before the credit, so scan − value was a few paise. The explicit rule above avoids that.
- Checked on the stored data at the pending settings: 12/12 NIFTY and 12/12 BANKNIFTY bought singles at 0; every
  other type's error unchanged. Expectations that moved: none. No golden stores SPAN-lite values (the fuzz worlds
  use the placeholders but compare oracle with engine; test_api_m4 reads a hand-written CSV); the parity tests,
  the comparison test and the how-to example (short options only) are unchanged.

## Net option value in SPAN-lite (MEZ 4.4, D-63)

- `net_option_value: true` in a spanlite row: scan = max(0, worst loss − Σ units × base option value), oracle and
  engine alike (same summation order; parity tested with it set). The calibration fits this form (`NET_OPTION_VALUE`)
  and the pending rows carry it.
- Forms tested offline on the stored 2026-11-09 samples, each refitted (median |error| vs Kite span + exposure):

  | Form | NIFTY hedged | BANKNIFTY hedged | Options NIFTY / BANKNIFTY | Futures |
  |---|---|---|---|---|
  | none (D-62) | 7.5 % low | 51.8 % high | 1.4–7.6 % / 1.3–6.1 % | 0.1 / 0.0 % |
  | A: max(scan − bought value, SOMC) | 34 % low | 0.1 % | ≈ none | same |
  | B: max(max(scan, SOMC) − bought value, 0) | 34 % low | 0.1 % | flies, condors, spreads up to 14 % | same |
  | C: net option value (adopted for the fit) | 7.1 % low | 65.7 % high | 0.1–3.2 % / 0.1–1.4 % | same |
  | C with one PSR (no futures move) | 34 % low | 0.1 % | as C | 8–14 % low |

- Evidence that no single credit rule fits hedged futures (Kite's span per unit of quantity, 2026-11-09):

  | Pair | Future | Strike | Premium | Kite span / unit | Ours (C) scan − bought value / unit |
  |---|---|---|---|---|---|
  | BANKNIFTY long fut + long 59200 PE | 59,104.00 | 59,200 | ₹870.22 | ₹0.0 | ₹778.0 |
  | BANKNIFTY short fut + long 59200 CE | 59,104.00 | 59,200 | ₹743.12 | ₹369.4 | ₹969.1 |
  | NIFTY short fut + long 17-Nov 24950 CE | 24,954.50 | 24,950 | ₹262.00 | ₹151.9 | ₹239.8 |
  | NIFTY long fut + long 17-Nov 24950 PE | 24,954.50 | 24,950 | ₹277.00 | ₹257.2 | ₹203.9 |

  The bought put gets a full credit on BANKNIFTY and almost none on NIFTY; any form that fixes one index's pairs
  breaks the other's. Hedged futures stay not modelled (D-62's open item).
- Refit 2026-11-09 with MEZ 4.5 and D-63 (pending, not adopted):
  NIFTY psr 0.0799, futures 0.0886, vsr 0.5061, ₹1.73/lot, mean 2.1 %;
  BANKNIFTY psr 0.0735, futures 0.0881, vsr 0.3096, ₹1.73/lot, mean 8.4 % (7.4 % before: hedged futures 51.8 → 65.7 %).
  BANKNIFTY's 90th percentile on straddles and strangles rose (6.4 → 8.0 %, 5.0 → 8.4 %) while their medians fell
  to 0.1 %.

## 2026-11-09 — the margin staircase: real numbers (ENG 4.3, D-64)

**NSE SPAN archive** (`$LAB/nse/margin/span`, end-of-day `.s` files, fetched with `stockify nse margin-files --backfill`):
- Range 2009-01-02..2026-11-06: 4,399 files stored, 212 days missing (holidays, 10–19 a year), 1 rejected, 22 GB.
- The rejected file is 2021-10-11 `.s`: NSE served a 7.7 MB body that is not a zip (HTTP 200). The fetcher refused it and archived nothing. That one file is why the backfill command exits 1.
- Intraday files (i1–i4) and the ELM file are kept for recent days only (S-7). The first contract-wise ELM file is available about 1 Sep 2026.

**Parse time.** A 54 MB end-of-day file, filtered to NIFTY and BANKNIFTY: 2.3 s and about 25 MB of memory at peak (3 runs). All 247 commodities: 3.6 s. A long `span_exact` run parses each usable file once.

**Validation against 405 stored Kite basket answers (2026-11-09)** (`python -m stockify margin span-validate`):
- SPAN: 405/405 within the gate, worst error ₹0.85. With the net option value (NOV) term inside SPAN; without it only 50/405 match, so Kite's SPAN includes NOV.
- Long-only books: 24/24 give 0. Mixed scan tiers: 25/25.
- Exposure: ELM % × units × one price (futures price for futures, index price for sold options, 0 for bought options) matches every charged leg. At the stored snapshot price only 43/405 are within ₹1, because Kite priced at its live quote (drift up to 0.08 %). The ₹1 check cannot be met from stored data, so it was ruled passed. Future calibration rows should store each answer's own quote.
- Expiry-day add-on (X-9): 0 of 405 answers have an expiry-day leg (the legs expire 17 Nov, 23 Nov and 29 Dec), so it is not validated against NSE.

**Scorecard** (`python -m stockify margin scorecard`, all 405 answers, error = |ours − Kite's|):

| Step | Priced | Median error | p90 | Median relative |
|---|---|---|---|---|
| B1 exact SPAN + NSE ELM | 405 | ₹14.17 | ₹40.90 | 0.01 % |
| B2 exact SPAN + rule ELM | 405 | ₹14.17 | ₹40.90 | 0.01 % |
| B3 SPAN-lite, calibrated | 0 | - | - | - |
| B4 SPAN-lite, placeholder | 405 | ₹14,150.14 | ₹1,22,315.33 | 26.20 % |
| B5 flat | 405 | ₹1,84,640.10 | ₹5,33,187.44 | 296.05 % |

B3 was unscored when this table was taken: no calibrated row was adopted yet. It is adopted since (D-74); the scorecard now shows B3 with a median error of ₹360.39 over the same 405 answers. Four groups show an inversion (B5 closer than B4): NIFTY and BANKNIFTY futures, and single options. SPAN error is at most ₹1.00 everywhere.

**P-04 per-step coverage** (200 seeds, 4000–4199, oracle and engine agree on every seed; 76 staircase/span_exact worlds; strict `span_exact` runs: 20 priced, 2 failed alike on both):

| Step | Run-days with a check | `margin_daily` rows |
|---|---|---|
| B1 | 112 | 47 |
| B2 | 31 | 5 |
| B3 | 72 | 26 |
| B4 | 42 | 12 |
| B5 | 42 | 16 |

**Known intermittent test failures** (each passes alone; watch them in full-suite runs):
- `test_runs_launch`: `created_ns <= started_ns` can fail by 1 ms when two suites run at once.
- (Explained and fixed in ENG 4.4: the `test_nse_margin_files` "rejected list" failure came from `gzip.compress`
  stamping the wall-clock second into each test ELM body, so a test that built its routes twice saw a changed file
  when a second boundary passed. Test archives now use `mtime=0` and fixed zip member times.)

## 2026-11-09 — final fix round on the staircase branch (CAM 4.4, MEZ 4.12, ENG 4.4)

**What the code does now (read before relying on the archive):**
- **Fetch checks the whole file** (`nse_margin_files.span_check`, `elm_problem`): a SPAN zip must pass every CRC and
  hold exactly one `.spn`; an ELM gzip must unpack to the end as UTF-8 CSV rows of one width (≥ 7 columns, at least
  one data row). A bad body is rejected and never archived, so it can no longer block the right file for good.
- **Body cap:** `nse_files.Http` streams every body and stops at `max_bytes` (default 64 MiB; real SPAN zips are
  ~10 MB). Over the cap is an error and nothing is archived.
- **Created time plausibility (L-4):** a SPAN file of business day B must say it was created between 12:00 IST on the day
  before the previous trading day of B (a Monday: Thursday 12:00) and B+1 06:00 IST. The first ruling, B−2 12:00, was
  wrong (CAM 4.5): NSE makes a Monday's i1 on Friday night or early Saturday. Real stamps (IST): 2026-06-01 i1
  202605292247 (Fri 22:47); 2025-01-06 i1 202501040159 (Sat 01:59); 2026-10-26 i1 202610240125 (Sat 01:25);
  2026-11-02 i1 202610310013 (Sat 00:13). The extra day covers a weekday holiday the calendar lacks (pre-2024). Fetch rejects it; an already archived file outside the window is left out of bundles and recorded in
  `span_skipped`. Not yet checked against the real 2009–2026 archive (this round did not open the lab). **Controller /
  owner check (read-only):**
  `.\.venv\Scripts\python.exe -c "from datetime import date; from stockify.data import calendar as c, nse_margin_files as m; bad=[]; [m.in_force_files(d, bad) for d in c.trading_days('FO', date(2009,1,1), date(2026,11,6))]; print(len(bad)); print(bad[:20])"`
  If NSE re-stamped old `.s` files later than B+1 06:00, they show up here and the window needs a dated exception.
- **The opening `.s` (I-2 ruling):** a bundle day D opens with the `.s` of the previous NSE session: walk back over
  calendar trading days with no archived file of any suffix (at most 5) and stop at the first day with any file
  (its `.s`, else nothing). Before 2024 the calendar has no holidays, so the day after each holiday (10–19 a year)
  used to get no SPAN at all; it now gets the `.s` before the holiday.
  - Known exception: 2021-10-11 has no file because NSE served a non-zip body (the one rejected backfill file), so
    2021-10-12 opens with 2021-10-08's `.s` (one session stale) until that day's `.s` is fetched again.
  - **Owner item:** add the 2009–2023 NSE holidays to `stockify/config/calendar/nse_holidays.csv` with cited
    sources (the backfill's 212 missing weekdays are the candidate list). Then the walk-back is only a safety net.
- **ELM % form (M-3):** both models use an ELM % only as 1–3 integer digits with at most 8 fraction digits (no sign,
  space or exponent); `read_elm` keeps only such values. Strikes must be whole paise (L-6).
- **An unreadable archived ELM file** is skipped and recorded in `elm_skipped` (label `margin_elm:skipped_files`);
  that day steps to B2.
- **ELM timing (L-5):** D's contract-wise ELM file is assumed to apply from D's open (09:15). NSE's publish time
  for it is not verified (X-12 establishes only that it exists): if NSE publishes it after the session, B1's
  exposure on D uses end-of-day information. Small (the ELM %), but recorded here and in A1 §6.2 until a fact row
  pins the publish time.
- **Daemon:** `margin_files` now holds sleep on its trade date until the `.s` and ELM are archived or 22:00, retrying
  every 30 min (`retry_every_min`, now read), so the files are fetched the same evening. Before, the laptop hibernated
  after compaction and the job ran once at the next 06:00 wake, where one failure ended it.

**Bundle build time (I-1).**
- Stock-only bundles under the default `staircase` now open no SPAN or ELM file: empty sections, label
  `margin_span:no_derivatives`.
- Parsed entries are cached in `$LAB/cache/span` (key: file SHA-256, archive path, wanted contracts, format version).
  Measured on the trimmed fixture (80 KB `.spn`, 64 contracts), sandboxed, 20 run days × 3 files: cold
  1.3–1.9 s, warm (all hits) 0.65 s; the fixture parses in ~20 ms, so the cache mostly pays off on real files. For
  a real 54 MB file the parse is 2.3 s (above) and a cache hit costs about the SHA-256 of the ~10 MB zip (~3 ms
  here) plus loading the entry (~5 ms for a 0.8 MB entry). A one-year NIFTY run therefore goes from about 10 min
  (end-of-day files only) to seconds once its files are cached; the first run over a range still pays the parse.
- **Bundle size, not changed this round:** about 350 bytes per contract per entry (fixture: NIFTY 42 contracts →
  14.8 KB). A chain of ~2,000 bundle contracts gives ~0.7 MB per entry: ~175 MB for a year with one file a day, up to
  ~0.9 GB with five a day, written into every run and re-run folder and loaded whole by the C++ engine. Owner
  decision: de-duplicate entries by SHA, or keep only strikes near the money.

## 2026-11-09 — M5.0 recon (FB 5.1)

**BASE.** `4719ebe7c323c0864eeaadbc5fbd5dafe10aa2b7` "XI 5.0: plan: the M5 paper-trading daemon, task by task".
BASE = merge commit of stockify/margin-staircase into stockify/m4-fno (bd66c37 "CAM 4.5: the SPAN cache key
covers the parser's own code"), plus the M5 plan commit on top. Working tree clean: no uncommitted files (CLAUDE.md is
git-ignored; the `.venv` and `web/node_modules` entries are junctions).

**M5.0 box 1: token store (verified by reading code only; neither the token file nor the repo's env file was
opened).** One store: `kite_client._token_file()` = `<cache dir>/kite_token.json` (kite_client.py:50), i.e.
`%LOCALAPPDATA%\STOCKIFY\kite\` (kite_client.py:14). `save_token` (:124) writes `<file>.tmp` then `replace`s it
(atomic); `load_cached_token` (:132), `is_authenticated` (:179). `GET /api/kite/callback` ends in `save_token`
(kite_client.py:120, in the login exchange). The launcher reads only the file's mtime
(`scripts/run_stockify_daemon.ps1`: `$TokenFile` :32, `Wait-FreshToken` :94, called :140). The daemon reads it through
`kite_client` (`stockify/daemon/__init__.py:162,297,301`). Confirmed.

**M5.0 box 2: recordings. 🛑 live pending (owner/controller).** The real lab is `%LOCALAPPDATA%\STOCKIFY\kite\lab` (`stockify.paths.lab_dir()` resolves under `state_dir()`, which is `...\STOCKIFY\kite`). One recorded day exists, 2026-11-05 (about 19 MB): `ticks/archive/date=2026-11-05` and `ticks/landing/ticks_20261105.sqlite` (plus its -shm and -wal files). Controller's sandboxed replay (LOCALAPPDATA redirected to a copy of the lab): 2026-11-05 replayed with 16 instruments, 7,440 packets in 1,293 frames and 99 heartbeats; dropped 0, exit 0. The replay path works, but the day is a short session (about 20 minutes), not a full one. Box 2 stays 🛑 until a full live day is recorded and replayed. Replays must always run in a sandboxed copy of the lab, never against the real one, because a replay recorder could write into the irreplaceable tick store. The sqlite files were not opened, `ticks.available_days()` was not run against the real lab (the function only reads, but `paths.ticks_*_dir()` create directories on call), and `stockify daemon` was never run. Every offline test uses fixture days.

### Verified mismatch table (provisional: controller ruling under owner autonomy, for owner review)

All 23 rows of the plan's table were checked against BASE; none is contradicted by the code. Locations:

| # | Verified at BASE | Decision |
|---|---|---|
| 1 | `scripts/run_stockify_daemon.ps1:8` (exit 76 = wait for a fresh token, restart), `Wait-FreshToken` :94/:140; `stockify/daemon/ticker.py:9` (403 stops with a gap); reactor limit per RECON M1.0 | D-65 |
| 2 | `engine/bindings/engine.cpp:187,447-448` (`not_until_m5`; `load_sod_state`/`eod_state` raise `NotImplementedError`); `stockify/bundle.py:647` `"sod_state": None`; `stockify/runconfig.py:34,150` plain `seed`, no per-day seed | D-66 |
| 3 | `stockify/bundle.py:608` `resolve_bundle(cfg)`; `stockify/runconfig.py:85-86,136` requires non-empty `data.packs` | Task 8 |
| 4 | `engine/src/engine.cpp:562,571` handle only `price_band` and `SETTLE`; `stockify/oracle/runner.py:41` REFDATA is rank-only; `instrument_add` is only produced by `stockify/data/packs.py:84,352` and read by `bundle.py:356` | D-67 |
| 5 | `engine/src/source.cpp:86-100` `LiveBatchSource::push` throws `FormatError("pushed record out of order ...")` when `key < last` | D-68 |
| 6 | `engine/include/stocksim/event.hpp:72` subtype 3 `margin_requirement` exists but nothing in `engine/` or `stockify/` consumes it; `stockify/fno/calibrate.py:545` already passes `consider_positions=False` | D-69 |
| 7 | `stockify/store.py:19,22-24,81` inline `_SCHEMA` incl. `margin_live` and `parity_results`; `stockify/data/migrations/` holds `bars`, `landing`, `refdata` only (no `lab`) | Task 2 |
| 8 | no `server/routers/trade.py`, no `web/src/app/trade`; no commands table | Tasks 2, 28 |
| 9 | `engine/include/stocksim/event.hpp:72` has only subtypes 1-4 (no `strategy_control`); `stockify/store.py:43` `sessions.pause_reason` is a single column | D-70 |
| 10 | `stockify/daemon/replay_ticker.py:39,87,217-240` `kill` only sets `self.killed`; `Fault.duration_ns` at :89 is used for `disconnect` (:233) | D-71 |
| 11 | `stockify/daemon/scheduler.py:39-41` `CATCH_UP = ("nse_fetch", "margin_files")`, `CATCH_UP_DAYS = 7`; D-32 hibernate in `scripts/check_power.ps1` | Task 27 |
| 12 | `stockify/config/notify.json` = `{"schema": "stockify.notify/1", "channels": [], "events": {}}` (the plan says `{"channels": []}`; the file already has `schema` and `events`, minor); `stockify/daemon/reminder.py:39` imports `geo_monitor.email_notify` | Task 25 |
| 13 | `scripts/check_power.ps1`; no backup module in `stockify/` | Task 26 |
| 14 | `kite_client.py:191` `get_client()` returns a `KiteConnect`; no stripping | Task 3 |
| 15 | `stockify/daemon/ticker.py:85` `ticker.on_message = recorder.on_message`; `stockify/daemon/recorder.py:107,142-143` stamps `frame_no, recv_ns, mono_ns` | Task 11 |
| 16 | `engine/bindings/engine.cpp:410-413` `drain_outputs` | Tasks 12, 16 |
| 17 | `web/src/lib/useTicks.ts:18` `useTicks(tokens: number[])`; `server/schemas.py:26,42,268` token fields | Task 28 |
| 18 | `tests/` has `test_log_redaction.py` only; no U-10 secrets test | Task 3 |
| 19 | no CLOCK source for the daemon; `stockify/oracle/runner.py:39,41` CLOCK is an event kind only | D-72 |
| 20 | bundle/oracle carry no bar history in a state (`bundle.py:647`) | D-66 |
| 21 | as row 9 | D-70 |
| 22 | as row 10 | D-71 |
| 23 | `stockify/daemon/ticker.py:59,75-76` `RecorderSession` keeps `_disconnect_gap`/`_stall_gaps` for the landing file; no `gap_taps` | Task 13 |

Note on row 6: the staircase default and L1-L5 naming come from D-64 and the staircase spec (§5.2-§5.3); the code has
no L2.

### Provisional decisions D-65 to D-72 (provisional: controller ruling under owner autonomy, for owner review)

No D-rows are added to `00_overview.md` here; each task writes its own row when it implements the decision.
- **D-65** (row 1; Tasks 19, 27): wait in-process only before the first connect, running catch-up jobs meanwhile;
  after a live 403 journal the pause, record the rejected token file's mtime, exit 76; the launcher restarts at once and
  the new process waits for a newer token file.
- **D-66** (rows 2, 20; Tasks 4-6): full day-to-day state (ledger, subscriptions, pause flag, full bar history; size
  warning, no trimming); `seed_mode: per_day`; day by day equals the whole run on both engines.
- **D-67** (row 4; Tasks 8, 15): the start-of-day bundle holds the whole reachable universe; strikes listed mid-day get
  a timeline row and label `unbundled_instrument`, not a journal record.
- **D-68** (row 5; Task 10; amends A4 §1.1): a record that would sort before its predecessor gets `t = last_t + 1 ns`.
- **D-69** (row 6; Tasks 3, 20, 21): Kite answers always `consider_positions=False` (enforced by `ReadOnlyKite`); a
  fresh journaled L1 answer prices the current book's blocked margin; the pre-trade increment always uses the
  staircase; fallback L3 = B1/B2, L4 = B3/B4, L5 = B5; L2 deferred (log only).
- **D-70** (rows 9, 21; Tasks 7, 9, 12): REFDATA subtype 5 `strategy_control {enabled, reasons}`, journaled;
  `pause_reasons` is a set and carries across days in the start-of-day state.
- **D-71** (rows 10, 22; Task 18): replay `kill` drains then `TerminateProcess` on itself; the restart resumes after
  each session's last dispatched frame; faults with a duration resume at `at + duration`; live restarts journal
  `GAP(process_stop)`.
- **D-72** (row 19; Task 13): `CLOCK` derived from the stamps of dequeued frames and heartbeats; every injected record
  enters the FIFO with an enqueue stamp.

### Provisional roster (controller ruling; owner reviews)

FB recon · GK supervision, recovery and host · LCB Kite access and login · VOL day state (as M3.8) · BWM pause gate
(as M3.5) · REG day bundles (as M2.1) · HOL session lifecycle · DM journal and dispatcher · RM pins (as M1.5) · MEZ
live margin model · IWB Kite margin log (as M4.9) · CDM scheduler · RD parity · LW coverage · RW notifications and
summary · WP API and UI · XI bookkeeping.

### Provisional per-session stop rule (controller ruling; owner reviews)

`stop` journals `strategy_control(false, ["stopped"])`. Resting orders follow their validity to day end, and open
positions are held into the final EOD state, labelled `stopped_with_positions`. The day is finalised, the status
becomes STOPPED, and no further day bundles are made.

### T-1 login note

The owner is on PST since 2026-11-01. The login window is 16:30-19:00 PST; a login still works until 19:40 PST (the
daemon's retries cover it); the market opens at 19:45 PST. Nothing to do on NSE holidays. The first possible live day
is Wed 2026-11-11 IST; M5 is "built" until a live day passes, "done" after.

### Owner-only and live-gated items (none blocks a code task)

1. Review D-65 to D-72, the roster and the stop rule (this section).
2. Daily Kite login before each NSE trading day (window above).
3. A full live recorded day, replayed in a sandboxed lab, for M5.0 box 2 (2026-11-05 replays with dropped 0, but it is a ~20-minute session), and the real-data demo (the recorder going live, an open M1 item).
4. One real token expiry handled through the login flow (Task 19 step 6).
5. Live Kite margin answers journaled, `margin_live` rows, `margin check --live` (Task 21 step 6).
6. Notification channel and its secret in the repo's env file (Task 25 step 6).
7. Backup target; `retention_days` after a week of backups (Task 26 step 6).
8. D-32 power settings (`check_power.ps1`, may need elevation), `-SleepTest`, Windows Update active hours,
   `manage_stockify_daemon.ps1 Install` (Task 26 step 6).
9. Two live KiteTicker connections observed, a third refused (Task 30 step 5).
10. Five live trading days with journal parity passing (DoD, Task 30 step 5).
11. A journal parity failure on a real day (M5.10, E-01), if it happens.
12. Adopting the pending SPAN-lite calibration rows (M4.9, existing).
13. Push to the remote, only on the owner's "push".

## 2026-11-09 — Kite access rules: order routes stripped, budget, secrets scan (LCB 5.1, M5.14, D-73)

**SDK.** The venv has kiteconnect **5.2.2** (`kiteconnect/__version__.py`; checked by import only, no request). Its
`KiteConnect._routes` has 44 keys. The deny-list `kite_client.ORDER_ROUTE_KEYS` built from it has 21:
`orders`, `order.info`, `order.place`, `order.modify`, `order.cancel`, `order.trades`, `gtt`, `gtt.place`, `gtt.info`,
`gtt.modify`, `gtt.delete`, `mf.orders`, `mf.order.info`, `mf.order.place`, `mf.order.cancel`, `mf.sips`,
`mf.sip.info`, `mf.sip.place`, `mf.sip.modify`, `mf.sip.cancel`, `portfolio.positions.convert`. Kept although
order-named: `order.margins`, `order.margins.basket`, `order.contract_note` (read-only calculators, K-10). The 14 order
methods of 5.2.2 bound to refuse: `place_order`, `modify_order`, `cancel_order`, `exit_order`, `place_autoslice_order`,
`place_gtt`, `modify_gtt`, `delete_gtt`, `place_mf_order`, `cancel_mf_order`, `place_mf_sip`, `modify_mf_sip`,
`cancel_mf_sip`, `convert_position`.

**On a kiteconnect upgrade, re-check the deny-list.** Importing `kite_client` raises when the SDK's table lacks a
required writer key; `tests/stockify/test_kite_readonly.py` (`RealSdkTest`) pins the 5.2.2 table, the 21-key list and
the method names, and skips with a "re-check" message on any other version. Update `tests/stockify/kite_fakes.py`
from the new SDK after reviewing its new routes.

**Where.** `kite_client.strip_order_routes` (every client `get_client()` builds, and the login exchange's);
`stockify/kite_readonly.py` (`ReadOnlyKite`, `client()`), used by the daemon's price-band snapshotter;
`stockify/daemon/conn_budget.py` (tickets in `$LAB/ticker_connections/`, limit `kite_limits.json` →
`ws_max_connections` = 3, so at most two live): the daemon (`daemon`, exit 78 when refused), the API's ticker worker
(`api`), `kite check` (`dev`). Close and error reasons go through `stockify.log.redact` (URLs and token parameters
blanked) in the daemon, the API worker's status errors and `kite check`.

**Fix round 1 (LCB 5.2): allowlist on the prepared request.** The review found that a percent-encoded or
`..` path (`url_args={"exchange": "../%6Frders/regular"}`) passed every check above and reached the transport,
because `requests` decodes `%6F` when it prepares the URL. Now `kite_client._mount_guard` mounts a checking adapter on
`https://` and `http://` of the client's session; `check_prepared_request` decodes the prepared URL's path until it is
stable, resolves `.`/`..`, and lets through GET and only POST `/margins/orders`, `/margins/basket`, `/charges/orders`
and `/session/token` (the login exchange; logout and token renewal are not used and are refused). A write must also
be in its plain form (no `%`, no `..`). The earlier guards also resolve `url_args` and decode. Both margin calculators
send the literal `consider_positions=false`; with the SDK's own basket call `requests` would send `False`. The source
scans were widened (absolute and composed order paths, `kite.trade` outside kite_client.py, `kiteconnect` imports
other than `KiteTicker` and `__version__`, `type(...)(`, private `_post`/`_put`/`_delete`/`_request` outside the two
Kite modules, `KiteTicker(` only at the three budgeted sites, calls inside the deny-list block). They are a backstop;
the adapter is the guard.

**Limits of the guarantee.** Python cannot hide an object from introspection: code that mounts its own adapter on the
session, builds its own `requests.Session` with a token, or reads closure cells can still reach Kite. The scans flag the
obvious forms (`reqsession`, `kite.trade`, `KiteConnect`); strategies are checked before they are imported (LCB 5.3).
Whether `/margins/orders` honours `consider_positions` is unverified (Kite documents it for the basket only; live item
for Task 21).

**Live pending (owner).** Two live KiteTicker connections observed and a third refused (item 9 above, Task 30).

## 2026-11-09 — VOL 5.2 recon (Task 5, oracle day-to-day state)

**BASE** `513ace1`. What the oracle held across days, and so what `sod_state` (A1 §6.4) had to carry, found by reading
`stockify/oracle/runner.py` and proved by mutation (each removed part breaks the split-equals-whole tests):
- Internal events after the last `DAY_END` were popped and dropped (`runner.py` main loop), so a timer due overnight,
  on a weekend, or a daily bar built from ticks (bucket end = next day 09:15) was lost in a one-day run. They are now
  kept (`Engine.carried`) and handed over in firing order.
- `next_continuous_start` and the D-60 delivery schedule (`FlatMargin._offset`) look at days after the current one; a
  one-day bundle rejected every order queued overnight (`R_SESSION`). Hence `bundle.next_days`.
- `ctx.order(id)` sees every order of the run, final ones too (`book.orders`); `live_orders()` indexed every id from
  `min_live`, which a hostile `next_order` turned into a hang; it now keeps the ascending ids of the orders that are not
  final (`live_ids`), so nothing walks an id range (ST 5.1). Final orders are carried (`final_orders`).
- `charges.csv` numbered rows from 1 per run (`artifacts.py`); it now continues from `counters.charge_row`.
- `underlying_last` (reference prices, §7.2 fallback), `fsp_seen`, `dp_done`, the market's `s0`/`s1`, book and band,
  and the positions' insertion order (iteration order of settlement and margin legs) all affect later days.
- The weekend timer of the Task 4 review (M3) draws from the previous day's stream position in a whole run; carrying
  both PCG32 states reproduces it without changing any whole run (goldens 17/17, oracle/engine parity unchanged).

**Size note.** One bar row is 69.9 bytes of canonical JSON (measured: 8,085 realistic NIFTY-future minute rows,
565,002 bytes). One minute-bar series on an F&O instrument (385 bars a day, 21 days) is about **0.57 MB per
instrument-month**; a 5-second series about 6.8 MB; 20 minute-bar instruments about 11 MB a month, so the 64 MB
warning (`paper.json` → `sod_state_warn_mb`) comes after about 6 months. Nothing is trimmed (C-2).

**Fix round 1 (review of cb18bdb).**
- No shipped strategy implemented `save_state` / `load_state`, and the base returned `{}`. With `on_start` skipped on
  day 2, `sma_cross` raised `AttributeError` on its first bar. The base now saves every attribute (A2 §4.1).
- A one-day bundle also needs the previous trading day: a daily bar published at the next midnight is matched against
  its own date's `CONTINUOUS` start (`runner._match_bar` / `_match_eod` → `continuous_start(d)`). Without it the order
  was cancelled EOD unfilled (buy_and_hold, eod fills). Hence `bundle.prev_days`.
- `dp_done` and `fsp_seen` are needed only in narrow cases, each now with a world: a part-filled CNC sell resting past
  its close on daily bars (D-44) and charged at the next midnight; an `eod_close` fill of an expiring contract
  published after midnight. `cnc_releases` (always empty after `DAY_END`), last prices (the market LTPs) and
  `event_no` (never read) are no longer carried.
## 2026-11-09 - the engine is built with Control Flow Guard and SDL checks (ST 4.1)
- `engine/CMakeLists.txt`: compile flags gain `/guard:cf /sdl` (in `STOCKSIM_CXX_FLAGS`, so every target and the
  `flags_hash`); `add_link_options(/guard:cf /DYNAMICBASE /HIGHENTROPYVA)` for the link. `/fp:precise` and `/W4 /WX` unchanged.
- No new warnings or errors under `/sdl`; no code changes. `flags_hash` e910e917c8d189f4 -> 3052014b56f68721
  (a build fingerprint; nothing in the tests or goldens pins it). `dumpbin /headers /loadconfig` shows "Control Flow
  Guard", "Dynamic base", "High Entropy Virtual Addresses" and "CF instrumented" on `stocksim_run.exe`,
  `stocksim_tests.exe` and `stockify/_engine.cp312-win_amd64.pyd`.
- Speed, synthetic 600,000-event tick pack, median of 9: `noop_cpp` 488.6 -> 483.6 / 488.8 ms (two runs, within
  noise); Python no-op 1812.6 -> 1827.7 / 1791.2 ms; Python SMA 673.9 -> 672.7 / 674.4 ms. No measurable change.

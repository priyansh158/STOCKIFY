# 00 — Overview: goals, invariants, architecture, decisions

## 1. Goals and non-goals

**Goal.** A personal research lab for NSE strategies that answers one question honestly: *would this rule have made money after realistic costs, fills and margin, and does it keep doing so live?*

It does that with four capabilities that share one engine:

1. **Record** Kite market data every trading day (the only way to get intraday history for options, which Kite deletes at expiry).
2. **Test**: run a strategy once over historical or recorded data (backtest / replay).
3. **Paper trade**: run a strategy live and unattended against the real feed, with no real orders.
4. **Compare**: check that test results and paper results agree, and keep research honest (trial counts, out-of-sample checks).

**Non-goals (still out of scope).** Real order placement. Multi-user access. Exchanges other than NSE (BSE/MCX may come later; nothing here should make them impossible). Tick-perfect exchange simulation (Kite's feed does not allow it; see concept C-1).

## 2. Invariants (rules every task must respect)

| ID | Invariant | Why |
|---|---|---|
| I-1 | **No market rule is hard-coded.** Rates, lot sizes, tick sizes, freeze limits, session times, expiry weekdays, margin parameters and broker cut-offs live in dated config files or dated tables, each row carrying `effective_from`, `source` and `verified_on`. | NSE changed lot sizes, expiry days, session times and STT several times in 2024–2026 (see `01_facts.md`). Old backtests must keep using old rules. |
| I-2 | **The engine is deterministic.** Same bundle + same event pack + same seed ⇒ byte-identical deterministic artifacts. No wall-clock reads, no unseeded or library-dependent randomness, no iteration over unordered containers in anything that affects output, explicit tie-breaking everywhere. | Without this, no parity test and no bug report is meaningful. Concept C-9. |
| I-3 | **Money is `int64` paise; quantity is `int64` units** (not lots); rates are integers too (ppb, ppm, basis points). Floating point only for analytics (IV, Greeks, SPAN-lite internals, metrics). Charges are computed per component and rounded to paise by one documented rule (`A4_engine_semantics.md` §0, §5). | Ledger balances must reconcile exactly, and two implementations must agree to the paisa; floats drift and round differently. |
| I-4 | **Time is stored as UTC epoch nanoseconds (`int64`).** IST is used only at the edges (session logic, display, file names). Never use the machine's local time zone. | The owner's machine is in Seattle (PDT/PST with daylight saving); the exchange is in IST (no DST). Mixing them silently shifts data by 12.5–13.5 hours. |
| I-5 | **One price basis per run**: `raw` (recorded ticks, NSE bhavcopy) or `adjusted` (Kite historical candles, which Kite adjusts retroactively for corporate actions). A run never mixes them. | Mixing creates fake jumps at ex-dates. Concept C-5. |
| I-6 | **Data first, rules as validators.** Expiry dates, lot sizes and settlement prices come from data (Kite instruments, NSE files). Rules generate future dates and flag data that disagrees. | Exchanges move expiries for holidays and resize contracts mid-life; rules miss those. |
| I-7 | **Never fabricate market data.** No forward-filling, no interpolated ticks. A gap is an explicit `GAP` event; a missing quote means "no fill possible". | A simulator that invents prices produces fills that could not have happened. |
| I-8 | **Every run and session is reproducible from its manifest**: engine build id, Python version, config hashes, bundle hash, pack hashes, strategy source hash, parameters, seed. | So any result can be re-run months later and audited. |
| I-9 | **Processes talk through files and SQLite (WAL) only.** FastAPI never runs the engine in-process for tests; heavy work runs in subprocesses. | A C++ crash or a long replay must not take down the API. |
| I-10 | **Secrets never leave their store.** `api_secret` and access tokens are never logged, written into run artifacts, bundles, journals, or test fixtures. | Artifacts are meant to be shared and exported. |
| I-11 | **C++ never touches the network or SQLite.** Python resolves all inputs into a *run bundle* (JSON) and *event packs* (binary), and imports C++ outputs. | Keeps the C++ build small and portable, and makes every input hashable. Decision D-04. |
| I-12 | **Each milestone ends green**: tests pass, lint and builds pass, demo runs, plan docs updated. | Half-finished milestones compound. |
| I-13 | **Schema changes to data that already holds recordings are migrations**, never drops or rewrites. 🛑 before any migration touching `ticks/` or `refdata.sqlite`. | Recorded days are irreplaceable. |
| I-14 | **Approximations are labelled.** Any number produced by an approximate model (flat margin, LTP fills, EOD option fills) carries a label that reaches the UI and the run manifest. | So nobody, including future-you, mistakes a rough estimate for a measurement. |

## 3. Target architecture

```
                Kite Connect REST                           Kite WebSocket (max 3 connections per API key)
        (instruments, historical, quote, margins,                  │                          │
           charges, session/login)                          conn #1: API (UI LTP)     conn #2: daemon
                 │                                                 │                          │
┌────────────────┴─────────────┐   ┌────────────────────────┐  ┌───┴──────────┐   ┌────────────┴──────────────────────┐
│ FastAPI  server/             │   │ NSE public files       │  │ ticker_bridge │   │ market daemon  stockify/daemon/     │
│ auth, search, portfolio,     │   │ (bhavcopy, index close,│  │ → useTicks UI │   │  ticker supervisor (raw frames)   │
│ /api/tests  /api/trade       │   │  freeze qty, SPAN/ELM) │  └──────────────┘   │   ├─ recorder → ticks/landing     │
│ /api/data   /api/alerts      │   └───────────┬────────────┘                     │   ├─ paper sessions                │
└──────┬───────────────────────┘               │                                  │   │   journal (.evt) → _engine      │
       │ spawns subprocess                     ▼                                  │   └─ scheduler: sync, compaction, │
       ▼                          stockify/data/ (Python)                           │      bhavcopy, parity, reports    │
 python -m stockify run   ◄── refdata.sqlite · bars.sqlite · nse/ · ticks/ ──────────┴───────────────────────────────────┘
       │                              │
       │  run bundle (JSON) + event packs (.evt)
       ▼
 stocksim_core (C++20)  ── deterministic artifacts ──►  $LAB/runs/<run_id>/  ── imported ──►  lab.sqlite ──► UI
 (linked into stockify._engine via pybind11, and into the stocksim_run CLI)
```

### Processes

| Process | Started by | Owns | Never does |
|---|---|---|---|
| FastAPI (`server/`) | `run.ps1` (existing) | REST API, Kite login callback, UI LTP ticker, launching runs | Runs the engine in-process for tests; writes tick data |
| Market daemon (`python -m stockify daemon`) | `run.ps1 -Daemon` / Scheduled Task | Its own KiteTicker, recorder, paper sessions, scheduled jobs | Serves HTTP to the UI (it writes status rows the API reads) |
| Run worker (`python -m stockify run …`) | FastAPI subprocess, CLI | One backtest/replay | Talks to Kite |
| Geo monitor (existing) | `run.ps1 -GeoMonitor` | Unchanged | — |

### Data stores (all under `$LAB/`)

| Store | Content | Writer(s) | Details |
|---|---|---|---|
| `refdata.sqlite` | Instruments with full history, token map, dated contract specs, NSE EOD tables, settlement prices, freeze and daily limits, tick-day status, data quality | data jobs, daemon | `A1_schemas.md` §1 |
| `bars.sqlite` | Kite historical candles cache (adjusted basis) and detected corporate-action adjustments | `stockify bars fetch` | §2 |
| `ticks/landing/ticks_YYYYMMDD.sqlite` | Raw recorded packets for one IST day | recorder | §3 |
| `ticks/archive/date=YYYY-MM-DD/*.parquet` | Verified, compressed archive of each day | nightly compaction | §3 |
| `nse/raw/<dataset>/…` | NSE files and daily Kite instrument dumps exactly as downloaded (+ sha256) | NSE fetcher, instruments sync | §1.2 |
| `packs/<sha256>.evt` (+ `.json`) | Event packs: engine input, content-addressed | `stockify pack build` | §5 |
| `runs/<run_id>/` | Bundle, manifest, artifacts of one run | run worker | §6 |
| `journals/<session_id>/<YYYYMMDD>.evt` | Exactly what a paper session's engine saw | daemon | §5 |
| `lab.sqlite` | Runs index + imported results, sessions, timeline, parity results, trial registry | run importer, daemon, API | §4 |

### The two main data paths

**Research path.** Recorder writes landing SQLite → nightly compaction verifies and archives to Parquet → `pack build` turns the requested slice into an event pack → `stockify run` resolves a bundle and calls the engine → artifacts → imported into `lab.sqlite` → UI.

**Live path.** Daemon receives raw frames → the same frames go to the recorder and to each paper session → each session encodes its events into its journal *first*, then feeds them to its engine → outputs persisted → every night the journal is replayed through a fresh engine and must reproduce the live results exactly (concept C-10).

## 4. Milestones

| ID | Name | Delivers | Demo | Size | Status |
|---|---|---|---|---|---|
| M0 | Cleanup + toolchain | LLM pipeline removed; C++20/pybind11 builds on Windows (MSVC) | `python -c "import stockify._engine as e; print(e.build_info())"` | M | done 2026-11-04 |
| M1 | Data layer + recorder | refdata with history, NSE EOD ingestion, Kite bar cache, recorder, replay ticker, compaction, event packs, data-quality report | Record a live day (or replay a fixture), compact it, build a pack, print the quality report | L | not started |
| M2 | Python oracle | Readable Python backtester, golden scenarios, EOD option research from bhavcopy | SMA crossover on daily bars; short straddle on EOD bhavcopy | M | not started |
| M3 | C++ engine core (equity) | Clock, orders, three fill models, costs, ledger, metrics, C++ and Python strategies, runners, `/tests` UI | Same SMA run via C++ matches the oracle to the paisa | L | not started |
| M4 | F&O | Dated contract specs in the engine, Black-76 + IV, chains, settlement, margin T0/T1 (+T2 stretch), calibration vs Kite | Short straddle replayed on recorded chains with SPAN-lite margin | L | not started |
| M5 | Paper daemon | Sessions, journals, crash recovery, live margins, token-expiry handling, nightly parity, `/trade` UI | A session runs through a replayed day, survives a forced kill (`taskkill /F`), passes parity | L | not started |
| M6 | Reporting + research | Compare, export, `/data` coverage, trial registry, walk-forward, cost/fill sensitivity | Walk-forward report with deflated Sharpe for a sweep | M | not started |

## 5. Decision log

Each entry: what was decided, what changed from the original plan (if anything), and why. New entries go at the bottom with a date.

| ID | Decision | Changed from original? | Why |
|---|---|---|---|
| D-01 | Remove the 5-stage Claude analysis pipeline; keep Kite auth, instruments cache, ticker, portfolio, search, geo monitor. Kite Connect is the main data source; Next.js UI and FastAPI stay. | No | Owner decision. |
| D-02 | **Milestone order**: data + recorder (M1) → Python oracle (M2) → C++ core (M3) → F&O (M4) → paper daemon (M5) → reporting (M6). | Yes (original: C++ core before any research tool; recorder inside M1 but not prioritised) | Tick history only exists from the day recording starts. A small Python backtester gives a research tool in weeks rather than months, and becomes the reference the C++ engine must match. |
| D-03 | **Strategies can be Python (default) or C++.** The engine core (orders, fills, costs, ledger, settlement, margin) is C++. Python strategies run through a pybind11 trampoline. | Yes (original: strategies only in C++) | Research is an iteration game; a recompile per idea slows it. Kite delivers roughly one snapshot per instrument per second, so Python callbacks are not the bottleneck. |
| D-04 | **C++ never touches SQLite or the network.** Python builds a *run bundle* (resolved config + instruments + specs + sessions + charges for the run's dates) and *event packs*; C++ writes artifact files that Python imports. | Yes (original: C++ reads `bars.sqlite`/tick SQLite and writes `lab.sqlite`) | Smaller, portable C++ build; one input format for backtests and live journals; every input is hashable (I-8); schema changes stay in one language. |
| D-05 | **Integer paise, integer units**, one rounding rule for charges (I-3). | Yes (new) | Exact reconciliation; deterministic across runs. |
| D-06 | **Instrument identity is a surrogate `instrument_id`.** Derivatives are keyed by (exchange, underlying, type, expiry, strike); equities and indices by (exchange, tradingsymbol). Kite `instrument_token` is mapped with validity intervals. | Yes (original: cache keyed by token) | Zerodha states tokens are unique only for the current day and are reused after expiry (`01_facts.md` K-9). |
| D-07 | **Everything time-varying is dated** (I-1): charges, contract specs, freeze limits, sessions, expiry rules, margin parameters, broker cut-offs. | Extended (original dated only `charges.json`) | Lot sizes changed mid-life for live contracts in Dec 2025; F&O hours changed in Aug 2026; STT changed in Oct 2024 and Apr 2026. |
| D-08 | **Recorder tiers**: FULL mode for anything a simulation may trade (bid/ask, exchange timestamp, OI); QUOTE mode only for context instruments; never LTP mode. Raw packet bytes are stored alongside parsed fields. | Yes (original: quote by default, full for a small set) | Quote mode has no bid/ask and no exchange timestamp (K-3). Raw bytes let a parser bug be fixed later by re-parsing. |
| D-09 | **Storage**: per-day SQLite landing → nightly verified Parquet (zstd) archive → content-addressed event packs for the engine. Landing files are deleted only after verified archiving and a retention window. | Yes (original: SQLite only, "retention setting later") | Rough estimate 0.5–1 GB/day of landing data for ~300–400 instruments; decide the archive format before data accumulates. |
| D-10 | **Default fill model for options is quote-based** (cross the spread using recorded depth). LTP-crossing and bar models remain, labelled approximate where appropriate. | Yes | Option spreads are wide relative to premium; LTP fills systematically flatter option strategies (C-2). |
| D-11 | **Three parity gates**: determinism (same inputs → identical artifacts), journal parity (live session replayed → identical results), oracle parity (C++ and Python agree to the paisa on golden and randomised scenarios). | Yes (original: determinism test only) | Live and backtest use different drivers; only parity proves they agree (C-10). |
| D-12 | **NSE public EOD files are ingested from M1** (F&O and CM bhavcopy, index closes, quantity freeze; SPAN/ELM files when their public URLs are verified). | Yes (new) | Free, years deep, official settlement prices and lot sizes, and they allow daily-resolution option backtests immediately. |
| D-13 | **Final settlement prices are never computed**; they come from NSE published data. | Yes (new) | The closing price methodology changed on 2026-08-03 (closing auction for F&O stocks); reproducing it is neither possible from Kite data nor necessary. |
| D-14 | **Margin model tiers**: T0 flat % (default until M4), T1 SPAN-lite scenario margin (M4), T2 exact SPAN from NSE risk-parameter files (stretch), LIVE = Kite basket-margin API for paper sessions. T1 is calibrated against LIVE. | Yes (original: flat % only, Kite API in paper) | Margin decides position size, so a wrong margin model distorts every return number for hedged books (C-12). |
| D-15 | **Replay ticker**: a fake ticker that streams recorded days at 1×–N× speed through the real daemon code. | Yes (new) | NSE trades 20:30–03:10 Seattle time (PDT, pre-open included); development and end-to-end tests must work in daytime. |
| D-16 | **Paper sessions are journal-first** and recover from crashes by replaying their journal. Daily Kite login is a fixed constraint; per-day *coverage* is a first-class metric. | Extended | Official Kite login is a browser flow each day (K-7); gaps must be visible, never hidden. |
| D-17 | ~~**Portable C++** (MSVC 2022 and GCC ≥ 12 / Clang ≥ 15), CMake presets for both, CI builds both.~~ **Superseded by D-26.** | Yes (original: MSVC only) | Keeps the option of an always-on Linux box or VPS for the daemon (M5). |
| D-18 | **Price-basis rule** (I-5) and a corporate-action detector for the bar cache. | Yes (new; corrects earlier advice) | Kite's historical candles *are* adjusted for corporate actions, retroactively (K-6). Earlier advice in chat said they were not; that was wrong. |
| D-19 | **Research hygiene is built in**: every run belongs to a trial family; reports show trial counts and deflated metrics; a holdout window is locked by default. | Yes (new) | Backtest overfitting is the most common way research lies (C-14). |
| D-20 | Two KiteTicker connections in normal operation (API for UI LTP, daemon for recording/sessions) out of three allowed. Development uses the replay ticker. | Refined | Avoids silently exceeding the connection limit during development. |
| D-21 | SEBI's retail-algo rules (static IP for order APIs, mandatory market protection, order-rate limits) are documented but not implemented, because the lab places no real orders. The fill model *simulates* market protection. | New | Keeps the door open for live trading later without paying for it now. |
| D-22 | **Run bundle**: the C++ engine consumes one resolved JSON bundle per run (or per session day) and nothing else besides packs. The bundle is archived with the run. | New (consequence of D-04) | Both runners (Python and the C++ CLI) see identical resolved inputs; a run can be re-executed from its folder alone. |
| D-23 | **Prices that are not a multiple of the tick size are rejected (`R_TICK`), not rounded.** | Yes (original: "tick-size rounding" as an order check) | Kite rejects such orders; silent rounding would hide strategy bugs. |
| D-24 | **A4 is written for exact parity** (after an independent review of v1): integer rates in ppb/ppm/bp, 128-bit intermediates, one rounding rule; CSV artifacts contain no floats; one canonical float format for `metrics.json`; explicit eligibility rules against intra-bar look-ahead; a validity check on Kite's interval VWAP; a single `(t, rank, src, seq)` event ordering including internal events; PCG32 specified bit for bit. | New (2026-10-29) | Decimal-vs-double rounding, float formatting and loose ordering rules would each break "oracle and engine agree to the paisa". |
| D-25 | **Paper sessions run one engine per session-day**, finished after the evening settlement records; recovery replays that day's journal from its start-of-day state. | New | No mid-day engine serialization; settlement data that arrives hours after the close is still journaled in order. |
| D-26 | **Windows only, permanently.** The engine builds with MSVC only (presets `win-msvc-debug` / `win-msvc-release`, Ninja generator inside the VS developer shell); no Linux presets, no GCC/Clang flags, no `scripts/build_engine.sh`, no systemd, no hosted CI (M0.5 dropped). Cross-platform determinism checks (A3 DT-02, DT-04) become oracle-vs-engine and manual time-zone checks. The daemon host is the Windows machine (M5.13 option A). | Yes (replaces D-17) | Owner decision, 2026-11-02. `stockify_paths` already requires `LOCALAPPDATA`; keeping one platform keeps the toolchain and tests small. |
| D-27 | **`run.ps1 -Daemon` restarts the daemon after a crash** with backoff (5 s doubling to 5 min, reset after 10 min of uptime), through `scripts/run_stockify_daemon.ps1`. The geo monitor keeps its existing behaviour. | Yes (the plan assumed `-GeoMonitor` already restarted with backoff; it does not — `run.ps1` stops everything when a child exits) | The daemon must stay up unattended (M5). |
| D-28 | **M0 landed as one commit** (`stockify/m0-cleanup` branch) together with the pre-existing uncommitted Windows-paths / geo-monitor / test-suite work, instead of one commit per task. | Yes (README protocol step 3) | Owner decision: resolve the uncommitted work and the overhaul once, in one clean commit. Later milestones go back to one commit per task. |
| D-29 | **`CLAUDE.md` is never committed** (repo `.gitignore` and the owner's global git ignore). The M0.6 rewrite is a local, untracked file. | Yes (M0.6 assumed a tracked file) | Owner rule for every repository. |
| D-30 | **The platform is named STOCKIFY, inside and out.** The plan's former product name is gone from the UI, docs, logs and code. Internal names follow: Python package `stockify/` (`python -m stockify`, `stockify._engine`), plan folder `docs/plan/`, tests `tests/stockify/`, data folder `stockify_paths.state_dir() / "lab"` (`$LAB`, store `lab.sqlite`), config schema tags `stockify.<file>/1`, launcher `scripts/run_stockify_daemon.ps1`, branches `stockify/mN-…`. The C++ target names (`stocksim_core`, `stocksim_run`, namespace `stocksim`) are unchanged. | Yes (the plan used a separate product name and `simlab` paths) | Owner decisions, 2026-11-02 (name) and 2026-11-04 (internal names). |
| D-31 | **Commit names follow a football-position roster** (§5a): every stage gets the position whose main job matches its role, similar stages get different positions, and titles read `<POS> <milestone>.<n>: summary` (`n` counts that position's commits within the milestone; `XI` = team-wide). | Yes (README step 3 used `M1.3: …`) | Owner decision, 2026-11-04: history should show which component each commit belongs to. |
| D-32 | **The daemon runs on the owner's laptop**: plugged in, lid closed (kept in a wardrobe). Power settings: AC lid = do nothing, battery lid = sleep, AC never sleep/hibernate on idle, AC CPU max 80% (heat), AC wake timers on. The daemon keeps the machine awake during sessions; **after the close and the day's verified compaction it hibernates the laptop** and schedules a self-wake for 06:00 IST on the next trading day (M1.5). Missed jobs run as catch-up on wake. The daily Kite login stays manual. | Yes (M5.13 option A assumed an always-awake machine) | Owner decisions, 2026-11-04. Modern Standby here disconnects the network, so plain sleep is useless; hibernate is zero-power and cooler in an enclosed space. |
| D-33 | **No sourced charge row = no charge = run refused.** F&O NSE transaction charges have no sourced row before 2024-10-01 (Z-3), so an F&O run on an earlier date fails with "no charge row" instead of borrowing a later rate. The M1.2 coverage test samples dates from 2024-10-15 on and asserts this hole explicitly. | Yes (M1.2 asked for 2024 sample dates to resolve) | Never invent history (I-1); add the earlier rows only from a source. |
| D-34 | **Market protection is configured in basis points**: `broker_policy.json` → `market_protection_auto_bp` (200 = 2%, assumption, labelled), matching A4 §3.3. | Yes (A1 §7.5 had `market_protection_auto_pct` = "2") | One unit everywhere; A4 uses bp. |
| D-35 | **The F&O pre-open applies only to current-month futures**, plus next-month futures in the last 5 trading days before the current expiry (X-6). `sessions.json` marks the FO_FUT pre-open phase `scope: near_month_futures`; `calendar.futures_preopen_eligible` decides per contract. | Yes (A1 §7.3 / A4 §4.7 applied it to every future) | Follow the exchange circular. |
| D-36 | **`DAY_END` is 23:59:59.999999999 IST** as in A4 §1.2; `schedule.json` → `day_end` ("23:59:59") is only the scheduler's job time. | Clarified | The two values serve different purposes. |
| D-37 | **Archive row order: each Parquet row group covers a contiguous `seq` range; inside a group rows are ordered by `(instrument_id, seq)`, and integer columns use delta encoding.** Readers re-sort a row group by `seq` (`compact.iter_rows`); every A1 §3.2 column, including parsed depth, is kept. `meta.json` → `layout` records it. | Yes (A1 §3.2 said "sorted by `seq`") | Owner decision, 2026-11-05. Measured on the first real day: 4.8 MB → 3.6 MB per 5 minutes (landing 15.9 MB, 3.3× → 4.4×), about 0.29 GB per full day, roughly 4 years in 300 GB. |
| D-38 | **The verified archive is the record; the landing file is only the day's write buffer.** `storage.json` → `retention_days: 0`: after the close, once compaction verifies the archive, the archive is re-verified against the digest in `tick_days` and the landing file is deleted. A day still being recorded is never compacted or deleted. Every reader (replay, quality report, packs, backtests) reads ticks through `stockify.data.ticks`, archive first. | Yes (D-09 kept landing files until backups existed; M1.7 🛑) | Owner decision, 2026-11-05. The landing file sat on the same disk, so it was never a backup; the archive holds every raw byte, timestamp, gap and subscription. A backup target is still to be chosen. |

| D-39 | **Final settlement prices come from the F&O bhavcopy itself**: on expiry day every expiring contract of an underlying (future, calls, puts) carries one settlement price, NSE's FSP. Index → cross-checked against `ind_close_all`; stock → cross-checked against the CM official close. A difference above one tick goes to `data_quality` (`fsp_vs_close_paise`); contracts that disagree with each other go there too (`fsp_settle_spread_paise`) and no FSP is stored. | Yes (M1.3 said stock FSP = official close) | Evidence 2026-10-27: all 219 expiring futures had `SttlmPric = UndrlygPric`; expiring PAGEIND options settled at 34,559.76 while the CM close was 34,610.00. 18 of 213 stocks differed by more than a tick (closing auction, D-13). Indices all matched. |
| D-40 | **UDiFF bhavcopies are used from 2024-01-01**, legacy files before. | Yes (S-1/S-3 said 2024-07-08) | NSE back-published UDiFF to 2024-01-01 with lot sizes; legacy F&O has no lot size, so the M1.3 🛑 applies only before 2024 (backfill starts 2024-01-01). |
| D-41 | **Early expiry is data, not an error**: `instruments.actual_expiry` (refdata migration 0002) holds the day NSE really expired a contract when it differs from the scheduled `expiry` (UDiFF `FininstrmActlXpryDt`). `expiry` stays the contract's identity (Kite's symbol). Settlement, `expiries()` and `option_chain()` use the actual date; packs carry it in `instrument_add`. | Yes (new) | Found by the 2024–2026 backfill: IDFC (merger, 2024-10-09), ITC (hotels demerger, 2025-01-03), Siemens (demerger, 2025-04-04), ABFRL (demerger, 2025-05-21): every contract of the stock expired early at one settlement price. Legacy files do not carry the field. |

### 5a. Roster (D-31)

| Pos | Stage | Role on the pitch → role in STOCKIFY |
|---|---|---|
| GK | M0.4 process supervision | last line of defence; keeps the system up, restarts play after a crash |
| SW | M0.0 recon | reads the whole pitch from behind |
| RB | M0.1 analysis-pipeline removal | the clearance |
| CM | M0.2 `stockify` Python core | the link between the engine (back) and the API/UI (front) |
| ST | M0.3 C++ engine | the focal point everything is played into; does the finishing |
| LB | M1.0 recon | the second scout, other flank: checks prerequisites before the push |
| CB | M1.1 reference data and instruments | organises the back line; every identity passes through it |
| CDM | M1.2 calendar, sessions, rule configs | sets tempo and timing; shields with dated rules |
| LWB | M1.3 NSE end-of-day files | wide supply line from outside |
| RWB | M1.4 Kite historical bar cache | the mirror wing: the other external history feed |
| RM | M1.5 tick recorder | the tireless runner; covers every minute of the session |
| LM | M1.6 replay ticker | mirrors the recorder's runs on the other flank |
| CF | M1.7 compaction and archive | hold-up play: keeps the day's data safe for later |
| CAM | M1.8 event packs | the playmaker delivering the final ball to the striker |
| LW | M1.9 data-quality report | takes the ball to the touchline and exposes bad data |
| SS | M1.10 tracked defect close-out | taps in the rebound |
| RW | free (M2) | |
| XI | team-wide (renames, plan bookkeeping) | the whole team |

Positions for M2 onwards are chosen when each milestone starts; a stage that keeps the same role across milestones keeps its position.

## 6. Corrections to earlier chat advice

These came out of verifying facts for this plan (details and sources in `01_facts.md`):

1. **Kite historical candles are adjusted for corporate actions** (splits, bonuses, rights, spin-offs, extraordinary dividends), applied on the ex-date and retroactively. The real risk is the opposite of what was said earlier: cached bars go stale after an ex-date, and adjusted bars cannot be mixed with raw ticks or bhavcopy prices. Handled by D-18 and I-5.
2. **NSE session times changed on 2026-08-03**: F&O now trades until 15:40 IST, F&O stocks in the cash market stop continuous trading at 15:15 and close through a closing auction (15:15–15:35), and the derivatives close-price VWAP window moved to 15:10–15:40. An F&O pre-open auction for futures started 2025-12-08. All of this goes into dated `sessions.json`.
3. **STT on F&O rose again on 2026-04-01** (futures 0.05%, options 0.15% of premium on the sell side). This doubles the round-trip cost of a Nifty futures lot compared with the earlier rate (worked numbers in `02_concepts.md` C-11).
4. **Kite Connect's data plan is ₹500/month** (live + historical), not a separate paid historical add-on.

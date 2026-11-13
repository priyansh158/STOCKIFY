# A3 — Testing: layers, golden scenarios, parity, CI

Test IDs: **U** unit, **G** golden scenario, **P** property/differential, **DT** determinism, **E** end-to-end, **V** validation against reality (owner-run). Python tests use the repo's existing runner (`tests/run_tests.py`, `unittest` discovery, `SandboxedDBTest`, `isolated_env`); C++ tests use doctest under `ctest`.

---

## §1 Layers

| Layer | Where | Runs |
|---|---|---|
| U | `engine/tests/*.cpp`, `tests/stockify/**/test_*.py` | every commit |
| G | `tests/golden/*.yaml`, runner `python -m stockify golden run --impl oracle\|engine` | every commit (engine from M3 on) |
| P | `tests/stockify/property/` | every commit (small N), weekly (large N) |
| DT | `tests/stockify/determinism/` | every commit; cross-OS weekly |
| E | `tests/stockify/e2e/` (replay ticker, daemon, API) | every commit (fast fixture day) |
| V | owner-run tools; results saved as dated fixtures | when charges or margins change; monthly |

## §2 Golden scenario format

A golden is a tiny, hand-checkable world: a bundle, a few events, a scripted strategy, and expected outputs with a written explanation. Its bundle (including charge rows, specs, freeze limits and phases) is written inside the YAML, so a golden never depends on the live config files.

```yaml
id: G01
title: Market order on bar data fills at the next bar's open
semantics: ["A4 §3.10", "A4 §4.0", "A4 §4.1"]
bundle:
  dates: ["2026-03-02"]
  instruments:
    - { id: 1, exchange: NSE, kind: EQ, tradingsymbol: TEST, session_group: CM,
        specs: [ { from: "2026-01-01", lot: 1, tick_paise: 5 } ] }
  charges: zero                      # or a list of explicit rows (A1 §7.2 format)
run: { fill_model: { kind: bar, slippage: { kind: none } }, capital_paise: 10000000, latency_ms: 0, price_basis: raw }
events:                              # times in IST for readability; converted to UTC ns
  - { kind: BAR, instrument: 1, interval_s: 60, start: "2026-03-02T09:15:00+05:30", o: 10000, h: 10100, l: 9900, c: 10050, v: 1000 }
  - { kind: BAR, instrument: 1, interval_s: 60, start: "2026-03-02T09:16:00+05:30", o: 10010, h: 10060, l: 9990, c: 10040, v: 800 }
strategy:
  kind: script                       # built-in test strategy, same in both implementations
  actions:
    - { on_event: 1, place: { instrument: 1, side: BUY, qty: 10, product: CNC, type: MARKET } }   # events are numbered from 1
expect:
  orders: [ { order_id: 1, status: COMPLETE, filled_qty: 10 } ]
  fills:  [ { order_id: 1, qty: 10, price_paise: 10010, t: "2026-03-02T09:17:00+05:30", evidence: bar_open } ]
notes: |
  The 09:15 bar becomes known at 09:16:00 (start + interval, C-3); the order is submitted then.
  A bar is eligible only if the order was active at its start (A4 §4.0): the 09:15 bar is not,
  the 09:16 bar (start 09:16:00, known at 09:17:00) is. Market orders on bars fill at the open
  (100.10 → 10010 paise); protection (AUTO, ref = previous close 100.50) caps buys at 102.50, so it does not bind.
```

The `script` strategy (in `stockify/strategies/_script.py`, run on the engine through the Python trampoline) performs listed actions on given event numbers (1-based) or engine times, so goldens test the engine, not strategy logic.

## §3 Golden scenarios

G13 and G15 test the Python data layer (instruments, calendar), so they live in the Python test suite; the golden runner skips them for `--impl oracle|engine`. Every other golden runs on both implementations once the milestone that introduces its feature is done (M2.5 lists the first batch).

| ID | What it proves | Rules |
|---|---|---|
| G01 | Market order on bars fills at the next eligible bar's open | A4 §4.0, §4.1 |
| G02 | A limit exactly at the bar low: `through` = no fill, `touch` = fill | A4 §4.1 |
| G03 | Stops across a gap: SL-M fills at the open (worse than the trigger); an SL whose limit is gapped past does not fill; a timer order at 09:16:30 cannot fill on the 09:16 bar | A4 §3.4, §4.0, §4.1 |
| G04 | MIS square-off at the dated cut-off (equity 15:20 before 2025-12-26, 15:25 after) with fee and `AUTO_SQOFF` tag; an MIS order after the cut-off is rejected `R_SESSION`; the square-off order itself is not | A4 §3.2, §6.3, Z-5 |
| G05 | Participation cap splits an order across bars | A4 §4.0, §4.1 |
| G06 | IOC remainder cancelled after the first eligible event; a non-marketable IOC is cancelled unfilled | A4 §3.7 |
| G07 | Protected market buy becomes a limit; the price runs away; the remainder rests and fills later at its limit | A4 §3.3, K-12 |
| G08 | Every charge component and total equals Kite's virtual contract note for the captured cases (equity CNC/MIS, futures, options; buy and sell) | A4 §5, K-10, V-01 |
| G09 | One NIFTY futures lot (65 units) bought and sold at 25,000.00 on one day, golden charge rows with exchange rate 0.00183%: **47,922 paise on 2026-03-31** (STT 0.02%) and **96,672 paise on 2026-04-01** (STT 0.05%). Component check for 2026-04-01 (exact, D-46): brokerage 2 × 2,000; STT 81,250; exchange 2 × 2,973.75; SEBI 2 × 162.5; stamp 3,300 (₹32.50 rounded to the rupee); GST 2 × 924.525; order totals 9,361 + 87,311. Kite's own note for that sell (2026-11-06): ₹873.10775. (First written as 47,874 / 96,624 with per-component rounding; the capture showed Kite does not round that way.) | C-11, Z-2, A4 §5.3, D-46 |
| G10 | Index option expiry: long ITM call receives intrinsic minus exercise STT and exercise brokerage; OTM expires at 0; short ITM put pays intrinsic; FSP from `SETTLE`; fallback label when no `SETTLE` arrives by `DAY_END` | A4 §7.2 |
| G11 | Stock future and ITM stock option go to physical delivery: holdings at the settlement price / strike; STT 0.1% both sides; brokerage 0.25%, and 0.1% in a netted case | A4 §7.3, Z-4 |
| G12 | Mid-life lot change: a 150-unit NIFTY quarterly position is unchanged across 2025-12-30; on 2025-12-31 a new order of 75 is rejected `R_QTY_LOT` and 65 accepted; a reduce-only order of 75 that closes half the old position is accepted under the ⚠ exception | C-7, X-3, A4 §3.2 |
| G13 | Token reuse: the same token resolves to different instruments on different dates (data layer) | C-6, K-9 |
| G14 | F&O DAY orders cancelled at `SEG_CLOSE` 15:30 on 2026-07-31 and 15:40 on 2026-08-03 | A4 §1.10, X-7 |
| G15 | Generated expiries are Thursdays before and Tuesdays from 2025-09-01; the validator flags a data/rule mismatch (data layer) | X-2, I-6 |
| G16 | Freeze slicing uses `floor_to(freeze, lot)` per child: 4,030 NIFTY units with freeze 3,510 → 3,510 + 520; with freeze 1,800 → 1,755 + 1,755 + 520; parent `SLICED`, child ids follow the parent; each child pays its own charges | A4 §3.8, X-5 |
| G17 | Black-76 reference values: Hull's textbook example (F = 20, K = 20, r = 9%, σ = 25%, T = 4/12 → put ≈ 1.1166) and the vectors below | C-13 |
| G18 | Price → IV → price round trip over a grid (moneyness 0.8–1.2, T 1–90 days, σ 5–80%): points whose model price is below one tick (₹0.05) must return NaN with `LOW_VEGA` or `BELOW_TICK`; all others round-trip within relative 1e-8; out-of-bounds prices return the right reason | C-13, M4.3 |
| G19 | Closing auction from 2026-08-03: an order in a CAS stock at 15:16 is rejected `R_CAS` and its open DAY orders are cancelled at 15:15; a non-CAS stock still trades until 15:30 | A4 §3.2, §4.8, X-7 |
| G20 | Orders submitted at 09:05 for a future (group in `PREOPEN`) and an option (group `CLOSED`): with `queue` both activate at 09:15 and fill on the first eligible event; with `reject` both are rejected `R_SESSION` | A4 §3.10, X-6 |
| G21 | On a CAS day a CAS stock's MIS is squared off at 15:12 while a plain stock waits for the 15:20 CM cut-off; a MIS order on the CAS stock after 15:12 is rejected `R_SESSION`, the same order on the plain stock fills | A4 §6.3, Z-5 |

G17 vectors (r = 6.5%, ACT/365; values from the plan author's reference implementation, to be confirmed by the two independent implementations):

| F | K | T (days) | σ | Call | Put | Call Δ | Γ | Vega (per 1.00) |
|---|---|---|---|---|---|---|---|---|
| 25,000 | 25,000 | 7 | 0.12 | 165.5341222 | 165.5341222 | 0.5026877831 | 0.000959024827 | 1379.4192724 |
| 25,000 | 25,500 | 30 | 0.15 | 227.0602624 | — | 0.3285760435 | 0.000335195668 | 2582.8433294 |
| 25,000 | 24,000 | 2 | 0.20 | — | 0.3180899443 | put Δ −0.0028470787 | 0.000023580120 | 16.1507670 |

**Tolerances (one set for the whole plan):** prices vs these vectors ≤ 1e-6 absolute (₹); Greeks ≤ 1e-9 relative; C++ vs Python on the same inputs ≤ 1e-12 relative; IV solver stops at 1e-10 absolute in price; G18 round trip ≤ 1e-8 relative.

## §4 Property and differential tests

| ID | Property |
|---|---|
| P-01 | PCG32 (C++) and the Python port produce identical outputs (raw `next_u32`, `below(n)`, `random()`) for 20 seeds × 2 streams × 10,000 draws, plus the published `pcg_basic` reference sequence |
| P-02 | Splitting a pack into random `push()` batches (including boundaries exactly at timer, phase and bar-end times) gives byte-identical artifacts to `run()` |
| P-03 | Inserting random `CLOCK` events changes no deterministic artifact (A4 §1.6) |
| P-04 | Differential fuzz: random small event streams (bars and snapshots, with gaps) plus a seeded random-order strategy; oracle and engine CSV artifacts identical, `metrics.json` within relative 1e-12. 200 seeds per commit, 5,000 weekly; failures are shrunk and saved as new goldens |
| P-05 | Pack builder: same spec and sources ⇒ same SHA-256 |
| P-06 | Accounting identity on random runs: Δcash = Σ sells − Σ buys − charges ± settlements ± futures marks; NAV reconciles each day |
| P-07 | Causality: every fill satisfies A4 §4.0 — bar fills have `effective_start ≥ t_activate`; snapshot evidence fills have `t(s0) ≥ t_activate`; book fills have `t(s1) > t_activate` |

## §5 Determinism

| ID | Check |
|---|---|
| DT-01 | The same run twice ⇒ identical `determinism.json` |
| DT-02 | Oracle (Python) vs engine (MSVC): CSVs and `events_log.txt` identical; `metrics.json` within relative 1e-12; `margin_daily.csv` within 100 paise per row (A4 §9.3) |
| DT-03 | `stockify run --rerun <id>` from the archived bundle and packs reproduces `determinism.json` |
| DT-04 | (manual) Python tests pass with the Windows time zone set to Pacific and to India Standard Time (`tzutil /s`) with identical outputs (I-4) |

## §6 Unit-test highlights

| ID | Module | Must cover |
|---|---|---|
| U-01 | Kite packet parser | every packet length; the 1-byte heartbeat; index vs tradable; depth layout; paise; multi-packet frames |
| U-02 | Calendar/time | phase boundaries across 2025-12-08 and 2026-08-03; holiday-shifted expiry; IST conversions |
| U-03 | NSE parsers | header mapping by name; decimal → paise exactness; unknown column fails; legacy vs UDiFF parity |
| U-04 | Bar cache | chunking, throttle (injected clock), limit learning, adjustment detection, expired instruments only flagged |
| U-05 | Indicators | SMA/EMA/RSI/ATR/VWAP vectors identical in C++ and Python (A2 §4.3) |
| U-06 | Costs | every component with ppb rates; the half-paisa cases (e.g. stamp on ₹900 CNC buy = 13.5 → 14 paise; MIS brokerage on ₹1,950 = 58.5 → 59 paise); row matching errors |
| U-07 | Margin | hand-computed T0 and T1 cases; an iron fly needs less T1 margin than a naked straddle |
| U-08 | Black-76/IV | call and put bounds, deep ITM/OTM, T → 0, `LOW_VEGA`/`BELOW_TICK` flags |
| U-09 | Strategy lint | allowed vs forbidden imports (A2 §4.4) |
| U-10 | Secrets | with a fake token in `isolated_env`, no artifact, bundle, journal, log or export contains it (I-10). Built for the daemon: the status file refuses secret-shaped names and values (`test_token_watch.py`), the order-route scan and secrets scan (`LCB 5.1`), and E-03's token file holds only a fake value. Export (DMF 6.2): `export.build` scans every member's name and bytes for the secret literals (env key and secret, token-file values) and `api_secret`, `access_token`, `enctoken`, `request_token`, `Bearer ...` text and refuses with no zip written (`export.find_secret`); `test_export.py::test_export_contains_no_secret` and the seeded property test cover it. A second guard refuses text members naming the lab, home or repo folder or the Windows user name. Whole files are read into memory; with `--with-packs` a pack over 512 MiB is refused |
| U-11 | Float format | C++ formatter equals Python `repr` on 10⁶ random doubles plus edge cases (0.0, -0.0, 1e-4, 1e16, subnormals) |
| U-12 | Interval VWAP | A4 §2.1 validity: volume 9,999,000 → 10,000,000 with ATP 10000 → 10001 paise gives an implausible value that must be discarded, not used |
| U-13 | Recorder | batching (250 ms / 2,000 rows), `seq` continuing after restart, one gap row per reason, re-centring with hysteresis, pinning, the 3,000 cap, `subscription_log`, the queue-full alarm |
| U-14 | Deflated Sharpe | `test_stats.DeflatedSharpeTests.test_dsr_matches_the_hand_computed_example` (a worked example done by hand), `SharpeTests`, `PropertyLoopTests`; `MetricsRefactorTests` keeps `metrics.json` unchanged |
| U-15 | Bootstrap and seeding | `test_bootstrap.PercentileTests`, `IndexTests` (the same seed gives the same indices), `IntervalTests`; `test_seeding.SeedingTests` (streams never collide) |
| U-16 | Holdout | `test_holdout.EnforceTests`, `ValidateTests`, `QueueTests`, `LaunchTests`, `ImportAndLateTests` (the boundary day is locked, the day before is not; every entry point; an unlock needs a reason and is logged once), `CliTests`, `ApiTests` |
| U-17 | Export and secrets | `test_export.SecretScanTests`, `ExportSecretsTests`, `ExportFalseRefusalTests`, `ExportPersonalDataTests`, `ExportPropertyTests`, `ExportCliTests`, `ExportApiTests`; `test_compare.CompareLogicTests`, `CompareCliTests`, `CompareApiTests` |
| U-18 | Probability of backtest overfitting | `test_pbo.CscvTests` (an independent brute-force reference at 4 and 16 slices, ties, flat columns), `FamilyTests`, `SentenceTests`; timing `[pbo] 12,870 combinations x 100 trials` is printed by the suite |
| U-19 | Sweeps, walk-forward, sensitivity | `test_sweep_spec.SweepSpecTest`, `SweepPropertiesTest`; `test_sweep_run`; `test_walkforward.WindowTests`, `SelectTests`, `RunTests`; `test_walkforward_finish.StitchTests`, `FinishTests`; `test_wf_report`; `test_sensitivity`; `test_pool`; `test_demo_pipeline.DemoPipelineTests` (sweep, walk-forward, sensitivity and report through the real pool on the oracle fixture) |
| U-20 | Registry, regimes, report | `test_registry`, `test_regimes`, `test_report`, `test_plain`, `test_store_research.ResearchStoreTests`, `test_config.ResearchConfigTests`, `test_costs_scale`, `test_extra_ticks` |
| U-21 | Coverage and quality | `test_quality_coverage` (`MixedModeTest`, `AllFullCoverageTests`, `UnloggedCoverageTests`, `MeansProperty`), `reporting/test_coverage`, `data/test_readonly_reads` (a store in a folder named with `#` or `%`) |
| U-22 | Notes and plots (CH 6.1 to 6.7, 6.22, 6.25) | `test_notes.CleanNoteTests` and `EngineParityTests` (bidi controls, D-127), `test_order_note`, `test_engine_note.NoteParityTests`, `test_plot`, `test_strategy_notes`, `test_fifo_trades` |
| U-23 | Chart bars, model and APIs (CH 6.9 to 6.13) | `charts/test_chartbars`, `charts/test_chart_model` (markers, settlements, the G10 golden, paging, `StrategyIntervalTests` D-112, `PageCapTests` D-115), `charts/test_chart_wf`, `charts/test_chart_session`, `test_api_chart.ChartApiTests`, `test_api_research` |
| U-24 | Chart web helpers (CH 6.14 to 6.19) | `test_chart_checks.ChartHelperChecks` runs `tests/stockify/chart_check.ts`; `test_research_checks.ResearchHelperChecks` runs `research_check.ts` (Node, no browser) |
| U-25 | Engine checked arithmetic and carried state (CH 6.21, 6.24) | `test_engine_checked_arithmetic.CheckedWorldTests`, `CheckedHelperParityTests`; `test_engine_strategy_state.ResumeThroughTheTwins`, `StateIsReadChecked` |
| U-26 | Journal and log sharing (LCM 6.4) | `test_journal` (reads sharing DELETE), `test_log_rotation.SharedRotationTests` |

## §7 End-to-end

| ID | Scenario |
|---|---|
| E-01 | A paper session over a fixture day via the replay ticker passes journal parity. Built: `tests/stockify/daemon/test_session_day.py`, `test_recovery.py`, `tests/stockify/test_parity*.py` and E-03 |
| E-02 | `taskkill /F` of the daemon mid-day, restart: final artifacts equal an uninterrupted run (M5.4). Built: `tests/stockify/daemon/test_e02_kill.py` |
| E-03 | Injected disconnect and token expiry: sessions pause and resume; gaps and coverage are correct. Built: `tests/stockify/daemon/test_e03_faults.py`: three subprocesses in a sandbox (disconnect, token expiry with a newer fake token file, kill), gap reasons, coverage `(continuous - 150 s) / continuous`, journal parity, and orders, fills and journal bytes equal to a twin with no kill (XI 5.12) |
| E-04 | Recorder → compaction → pack → run on a fixture day |
| E-05 | API: `POST /api/tests` → poll → fetch artifacts; `npm run build` passes |

## §8 Validation against reality (owner-run)

| ID | What | Output |
|---|---|---|
| V-01 | Capture Kite virtual contract notes for the G08 cases (M2.5) | `tests/fixtures/kite_charges/<date>.json` |
| V-02 | Margin calibration vs Kite basket margins (M4.9) | `margin_calibration` rows, error report |
| V-03 | First live recording days: packet rates, coverage, latency | `RECON.md` entries |
| V-04 | After any NSE/Zerodha change in `01_facts.md`: re-run V-01/V-02 and the affected goldens | dated fixtures |

## §9 CI

- No hosted CI (D-26). Before every commit, locally on Windows: C++ build + `ctest`; Python units; goldens on the oracle (from M2) and the engine (from M3); P-01–P-03, P-05–P-07; P-04 with 200 seeds (from M3); DT-01; E-01, E-04, E-05 once their milestones land; web lint and build.
- Weekly (manual): P-04 with 5,000 seeds; DT-02; DT-04; the M3.13 benchmark.
- Never in automated runs: anything needing Kite credentials (I-10). Fixtures are small, in real formats, and contain no secrets.

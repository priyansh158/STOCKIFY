# M6 — Reporting and research hygiene

> **Goal**: tools that make results comparable, data coverage visible, and research honest: run comparison, export, a data-coverage page, a trial registry with deflated Sharpe, walk-forward analysis, a locked holdout, and sensitivity checks.
> **Depends on**: M3 (runs), M5 (sessions) for the session parts. **Size**: M.
> **Demo**: a parameter sweep of `opening_range_breakout`, walk-forward evaluated, with a report showing in-sample vs out-of-sample Sharpe, the deflated Sharpe given the number of trials, and the cost-sensitivity table.

Concepts: C-4, C-14, C-16, C-17.

> **M6.0 recon (2026-11-12, RCB 6.1)**: done at BASE `7368e7f`; the findings are in `RECON.md` ("2026-11-12 — M6.0 recon"). The spec differs from the code in 17 places (the plan's mismatch table); the five M6 commands are stubs, `trial_families`, `runs.family_id` and `holdout_unlocks` already exist but nothing fills them, and the real lab has 7 runs, one recorded tick day and one day of RELIANCE minute bars.

---

## M6.1 Compare runs (S)

- [x] `/tests/compare?ids=…` (2–6 runs): metrics side by side, equity curves normalised to 1.0, drawdowns, labels with differences highlighted, and a manifest diff (parameters, fill model, margin model, config hashes, engine build).
- [x] CLI: `python -m stockify compare <run_id> <run_id> …` prints the same table.

## M6.2 Export (S)

- [x] `GET /api/tests/{id}/export` and `python -m stockify export <run_id>`: a zip with bundle, manifest, the CSV artifacts, the JSON files (`metrics.json`, `manifest.json`, plus a single `summary.json` with the run's config, metrics and labels) and `report.md` (packs excluded by default, their hashes listed; `--with-packs` includes them). Secrets can never be inside (I-10); a test asserts it by scanning the zip for token-like strings.

## M6.3 Data coverage page (M)

- [x] (page done, `web/src/app/data/`, RCM 6.4) `/data`: a calendar (date × dataset) showing recorder coverage % (FULL and QUOTE), archive status (landing / archived / verified), NSE files fetched (F&O, CM, index closes), bar-cache ranges per instrument; drill-down to gaps and quality metrics (M1.9); storage used and free; live recorder status (M1.5).
- [x] `GET /api/data/coverage?from=&to=` returns the matrix (Task 28, LCM 6.2, D-121); `GET /api/data/gaps?date=` the day's gaps.
- [x] (Task 27, LCM 6.1) The quality report writes `coverage_full_pct` and `coverage_quote_pct` as day rows; older days get them when `data report --date D` is re-run (owner step, RECON).
- [x] (Task 29, RCM 6.1) Web foundation: `web/src/components/charts.tsx` (LineChart, IntervalBar, HeatGrid, hand-written SVG and tables), `web/src/lib/research.ts` (fetchers) and `researchHelpers.ts` (pure helpers, checked by `tests/stockify/research_check.ts`), and the Research and Data nav links; the pages follow in Tasks 30 to 36 (Task 33, RCM 6.5: the `/research` overview, `web/src/app/research/`, lists the families and the sweep and walk-forward jobs; the server writes its two sentences, `plain.family_best_sentence` and `plain.job_status_sentence`; Task 34, RCM 6.6: `/research/family?id=`, `web/src/app/research/family/page.tsx`, shows the tries with the picked one marked, other roles and paper sessions apart and not counted, the holdout unlock log in IST with the owner's reason as plain text, and the overfitting sentence or its note; `family_report` now also carries the meaning sentences `trials`, `objective` and `holdout_unlocks`).
- [x] (Task 30, RCM 6.2) The run page's "How much to trust this result" panel (`web/src/app/tests/[id]/ResearchPanel.tsx`): tries and deflated Sharpe from the server's sentence, three interval bars, the five regime tables with thin groups marked "few days", the sensitivity table (or the command to run it), the holdout-look count, and an amber banner when the run's costs were scaled.

## M6.4 Trial registry and deflated Sharpe (M)

Tables: `trial_families`, run `family_id` (A1 §4).

- [x] Every run and session has a `family_id` (default: strategy name + dataset + objective; the owner can name families). Runs are never deleted from the registry, even when their folders are pruned.
- [x] For the best run of a family report, from daily returns (per-period, not annualised):
  - (Statistics core `stockify/research/stats.py`, D-86; the report objects `report.run_research` and `report.family_report`, `python -m stockify research show`; the API (`/api/tests/{id}/research`, `/api/research/families`) is Task 9; the run page follows in Tasks 30-34.)
  - `SR` = Sharpe of the selected run; `T` = number of daily returns; `γ₃` = skewness; `γ₄` = kurtosis (not excess); `N` = number of trials in the family; `V` = variance of the per-period Sharpe across the family's trials.
  - Expected maximum Sharpe under the null: `SR₀ = √V · [(1 − γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e))]`, with `γ ≈ 0.5772` (Euler–Mascheroni); for `N = 1`, `SR₀ = 0`.
  - **Deflated Sharpe Ratio**: `DSR = Φ( (SR − SR₀)·√(T − 1) / √(1 − γ₃·SR + ((γ₄ − 1)/4)·SR²) )` — the probability the true Sharpe exceeds what luck alone would give the best of N trials (Bailey and López de Prado, 2014).
  - Also the probabilistic Sharpe ratio against zero (the same formula with `SR₀ = 0`), `N` and `T`.
- [ ] Shown on every run page as "best of N trials · DSR = …". Unit-test the formula against a hand-computed example.

## M6.5 Walk-forward analysis (M)

- [ ] (windows, training grids, selection, out-of-sample runs and the persisted state done, `stockify/research/walkforward.py`, D-132; `walkforward.finish` stitches the test pieces, compares in-sample with out-of-sample and writes `walkforward.json`, D-133; `wf_report.render` is the only writer of `report.md`, the command and `GET /api/research/walkforward/{job_id}` are done, D-134) `python -m stockify walkforward --sweep sweep.yaml --train 120d --test 30d --step 30d --objective sharpe`: for each window, run the parameter grid on the training range (parallel subprocesses), pick the best by the objective, run it on the following test range; stitch the test pieces into one out-of-sample equity curve.
- [x] (done: `wf_report.render`, D-134; `python -m stockify research report --walkforward JOB_ID [--sensitivity-run RUN_ID]` writes it again and prints the path, and `tests/stockify/research/test_demo_pipeline.py` runs sweep, walk-forward, sensitivity and report through the real pool on the oracle fixture, WF 6.7, Task 24) Report: in-sample vs out-of-sample metrics and their ratio, the chosen parameters per window (stability), and the stitched curve. Every run joins the family (M6.4).
- [x] (the report page `/research/walkforward/[id]` is done, RCM 6.8, Task 36, beside `report.md`; both read their notes from `wf_report.notes`) Report: in-sample vs out-of-sample metrics and their ratio, the chosen parameters per window (stability), and the stitched curve. Every run joins the family (M6.4).

## M6.6 Locked holdout (S)

- [x] (done, `stockify/research/holdout.py`, D-88; the owner chose `holdout_from` = 2026-08-01 on 2026-11-12, D-89) `stockify/config/research.json` → `holdout_from` (a date chosen by the owner, e.g. the last three months). Runs whose data overlaps the holdout need `--unlock-holdout "<reason>"`; each unlock is logged in `holdout_unlocks` and counted on the family's report.

## M6.7 Sensitivity suite (S)

> The `costs.scale` run-config key the costs row needs is done (FW 6.1, D-90, `bundle.scale_charges`); the suite itself is Tasks 14 to 16. `fill_model.slippage.extra_ticks` (FW 6.2, D-91) is done on both runners; the engine must be rebuilt before Task 16.

- [x] (suite and CLI done, `stockify/research/sensitivity.py`, FW 6.3; the API and plain sentence done, `GET /api/tests/{id}/sensitivity` and `plain.fragile_sentence`, FW 6.4) `python -m stockify sensitivity --run <id>` re-runs with costs × {0, 1, 1.5, 2}, slippage + {0, 1, 2} ticks, latency {0, 250, 1000} ms, and every applicable fill model; prints a table and flags a strategy as **fragile** if its net P&L turns negative under costs × 1.5 or one extra tick.

## M6.8 Bootstrap confidence intervals (S)

- [x] (bootstrap intervals done, `stockify/research/bootstrap.py`, cached by `report.run_research` in `runs/<id>/research.json`; the API is Task 9, the run-page display Tasks 30-34) Stationary block bootstrap of daily returns (mean block length configurable, seeded): 90% intervals for Sharpe, mean daily return and max drawdown, on every run report.

## M6.9 Regime breakdown (S)

- [x] (done, `stockify/research/regimes.py`, cached beside the bootstrap in `runs/<id>/research.json` under its own key; shown on the run page in Tasks 30-34) Performance by month, weekday, expiry day vs other days (from the calendar), India VIX bucket (recorded `INDIA VIX`, else bhavcopy index closes), and opening-gap size.

## M6.10 Parameter sweeps (M)

- [ ] `sweep.yaml` (grid or seeded random search) → parallel runs (≤ `cpu_count − 1`), results table, and a two-parameter heatmap in the UI. A sweep is a family by default. (`sweep.yaml` load, validate and expand: done, `stockify/research/sweep.py`, A1 §6.6, Task 17; running it, `python -m stockify sweep`, the job, family and results files: done, D-101, Task 18; the sweep API and its heatmap cells, `GET /api/research/jobs` and `/api/research/sweeps/{job_id}`, are done, WF 6.3, Task 19; the heatmap page, `/research/sweeps/[id]`, is done: a diverging scale centred on zero for signed objectives, axis pickers in the URL, the plain states for a sweep still running, failed or missing, RCM 6.7, Task 35.)

## M6.11 Probability of backtest overfitting (stretch, M)

- [x] (done, `stockify/research/pbo.py`, on the family report as `pbo` / `pbo_note`; a walk-forward family gets a note, its windows share no days) Combinatorially symmetric cross-validation (Bailey, Borwein, López de Prado and Zhu, 2015) over a family's trial return matrix; report the PBO estimate.

## M6.12 Project write-up (S, optional)

- [ ] A public-facing `README` section for the repository: what STOCKIFY does, the architecture diagram, and **measured** numbers (determinism across platforms, journal-parity record, recorder-vs-live divergence, margin calibration error, engine throughput). Concrete measured results explain the project better than a feature list.

---

## Verification

```
python -m stockify sweep --config examples/sweeps/orb_grid.yaml
python -m stockify walkforward --sweep examples/sweeps/orb_grid.yaml --train 120d --test 30d --step 30d --objective sharpe
python -m stockify sensitivity --run <best_run_id>
.\.venv\Scripts\python.exe tests\run_tests.py
cd web; npm run lint; npm run build
```

## Candlestick charts (CH)

- [x] CH 6.1 Note cleaning and Indian-grouped price and quantity text, the same in Python and C++ (`stockify/oracle/notes.py`, `engine/src/notes.cpp`; `fmt_price`, `fmt_qty`, `to_level` exported from `stockify.strategy`).
- [x] CH 6.2 Every order carries the strategy's own `note`, written to `orders.csv` by the oracle (`Order.note`, `ctx.place(note=)`, `system_notes.json`, bundle `system_notes`, D-92; the engine half is CH 6.3).
- [x] CH 6.3 The note in the C++ engine, the pybind bridge and the carried sod_state, byte for byte with the oracle (`Engine.place(note=)`, `sod_state` order `note`, optional on read, D-92).
- [x] CH 6.4 `ctx.plot` and `plots.csv` on both runners (`ctx.plot(name, value, *, kind="price", instrument=0)`, 8 series and 200,000 points per run, a deterministic file; D-94).
- [x] CH 6.5 The FIFO trade loop is a public function, `stockify.oracle.metrics.fifo_trades` (returns `TradeChunk`s and the traded value), shared by `compute` and the chart's trade markers; `metrics.json` is byte-identical on every golden.
- [x] CH 6.6 The shipped Python strategies say why they trade and draw their own lines (`note=` on every order, `ctx.plot` in sma_cross and the opening-range breakout; the exact sentences are D-95, for the C++ twins to copy).
- [x] CH 6.7 The C++ twins `orb_cpp` and `sma_cross_cpp` write the same notes and plots as their Python originals, byte for byte (the sentences are D-95).
- [x] CH 6.8 The engine side of the charts is reviewed and recorded (differential review, sharp-edges, insecure-defaults; one slippage `k` fix; `RECON.md`; c-review runs at the final review).
- [x] CH 6.9 Display bars read back from a run's own pack or journal with the runner's bucketing (`stockify/oracle/bars.py`, `stockify/reporting/chartbars.py`; D-123); `_build_bars` changes no artifact byte.
- [x] CH 6.10 A run's chart model: its own bars, a marker per fill and per expiry settlement with the note, costs and trade profit, and its plotted lines (`stockify/reporting/chart.py`, `stockify/reporting/settlements.py`; D-124).
- [x] CH 6.11 `GET /api/tests/{id}/chart` and its DTOs (`RunChartDTO`, mirrored in `web/src/lib/types.ts`; D-125).
- [x] CH 6.12 `GET /api/research/walkforward/{job_id}/chart` and `WalkforwardChartDTO` (mirrored in `web/src/lib/types.ts`; D-126).
- [x] CH 6.13 `GET /api/trade/sessions/{id}/chart` and `SessionChartDTO` (`stockify/reporting/chart_session.py`): a paper session day's bars read incrementally from its journal, its fills with notes, FIFO profit across days (D-103).
- [x] CH 6.14 `lightweight-charts` is pinned (5.2.1, moved to 5.3.0 by CH 6.23) (exact, with `fancy-canvas@2.1.0`; D-110), the pure chart helpers are in `web/src/lib/chart.ts` (checked by `test_chart_checks.py`), and the footer carries the TradingView notice. The chart types are C11's DTOs in `web/src/lib/types.ts`.
- [x] CH 6.15 `PriceChart` (client-only, loaded through `PriceChartLazy`) draws candles, volume, buy/sell markers with the strategy's note on a card, and the strategy's own lines; every trade is also a button in a list under the chart. `chart_api.ts` reads the run, walk-forward and session chart endpoints (a 404 gives null). The owner's look at 1024 px is still open (STOP, not a blocker).
- [x] CH 6.16 The run page shows the chart ("Price and trades", between the equity panel and the fills); the Fills table has a Why column (the note, else the tag) joined to the chart's markers by `fill_id`, a row's "Show on chart" opens that marker, and a marker click highlights its row. A run that is not finished shows the panel with its status. Page order and the collapsed regime tables: D-111. The owner's look at 1024 px is still open (STOP, not a blocker).
- [x] CH 6.17 The paper session page shows its live chart ("Price and trades", `GET /api/trade/sessions/{id}/chart` through `PriceChartLazy`): today by default, a day picker from `days`, a "Paper trading: no real orders" badge, and polling every 5 s only while the day is open and the tab visible (pure `pollDelay` in `web/src/lib/chart.ts`; no WebSocket). A day with no recording says "No recording yet for this day", an unreadable one says so (`prunedWords`), and an incomplete profit and loss shows the notice.
- [x] CH 6.18 The walk-forward page shows "Out-of-sample trades" under the stitched curve: the test windows' bars and the stitched fills from `GET /api/research/walkforward/{job_id}/chart`, each window a band (`windowBands`, `shadePoints` in `web/src/lib/chart.ts`), a window with nothing to draw (excluded, no trades, failed) shaded in the warning tint and named in plain words ("Window 3 (out of sample): excluded: <the server's reason>"), the trades list kept (the page has no Fills table); checked by `test_chart_checks.py`.
- [x] CH 6.19 The sweep page charts the selected trial under the heatmap (`?trial=<run id>`, kept across a reload; no `trial` means the best trial; an unknown or unfinished one shows an empty state). A heat-grid cell (Enter or Space) and a table row's "Show chart" pick it; the chart is `GET /api/tests/{run_id}/chart` through `PriceChartLazy`, with the trades list on. Pure helpers `resolveTrial`, `trialWords`, `trialHref` in `web/src/lib/chart.ts`.

## Definition of done

The demo report exists and the owner has read it; `/data` shows the full coverage history; DSR and bootstrap intervals appear on every run page; all boxes ticked.

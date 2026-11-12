# M6 — Reporting and research hygiene

> **Goal**: tools that make results comparable, data coverage visible, and research honest: run comparison, export, a data-coverage page, a trial registry with deflated Sharpe, walk-forward analysis, a locked holdout, and sensitivity checks.
> **Depends on**: M3 (runs), M5 (sessions) for the session parts. **Size**: M.
> **Demo**: a parameter sweep of `opening_range_breakout`, walk-forward evaluated, with a report showing in-sample vs out-of-sample Sharpe, the deflated Sharpe given the number of trials, and the cost-sensitivity table.

Concepts: C-4, C-14, C-16, C-17.

> **M6.0 recon (2026-11-12, RCB 6.1)**: done at BASE `7368e7f`; the findings are in `RECON.md` ("2026-11-12 — M6.0 recon"). The spec differs from the code in 17 places (the plan's mismatch table); the five M6 commands are stubs, `trial_families`, `runs.family_id` and `holdout_unlocks` already exist but nothing fills them, and the real lab has 7 runs, one recorded tick day and one day of RELIANCE minute bars.

---

## M6.1 Compare runs (S)

- [ ] `/tests/compare?ids=…` (2–6 runs): metrics side by side, equity curves normalised to 1.0, drawdowns, labels with differences highlighted, and a manifest diff (parameters, fill model, margin model, config hashes, engine build).
- [ ] CLI: `python -m stockify compare <run_id> <run_id> …` prints the same table.

## M6.2 Export (S)

- [ ] `GET /api/tests/{id}/export` and `python -m stockify export <run_id>`: a zip with bundle, manifest, the CSV artifacts, the JSON files (`metrics.json`, `manifest.json`, plus a single `summary.json` with the run's config, metrics and labels) and `report.md` (packs excluded by default, their hashes listed; `--with-packs` includes them). Secrets can never be inside (I-10); a test asserts it by scanning the zip for token-like strings.

## M6.3 Data coverage page (M)

- [ ] `/data`: a calendar (date × dataset) showing recorder coverage % (FULL and QUOTE), archive status (landing / archived / verified), NSE files fetched (F&O, CM, index closes), bar-cache ranges per instrument; drill-down to gaps and quality metrics (M1.9); storage used and free; live recorder status (M1.5).
- [ ] `GET /api/data/coverage?from=&to=` returns the matrix.

## M6.4 Trial registry and deflated Sharpe (M)

Tables: `trial_families`, run `family_id` (A1 §4).

- [x] Every run and session has a `family_id` (default: strategy name + dataset + objective; the owner can name families). Runs are never deleted from the registry, even when their folders are pruned.
- [ ] For the best run of a family report, from daily returns (per-period, not annualised):
  - (Statistics core done: `stockify/research/stats.py`, D-86; the report that uses it follows.)
  - `SR` = Sharpe of the selected run; `T` = number of daily returns; `γ₃` = skewness; `γ₄` = kurtosis (not excess); `N` = number of trials in the family; `V` = variance of the per-period Sharpe across the family's trials.
  - Expected maximum Sharpe under the null: `SR₀ = √V · [(1 − γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e))]`, with `γ ≈ 0.5772` (Euler–Mascheroni); for `N = 1`, `SR₀ = 0`.
  - **Deflated Sharpe Ratio**: `DSR = Φ( (SR − SR₀)·√(T − 1) / √(1 − γ₃·SR + ((γ₄ − 1)/4)·SR²) )` — the probability the true Sharpe exceeds what luck alone would give the best of N trials (Bailey and López de Prado, 2014).
  - Also the probabilistic Sharpe ratio against zero (the same formula with `SR₀ = 0`), `N` and `T`.
- [ ] Shown on every run page as "best of N trials · DSR = …". Unit-test the formula against a hand-computed example.

## M6.5 Walk-forward analysis (M)

- [ ] `python -m stockify walkforward --sweep sweep.yaml --train 120d --test 30d --step 30d --objective sharpe`: for each window, run the parameter grid on the training range (parallel subprocesses), pick the best by the objective, run it on the following test range; stitch the test pieces into one out-of-sample equity curve.
- [ ] Report: in-sample vs out-of-sample metrics and their ratio, the chosen parameters per window (stability), and the stitched curve. Every run joins the family (M6.4).

## M6.6 Locked holdout (S)

- [ ] `stockify/config/research.json` → `holdout_from` (a date chosen by the owner, e.g. the last three months). Runs whose data overlaps the holdout need `--unlock-holdout "<reason>"`; each unlock is logged in `holdout_unlocks` and counted on the family's report.

## M6.7 Sensitivity suite (S)

- [ ] `python -m stockify sensitivity --run <id>` re-runs with costs × {0, 1, 1.5, 2}, slippage + {0, 1, 2} ticks, latency {0, 250, 1000} ms, and every applicable fill model; prints a table and flags a strategy as **fragile** if its net P&L turns negative under costs × 1.5 or one extra tick.

## M6.8 Bootstrap confidence intervals (S)

- [x] (bootstrap intervals done, `stockify/research/bootstrap.py`; the run-page display is Task 8) Stationary block bootstrap of daily returns (mean block length configurable, seeded): 90% intervals for Sharpe, mean daily return and max drawdown, on every run report.

## M6.9 Regime breakdown (S)

- [ ] Performance by month, weekday, expiry day vs other days (from the calendar), India VIX bucket (recorded `INDIA VIX`, else bhavcopy index closes), and opening-gap size.

## M6.10 Parameter sweeps (M)

- [ ] `sweep.yaml` (grid or seeded random search) → parallel runs (≤ `cpu_count − 1`), results table, and a two-parameter heatmap in the UI. A sweep is a family by default.

## M6.11 Probability of backtest overfitting (stretch, M)

- [ ] Combinatorially symmetric cross-validation (Bailey, Borwein, López de Prado and Zhu, 2015) over a family's trial return matrix; report the PBO estimate.

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

## Definition of done

The demo report exists and the owner has read it; `/data` shows the full coverage history; DSR and bootstrap intervals appear on every run page; all boxes ticked.

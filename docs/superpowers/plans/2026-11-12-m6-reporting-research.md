# M6 Reporting and Research Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Results become comparable, data coverage visible and research honest: run comparison, a safe export, a `/data` coverage page, a trial registry with deflated Sharpe, bootstrap intervals, regime splits, a locked holdout, a sensitivity suite, parameter sweeps, walk-forward analysis and (stretch) the probability of backtest overfitting, all shown on desktop pages in plain words.

**Architecture:**
- Two new Python packages hold the work. `stockify/research/` has the statistics and the trial machinery (pure functions in `stats.py`, `bootstrap.py`, `pbo.py`; orchestration in `registry.py`, `pool.py`, `sweep.py`, `walkforward.py`, `sensitivity.py`). `stockify/reporting/` has `compare.py`, `export.py` and `coverage.py`.
- Every statistic is computed from `lab.sqlite` rows (`run_equity`, `research_jobs`), never from a run folder, so a pruned folder still counts as a trial and a job can be finished in a fresh process.
- Heavy work (sweeps, walk-forward windows, sensitivity re-runs) goes through `python -m stockify run` subprocesses (I-9). Analytics (DSR, bootstrap, PBO) run inline in sync API routes and cache their result as a derived file.
- A sweep, a walk-forward and a sensitivity suite are each a **job** (`research_jobs`). A sweep and a walk-forward job is **its own family**; a sensitivity job's runs sit in the source run's family with role `sensitivity`.
- The API and the web pages only read.

**Tech Stack:** Python 3.12 stdlib (`statistics.NormalDist`, `itertools`, `zipfile`, `sqlite3`, `fractions`, `decimal`), numpy 2.5 and pandas 2.3 (already in the venv), PyYAML (already in `requirements.txt`, used for `sweep.yaml`), matplotlib (already used by `oracle/report.py`), FastAPI, Next.js 16 with hand-written SVG charts, C++20 for one small engine change (Task 14). **No new package.** scipy, statsmodels and hypothesis are not installed.

**Spec:** `docs/plan/M6_reporting_research.md` (items M6.1–M6.12). Read it with: `00_overview.md` §2 (I-2, I-3, I-4, I-8, I-9, I-10, I-13, I-14) and §5 (D-26, D-31, D-48, D-76, D-77, D-83, D-84); `A1_schemas.md` §4 (lab tables), §6.1 (run.json), §6.3 (run folder); `A2_interfaces.md` §6 (API table); `A3_testing.md` (U-10); `A4_engine_semantics.md` §4.4 (slippage) and §8 (metrics); `RECON.md` (M5 close and M6.0 once written); `web/design.md`.

**Base:** BASE = branch `stockify/m5-paper` at the commit Task 1 records. M6 work happens on `stockify/m6-research` in its own worktree.

## Global Constraints

- **Windows only (D-26).** Run everything with `.\.venv\Scripts\python.exe` (system `python` lacks tzdata). No Linux presets, CI, systemd or POSIX-only code.
- **Tests:** stdlib `unittest`, no pytest. Offline and credential-free. `import tests.support` first (it sandboxes `state_dir`/`logs_dir`); use `tests.support.isolated_env({...})`, never `patch.dict(os.environ, ..., clear=True)`; SQLite-backed tests subclass `SandboxedDBTest` with `db_modules`. Run with `.\.venv\Scripts\python.exe tests\run_tests.py <filter>`; the full suite (4–10 min) runs in the background and must not exit 3 (build the engine first with `.\run.ps1 -BuildEngine`).
- **No new packages without the owner's word.** scipy, statsmodels and hypothesis are not installed, so deflated Sharpe, the stationary block bootstrap, PBO/CSCV and the regime splits use the stdlib plus the numpy and pandas already in the venv. The normal distribution is `statistics.NormalDist` (`cdf`, `inv_cdf`). Any task that would need a new package is marked 🛑 owner with its fallback; none is expected. Charts are hand-written SVG, no chart library.
- **Determinism:** random draws come from the project's existing seeding only: `stockify.bundle.day_seed` feeding `stockify.oracle.rng.Pcg32`, through `stockify/research/seeding.py` (Task 2), on streams other than the engine's streams 1 and 2. No wall clock in any statistic or result: ids and `created_ns` may carry time, no number may read it, and ties are broken by name then run id, never by time. Money is int64 paise, quantity units, time UTC epoch ns, IST only at the edges (display, trading dates). Statistics use plain left-to-right loops in A4 §8 order wherever a number must equal `metrics.json` (no `sum()`, no numpy reductions there); numpy is allowed where a result is compared with a tolerance (PBO). Every statistic that is undefined is `None` (JSON `null`), never `NaN` or `inf`.
- **Shared helpers, not copies.** The risk-free rule is `oracle.metrics.rate_on` (Task 5 makes the private `_rate_on` public), drawdown is `oracle.metrics.max_drawdown` / `drawdown_series` (Task 5 extracts them from `compute`), and every derived file is written with `stockify.research.fileio.atomic_write_text` (Task 2). No task re-implements them.
- **UI:**
  - Desktop only (D-83), readable down to about 1024 px, wide tables are fine.
  - Every page is designed with the Hallmark skill (`~/.claude/skills/hallmark`): app-screen flow, `design.md` locked (`web/design.md`), **static** `node ~/.claude/skills/hallmark/scripts/sloplint.mjs <files> --scope app` (never `--render`), no imagegen.
  - Tokens from `globals.css` and `tokens.css`; components from `web/src/components/ui.tsx` (`Panel`, `PageHead`, `Stat`, `Table`, `Chip`, `Skeleton`, `Empty`); every data region has loading, empty, error and full states.
  - Money is ₹ through `web/src/lib/format.ts` (`Intl.NumberFormat("en-IN")`), never `$`.
  - **Plain words.** Every statistic on screen sits beside a one-line plain-English meaning (for example "best of 12 tries, after allowing for luck"). The sentences are written once, server-side, in `stockify/research/plain.py` (Task 7 owns its structure) and shipped in the DTOs as `plain` fields, so the CLI, `report.md` and the pages say the same thing. No raw code, enum or Greek letter reaches the screen.
  - Next.js 16 has breaking changes: read the relevant pages of `web/node_modules/next/dist/docs/` before writing web code (run `.\run.ps1 -InstallWeb` if `node_modules` is missing). `searchParams` and `params` are Promises (see `web/src/app/tests/page.tsx`).
  - `webapp-testing` cannot run (no `playwright`); verification is lint, build, sloplint, the pure-function checks (`tests/stockify/*_check.ts` pattern, see `test_param_checks.py`) and the owner's look at 1024 px.
- **API:** loopback only (D-77): new routes live in routers already wrapped by `server/local_only.py`; a new router is registered in `server/main.py` the same way as the others. `server/schemas.py` ↔ `web/src/lib/types.ts` are tied by the blockwatch block `dto-fields`: append DTOs to both files in the same order and run `~/.tools/blockwatch/blockwatch.exe`. Fixed paths (`/api/tests/compare`) are declared before `/api/tests/{run_id}`. List endpoints take `offset`/`limit`. Sync `def` routes for anything that reads files or computes.
- **Data safety:**
  - The real lab (`%LOCALAPPDATA%\STOCKIFY`) is read-only apart from derived outputs: `runs/<id>/research.json`, `runs/<id>/sensitivity.json`, `research/<job_id>/…`, `exports/…`, and new `lab.sqlite` rows made by the new commands. Tests never touch it.
  - Runs are never deleted from the registry (M6.4): migration 0005 adds a `BEFORE DELETE` trigger that aborts.
  - Recorded data is irreplaceable (I-13): no migration touches `ticks/` or `refdata.sqlite`; the only change near them is new `data_quality` metric rows (Task 27).
  - **I-10:** an export never holds a secret. Export copies from an allow-list of file names, and a test scans every member of the zip for token-like strings.
- **Config, not constants (I-1 in spirit):** every grid, edge, window default and threshold lives in `stockify/config/research.json` (Task 2). These are not market rules, so no `effective_from` rows, but the loader validates them.
- **Commits:** `<POS> 6.<n>: summary` (D-31; the roster below is provisional, the controller rules). The owner's git identity, no co-author, tool or "agent" lines (skill `no-agentic-coauthor`). **Stage explicit file paths only** (each commit block lists them; never a directory such as `docs/plan`, `stockify`, `server` or `web`, and never `CLAUDE.md`, D-29): run `git status --short` first and add only the files the task touched. Push only on the owner's "push". Each task ticks its boxes in `docs/plan/M6_reporting_research.md` in the same commit (README step 4); a deviation gets the next free `D-xx` row in `00_overview.md` §5 in the same commit (`D-85` onward). The pre-commit hook runs gitleaks, then blockwatch; both fail closed.
- **Process:** subagent-driven-development, one implementer per task, then a task review, then fix rounds. A task commits once its offline tests pass; a live or owner step after that is a 🛑 recorded in RECON as "code done, live pending" and never blocks the next task.
- **Skills by what a diff touches (CLAUDE.md):** new config key, CLI flag or endpoint → `sharp-edges` and `insecure-defaults` (report only); money arithmetic → `dimensional-analysis` (report only, never write annotations); `engine/` C++ → `modern-cpp` while writing, `c-review` after; parsers and round-trips → the property-based-testing method with the stdlib; `web/` → `design:design-critique`, `design:accessibility-review`, `design:ux-copy`; statistics → the statistics skills as method only (scipy/statsmodels absent).
- **Provisional roster (controller rules, owner reviews in RECON):** RCB recon · AM registry and config · IL statistics · IR bootstrap and regimes · PM holdout and pool · WF sweeps and walk-forward · FW costs, slippage and sensitivity · LF PBO · DMF compare and export · LCM coverage · RCM web pages · OM write-up · XI bookkeeping.

## Review Focus

Failure modes the spec implies that no single item's tests would catch. Each line is pinned by a named test in the owning task.

1. **Short, flat or empty return series** (a run that never traded, one trading day, a failed or running run, a pruned folder). The owner expects "no trades, no Sharpe" or "not enough days to say" in words, never `NaN`, `inf`, a crash or a blank page; JSON never carries `NaN`. Pinned by `test_flat_series_has_no_sharpe_but_a_sentence` (Task 5), `test_short_series_bootstrap_says_so` (Task 6), `test_pruned_folder_still_counts` (Task 4).
2. **A family of one, or of identical trials.** "Best of 1 try" must give `SR₀ = 0` and `DSR = PSR`, and identical or undefined Sharpes (variance 0) must not divide by zero. Pinned by `test_one_trial_dsr_equals_psr` and `test_identical_trials_have_zero_expected_max` (Task 5).
3. **Variants inflating the trial count.** Sensitivity re-runs, walk-forward test pieces and re-runs are not new tries; counting them would make every strategy look luckier than it is, and not counting sweep trials would hide the luck. Pinned by `test_variants_and_oos_runs_are_not_trials` (Task 4) and `test_sensitivity_runs_do_not_change_n` (Task 15).
4. **The holdout boundary.** A run ending the day before `holdout_from` passes, one ending on it is locked (exit 2, HTTP 422), a pack named only by its hash is checked through its manifest, an empty or whitespace reason is refused, one sweep or sensitivity job logs one unlock however many runs it makes, and a walk-forward whose test window crosses the line is locked as a whole. Pinned by Task 11 and `test_walkforward_test_window_crossing_holdout_is_locked` (Task 20).
5. **A lab with no or partial recordings.** `/data` on an empty lab, on a day with only a landing file, on weekends and holidays, and with no `data report` run for a day must show "no recording", "not reported yet" or "market closed", never a zero that looks like a measurement. Pinned by `test_empty_lab_matrix_has_no_numbers` and `test_unreported_day_is_not_a_zero` (Task 28).

## Gates and owner steps

| Item | Offline (code, tests) | 🛑 Owner or live gate |
|---|---|---|
| M6.1 compare | Tasks 25, 31 | none |
| M6.2 export | Task 26 | owner opens one export (optional) |
| M6.3 `/data` | Tasks 27, 28, 32 | 🛑 owner re-runs `data report --date D` for each recorded day so the new coverage metrics exist (derived rows only); 🛑 live: "full coverage history" needs the recorded days to exist |
| M6.4 registry, DSR | Tasks 3, 4, 5, 7, 8, 9, 30, 33, 34 | 🛑 owner runs `research families --backfill` once on the real lab (updates only NULL `family_id` rows; `--dry-run` first) and names families if wanted |
| M6.5 walk-forward | Tasks 20, 21, 22, 36 | 🛑 live: the demo needs about 330 calendar days of cached RELIANCE minute bars (train 120d + a few 30d tests), fetched with `bars fetch` (Kite login) |
| M6.6 holdout | Task 11 | 🛑 owner chooses `holdout_from` (shipped `null`; the plan suggests the last three months) |
| M6.7 sensitivity | Tasks 13, 14, 15, 16 | demo on the best real run, after the sweep; 🛑 the engine is rebuilt (`.\run.ps1 -BuildEngine`) for Task 14 |
| M6.8, M6.9 | Tasks 6, 10 | VIX source: if reading the recorded `INDIA VIX` is too slow, Task 10 falls back to bhavcopy and records a D-row (owner reviews) |
| M6.10 sweeps | Tasks 12, 17, 18, 19, 35 | the demo sweep needs the same minute bars as M6.5 |
| M6.11 PBO (stretch) | Task 23 | none |
| M6.12 write-up | Task 38 only prepares the generator and the draft | 🛑 gated on live days and the owner's approval of every public sentence; nothing is published or pushed without "push" |
| Definition of done | Task 37 | 🛑 the owner reads the demo report |

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `docs/plan/RECON.md` (append) | M6.0 findings, mismatch table, provisional decisions | 1 |
| `stockify/config/research.json`, `stockify/config/loader.py` (modify) | every M6 grid, edge, default and threshold, validated | 2 |
| `stockify/research/seeding.py`, `stockify/research/fileio.py` | `stream(seed, purpose, anchor)`; `atomic_write_text` | 2 |
| `stockify/paths.py` (modify) | `research_dir`, `research_path`, `exports_dir`, `export_path` | 2 |
| `stockify/data/migrations/lab/0005_research.sql`, `stockify/store.py` (modify) | `runs.role`, `runs.job_id`, `research_jobs`, `holdout_unlocks.job_id`, `trial_families.named`, triggers; store functions | 3 |
| `stockify/research/registry.py`, `stockify/runs.py`, `stockify/sessionconfig.py` (modify), `tests/stockify/research/fixtures.py` | default family id, family rows, roles, `TrialRun` listing, objective pick | 4 |
| `stockify/research/stats.py`, `stockify/oracle/metrics.py` (modify) | returns, moments, Sharpe, PSR, expected maximum, DSR; public `rate_on`, `max_drawdown`, `drawdown_series` | 5 |
| `stockify/research/bootstrap.py` | stationary block bootstrap and intervals | 6 |
| `stockify/research/plain.py` | the structure: every key's meaning; DSR, PSR and interval sentences | 7 |
| `stockify/research/report.py` | `run_research`, `family_report`, `list_families`, derived-file cache; `research` CLI | 8 |
| `server/routers/research.py`, `server/routers/tests.py`, `server/schemas.py`, `web/src/lib/types.ts` (modify) | research API and DTOs | 9 and later |
| `stockify/research/regimes.py` | month, weekday, expiry, VIX and gap splits | 10 |
| `stockify/research/holdout.py` | locked holdout and its log | 11 |
| `stockify/research/pool.py` | bounded parallel `stockify run` subprocesses | 12 |
| `stockify/runconfig.py`, `stockify/bundle.py` (modify) | `costs.scale` | 13 |
| `stockify/runconfig.py`, `stockify/oracle/matcher.py`, `engine/src/matcher.cpp` (modify) | `fill_model.slippage.extra_ticks` | 14 |
| `stockify/research/sensitivity.py` | variants, applicability, suite, fragile flag, `sensitivity.json`; API | 15, 16 |
| `stockify/research/sweep.py`, `examples/sweeps/orb_grid.yaml` | `sweep.yaml`, expansion, execution, results, API | 17, 18, 19 |
| `stockify/research/walkforward.py`, `stockify/research/wf_report.py` | windows, per-window sweeps, selection, persistence, stitching, the one `report.md` renderer | 20, 21, 22 |
| `stockify/research/pbo.py` | CSCV | 23 |
| `stockify/reporting/compare.py` | run comparison | 25 |
| `stockify/reporting/export.py` | zip export | 26 |
| `stockify/reporting/coverage.py`, `stockify/data/quality.py` (modify), `server/routers/data.py` (modify) | coverage matrix, gaps, storage | 27, 28 |
| `stockify/__main__.py` (modify) | `compare`, `export`, `sweep`, `walkforward`, `sensitivity` leave `_STUBS`; `research` is new; `run` gains `--role`, `--job-id`, `--unlock-holdout` | 8, 11, 12, 15, 18, 22, 24, 25, 26 |
| `web/src/components/charts.tsx`, `web/src/lib/research.ts`, `web/src/lib/data.ts` | SVG charts, fetchers, pure helpers | 29–36 |
| `web/src/app/tests/[id]/page.tsx`, `web/src/app/tests/compare/page.tsx`, `web/src/app/tests/CompareSelect.tsx`, `web/src/app/data/page.tsx`, `web/src/app/research/…`, `web/src/components/AppNav.tsx` | pages | 30–36 |
| `tests/stockify/research/`, `tests/stockify/reporting/` (new packages, each with `__init__.py` like `tests/stockify/daemon/`) | tests | all |

## Mismatches between the M6 spec and the code

Task 1 records these in RECON as **provisional decisions** (controller ruling; owner reviews). The task that implements each writes its D-row.

| # | The spec assumes | The code at BASE | Resolution |
|---|---|---|---|
| 1 | M6.1/M6.2 add `compare` and `export` | `__main__._STUBS` lists `compare`, `export`, `sweep`, `walkforward`, `sensitivity` as "M6" stubs. `stockify/compare.py` and `_cmd_compare` already exist but diff two run folders (`compare-artifacts`). | New code lives in `stockify/reporting/` and `stockify/research/`; the five stubs become real commands; `compare-artifacts` and `stockify/compare.py` are untouched. |
| 2 | `GET /api/tests/compare` is M6.1 | It exists as a 501 stub declared before `/api/tests/{run_id}`. | Task 25 replaces the body, keeps the position. |
| 3 | M6.4 "tables `trial_families`, run `family_id`" | Both already exist (lab migration 0001) and `runconfig` already accepts `family_id` and `unlock_holdout`; `holdout_unlocks` exists too. Nothing fills them. | Migration 0005 adds only columns, `research_jobs` and triggers; no table is recreated. |
| 4 | M6.4/M6.5 "every run joins the family" | Counting sensitivity re-runs, walk-forward test pieces and determinism re-runs as trials would inflate `N` for no reason. | `runs.role` ∈ `trial`, `sensitivity`, `oos`, `rerun`. All carry the family id; only `trial` runs with status `done` count in `N` and `V`. Sessions carry a family id and are listed in the family but are not trials. **D-row.** |
| 5 | M6.4 "Sharpe of the selected run, per-period" | The run page's Sharpe is on excess returns (`x_d = r_d − rate_d/252`, A4 §8) and is **null when sd = 0**. | The statistics use the same excess returns and the same rule: `sharpe_per_period · √252` equals `metrics.json` Sharpe to 1e-12, and a flat series has no Sharpe (`None`). Flat trials count in `N`, not in `V`. **D-row.** |
| 6 | M6.7 "costs × {0, 1, 1.5, 2}" | No run-config key scales costs. Post-hoc scaling is not exact (costs change cash and margin). | New key `costs.scale` (decimal string, default `"1"`, omitted from the normalised form when `"1"`), applied once in `bundle.resolve_bundle`: percentage strings scaled exactly in `Decimal`, integer-paise leaves rounded once half-up (`stockify.numerics`). Both engines read the bundle, so neither changes. **D-row.** |
| 7 | M6.7 "slippage + {0,1,2} ticks" | `fill_model.slippage` has four kinds (`none`, `fixed_ticks`, `random_ticks`, `bps`); the demo run uses `bps`; no kind can express "k more ticks". | New key `fill_model.slippage.extra_ticks` (int ≥ 0, valid for every kind, omitted when 0): for a fill that takes slippage, `move += extra_ticks · tick` after the kind's own move is computed (the `random_ticks` RNG draw is unchanged), then the existing adverse sign and `cap` apply. **This needs an engine change** (`matcher.cpp`, mirrored in `matcher.py`, with a parity test), so it has its own task (14). **D-row.** |
| 8 | M6.7 "every applicable fill model" | "Applicable" is not defined; `runconfig` and `bundle` already enforce what each kind needs. | A kind is applicable when `runconfig.validate` and a dry `bundle.resolve_bundle` accept the variant config; otherwise the row says "not applicable" with that error text. A variant that passes both but fails when run is listed `failed` with its error. One axis at a time, not a cross product. |
| 9 | M6.6 "runs whose data overlaps the holdout" | A run's dates come from its pack specs or, for `{"sha256"}` packs, the pack manifest. Paper sessions trade the present. `_cmd_run` and the API turn `RunConfigError` into exit 2 / 422. | Overlap = the run's last day ≥ `holdout_from`; an open `to` overlaps. Sessions are exempt. The reason is the existing `unlock_holdout` key. `HoldoutLocked` subclasses `RunConfigError`. A job's reason lives in the job config, every trial inherits it, and **one unlock row is logged per job**. **D-row.** |
| 10 | M6.5 `--train 120d` | "d" is not defined. | Calendar days; windows are half-open on calendar dates; `step ≥ test` so out-of-sample pieces never overlap. A partial last test window is dropped. **D-row.** |
| 11 | M6.4/M6.10 "a sweep is a family by default"; "every run joins the family" | The shipped example config already names a family; windows with different dates would pollute `V` and PBO. | Each sweep or walk-forward job is **its own family** (`sweep/<job_id>` / `walkforward/<job_id>`), the base config's `family_id` is ignored; walk-forward training runs are `trial`, test runs `oos`. So `N` of a walk-forward = windows × grid by construction, and the deflated Sharpe of its stitched curve uses that `N` and the variance of those Sharpes (conservative; not the maximum of anything). Trials are not pooled across separate sweeps: an owner who wants that accounting names one family by hand (`research families --name`) and runs trials through `run --config` with that `family_id`. **D-row, owner reviews.** |
| 12 | M6.4 "best run of a family" | Families pick their best by their objective (`sharpe`, `calmar`, `net_pnl`, …). | Rank and "best of N" use the family's objective; the deflated Sharpe is always that picked run's Sharpe; the sentence names the objective and the run the job actually picked. |
| 13 | M6.9 "VIX bucket (recorded `INDIA VIX`, else bhavcopy)" | Reading one instrument from a full-day archive is not cheap. | The bucket uses the previous trading day's close (what was known at the open); recorded first, bhavcopy otherwise, source shown per day; Task 10 measures and may flip the order with a D-row. |
| 14 | M6.3 "recorder coverage % (FULL and QUOTE)" | `data_quality` has per-instrument `coverage_pct`, no per-mode figure; modes live in the landing/archive `subscription_log`. | Task 27 adds two day-level metrics (`coverage_full_pct`, `coverage_quote_pct`) to `quality.py`; the matrix reads them and shows "not reported yet" until `data report` has run for the day. |
| 15 | M6.12 "determinism across platforms" | D-26: Windows only; DT-02 cross-platform checks are dropped. | The write-up reports determinism across repeated runs, builds and the oracle/engine pair on Windows, and says plainly that cross-platform was not measured. |
| 16 | M6 verification `python -m stockify sweep --config examples/sweeps/orb_grid.yaml` | Neither the command nor `examples/sweeps/` exists. | Task 17 creates the file, Task 18 the command. |
| 17 | M5 plan's "layouts ≤390 px, tap targets 44 px" | Superseded by D-83. | M6 pages are desktop (about 1024 px and up). |

---

### Task 1: M6.0 recon and provisional decisions (RCB 6.1)

**Files:**
- Modify: `docs/plan/RECON.md` (append `## 2026-11-12 — M6.0 recon (RCB 6.1)`), `docs/plan/M6_reporting_research.md` (add a "M6.0 recon" box)

**Interfaces:**
- Produces: the recorded BASE, the verified mismatch table, the provisional decisions (each task writes its own D-row), the provisional roster, and the list of 🛑 owner steps (this plan's gates table).

- [ ] **Step 1: Restate the base.** `git log -1 --format="%H %s"`, `git status --short`. Create the branch `stockify/m6-research` from BASE in its own worktree (skill `using-git-worktrees`) if it is not already there. Record BASE and any uncommitted file.
- [ ] **Step 2: Check the toolchain.** `.\.venv\Scripts\python.exe -c "import numpy, pandas, yaml, matplotlib; print(numpy.__version__, pandas.__version__, yaml.__version__)"` prints 2.5.x / 2.3.x / 6.0.x; `-c "import scipy"` raises `ModuleNotFoundError` (expected, so is `statsmodels`/`hypothesis`). Record that `requirements.txt` has PyYAML.
- [ ] **Step 3: Verify each mismatch row** with one search per row, for example `rg -n "_STUBS|compare-artifacts" stockify/__main__.py`, `rg -n "def compare_tests" server/routers/tests.py`, `rg -n "trial_families|holdout_unlocks|family_id|unlock_holdout" stockify server`, `rg -n "def slipped" stockify/oracle/matcher.py engine/src/matcher.cpp`, `rg -n "_rate_on|drawdown" stockify/oracle/metrics.py`, `rg -n "flat_paise|pct" stockify/config/charges.json`. Record file and line for each. Confirm that pack spec `from`/`to` are inclusive (`stockify/data/packs.py`) and that the API's POST `/api/tests` turns `RunConfigError` into 422.
- [ ] **Step 4: Read the Next.js 16 docs** pages for dynamic routes, `searchParams`, client components and forms in `web/node_modules/next/dist/docs/` and note the paths in RECON for the web tasks. Read `web/design.md` and `~/.claude/skills/hallmark/references/flow.md` § App screens once.
- [ ] **Step 5: Baseline.** Start the full test suite in the background (`.\.venv\Scripts\python.exe tests\run_tests.py`); record the result and exit code, and `cd web; npm run lint; npm run build`. Any pre-existing failure is recorded, not fixed here.
- [ ] **Step 6: Inventory the real lab (read-only).** Count `runs` rows, `tick_days` rows by status, `nse_fetch_log` datasets, cached bar ranges (`bar_fetch_log`) and which minute bars exist for RELIANCE. Record the counts only.
- [ ] **Step 7: Write the RECON section** (table, provisional decisions, roster, gates) and commit.

```bash
git add docs/plan/RECON.md docs/plan/M6_reporting_research.md
git commit -m "RCB 6.1: M6 recon: what the runs store, the stubs and the data layer really look like"
```

---

### Task 2: Research config, seeding, file helper and paths (AM 6.1)

**Files:**
- Modify: `stockify/config/research.json`, `stockify/config/loader.py`, `stockify/paths.py`, `tests/stockify/test_paths.py`
- Create: `stockify/research/__init__.py`, `stockify/research/seeding.py`, `stockify/research/fileio.py`
- Test: `tests/stockify/research/__init__.py`, `tests/stockify/research/test_config.py`, `tests/stockify/research/test_seeding.py`, `tests/stockify/research/test_fileio.py`

**Interfaces:**
- Produces: `stockify/config/research.json` (schema `stockify.research/1`; the existing `holdout_from: null` and `walk_forward` keys stay):

```json
{ "schema": "stockify.research/1", "holdout_from": null,
  "walk_forward": {"train_days": 120, "test_days": 30, "step_days": 30, "objective": "sharpe"},
  "bootstrap": {"mean_block": "10", "resamples": 2000, "ci": "0.90", "min_days": 20},
  "sensitivity": {"cost_scale": ["0", "1", "1.5", "2"], "extra_ticks": [0, 1, 2], "latency_ms": [0, 250, 1000],
                  "fragile_cost_scale": "1.5", "fragile_extra_ticks": 1},
  "regimes": {"reference_index": "NIFTY 50", "vix_symbol": "INDIA VIX", "vix_edges": ["13", "16", "20"],
              "gap_edges_pct": ["-0.5", "-0.1", "0.1", "0.5"], "thin_days": 10},
  "pbo": {"slices": 16, "min_trials": 2},
  "sweep": {"max_workers": null},
  "coverage": {"ok_pct": "99", "default_days": 60, "max_days": 400} }
```

- `research.settings() -> dict` (in `stockify/research/__init__.py`): the loaded, validated file.
- `seeding.STREAMS = {"bootstrap": 11, "pbo": 12, "random_search": 13}`, `seeding.ANCHOR = date(2000, 1, 3)`, `seeding.stream(seed: int, purpose: str, anchor: date | None = None) -> Pcg32` = `Pcg32(day_seed(seed, anchor or ANCHOR), STREAMS[purpose])`; an unknown purpose raises `KeyError`.
- `fileio.atomic_write_text(path: Path, text: str) -> None`: a temp file in the same folder, then `os.replace` (the pattern of `bundle._cache_write`); the temp file is removed on error.
- `paths.research_dir() -> Path`, `paths.research_path(job_id: str) -> Path` (one safe segment ≤ 64, direct child of `research_dir()`, else `ValueError`, like `run_path`), `paths.exports_dir() -> Path`, `paths.export_path(run_id: str, with_packs: bool) -> Path` (`<exports>/<run_id>.zip` or `<run_id>-with-packs.zip`; the id checked by `run_path`).

- [ ] **Step 1: Write the failing tests.**
  - `test_shipped_research_json_validates`.
  - `test_holdout_from_must_be_an_iso_date_or_null` (`"2026-13-40"` and `20260801` refused).
  - `test_bootstrap_ci_must_be_in_the_open_unit_interval` (`"0"`, `"1"`, `"0.9x"` refused; `resamples` ≥ 100; `mean_block` ≥ 1).
  - `test_decimal_fields_are_strings` (a JSON number for `cost_scale` or `ok_pct` refused, naming the key).
  - `test_edges_must_be_strictly_increasing`.
  - `test_unknown_key_refused_naming_it` (any level).
  - `test_pbo_slices_must_be_even_and_at_least_4`.
  - `test_coverage_days_limits` (`default_days ≤ max_days`, both ≥ 1; `ok_pct` in (0, 100]).
  - Seeding: `test_streams_are_not_the_engines` (no value in `{1, 2}`, all distinct), `test_same_seed_same_numbers`, `test_state_is_day_seed_of_the_anchor` (`stream(7, "pbo").state == Pcg32(day_seed(7, ANCHOR), 12).state`), `test_unknown_purpose_raises`.
  - `fileio`: `test_atomic_write_replaces_whole_file`, `test_failed_write_leaves_the_old_file_and_no_temp`.
  - Paths: `test_research_path_refuses_unsafe_ids` (`..`, `a/b`, `CON`, 65 characters), `test_exports_dir_is_under_the_lab`, `test_export_path_names_the_variant`.
- [ ] **Step 2: Run to verify failure.** `.\.venv\Scripts\python.exe tests\run_tests.py research` → FAIL (modules missing).
- [ ] **Step 3: Implement** the config file, the loader validator (today the loader checks only the schema string for `research.json`; follow how it validates the other `*.json`, and keep `tests/stockify/test_config_coverage.py` green), `seeding.py`, `fileio.py`, `settings()` and the four path helpers.
- [ ] **Step 4: Run** `tests\run_tests.py research`, `config` and `paths` → PASS; `.\.venv\Scripts\python.exe -m stockify config` validates.
- [ ] **Step 5: Commit.**

```bash
git add stockify/config/research.json stockify/config/loader.py stockify/paths.py stockify/research/__init__.py stockify/research/seeding.py stockify/research/fileio.py tests/stockify/test_paths.py tests/stockify/research/__init__.py tests/stockify/research/test_config.py tests/stockify/research/test_seeding.py tests/stockify/research/test_fileio.py docs/plan/M6_reporting_research.md
git commit -m "AM 6.1: research config, a seeded stream per purpose, an atomic file helper and the research and export folders"
```

---

### Task 3: Lab migration 0005 and store functions (AM 6.2) — M6.4

**Files:**
- Create: `stockify/data/migrations/lab/0005_research.sql`
- Modify: `stockify/store.py` (`SCHEMA_VERSION = 5`, `RUN_FIELDS`, new functions), `docs/plan/A1_schemas.md` §4
- Test: `tests/stockify/research/test_store_research.py`, `tests/stockify/test_store_migrations.py` (extend)

**Interfaces:**
- Produces, migration 0005 (additive only, I-13):

```sql
ALTER TABLE runs ADD COLUMN role TEXT NOT NULL DEFAULT 'trial';      -- trial | sensitivity | oos | rerun
ALTER TABLE runs ADD COLUMN job_id TEXT;                              -- the sweep / walk-forward / sensitivity job
ALTER TABLE trial_families ADD COLUMN named INTEGER NOT NULL DEFAULT 0;
ALTER TABLE holdout_unlocks ADD COLUMN job_id TEXT;
CREATE INDEX IF NOT EXISTS ix_runs_family ON runs (family_id, role, status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_unlock_job ON holdout_unlocks (job_id, reason) WHERE job_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS research_jobs (job_id TEXT PRIMARY KEY, kind TEXT NOT NULL,   -- sweep | walkforward | sensitivity
  name TEXT, family_id TEXT, status TEXT NOT NULL, created_ns INTEGER NOT NULL, finished_ns INTEGER,
  folder TEXT, spec_json TEXT,     -- the job's input (sweep spec, windows parameters)
  state_json TEXT,                 -- what the job decided: windows, chosen params, train and test run ids
  error TEXT);
CREATE TRIGGER IF NOT EXISTS runs_are_never_deleted BEFORE DELETE ON runs
  BEGIN SELECT RAISE(ABORT, 'runs are never deleted from the registry (M6.4)'); END;
CREATE TRIGGER IF NOT EXISTS holdout_unlocks_no_update BEFORE UPDATE ON holdout_unlocks
  BEGIN SELECT RAISE(ABORT, 'holdout unlocks are a log (M6.6)'); END;
CREATE TRIGGER IF NOT EXISTS holdout_unlocks_no_delete BEFORE DELETE ON holdout_unlocks
  BEGIN SELECT RAISE(ABORT, 'holdout unlocks are a log (M6.6)'); END;
```

- `store` (all new, same locking as the neighbours): `SCHEMA_VERSION = 5`; `RUN_FIELDS` gains `"role", "job_id"`; `add_family(family_id: str, *, objective: str, description: str | None = None, named: bool = False, now_ns: int | None = None) -> bool` (insert if absent; returns whether it inserted); `name_family(family_id: str, description: str) -> None` (sets `named = 1`); `get_family(family_id) -> dict | None`; `list_families() -> list[dict]`; `family_runs(family_id: str, *, roles: tuple[str, ...] | None = ("trial",), status: str | None = "done") -> list[dict]` (ordered by `name`, then `run_id`; never by time); `run_nav_series(run_id: str) -> list[tuple[str, int]]` (`(trade_date, nav_paise)` ascending from `run_equity`); `log_holdout_unlock(run_id: str, family_id: str | None, reason: str, *, job_id: str | None = None, now_ns: int | None = None) -> bool` (`INSERT OR IGNORE`, so a second run of the same `(job_id, reason)` adds nothing; returns whether a row was added); `holdout_unlock_rows(family_id: str | None = None) -> list[dict]`; `create_research_job(row: dict) -> None`, `update_research_job(job_id: str, **fields) -> bool`, `get_research_job(job_id) -> dict | None`, `list_research_jobs(kind: str | None = None, offset: int = 0, limit: int = 50) -> list[dict]`; `backfill_null_families(compute) -> int` (calls `compute(row) -> str | None` for each `runs` row whose `family_id IS NULL`, updates only that column, returns the count).

- [ ] **Step 1: Write the failing tests** (`SandboxedDBTest`, `db_modules = (stockify.store,)`):
  - `test_migration_0005_applies_over_a_populated_0004_lab` (rows from `runs`, `session_days`, `holdout_unlocks` intact; `store.schema_version() == 5`).
  - `test_runs_cannot_be_deleted` (`DELETE FROM runs` raises `sqlite3.DatabaseError`; the row is still there).
  - `test_holdout_unlock_log_is_append_only`.
  - `test_unlock_is_logged_once_per_job_and_reason` (two `log_holdout_unlock` calls with the same `job_id` and `reason` and different run ids add one row; a standalone run (`job_id=None`) adds one row each time).
  - `test_family_runs_orders_by_name_then_run_id_and_filters_roles_and_status`.
  - `test_run_nav_series_comes_from_run_equity`.
  - `test_research_job_roundtrip_with_state_json`.
  - `test_naming_a_family_survives_add_family` (`named` stays 1, description kept).
  - `test_backfill_updates_only_null_family_rows`.
- [ ] **Step 2: Run to verify failure.** `tests\run_tests.py research.test_store` and `store` → FAIL.
- [ ] **Step 3: Write `0005_research.sql`; implement the store functions** (never a raw `UPDATE` of anything but `family_id` in `backfill_null_families`). Update A1 §4 with the new columns, table and triggers. Grep `tests/` for `DELETE FROM runs`; none may remain.
- [ ] **Step 4: Run** `tests\run_tests.py research`, `store`, `runs` → PASS.
- [ ] **Step 5: Commit.**

```bash
git add stockify/data/migrations/lab/0005_research.sql stockify/store.py docs/plan/A1_schemas.md docs/plan/M6_reporting_research.md tests/stockify/research/test_store_research.py tests/stockify/test_store_migrations.py
git commit -m "AM 6.2: lab migration 0005: run roles, research jobs, an append-only unlock log, and runs that can never be deleted"
```

---

### Task 4: Trial registry: families, roles, the run integration (AM 6.3) — M6.4

**Files:**
- Create: `stockify/research/registry.py`, `tests/stockify/research/fixtures.py`
- Modify: `stockify/runs.py` (`queue`, `launch`, `rerun`, `import_run`), `stockify/sessionconfig.py` (default family)
- Test: `tests/stockify/research/test_registry.py`

**Interfaces:**
- Consumes: Task 3 store functions.
- Produces:
  - `registry.ROLES = ("trial", "sensitivity", "oos", "rerun")`; `registry.OBJECTIVES = ("sharpe", "sortino", "calmar", "cagr", "net_pnl")`.
  - `registry.default_family_id(cfg: dict, objective: str = "sharpe") -> str` = `"<strategy>/<dataset>-<interval>-<universe>/<objective>"`; **it ignores `cfg["family_id"]`**. `<strategy>` is the module's last part of a `python` ref (the registered name of a `cpp` ref, `script` otherwise); dataset and interval come from the first pack spec (`packs` when packs are named by hash only); `<universe>` is the first instrument with every character outside `[A-Za-z0-9._-]` replaced by `_`, plus `_plus<n>` for n more. **Each of the three segments is at most 64 characters** (the `runconfig` rule: every `/`-separated part passes `paths.safe_segment(part, 64)`) and the whole at most 128; an over-long universe part is truncated first (the `_plus<n>` suffix is kept).
  - `registry.job_family_id(kind: str, job_id: str) -> str` = `f"{kind}/{job_id}"` (`kind` `sweep` or `walkforward`; ≤ 64 each, guaranteed because job names are ≤ 40 characters).
  - `registry.family_for_session(cfg: dict) -> str` (same rule, dataset `live`); `registry.ensure_family(family_id: str, objective: str = "sharpe") -> None`.
  - `registry.objective_value(row: dict, objective: str) -> float | None`: from `metrics_json` (`sharpe`, `sortino`, `calmar`, `cagr`; `net_pnl` = `nav_end_paise − nav_start_paise`); `None` when the metric is null.
  - `TrialRun` frozen dataclass `(run_id, name, job_id, dates: tuple[str, ...], navs: tuple[int, ...], capital_paise: int, seed: int, objective_value: float | None)`; `registry.trials(family_id: str) -> list[TrialRun]` read from `run_equity`, `config_json` and `metrics_json` only (never a folder); `registry.best_trial(family_id: str) -> TrialRun | None`: the trial with the highest `objective_value` under the family's own objective (ties → lowest `name`, then `run_id`; trials with a `None` value are ineligible); `registry.backfill(dry_run: bool = False) -> int`.
  - `runs.queue(config, *, role="trial", job_id=None)` and `runs.launch(..., role="trial", job_id=None)` fill a missing `family_id` with `default_family_id(cfg)` (written into the stored normalised config too), call `ensure_family`, and store `role` and `job_id`. `runs.rerun` creates its row with `role="rerun"` and the source's stored family. **`import_run` and `rerun` keep the stored family** when the folder's `run.json` has none (a pre-M6 folder), and keep an existing row's `role` and `job_id`.
  - `tests/stockify/research/fixtures.py`: `make_run(run_id: str, family_id: str | None, *, daily_returns: list[float] | None = None, navs: list[int] | None = None, capital_paise: int = 100_000_000, role: str = "trial", status: str = "done", job_id: str | None = None, name: str | None = None, seed: int = 0, start: str = "2026-01-05", with_folder: bool = False) -> None` writes a `runs` row (with `config_json`, and a `metrics_json` made by `oracle.metrics.compute` on the same equity, so `sharpe`, `calmar`, `nav_start_paise`, `nav_end_paise` are real) and its `run_equity` rows over consecutive weekdays from `start`; with `with_folder` also a minimal run folder (`run.json`, `manifest.json`, `metrics.json`, `equity.csv`).

- [ ] **Step 1: Write the failing tests** (`SandboxedDBTest`, `db_modules = (stockify.store,)`):
  - `test_default_family_id_shape`: the ORB RELIANCE minute config (`examples/runs/orb_reliance_minute.json`, which carries its own `family_id`) → the computed id still starts with `opening_range_breakout/`, ends with `/sharpe`, and passes `runconfig.validate`; `NSE:RELIANCE` becomes `NSE_RELIANCE`.
  - `test_default_family_segments_are_at_most_64_characters`: a 90-character strategy module name and 200 instruments give three segments each ≤ 64 and a total ≤ 128, and `runconfig.validate` accepts the result.
  - `test_job_family_id_shape`.
  - `test_new_run_gets_the_default_family_and_a_family_row`; `test_explicit_family_is_kept`.
  - `test_variants_and_oos_runs_are_not_trials` (roles `sensitivity`, `oos`, `rerun` appear in `family_runs(roles=None)` but not in `trials`).
  - `test_failed_queued_and_running_runs_are_not_trials`.
  - `test_pruned_folder_still_counts` (delete the run folder; `trials()` unchanged, built from the database).
  - `test_best_trial_follows_the_family_objective` (same trials, objective `sharpe` picks one, `net_pnl` another; ties by name then run id; a null value is ineligible).
  - `test_session_gets_a_family_with_dataset_live`.
  - `test_import_run_keeps_the_stored_family_for_a_pre_m6_folder` and `test_rerun_keeps_the_family_and_marks_the_role_rerun`.
  - `test_backfill_fills_only_null_families_and_dry_run_changes_nothing`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** `registry.py`, the `runs.py` and `sessionconfig.py` changes and the fixture helper.
- [ ] **Step 4: Run** `tests\run_tests.py research`, `runs`, `session` → PASS.
- [ ] **Step 5: D-row** (roles; sessions not trials; job families, mismatch rows 4, 11, 12); M6.4 boxes; **Commit.**

```bash
git add stockify/research/registry.py stockify/runs.py stockify/sessionconfig.py docs/plan/00_overview.md docs/plan/M6_reporting_research.md tests/stockify/research/fixtures.py tests/stockify/research/test_registry.py
git commit -m "AM 6.3: every run and session has a family, and only real tries count"
```

---

### Task 5: Sharpe, probabilistic and deflated Sharpe (IL 6.1) — M6.4

**Files:**
- Create: `stockify/research/stats.py`
- Modify: `stockify/oracle/metrics.py` (public `rate_on`, extracted `max_drawdown` and `drawdown_series`; `compute` uses them)
- Test: `tests/stockify/research/test_stats.py`

**Interfaces:**
- Produces in `oracle/metrics.py` (behaviour of `compute` unchanged, byte for byte; `_rate_on = rate_on` stays as an alias for any caller): `rate_on(rows: list[dict], d: str) -> float`; `max_drawdown(navs: list[float]) -> tuple[float, int]` (the max drawdown and the longest duration, exactly the loop in `compute`, peak starting at `navs[0]`); `drawdown_series(navs: list[float]) -> list[float]` (`1 − NAV_d / peak` per day).
- Produces in `stats.py` (pure functions, no I/O, plain loops in A4 §8 order; undefined is `None`):
  - `Moments` frozen dataclass `(n: int, mean: float, sd: float, skew: float | None, kurt: float | None)`; `moments(x: list[float]) -> Moments` with `sd` sample (ddof 1), `skew = m3 / m2**1.5` and `kurt = m4 / m2**2` (**not** excess) using the population moments `m_k = Σ(x − mean)^k / n`; `skew`/`kurt` are `None` when `m2 == 0`.
  - `risk_free_rows() -> list[dict]` (`loader.load_json("pricing.json")["risk_free"]`).
  - `returns_from_nav(capital: int, navs: list[int]) -> list[float]`: `r_d = NAV_d / NAV_{d−1} − 1` with `NAV_0 = capital` (paise converted to float once).
  - `excess_returns(dates: list[str], r: list[float], rf_rows: list[dict]) -> list[float]`: `x_d = r_d − metrics.rate_on(rf_rows, dates[d]) / 252`.
  - `sharpe_per_period(x: list[float]) -> float | None`: `mean / sd`; **`None` when `n < 2` or when `min(x) == max(x)`** (A4 §8: the deviation is exactly 0, so the Sharpe is undefined, as in `metrics.json`); `annualised(sr: float) -> float` (`· √252`).
  - `expected_max_sharpe(n_trials: int, var_sharpe: float) -> float`: `0.0` for `n_trials ≤ 1`, else `√V · ((1 − γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e)))`, `γ = 0.5772156649015329`, `Φ = NormalDist()`.
  - `probabilistic_sharpe(sr: float, n: int, skew: float, kurt: float, benchmark: float = 0.0) -> float | None` = `Φ((sr − benchmark)·√(n − 1) / √(1 − skew·sr + ((kurt − 1)/4)·sr²))`; `None` when `n < 2` or the radicand is ≤ 0.
  - `DsrResult` frozen dataclass `(sr: float, t: int, skew: float, kurt: float, n_trials: int, var_sharpe: float, sr0: float, dsr: float | None, psr: float | None)`; `deflated_sharpe(x: list[float], trial_srs: list[float | None], n_trials: int) -> DsrResult | None` where `x` is the selected run's excess returns, `trial_srs` the family's per-period Sharpes (undefined ones are `None`: a flat trial counts in `n_trials` but not in `V`), `V` = sample variance (ddof 1) of the defined ones (0.0 when fewer than two). Returns `None` when `sharpe_per_period(x)` is `None` or `moments(x)` has no skew.

- [ ] **Step 1: Write the failing tests.**
  - **Hand-computed example** `test_dsr_matches_the_hand_computed_example`: `SR = 0.1`, `T = 250`, `skew = −0.5`, `kurt = 4.0`, `N = 10`, `V = 0.0004` give `SR₀ = 0.031491966026915`, `DSR = 0.8534253186458305`, `PSR = 0.9375434364545412`, each within 1e-9 (call `expected_max_sharpe(10, 0.0004)` and `probabilistic_sharpe` directly with `benchmark = SR₀` and `0.0`).
  - `test_one_trial_dsr_equals_psr` (`n_trials = 1` → `sr0 == 0.0`, `dsr == psr`).
  - `test_identical_trials_have_zero_expected_max` (`V = 0` → `sr0 == 0.0`, no exception).
  - `test_more_trials_raise_the_bar` (`expected_max_sharpe(100, v) > expected_max_sharpe(10, v) > expected_max_sharpe(2, v)`).
  - `test_moments_known_series`: `[0, 0, 3]` → skew `0.7071067811865476` (1e-12); `[−1, 1, −1, 1]` → kurt `1.0`; `[−1, 0, 0, 1]` → kurt `2.0`; constant series → `skew is None`.
  - `test_flat_series_has_no_sharpe_but_a_sentence` (here and in Task 7): `[0.0] * 30` and `[−0.001] * 30` → `sharpe_per_period is None` and `deflated_sharpe(...) is None`; a one-value series → `None`; no result field of any call is `NaN` or `inf` (`math.isfinite` on every float).
  - `test_flat_trial_counts_in_n_not_in_v` (`trial_srs = [0.1, None, 0.3]`, `n_trials = 3` → `V` is the variance of 0.1 and 0.3 only).
  - `test_radicand_not_positive_gives_none`.
  - `test_sharpe_matches_metrics_json`: call `oracle.metrics.compute(capital=…, equity=…, fills=[], orders={}, charges=[], risk_free=rf, rejects={}, participation_ppm=0, exposure=None)`; `annualised(sharpe_per_period(excess_returns(...)))` equals `metrics["sharpe"]` within 1e-12 with `rf` empty and with a dated 7% row, and both are `None` for a flat NAV.
  - `test_metrics_refactor_changes_nothing`: `max_drawdown` and `drawdown_series` hand-checked on `[100, 120, 90, 130, 80]` (drawdown 0, 0, 0.25, 0, 1 − 80/130; max 1 − 80/130, duration 2 … check against the old loop), and the existing oracle and golden tests stay green.
- [ ] **Step 2: Run to verify failure.** `tests\run_tests.py research.test_stats` → FAIL.
- [ ] **Step 3: Implement `stats.py` and the `metrics.py` extraction** (the loops move, they do not change).
- [ ] **Step 4: Run** `tests\run_tests.py research`, `oracle`, `golden`, `engine_parity` → PASS. Skill `dimensional-analysis` over the diff (report only).
- [ ] **Step 5: D-row** (excess returns, `None` for sd 0, `N`/`V` definition, non-excess kurtosis, ddof; mismatch row 5); **Commit.**

```bash
git add stockify/research/stats.py stockify/oracle/metrics.py docs/plan/00_overview.md docs/plan/M6_reporting_research.md tests/stockify/research/test_stats.py
git commit -m "IL 6.1: Sharpe, probabilistic and deflated Sharpe from the stdlib, checked against a hand-computed example and metrics.json"
```

---

### Task 6: Stationary block bootstrap intervals (IR 6.1) — M6.8

**Files:**
- Create: `stockify/research/bootstrap.py`
- Test: `tests/stockify/research/test_bootstrap.py`

**Interfaces:**
- Consumes: `seeding.stream`, `stats.sharpe_per_period`, `stats.annualised`, `oracle.metrics.max_drawdown`; the callers pass `mean_block`, `resamples`, `ci` and `min_days` from `research.settings()["bootstrap"]` (decimal strings converted to float at that edge).
- Produces:
  - `percentile(sorted_values: list[float], q: float) -> float` (linear interpolation between ranks, `q` in [0, 1]; no numpy).
  - `stationary_indices(n: int, mean_block: float, rng: Pcg32) -> list[int]` (Politis–Romano: a start index uniform on `[0, n)`; at each following step, with probability `1/mean_block` (an integer threshold on `next_u32`) restart at a new uniform index, else take the next index, wrapping circularly; length `n`).
  - `Interval` frozen dataclass `(point: float | None, lo: float | None, hi: float | None)`; `BootstrapResult` frozen dataclass `(n_days: int, mean_block: float, resamples: int, ci: float, seed: int, sharpe: Interval, mean_daily_return: Interval, max_drawdown: Interval, enough_days: bool)`.
  - `bootstrap_ci(r: list[float], x: list[float], *, mean_block: float, resamples: int, ci: float, min_days: int, seed: int, last_date: date) -> BootstrapResult`: `r` plain daily returns, `x` the matching excess returns. **The seed is the run config's `seed` key and `last_date` the run's last trading date**, so the generator is `seeding.stream(seed, "bootstrap", last_date)`, i.e. the existing `day_seed` feeding PCG (D-row). Each resample draws one index list applied to both series: `sharpe` = annualised `sharpe_per_period(x[idx])`, `mean_daily_return` = mean of `r[idx]`, `max_drawdown` from `oracle.metrics.max_drawdown` over the compounded path of `r[idx]` starting at 1.0. Intervals are the `(1−ci)/2` and `1−(1−ci)/2` percentiles; a resample with an undefined Sharpe is dropped from that interval only. `point` is the statistic on the original series. With fewer than `min_days` values: `enough_days = False` and every interval is `Interval(None, None, None)`.

- [ ] **Step 1: Write the failing tests.**
  - `test_percentile_hand_checked` (`[1, 2, 3, 4]`: q 0 → 1, 0.5 → 2.5, 1 → 4, 0.25 → 1.75).
  - `test_same_seed_same_result_and_other_seed_differs`.
  - `test_mean_block_one_is_iid_resampling` (over 20,000 draws the share of "next index = previous + 1" is within 0.02 of `1/n`).
  - `test_mean_block_length_is_about_the_setting` (mean run length of consecutive indices with `mean_block = 10` within 10% over 50,000 steps).
  - `test_indices_wrap_circularly` and `test_length_is_n`.
  - `test_constant_series_interval_collapses_to_the_point` (for `mean_daily_return` and `max_drawdown`; the Sharpe interval is `None` because every resample's Sharpe is undefined, with `enough_days` true).
  - `test_intervals_are_ordered` (`lo ≤ hi` for all three statistics on a noisy seeded series; `0 ≤ lo ≤ hi ≤ 1` for drawdown).
  - `test_interval_contains_the_point_for_mean_return_and_sharpe` (seeded noise with a positive drift, 250 days; drawdown is **not** asserted to bracket its point, because a percentile bootstrap of a maximum is biased: the test says so in its docstring).
  - `test_mean_interval_coverage_on_a_known_distribution`: 100 seeded repeats (n = 60 normal draws from the seeded stream with true mean 0.001, `resamples = 200`, `ci = 0.9`); the share of intervals containing the true mean lies in `[0.75, 0.99]` (deterministic, tuned once).
  - `test_short_series_bootstrap_says_so` (`n = 5 < min_days`: `enough_days is False`, all intervals `None`, no exception).
  - `test_zero_variance_resample_is_dropped_not_nan` (a series that is mostly flat so some resamples are flat: those resamples are skipped for the Sharpe interval, which is still finite).
  - `test_results_are_finite_json` (`json.dumps(dataclasses.asdict(result), allow_nan=False)` works).
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement `bootstrap.py`.** Plain loops; `resamples` × `n` ≈ 0.5M steps for a year, so no numpy needed.
- [ ] **Step 4: Run** → PASS. Time `resamples = 2000`, `n = 1500` and record the seconds in RECON (it justifies the cache in Task 8).
- [ ] **Step 5: Commit.**

```bash
git add stockify/research/bootstrap.py docs/plan/M6_reporting_research.md tests/stockify/research/test_bootstrap.py
git commit -m "IR 6.1: stationary block bootstrap with seeded 90% intervals for Sharpe, mean return and drawdown"
```

---

### Task 7: Plain words: the structure of `plain.py` (IL 6.2) — M6.4, M6.8

**Files:**
- Create: `stockify/research/plain.py`
- Test: `tests/stockify/research/test_plain.py`

**Interfaces:**
- Produces: **this task owns the structure of `plain.py`; later tasks add only their own builder, named below, and its test.**
  - `plain.KEYS` (tuple, complete from this task on) and `plain.meaning(key: str) -> str`, one line (≤ 110 characters, no Greek letters, no "ddof", "kurtosis", "skew", "logit") for each of: **run metrics** `net_pnl, return, cagr, sharpe, sortino, max_drawdown, calmar, trades, win_rate, costs, cost_share`; **research** `dsr, psr, expected_best_luck, trials, bootstrap_sharpe, bootstrap_mean_return, bootstrap_max_drawdown, holdout_unlocks`; **regimes** `regime_month, regime_weekday, regime_expiry, regime_vix, regime_gap`; **sensitivity** `fragile, cost_scale, extra_ticks, latency`; **walk-forward and PBO** `wf_ratio, wf_stability, pbo`. An unknown key raises `KeyError`.
  - Builders added **in this task**: `dsr_sentence(n_trials: int, rank: int, dsr: float | None, *, objective: str = "sharpe", picked_name: str | None = None, flat: bool = False) -> str`: rank 1 → "best of 12 tries by <objective label>, after allowing for luck: 85% confident the edge is real"; another rank → "4th best of 12 tries by <objective label>; the job picked <picked_name>, so this is a runner-up"; `n_trials == 1` → "the only try, so nothing to allow for"; `flat` → "no trades, so no Sharpe to judge"; `dsr is None` otherwise → "too few days to say"; `psr_sentence(psr: float | None) -> str`; `interval_sentence(label: str, interval, unit: str) -> str` ("between 0.4 and 1.9 in 9 of 10 re-shuffles of your own days"; `None` bounds → "too few days to say"). `objective label` maps `sharpe` → "steadiness of returns (Sharpe)", `net_pnl` → "profit after costs", and so on, for all `registry.OBJECTIVES`.
  - Builders added **by later tasks**: `regime_sentence` (Task 10), `fragile_sentence` (Task 16), `wf_sentence` (Task 22), `pbo_sentence` (Task 23). Until its task lands, a builder does not exist; `KEYS` already holds its meaning.
- Wording is refined with skill `design:ux-copy` (report only; the implementer applies it). The phrases "best of N tries" and "after allowing for luck" are fixed.

- [ ] **Step 1: Write the failing tests:** `test_every_key_has_a_one_line_meaning` (every entry of `KEYS`, at most 110 characters, one line), `test_meanings_have_no_jargon_symbols`, `test_unknown_key_raises`, `test_dsr_sentence_for_best_of_12` (contains "best of 12 tries" and "after allowing for luck"), `test_dsr_sentence_names_the_objective_and_the_picked_run_for_a_runner_up`, `test_dsr_sentence_for_a_single_try`, `test_dsr_sentence_for_a_flat_run`, `test_dsr_sentence_when_there_are_too_few_days`, `test_interval_sentence_with_and_without_bounds`, `test_sentences_have_no_greek_letters_or_raw_codes`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit.**

```bash
git add stockify/research/plain.py docs/plan/M6_reporting_research.md tests/stockify/research/test_plain.py
git commit -m "IL 6.2: one plain-English sentence for every statistic, written once"
```

---

### Task 8: Run and family reports, the cache, the research CLI (IL 6.3) — M6.4, M6.8

**Files:**
- Create: `stockify/research/report.py`
- Modify: `stockify/__main__.py` (new `research` command)
- Test: `tests/stockify/research/test_report.py`

**Interfaces:**
- Consumes: Tasks 3–7.
- Produces:
  - `report.RunResearch` frozen dataclass `(run_id, family_id, role, objective: str, n_trials: int, rank: int | None, is_best: bool, picked_run_id: str | None, picked_name: str | None, dsr: DsrResult | None, bootstrap: BootstrapResult | None, regimes: dict | None, holdout_unlocks: int, plain: dict[str, str])` (`regimes` stays `None` until Task 10 fills it).
  - `report.run_research(run_id: str, *, use_cache: bool = True) -> RunResearch`: loads the run's NAV rows (`store.run_nav_series`), builds `r` and `x`, finds the family's trials, `V` from their per-period Sharpes; **`rank`, `is_best` and `picked_*` follow the family's objective** (`registry.best_trial`, ties by name then run id); the deflated Sharpe is that run's own Sharpe against the family's `N` and `V` (the sentence says which objective picked and which run); runs with role other than `trial` get `rank = None` and say they are not a try. The bootstrap uses the run config's `seed` and last trade date. The cache is `runs/<id>/research.json`, written with `fileio.atomic_write_text`, keyed by a SHA-256 of `(returns, bootstrap settings, seed, CODE_VERSION constant)`; a miss recomputes; a missing folder skips the write silently.
  - `report.TrialRow` `(run_id, name, role, status, objective_value, sharpe_per_period: float | None, net_pnl_paise: int | None, days: int, job_id)`; `report.FamilyReport` `(family_id, description, objective, named, n_trials, var_sharpe, best_run_id, best: DsrResult | None, trials: list[TrialRow], others: list[TrialRow], sessions: list[dict], holdout_unlocks: list[dict], plain: dict[str, str])` (`pbo` is added by Task 23); `report.family_report(family_id: str) -> FamilyReport`; `report.FamilySummary` `(family_id, description, named, objective, n_trials, n_other_runs, n_sessions, best_run_id, best_dsr: float | None, unlocks: int)`; `report.list_families() -> list[FamilySummary]` (`unlocks` counts rows of `holdout_unlocks`, i.e. jobs or standalone runs, not trials).
  - CLI: `python -m stockify research families [--backfill] [--dry-run] [--name FAMILY_ID "description"]` (prints a table; `--backfill` is `registry.backfill`), `python -m stockify research show <family_id | run_id>` (prints the lines the pages will show, in plain words).

- [ ] **Step 1: Write the failing tests:**
  - `test_run_research_for_the_best_run` (a family of 12 trials with known Sharpes: `n_trials == 12`, `rank == 1`, `is_best`, `dsr.sr0 == stats.expected_max_sharpe(12, V)`).
  - `test_rank_and_best_follow_the_objective` (objective `net_pnl`: the run with the highest net P&L is best even though another has the higher Sharpe; the sentence names the objective and the picked run).
  - `test_a_runner_up_says_which_run_the_job_picked`.
  - `test_one_trial_family_reports_the_only_try`.
  - `test_variants_do_not_change_n` (add five `sensitivity` runs; `n_trials` unchanged; their `rank` is `None`).
  - `test_flat_run_has_no_sharpe_and_a_sentence`.
  - `test_cache_file_is_written_and_reused` (the second call does not call `bootstrap_ci`, via a patched counter) and `test_cache_is_invalidated_when_returns_or_seed_change`.
  - `test_missing_folder_computes_without_caching`.
  - `test_family_report_lists_sessions_and_unlocks_without_counting_them`.
  - `test_report_is_finite_json` (`allow_nan=False`) for a flat series, a one-day run and a failed run.
  - CLI: `test_research_show_prints_the_plain_sentence` (capture stdout), `test_research_families_name_sets_the_flag`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** `report.py` and the `research` command.
- [ ] **Step 4: Run** `tests\run_tests.py research` → PASS.
- [ ] **Step 5: Commit.**

```bash
git add stockify/research/report.py stockify/__main__.py docs/plan/M6_reporting_research.md tests/stockify/research/test_report.py
git commit -m "IL 6.3: run and family research reports, a derived-file cache and the research command"
```

---

### Task 9: Research API: run research and families (WF 6.1) — M6.4, M6.8

**Files:**
- Create: `server/routers/research.py`, `tests/stockify/test_api_research.py`
- Modify: `server/schemas.py`, `web/src/lib/types.ts` (blockwatch pair), `server/routers/tests.py` (add `/api/tests/{id}/research`), `server/main.py` (register the router), `docs/plan/A2_interfaces.md` §6

**Interfaces:**
- Consumes: `report.run_research`, `report.family_report`, `report.list_families`.
- Produces DTOs (appended to both files, same order): `IntervalDTO(point, lo, hi)`, `DsrDTO(sr, t, skew, kurt, n_trials, var_sharpe, sr0, dsr, psr)`, `BootstrapDTO(n_days, mean_block, resamples, ci, enough_days, sharpe, mean_daily_return, max_drawdown)`, `RunResearchDTO(run_id, family_id, role, objective, n_trials, rank, is_best, picked_run_id, picked_name, dsr, bootstrap, regimes, holdout_unlocks, plain)` with **`regimes` typed loosely as `dict[str, Any] | None` / `Record<string, unknown> | null`**, which Task 10 tightens in both files without reordering any field, `TrialRowDTO`, `FamilySummaryDTO`, `FamilyDetailDTO`. All floats are `float | None`; responses serialise with `allow_nan=False` semantics (a test asserts it).
- Routes: `GET /api/tests/{run_id}/research` (404 for an unknown run; 200 with `dsr: null` and a sentence for a run too short to score); `GET /api/research/families?offset&limit` and `GET /api/research/families/{family_id:path}` (404 unknown). Declared with the other fixed paths first.

- [ ] **Step 1: Write the failing tests** (TestClient as in `test_api_tests.py`; loopback Host): `test_run_research_returns_dsr_and_bootstrap`, `test_run_research_404`, `test_run_research_for_a_failed_run_is_empty_not_an_error`, `test_families_list_and_detail` (family ids containing `/`), `test_responses_have_no_nan`, `test_non_loopback_host_refused` (D-77).
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement the routes and DTOs; mirror them in `types.ts`;** `~/.tools/blockwatch/blockwatch.exe` clean. Skills: `sharp-edges`, `insecure-defaults` over the diff (report only).
- [ ] **Step 4: Run** `tests\run_tests.py api_research` and `api_tests` → PASS.
- [ ] **Step 5: Commit.**

```bash
git add server/routers/research.py server/routers/tests.py server/main.py server/schemas.py web/src/lib/types.ts docs/plan/A2_interfaces.md docs/plan/M6_reporting_research.md tests/stockify/test_api_research.py
git commit -m "WF 6.1: API for a run's deflated Sharpe and bootstrap intervals and for trial families"
```

---

### Task 10: Regime breakdown (IR 6.2) — M6.9

**Files:**
- Create: `stockify/research/regimes.py`
- Modify: `stockify/research/report.py` (`regimes` filled, and the cache key extended), `stockify/research/plain.py` (add `regime_sentence`), `server/schemas.py`, `web/src/lib/types.ts`
- Test: `tests/stockify/research/test_regimes.py`

**Interfaces:**
- Consumes: `store.run_nav_series`, `calendar`, refdata (`instruments`, `index_eod`, `tick_days`), `ticks`, `research.settings()["regimes"]`. `index_eod` holds instrument ids, so the reference index and the VIX symbol are resolved to ids through refdata `instruments` by trading symbol.
- Produces:
  - `Bucket` frozen dataclass `(label: str, days: int, pnl_paise: int, mean_pnl_paise: int | None, up_days: int, up_share: float | None, thin: bool)` (`pnl_paise` is the exact sum of daily NAV changes; `mean_pnl_paise` rounded half away from zero; `thin` when `days < thin_days`).
  - `RegimeReport` `(month: list[Bucket], weekday: list[Bucket], expiry: list[Bucket], vix: list[Bucket], gap: list[Bucket], vix_source: dict[str, str], notes: list[str])`; `regimes.report(run_id: str) -> RegimeReport`. Daily P&L of day d = `NAV_d − NAV_{d−1}` (day 1 against capital).
  - `expiry_days(start: date, end: date) -> set[date]`: dates on which an index future or option of the `index_underlyings` expires, from refdata instruments (`COALESCE(actual_expiry, expiry)`, I-6, D-41), falling back to `calendar.generated_expiries` where refdata has none; buckets "expiry day" and "other days".
  - `vix_close(day: date) -> tuple[int, str] | None` (points × 100 as stored, source `"recorded"` or `"bhavcopy"`): recorded `INDIA VIX` from the day's tick archive first, else `index_eod`; `None` when neither. A day's VIX bucket uses the **previous trading day's** close, with edges `vix_edges` (4 buckets).
  - `opening_gap(day: date) -> int | None`: basis points of `open / previous close − 1` of `reference_index` from `index_eod`; buckets by `gap_edges_pct` (5 buckets). Days with no value go into a bucket labelled "no data" (counted, never dropped silently).
  - **`regimes.refdata_digest(start: date, end: date) -> str`**: SHA-256 over the `index_eod` rows of the reference index and the VIX symbol in `[start − 7 days, end]`, the instrument expiry rows in that range, and the `tick_days` rows (date, status, digest) in the range. `run_research`'s cache key now includes it, so late-arriving bhavcopy or a newly archived day invalidates the cached regimes.
  - Weekday labels Monday–Friday from the IST trading date; month labels `YYYY-MM`. `plain.regime_sentence(bucket: Bucket) -> str` ("Mondays: 41 days, average +₹312 a day, up on 56% of them"; thin buckets say "only N days, take with care"). `RegimeBucketDTO`/`RegimeReportDTO` replace the loose `regimes` type in both files (field order unchanged).

- [ ] **Step 1: Write the failing tests** (`SandboxedDBTest` with refdata, fixtures built through the helpers used by `tests/stockify/data`): `test_weekday_buckets_sum_to_the_run` (Σ `days` and Σ `pnl_paise` equal the totals for every dimension), `test_month_buckets_hand_checked` (a 6-day fixture with known P&L), `test_expiry_vs_other_days`, `test_vix_bucket_uses_the_previous_close`, `test_vix_prefers_recorded_and_labels_the_source`, `test_vix_falls_back_to_bhavcopy`, `test_missing_vix_goes_to_no_data_not_dropped`, `test_gap_buckets_hand_checked` (±0.3% and ±0.7% gaps), `test_thin_buckets_are_flagged`, `test_run_with_one_day_has_one_bucket_each`, `test_empty_refdata_gives_no_data_buckets_and_a_note`, `test_regime_cache_is_invalidated_when_bhavcopy_arrives_late` (cached regimes, then add an `index_eod` row: the digest changes and the regimes are recomputed), `test_regime_sentence_plain_and_thin`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement `regimes.py`; wire it into `run_research`; add the DTOs.** Measure `vix_close` on one real archive day (read-only) and record the seconds; if more than 5 s per day, make bhavcopy the first source, record a D-row and keep "recorded" as the fallback.
- [ ] **Step 4: Run** `tests\run_tests.py research` and `api_research` → PASS; blockwatch clean.
- [ ] **Step 5: Commit.**

```bash
git add stockify/research/regimes.py stockify/research/report.py stockify/research/plain.py server/schemas.py web/src/lib/types.ts docs/plan/M6_reporting_research.md tests/stockify/research/test_regimes.py
git commit -m "IR 6.2: performance by month, weekday, expiry day, VIX level and opening gap"
```

---

### Task 11: Locked holdout (PM 6.1) — M6.6

**Files:**
- Create: `stockify/research/holdout.py`
- Modify: `stockify/runs.py` (`queue`, `launch`), `stockify/runconfig.py` (validate `unlock_holdout`), `stockify/__main__.py` (`run --unlock-holdout`, `oracle run --unlock-holdout`), `stockify/research/report.py`, `docs/plan/A1_schemas.md` §6.1
- Test: `tests/stockify/research/test_holdout.py`

**Interfaces:**
- Produces:
  - **`class HoldoutLocked(runconfig.RunConfigError)`** (message names the holdout date, the run's last day and how to unlock). Because it is a `RunConfigError`, `_cmd_run` exits 2 and the API's POST handler answers 422 without new handlers.
  - `holdout.holdout_from() -> date | None`.
  - `holdout.run_span(cfg: dict) -> tuple[date, date | None] | None`: the earliest `from` and the latest `to` over the pack specs (`to` absent → `None`, open-ended); for a `{"sha256"}` pack the dates of the pack's manifest (`stockify.data.packs`); `None` when no dates are known.
  - `holdout.enforce(cfg: dict) -> str | None`: `None` when the holdout is off or not overlapped; the reason when overlapped **and** `cfg["unlock_holdout"]` is a string of at least 3 non-space characters; raises `HoldoutLocked` when overlapped otherwise, and when the span is unknown while a holdout is set (fail closed). Overlap rule: `last_day ≥ holdout_from`, or open-ended.
  - `runs.queue` and `runs.launch` call `enforce` before creating a row and, when a reason applies, `store.log_holdout_unlock(run_id, family_id, reason, job_id=job_id)`; a job's runs share a `job_id`, so **one row per `(job_id, reason)`** is logged however many runs the job makes, and a standalone run logs its own row. A `launch(run_id=…)` of an already queued run never logs again. `runconfig.validate` accepts `unlock_holdout` only as `null` or such a string (D-76 style message).
  - **Job plumbing (used by Tasks 15, 18, 20):** a job's `unlock_reason` is written into the job's config and copied into every trial config as `unlock_holdout`; there is no other path.
  - CLI: `--unlock-holdout "<reason>"` on `run` sets the config key. `report.family_report` counts and lists the family's unlock rows (`FamilyReport.holdout_unlocks`).

- [ ] **Step 1: Write the failing tests** (holdout set to `2026-08-01` with `isolated_env`/patched settings): `test_off_by_default_never_locks`, `test_run_ending_the_day_before_passes` (`to = 2026-07-31`), `test_run_ending_on_the_holdout_day_is_locked`, `test_range_crossing_the_line_is_locked`, `test_open_ended_pack_is_locked`, `test_sha_pack_is_checked_through_its_manifest`, `test_unknown_span_fails_closed`, `test_unlock_needs_a_real_reason` (`""`, `"  "`, `"x"` refused), `test_unlock_runs_and_is_logged_once_with_run_and_family`, `test_queue_then_launch_logs_once`, `test_twelve_runs_of_one_job_log_one_unlock`, `test_holdout_locked_is_a_run_config_error_exit_2_and_api_422_naming_the_date`, `test_unlock_count_appears_on_the_family_report`, `test_paper_sessions_are_exempt`, `test_cli_flag_sets_the_reason`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.** Skills: `differential-review` over this diff (it guards the research rules), `sharp-edges`.
- [ ] **Step 4: Run** `tests\run_tests.py holdout`, `runconfig`, `runs`, `api_tests` → PASS.
- [ ] **Step 5: D-row** (scope, fail-closed, sessions exempt, reason in `unlock_holdout`, one row per job; mismatch row 9); 🛑 owner choice of `holdout_from` recorded in RECON as pending; **Commit.**

```bash
git add stockify/research/holdout.py stockify/runs.py stockify/runconfig.py stockify/__main__.py stockify/research/report.py docs/plan/A1_schemas.md docs/plan/00_overview.md docs/plan/M6_reporting_research.md tests/stockify/research/test_holdout.py
git commit -m "PM 6.1: a locked holdout: runs over it need a reason, and every unlock is logged once per job and counted"
```

---

### Task 12: Bounded parallel run pool (PM 6.2) — shared by M6.5, M6.7, M6.10

**Files:**
- Create: `stockify/research/pool.py`
- Modify: `stockify/__main__.py` (`run --role`, `run --job-id`)
- Test: `tests/stockify/research/test_pool.py`

**Interfaces:**
- Produces:
  - `PoolJob` frozen dataclass `(config: dict, role: str = "trial", job_id: str | None = None, impl: str = "engine")`; `PoolOutcome` frozen dataclass `(run_id: str | None, status: str, error: str | None)` with `status` `done` or `failed`.
  - `default_workers(requested: int | None = None) -> int` = `min(requested or ∞, max(1, os.cpu_count() − 1))` and at least 1; `research.settings()["sweep"]["max_workers"]` caps it when set.
  - `run_all(jobs: list[PoolJob], *, workers: int | None = None, popen=subprocess.Popen, tmp_dir: Path | None = None) -> list[PoolOutcome]`: writes each config to a temp JSON (in a throw-away folder under `research_dir()`), starts `[sys.executable, "-m", "stockify", "run", "--config", file, "--role", role, "--job-id", id, "--impl", impl, "--no-report"]` with the parent's environment, at most `workers` at once, collects `stdout` (the run folder, whose name is the run id) and the exit code (0 done, 1 failed, 2 config refused, including a locked holdout: `failed` with stderr's last line as `error`), returns outcomes **in input order**, removes the temp files. A KeyboardInterrupt kills the children and re-raises.
  - CLI `run --role {trial,sensitivity,oos,rerun}` and `--job-id` are passed to `runs.launch`.

- [ ] **Step 1: Write the failing tests** with a fake `popen` (a class with `communicate`, `poll`, `returncode`, `kill`, counting concurrent live instances): `test_at_most_workers_run_at_once`, `test_results_are_in_input_order_even_when_jobs_finish_out_of_order`, `test_failed_job_is_reported_not_raised`, `test_refused_config_exit_2_has_the_message`, `test_default_workers_is_cpu_minus_one_and_at_least_one` (patch `os.cpu_count` to 1, 2, 16, `None`), `test_sweep_max_workers_setting_caps_it`, `test_temp_files_are_removed`, `test_children_inherit_the_sandbox_environment` (the env passed to `popen` has the test's `LOCALAPPDATA`), `test_keyboard_interrupt_kills_children`, and one real-process smoke `test_two_real_subprocesses_run_a_tiny_config` (oracle impl, the fixture used by `test_runs_launch.py`; both rows exist with `role` and `job_id` stored).
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement `pool.py`** with `popen` injectable and polling (`poll()` every 0.2 s; no threads needed).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit.**

```bash
git add stockify/research/pool.py stockify/__main__.py docs/plan/M6_reporting_research.md tests/stockify/research/test_pool.py
git commit -m "PM 6.2: a bounded pool of run subprocesses for sweeps, walk-forward and sensitivity"
```

---

### Task 13: `costs.scale` in the run config (FW 6.1) — enables M6.7

**Files:**
- Modify: `stockify/runconfig.py` (`DEFAULTS["costs"]`, validation), `stockify/bundle.py` (`resolve_bundle`), `stockify/oracle/report.py` (`LABEL_TEXT`), `stockify/runs.py` (re-validation accepts the key), `docs/plan/A1_schemas.md` §6.1, `web/src/app/tests/ui.tsx` (`labelMeaning`)
- Test: `tests/stockify/test_costs_scale.py`

**Interfaces:**
- Produces: run-config key `costs.scale`, a decimal string in `["0", "10"]` (exact via `numerics`), default `"1"`, **left out of the normalised form when it equals 1** (so every existing run config, bundle and hash is unchanged, like `seed_mode`). `bundle.scale_charges(charges: dict, scale: Decimal) -> dict`, applied once by `resolve_bundle` to the dated `charges` section:
  - **percentage and rate strings** (the `CHARGE_RATE_FIELDS` of the loader) are multiplied exactly in `Decimal` and re-encoded as decimal strings;
  - **integer paise leaves** (names ending `_paise`, such as `flat_paise`) are computed exactly as `value × scale` and **rounded once to whole paise, half-up, through `stockify.numerics`** (`mul_div_round_half_up` with `d = 10^k`, `k ≥ 1` the scale's decimal places; an integer scale needs no rounding);
  - dates, ids, `verified` flags and `source` text are untouched; a numeric leaf of a class the function does not know makes it raise, so a future new field cannot be skipped silently.
  The manifest gets the label `costs:scaled` when scale ≠ 1. Both engines read the bundle, so neither changes.

- [ ] **Step 1: Read `stockify/config/charges.json` and A1 §7.2** and list the leaf classes in the test's docstring.
- [ ] **Step 2: Write the failing tests:** `test_scale_one_is_byte_identical_to_no_key` (bundle bytes and `determinism.json` equal), `test_scale_one_left_out_of_the_normalised_config`, `test_scale_zero_gives_zero_charges` (oracle run on a golden fixture: every `charges.csv` total is 0), `test_scale_two_doubles_every_charge_leaf_in_the_bundle` (assert on the **bundle's `charges` section**, where `"2"` is an integer scale so doubling is exact for rates and `flat_paise` alike, not on the rounded `charges.csv`), `test_scale_one_and_a_half_rounds_each_paise_leaf_once_half_up` (`flat_paise` 2000 → 3000; a leaf of 1999 → 2999, not 2998), `test_percentage_strings_scale_exactly` (`"0.00297"` × `"1.5"` → `"0.004455"`), `test_scale_applies_to_flat_amounts_too`, `test_scale_refused_when_negative_above_ten_or_not_a_string` (each names `costs.scale`), `test_unknown_charge_leaf_raises`, `test_manifest_carries_the_label`, `test_engine_and_oracle_agree_with_scaled_costs` (skips when the engine is not loadable; CSVs byte-identical for scale `"1.5"`), `test_scale_changes_the_bundle_hash`.
- [ ] **Step 3: Run to verify failure.**
- [ ] **Step 4: Implement.** Skills: `sharp-edges`, `insecure-defaults`, `dimensional-analysis` (money scaling, report only).
- [ ] **Step 5: Run** `tests\run_tests.py costs_scale`, `bundle`, `runconfig`, `oracle`, `engine_parity` → PASS.
- [ ] **Step 6: D-row** (mismatch row 6; the rounding rule); **Commit.**

```bash
git add stockify/runconfig.py stockify/bundle.py stockify/oracle/report.py stockify/runs.py web/src/app/tests/ui.tsx docs/plan/A1_schemas.md docs/plan/00_overview.md docs/plan/M6_reporting_research.md tests/stockify/test_costs_scale.py
git commit -m "FW 6.1: a costs.scale run-config key, applied once to the bundle's charge rows"
```

---

### Task 14: `fill_model.slippage.extra_ticks` on both runners (FW 6.2) — enables M6.7

**Files:**
- Modify: `stockify/runconfig.py` (`SLIPPAGE_KEYS`, validation), `stockify/oracle/matcher.py` (`slipped`), `engine/src/matcher.cpp` (`slipped`), `docs/plan/A4_engine_semantics.md` §4.4, `docs/plan/A1_schemas.md` §6.1
- Test: `tests/stockify/test_extra_ticks.py`; engine doctest in `engine/tests/` beside the existing matcher tests

**Interfaces:**
- Produces: `fill_model.slippage.extra_ticks`, an int in `[0, 1000]`, allowed for **every** slippage kind (`none`, `fixed_ticks`, `random_ticks`, `bps`), default 0, **omitted from the normalised form when 0** (existing hashes unchanged). **Exact definition (A4 §4.4 amended):** for a fill that takes slippage, `move` is computed by the kind as today (for `random_ticks` the RNG draw is made exactly as today, so the stream does not shift), then `move = move + extra_ticks · tick`; the existing adverse sign (buy up, sell down) and the `cap` (protection or limit price) apply afterwards. Fills that take no slippage (resting limit fills) are unchanged. **It needs an engine change:** `engine/src/matcher.cpp::slipped` reads `slip.contains("extra_ticks")` with `checked_mul`/`checked_add`, mirrored line for line in `oracle/matcher.py::slipped`. The bundle already carries the `fill_model.slippage` object to both runners (verify in `bundle.py`).

- [ ] **Step 1: Write the failing tests:** `test_extra_ticks_zero_is_left_out_and_changes_no_hash`, `test_extra_ticks_accepted_for_every_kind_and_refused_below_zero_above_1000_or_non_int`, `test_slipped_adds_extra_ticks_for_each_kind` (hand-checked: buy at 10000, tick 5, `bps` bp 10 → ceil_to(10, 5) = 10 → +10, plus 2 extra ticks → +20; `fixed_ticks` k 1 + 2 extra → +15), `test_cap_still_limits_the_move`, `test_random_ticks_rng_draw_is_unchanged_by_extra_ticks` (the next draw after a fill is the same with and without `extra_ticks`), `test_sell_side_moves_down`, `test_engine_and_oracle_agree_with_extra_ticks` (a bar run and a snapshot run for each of the four kinds, CSVs byte-identical; skips when the engine is not loadable), and a C++ doctest for `slipped` with the same numbers.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** the validation, the oracle change and the C++ change (skills `modern-cpp` while writing, then `c-review` over the diff); rebuild with `.\run.ps1 -BuildEngine` (builds, runs ctest).
- [ ] **Step 4: Run** `tests\run_tests.py extra_ticks`, `engine_parity`, `golden`, `runconfig` → PASS; `ctest` green.
- [ ] **Step 5: D-row** (mismatch row 7; the engine change; A4 §4.4 text); 🛑 note in RECON that the engine must be rebuilt on any machine before the sensitivity task; **Commit.**

```bash
git add stockify/runconfig.py stockify/oracle/matcher.py engine/src/matcher.cpp engine/tests/test_matcher_extra_ticks.cpp docs/plan/A4_engine_semantics.md docs/plan/A1_schemas.md docs/plan/00_overview.md docs/plan/M6_reporting_research.md tests/stockify/test_extra_ticks.py
git commit -m "FW 6.2: extra slippage ticks on top of any slippage model, the same on the engine and the oracle"
```

(If the engine tests live in an existing file, name that file in the `git add` instead of a new one.)

---

### Task 15: Sensitivity suite: variants, applicability, run, CLI (FW 6.3) — M6.7

**Files:**
- Create: `stockify/research/sensitivity.py`
- Modify: `stockify/__main__.py` (`sensitivity` leaves `_STUBS`)
- Test: `tests/stockify/research/test_sensitivity.py`

**Interfaces:**
- Consumes: `pool.run_all`, `registry`, `stats`, `holdout` (through the runs), `fileio`, `research.settings()["sensitivity"]`, `store.get_run`, `runconfig.validate`, `bundle.resolve_bundle`.
- Produces:
  - `Variant` frozen dataclass `(key: str, axis: str, label: str, config: dict)` with `axis` ∈ `baseline, costs, slippage, latency, fill_model`; keys like `costs_x1.5`, `slip_plus1`, `latency_250`, `fill_snapshot_quote`.
  - `variants(cfg: dict, settings: dict | None = None) -> list[Variant]`: the stored normalised config with, one axis at a time: `costs.scale` ∈ `cost_scale`; `fill_model.slippage.extra_ticks` = base `extra_ticks` + each of `extra_ticks` (works for every kind, `bps` included, Task 14); `latency_ms` ∈ `latency_ms`; and every other `fill_model.kind`. Variants whose canonical config equals the baseline's (the `×1`, `+0`, latency 0, the baseline kind) are dropped; the baseline is first and is **not re-run** (its numbers come from the source run). **The source's `unlock_holdout` is dropped** from every variant; the job's own reason (below) is set instead. Variant run names are `<name>-<key>` with the name part truncated so the whole is ≤ 64 characters and a safe segment.
  - `applicable_fill_models(cfg: dict) -> dict[str, str | None]`: kind → `None` when `runconfig.validate` and a dry `bundle.resolve_bundle` accept the variant config, else the error text in plain words (no rule is re-encoded here).
  - `SensitivityRow` `(key, axis, label, run_id: str | None, status: str, net_pnl_paise: int | None, sharpe: float | None, delta_net_paise: int | None, error: str | None)` (`status` ∈ `baseline, done, failed, not_applicable`); `SensitivityResult` `(run_id, job_id, rows, baseline_net_paise, fragile: bool, verdict: str, reasons: list[str])` with `verdict` ∈ `fragile, robust, already_losing`.
  - `run_suite(run_id: str, *, workers: int | None = None, unlock_reason: str | None = None, pool_run=pool.run_all, now_ns: int | None = None) -> SensitivityResult`: creates a `research_jobs` row (kind `sensitivity`, job id `sens-<run name ≤ 30>-<stamp>-<6 hex>`, family = the source run's), runs every variant with role `sensitivity` and the job id (the holdout reason, if any, in every variant config; one unlock row per job, Task 11), writes `runs/<id>/sensitivity.json` with `fileio.atomic_write_text`, updates the job. **Fragile** (spec): baseline net P&L > 0 and net P&L < 0 under `costs.scale == fragile_cost_scale` **or** under `fragile_extra_ticks` extra ticks; a baseline ≤ 0 is `already_losing`, not fragile. Net P&L = `nav_end_paise − nav_start_paise`. A variant that fails is a row with its error, never dropped.
  - `format_table(res: SensitivityResult) -> str`; CLI `python -m stockify sensitivity --run RUN_ID [--workers N] [--unlock-holdout R]` prints it and exits 0 (1 when any variant failed unexpectedly, 2 for an unknown or unfinished run).

- [ ] **Step 1: Write the failing tests** (stub `pool_run` returning canned outcomes with runs built by `fixtures.make_run`): `test_variants_are_one_axis_at_a_time_and_skip_the_baseline_duplicates`, `test_extra_ticks_variants_apply_to_a_bps_base` (the demo ORB config: variants exist with `extra_ticks` 1 and 2), `test_extra_ticks_add_to_a_base_that_already_has_some`, `test_fill_model_applicability_comes_from_the_validator` (a config the validator refuses for `eod_close` → reason is the validator's message), `test_unapplicable_fill_model_is_listed_with_a_reason`, `test_fragile_when_costs_x1_5_turns_it_negative`, `test_fragile_when_one_extra_tick_turns_it_negative`, `test_robust_when_it_survives_both`, `test_already_losing_is_not_fragile`, `test_failed_variant_is_a_row_with_its_error`, `test_sensitivity_runs_do_not_change_n`, `test_variant_names_are_safe_segments_at_most_64`, `test_source_unlock_is_not_inherited_and_the_job_reason_logs_one_row`, `test_result_is_written_atomically_and_finite_json`, `test_unknown_or_running_run_refused`, `test_holdout_locked_source_refuses_without_unlock`, and an end-to-end `test_suite_on_a_real_tiny_run_with_the_oracle`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.** Skill `sharp-edges` (new CLI flags).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit.**

```bash
git add stockify/research/sensitivity.py stockify/__main__.py docs/plan/M6_reporting_research.md tests/stockify/research/test_sensitivity.py
git commit -m "FW 6.3: sensitivity suite: costs, extra slippage ticks, latency and fill models, with a fragile flag"
```

---

### Task 16: Sensitivity API and wording (FW 6.4) — M6.7

**Files:**
- Modify: `stockify/research/plain.py` (add `fragile_sentence`), `server/routers/tests.py` (`GET /api/tests/{id}/sensitivity`), `server/schemas.py`, `web/src/lib/types.ts`, `docs/plan/A2_interfaces.md`
- Test: `tests/stockify/research/test_plain.py` (extend), `tests/stockify/test_api_research.py` (extend)

**Interfaces:**
- Produces: `plain.fragile_sentence(result: SensitivityResult) -> str` ("breaks if costs rise by half: net P&L turns from +₹41,200 to −₹3,100"; "survives 1.5 times the costs and one extra tick"; "already losing money, so there is nothing to break"). DTOs `SensitivityRowDTO`, `SensitivityDTO(run_id, job_id, rows, baseline_net_paise, fragile, verdict, reasons, plain)`; `GET /api/tests/{id}/sensitivity` returns the stored `sensitivity.json` or 404 "not run yet; run `python -m stockify sensitivity --run <id>`" (the run id is checked by `paths.run_path`).

- [ ] **Step 1: Write the failing tests:** `test_fragile_sentence_for_each_verdict`, `test_fragile_sentence_names_the_axis_and_both_rupee_figures`, `test_api_returns_404_until_run`, `test_api_returns_the_stored_result_without_nan`, `test_api_refuses_an_unsafe_run_id`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement; mirror the DTOs; blockwatch clean.** Skill `design:ux-copy` on the sentence (report only).
- [ ] **Step 4: Run** `tests\run_tests.py plain`, `api_research` → PASS.
- [ ] **Step 5: Commit.**

```bash
git add stockify/research/plain.py server/routers/tests.py server/schemas.py web/src/lib/types.ts docs/plan/A2_interfaces.md docs/plan/M6_reporting_research.md tests/stockify/research/test_plain.py tests/stockify/test_api_research.py
git commit -m "FW 6.4: the sensitivity result over the API, in plain words"
```

---

### Task 17: `sweep.yaml`: load, expand, example (WF 6.2) — M6.10

**Files:**
- Create: `stockify/research/sweep.py`, `examples/sweeps/orb_grid.yaml`
- Modify: `docs/plan/A1_schemas.md` (new §6.5 `sweep.yaml`)
- Test: `tests/stockify/research/test_sweep_spec.py`

**Interfaces:**
- Produces `sweep.yaml` (schema `stockify.sweep/1`; unknown keys refused naming the key and the closest known one, as D-76; **there is no `family_id` key: a sweep is its own family, Task 18**):

```yaml
schema: stockify.sweep/1
name: orb_grid                             # a safe segment of at most 40 characters
base: ../runs/orb_reliance_minute.json     # a path (relative to this file) or an inline run config
objective: sharpe                          # one of registry.OBJECTIVES
search: {kind: grid}                       # or {kind: random, trials: 12, seed: 7}
workers: null                              # capped at cpu_count - 1
axes:                                      # dotted paths into the run config; decimals are strings
  strategy.params.range_minutes: [5, 10, 15, 20, 30]
  strategy.params.exit_hhmm: ["14:30", "15:00", "15:10"]
```

- `SweepSpec` frozen dataclass `(name, base: dict, axes: dict[str, list], search: dict, objective: str, workers: int | None, source: Path | None)`; `load_spec(path: Path) -> SweepSpec` (raises `SweepSpecError(ValueError)`); `set_path(cfg: dict, dotted: str, value) -> dict` (a deep copy; a path that does not exist in `base` is refused unless it is under `strategy.params`, so `strategy.parms.x` is caught); `GridPoint` frozen dataclass `(index: int, params: dict, config: dict)` (`config.family_id` is left to Task 18); `expand(spec: SweepSpec) -> list[GridPoint]` (grid: Cartesian product, axes in file order, last axis fastest; random: `trials` distinct grid points drawn without replacement with a partial Fisher–Yates on `seeding.stream(seed, "random_search")`, returned in grid order; `trials ≥ grid size` gives the full grid). Point names are `<name>-t<index:03d>` (Task 20 adds the window part).

- [ ] **Step 1: Write the failing tests:** `test_grid_is_cartesian_in_axis_order`, `test_random_search_is_seeded_and_without_replacement`, `test_random_trials_at_or_above_grid_size_give_the_full_grid`, `test_unknown_key_in_sweep_yaml_refused_naming_it`, `test_family_id_key_is_refused_with_a_hint` (the message says a sweep is its own family), `test_name_longer_than_40_refused`, `test_set_path_typo_refused`, `test_every_expanded_config_passes_runconfig_validate`, `test_point_names_are_safe_unique_segments`, `test_base_path_is_relative_to_the_yaml_file`, `test_example_orb_grid_yaml_loads_and_expands_to_15` (5 × 3).
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement; create `examples/sweeps/orb_grid.yaml`** with the example above (`base` pointing at `examples/runs/orb_reliance_minute.json`, whose `strategy.params.qty` stays as it is). Skills: `sharp-edges` (a new file format), `insecure-defaults`.
- [ ] **Step 4: Run** `tests\run_tests.py sweep_spec` → PASS.
- [ ] **Step 5: Commit.**

```bash
git add stockify/research/sweep.py examples/sweeps/orb_grid.yaml docs/plan/A1_schemas.md docs/plan/M6_reporting_research.md tests/stockify/research/test_sweep_spec.py
git commit -m "WF 6.1: sweep.yaml grids and seeded random search, expanded into validated run configs"
```

---

### Task 18: Run a sweep: job, family, results, CLI (WF 6.2) — M6.10

**Files:**
- Modify: `stockify/research/sweep.py`, `stockify/__main__.py` (`sweep` leaves `_STUBS`)
- Test: `tests/stockify/research/test_sweep_run.py`

**Interfaces:**
- Consumes: `expand`, `pool.run_all`, `registry` (`job_family_id`, `ensure_family`, `objective_value`), `holdout`, `fileio`, `store.create_research_job`.
- Produces: `SweepResult` `(job_id, name, family_id, objective, axes: dict[str, list], rows: list[dict], best_run_id: str | None)` where a row is `{index, params, run_id, status, objective_value, sharpe, net_pnl_paise, trades, max_drawdown, error}`; `run_sweep(spec: SweepSpec, *, unlock_reason: str | None = None, workers: int | None = None, pool_run=pool.run_all, now_ns: int | None = None) -> SweepResult`: creates a `research_jobs` row (kind `sweep`, job id `<name>-<UTC stamp>-<6 hex>` made unique; the stamp lives only in the id and `created_ns`), **its own family `registry.job_family_id("sweep", job_id)` with the sweep's objective** (the base config's `family_id` is overwritten), copies `unlock_reason` into every trial config as `unlock_holdout`, runs every grid point through the pool with role `trial` and the job id, writes `research/<job_id>/sweep.json` and `results.csv` (floats written with `repr`, none `NaN`) with `fileio.atomic_write_text`, picks the best by `objective` (a trial whose objective is `None` is ineligible; ties → lowest grid index), stores spec and result in the job row (`spec_json`, `state_json`) and marks it `done` (`failed` with the message when the pool raises).
- CLI `python -m stockify sweep --config FILE [--workers N] [--unlock-holdout R]` prints the results table (best first) in plain headings and the job id.

- [ ] **Step 1: Write the failing tests** (stub pool; runs from `fixtures.make_run`): `test_failed_trial_is_listed_not_fatal`, `test_ineligible_best_and_tie_break_by_grid_index`, `test_sweep_is_its_own_family_and_the_base_family_is_ignored` (`n_trials` of that family equals the grid size; a manual run in the base's old family is not in it), `test_family_objective_is_the_sweeps`, `test_sweep_job_row_and_files_are_written`, `test_sweep_json_has_no_nan`, `test_holdout_locked_base_refuses_without_unlock_and_marks_the_job_failed`, `test_unlock_reason_reaches_every_trial_and_logs_one_row`, `test_workers_never_exceed_cpu_minus_one`, `test_cli_prints_the_table_and_job_id`, and `test_sweep_end_to_end_with_the_oracle_on_the_fixture` (2 × 2 grid).
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.** Skill `sharp-edges` (new CLI flags).
- [ ] **Step 4: Run** `tests\run_tests.py sweep` → PASS.
- [ ] **Step 5: Commit.**

```bash
git add stockify/research/sweep.py stockify/__main__.py docs/plan/M6_reporting_research.md tests/stockify/research/test_sweep_run.py
git commit -m "WF 6.2: run a sweep in parallel as its own family, with a results table and files"
```

---

### Task 19: Sweep API and heatmap data (WF 6.3) — M6.10

**Files:**
- Modify: `server/routers/research.py`, `server/schemas.py`, `web/src/lib/types.ts`, `docs/plan/A2_interfaces.md`
- Test: `tests/stockify/test_api_research.py` (extend)

**Interfaces:**
- Produces (read only): `GET /api/research/jobs?kind=&offset=&limit=` (`ResearchJobDTO(job_id, kind, name, family_id, status, created_ns, finished_ns, error)`) and `GET /api/research/sweeps/{job_id}?x=&y=` returning `SweepDTO(job_id, name, family_id, objective, axes, rows, best_run_id, cells, plain)`; `cells` is, for the two axes the query chooses (default the first two), the objective per cell, the **best** over any remaining axes (the sentence says so); a one-axis sweep returns a single row of cells; an unknown axis is 422 naming it. A job of another kind is 404 on this route.

- [ ] **Step 1: Write the failing tests:** `test_jobs_list_filters_by_kind`, `test_api_sweep_cells_for_two_axes_take_the_best_over_the_rest`, `test_one_axis_sweep_has_one_row_of_cells`, `test_unknown_axis_422_names_it`, `test_sweep_404_for_other_kinds_and_unknown`, `test_sweep_response_has_no_nan`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement; mirror the DTOs; blockwatch clean.**
- [ ] **Step 4: Run** `tests\run_tests.py api_research` → PASS.
- [ ] **Step 5: Commit.**

```bash
git add server/routers/research.py server/schemas.py web/src/lib/types.ts docs/plan/A2_interfaces.md docs/plan/M6_reporting_research.md tests/stockify/test_api_research.py
git commit -m "WF 6.3: sweep results and heatmap cells over the API"
```

---

### Task 20: Walk-forward: windows, training grids, selection, test runs, persistence (WF 6.4) — M6.5

**Files:**
- Create: `stockify/research/walkforward.py`
- Test: `tests/stockify/research/test_walkforward.py`

**Interfaces:**
- Consumes: `sweep.expand`, `sweep.SweepSpec`, `pool.run_all`, `registry`, `holdout` (through the runs), `store` research-job functions.
- Produces:
  - `parse_span(text: str) -> int` (`"120d"` → 120; anything but `<digits>d` raises `ValueError` saying calendar days).
  - `Window` frozen dataclass `(index: int, train_from: date, train_to: date, test_from: date, test_to: date)`; `windows(start: date, end: date, train: int, test: int, step: int) -> list[Window]`: window k starts at `start + k·step`; `train_to = train_from + train − 1 days`; `test_from = train_to + 1 day`; `test_to = test_from + test − 1 days`; kept only when `test_to ≤ end`; `step < test` raises `ValueError` ("out-of-sample pieces would overlap"). Dates are inclusive, as pack specs (confirmed in Task 1).
  - `window_config(cfg: dict, from_: date, to: date) -> dict`: replaces `from`/`to` in every pack spec; packs named only by hash raise `WalkforwardError` ("walk-forward needs pack specs").
  - `select_best(rows: list[dict], objective: str) -> dict | None`: highest `objective_value` among `status == "done"` rows with a defined value; ties → lowest `index`; `None` when none is eligible.
  - `WindowResult` `(window: Window, train_run_ids: list[str], chosen: dict | None, test_run_id: str | None, test_status: str, error: str | None)`; `WalkforwardResult` `(job_id, name, family_id, objective, train, test, step, span: tuple[str, str], windows: list[WindowResult])`.
  - `run_windows(spec: SweepSpec, *, train: int, test: int, step: int, objective: str, start: date | None = None, end: date | None = None, unlock_reason: str | None = None, workers: int | None = None, pool_run=pool.run_all, now_ns: int | None = None) -> WalkforwardResult`: span defaults to the earliest `from` and latest `to` of the base config; creates a `research_jobs` row (kind `walkforward`) with `spec_json` (the parameters) and **its own family `walkforward/<job_id>`** (objective as given); `unlock_reason` goes into every window config as `unlock_holdout` (one unlock row for the job); **phase 1** runs all `windows × grid` training runs in one pool call (role `trial`, the job id; names `<name>-w<window:02d>-t<index:03d>`); **phase 2** runs each window's chosen parameters on its test range (role `oos`, same family and job; name `<name>-w<window:02d>-oos`) in one pool call. **After each phase the job's `state_json` is updated** with `{"windows": [{index, train_from, train_to, test_from, test_to, train_run_ids, chosen_params, chosen_run_id, test_run_id, test_status, error}…]}`, so Task 21's `finish` needs only the database. A window with no eligible training run has `chosen = None` and no test run (listed, not fatal). A pool or holdout failure marks the job `failed` with the message.

- [ ] **Step 1: Write the failing tests:** `test_windows_for_a_known_span` (start 2026-01-01, end 2026-06-30, 120/30/30 → list the exact dates of the first and the last kept window; the next is dropped), `test_step_smaller_than_test_refused`, `test_partial_last_window_is_dropped`, `test_zero_windows_is_a_clear_error`, `test_parse_span` (`"120d"`; refuses `"4m"`, `"120"`, `"-3d"`), `test_window_config_replaces_every_pack_range`, `test_hash_only_packs_refused`, `test_select_best_ties_by_grid_index_and_skips_none`, `test_one_pool_call_for_all_training_runs_then_one_for_tests` (stub pool counts calls and checks roles and names), `test_walkforward_is_its_own_family_and_n_is_windows_times_grid`, `test_test_runs_have_role_oos_and_do_not_change_n`, `test_window_without_an_eligible_candidate_is_listed`, `test_state_json_holds_windows_chosen_params_and_run_ids_after_each_phase`, `test_walkforward_test_window_crossing_holdout_is_locked` (the job is marked `failed` with the message, not left `running`; one unlock row when a reason is given), `test_job_row_is_updated_when_the_pool_raises`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: D-row** (mismatch rows 10, 11: calendar days, inclusive dates, `step ≥ test`, two-phase pooling, job family and `N`); **Commit.**

```bash
git add stockify/research/walkforward.py docs/plan/00_overview.md docs/plan/M6_reporting_research.md tests/stockify/research/test_walkforward.py
git commit -m "WF 6.4: walk-forward windows, parallel training grids, best-by-objective selection, out-of-sample runs and a persisted state"
```

---

### Task 21: Walk-forward: stitching, in-sample vs out-of-sample, `finish` (WF 6.5) — M6.5

**Files:**
- Modify: `stockify/research/walkforward.py`
- Test: `tests/stockify/research/test_walkforward_finish.py`

**Interfaces:**
- Produces:
  - `stitch(pieces: list[tuple[list[str], list[float]]]) -> tuple[list[str], list[float]]`: the test windows' `(dates, plain returns)` in window order, compounded into one index starting at 1.0 (`index_d = index_{d−1} · (1 + r_d)`); capital is reinvested, no state carries between windows, gaps between windows are not filled (I-7).
  - `WalkforwardSummary` `(windows: int, oos_days: int, is_sharpe: float | None, oos_sharpe: float | None, ratio: float | None, chosen_modal_share: float | None, n_trials: int, var_sharpe: float, dsr: DsrResult | None)`: all Sharpe values are annualised from `stats` excess returns (undefined stays `None`); `is_sharpe` = the mean over windows of the chosen training run's Sharpe; `oos_sharpe` = the stitched series' Sharpe; `ratio = oos_sharpe / is_sharpe` (`None` when `is_sharpe` is undefined or ≤ 0); `chosen_modal_share` = the share of windows that chose the most common parameter set; `n_trials` = the job family's trial count (every training run), `var_sharpe` their variance, `dsr` = `stats.deflated_sharpe` of the stitched excess returns against them.
  - `finish(job_id: str) -> WalkforwardFinished` `(summary, windows: list[dict], stitched_dates: list[str], stitched_index: list[float])`: **reads only the database** (`research_jobs.state_json`, `run_equity`, `runs`), so it works in a fresh process with the run folders gone; writes `research/<job_id>/walkforward.json` (windows with chosen parameters and both periods' key metrics, the stitched `dates` and `index`, the summary) with `fileio.atomic_write_text`, and the equity PNG (matplotlib Agg) beside it; marks the job `done`. It writes **no `report.md`**: Task 22's `wf_report.render` is its only owner.

- [ ] **Step 1: Write the failing tests:** `test_stitch_compounds_and_starts_at_one`, `test_stitch_keeps_window_order_and_gaps`, `test_summary_on_a_constructed_job` (fixture runs with known daily returns: hand-computed IS Sharpe, OOS Sharpe and ratio), `test_ratio_is_none_when_in_sample_is_not_positive_or_undefined`, `test_modal_share_hand_checked` (parameter sets A, A, B → 2/3), `test_dsr_uses_every_training_run_as_a_trial` (`n_trials == windows × grid`, `V` equals the variance of those Sharpes), `test_finish_works_from_the_database_alone` (delete the run folders and open a fresh store connection first), `test_flat_oos_series_has_no_sharpe_and_no_nan`, `test_walkforward_json_has_no_nan`, `test_window_with_a_failed_test_run_is_listed_and_left_out_of_the_stitch`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.** Skills: `statistical-analysis` and `statistical-power` as method checks on the summary (report only).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit.**

```bash
git add stockify/research/walkforward.py docs/plan/M6_reporting_research.md tests/stockify/research/test_walkforward_finish.py
git commit -m "WF 6.5: stitched out-of-sample curve, in-sample vs out-of-sample summary and deflated Sharpe over every training run"
```

---

### Task 22: Walk-forward report, CLI and API (WF 6.6) — M6.5

**Files:**
- Create: `stockify/research/wf_report.py`
- Modify: `stockify/research/plain.py` (add `wf_sentence`), `stockify/__main__.py` (`walkforward` leaves `_STUBS`), `server/routers/research.py`, `server/schemas.py`, `web/src/lib/types.ts`, `docs/plan/A2_interfaces.md`
- Test: `tests/stockify/research/test_wf_report.py`, `tests/stockify/research/test_plain.py` (extend), `tests/stockify/test_api_research.py` (extend)

**Interfaces:**
- Produces:
  - `plain.wf_sentence(summary: WalkforwardSummary) -> str` ("kept 38% of the in-sample Sharpe out of sample"; "in-sample was not positive, so there is no ratio"; stability: "the same settings won 4 of 7 windows").
  - **`wf_report.render(job_id: str, *, sensitivity_run: str | None = None) -> Path`: the only writer of `research/<job_id>/report.md`.** It contains the in-sample vs out-of-sample table and the ratio sentence, the deflated Sharpe sentence with `N`, the per-window parameters and the stability line, the equity PNG, the cost-sensitivity table of `sensitivity_run` (read from its `sensitivity.json`) or the line "not run yet: `python -m stockify sensitivity --run <id>`", the family's holdout unlock count, and the PBO sentence when one exists (Task 23 supplies it; until then the line is absent).
  - CLI `python -m stockify walkforward --sweep FILE --train 120d --test 30d --step 30d --objective sharpe [--from D --to D] [--workers N] [--unlock-holdout R] [--sensitivity-run RUN_ID]` (defaults from `research.json` `walk_forward`): runs `run_windows`, `finish`, then `wf_report.render`; prints the summary and the folder.
  - API: `GET /api/research/walkforward/{job_id}` → `WalkforwardDTO(job_id, name, family_id, objective, train, test, step, summary, windows, stitched_dates, stitched_index, plain)`; the job list is `GET /api/research/jobs?kind=walkforward`.

- [ ] **Step 1: Write the failing tests:** `test_wf_sentence_ratio_none_and_stability`, `test_report_md_has_the_two_sharpes_the_ratio_and_the_dsr_sentence`, `test_report_without_sensitivity_says_how_to_run_it`, `test_report_with_sensitivity_has_the_table`, `test_report_is_plain_words_only` (no Greek letters, no raw status codes), `test_render_is_the_only_writer_of_report_md` (`finish` leaves no `report.md`), `test_cli_walkforward_end_to_end_on_the_fixture` (a tiny oracle-driven fixture with a 2 × 2 grid and 3 windows), `test_api_walkforward_detail_and_404`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement; mirror DTOs; blockwatch clean.** Skills: `sharp-edges` (CLI), `design:ux-copy` (report wording, report only).
- [ ] **Step 4: Run** `tests\run_tests.py wf_report`, `walkforward`, `api_research` → PASS.
- [ ] **Step 5: Commit.**

```bash
git add stockify/research/wf_report.py stockify/research/plain.py stockify/__main__.py server/routers/research.py server/schemas.py web/src/lib/types.ts docs/plan/A2_interfaces.md docs/plan/M6_reporting_research.md tests/stockify/research/test_wf_report.py tests/stockify/research/test_plain.py tests/stockify/test_api_research.py
git commit -m "WF 6.6: the walk-forward report, command and API, with one writer for report.md"
```

---

### Task 23: Probability of backtest overfitting, CSCV (LF 6.1) — M6.11 (stretch)

**Files:**
- Create: `stockify/research/pbo.py`
- Modify: `stockify/research/report.py` (`FamilyReport.pbo`, `pbo_note`), `stockify/research/plain.py` (add `pbo_sentence`), `stockify/research/wf_report.py` (the PBO line), `server/schemas.py`, `web/src/lib/types.ts`
- Test: `tests/stockify/research/test_pbo.py`

**Interfaces:**
- Consumes: `registry.trials`, `stats.excess_returns`, `stats.sharpe_per_period`, `seeding.stream` (only for the tests' noise), numpy.
- Produces:
  - `TrialMatrix` frozen dataclass `(run_ids: list[str], dates: list[str], x: np.ndarray, dropped: list[str], flat: list[str])` with `x` of shape `T × N` holding excess daily returns over the **dates common to every kept trial** (intersection, sorted). `dropped` lists trials with no date in common with the rest; `flat` lists trials whose series is flat over those dates (Sharpe undefined) and are excluded from `x`. `trial_matrix(family_id: str) -> TrialMatrix`.
  - `PboResult` frozen dataclass `(pbo: float, n_trials: int, n_days: int, slices: int, combinations: int, rows_dropped: int, median_logit: float, prob_loss: float)`; `cscv(x: np.ndarray, slices: int = 16) -> PboResult`: split the `T` rows into `S` equal consecutive blocks (the remainder rows at the end are dropped and **counted in `rows_dropped`**); for each of the `C(S, S/2)` combinations (lexicographic from `itertools.combinations`) the in-sample set is the chosen blocks and the out-of-sample set is the rest; performance is the **per-period Sharpe with ddof 1** computed from per-block `(n, Σx, Σx²)` (vectorised); a column that is flat on a union has performance 0.0 on it (so the matrix never produces `NaN`); `n*` = the in-sample best (ties → lowest column); `ω = midrank of n* among the N out-of-sample values / (N + 1)`; `λ = ln(ω / (1 − ω))`; `PBO = share of combinations with λ ≤ 0`; `prob_loss` = share where the out-of-sample Sharpe of `n*` is below 0. No randomness. `ValueError` for odd or `< 4` slices, `N < min_trials` (`research.settings()["pbo"]`), or `T < 2·slices`.
  - `FamilyReport.pbo: PboResult | None` plus `pbo_note: str | None` (why it is absent, in plain words: "the windows cover different days, so the tries cannot be compared day by day" for a walk-forward family, "fewer than 2 tries", "too few common days"); `plain.pbo_sentence`; DTO `PboDTO`.

- [ ] **Step 1: Write the failing tests:** `test_combination_count_is_c_s_s_over_2` (12,870 for 16), `test_dominating_trial_gives_pbo_zero` (trial 0 beats the others by a fixed margin in every block), `test_pure_noise_pbo_is_about_one_half` (seeded noise, 320 days × 20 trials, `0.35 ≤ pbo ≤ 0.65`), `test_matches_an_independent_brute_force_reference` (a slow pure-Python loop inside the test, `S = 4`, `N = 3`, same `pbo`, `median_logit`, `prob_loss` to 1e-12), `test_permuting_trials_does_not_change_pbo` (continuous noise, no ties), `test_result_is_deterministic`, `test_remainder_rows_are_reported` (`T = 330`, `S = 16` → `rows_dropped == 10`), `test_odd_slices_short_series_and_single_trial_refused`, `test_trial_matrix_uses_common_dates_only_and_lists_dropped_and_flat`, `test_a_sweep_family_has_a_pbo_and_a_walkforward_family_has_a_note`, `test_pbo_sentence_plain`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** (index arrays of shape `combinations × S/2` and numpy gathers, so 12,870 × 100 trials stays well under a second).
- [ ] **Step 4: Run** → PASS; time the 100-trial case into RECON.
- [ ] **Step 5: Commit.**

```bash
git add stockify/research/pbo.py stockify/research/report.py stockify/research/plain.py stockify/research/wf_report.py server/schemas.py web/src/lib/types.ts docs/plan/M6_reporting_research.md tests/stockify/research/test_pbo.py
git commit -m "LF 6.1: probability of backtest overfitting by combinatorially symmetric cross-validation"
```

---

### Task 24: `research report` command and the demo pipeline test (WF 6.7) — M6 demo

**Files:**
- Modify: `stockify/__main__.py` (`research report`)
- Test: `tests/stockify/research/test_demo_pipeline.py`

**Interfaces:**
- Consumes: `wf_report.render`, `sweep.run_sweep`, `walkforward`, `sensitivity.run_suite`.
- Produces: CLI `python -m stockify research report --walkforward JOB_ID [--sensitivity-run RUN_ID]` re-renders `research/<job_id>/report.md` through `wf_report.render` (the only writer) and prints its path. The end-to-end test `test_demo_pipeline_on_the_fixture` runs sweep → walk-forward → sensitivity → report on the tiny oracle fixture, with the lab sandboxed by `tests.support`, and asserts the report contains the words "after allowing for luck", the ratio line and the sensitivity table, and that every path written lies under the sandbox's `state_dir()` (checked by listing `exports_dir()`, `research_dir()` and the runs folder and comparing with `stockify_paths.state_dir()`).

- [ ] **Step 1: Write the failing tests:** `test_research_report_command_rerenders_with_the_sensitivity_table`, `test_research_report_unknown_job_exits_2`, `test_demo_pipeline_on_the_fixture`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement the command.**
- [ ] **Step 4: Run** `tests\run_tests.py demo_pipeline` → PASS.
- [ ] **Step 5: Commit.**

```bash
git add stockify/__main__.py docs/plan/M6_reporting_research.md tests/stockify/research/test_demo_pipeline.py
git commit -m "WF 6.7: the research report command and the whole demo pipeline run on the fixture"
```

---

### Task 25: Compare runs: logic, CLI, API (DMF 6.1) — M6.1

**Files:**
- Create: `stockify/reporting/__init__.py`, `stockify/reporting/compare.py`
- Modify: `stockify/__main__.py` (`compare` leaves `_STUBS`; `compare-artifacts` stays), `server/routers/tests.py` (the stub body), `server/schemas.py`, `web/src/lib/types.ts`, `docs/plan/A2_interfaces.md`
- Test: `tests/stockify/reporting/__init__.py`, `tests/stockify/reporting/test_compare.py`, `tests/stockify/test_api_tests.py` (extend)

**Interfaces:**
- Consumes: `plain.meaning` (keys `net_pnl, return, cagr, sharpe, sortino, max_drawdown, calmar, trades, win_rate, costs, cost_share` exist from Task 7), `oracle.metrics.drawdown_series`, `store`.
- Produces:
  - `MetricRow` `(key: str, label: str, values: list[float | int | None], kind: str, plain: str)` (`kind` ∈ `money, ratio, pct, count`; `plain = plain.meaning(key)`); `DiffRow` `(field: str, values: list[str | None], differs: bool)`.
  - `RunComparison` `(run_ids: list[str], names: list[str], metrics: list[MetricRow], equity: list[dict], drawdown: list[dict], labels: dict[str, list[bool]], differing_labels: list[str], manifest_diff: list[DiffRow], notes: list[str])`; each `equity`/`drawdown` entry is `{run_id, dates, values}`; equity is **normalised to 1.0 at the run's own start** (NAV / capital), drawdown is `drawdown_series` of the NAV rows. `manifest_diff` covers parameters (`strategy.params.*`, one row per differing key), `fill_model`, `margin.model`, the config hashes and bundle/pack hashes, and the engine build id; equal rows are listed with `differs = False`.
  - `compare_runs(run_ids: list[str]) -> RunComparison`: 2–6 distinct ids else `ValueError("compare 2 to 6 runs")`; an unknown run raises `KeyError`; a run that is not `done` is refused naming its status; a missing folder falls back to the database (`bundle_sha256`, `engine_build_id`, `config_json`) with the note "manifest not available (folder removed)".
  - `format_table(c: RunComparison) -> str` (aligned text; a `*` marks the cells that differ); CLI `python -m stockify compare RUN_ID RUN_ID [RUN_ID…]`.
  - **API `GET /api/tests/compare?ids=a&ids=b&ids=c`: repeated `ids` query parameters** (`ids: list[str] = Query(...)`); a comma-separated value is also split (run ids never contain a comma) so a hand-typed `?ids=a,b` works. `ComparisonDTO(run_ids, names, metrics, equity, drawdown, labels, differing_labels, manifest_diff, notes)`; 422 for a bad count, 404 for an unknown id.

- [ ] **Step 1: Write the failing tests:** `test_two_to_six_runs_only`, `test_equity_is_normalised_to_one_at_each_runs_start`, `test_runs_of_different_lengths_keep_their_own_dates`, `test_drawdown_series_hand_checked`, `test_manifest_diff_lists_only_real_differences_as_differs` (two ORB runs differing in `range_minutes`: that row differs, `fill_model` matches), `test_differing_labels_are_highlighted`, `test_missing_folder_falls_back_to_the_database_with_a_note`, `test_not_done_run_refused_naming_the_status`, `test_every_metric_row_has_its_plain_meaning`, `test_cli_prints_the_table` (stdout has both run names and `*` on a differing metric), `test_compare_artifacts_command_still_works`, `test_api_compare_repeated_ids_ok_422_404`, `test_api_compare_accepts_a_comma_list`, `test_api_compare_declared_before_the_run_id_route` (`compare` is never read as a run id).
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement; replace the 501 stub body; mirror DTOs; blockwatch clean.**
- [ ] **Step 4: Run** `tests\run_tests.py compare`, `api_tests` → PASS.
- [ ] **Step 5: Commit.**

```bash
git add stockify/reporting/__init__.py stockify/reporting/compare.py stockify/__main__.py server/routers/tests.py server/schemas.py web/src/lib/types.ts docs/plan/A2_interfaces.md docs/plan/M6_reporting_research.md tests/stockify/reporting/__init__.py tests/stockify/reporting/test_compare.py tests/stockify/test_api_tests.py
git commit -m "DMF 6.1: compare two to six runs side by side, in the terminal and over the API"
```

---

### Task 26: Export a run as a zip (DMF 6.2) — M6.2

**Files:**
- Create: `stockify/reporting/export.py`, `tests/stockify/reporting/test_export.py`
- Modify: `stockify/__main__.py` (`export` leaves `_STUBS`), `server/routers/tests.py` (`GET /api/tests/{run_id}/export`), `tests/stockify/test_secrets.py` (U-10 extension), `docs/plan/A3_testing.md` (U-10 row)

**Interfaces:**
- Produces: `export.ALLOWED` (tuple of file names copied when present: `bundle.json, run.json, manifest.json, metrics.json, determinism.json, orders.csv, order_events.csv, fills.csv, charges.csv, positions_eod.csv, equity.csv, margin_daily.csv, report.md, equity.png`; **not** `events_log.txt` (free text from strategies), `request.json`, `runner.pid`, `research.json`, `sensitivity.json` or anything else); `export.build(run_id: str, *, with_packs: bool = False, out: Path | None = None) -> Path` writes the zip (default `paths.export_path(run_id, with_packs)`, through `fileio.atomic_write_bytes`-style temp-then-replace using `fileio.atomic_write_text`'s pattern for bytes: add `fileio.atomic_write_bytes(path, data)` in this task) holding the allowed files plus a generated `summary.json` (`{"run_id", "config", "metrics", "labels", "family_id", "role", "packs": [{"sha256", "bytes", "included"}]}`, canonical JSON from `numerics.dumps_canonical`; no paths, no host name). With `with_packs` the pack files named in the bundle go in under `packs/<sha>.evt`. The zip is **byte-identical for the same run** (members sorted, fixed timestamp 1980-01-01, constant compression level). A run that is not `done`, or whose folder is gone, raises `ExportError` (CLI exit 1, API 409 / 404). The run id is checked by `paths.run_path`.
- CLI `python -m stockify export RUN_ID [--with-packs] [--out FILE]` prints the path. API `GET /api/tests/{id}/export?with_packs=false` streams the zip (`application/zip`, `Content-Disposition` with the run id only); `with_packs=true` is allowed (loopback only).

- [ ] **Step 1: Write the failing tests:** `test_zip_holds_the_allowed_files_and_summary`, `test_packs_excluded_by_default_with_hashes_listed`, `test_with_packs_includes_them`, `test_zip_is_byte_identical_for_the_same_run`, `test_events_log_and_request_json_are_never_exported`, `test_a_planted_unexpected_file_in_the_folder_is_not_exported`, `test_running_or_failed_run_refused`, `test_unknown_or_pruned_run_refused_not_a_traceback`, `test_path_traversal_ids_refused`, `test_api_download_and_404_409`, and the **secrets test** `test_export_contains_no_secret` (U-10): inside `isolated_env({"KITE_API_SECRET": "SEKRET-<random>", "KITE_API_KEY": "KEY-<random>"})` with a fake token file in the sandbox's `state_dir()` and the fake values also planted in a sibling file of the run folder and in `events_log.txt`, build an export (with and without packs) and scan every member's bytes (and the member names) for those literal values and for the case-insensitive patterns `api[_-]?secret`, `access[_-]?token`, `enctoken`, `request[_-]?token`, `Bearer\s+\S+` (a 64-hex run or pack hash is not a match by itself).
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.** Skills: `differential-review` and `insecure-defaults` over the diff (export is a data-leaving feature).
- [ ] **Step 4: Run** `tests\run_tests.py export`, `secrets`, `api_tests`, `fileio` → PASS.
- [ ] **Step 5: Commit.**

```bash
git add stockify/reporting/export.py stockify/research/fileio.py stockify/__main__.py server/routers/tests.py docs/plan/A3_testing.md docs/plan/M6_reporting_research.md tests/stockify/reporting/test_export.py tests/stockify/test_secrets.py tests/stockify/research/test_fileio.py
git commit -m "DMF 6.2: export a finished run as a reproducible zip that can never hold a secret"
```

---

### Task 27: Recorder coverage metrics in the quality report (LCM 6.1) — M6.3

**Files:**
- Modify: `stockify/data/quality.py`
- Test: `tests/stockify/data/test_quality_coverage.py` (new, beside the existing quality tests; add `__init__.py` only if that package lacks one)

**Interfaces:**
- Produces: two more day-level rows (`instrument_id = 0`) when a report is made: `coverage_full_pct` and `coverage_quote_pct`, the mean of the instruments' `coverage_pct` over the instruments that were subscribed in FULL (a `subscription_log` row with mode `full`, read through the day object's `subscriptions()`) and over the rest; `None` when a group is empty. `FOREIGN_METRICS` handling is unchanged; no schema change, no migration, existing rows untouched; only packets are read, as today.

- [ ] **Step 1: Write the failing tests:** `test_quality_report_writes_the_two_day_metrics` (a small recorded fixture with one FULL and one QUOTE instrument and known coverage), `test_empty_group_gives_none`, `test_quality_rerun_keeps_the_foreign_metrics`, `test_existing_metrics_are_unchanged` (the old rows equal their pre-change values on the same fixture), `test_report_reads_packets_only_and_writes_only_data_quality_rows`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.** Skill `differential-review` on this diff (it touches the recorded-data path).
- [ ] **Step 4: Run** `tests\run_tests.py quality` → PASS.
- [ ] **Step 5: Record** the 🛑 owner step in RECON ("re-run `python -m stockify data report --date D` for each recorded day; derived rows only") and **Commit.**

```bash
git add stockify/data/quality.py docs/plan/RECON.md docs/plan/M6_reporting_research.md tests/stockify/data/test_quality_coverage.py
git commit -m "LCM 6.1: the quality report adds FULL and QUOTE recorder coverage for each day"
```

---

### Task 28: Data coverage matrix, gaps, storage, API (LCM 6.2) — M6.3

**Files:**
- Create: `stockify/reporting/coverage.py`, `tests/stockify/reporting/test_coverage.py`
- Modify: `server/routers/data.py`, `server/schemas.py`, `web/src/lib/types.ts`, `docs/plan/A2_interfaces.md`

**Interfaces:**
- Consumes: `refdata` (`tick_days`, `nse_fetch_log`, `data_quality`), `bars.sqlite` (`bar_fetch_log`), `calendar.trading_days`, `store.latest_recorder_status`, `compact.disk_warnings`, `ticks`, `research.settings()["coverage"]` (`ok_pct`, `default_days`, `max_days`).
- Produces:
  - `coverage.Cell` frozen dataclass `(state: str, value: float | None, detail: str | None)` with `state` ∈ `ok, partial, missing, not_reported, closed, not_applicable`; `coverage.matrix(from_: date, to: date) -> CoverageMatrix` with `datasets` (`recorder_full`, `recorder_quote`, `archive`, one per `nse_fetch_log` dataset family: F&O, equities, index closes, named from `sources.json` with plain labels), `days` (every calendar date; weekends and holidays are `closed`, never zeros), `bars` (per instrument and interval: cached range and day count, newest first, limited), `storage` (`landing_bytes, archive_bytes, refdata_bytes, lab_bytes, free_bytes, total_bytes, warnings` from `disk_warnings`), `recorder` (the latest recorder status or `None`). Recorder cells: `ok` when the value ≥ **`coverage.ok_pct` from `research.json`**, `partial` below, `not_reported` when no quality rows with those metrics exist, `missing` when no `tick_days` row exists for a trading day. Archive cell: `landing`/`archived`/`verified` from `tick_days.status` (`landing_deleted` shown as verified). Reads only; no heavy file reads.
  - `coverage.gaps(day: date) -> list[dict]` (`start_ns, end_ns, start_ist, duration_s, reason, scope, frames`) from the day's gap table via `ticks.open_day` (404 when no recording).
  - API: `GET /api/data/coverage?from=&to=` (defaults: the last `default_days` calendar days; a range over `max_days` is 422), `GET /api/data/gaps?date=`; DTOs `CoverageCellDTO`, `CoverageDayDTO`, `BarRangeDTO`, `StorageDTO`, `CoverageMatrixDTO`, `GapDTO`. Quality metrics for the drill-down stay on the existing `/api/data/quality`.

- [ ] **Step 1: Write the failing tests** (`SandboxedDBTest` with refdata and bars): `test_empty_lab_matrix_has_no_numbers` (all cells `missing`/`closed`, no `value`), `test_unreported_day_is_not_a_zero` (a `tick_days` row but no quality rows → `not_reported`), `test_weekends_and_holidays_are_closed`, `test_landing_only_day_shows_landing_and_a_partial_state`, `test_verified_archive_day`, `test_recorder_cells_use_the_two_day_metrics_and_the_configured_threshold` (fixture quality rows 99.5 and 97.2 with `ok_pct` 99; then with `ok_pct` 98 the second is still `partial` and 97.2 → `partial`, and a patched `ok_pct` of 97 makes it `ok`), `test_nse_dataset_cells` (`ok`, `missing`, `error` from `outcome`), `test_bar_ranges_per_instrument`, `test_storage_numbers_and_warnings`, `test_gaps_for_a_recorded_day_and_404_without`, `test_range_limits_come_from_the_config_and_bad_dates_422`, `test_matrix_is_finite_json`, `test_non_loopback_host_refused`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement; mirror DTOs; blockwatch clean.**
- [ ] **Step 4: Run** `tests\run_tests.py coverage`, `quality`, `api` → PASS.
- [ ] **Step 5: Commit.**

```bash
git add stockify/reporting/coverage.py server/routers/data.py server/schemas.py web/src/lib/types.ts docs/plan/A2_interfaces.md docs/plan/M6_reporting_research.md tests/stockify/reporting/test_coverage.py
git commit -m "LCM 6.2: the data coverage matrix by date and dataset, with gaps, bar ranges and storage"
```

---

### Task 29: Web foundation: charts, fetchers, helpers (RCM 6.1)

**Files:**
- Create: `web/src/components/charts.tsx`, `web/src/lib/research.ts`, `tests/stockify/research_check.ts`, `tests/stockify/test_research_checks.py`
- Modify: `web/src/lib/format.ts` (only if a ₹ or percent helper is missing), `web/src/components/AppNav.tsx` (Research, Data links)

**Interfaces:**
- Consumes: the Task 9 and 16 DTOs in `types.ts`.
- Produces:
  - `charts.tsx` (hand-written SVG, no library; tokens only): `LineChart({series: {label: string; points: [number, number][] }[]; yFormat; baseline?})`, `IntervalBar({label, point, lo, hi, format})`, `HeatGrid({xs, ys, cells, format, best})`, each with an `aria-label` summary and a visible table fallback.
  - `research.ts`: fetchers `getRunResearch(id)`, `getSensitivity(id)` (404 → `null`), `getFamilies()`, … following the `api.ts` conventions; pure helpers `heatColourStep(value, min, max, steps)`, `bucketTone(bucket)`, `sentenceOrDash(s)`.

- [ ] **Step 1: Read** `web/node_modules/next/dist/docs/` for server components and `searchParams`; read `web/design.md`; invoke the **Hallmark** skill (app-screen scope, `design.md` present, so the system is locked) and follow it; name the files to be touched first.
- [ ] **Step 2: Write the failing checks** in `research_check.ts` run by `test_research_checks.py` (same pattern as `test_param_checks.py`, skipped with a clear message when `node` is missing): `heatColourStep` hand-checked, `bucketTone`, `sentenceOrDash(null)`.
- [ ] **Step 3: Implement** the helpers, charts, fetchers and the nav links.
- [ ] **Step 4: Verify.** `cd web; npm run lint; npm run build` pass; `node ~/.claude/skills/hallmark/scripts/sloplint.mjs web/src/components/charts.tsx --scope app` has no FAIL (no `--render`); skills `design:design-critique`, `design:accessibility-review` over the files, findings applied.
- [ ] **Step 5: Commit.**

```bash
git add web/src/components/charts.tsx web/src/lib/research.ts web/src/components/AppNav.tsx tests/stockify/research_check.ts tests/stockify/test_research_checks.py docs/plan/M6_reporting_research.md
git commit -m "RCM 6.1: SVG charts, research fetchers and the nav links"
```

---

### Task 30: The run page's research panel (RCM 6.2) — M6.4, M6.7–M6.9 on screen

**Files:**
- Modify: `web/src/app/tests/[id]/page.tsx`, `web/src/lib/research.ts` (only if a helper is missing)

**Interfaces:**
- Consumes: `GET /api/tests/{id}/research`, `GET /api/tests/{id}/sensitivity` (404 allowed), Task 29 charts.
- Produces: a "How much to trust this result" panel: one line `best of N tries · deflated Sharpe 85%` (from `plain`, which already names the objective and, for a runner-up, the run the job picked) plus its meaning; three `IntervalBar` rows (Sharpe, average day, worst drop) each with its plain sentence; the regime tables (month, weekday, expiry, VIX, gap) with thin buckets marked "few days"; the sensitivity table (or "not run yet" with the command); the holdout-unlock count when above zero. All four states per region; every number sits beside its API `plain` sentence.

- [ ] **Step 1: Read** the Next docs again if needed; invoke **Hallmark** (app-screen); name the files first.
- [ ] **Step 2: Implement** the panel.
- [ ] **Step 3: Verify:** `npm run lint; npm run build`; sloplint (static) on the page; skills `design:design-critique`, `design:accessibility-review`, `design:ux-copy` applied; start the API and web on loopback against a sandbox lab seeded with the Task 4 fixtures and `curl` the run page to confirm the sentences render (the visual check at 1024 px is the owner's, 🛑).
- [ ] **Step 4: Commit.**

```bash
git add "web/src/app/tests/[id]/page.tsx" web/src/lib/research.ts docs/plan/M6_reporting_research.md
git commit -m "RCM 6.2: the run page says how much to trust the result: tries, deflated Sharpe, intervals, regimes, sensitivity"
```

---

### Task 31: `/tests/compare` page and the list selector (RCM 6.3) — M6.1

**Files:**
- Create: `web/src/app/tests/compare/page.tsx`, `web/src/app/tests/CompareSelect.tsx` (a `"use client"` component)
- Modify: `web/src/lib/research.ts` (`getComparison`, `compareHref`), `web/src/app/tests/page.tsx`, `tests/stockify/research_check.ts`

**Interfaces:**
- Consumes: `GET /api/tests/compare?ids=a&ids=b`.
- Produces: `compareHref(ids: string[]) -> string` builds `/tests/compare?ids=a&ids=b…` (repeated `ids`, each encoded, at most six). `CompareSelect` is a client component on the list page: a checkbox per run row inside a GET `<form action="/tests/compare">` (so the browser itself sends repeated `ids`), a live count, and a "Compare (2–6)" submit disabled outside 2–6. The page reads `ids` as `string | string[]` from the Promise `searchParams`; it shows the metrics table (runs as columns, a `plain` line per metric row, differing cells marked by the sign or a word as well as colour), the normalised equity `LineChart` and the drawdown `LineChart`, labels with differences highlighted, and the manifest diff table (differences first, matches collapsed under a "what matched" disclosure). Wrong counts show an `Empty` saying what to do; an unknown id an error `Panel`.

- [ ] **Step 1: Read** the Next docs for client components and forms; invoke **Hallmark** (app-screen); name the files first.
- [ ] **Step 2: Add failing checks** to `research_check.ts`: `compareHref(["a","b"]) === "/tests/compare?ids=a&ids=b"`, seven ids are cut to six, an id needing encoding is encoded.
- [ ] **Step 3: Implement** the helper, the client component, the page and the list changes.
- [ ] **Step 4: Verify:** lint, build, sloplint (static), the three design skills, `curl` of `/tests/compare?ids=…&ids=…` against the sandbox API.
- [ ] **Step 5: Commit.**

```bash
git add web/src/app/tests/compare/page.tsx web/src/app/tests/CompareSelect.tsx web/src/app/tests/page.tsx web/src/lib/research.ts tests/stockify/research_check.ts docs/plan/M6_reporting_research.md
git commit -m "RCM 6.3: compare runs on one page: metrics, equity, drawdown, labels and what differs in the setup"
```

---

### Task 32: `/data` coverage page (RCM 6.4) — M6.3

**Files:**
- Create: `web/src/app/data/page.tsx`, `web/src/lib/data.ts`
- Modify: `tests/stockify/research_check.ts`

**Interfaces:**
- Consumes: `GET /api/data/coverage`, `GET /api/data/gaps`, `GET /api/data/quality`, `GET /api/data/recorder`.
- Produces: the live recorder strip at the top (connected, packets per second, last packet age, in words); the date × dataset calendar grid (rows are dates, newest first; each cell a `Chip` with a word and a figure, never colour alone; `closed` days dimmed; `not reported` says how to get it); a range selector (30, 60, 180 days via `searchParams`); a clicked day opens its drill-down (a `searchParams` day, server-rendered): gaps table (start IST, length, reason in plain words, scope), the day's worst-coverage instruments from `/api/data/quality`, the NSE files for that day; a "Bars" section (instrument, interval, from, to); a "Storage" section (used, free, warnings). Pure helpers `cellWords(cell)` ("98.4% covered", "no recording", "not reported yet", "market closed") and `gapReasonWords(reason)` in `data.ts`.

- [ ] **Step 1: Read** the Next docs; invoke **Hallmark** (app-screen); name the files first.
- [ ] **Step 2: Failing checks** for `cellWords` and `gapReasonWords` (every reason in A1's `gap-reasons` block has a plain word; an unknown reason falls back to a readable "other: x"); implement.
- [ ] **Step 3: Implement** the page and fetchers.
- [ ] **Step 4: Verify:** lint, build, sloplint (static), design skills, `curl /data` and `/data?day=…` against a sandbox API; an empty lab renders the empty state.
- [ ] **Step 5: Commit.**

```bash
git add web/src/app/data/page.tsx web/src/lib/data.ts tests/stockify/research_check.ts docs/plan/M6_reporting_research.md
git commit -m "RCM 6.4: the /data page: recording, archive and NSE-file coverage by day, with gaps and storage"
```

---

### Task 33: `/research` overview page (RCM 6.5) — M6.4

**Files:**
- Create: `web/src/app/research/page.tsx`
- Modify: `web/src/lib/research.ts`, `tests/stockify/research_check.ts`

**Interfaces:**
- Consumes: `GET /api/research/families`, `GET /api/research/jobs`.
- Produces: the families table (name, tries, the best deflated Sharpe in plain words, unlock count, link to the family page) and the sweep and walk-forward job lists below it (name, status, created IST, link). Pure helpers `familyHref(id)` (encodes `/`) and `jobHref(kind, id)`, checked in `research_check.ts`.

- [ ] **Step 1: Read** the Next docs; invoke **Hallmark** (app-screen); name the files first.
- [ ] **Step 2: Failing checks** for `familyHref` and `jobHref`; implement.
- [ ] **Step 3: Implement** the page.
- [ ] **Step 4: Verify:** lint, build, sloplint (static), design skills, `curl /research`; empty and error states.
- [ ] **Step 5: Commit.**

```bash
git add web/src/app/research/page.tsx web/src/lib/research.ts tests/stockify/research_check.ts docs/plan/M6_reporting_research.md
git commit -m "RCM 6.5: the research overview: families and jobs"
```

---

### Task 34: `/research/family` page (RCM 6.6) — M6.4, M6.11

**Files:**
- Create: `web/src/app/research/family/page.tsx`

**Interfaces:**
- Consumes: `GET /api/research/families/{id:path}` (the id from `?id=`).
- Produces: the trial table with the picked run marked and `plain.trials` above it (other roles listed apart, labelled as not tries), the holdout unlock log, the PBO sentence or its note, and sessions listed apart from tries. Money in ₹; every statistic with its sentence.

- [ ] **Step 1: Read** the Next docs; invoke **Hallmark** (app-screen); name the file first.
- [ ] **Step 2: Implement** the page.
- [ ] **Step 3: Verify:** lint, build, sloplint (static), design skills, `curl /research/family?id=…` for a sweep family and a manual one.
- [ ] **Step 4: Commit.**

```bash
git add web/src/app/research/family/page.tsx docs/plan/M6_reporting_research.md
git commit -m "RCM 6.6: a family's tries, unlocks and overfitting estimate on one page"
```

---

### Task 35: `/research/sweeps/[id]` heatmap page (RCM 6.7) — M6.10

**Files:**
- Create: `web/src/app/research/sweeps/[id]/page.tsx`
- Modify: `tests/stockify/research_check.ts`

**Interfaces:**
- Consumes: `GET /api/research/sweeps/{id}?x=&y=`.
- Produces: the two-parameter `HeatGrid` with axis pickers via `searchParams`, the best cell marked and the "best over the other settings" sentence, then the results table. Pure helper `heatCells(rows, xKey, yKey)` (best over the other axes, marks the best cell) checked in `research_check.ts`.

- [ ] **Step 1: Read** the Next docs for dynamic segments; invoke **Hallmark** (app-screen); name the file first.
- [ ] **Step 2: Failing check** for `heatCells` (best over a third axis; the marked cell); implement.
- [ ] **Step 3: Implement** the page.
- [ ] **Step 4: Verify:** lint, build, sloplint (static), design skills, `curl` the route for a one-axis and a two-axis sweep.
- [ ] **Step 5: Commit.**

```bash
git add "web/src/app/research/sweeps/[id]/page.tsx" tests/stockify/research_check.ts docs/plan/M6_reporting_research.md
git commit -m "RCM 6.7: the sweep heatmap and results table"
```

---

### Task 36: `/research/walkforward/[id]` report page (RCM 6.8) — M6.5

**Files:**
- Create: `web/src/app/research/walkforward/[id]/page.tsx`

**Interfaces:**
- Consumes: `GET /api/research/walkforward/{id}`.
- Produces: in-sample vs out-of-sample with the ratio sentence, the stitched curve (`LineChart`), the per-window parameters and the stability line, the deflated Sharpe sentence, a link to the family page; the same content as the CLI's `report.md`. No Greek letters; every statistic with its sentence.

- [ ] **Step 1: Read** the Next docs; invoke **Hallmark** (app-screen); name the file first.
- [ ] **Step 2: Implement** the page.
- [ ] **Step 3: Verify:** lint, build, sloplint (static), design skills, `curl` the route against the Task 24 fixture job.
- [ ] **Step 4: Commit.**

```bash
git add "web/src/app/research/walkforward/[id]/page.tsx" docs/plan/M6_reporting_research.md
git commit -m "RCM 6.8: the walk-forward report page"
```

---

### Task 37: Milestone close (XI 6.1)

**Files:**
- Modify: `docs/plan/M6_reporting_research.md` (boxes), `docs/plan/00_overview.md` (§4 status, §5 D-rows if any were deferred), `docs/plan/A1_schemas.md`, `docs/plan/A2_interfaces.md` (every new endpoint), `docs/plan/A3_testing.md` (rows: DSR hand example, bootstrap determinism, holdout boundary, export secrets, PBO reference), `docs/plan/RECON.md`

**Interfaces:**
- Produces: M6 marked "built (open: live gates)" in §4 with each 🛑 listed; the RECON closing section with timings measured (bootstrap, VIX read, PBO), results of every verification command, and the owner list.

- [ ] **Step 1: Full suite** in the background: `.\.venv\Scripts\python.exe tests\run_tests.py` exits 0 (build the engine first if it exits 3). `cd web; npm run lint; npm run build` pass. `~/.tools/blockwatch/blockwatch.exe` clean. `git diff BASE -- requirements.txt requirements-lock.txt` is empty (no new package).
- [ ] **Step 2: Run the spec's verification** on the fixture lab (sandbox): `sweep`, `walkforward`, `sensitivity`, `research report`, `compare`, `export`; keep the transcript in RECON. Mark the commands that need the owner's real data (minute bars) 🛑.
- [ ] **Step 3: Milestone audits** (CLAUDE.md): skill `spec-to-code-compliance` over `docs/plan/M6_reporting_research.md` and this plan; `differential-review` over the branch; `insecure-defaults` over the new endpoints; `c-review` was run in Task 14; `supply-chain-risk-auditor` is skipped with the empty dependency diff recorded. Fix findings in rounds; reports go under `.superpowers/audits/`.
- [ ] **Step 4: Update the docs** and the RECON list of owner steps: set `holdout_from`; `research families --backfill --dry-run` then for real; `data report --date D` per recorded day; fetch minute bars; run the demo and read the report.
- [ ] **Step 5: Commit** the doc files named in Files (no "push" until the owner says so).

```bash
git add docs/plan/M6_reporting_research.md docs/plan/00_overview.md docs/plan/A1_schemas.md docs/plan/A2_interfaces.md docs/plan/A3_testing.md docs/plan/RECON.md
git commit -m "XI 6.1: M6 built: reporting and research hygiene, with the live gates listed"
```

(Audit reports under `.superpowers/audits/` are added by explicit file name once they exist.)

---

### Task 38: Project write-up generator and draft (OM 6.1) — M6.12, 🛑 gated

**Files:**
- Create: `stockify/tools/measure.py`, `tests/stockify/test_measure.py`, `docs/plan/writeup_draft.md`
- Modify: `stockify/__main__.py` (`tools measure`)

**Interfaces:**
- Produces: `measure.collect() -> dict` reading only stores and reports and returning, for each measured number, `{"value": …, "n": <sample size>, "source": <table or report>, "needs": None | "<what is missing>"}`: determinism (the share of `rerun` runs whose `determinism.json` was reproduced byte for byte, and the oracle/engine pair on the goldens; **Windows only, cross-platform not measured, D-26**), journal-parity record (`parity_results` kind `journal`: pass / total days), recorder-vs-live divergence (`parity_results` kind `recorder`: median absolute NAV difference in paise and the share of fills matched), margin calibration error (the M4 scorecard's staircase error against Kite's answers), engine throughput (the M3.13 bench re-measured on a full recorded day, D-48). A number whose sample is missing carries `needs` ("5 live days: 0 so far") and **the generator prints "not measured yet" instead of a number**. CLI `python -m stockify tools measure [--json]`.
- `docs/plan/writeup_draft.md` is a draft of the public README section (what STOCKIFY does, the architecture diagram from `00_overview.md` §3 as text, and the measured-numbers table filled only from `measure.collect()`); it contains no personal detail, path, token, account number or host name, and says "paper trading only, no real orders". **Nothing is added to the repository `README.md`, published or pushed by this task.**

- [ ] **Step 1: Write the failing tests:** `test_missing_sample_prints_not_measured_yet_never_a_number`, `test_each_number_carries_its_sample_and_source`, `test_cross_platform_determinism_is_stated_as_not_measured`, `test_output_has_no_paths_or_secrets` (scan for the sandbox path, `KITE`, `token`), `test_json_is_finite`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement the generator and write the draft** from the real output at that moment (every unmeasured row reads "not measured yet").
- [ ] **Step 4: Run** `tests\run_tests.py measure` → PASS.
- [ ] **Step 5: Commit the generator and the draft** and record in RECON the 🛑 gates: (a) five or more live days of journal parity and recorder parity, (b) a full-day throughput measurement, (c) the owner reads and approves every public sentence, (d) the owner's "push". Until then M6.12 stays unticked.

```bash
git add stockify/tools/measure.py stockify/__main__.py tests/stockify/test_measure.py docs/plan/writeup_draft.md docs/plan/RECON.md
git commit -m "OM 6.1: a generator for the measured numbers and a draft write-up that says not measured yet where it is"
```

---

## Owner-only list (collected from the tasks)

1. Choose `holdout_from` in `stockify/config/research.json` (Task 11).
2. Run `python -m stockify research families --backfill --dry-run`, then without `--dry-run`; name families if wanted (Tasks 4, 8).
3. Rebuild the engine on the machine that runs the demo (`.\run.ps1 -BuildEngine`) after Task 14.
4. Re-run `python -m stockify data report --date D` for each recorded day so `/data` has its two new metrics (Task 27).
5. Fetch about 330 calendar days of RELIANCE minute bars with `bars fetch` (needs the Kite login) so the demo sweep and walk-forward have data (Tasks 18, 22).
6. Run the demo, read `research/<job_id>/report.md` and the `/research` pages at 1024 px (Definition of done).
7. Review the D-rows for the statistical choices (job families and `N`, excess-return Sharpe, previous-close VIX) and the one thing not resolved: trials are not pooled across separate sweeps (mismatch row 11).
8. Approve every public sentence of the write-up after the live days exist, and say "push" (Task 38).

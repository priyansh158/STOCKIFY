# M6 Reporting and Research Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Results become comparable, data coverage visible and research honest: run comparison, a safe export, a `/data` coverage page, a trial registry with deflated Sharpe, bootstrap intervals, regime splits, a locked holdout, a sensitivity suite, parameter sweeps, walk-forward analysis and (stretch) the probability of backtest overfitting, all shown on desktop pages in plain words.

**Architecture:**
- Two new Python packages hold the work. `stockify/research/` has the statistics and the trial machinery (pure functions in `stats.py`, `bootstrap.py`, `pbo.py`; orchestration in `registry.py`, `pool.py`, `sweep.py`, `walkforward.py`, `sensitivity.py`). `stockify/reporting/` has `compare.py`, `export.py` and `coverage.py`.
- Every statistic is computed from `lab.sqlite` rows (`run_equity`), never from a run folder, so a pruned folder still counts as a trial.
- Heavy work (sweeps, walk-forward windows, sensitivity re-runs) goes through `python -m stockify run` subprocesses (I-9). Analytics (DSR, bootstrap, PBO) run inline in sync API routes and cache their result as a derived file.
- The API and the web pages only read.

**Tech Stack:** Python 3.12 stdlib (`statistics.NormalDist`, `itertools`, `zipfile`, `sqlite3`), numpy 2.5 and pandas 2.3 (already in the venv), PyYAML (already in `requirements.txt`, used for `sweep.yaml`), matplotlib (already used by `oracle/report.py`), FastAPI, Next.js 16 with hand-written SVG charts. **No new package.** scipy, statsmodels and hypothesis are not installed.

**Spec:** `docs/plan/M6_reporting_research.md` (items M6.1–M6.12). Read it with: `00_overview.md` §2 (I-2, I-3, I-4, I-8, I-9, I-10, I-13, I-14) and §5 (D-26, D-31, D-48, D-76, D-77, D-83, D-84); `A1_schemas.md` §4 (lab tables), §6.1 (run.json), §6.3 (run folder); `A2_interfaces.md` §6 (API table); `A3_testing.md` (U-10); `A4_engine_semantics.md` §8 (metrics); `RECON.md` (M5 close and M6.0 once written); `web/design.md`.

**Base:** BASE = branch `stockify/m5-paper` at the commit Task 1 records. M6 work happens on a branch from it (`stockify/m6-research`) in its own worktree.

## Global Constraints

- **Windows only (D-26).** Run everything with `.\.venv\Scripts\python.exe` (system `python` lacks tzdata). No Linux presets, CI, systemd or POSIX-only code.
- **Tests:** stdlib `unittest`, no pytest. Offline and credential-free. `import tests.support` first (it sandboxes `state_dir`/`logs_dir`); use `tests.support.isolated_env({...})`, never `patch.dict(os.environ, ..., clear=True)`; SQLite-backed tests subclass `SandboxedDBTest` with `db_modules`. Run with `.\.venv\Scripts\python.exe tests\run_tests.py <filter>`; the full suite (4–10 min) runs in the background and must not exit 3 (build the engine first with `.\run.ps1 -BuildEngine`).
- **No new packages without the owner's word.** scipy, statsmodels and hypothesis are not installed, so deflated Sharpe, the stationary block bootstrap, PBO/CSCV and the regime splits use the stdlib plus the numpy and pandas already in the venv. The normal distribution is `statistics.NormalDist` (`cdf`, `inv_cdf`). Any task that would need a new package is marked 🛑 owner with its fallback; none is expected. Charts are hand-written SVG, no chart library.
- **Determinism:** random draws come from the project's seeding only: `stockify.bundle.day_seed` feeding `stockify.oracle.rng.Pcg32`, through `stockify/research/seeding.py` (Task 2), on streams other than the engine's streams 1 and 2. No wall clock in any result: ids and `created_ns` may carry time, no statistic may. Money is int64 paise, quantity units, time UTC epoch ns, IST only at the edges (display, trading dates). Statistics use plain left-to-right loops in A4 §8 order wherever a number must equal `metrics.json` (no `sum()`, no numpy reductions there); numpy is allowed where a result is compared with a tolerance (PBO).
- **UI:**
  - Desktop only (D-83), readable down to about 1024 px, wide tables are fine.
  - Every page is designed with the Hallmark skill (`~/.claude/skills/hallmark`): app-screen flow, `design.md` locked (`web/design.md`), **static** `node ~/.claude/skills/hallmark/scripts/sloplint.mjs <files> --scope app` (never `--render`), no imagegen.
  - Tokens from `globals.css` and `tokens.css`; components from `web/src/components/ui.tsx` (`Panel`, `PageHead`, `Stat`, `Table`, `Chip`, `Skeleton`, `Empty`); every data region has loading, empty, error and full states.
  - Money is ₹ through `web/src/lib/format.ts` (`Intl.NumberFormat("en-IN")`), never `$`.
  - **Plain words.** Every statistic on screen sits beside a one-line plain-English meaning (for example "best of 12 tries, after allowing for luck"). The sentences are written once, server-side, in `stockify/research/plain.py` (Task 6) and shipped in the DTOs as `plain` fields, so the CLI, `report.md` and the pages say the same thing. No raw code, enum or Greek letter reaches the screen.
  - Next.js 16 has breaking changes: read the relevant pages of `web/node_modules/next/dist/docs/` before writing web code (run `.\run.ps1 -InstallWeb` if `node_modules` is missing). `searchParams` and `params` are Promises (see `web/src/app/tests/page.tsx`).
  - `webapp-testing` cannot run (no `playwright`); verification is lint, build, sloplint, the pure-function checks (`tests/stockify/*_check.ts` pattern, see `test_param_checks.py`) and the owner's look at 1024 px.
- **API:** loopback only (D-77): new routes live in routers already wrapped by `server/local_only.py`; a new router is registered in `server/main.py` the same way as the others. `server/schemas.py` ↔ `web/src/lib/types.ts` are tied by the blockwatch block `dto-fields`: append DTOs to both files in the same order and run `~/.tools/blockwatch/blockwatch.exe`. Fixed paths (`/api/tests/compare`) are declared before `/api/tests/{run_id}`. List endpoints take `offset`/`limit`. Sync `def` routes for anything that reads files or computes.
- **Data safety:**
  - The real lab (`%LOCALAPPDATA%\STOCKIFY`) is read-only apart from derived outputs: `runs/<id>/research.json`, `runs/<id>/sensitivity.json`, `research/<job_id>/…`, `exports/…`, and new `lab.sqlite` rows made by the new commands. Tests never touch it.
  - Runs are never deleted from the registry (M6.4): migration 0005 adds a `BEFORE DELETE` trigger that aborts.
  - Recorded data is irreplaceable (I-13): no migration touches `ticks/` or `refdata.sqlite`; the only change near them is new `data_quality` metric rows (Task 20).
  - **I-10:** an export never holds a secret. Export copies from an allow-list of file names, and a test scans every member of the zip for token-like strings.
- **Config, not constants (I-1 in spirit):** every grid, edge and threshold lives in `stockify/config/research.json` (Task 2). These are not market rules, so no `effective_from` rows, but the loader validates them.
- **Commits:** `<POS> 6.<n>: summary` (D-31; the roster below is provisional, the controller rules). The owner's git identity, no co-author, tool or "agent" lines (skill `no-agentic-coauthor`). Stage explicit paths only; never `CLAUDE.md` (D-29). Push only on the owner's "push". Each task ticks its boxes in `docs/plan/M6_reporting_research.md` in the same commit (README step 4); a deviation gets the next free `D-xx` row in `00_overview.md` §5 in the same commit (`D-85` onward). The pre-commit hook runs gitleaks, then blockwatch; both fail closed.
- **Process:** subagent-driven-development, one implementer per task, then a task review, then fix rounds. A task commits once its offline tests pass; a live or owner step after that is a 🛑 recorded in RECON as "code done, live pending" and never blocks the next task.
- **Skills by what a diff touches (CLAUDE.md):** new config key, CLI flag or endpoint → `sharp-edges` and `insecure-defaults` (report only); money arithmetic → `dimensional-analysis` (report only, never write annotations); parsers and round-trips → the property-based-testing method with the stdlib; `web/` → `design:design-critique`, `design:accessibility-review`, `design:ux-copy`; statistics → the statistics skills as method only (scipy/statsmodels absent).
- **Provisional roster (controller rules, owner reviews in RECON):** RCB recon · AM registry and config · IL statistics · IR bootstrap and regimes · PM holdout and pool · WF sweeps and walk-forward · FW sensitivity · LF PBO · DMF compare and export · LCM coverage · RCM web pages · OM write-up · XI bookkeeping.

## Review Focus

Failure modes the spec implies that no single item's tests would catch. Each line is pinned by a named test in the owning task.

1. **Short, flat or empty return series** (a run that never traded, one trading day, a failed or running run, a pruned folder). The owner expects "not enough days to say" in words, never `NaN`, `inf`, a crash or a blank page; JSON never carries `NaN`. Pinned by `test_flat_series_has_no_sharpe_but_a_sentence` (Task 4), `test_short_series_bootstrap_says_so` (Task 5), `test_pruned_folder_still_counts` (Task 3).
2. **A family of one, or of identical trials.** "Best of 1 try" must give `SR₀ = 0` and `DSR = PSR`, and identical Sharpes (variance 0) must not divide by zero. Pinned by `test_one_trial_dsr_equals_psr` and `test_identical_trials_have_zero_expected_max` (Task 4).
3. **Variants inflating the trial count.** Sensitivity re-runs, walk-forward test pieces and re-runs are not new tries; counting them would make every strategy look luckier than it is, and not counting sweep trials would hide the luck. Pinned by `test_variants_and_oos_runs_are_not_trials` (Task 3) and `test_sensitivity_runs_do_not_change_n` (Task 12).
4. **The holdout boundary.** A run ending the day before `holdout_from` passes, one ending on it is locked, a pack named only by its hash is checked through its manifest, an empty or whitespace reason is refused, and a walk-forward whose test window crosses the line is locked as a whole. Pinned by Task 9 and `test_walkforward_test_window_crossing_holdout_is_locked` (Task 14).
5. **A lab with no or partial recordings.** `/data` on an empty lab, on a day with only a landing file, on weekends and holidays, and with no `data report` run for a day must show "no recording", "not reported yet" or "market closed", never a zero that looks like a measurement. Pinned by `test_empty_lab_matrix_has_no_numbers` and `test_unreported_day_is_not_a_zero` (Task 20).

## Gates and owner steps

| Item | Offline (code, tests) | 🛑 Owner or live gate |
|---|---|---|
| M6.1 compare | Task 18, 22 | none |
| M6.2 export | Task 19 | owner opens one export (optional) |
| M6.3 `/data` | Task 20, 23 | 🛑 owner re-runs `data report --date D` for each recorded day so the new coverage metrics exist (derived rows only); 🛑 live: "full coverage history" needs the recorded days to exist |
| M6.4 registry, DSR | Task 3, 4, 6, 7 | 🛑 owner runs `research families --backfill` once on the real lab (updates only NULL `family_id` rows; `--dry-run` first) and names families if wanted |
| M6.5 walk-forward | Task 14, 15 | 🛑 live: the demo needs about 330 calendar days of cached RELIANCE minute bars (train 120d + a few 30d tests), fetched with `bars fetch` (Kite login) |
| M6.6 holdout | Task 9 | 🛑 owner chooses `holdout_from` (shipped `null`; the plan suggests the last three months) |
| M6.7 sensitivity | Task 11, 12 | demo on the best real run, after the sweep |
| M6.8, M6.9 | Task 5, 8 | VIX source: if reading the recorded `INDIA VIX` is too slow, Task 8 falls back to bhavcopy and records a D-row (owner reviews) |
| M6.10 sweeps | Task 13, 24 | the demo sweep needs the same minute bars as M6.5 |
| M6.11 PBO (stretch) | Task 16 | none |
| M6.12 write-up | Task 26 only prepares the generator and the draft | 🛑 gated on live days and the owner's approval of every public sentence; nothing is published or pushed without "push" |
| Definition of done | Task 25 | 🛑 the owner reads the demo report |

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `docs/plan/RECON.md` (append) | M6.0 findings, mismatch table, provisional decisions | 1 |
| `stockify/config/research.json`, `stockify/config/loader.py` (modify) | every M6 grid, edge and threshold, validated | 2 |
| `stockify/research/seeding.py` | `stream(seed, purpose, anchor)` over `day_seed` and `Pcg32` | 2 |
| `stockify/paths.py` (modify) | `research_dir`, `research_path`, `exports_dir`, `export_path` | 2 |
| `stockify/data/migrations/lab/0005_research.sql`, `stockify/store.py` (modify) | `runs.role`, `runs.job_id`, `research_jobs`, `trial_families.named`, no-delete triggers; store functions | 3 |
| `stockify/research/registry.py`, `stockify/runs.py`, `stockify/sessionconfig.py` (modify) | default family id, family rows, roles, trial listing | 3 |
| `stockify/research/stats.py` | returns, moments, Sharpe, PSR, expected maximum, DSR | 4 |
| `stockify/research/bootstrap.py` | stationary block bootstrap and intervals | 5 |
| `stockify/research/plain.py` | the plain-English sentence for every statistic | 6 |
| `stockify/research/report.py` | `run_research`, `family_report`, `list_families`, derived-file cache | 6 |
| `server/routers/research.py`, `server/routers/tests.py`, `server/schemas.py`, `web/src/lib/types.ts` (modify) | research API and DTOs | 7 and later |
| `stockify/research/regimes.py` | month, weekday, expiry, VIX and gap splits | 8 |
| `stockify/research/holdout.py` | locked holdout and its log | 9 |
| `stockify/research/pool.py` | bounded parallel `stockify run` subprocesses | 10 |
| `stockify/runconfig.py`, `stockify/bundle.py` (modify) | `costs.scale` | 11 |
| `stockify/research/sensitivity.py` | variants, suite, fragile flag, `sensitivity.json` | 12 |
| `stockify/research/sweep.py`, `examples/sweeps/orb_grid.yaml` | `sweep.yaml`, expansion, execution, results | 13 |
| `stockify/research/walkforward.py` | windows, per-window sweeps, selection, stitching, report | 14, 15 |
| `stockify/research/pbo.py` | CSCV | 16 |
| `stockify/research/demo_report.py` | `research report`: the combined demo report | 17 |
| `stockify/reporting/compare.py` | run comparison | 18 |
| `stockify/reporting/export.py` | zip export | 19 |
| `stockify/reporting/coverage.py`, `stockify/data/quality.py` (modify), `server/routers/data.py` (modify) | coverage matrix, gaps, storage | 20 |
| `stockify/__main__.py` (modify) | `compare`, `export`, `sweep`, `walkforward`, `sensitivity` leave `_STUBS`; `research` is new; `run` gains `--role`, `--job-id`, `--unlock-holdout` | 9, 10, 12, 13, 15, 17, 18, 19 |
| `web/src/components/charts.tsx`, `web/src/lib/research.ts`, `web/src/lib/data.ts` | SVG charts, fetchers, pure helpers | 21–24 |
| `web/src/app/tests/[id]/page.tsx` (modify), `web/src/app/tests/compare/page.tsx`, `web/src/app/data/page.tsx`, `web/src/app/research/…`, `web/src/components/AppNav.tsx` (modify) | pages | 21–24 |
| `tests/stockify/research/`, `tests/stockify/reporting/` (new packages, each with `__init__.py` like `tests/stockify/daemon/`) | tests | all |

## Mismatches between the M6 spec and the code

Task 1 records these in RECON as **provisional decisions** (controller ruling; owner reviews). The task that implements each writes its D-row.

| # | The spec assumes | The code at BASE | Resolution |
|---|---|---|---|
| 1 | M6.1/M6.2 add `compare` and `export` | `__main__._STUBS` lists `compare`, `export`, `sweep`, `walkforward`, `sensitivity` as "M6" stubs. `stockify/compare.py` and `_cmd_compare` already exist but diff two run folders' deterministic files (`compare-artifacts`). | New code lives in `stockify/reporting/` and `stockify/research/`; the five stubs become real commands; `compare-artifacts` and `stockify/compare.py` are untouched. |
| 2 | `GET /api/tests/compare` is M6.1 | It exists as a 501 stub declared before `/api/tests/{run_id}`. | Task 18 replaces the body, keeps the position. |
| 3 | M6.4 "tables `trial_families`, run `family_id`" | Both already exist (lab migration 0001) and `runconfig` already accepts `family_id` and `unlock_holdout`; `holdout_unlocks` exists too. Nothing fills them. | Migration 0005 adds only columns, `research_jobs` and triggers; no table is recreated. |
| 4 | M6.4 "every run joins the family"; M6.5 "every run joins the family" | Counting sensitivity re-runs, walk-forward test pieces and determinism re-runs as trials would inflate `N` for no reason. | `runs.role` ∈ `trial`, `sensitivity`, `oos`, `rerun`. All carry the family id; only `trial` runs with status `done` count in `N` and `V`. Sessions carry a family id and are listed in the family but are not trials (they do not search). **D-row.** |
| 5 | M6.4 "Sharpe of the selected run, per-period" | The run page's Sharpe is on excess returns (`x_d = r_d − rate_d/252`, A4 §8, `pricing.json` risk-free rows). | The statistics use those same excess returns, so `sharpe_per_period · √252` equals `metrics.json` Sharpe to 1e-12 (asserted). **D-row.** |
| 6 | M6.7 "costs × {0, 1, 1.5, 2}" | No run-config key scales costs; `costs` holds `broker`, `allow_unverified`, `auto_square_off_fee`, `dp_charges`. Post-hoc scaling is not exact (costs change cash and margin). | New key `costs.scale` (decimal string, default `"1"`, omitted from the normalised form when `"1"`), applied once in `bundle.resolve_bundle`. Both engines read the bundle, so neither changes. **D-row.** |
| 7 | M6.7 "slippage + {0,1,2} ticks, every applicable fill model" | `fill_model.slippage` has one kind. "Applicable" is not defined. | "+k ticks" is `fixed_ticks` (k + existing k when `fixed_ticks`, k when `none`); `random_ticks` and `bps` bases are reported "not applicable". A fill model is applicable when A4 §4 allows it for the run's datasets; a variant that still fails is listed with its error. One axis at a time, not a cross product. |
| 8 | M6.6 "runs whose data overlaps the holdout" | A run's dates come from its pack specs (`from`/`to`) or, for `{"sha256"}` packs, from the pack manifest. Paper sessions trade the present. | Overlap = the run's last day ≥ `holdout_from`; an open `to` overlaps. Sessions are exempt. The reason is the existing `unlock_holdout` config key. **D-row.** |
| 9 | M6.5 `--train 120d` | "d" is not defined. | Calendar days; windows are half-open on calendar dates; `step ≥ test` is required so out-of-sample pieces never overlap. A partial last test window is dropped. **D-row.** |
| 10 | M6.5 "deflated Sharpe … given the number of trials" | The stitched curve is not the maximum of anything. | `N` = every training run of the job (windows × grid), `V` = variance of their per-period Sharpes: the honest, conservative count. **D-row, owner reviews the wording.** |
| 11 | M6.9 "VIX bucket (recorded `INDIA VIX`, else bhavcopy)" | Reading one instrument from a full-day archive is not cheap. | The bucket uses the previous trading day's close (what was known at the open); recorded first, bhavcopy otherwise, source shown per day; Task 8 measures and may flip the order with a D-row. |
| 12 | M6.3 "recorder coverage % (FULL and QUOTE)" | `data_quality` has per-instrument `coverage_pct`, no per-mode figure. | Task 20 adds two day-level metrics (`coverage_full_pct`, `coverage_quote_pct`) to `quality.py`; the matrix reads them and shows "not reported yet" until `data report` has run for the day. |
| 13 | M6.12 "determinism across platforms" | D-26: Windows only; DT-02 cross-platform checks are dropped. | The write-up reports determinism across repeated runs, builds and the oracle/engine pair on Windows, and says plainly that cross-platform was not measured. |
| 14 | M6 verification `python -m stockify sweep --config examples/sweeps/orb_grid.yaml` | Neither the command nor `examples/sweeps/` exists. | Task 13 creates both. |
| 15 | M5 plan's "layouts ≤390 px, tap targets 44 px" | Superseded by D-83. | M6 pages are desktop (about 1024 px and up). |

---

### Task 1: M6.0 recon and provisional decisions (RCB 6.1)

**Files:**
- Modify: `docs/plan/RECON.md` (append `## 2026-11-12 — M6.0 recon (RCB 6.1)`), `docs/plan/M6_reporting_research.md` (add a "M6.0 recon" box)

**Interfaces:**
- Produces: the recorded BASE, the verified mismatch table, the provisional decisions (each task writes its own D-row), the provisional roster, and the list of 🛑 owner steps (this plan's gates table).

- [ ] **Step 1: Restate the base.** `git log -1 --format="%H %s"`, `git status --short`. Create the branch `stockify/m6-research` from BASE in its own worktree (skill `using-git-worktrees`). Record BASE and any uncommitted file.
- [ ] **Step 2: Check the toolchain.** `.\.venv\Scripts\python.exe -c "import numpy, pandas, yaml, matplotlib; print(numpy.__version__, pandas.__version__, yaml.__version__)"` prints 2.5.x / 2.3.x / 6.0.x; `-c "import scipy"` raises `ModuleNotFoundError` (expected, so is `statsmodels`/`hypothesis`). `.\.venv\Scripts\python.exe -c "from statistics import NormalDist; print(NormalDist().inv_cdf(0.9))"` prints 1.2815…. Record `requirements.txt` has PyYAML.
- [ ] **Step 3: Verify each mismatch row** with one search per row, for example `rg -n "_STUBS|compare-artifacts|class .*Compare" stockify/__main__.py stockify/compare.py`, `rg -n "def compare_tests" server/routers/tests.py`, `rg -n "trial_families|holdout_unlocks|family_id|unlock_holdout" stockify server`, `rg -n "FILL_KINDS" stockify`, `rg -n "def phases|def trading_days" stockify/data/calendar.py`, `rg -n "risk_free" stockify`. Record file and line for each.
- [ ] **Step 4: Read the Next.js 16 docs** pages for dynamic routes, `searchParams` and server components in `web/node_modules/next/dist/docs/` and note the paths in RECON for the web tasks. Read `web/design.md` and `~/.claude/skills/hallmark/references/flow.md` § App screens once.
- [ ] **Step 5: Baseline.** Start the full test suite in the background (`.\.venv\Scripts\python.exe tests\run_tests.py`); record the result and exit code, and `cd web; npm run lint; npm run build` result. Any pre-existing failure is recorded, not fixed here.
- [ ] **Step 6: Inventory the real lab (read-only).** Count `runs` rows, `tick_days` rows by status, `nse_fetch_log` datasets, cached bar ranges (`bar_fetch_log`) and which minute bars exist for RELIANCE. Record the counts only; this decides what the demo can show and what the owner must fetch.
- [ ] **Step 7: Write the RECON section** (table, provisional decisions, roster, gates) and commit.

```bash
git add docs/plan/RECON.md docs/plan/M6_reporting_research.md
git commit -m "RCB 6.1: M6 recon: what the runs store, the stubs and the data layer really look like"
```

---

### Task 2: Research config, seeding and paths (AM 6.1)

**Files:**
- Modify: `stockify/config/research.json`, `stockify/config/loader.py`, `stockify/paths.py`
- Create: `stockify/research/__init__.py`, `stockify/research/seeding.py`
- Test: `tests/stockify/research/__init__.py`, `tests/stockify/research/test_config.py`, `tests/stockify/research/test_seeding.py`; extend `tests/stockify/test_paths.py`

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
  "sweep": {"max_workers": null} }
```

- `research.settings() -> dict` (in `stockify/research/__init__.py`): the loaded, validated file (`loader.load_json("research.json")`).
- `seeding.STREAMS = {"bootstrap": 11, "pbo": 12, "random_search": 13}` and `seeding.ANCHOR = date(2000, 1, 3)`; `seeding.stream(seed: int, purpose: str, anchor: date | None = None) -> Pcg32` = `Pcg32(day_seed(seed, anchor or ANCHOR), STREAMS[purpose])`. A purpose not in `STREAMS` raises `KeyError`.
- `paths.research_dir() -> Path`, `paths.research_path(job_id: str) -> Path` (one safe segment ≤ 128, direct child of `research_dir()`, else `ValueError`, like `run_path`), `paths.exports_dir() -> Path`, `paths.export_path(run_id: str, with_packs: bool) -> Path` (`<exports>/<run_id>.zip` or `<run_id>-with-packs.zip`; the id checked by `run_path`).

- [ ] **Step 1: Write the failing tests.**
  - `test_shipped_research_json_validates`.
  - `test_holdout_from_must_be_an_iso_date_or_null` (`"2026-13-40"` and `20260801` refused).
  - `test_bootstrap_ci_must_be_in_the_open_unit_interval` (`"0"`, `"1"`, `"0.9x"` refused; `resamples` ≥ 100; `mean_block` ≥ 1).
  - `test_decimal_fields_are_strings` (a JSON number for `cost_scale` refused, naming the key).
  - `test_edges_must_be_strictly_increasing`.
  - `test_unknown_key_refused_naming_it` (any level).
  - `test_pbo_slices_must_be_even_and_at_least_4`.
  - Seeding: `test_streams_are_not_the_engines` (no value in `{1, 2}`, all distinct), `test_same_seed_same_numbers`, `test_state_is_day_seed_of_the_anchor` (`stream(7, "pbo").state` equals what `Pcg32(day_seed(7, ANCHOR), 12).state` gives), `test_unknown_purpose_raises`.
  - Paths: `test_research_path_refuses_unsafe_ids` (`..`, `a/b`, `CON`), `test_exports_dir_is_under_the_lab`, `test_export_path_names_the_variant`.
- [ ] **Step 2: Run to verify failure.** `.\.venv\Scripts\python.exe tests\run_tests.py research` → FAIL (modules missing).
- [ ] **Step 3: Implement** the config file, the loader validator (follow how `loader.py` validates the other `*.json`; `tests/stockify/test_config_coverage.py` must stay green, extend it if it enumerates keys), `seeding.py`, `settings()` and the four path helpers.
- [ ] **Step 4: Run** `tests\run_tests.py research` and `tests\run_tests.py config` → PASS; `.\.venv\Scripts\python.exe -m stockify config` validates.
- [ ] **Step 5: Commit** (add the M6.0 config boxes; D-row not needed).

```bash
git add stockify/config/research.json stockify/config/loader.py stockify/paths.py stockify/research tests/stockify/research tests/stockify/test_paths.py docs/plan/M6_reporting_research.md
git commit -m "AM 6.1: research config, a seeded stream per purpose, and the research and export folders"
```

---

### Task 3: Trial registry: migration 0005, families, roles (AM 6.2) — M6.4

**Files:**
- Create: `stockify/data/migrations/lab/0005_research.sql`, `stockify/research/registry.py`, `tests/stockify/research/fixtures.py`
- Modify: `stockify/store.py` (`SCHEMA_VERSION = 5`, `RUN_FIELDS`, new functions), `stockify/runs.py` (`queue`, `launch`, `rerun`, `import_run`), `stockify/sessionconfig.py` (default family), `docs/plan/A1_schemas.md` §4
- Test: `tests/stockify/research/test_registry.py`, `tests/stockify/test_store_migrations.py` (extend)

**Interfaces:**
- Produces, migration 0005 (additive only, I-13):

```sql
ALTER TABLE runs ADD COLUMN role TEXT NOT NULL DEFAULT 'trial';      -- trial | sensitivity | oos | rerun
ALTER TABLE runs ADD COLUMN job_id TEXT;                              -- the sweep / walk-forward job
ALTER TABLE trial_families ADD COLUMN named INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS ix_runs_family ON runs (family_id, role, status);
CREATE TABLE IF NOT EXISTS research_jobs (job_id TEXT PRIMARY KEY, kind TEXT NOT NULL,   -- sweep | walkforward
  name TEXT, family_id TEXT, status TEXT NOT NULL, created_ns INTEGER NOT NULL, finished_ns INTEGER,
  folder TEXT, spec_json TEXT, error TEXT);
CREATE TRIGGER IF NOT EXISTS runs_are_never_deleted BEFORE DELETE ON runs
  BEGIN SELECT RAISE(ABORT, 'runs are never deleted from the registry (M6.4)'); END;
CREATE TRIGGER IF NOT EXISTS holdout_unlocks_no_update BEFORE UPDATE ON holdout_unlocks
  BEGIN SELECT RAISE(ABORT, 'holdout unlocks are a log (M6.6)'); END;
CREATE TRIGGER IF NOT EXISTS holdout_unlocks_no_delete BEFORE DELETE ON holdout_unlocks
  BEGIN SELECT RAISE(ABORT, 'holdout unlocks are a log (M6.6)'); END;
```

- `store` (all new, same locking as the neighbours): `RUN_FIELDS` gains `"role", "job_id"`; `add_family(family_id: str, *, objective: str, description: str | None = None, named: bool = False, now_ns: int | None = None) -> bool` (insert if absent; returns whether it inserted); `name_family(family_id: str, description: str) -> None` (sets `named = 1`); `get_family(family_id) -> dict | None`; `list_families() -> list[dict]`; `family_runs(family_id: str, *, roles: tuple[str, ...] | None = ("trial",), status: str | None = "done") -> list[dict]` (oldest `created_ns` first, then `run_id`); `run_nav_series(run_id: str) -> list[tuple[str, int]]` (`(trade_date, nav_paise)` ascending from `run_equity`); `log_holdout_unlock(run_id: str, family_id: str | None, reason: str, *, now_ns: int | None = None) -> None`; `holdout_unlock_rows(family_id: str | None = None) -> list[dict]`; `create_research_job(row: dict) -> None`, `update_research_job(job_id: str, **fields) -> bool`, `get_research_job(job_id) -> dict | None`, `list_research_jobs(kind: str | None = None, offset: int = 0, limit: int = 50) -> list[dict]`; `backfill_null_families(compute) -> int` (calls `compute(row) -> str` for each `runs` row whose `family_id IS NULL`, updates only that column, returns the count; `compute` may return `None` to skip).
- `registry.ROLES = ("trial", "sensitivity", "oos", "rerun")`; `registry.OBJECTIVES = ("sharpe", "sortino", "calmar", "cagr", "net_pnl")`; `registry.default_family_id(cfg: dict, objective: str = "sharpe") -> str` = `"<strategy>/<dataset>-<interval>-<universe>/<objective>"`: `<strategy>` is the module's last part of a `python` ref (or the registered name of a `cpp` ref, or `script`); the dataset and interval come from the first pack spec (`packs` when packs are named by hash only); `<universe>` is the first instrument with every character outside `[A-Za-z0-9._-]` replaced by `_`, plus `_plus<n>` for n more; the result must pass `runconfig.validate`'s family rule (≤128 characters, `/`-separated safe segments), truncating the universe part when needed; `registry.family_for_session(cfg: dict) -> str` (same rule with dataset `live`); `registry.ensure_family(family_id: str, objective: str = "sharpe") -> None`; `registry.trials(family_id: str) -> list[Trial]` where `Trial` is a frozen dataclass `(run_id, name, created_ns, job_id, dates: tuple[str, ...], navs: tuple[int, ...], capital_paise: int)` read from `run_equity` and `config_json` only; `registry.backfill(dry_run: bool = False) -> int`.
- `runs.queue(config, *, role="trial", job_id=None)` and `runs.launch(..., role="trial", job_id=None)` fill a missing `family_id` with `default_family_id(cfg)` (written into the stored normalised config too, so a re-run keeps it), call `ensure_family`, and store `role` and `job_id`. `runs.rerun` creates its row with `role="rerun"` and the source's family. `import_run` keeps an existing row's `role`/`job_id`.
- `tests/stockify/research/fixtures.py`: `make_run(run_id: str, family_id: str | None, *, daily_returns: list[float] | None = None, navs: list[int] | None = None, capital_paise: int = 100_000_000, role: str = "trial", status: str = "done", job_id: str | None = None, start: str = "2026-01-05", with_folder: bool = False) -> None` writes a `runs` row (with `config_json` and `metrics_json` carrying `nav_start_paise`, `nav_end_paise`, `sharpe` from `oracle.metrics`-compatible arithmetic) and its `run_equity` rows over consecutive weekdays from `start`; with `with_folder` also a minimal run folder.

- [ ] **Step 1: Write the failing tests** (`SandboxedDBTest`, `db_modules = (stockify.store,)`):
  - `test_migration_0005_applies_over_a_populated_0004_lab` (rows from `runs`, `session_days`, `holdout_unlocks` intact; `store.schema_version() == 5`).
  - `test_runs_cannot_be_deleted` (`DELETE FROM runs` raises `sqlite3.DatabaseError`; the row is still there).
  - `test_holdout_unlock_log_is_append_only`.
  - `test_default_family_id_shape`: the ORB RELIANCE minute config (`examples/runs/orb_reliance_minute.json`) → starts with `opening_range_breakout/`, ends with `/sharpe`, passes `runconfig.validate`; an instrument `NSE:RELIANCE` becomes `NSE_RELIANCE`; 200 instruments stay ≤ 128 characters.
  - `test_new_run_gets_the_default_family_and_a_family_row`; `test_explicit_family_is_kept`.
  - `test_variants_and_oos_runs_are_not_trials` (roles `sensitivity`, `oos`, `rerun` appear in `family_runs(roles=None)` but not in `trials`).
  - `test_failed_queued_and_running_runs_are_not_trials`.
  - `test_pruned_folder_still_counts` (delete the run folder; `trials()` unchanged, built from `run_equity`).
  - `test_naming_a_family_survives_new_runs` (`named` stays 1, description kept).
  - `test_session_gets_a_family_with_dataset_live`.
  - `test_backfill_updates_only_null_rows_and_dry_run_changes_nothing`.
- [ ] **Step 2: Run to verify failure.** `tests\run_tests.py research` and `tests\run_tests.py store` → FAIL.
- [ ] **Step 3: Write `0005_research.sql`; implement the store functions** (never raw `UPDATE` of anything but `family_id` in `backfill_null_families`), **`registry.py`, the `runs.py` and `sessionconfig.py` changes, and `fixtures.make_run`.** Update A1 §4 with the new columns, table and triggers. Grep `tests/` for `DELETE FROM runs`; none may remain.
- [ ] **Step 4: Run** `tests\run_tests.py research`, `store`, `runs`, `session` → PASS.
- [ ] **Step 5: D-row** (roles; sessions not trials) and the M6.4 boxes ticked; **Commit.**

```bash
git add stockify/data/migrations/lab/0005_research.sql stockify/store.py stockify/runs.py stockify/sessionconfig.py stockify/research tests/stockify docs/plan
git commit -m "AM 6.2: every run and session has a family, runs can never be deleted, and only real tries count"
```

---

### Task 4: Sharpe, probabilistic and deflated Sharpe (IL 6.1) — M6.4

**Files:**
- Create: `stockify/research/stats.py`
- Test: `tests/stockify/research/test_stats.py`

**Interfaces:**
- Produces (pure functions, no I/O, plain loops in A4 §8 order; floats; undefined is `None`, never `NaN`):
  - `Moments` frozen dataclass `(n: int, mean: float, sd: float, skew: float | None, kurt: float | None)`; `moments(x: list[float]) -> Moments` with `sd` sample (ddof 1), `skew = m3 / m2**1.5` and `kurt = m4 / m2**2` (**not** excess) using the population moments `m_k = Σ(x − mean)^k / n`; `skew`/`kurt` are `None` when `m2 == 0`.
  - `risk_free_rows() -> list[dict]` (`loader.load_json("pricing.json")["risk_free"]`) and `rate_on(rows: list[dict], d: str) -> float` (same rule as `oracle.metrics._rate_on`).
  - `returns_from_nav(capital: int, navs: list[int]) -> list[float]`: `r_d = NAV_d / NAV_{d−1} − 1` with `NAV_0 = capital` (paise converted to float once).
  - `excess_returns(dates: list[str], r: list[float], rf_rows: list[dict]) -> list[float]`: `x_d = r_d − rate_on(rf_rows, dates[d]) / 252`.
  - `sharpe_per_period(x: list[float]) -> float | None` (`mean / sd`; `None` when `n < 2`; **`0.0` when all values are equal**, `sd` forced to exactly 0 as A4 does, so a run that never traded has Sharpe 0, not undefined); `annualised(sr: float) -> float` (`· √252`).
  - `expected_max_sharpe(n_trials: int, var_sharpe: float) -> float`: `0.0` for `n_trials ≤ 1`, else `√V · ((1 − γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e)))`, `γ = 0.5772156649015329`, `Φ = NormalDist()`.
  - `probabilistic_sharpe(sr: float, n: int, skew: float, kurt: float, benchmark: float = 0.0) -> float | None` = `Φ((sr − benchmark)·√(n − 1) / √(1 − skew·sr + ((kurt − 1)/4)·sr²))`; `None` when `n < 2` or the radicand is ≤ 0.
  - `DsrResult` frozen dataclass `(sr: float, t: int, skew: float, kurt: float, n_trials: int, var_sharpe: float, sr0: float, dsr: float | None, psr: float | None)`; `deflated_sharpe(x: list[float], trial_srs: list[float | None], n_trials: int) -> DsrResult | None` where `x` is the selected run's excess returns, `trial_srs` the family's per-period Sharpes (undefined ones are `None`: they count in `n_trials` but not in `V`), `V` = sample variance (ddof 1) of the defined ones (0.0 when fewer than two). Returns `None` when `x` has fewer than 2 values or `moments(x)` has no skew.
- Consumes: `oracle.metrics` only in the tests.

- [ ] **Step 1: Write the failing tests.**
  - **Hand-computed example** `test_dsr_matches_the_hand_computed_example`: `SR = 0.1`, `T = 250`, `skew = −0.5`, `kurt = 4.0`, `N = 10`, `V = 0.0004` give `SR₀ = 0.031491966026915`, `DSR = 0.8534253186458305`, `PSR = 0.9375434364545412`, each within 1e-9 (call `expected_max_sharpe(10, 0.0004)` and `probabilistic_sharpe` directly with `benchmark = SR₀` and `0.0`).
  - `test_one_trial_dsr_equals_psr` (`n_trials = 1` → `sr0 == 0.0`, `dsr == psr`).
  - `test_identical_trials_have_zero_expected_max` (`V = 0` → `sr0 == 0.0`, no exception).
  - `test_more_trials_raise_the_bar` (`expected_max_sharpe(100, v) > expected_max_sharpe(10, v) > expected_max_sharpe(2, v)`).
  - `test_moments_known_series`: `[0, 0, 3]` → skew `0.7071067811865476` (1e-12); `[−1, 1, −1, 1]` → kurt `1.0`; `[−1, 0, 0, 1]` → kurt `2.0`; constant series → `skew is None`.
  - `test_flat_series_has_no_sharpe_but_a_sentence`: `[0.0] * 30` → `sharpe_per_period == 0.0`, `deflated_sharpe(...) is None` (no skew), and a one-value series → `sharpe_per_period is None`; no result field is `NaN` or `inf` (`math.isfinite`).
  - `test_radicand_not_positive_gives_none`.
  - `test_sharpe_matches_metrics_json`: build `equity` rows and call `oracle.metrics.compute(capital=…, equity=…, fills=[], orders={}, charges=[], risk_free=rf, rejects={}, participation_ppm=0, exposure=None)`; `annualised(sharpe_per_period(excess_returns(...)))` equals `metrics["sharpe"]` within 1e-12, with `rf` empty and with a dated 7% row.
  - `test_undefined_sharpes_count_in_n_not_in_v`.
- [ ] **Step 2: Run to verify failure.** `tests\run_tests.py research.test_stats` → FAIL.
- [ ] **Step 3: Implement `stats.py`** as specified.
- [ ] **Step 4: Run** → PASS. Skill `dimensional-analysis` over the diff (report only).
- [ ] **Step 5: D-row** (excess returns, `N`/`V` definition, non-excess kurtosis, ddof); **Commit.**

```bash
git add stockify/research/stats.py tests/stockify/research/test_stats.py docs/plan
git commit -m "IL 6.1: Sharpe, probabilistic and deflated Sharpe from the stdlib, checked against a hand-computed example"
```

---

### Task 5: Stationary block bootstrap intervals (IR 6.1) — M6.8

**Files:**
- Create: `stockify/research/bootstrap.py`
- Test: `tests/stockify/research/test_bootstrap.py`

**Interfaces:**
- Consumes: `seeding.stream`, `stats.sharpe_per_period`, `stats.annualised`; the callers pass `mean_block`, `resamples`, `ci` and `min_days` from `research.settings()["bootstrap"]` (decimal strings converted to float at that edge).
- Produces:
  - `percentile(sorted_values: list[float], q: float) -> float` (linear interpolation between ranks, `q` in [0, 1]; no numpy).
  - `stationary_indices(n: int, mean_block: float, rng: Pcg32) -> list[int]` (Politis–Romano: a start index uniform on `[0, n)`; at each following step, with probability `1/mean_block` (an integer threshold on `next_u32`) restart at a new uniform index, else take the next index, wrapping circularly; length `n`).
  - `Interval` frozen dataclass `(point: float | None, lo: float | None, hi: float | None)`; `BootstrapResult` frozen dataclass `(n_days: int, mean_block: float, resamples: int, ci: float, seed: int, sharpe: Interval, mean_daily_return: Interval, max_drawdown: Interval, enough_days: bool)`.
  - `bootstrap_ci(r: list[float], x: list[float], *, mean_block: float, resamples: int, ci: float, min_days: int, seed: int, last_date: date) -> BootstrapResult`: `r` plain daily returns, `x` the matching excess returns; each resample draws one index list and applies it to both, so `sharpe` = annualised Sharpe of `x[idx]`, `mean_daily_return` = mean of `r[idx]`, `max_drawdown` from the compounded path of `r[idx]` starting at 1.0 (A4 §8 definition: peak starts at `NAV_0`). The generator is `seeding.stream(seed, "bootstrap", last_date)`. Intervals are the `(1−ci)/2` and `1−(1−ci)/2` percentiles of the resampled values (a resample with an undefined Sharpe is dropped from that interval only). `point` is the statistic on the original series. With fewer than `min_days` values: `enough_days = False` and every interval is `Interval(None, None, None)`.

- [ ] **Step 1: Write the failing tests.**
  - `test_percentile_hand_checked` (`[1, 2, 3, 4]`: q 0 → 1, 0.5 → 2.5, 1 → 4, 0.25 → 1.75).
  - `test_same_seed_same_result_and_other_seed_differs`.
  - `test_mean_block_one_is_iid_resampling` (every index independent: over 20,000 draws the share of "next index = previous + 1" is within 0.02 of `1/n`).
  - `test_mean_block_length_is_about_the_setting` (mean run length of consecutive indices with `mean_block = 10` within 10% over 50,000 steps).
  - `test_indices_wrap_circularly` and `test_length_is_n`.
  - `test_constant_series_interval_collapses_to_the_point`.
  - `test_interval_brackets_the_point_on_a_noisy_series` (seeded noise, 250 days, `lo ≤ point ≤ hi` for the three statistics).
  - `test_short_series_bootstrap_says_so` (`n = 5 < min_days`: `enough_days is False`, all intervals `None`, no exception).
  - `test_zero_variance_resample_is_dropped_not_nan`.
  - `test_results_are_finite_json` (`json.dumps(dataclasses.asdict(result), allow_nan=False)` works).
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement `bootstrap.py`.** Plain loops; `resamples` × `n` ≈ 0.5M steps for a year, so no numpy needed.
- [ ] **Step 4: Run** → PASS. Time `resamples = 2000`, `n = 1500` and record the seconds in RECON (it decides the cache in Task 6).
- [ ] **Step 5: Commit.**

```bash
git add stockify/research/bootstrap.py tests/stockify/research/test_bootstrap.py docs/plan
git commit -m "IR 6.1: stationary block bootstrap with seeded 90% intervals for Sharpe, mean return and drawdown"
```

---

### Task 6: Plain words, run and family reports, research CLI (IL 6.2) — M6.4, M6.8

**Files:**
- Create: `stockify/research/plain.py`, `stockify/research/report.py`
- Modify: `stockify/__main__.py` (new `research` command)
- Test: `tests/stockify/research/test_plain.py`, `tests/stockify/research/test_report.py`

**Interfaces:**
- Consumes: Tasks 3–5.
- Produces:
  - `plain.KEYS` (tuple) and `plain.meaning(key: str) -> str`, one line (≤ 110 characters, no Greek letters, no "ddof", "kurtosis", "skew", "logit") for each of: `sharpe, dsr, psr, expected_best_luck, trials, bootstrap_sharpe, bootstrap_mean_return, bootstrap_max_drawdown, regime_month, regime_weekday, regime_expiry, regime_vix, regime_gap, pbo, wf_ratio, wf_stability, fragile, holdout_unlocks, cost_scale, extra_ticks, latency`.
  - Sentence builders returning `str`: `dsr_sentence(n_trials: int, rank: int, dsr: float | None) -> str` (for rank 1: "best of 12 tries, after allowing for luck: 85% confident the edge is real"; for others "4th best of 12 tries …"; for `n_trials == 1`: "the only try, so nothing to allow for"; `dsr None`: "too few days to say"), `psr_sentence(psr)`, `interval_sentence(label: str, interval, unit: str)` ("between 0.4 and 1.9 in 9 of 10 re-shuffles of your own days"), `regime_sentence(bucket)`, `wf_sentence(ratio)`, `fragile_sentence(result)`, `pbo_sentence(pbo)`. The exact wording is refined with skill `design:ux-copy`; the phrases "best of N tries" and "after allowing for luck" are fixed.
  - `report.RunResearch` frozen dataclass `(run_id, family_id, role, n_trials, rank, is_best, dsr: DsrResult | None, bootstrap: BootstrapResult | None, regimes: object | None, holdout_unlocks: int, plain: dict[str, str])`; `report.run_research(run_id: str, *, use_cache: bool = True) -> RunResearch`: loads the run's NAV rows (`store.run_nav_series`), builds `r`, `x`, finds the family's trials, `V` from their per-period Sharpes, `rank` by Sharpe (ties: older first), and the cached bootstrap (and regimes once Task 8 adds them) from `runs/<id>/research.json` keyed by a SHA-256 of `(returns, settings, seed, code version constant)`; a cache miss recomputes and writes the file atomically (temp file then replace), skipped silently when the folder is gone.
  - `report.TrialRow` `(run_id, name, role, status, sharpe_per_period: float | None, net_pnl_paise: int | None, days: int, created_ns, job_id)`; `report.FamilyReport` `(family_id, description, objective, named, n_trials, var_sharpe, best_run_id, best: DsrResult | None, trials: list[TrialRow], others: list[TrialRow], sessions: list[dict], holdout_unlocks: list[dict], plain: dict[str, str])`; `report.family_report(family_id: str) -> FamilyReport`; `report.list_families() -> list[FamilySummary]` (`family_id, description, named, n_trials, n_other_runs, n_sessions, best_run_id, best_dsr: float | None, unlocks: int`).
  - CLI: `python -m stockify research families [--backfill] [--dry-run] [--name FAMILY_ID "description"]` (prints a table; `--backfill` is `registry.backfill`), `python -m stockify research show <family_id | run_id>` (prints the same lines the pages will show, in plain words).

- [ ] **Step 1: Write the failing tests.**
  - `test_every_statistic_key_has_a_one_line_meaning`; `test_meanings_have_no_jargon_symbols`.
  - `test_dsr_sentence_for_best_of_12` (contains "best of 12 tries" and "after allowing for luck"); `test_dsr_sentence_for_a_single_try`; `test_dsr_sentence_when_there_are_too_few_days`.
  - `test_run_research_for_the_best_run` (fixture family of 12 trials with known Sharpes: `n_trials == 12`, `rank == 1`, `is_best`, `dsr.sr0` equals `stats.expected_max_sharpe(12, V)`); `test_run_research_for_a_middle_run_says_its_rank`.
  - `test_one_trial_family_reports_the_only_try`.
  - `test_variants_do_not_change_n` (add five `sensitivity` runs; `n_trials` unchanged).
  - `test_cache_file_is_written_and_reused` (second call does not call `bootstrap_ci`, via a patched counter) and `test_cache_is_invalidated_when_returns_change`.
  - `test_missing_folder_computes_without_caching`.
  - `test_family_report_lists_sessions_and_unlocks_without_counting_them`.
  - `test_report_is_finite_json` (`allow_nan=False`) for a flat series, a one-day run and a failed run.
  - CLI: `test_research_show_prints_the_plain_sentence` (capture stdout).
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** `plain.py`, `report.py`, the `research` command. Skills: `design:ux-copy` on `plain.py` wording (report only, the implementer applies it).
- [ ] **Step 4: Run** `tests\run_tests.py research` → PASS.
- [ ] **Step 5: Commit.**

```bash
git add stockify/research tests/stockify/research stockify/__main__.py docs/plan
git commit -m "IL 6.2: run and family research reports with a plain-English sentence for every statistic"
```

---

### Task 7: Research API: run research, families (WF 6.1) — M6.4, M6.8

**Files:**
- Create: `server/routers/research.py`, `tests/stockify/test_api_research.py`
- Modify: `server/schemas.py`, `web/src/lib/types.ts` (blockwatch pair), `server/routers/tests.py` (add `/api/tests/{id}/research`), `server/main.py` (register the router), `docs/plan/A2_interfaces.md` §6

**Interfaces:**
- Consumes: `report.run_research`, `report.family_report`, `report.list_families`.
- Produces DTOs (appended to both files, same order): `IntervalDTO(point, lo, hi)`, `DsrDTO(sr, t, skew, kurt, n_trials, var_sharpe, sr0, dsr, psr)`, `BootstrapDTO(n_days, mean_block, resamples, ci, enough_days, sharpe, mean_daily_return, max_drawdown)`, `RunResearchDTO(run_id, family_id, role, n_trials, rank, is_best, dsr, bootstrap, regimes, holdout_unlocks, plain)` (`regimes` typed in Task 8), `TrialRowDTO`, `FamilySummaryDTO`, `FamilyDetailDTO` (`pbo` typed in Task 16). All floats are `float | None`; the response is serialised with `allow_nan=False` semantics (a test asserts it).
- Routes: `GET /api/tests/{run_id}/research` (404 for an unknown run; 200 with `dsr: null` and a sentence for a run too short to score); `GET /api/research/families?offset&limit` and `GET /api/research/families/{family_id:path}` (404 unknown). Declared with the other fixed paths first.

- [ ] **Step 1: Write the failing tests** (TestClient as in `test_api_tests.py`; Host header loopback): `test_run_research_returns_dsr_and_bootstrap`, `test_run_research_404`, `test_run_research_for_a_failed_run_is_empty_not_an_error`, `test_families_list_and_detail` (family ids containing `/`), `test_responses_have_no_nan`, `test_non_loopback_host_refused` (D-77).
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement the routes and DTOs; mirror them in `types.ts`;** `~/.tools/blockwatch/blockwatch.exe` clean. Skills: `sharp-edges`, `insecure-defaults` over the diff (report only).
- [ ] **Step 4: Run** `tests\run_tests.py api_research` and `tests\run_tests.py api_tests` → PASS.
- [ ] **Step 5: Commit.**

```bash
git add server stockify web/src/lib/types.ts tests/stockify/test_api_research.py docs/plan/A2_interfaces.md
git commit -m "WF 6.1: API for a run's deflated Sharpe and bootstrap intervals and for trial families"
```

---

### Task 8: Regime breakdown (IR 6.2) — M6.9

**Files:**
- Create: `stockify/research/regimes.py`
- Modify: `stockify/research/report.py` (add `regimes` to `RunResearch` and its cache), `server/schemas.py`, `web/src/lib/types.ts`, `stockify/research/plain.py`
- Test: `tests/stockify/research/test_regimes.py`

**Interfaces:**
- Consumes: `store.run_nav_series`, `calendar`, refdata (`instruments`, `index_eod`), `ticks`, `research.settings()["regimes"]`.
- Produces:
  - `Bucket` frozen dataclass `(label: str, days: int, pnl_paise: int, mean_pnl_paise: int | None, up_days: int, up_share: float | None, thin: bool)` (`pnl_paise` is the exact sum of daily NAV changes; `mean_pnl_paise` rounded half away from zero; `thin` when `days < thin_days`).
  - `RegimeReport` `(month: list[Bucket], weekday: list[Bucket], expiry: list[Bucket], vix: list[Bucket], gap: list[Bucket], vix_source: dict[str, str], notes: list[str])`; `regimes.report(run_id: str) -> RegimeReport`. Daily P&L of day d = `NAV_d − NAV_{d−1}` (day 1 against capital).
  - `expiry_days(start: date, end: date) -> set[date]`: dates on which an index future or option of the `index_underlyings` expires, from refdata instruments (`COALESCE(actual_expiry, expiry)`, I-6, D-41), falling back to `calendar.generated_expiries` where refdata has none; the buckets are "expiry day" and "other days".
  - `vix_close(day: date) -> tuple[int, str] | None` (value in points × 100 as stored, source `"recorded"` or `"bhavcopy"`): recorded `INDIA VIX` from the day's tick archive first, else `index_eod`; `None` when neither. The VIX bucket of a day uses the **previous trading day's** close, with edges `vix_edges` (4 buckets).
  - `opening_gap(day: date) -> int | None`: basis points of `open / previous close − 1` of `reference_index` from `index_eod`; buckets by `gap_edges_pct` (5 buckets). Days with no value go into a bucket labelled "no data" (counted, never dropped silently).
  - Weekday labels are Monday–Friday from the IST trading date; month labels `YYYY-MM`.

- [ ] **Step 1: Write the failing tests** (`SandboxedDBTest` with refdata, fixtures built through `refdata` helpers used by `tests/stockify/data`): `test_weekday_buckets_sum_to_the_run` (Σ `days` and Σ `pnl_paise` equal the totals for every dimension), `test_month_buckets_hand_checked` (a 6-day fixture with known P&L), `test_expiry_vs_other_days`, `test_vix_bucket_uses_the_previous_close` (the day after a VIX 21 close is in the top bucket even if its own close is 12), `test_vix_prefers_recorded_and_labels_the_source`, `test_vix_falls_back_to_bhavcopy`, `test_missing_vix_goes_to_no_data_not_dropped`, `test_gap_buckets_hand_checked` (±0.3% and ±0.7% gaps), `test_thin_buckets_are_flagged`, `test_run_with_one_day_has_one_bucket_each`, `test_empty_refdata_gives_no_data_buckets_and_a_note`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement `regimes.py`; wire it into `run_research`; add `RegimeBucketDTO`/`RegimeReportDTO` to both type files.** Measure `vix_close` on one real archive day (read-only) and record the seconds; if more than 5 s per day, make bhavcopy the first source, record a D-row and keep "recorded" as the fallback.
- [ ] **Step 4: Run** `tests\run_tests.py research` and `api_research` → PASS; blockwatch clean.
- [ ] **Step 5: Commit.**

```bash
git add stockify/research server/schemas.py web/src/lib/types.ts tests/stockify/research docs/plan
git commit -m "IR 6.2: performance by month, weekday, expiry day, VIX level and opening gap"
```

---

### Task 9: Locked holdout (PM 6.1) — M6.6

**Files:**
- Create: `stockify/research/holdout.py`
- Modify: `stockify/runs.py` (`queue`, `launch`), `stockify/runconfig.py` (validate `unlock_holdout`), `stockify/__main__.py` (`run --unlock-holdout`, `oracle run --unlock-holdout`), `stockify/research/report.py`, `docs/plan/A1_schemas.md` §6.1, `docs/plan/M6_reporting_research.md`
- Test: `tests/stockify/research/test_holdout.py`

**Interfaces:**
- Produces:
  - `class HoldoutLocked(ValueError)` (message names the holdout date, the run's last day, and how to unlock).
  - `holdout.holdout_from() -> date | None` (`research.settings()["holdout_from"]`).
  - `holdout.run_span(cfg: dict) -> tuple[date, date | None] | None`: the earliest `from` and the latest `to` over the pack specs (`to` absent → `None`, open-ended); for a `{"sha256"}` pack the dates of the pack's manifest (`stockify.data.packs`); `None` when no dates are known.
  - `holdout.enforce(cfg: dict) -> str | None`: returns `None` when the holdout is off or not overlapped; returns the reason when overlapped **and** `cfg["unlock_holdout"]` is a non-empty, non-whitespace string; raises `HoldoutLocked` when overlapped otherwise, and when the span is unknown while a holdout is set (fail closed). Overlap rule: `last_day ≥ holdout_from`, or open-ended.
  - `runs.queue` and `runs.launch` call `enforce` before creating a row, and log the unlock (`store.log_holdout_unlock`) when they create the row (a `launch(run_id=…)` of an already queued run does not log twice). `runconfig.validate` accepts `unlock_holdout` only as `null` or a string of at least 3 non-space characters (D-76 style message).
  - CLI: `--unlock-holdout "<reason>"` on `run` sets the config key. `report.family_report` counts and lists the family's unlocks (`FamilyReport.holdout_unlocks`); `plain.meaning("holdout_unlocks")`.

- [ ] **Step 1: Write the failing tests** (holdout set to `2026-08-01` with `isolated_env`/patched settings): `test_off_by_default_never_locks`, `test_run_ending_the_day_before_passes` (`to = 2026-07-31`), `test_run_ending_on_the_holdout_day_is_locked`, `test_range_crossing_the_line_is_locked`, `test_open_ended_pack_is_locked`, `test_sha_pack_is_checked_through_its_manifest`, `test_unknown_span_fails_closed`, `test_unlock_needs_a_real_reason` (`""`, `"  "`, `"x"` refused), `test_unlock_runs_and_is_logged_once_with_run_and_family`, `test_queue_then_launch_logs_once`, `test_unlock_count_appears_on_the_family_report`, `test_paper_sessions_are_exempt`, `test_cli_flag_sets_the_reason`, `test_api_post_with_unlock_holdout_key_works_and_without_is_422_naming_the_date`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.** Skills: `differential-review` over this diff (it guards the research rules), `sharp-edges`.
- [ ] **Step 4: Run** `tests\run_tests.py holdout`, `runconfig`, `runs`, `api_tests` → PASS.
- [ ] **Step 5: D-row** (scope, fail-closed, sessions exempt, reason in `unlock_holdout`); 🛑 owner choice of `holdout_from` recorded in RECON as pending; **Commit.**

```bash
git add stockify docs/plan tests/stockify/research
git commit -m "PM 6.1: a locked holdout: runs over it need a reason, and every unlock is logged and counted"
```

---

### Task 10: Bounded parallel run pool (PM 6.2) — shared by M6.5, M6.7, M6.10

**Files:**
- Create: `stockify/research/pool.py`
- Modify: `stockify/__main__.py` (`run --role`, `run --job-id`), `stockify/runs.py` (already takes `role`/`job_id`)
- Test: `tests/stockify/research/test_pool.py`

**Interfaces:**
- Produces:
  - `PoolJob` frozen dataclass `(config: dict, role: str = "trial", job_id: str | None = None, impl: str = "engine")`; `PoolOutcome` frozen dataclass `(run_id: str | None, status: str, error: str | None)` with `status` `done` or `failed`.
  - `default_workers(requested: int | None = None) -> int` = `min(requested or ∞, max(1, os.cpu_count() − 1))` and at least 1; `research.settings()["sweep"]["max_workers"]` caps it when set.
  - `run_all(jobs: list[PoolJob], *, workers: int | None = None, popen=subprocess.Popen, tmp_dir: Path | None = None) -> list[PoolOutcome]`: writes each config to a temp JSON (in a throw-away folder under `research_dir()`), starts `[sys.executable, "-m", "stockify", "run", "--config", file, "--role", role, "--job-id", id, "--impl", impl, "--no-report"]` with the parent's environment, at most `workers` at once, collects `stdout` (the run folder, whose name is the run id) and the exit code (0 done, 1 failed, 2 config refused: `failed` with stderr's last line as `error`), returns outcomes **in input order**, removes the temp files. A KeyboardInterrupt kills the children and re-raises.
  - CLI `run --role {trial,sensitivity,oos,rerun}` and `--job-id` are passed to `runs.launch`.

- [ ] **Step 1: Write the failing tests** with a fake `popen` (a class with `communicate`, `poll`, `returncode`, `kill`, counting concurrent live instances): `test_at_most_workers_run_at_once`, `test_results_are_in_input_order_even_when_jobs_finish_out_of_order`, `test_failed_job_is_reported_not_raised`, `test_refused_config_exit_2_has_the_message`, `test_default_workers_is_cpu_minus_one_and_at_least_one` (patch `os.cpu_count` to 1, 2, 16, `None`), `test_sweep_max_workers_setting_caps_it`, `test_temp_files_are_removed`, `test_children_inherit_the_sandbox_environment` (the env passed to `popen` has the test's `LOCALAPPDATA`), `test_keyboard_interrupt_kills_children`, and one real-process smoke: `test_two_real_subprocesses_run_a_tiny_config` (oracle impl, the fixture used by `test_runs_launch.py`; asserts both rows exist with `role`/`job_id` stored).
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement `pool.py`** with `popen` injectable and polling (`poll()` every 0.2 s; no threads needed).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit.**

```bash
git add stockify/research/pool.py stockify/__main__.py tests/stockify/research/test_pool.py docs/plan
git commit -m "PM 6.2: a bounded pool of run subprocesses for sweeps, walk-forward and sensitivity"
```

---

### Task 11: `costs.scale` in the run config (FW 6.1) — enables M6.7

**Files:**
- Modify: `stockify/runconfig.py` (`DEFAULTS["costs"]`, validation), `stockify/bundle.py` (`resolve_bundle`), `stockify/oracle/report.py` (`LABEL_TEXT`), `stockify/runs.py` (re-validation accepts the key), `docs/plan/A1_schemas.md` §6.1, `web/src/app/tests/ui.tsx` (`labelMeaning`)
- Test: `tests/stockify/test_costs_scale.py`

**Interfaces:**
- Produces: run-config key `costs.scale`, a decimal string in `["0", "10"]` (exact via `numerics`), default `"1"`, **left out of the normalised form when it equals 1** (so every existing run config, bundle and hash is unchanged, like `seed_mode`). `bundle.scale_charges(charges: dict, scale: Decimal) -> dict` multiplies every charge parameter of the dated `charges` section (percentage rates, flat amounts, per-order caps; **not** the dates, ids, `verified` flags or `source` text) exactly with `Decimal`, re-encoded as the section's own decimal strings; `resolve_bundle` applies it once and adds the manifest label `costs:scaled` when scale ≠ 1. Both engines read the bundle, so neither changes.
- Consumes: `numerics`, `loader.load_json("charges.json")`.

- [ ] **Step 1: Read `stockify/config/charges.json` and A1 §7.2** and list every numeric leaf class in the test's docstring (percentage strings, flat paise, caps). A leaf class the scaler does not know makes `scale_charges` raise, so a future new field cannot be silently skipped.
- [ ] **Step 2: Write the failing tests:** `test_scale_one_is_byte_identical_to_no_key` (bundle bytes and `determinism.json` equal), `test_scale_one_left_out_of_the_normalised_config`, `test_scale_zero_gives_zero_charges` (oracle run on a golden fixture: every `charges.csv` total is 0), `test_scale_two_doubles_each_component_before_rounding` (compare `charges.csv` rows of scale 1 and 2 on a fixture with no stamp-duty rounding differences; the rounding rule of A4 §5 still applies once), `test_scale_applies_to_flat_amounts_too`, `test_scale_refused_when_negative_above_ten_or_not_a_string` (each names `costs.scale`), `test_unknown_charge_leaf_raises`, `test_manifest_carries_the_label`, `test_engine_and_oracle_agree_with_scaled_costs` (skips when the engine is not loadable; CSVs byte-identical for scale `"1.5"`), `test_scale_changes_the_bundle_hash`.
- [ ] **Step 3: Run to verify failure.**
- [ ] **Step 4: Implement.** Skills: `sharp-edges`, `insecure-defaults`, `dimensional-analysis` (money scaling, report only).
- [ ] **Step 5: Run** `tests\run_tests.py costs_scale`, `bundle`, `runconfig`, `oracle`, `engine_parity` → PASS.
- [ ] **Step 6: D-row; Commit.**

```bash
git add stockify web/src/app/tests/ui.tsx tests/stockify/test_costs_scale.py docs/plan
git commit -m "FW 6.1: a costs.scale run-config key, applied once to the bundle's charge rows"
```

---

### Task 12: Sensitivity suite (FW 6.2) — M6.7

**Files:**
- Create: `stockify/research/sensitivity.py`
- Modify: `stockify/__main__.py` (`sensitivity` leaves `_STUBS`), `server/routers/tests.py` (`GET /api/tests/{id}/sensitivity`), `server/schemas.py`, `web/src/lib/types.ts`, `stockify/research/plain.py`
- Test: `tests/stockify/research/test_sensitivity.py`

**Interfaces:**
- Consumes: `pool.run_all`, `registry`, `stats`, `research.settings()["sensitivity"]`, `holdout.enforce` (through the runs), `store.get_run`.
- Produces:
  - `Variant` frozen dataclass `(key: str, axis: str, label: str, config: dict)` with `axis` ∈ `baseline, costs, slippage, latency, fill_model`; keys like `costs_x1.5`, `slip_plus1`, `latency_250`, `fill_snapshot_quote`.
  - `variants(cfg: dict, settings: dict | None = None) -> list[Variant]`: the stored normalised config with, one axis at a time, `costs.scale` ∈ `cost_scale`, extra ticks ∈ `extra_ticks` (`none` → `fixed_ticks k`; `fixed_ticks k0` → `k0 + extra`; `random_ticks` and `bps` bases are skipped and reported by `applicable_slippage`), `latency_ms` ∈ `latency_ms`, and every applicable `fill_model.kind`. Variants whose canonical config equals the baseline's (the `×1`, `+0`, `latency 0`, the baseline fill model) are dropped; the baseline itself is first and is **not re-run** (its numbers come from the source run). Each variant has `family_id` of the source, a name `<name>-<key>` that is a safe segment ≤ 64.
  - `applicable_fill_models(cfg: dict) -> dict[str, str | None]`: kind → `None` when A4 §4 allows it for the run's datasets (read A4 §4 and the pack dataset names; `eod_close` needs `price_basis: raw` and an end-of-day pack), else the reason in plain words.
  - `SensitivityRow` `(key, axis, label, run_id: str | None, status: str, net_pnl_paise: int | None, sharpe: float | None, delta_net_paise: int | None, error: str | None)` (`status` ∈ `baseline, done, failed, not_applicable`); `SensitivityResult` `(run_id, rows, baseline_net_paise, fragile: bool, verdict: str, reasons: list[str], plain: dict[str, str])` with `verdict` ∈ `fragile, robust, already_losing`.
  - `run_suite(run_id: str, *, workers: int | None = None, unlock_reason: str | None = None, pool_run=pool.run_all) -> SensitivityResult`: runs every variant with role `sensitivity`, writes `runs/<id>/sensitivity.json` atomically, returns the result. **Fragile** (spec) means: baseline net P&L > 0 and net P&L < 0 under `costs.scale == fragile_cost_scale` or under `fragile_extra_ticks` extra ticks; a baseline ≤ 0 is `already_losing`, not fragile. Net P&L = `nav_end_paise − nav_start_paise` (after charges). A variant that fails is a row with its error, never dropped.
  - `format_table(res: SensitivityResult) -> str`; CLI `python -m stockify sensitivity --run RUN_ID [--workers N] [--unlock-holdout R]` prints it and exits 0 (1 when any variant failed unexpectedly, 2 for an unknown or unfinished run). `GET /api/tests/{id}/sensitivity` returns the stored result or 404 "not run yet; run `python -m stockify sensitivity --run <id>`"; DTOs `SensitivityRowDTO`, `SensitivityDTO`.

- [ ] **Step 1: Write the failing tests** (stub `pool_run` returning canned outcomes with runs built by `fixtures.make_run`): `test_variants_are_one_axis_at_a_time_and_skip_the_baseline_duplicates`, `test_extra_ticks_on_a_none_slippage_run_becomes_fixed_ticks`, `test_extra_ticks_on_fixed_ticks_adds`, `test_random_ticks_base_is_reported_not_applicable`, `test_fill_models_follow_the_datasets` (bars config: only `bar`; ticks config: both snapshot kinds), `test_unapplicable_fill_model_is_listed_with_a_reason`, `test_fragile_when_costs_x1_5_turns_it_negative`, `test_fragile_when_one_extra_tick_turns_it_negative`, `test_robust_when_it_survives_both`, `test_already_losing_is_not_fragile`, `test_failed_variant_is_a_row_with_its_error`, `test_sensitivity_runs_do_not_change_n` (family `n_trials` before and after), `test_variant_names_are_safe_segments`, `test_result_is_written_atomically_and_finite_json`, `test_unknown_or_running_run_refused`, `test_holdout_locked_source_refuses_without_unlock`, `test_api_returns_404_until_run` and `test_api_returns_the_stored_result`, and an end-to-end `test_suite_on_a_real_tiny_run_with_the_oracle`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.** Skill `sharp-edges` (new CLI flags).
- [ ] **Step 4: Run** → PASS; blockwatch clean.
- [ ] **Step 5: Commit.**

```bash
git add stockify server web/src/lib/types.ts tests/stockify/research docs/plan
git commit -m "FW 6.2: sensitivity suite: costs, slippage, latency and fill models, with a fragile flag"
```

---

### Task 13: Parameter sweeps (WF 6.2) — M6.10

**Files:**
- Create: `stockify/research/sweep.py`, `examples/sweeps/orb_grid.yaml`
- Modify: `stockify/__main__.py` (`sweep` leaves `_STUBS`), `server/routers/research.py`, `server/schemas.py`, `web/src/lib/types.ts`, `docs/plan/A1_schemas.md` (new §6.5 `sweep.yaml`)
- Test: `tests/stockify/research/test_sweep.py`, `tests/stockify/test_api_research.py` (extend)

**Interfaces:**
- Produces `sweep.yaml` (schema `stockify.sweep/1`; unknown keys refused naming the key and the closest known one, as D-76):

```yaml
schema: stockify.sweep/1
name: orb_grid
base: ../runs/orb_reliance_minute.json     # a path (relative to this file) or an inline run config
family_id: null                            # default: the base config's default family (strategy + dataset + objective)
objective: sharpe                          # one of registry.OBJECTIVES
search: {kind: grid}                       # or {kind: random, trials: 12, seed: 7}
workers: null                              # capped at cpu_count - 1
axes:                                      # dotted paths into the run config; decimals are strings
  strategy.params.range_minutes: [5, 10, 15, 20, 30]
  strategy.params.exit_hhmm: ["14:30", "15:00", "15:10"]
```

- `SweepSpec` frozen dataclass `(name, base: dict, axes: dict[str, list], search: dict, family_id: str | None, objective: str, workers: int | None, source: Path | None)`; `load_spec(path: Path) -> SweepSpec` (raises `SweepSpecError(ValueError)`); `set_path(cfg: dict, dotted: str, value) -> dict` (a deep copy; a path that does not exist in `base` is refused unless it is under `strategy.params`, so `strategy.parms.x` is caught; the final config always passes `runconfig.validate`); `Trial` `(index: int, params: dict, config: dict)`; `expand(spec: SweepSpec) -> list[Trial]` (grid: Cartesian product, axes in file order, last axis fastest; random: `trials` distinct grid points drawn without replacement with a partial Fisher–Yates on `seeding.stream(seed, "random_search")`, returned in grid order; `trials ≥ grid size` gives the full grid). Trial names `<name>-t<index:03d>`.
- `SweepResult` `(job_id, name, family_id, objective, axes: dict[str, list], rows: list[dict], best_run_id: str | None)` where a row is `{index, params, run_id, status, objective_value, sharpe, net_pnl_paise, trades, max_drawdown, error}`; `run_sweep(spec, *, unlock_reason=None, workers=None, pool_run=pool.run_all, now_ns=None) -> SweepResult`: creates a `research_jobs` row (kind `sweep`, job id `<name>-<UTC stamp>-<6 hex>` made unique; the stamp is metadata only), runs every trial through the pool with role `trial` and the job id, writes `research/<job_id>/sweep.json` and `results.csv` (floats written with `repr`, none `NaN`), picks the best by `objective` (a trial whose objective is `None` is ineligible; ties → lowest index), marks the job `done`.
- CLI `python -m stockify sweep --config FILE [--workers N] [--unlock-holdout R]` prints the results table (best first) in plain headings and the job id. API (read only): `GET /api/research/jobs?kind=&offset=&limit=` and `GET /api/research/sweeps/{job_id}` returning `SweepDTO(job_id, name, family_id, objective, axes, rows, best_run_id, cells, plain)` where `cells` is, for the two axes the query chooses (`?x=&y=`, default the first two) the objective per cell, the **best** over any remaining axes (the sentence says so); one-axis sweeps return a single row of cells. DTOs mirrored in `types.ts`.

- [ ] **Step 1: Write the failing tests:** `test_grid_is_cartesian_in_axis_order`, `test_random_search_is_seeded_and_without_replacement`, `test_random_trials_at_or_above_grid_size_give_the_full_grid`, `test_unknown_key_in_sweep_yaml_refused_naming_it`, `test_set_path_typo_refused`, `test_every_expanded_config_passes_runconfig_validate`, `test_trial_names_are_safe_unique_segments`, `test_base_path_is_relative_to_the_yaml_file`, `test_failed_trial_is_listed_not_fatal`, `test_ineligible_best_and_tie_break_by_index`, `test_trials_join_the_default_family_and_count_in_n` (a 6-trial sweep makes `n_trials == 6`), `test_sweep_job_row_and_files_are_written`, `test_sweep_json_has_no_nan`, `test_holdout_locked_base_refuses_without_unlock`, `test_workers_never_exceed_cpu_minus_one`, `test_example_orb_grid_yaml_loads_and_expands_to_15` (5 × 3), `test_api_sweep_cells_for_two_axes_take_the_best_over_the_rest`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement; create `examples/sweeps/orb_grid.yaml`** with the example above, `base` pointing at `examples/runs/orb_reliance_minute.json` and `strategy.params.qty` left as that file has it. Skills: `sharp-edges` (new CLI flags and a new file format), `insecure-defaults`.
- [ ] **Step 4: Run** `tests\run_tests.py sweep`, `api_research` → PASS; blockwatch clean.
- [ ] **Step 5: Commit.**

```bash
git add stockify server web/src/lib/types.ts examples/sweeps tests/stockify docs/plan
git commit -m "WF 6.2: sweep.yaml grids and seeded random search, run in parallel, with a results table and heatmap data"
```

---

### Task 14: Walk-forward: windows, training grids, selection, test runs (WF 6.3) — M6.5

**Files:**
- Create: `stockify/research/walkforward.py`
- Test: `tests/stockify/research/test_walkforward.py`

**Interfaces:**
- Consumes: `sweep.expand`, `sweep.SweepSpec`, `pool.run_all`, `registry`, `holdout.enforce` (through the runs).
- Produces:
  - `parse_span(text: str) -> int` (`"120d"` → 120; anything but `<digits>d` raises `ValueError` saying calendar days).
  - `Window` frozen dataclass `(index: int, train_from: date, train_to: date, test_from: date, test_to: date)`; `windows(start: date, end: date, train: int, test: int, step: int) -> list[Window]`: window k starts at `start + k·step`; `train_to = train_from + train − 1 days`; `test_from = train_to + 1 day`; `test_to = test_from + test − 1 days`; a window is kept only when `test_to ≤ end`; `step < test` raises `ValueError` ("out-of-sample pieces would overlap"). Dates are inclusive, matching the pack spec's `from`/`to` (confirm in `stockify/data/packs.py` and record in RECON).
  - `window_config(cfg: dict, from_: date, to: date) -> dict`: replaces `from`/`to` in every pack spec; a config with packs named only by hash raises `WalkforwardError` ("walk-forward needs pack specs").
  - `select_best(rows: list[dict], objective: str) -> dict | None`: highest `objective_value` among `status == "done"` rows with a defined value; ties → lowest `index`; `None` when none is eligible.
  - `WindowResult` `(window: Window, train_rows: list[dict], chosen: dict | None, test_run_id: str | None, test_status: str, error: str | None)`; `WalkforwardResult` `(job_id, name, family_id, objective, train, test, step, span: tuple[str, str], windows: list[WindowResult])`.
  - `run_windows(spec: SweepSpec, *, train: int, test: int, step: int, objective: str, start: date | None = None, end: date | None = None, unlock_reason: str | None = None, workers: int | None = None, pool_run=pool.run_all, now_ns: int | None = None) -> WalkforwardResult`: span defaults to the earliest `from` and latest `to` of the base config; creates a `research_jobs` row (kind `walkforward`); **phase 1** runs all `windows × grid` training runs in one pool call (role `trial`, the job id); **phase 2** runs each window's chosen parameters on its test range (role `oos`, same family, same job id) in one pool call; a window with no eligible training run has `chosen = None` and no test run (listed, not fatal). Stitching and the report are Task 15.

- [ ] **Step 1: Write the failing tests:** `test_windows_for_a_known_span` (start 2026-01-01, end 2026-06-30, 120/30/30 → the first window train 2026-01-01..04-30, test 05-01..05-30, the second shifted 30 days, the last whose `test_to ≤ end` kept and the next dropped; list the exact dates), `test_step_smaller_than_test_refused`, `test_partial_last_window_is_dropped`, `test_zero_windows_is_a_clear_error`, `test_parse_span` (`"120d"`, refuses `"4m"`, `"120"`, `"-3d"`), `test_window_config_replaces_every_pack_range`, `test_hash_only_packs_refused`, `test_select_best_ties_by_grid_index_and_skips_none`, `test_one_pool_call_for_all_training_runs_then_one_for_tests` (stub pool counts calls and checks roles), `test_test_runs_have_role_oos_and_do_not_change_n`, `test_training_runs_all_count_as_trials` (`n_trials == windows × grid`), `test_window_without_an_eligible_candidate_is_listed`, `test_walkforward_test_window_crossing_holdout_is_locked` (the run refuses; the job is marked `failed` with the message, not left `running`), `test_job_row_is_updated_when_the_pool_raises`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: D-row** (calendar days, inclusive dates, `step ≥ test`, two-phase pooling, `N` = every training run); **Commit.**

```bash
git add stockify/research/walkforward.py tests/stockify/research/test_walkforward.py docs/plan
git commit -m "WF 6.3: walk-forward windows, parallel training grids, best-by-objective selection and out-of-sample test runs"
```

---

### Task 15: Walk-forward: stitching, in-sample vs out-of-sample, report, CLI, API (WF 6.4) — M6.5

**Files:**
- Modify: `stockify/research/walkforward.py`, `stockify/__main__.py` (`walkforward` leaves `_STUBS`), `server/routers/research.py`, `server/schemas.py`, `web/src/lib/types.ts`, `stockify/research/plain.py`
- Test: `tests/stockify/research/test_walkforward.py` (extend), `tests/stockify/test_api_research.py` (extend)

**Interfaces:**
- Produces:
  - `stitch(pieces: list[tuple[list[str], list[float]]]) -> tuple[list[str], list[float]]`: the test windows' `(dates, plain returns)` in window order, compounded into one index starting at 1.0 (`index_d = index_{d−1} · (1 + r_d)`); the capital is reinvested, no state carries between windows (each starts flat); gaps between windows are not filled (I-7).
  - `WalkforwardSummary` `(windows: int, oos_days: int, is_sharpe: float | None, oos_sharpe: float | None, ratio: float | None, chosen_modal_share: float | None, n_trials: int, var_sharpe: float, dsr: DsrResult | None)`: all Sharpe values are annualised from `stats` excess returns; `is_sharpe` = the mean over windows of the chosen training run's Sharpe; `oos_sharpe` = the stitched series' Sharpe; `ratio = oos_sharpe / is_sharpe` (`None` when `is_sharpe ≤ 0` or undefined, with a plain sentence); `chosen_modal_share` = the share of windows that chose the most common parameter set; `dsr` = `stats.deflated_sharpe` of the stitched excess returns with `n_trials` = every training run of the job and `V` = the variance of their per-period Sharpes.
  - `finish(job_id: str) -> WalkforwardReport`: reads the job's runs from `lab.sqlite` (not folders), computes the summary, writes `research/<job_id>/walkforward.json` (windows with chosen parameters and both periods' key metrics, the stitched `dates` and `index`, the summary, `plain` sentences) and `report.md` (in-sample vs out-of-sample table with the ratio and its sentence, the chosen parameters per window, the DSR sentence, an equity PNG through matplotlib's Agg backend, and a placeholder line for the sensitivity section that Task 17 fills), marks the job `done`.
  - CLI `python -m stockify walkforward --sweep FILE --train 120d --test 30d --step 30d --objective sharpe [--from D --to D] [--workers N] [--unlock-holdout R]` (defaults from `research.json` `walk_forward`; runs `run_windows` then `finish`; prints the report's summary and the folder).
  - API: `GET /api/research/walkforward/{job_id}` → `WalkforwardDTO(job_id, name, family_id, objective, train, test, step, summary, windows, stitched_dates, stitched_index, plain)`; the job list is `GET /api/research/jobs?kind=walkforward`.

- [ ] **Step 1: Write the failing tests:** `test_stitch_compounds_and_starts_at_one`, `test_stitch_keeps_window_order_and_gaps`, `test_summary_on_a_constructed_job` (fixture runs with known daily returns: hand-computed IS Sharpe, OOS Sharpe and ratio), `test_ratio_is_none_with_a_sentence_when_in_sample_is_not_positive`, `test_modal_share_hand_checked` (parameter sets A, A, B → 2/3), `test_dsr_uses_every_training_run_as_a_trial` (`n_trials == windows × grid`, `V` equals the variance of those Sharpes), `test_finish_reads_the_database_not_the_folders` (delete the run folders first), `test_report_md_has_the_two_sharpes_and_the_dsr_sentence`, `test_walkforward_json_has_no_nan`, `test_window_with_a_failed_test_run_is_listed_and_left_out_of_the_stitch`, `test_cli_walkforward_end_to_end_on_the_fixture` (a tiny oracle-driven fixture with a 2×2 grid, 3 windows), `test_api_walkforward_detail_and_404`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.** Skills: `statistical-analysis` and `statistical-power` as method checks on the summary wording (report only); `sharp-edges` on the CLI.
- [ ] **Step 4: Run** `tests\run_tests.py walkforward`, `api_research` → PASS; blockwatch clean.
- [ ] **Step 5: Commit.**

```bash
git add stockify server web/src/lib/types.ts tests/stockify docs/plan
git commit -m "WF 6.4: walk-forward stitched out-of-sample curve, in-sample vs out-of-sample report, deflated Sharpe over every training run"
```

---

### Task 16: Probability of backtest overfitting, CSCV (LF 6.1) — M6.11 (stretch)

**Files:**
- Create: `stockify/research/pbo.py`
- Modify: `stockify/research/report.py` (`FamilyReport.pbo`), `server/schemas.py`, `web/src/lib/types.ts`, `stockify/research/plain.py`
- Test: `tests/stockify/research/test_pbo.py`

**Interfaces:**
- Consumes: `registry.trials`, `stats.excess_returns`, `seeding.stream` (only for the test's noise), numpy.
- Produces:
  - `trial_matrix(family_id: str) -> TrialMatrix` frozen dataclass `(run_ids: list[str], dates: list[str], x: np.ndarray)` with `x` of shape `T × N` holding excess daily returns over the **dates common to every trial** (intersection, sorted); trials with no common date are listed in `dropped`.
  - `PboResult` frozen dataclass `(pbo: float, n_trials: int, n_days: int, slices: int, combinations: int, median_logit: float, prob_loss: float, dropped: list[str])`; `cscv(x: np.ndarray, slices: int = 16) -> PboResult`: split the `T` rows into `S` equal consecutive blocks (the remainder rows at the end are dropped, and the count reported); for each of the `C(S, S/2)` combinations (lexicographic from `itertools.combinations`) the in-sample set is the chosen blocks and the out-of-sample set is the rest; performance is the per-period Sharpe computed from per-block `(n, Σx, Σx²)` so the cost is vectorised; `n*` = the in-sample best (ties → lowest column); `ω = midrank of n* among the N out-of-sample values / (N + 1)`; `λ = ln(ω / (1 − ω))`; `PBO = share of combinations with λ ≤ 0`; `prob_loss` = share where the out-of-sample Sharpe of `n*` is below 0. No randomness. `ValueError` for odd or `< 4` slices, `N < min_trials` (`research.settings()["pbo"]`), or `T < 2·slices`.
  - `FamilyReport.pbo: PboResult | None` plus `pbo_note: str | None` (why it is absent, in plain words); `plain.pbo_sentence`; DTO `PboDTO`.

- [ ] **Step 1: Write the failing tests:** `test_combination_count_is_c_s_s_over_2` (12,870 for 16), `test_dominating_trial_gives_pbo_zero` (trial 0 beats the others by a fixed margin in every block), `test_pure_noise_pbo_is_about_one_half` (seeded noise, 320 days × 20 trials, `0.35 ≤ pbo ≤ 0.65`), `test_matches_an_independent_brute_force_reference` (a slow pure-Python loop inside the test, `S = 4`, `N = 3`, same `pbo`, `median_logit`, `prob_loss` to 1e-12), `test_permuting_trials_does_not_change_pbo` (continuous noise, no ties), `test_result_is_deterministic`, `test_odd_slices_and_short_series_and_single_trial_refused`, `test_trial_matrix_uses_common_dates_only`, `test_family_report_carries_pbo_or_a_note`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** (index arrays of shape `combinations × S/2` and numpy gathers, so 12,870 × 100 trials stays well under a second).
- [ ] **Step 4: Run** → PASS; time the 100-trial case into RECON.
- [ ] **Step 5: Commit.**

```bash
git add stockify/research server/schemas.py web/src/lib/types.ts tests/stockify/research docs/plan
git commit -m "LF 6.1: probability of backtest overfitting by combinatorially symmetric cross-validation"
```

---

### Task 17: The combined demo report (WF 6.5) — M6 demo

**Files:**
- Create: `stockify/research/demo_report.py`, `tests/stockify/research/test_demo_pipeline.py`
- Modify: `stockify/research/walkforward.py` (the sensitivity placeholder), `stockify/__main__.py` (`research report`)

**Interfaces:**
- Consumes: `walkforward.finish` output, `sensitivity.json` of a chosen run, `report.family_report`.
- Produces: `demo_report.write(job_id: str, *, sensitivity_run: str | None = None) -> Path` rewrites `research/<job_id>/report.md` with: the in-sample vs out-of-sample table and ratio sentence, the deflated Sharpe sentence with `N`, the per-window parameters and stability, the cost-sensitivity table of `sensitivity_run` (or the line "not run yet: `python -m stockify sensitivity --run <id>`"), the holdout unlock count, and the PBO sentence when the family has one. CLI `python -m stockify research report --walkforward JOB_ID [--sensitivity-run RUN_ID]`. The end-to-end test `test_demo_pipeline_on_the_fixture` runs sweep → walk-forward → sensitivity → report on the tiny oracle fixture and asserts the report contains the words "after allowing for luck", the ratio line and the sensitivity table, and that no run folder or `lab.sqlite` outside the sandbox was touched.

- [ ] **Step 1: Write the failing tests** (`test_report_without_sensitivity_says_how_to_run_it`, `test_report_with_sensitivity_has_the_table`, `test_report_is_plain_words_only` (no Greek letters, no raw status codes), plus the end-to-end test above).
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests\run_tests.py demo_pipeline` → PASS.
- [ ] **Step 5: Commit.**

```bash
git add stockify tests/stockify/research docs/plan
git commit -m "WF 6.5: one report that holds the walk-forward, the deflated Sharpe and the cost-sensitivity table"
```

---

### Task 18: Compare runs: logic, CLI, API (DMF 6.1) — M6.1

**Files:**
- Create: `stockify/reporting/__init__.py`, `stockify/reporting/compare.py`
- Modify: `stockify/__main__.py` (`compare` leaves `_STUBS`; `compare-artifacts` stays), `server/routers/tests.py` (the stub body), `server/schemas.py`, `web/src/lib/types.ts`, `docs/plan/A2_interfaces.md`
- Test: `tests/stockify/reporting/__init__.py`, `tests/stockify/reporting/test_compare.py`, `tests/stockify/test_api_tests.py` (extend)

**Interfaces:**
- Produces:
  - `MetricRow` `(key: str, label: str, values: list[float | int | None], kind: str, plain: str)` (`kind` ∈ `money, ratio, pct, count`); `DiffRow` `(field: str, values: list[str | None], differs: bool)`.
  - `RunComparison` `(run_ids: list[str], names: list[str], metrics: list[MetricRow], equity: list[dict], drawdown: list[dict], labels: dict[str, list[bool]], differing_labels: list[str], manifest_diff: list[DiffRow], notes: list[str])` where each `equity`/`drawdown` entry is `{run_id, dates, values}`; equity is **normalised to 1.0 at the run's own start** (NAV / capital), drawdown is `1 − NAV/peak` as in A4 §8. `manifest_diff` covers parameters (`strategy.params.*`, one row per differing key), `fill_model`, `margin.model`, the config hashes and bundle/pack hashes, and the engine build id; equal rows are listed with `differs = False` so the table shows what matched.
  - `compare_runs(run_ids: list[str]) -> RunComparison`: 2–6 distinct ids else `ValueError("compare 2 to 6 runs")`; an unknown run raises `KeyError`; a run that is not `done` is refused naming its status; a missing folder falls back to the database (`bundle_sha256`, `engine_build_id`, `config_json`) and adds a note "manifest not available (folder removed)". Metrics: net P&L, return, CAGR, Sharpe, Sortino, max drawdown, Calmar, trades, win rate, costs and cost share of gross P&L; each row carries a `plain` meaning from `plain.meaning`.
  - `format_table(c: RunComparison) -> str` (aligned text; a `*` marks the cells that differ); CLI `python -m stockify compare RUN_ID RUN_ID [RUN_ID…]`. `GET /api/tests/compare?ids=a,b,c` → `ComparisonDTO(run_ids, names, metrics, equity, drawdown, labels, differing_labels, manifest_diff, notes)`; 422 for a bad count, 404 for an unknown id.

- [ ] **Step 1: Write the failing tests:** `test_two_to_six_runs_only`, `test_equity_is_normalised_to_one_at_each_runs_start`, `test_runs_of_different_lengths_keep_their_own_dates`, `test_drawdown_series_hand_checked`, `test_manifest_diff_lists_only_real_differences_as_differs` (two ORB runs differing in `range_minutes`: that row differs, `fill_model` matches), `test_differing_labels_are_highlighted`, `test_missing_folder_falls_back_to_the_database_with_a_note`, `test_not_done_run_refused_naming_the_status`, `test_cli_prints_the_table` (stdout has both run names and `*` on a differing metric), `test_compare_artifacts_command_still_works`, `test_api_compare_ok_422_404`, `test_api_compare_declared_before_the_run_id_route` (`ids` is never read as a run id).
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement; replace the 501 stub body; mirror DTOs; blockwatch clean.**
- [ ] **Step 4: Run** `tests\run_tests.py compare`, `api_tests` → PASS.
- [ ] **Step 5: Commit.**

```bash
git add stockify server web/src/lib/types.ts tests/stockify docs/plan
git commit -m "DMF 6.1: compare two to six runs side by side, in the terminal and over the API"
```

---

### Task 19: Export a run as a zip (DMF 6.2) — M6.2

**Files:**
- Create: `stockify/reporting/export.py`, `tests/stockify/reporting/test_export.py`
- Modify: `stockify/__main__.py` (`export` leaves `_STUBS`), `server/routers/tests.py` (`GET /api/tests/{run_id}/export`), `tests/stockify/test_secrets.py` (U-10 extension), `docs/plan/A3_testing.md` (U-10 row)

**Interfaces:**
- Produces: `export.ALLOWED` (tuple of file names copied when present: `bundle.json, run.json, manifest.json, metrics.json, determinism.json, orders.csv, order_events.csv, fills.csv, charges.csv, positions_eod.csv, equity.csv, margin_daily.csv, report.md, equity.png`; **not** `events_log.txt` (free text from strategies), `request.json`, `runner.pid` or anything else); `export.build(run_id: str, *, with_packs: bool = False, out: Path | None = None) -> Path` writes the zip (default `paths.export_path(run_id, with_packs)`, via a temp file then replace) holding the allowed files plus a generated `summary.json` (`{"run_id", "config", "metrics", "labels", "family_id", "role", "packs": [{"sha256", "bytes", "included"}]}`, canonical JSON from `numerics.dumps_canonical`; no paths, no host name). With `with_packs` the pack files named in the bundle go in under `packs/<sha>.evt`. The zip is **byte-identical for the same run** (members sorted, fixed timestamp 1980-01-01, constant compression level). A run that is not `done`, or whose folder is gone, raises `ExportError` (CLI exit 1, API 409 / 404). The run id is checked by `paths.run_path`.
- CLI `python -m stockify export RUN_ID [--with-packs] [--out FILE]` prints the path. API `GET /api/tests/{id}/export?with_packs=false` streams the zip (`application/zip`, `Content-Disposition` with the run id only); `with_packs=true` is allowed (loopback only).

- [ ] **Step 1: Write the failing tests:** `test_zip_holds_the_allowed_files_and_summary`, `test_packs_excluded_by_default_with_hashes_listed`, `test_with_packs_includes_them`, `test_zip_is_byte_identical_for_the_same_run`, `test_events_log_and_request_json_are_never_exported`, `test_a_planted_unexpected_file_in_the_folder_is_not_exported`, `test_running_or_failed_run_refused`, `test_unknown_or_pruned_run_refused_not_a_traceback`, `test_path_traversal_ids_refused`, `test_api_download_and_404_409`, and the **secrets test** `test_export_contains_no_secret` (U-10): inside `isolated_env({"KITE_API_SECRET": "SEKRET-<random>", "KITE_API_KEY": "KEY-<random>"})` with a fake token file in the sandbox's `state_dir()` and the fake values also planted in a sibling file of the run folder and in `events_log.txt`, build an export (with and without packs) and scan every member's bytes (and the member names) for those literal values and for the case-insensitive patterns `api[_-]?secret`, `access[_-]?token`, `enctoken`, `request[_-]?token`, `Bearer\s+\S+` (a 64-hex run or pack hash is not a match by itself).
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.** Skills: `differential-review` and `insecure-defaults` over the diff (export is a data-leaving feature).
- [ ] **Step 4: Run** `tests\run_tests.py export`, `secrets`, `api_tests` → PASS.
- [ ] **Step 5: Commit.**

```bash
git add stockify server tests/stockify docs/plan
git commit -m "DMF 6.2: export a finished run as a reproducible zip that can never hold a secret"
```

---

### Task 20: Data coverage: matrix, gaps, storage, API (LCM 6.1) — M6.3

**Files:**
- Create: `stockify/reporting/coverage.py`, `tests/stockify/reporting/test_coverage.py`
- Modify: `stockify/data/quality.py` (two day-level metrics), `server/routers/data.py`, `server/schemas.py`, `web/src/lib/types.ts`, `docs/plan/A2_interfaces.md`

**Interfaces:**
- Consumes: `refdata` (`tick_days`, `nse_fetch_log`, `data_quality`), `bars.sqlite` (`bar_fetch_log`), `calendar.trading_days`, `store.latest_recorder_status`, `compact.disk_warnings`, `ticks`.
- Produces:
  - `quality.py` writes two more day-level rows (`instrument_id = 0`) when a report is made: `coverage_full_pct` and `coverage_quote_pct`, the mean of the instruments' `coverage_pct` over instruments subscribed in FULL (any `subscriptions` row with mode `full`) and over the rest; `None` when the group is empty. `FOREIGN_METRICS` handling is unchanged; no schema change, no migration, existing rows untouched.
  - `coverage.Cell` frozen dataclass `(state: str, value: float | None, detail: str | None)` with `state` ∈ `ok, partial, missing, not_reported, closed, not_applicable`; `coverage.matrix(from_: date, to: date) -> CoverageMatrix` with `datasets` (`recorder_full`, `recorder_quote`, `archive`, one per `nse_fetch_log` dataset family: F&O, equities, index closes, named from `sources.json` with plain labels), `days` (every calendar date; weekends and holidays are `closed`, never zeros), `bars` (per instrument and interval: cached range and day count, newest first, limited), `storage` (`landing_bytes, archive_bytes, refdata_bytes, lab_bytes, free_bytes, total_bytes, warnings` from `disk_warnings`), `recorder` (the latest recorder status or `None`). Recorder cells: `ok` ≥ 99%, `partial` below, `not_reported` when no quality rows with those metrics exist, `missing` when no `tick_days` row exists for a trading day. Archive cell: `landing`/`archived`/`verified` from `tick_days.status` (`landing_deleted` shown as verified). Reads only; no heavy file reads.
  - `coverage.gaps(day: date) -> list[dict]` (`start_ns, end_ns, start_ist, duration_s, reason, scope, frames`) from the day's gap table via `ticks.open_day` (404 when no recording).
  - API: `GET /api/data/coverage?from=&to=` (defaults: the last 60 calendar days; a range over 400 days is 422), `GET /api/data/gaps?date=`; DTOs `CoverageCellDTO`, `CoverageDayDTO`, `BarRangeDTO`, `StorageDTO`, `CoverageMatrixDTO`, `GapDTO`. Quality metrics for the drill-down stay on the existing `/api/data/quality`.

- [ ] **Step 1: Write the failing tests** (`SandboxedDBTest` with refdata and bars): `test_empty_lab_matrix_has_no_numbers` (all cells `missing`/`closed`, no `value`), `test_unreported_day_is_not_a_zero` (a `tick_days` row but no quality rows → `not_reported`), `test_weekends_and_holidays_are_closed`, `test_landing_only_day_shows_landing_and_a_partial_state`, `test_verified_archive_day`, `test_recorder_cells_use_the_two_new_metrics` (fixture quality rows 99.5 and 97.2), `test_quality_report_writes_the_two_day_metrics` (a small recorded fixture; `FULL` vs `QUOTE` groups), `test_quality_rerun_keeps_the_foreign_metrics`, `test_nse_dataset_cells` (`ok`, `missing`, `error` from `outcome`), `test_bar_ranges_per_instrument`, `test_storage_numbers_and_warnings`, `test_gaps_for_a_recorded_day_and_404_without`, `test_range_limits_and_bad_dates_422`, `test_matrix_is_finite_json`, `test_non_loopback_host_refused`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.** Skill `differential-review` on the `quality.py` change (it touches the recorded-data path); confirm by test that it only reads packets.
- [ ] **Step 4: Run** `tests\run_tests.py coverage`, `quality`, `api` → PASS; blockwatch clean.
- [ ] **Step 5: Record** the 🛑 owner step in RECON ("re-run `python -m stockify data report --date D` for each recorded day; derived rows only") and **Commit.**

```bash
git add stockify server web/src/lib/types.ts tests/stockify docs/plan
git commit -m "LCM 6.1: the data coverage matrix by date and dataset, with gaps, bar ranges and storage"
```

---

### Task 21: Web foundation and the run page's research panel (RCM 6.1) — M6.4, M6.7–M6.9 on screen

**Files:**
- Create: `web/src/components/charts.tsx`, `web/src/lib/research.ts`, `tests/stockify/research_check.ts`, `tests/stockify/test_research_checks.py`
- Modify: `web/src/app/tests/[id]/page.tsx`, `web/src/lib/format.ts` (only if a ₹ or percent helper is missing), `web/src/components/AppNav.tsx` (Research, Data links)

**Interfaces:**
- Consumes: `GET /api/tests/{id}/research`, `GET /api/tests/{id}/sensitivity` (404 allowed).
- Produces:
  - `charts.tsx` (hand-written SVG, no library; tokens only): `LineChart({series: {label: string; points: [number, number][] }[]; yFormat; baseline?})`, `IntervalBar({label, point, lo, hi, format})`, `HeatGrid({xs, ys, cells, format, best})`, each with an `aria-label` summary and a visible table fallback.
  - `research.ts`: `getRunResearch(id)`, `getSensitivity(id)`, `getFamilies()`, … fetchers with the existing `api.ts` conventions; pure helpers `heatColourStep(value, min, max, steps)`, `bucketTone(bucket)`, `sentenceOrDash(s)`.
  - The run page gains a "How much to trust this result" panel: one line `best of N tries · deflated Sharpe 85%` plus its `plain.dsr`; three `IntervalBar` rows (Sharpe, average day, worst drop) each with its plain sentence; the regime tables (month, weekday, expiry, VIX, gap) with thin buckets marked "few days"; the sensitivity table (or "not run yet" with the command); the holdout-unlock count when above zero. All four states per region; every number is shown beside its API `plain` sentence.

- [ ] **Step 1: Read** `web/node_modules/next/dist/docs/` for server components and `searchParams`; read `web/design.md`; invoke the **Hallmark** skill (app-screen scope, `design.md` present, so the system is locked) and follow it; name the files to be touched first.
- [ ] **Step 2: Write the failing checks** in `research_check.ts` run by `test_research_checks.py` (same pattern as `test_param_checks.py`, skipped with a clear message when `node` is missing): `heatColourStep` hand-checked, `bucketTone`, `sentenceOrDash(null)`.
- [ ] **Step 3: Implement** the helpers, charts and the panel.
- [ ] **Step 4: Verify.** `cd web; npm run lint; npm run build` pass; `node ~/.claude/skills/hallmark/scripts/sloplint.mjs web/src/app/tests/[id]/page.tsx web/src/components/charts.tsx --scope app` has no FAIL (no `--render`); run skills `design:design-critique`, `design:accessibility-review`, `design:ux-copy` over the files and apply their findings. Start the API and web on loopback against a sandbox lab seeded by the Task 3 fixtures and fetch the route with `curl` to confirm it renders the sentences (visual check at 1024 px is the owner's, 🛑).
- [ ] **Step 5: Commit.**

```bash
git add web tests/stockify/research_check.ts tests/stockify/test_research_checks.py docs/plan
git commit -m "RCM 6.1: the run page says how much to trust the result: tries, deflated Sharpe, intervals, regimes, sensitivity"
```

---

### Task 22: `/tests/compare` page (RCM 6.2) — M6.1

**Files:**
- Create: `web/src/app/tests/compare/page.tsx`
- Modify: `web/src/lib/research.ts` (`getComparison`), `web/src/app/tests/page.tsx` (select 2–6 rows, a "Compare" action)

**Interfaces:**
- Consumes: `GET /api/tests/compare?ids=`.
- Produces: the page reads `?ids=a,b,c` (Promise `searchParams`); metrics table (runs as columns, a `plain` line per metric row, differing cells marked with the sign or word as well as colour), the normalised equity `LineChart` and drawdown `LineChart`, labels with differences highlighted, the manifest diff table (differences first, matches collapsed under a "what matched" disclosure). Wrong counts show an `Empty` with what to do; an unknown id an error `Panel`. The list page gets checkboxes and a "Compare (2–6)" button that links to the page.

- [ ] **Step 1: Read** the Next docs again for linking with query strings; invoke **Hallmark** (app-screen); name the files first.
- [ ] **Step 2: Add a failing check** to `research_check.ts` (`compareHref(ids)` encodes and limits to six) and implement it.
- [ ] **Step 3: Implement** the page and the list changes.
- [ ] **Step 4: Verify:** lint, build, sloplint (static), the three design skills, `curl` of `/tests/compare?ids=…` against the sandbox API.
- [ ] **Step 5: Commit.**

```bash
git add web docs/plan tests/stockify/research_check.ts
git commit -m "RCM 6.2: compare runs on one page: metrics, equity, drawdown, labels and what differs in the setup"
```

---

### Task 23: `/data` coverage page (RCM 6.3) — M6.3

**Files:**
- Create: `web/src/app/data/page.tsx`, `web/src/lib/data.ts`
- Modify: `web/src/components/AppNav.tsx` (if not done in Task 21), `tests/stockify/research_check.ts`

**Interfaces:**
- Consumes: `GET /api/data/coverage`, `GET /api/data/gaps`, `GET /api/data/quality`, `GET /api/data/recorder`.
- Produces: the page with the live recorder strip at the top (connected, packets per second, last packet age, in words); the date × dataset calendar grid (rows are dates, newest first; each cell a `Chip` with a word and a figure, never colour alone; `closed` days dimmed; `not reported` says how to get it); a range selector (30, 60, 180 days via `searchParams`); clicking a day opens its drill-down (a `searchParams` day, server-rendered): gaps table (start IST, length, reason in plain words, scope), the day's worst-coverage instruments from `/api/data/quality`, the NSE files for that day; a "Bars" section (instrument, interval, from, to); a "Storage" section (used, free, warnings). Pure helper `cellWords(cell)` ("98.4% covered", "no recording", "not reported yet", "market closed") checked in `research_check.ts`.

- [ ] **Step 1: Read** the Next docs; invoke **Hallmark** (app-screen); name the files first.
- [ ] **Step 2: Failing checks** for `cellWords` and `gapReasonWords` (every reason in A1's `gap-reasons` block has a plain word, and an unknown reason falls back to a readable "other: x"); implement.
- [ ] **Step 3: Implement** the page and fetchers.
- [ ] **Step 4: Verify:** lint, build, sloplint (static), design skills, `curl /data` and `/data?day=…` against a sandbox API; an empty lab renders the empty state.
- [ ] **Step 5: Commit.**

```bash
git add web docs/plan tests/stockify/research_check.ts
git commit -m "RCM 6.3: the /data page: recording, archive and NSE-file coverage by day, with gaps and storage"
```

---

### Task 24: `/research` pages: families, sweep heatmap, walk-forward report (RCM 6.4) — M6.4, M6.5, M6.10, M6.11

**Files:**
- Create: `web/src/app/research/page.tsx`, `web/src/app/research/family/page.tsx`, `web/src/app/research/sweeps/[id]/page.tsx`, `web/src/app/research/walkforward/[id]/page.tsx`
- Modify: `web/src/lib/research.ts`, `web/src/components/AppNav.tsx`, `tests/stockify/research_check.ts`

**Interfaces:**
- Consumes: `GET /api/research/families`, `/families/{id:path}`, `/jobs`, `/sweeps/{id}?x=&y=`, `/walkforward/{id}`.
- Produces: `/research` (families table: name, tries, best deflated Sharpe in plain words, unlock count, links; sweeps and walk-forward jobs below); `/research/family?id=<family>` (trial table with the best marked, `plain.trials`, the holdout unlock log, the PBO sentence or its note, sessions listed apart from tries); `/research/sweeps/[id]` (the two-parameter `HeatGrid` with axis pickers via `searchParams`, the best cell marked and the "best over the other settings" sentence, the results table); `/research/walkforward/[id]` (in-sample vs out-of-sample with the ratio sentence, the stitched curve, the per-window parameters and a stability line, the deflated Sharpe sentence, the same report the CLI wrote). Money in ₹; no Greek letters; every statistic with its sentence.

- [ ] **Step 1: Read** the Next docs for catch-all and dynamic segments; invoke **Hallmark** (app-screen); name the files first (no route is deleted).
- [ ] **Step 2: Failing checks:** `heatCells(rows, xKey, yKey, objective)` takes the best over the other axes and marks the best cell; `familyHref(id)` encodes `/`.
- [ ] **Step 3: Implement** the four pages.
- [ ] **Step 4: Verify:** lint, build, sloplint (static), design skills, `curl` each route against a sandbox API holding the Task 17 fixture job.
- [ ] **Step 5: Commit.**

```bash
git add web docs/plan tests/stockify/research_check.ts
git commit -m "RCM 6.4: research pages: trial families, the sweep heatmap and the walk-forward report"
```

---

### Task 25: Milestone close (XI 6.1)

**Files:**
- Modify: `docs/plan/M6_reporting_research.md` (boxes), `docs/plan/00_overview.md` (§4 status, §5 D-rows if any were deferred), `docs/plan/A1_schemas.md`, `docs/plan/A2_interfaces.md` (every new endpoint), `docs/plan/A3_testing.md` (rows: DSR hand example, bootstrap determinism, holdout boundary, export secrets, PBO reference), `docs/plan/RECON.md`

**Interfaces:**
- Produces: M6 marked "built (open: live gates)" in §4 with each 🛑 listed; the RECON closing section with timings measured (bootstrap, VIX read, PBO), results of every verification command, and the owner list.

- [ ] **Step 1: Full suite** in the background: `.\.venv\Scripts\python.exe tests\run_tests.py` exits 0 (build the engine first if it exits 3). `cd web; npm run lint; npm run build` pass. `~/.tools/blockwatch/blockwatch.exe` clean. `git diff BASE -- requirements.txt requirements-lock.txt` is empty (no new package).
- [ ] **Step 2: Run the spec's verification** on the fixture lab (sandbox): `sweep`, `walkforward`, `sensitivity`, `research report`, `compare`, `export`; keep the transcript in RECON. Note which commands need the owner's real data (minute bars) and mark them 🛑.
- [ ] **Step 3: Milestone audits** (CLAUDE.md): skill `spec-to-code-compliance` over `docs/plan/M6_reporting_research.md` and this plan; `differential-review` over the branch; `insecure-defaults` over new endpoints; `supply-chain-risk-auditor` is skipped with the empty dependency diff recorded. Fix findings in rounds; reports go under `.superpowers/audits/`.
- [ ] **Step 4: Update the docs** and the RECON list of owner steps: set `holdout_from`; `research families --backfill --dry-run` then for real; `data report --date D` per recorded day; fetch minute bars; run the demo and read the report.
- [ ] **Step 5: Commit** (no "push" until the owner says so).

```bash
git add docs/plan .superpowers/audits
git commit -m "XI 6.1: M6 built: reporting and research hygiene, with the live gates listed"
```

---

### Task 26: Project write-up generator and draft (OM 6.1) — M6.12, 🛑 gated

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

1. Choose `holdout_from` in `stockify/config/research.json` (Task 9).
2. Run `python -m stockify research families --backfill --dry-run`, then without `--dry-run`; name families if wanted (Task 3, 6).
3. Re-run `python -m stockify data report --date D` for each recorded day so `/data` has its two new metrics (Task 20).
4. Fetch about 330 calendar days of RELIANCE minute bars with `bars fetch` (needs the Kite login) so the demo sweep and walk-forward have data (Task 13, 15).
5. Run the demo, read `research/<job_id>/report.md` and the `/research` pages at 1024 px (Definition of done).
6. Approve every public sentence of the write-up after the live days exist, and say "push" (Task 26).

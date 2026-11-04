# M0 — Remove the analysis pipeline, set up the toolchain

> **Goal**: a smaller, green codebase with a `stockify` Python package and a C++20 engine skeleton that builds on Windows (MSVC; Windows only, D-26) and imports into Python.
> **Depends on**: nothing. **Unblocks**: everything. **Size**: M.
> **Demo**: `python -c "import stockify._engine as e; print(e.build_info())"` prints the engine build id, and the existing app still starts.

---

## M0.0 Recon (S)

- [x] Map the repo (top three levels) and locate every item the plan relies on. Record actual paths, function names and line numbers in `docs/plan/RECON.md` under a dated heading:
  - `kite_client.py`: `refresh_instruments` (plan hint `:279`), `get_instrument_token` (`:445`), `quote` (`:500`), `historical` (`:513`), `is_authenticated`, `get_client`, the `KiteAuthError` hierarchy.
  - `kite_ticker_worker.py`: `_Supervisor` reconnect/re-subscribe logic (`:67–225`); `ticker_bridge` and the `useTicks` hook.
  - SQLite store template in `geo_alerts.py` (`:56–78`: singleton connection, WAL, `check_same_thread=False`, Row factory).
  - Separate-process template: `geo_monitor/worker.py` (`run_forever`, `load_env`), `geo_monitor/__main__.py`, `scripts/run_geo_monitor.ps1`, child supervision in `run.ps1` (`:164–188`).
  - `stockify_paths.state_dir()`.
  - Tests: `tests/run_tests.py`, `tests/support.py` (`SandboxedDBTest`, `isolated_env`).
  - API/UI: `server/main.py`, `server/schemas.py`, `web/src/lib/types.ts`, `web/src/lib/api.ts`, `web/src/app/portfolio/page.tsx`, `HoldingsList.tsx`, `globals.css`.
- [x] Find **where and how the Kite access token is stored** after login, and the login callback route. The daemon (M5) must read the same token without copying it (I-10).
- [x] Build the import graph for the deletion list (next task): every importer of every module to be deleted, including whether `geo_monitor/` imports anything from `server/services/data.py`.
- [x] Check whether any **kept** module (`geo_monitor/`, `gnews/`, alerts, search) imports `anthropic` or `playwright`. 🛑 If one does, stop: removing the dependency would break a feature the owner keeps.
- [x] Note tracked defect #1 (`candles_since`) and where it lives.
- [x] 🛑 If any referenced item is missing or structured differently enough to change a later milestone, stop and report before deleting anything.

## M0.1 Remove the analysis pipeline (M)

- [x] Move `_fetch_candles_kite` and `_INTERVAL_TO_KITE` from `technical_analyst.py` into `stockify/data/history.py` as private helpers (M1.4 builds on them). Keep their tests if any.
- [x] If `geo_monitor/` imports from `server/services/data.py`, move that helper into `geo_monitor/` first and update the import.
- [x] Delete backend: `server/routers/analysis.py`, `server/routers/history.py`, `server/services/{analysis_pipeline,prompts,stream_claude,_position}.py`, the pipeline parts of `server/services/data.py`, `financial_analyst.py`, `geopolitical_risk_agent.py`, `stock_agent.py`, `analysis_history.py`, `technical_analyst.py`.
- [x] Delete frontend: `web/src/app/analyze/`, `web/src/app/history/`, `StageTabs`, `StagePane`, `stage-views`, `sse.ts`, `analyze-types.ts`, `json-helpers.ts`, and navigation links to them.
- [x] Delete the matching tests. Remove the `anthropic` dependency. Remove `playwright` only if nothing kept uses it (🛑 if kept end-to-end tests depend on it).
- [x] Keep: `geo_monitor/`, `geo_alerts.py`, `gnews/`, `server/routers/alerts.py`, `server/routers/search.py`, Kite auth, instruments cache, ticker, portfolio, search.
- [x] Verify:
  ```
  python -c "import server.main"
  .\.venv\Scripts\python.exe tests\run_tests.py        # Windows
  cd web; npm run lint; npm run build
  git grep -nE "analysis_pipeline|stream_claude|technical_analyst|anthropic" -- . ':!docs/'   # must be empty
  ```

## M0.2 Python package skeleton (S)

- [x] Create `stockify/` with `__init__.py`, `__main__.py` (argparse, matching the repo's existing CLI style; subcommand stubs: `refdata`, `nse`, `bars`, `daemon`, `compact`, `pack`, `data`, `oracle`, `run`, `golden`, `compare-artifacts`, `compare`, `export`, `parity`, `margin`, `sweep`, `walkforward`, `sensitivity`, `tools`), `paths.py`, `log.py`.
- [x] Add Python dependencies to the repo's requirements: `numpy`, `pandas`, `pyarrow`, `matplotlib`, `PyYAML`, and `pybind11` (dev). Pin versions the way the repo already does.
- [x] `stockify/numerics.py` (A4 §0, §9.2): exact decimal-string → paise/ppb/ppm/bp conversion (`Decimal`, failing on non-integers), `round_half_up_div`, `floor_div`/`ceil_div`, `floor_to`/`ceil_to`, the canonical float formatter and the canonical JSON writer. Everything that touches money or rates uses it.
- [x] `stockify/store.py`: the only access layer for `lab.sqlite` (A1 §4), following the `geo_alerts.py` pattern; tests use `SandboxedDBTest` with `db_modules = (stockify.store, …)`.
- [x] `stockify/paths.py`: `lab_dir() = stockify_paths.state_dir() / "lab"` and helpers for every store in `00_overview.md` §3 (create directories lazily). No other module builds paths by hand.
- [x] `stockify/config/`: empty-but-valid versions of `universe.json`, `charges.json`, `sessions.json`, `contract_rules.json`, `broker_policy.json`, `margin_params.json`, `pricing.json`, `schedule.json`, `storage.json`, `sources.json`, `kite_limits.json`, `notify.json`, `research.json`, `calendar/nse_holidays.csv`, `calendar/special_sessions.csv`. Schemas in `A1_schemas.md` §7; content is filled in M1.2 and later.
- [x] `stockify/config/loader.py`: load + validate each file against its schema (use `jsonschema` if already a dependency, otherwise hand-written checks); every dated table is sorted by `effective_from`; required row fields (`source`, `verified`, and `verified_on` when verified) are enforced; the `"VERIFY"` rule of A1 §7.2 is applied; rates convert exactly through `stockify.numerics`.
- [x] `tests/stockify/__init__.py`, `tests/stockify/test_paths.py`, `tests/stockify/test_config_loader.py` (uses `isolated_env`).

## M0.3 C++ toolchain (M)

> ⚠ **Status 2026-11-02**: every file below is written, but the engine has **not been compiled yet**. The dev machine's Visual Studio Build Tools 2026 has no C++ workload and there is no CMake (see `RECON.md`). Install them, then run `.\run.ps1 -BuildEngine`. M0 is done only when that passes.

- [x] `engine/CMakeLists.txt` (CMake ≥ 3.25, C++20) and `engine/CMakePresets.json` with configure, **build and test** presets named `win-msvc-debug`, `win-msvc-release` (so `cmake --build --preset <name>` and `ctest --preset <name>` work).
- [x] Flags (D-26, C-9): MSVC `/W4 /permissive- /utf-8 /fp:precise /EHsc`, plus `/WX` (option `STOCKSIM_WARNINGS_AS_ERRORS`, on by default). Never `/fp:fast`.
- [x] Vendor single headers in `engine/third_party/` with pinned versions recorded in `engine/third_party/VERSIONS.md`: `doctest.h`, `nlohmann/json.hpp` (for reading bundles only), `picosha2.h`. **No SQLite in C++** (D-04, I-11).
- [x] Targets:
  - `stocksim_core` (static library): for now `build_info.cpp` only.
  - `stocksim_run` (executable): `--version` prints build info.
  - `_engine` (pybind11 module): exposes `build_info() -> dict`; post-build step copies it into `stockify/` (`_engine.*.pyd`).
  - `stocksim_tests` (doctest): one passing test.
- [x] pybind11 comes from the venv: configure with `-Dpybind11_DIR=$(python -m pybind11 --cmakedir)` and `-DPython_EXECUTABLE=<venv python>`. Add `pybind11` to the Python dev requirements.
- [x] Build info (I-8): `configure_file(build_info.hpp.in)` embeds git short SHA, dirty flag, compiler id and version, build type, C++ standard, a hash of the compile flags, and the Python version the module was built against. `engine_build_id` = first 12 hex chars of SHA-256 over those fields.
- [x] `stockify/engine.py` imports `_engine` and checks the embedded Python version against `sys.version_info`; on mismatch it raises with "rebuild with `run.ps1 -BuildEngine`" instead of crashing.
- [x] `run.ps1 -BuildEngine`: check `cmake --version` ≥ 3.25; find MSVC with `vswhere` (`-latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64`); check the venv and `pybind11`; configure + build `win-msvc-release`; copy the module; run `ctest --test-dir engine/build/win-msvc-release -C Release`; print `build_info()`. Each missing prerequisite gives a one-line fix ("Install Visual Studio 2022 Build Tools with the C++ workload").
- ~~`scripts/build_engine.sh` does the same on Linux~~ — dropped (D-26).
- [x] The normal `run.ps1` start warns (does not fail) when the engine module is missing or older than `engine/` sources.
- [x] `.gitignore`: `engine/build*/`, `stockify/_engine*.pyd`. Runtime data never lives in the repo.

## M0.4 Process supervision hooks (S)

- [x] `run.ps1 -Daemon` and `-DaemonOnly`, copying the `-GeoMonitor` supervision (restart with backoff, log file under the state dir). The daemon entry `python -m stockify daemon` is a stub that logs "idle" every minute until M1.5.
- ~~`deploy/systemd/stockify-daemon.service`~~ — dropped (D-26).

## M0.5 Continuous integration — dropped (D-26: Windows only, no hosted CI)

- ~~`.github/workflows/stockify.yml`~~ (dropped): matrix `windows-latest` (MSVC) and `ubuntu-latest` (GCC ≥ 12): configure, build, `ctest`, offline Python tests; `ubuntu-latest` also runs `npm run lint` and `npm run build`. No Kite credentials in CI, ever (I-10).

## M0.6 Rewrite `CLAUDE.md` (S) — local, untracked file (D-29)

- [x] Replace the analysis-pipeline sections with the following, keeping any still-true rules from the old file (notably "ticker listeners must be cheap"):

```markdown
# STOCKIFY — notes for Claude

STOCKIFY is an NSE simulation lab: Kite data in, deterministic C++ engine, Python strategies,
paper trading. The full plan is docs/plan/ (start with README.md). Follow its execution
protocol, invariants (00_overview.md §2) and STOP gates.

## Layout
- server/        FastAPI (auth, search, portfolio, /api/tests, /api/trade, /api/data)
- web/           Next.js UI (mobile-first; types mirrored in web/src/lib/types.ts)
- stockify/        Python: data layer, daemon, runners, oracle, configs (stockify/config/)
- engine/        C++20 core (stocksim_core), pybind module stockify._engine, stocksim_run CLI
- geo_monitor/   standalone side process (unchanged)

## Commands
- Build engine: .\run.ps1 -BuildEngine   (Windows only)
- Engine tests: ctest --test-dir engine/build/<preset> -C Release
- Python tests: .\.venv\Scripts\python.exe tests\run_tests.py
- Web: cd web; npm run lint; npm run build
- CLI: python -m stockify --help

## Rules
- No hard-coded market rules: dated config only (stockify/config/), cite docs/plan/01_facts.md IDs.
- Engine is deterministic: no wall clock, no std:: random distributions, no unordered iteration in outputs.
- Money = int64 paise, quantity = units, time = UTC epoch ns. IST only at the edges.
- C++ never touches SQLite or the network; Python builds bundles and event packs.
- Never log or persist Kite secrets/tokens outside the existing token store.
- Ticker listeners must be cheap: enqueue raw frames, do work on another thread.
- Recorded tick data is irreplaceable: migrations only, never drops.
```

## Verification (all must pass)

```
python -c "import server.main"
python -c "import stockify, stockify.engine; print(stockify.engine.build_info())"
.\.venv\Scripts\python.exe tests\run_tests.py
ctest --test-dir engine/build/win-msvc-release -C Release
cd web; npm run lint; npm run build
```

## Definition of done

Every box ticked; the commands above pass on Windows; `RECON.md` exists; `CLAUDE.md` rewritten; `00_overview.md` §4 status updated.

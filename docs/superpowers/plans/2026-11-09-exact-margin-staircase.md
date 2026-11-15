# Exact Margins and the Margin Staircase — Implementation Plan
> Historical plan, kept as written. The platform is built and merged into `main`; what it really looks like now is in `docs/plan/RECON.md` ("2026-11-15 state") and `docs/plan/00_overview.md`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Archive NSE's daily SPAN and ELM files, compute the exchange's exact SPAN margin from them (oracle, then C++ engine, exact parity), and make every backtest use the most accurate margin source available through a measured staircase (B1 exact SPAN + NSE ELM → B2 exact SPAN + rule ELM → B3 calibrated SPAN-lite → B4 placeholder SPAN-lite → B5 flat), recording which step was used.

**Architecture:** Python fetches and archives the raw NSE files and resolves, per run day, the SPAN parameters in force (no lookahead) for only the run's instruments into the bundle; the oracle (`stockify/fno/span_exact.py`) and the engine (`engine/src/margin/span_exact.cpp`) compute exact SPAN from the bundle behind the existing `MarginModel` interface, and a `staircase` model chooses the step per underlying per check. Validation against the 400 stored Kite answers of 2026-11-09 gates the C++ work.

**Tech Stack:** Python 3.12 (stdlib `xml.etree.ElementTree.iterparse`, `zipfile`, `gzip`), C++20 (MSVC, existing engine), pybind11 bindings, unittest, existing `stockify.data.nse_files.Http`.

**Spec:** `docs/superpowers/specs/2026-11-09-exact-margin-staircase-design.md` (read it; this plan argues from it).

## Global Constraints

- Windows only (D-26). No new third-party dependencies (stdlib XML parsing; no marginism or other installs).
- Engine deterministic: no wall clock, no unordered iteration in outputs, `/fp:precise`. C++ never fetches files, never touches SQLite or the network; it reads SPAN parameters only from the bundle.
- Money = int64 paise; conversion of SPAN rupee values to paise exact per A4 §0.3; requirement rounded **up** to the whole rupee per underlying, as SPAN-lite does.
- All on-disk paths through `stockify.paths`. lab.sqlite: migrations only, never drop. Recorded tick data untouched.
- Oracle first, then engine; byte-identical artifacts on both (DT-02); P-04 fuzz must keep agreeing.
- Tests offline and credential-free; real-file tests use a trimmed fixture checked into `tests/fixtures/span/`.
- Kite: read-only endpoints only; no Kite call from any test; tokens never logged.
- No lookahead: a backtest at time t never uses a SPAN file whose `<created>` is after t; day D's `.s` file never prices D.
- Commit names: roster positions per `docs/plan/00_overview.md` §5a (MEZ for SPAN/margin model code, CAM for archive/data, RD for parity, WP for UI, ENG for docs); never stage CLAUDE.md; no co-author/tool names in messages.
- Every task's gate: targeted tests + `.\run.ps1 -BuildEngine` when C++ changed + full suite (`.\.venv\Scripts\python.exe tests\run_tests.py`, background) green before the next task.

## Review Focus

1. **Lookahead** — a run on day D at 10:00 must use D's `i1` (created the evening before) and never D's `.s`; with no D files, the previous day's `.s`. Test owned by Task 5.
2. **Contract missing from the in-force file** (a strike listed during D) — that whole underlying steps down (not mixed), and the step is recorded. Test owned by Task 6.
3. **Expiry-day double count** — NSE's ELM % already includes the expiry-day uplift (9 Nov file: 4/5 % vs 2/3 %); B1 must not add STOCKIFY's X-9 add-on, B2 must. Test owned by Task 6.
4. **A bad download archived as data** — NSE returns HTML block/404 pages with status 200 sometimes; a non-zip / non-gzip body must never be archived or parsed. Test owned by Task 1.
5. **Units and scaling of the risk arrays** — risk-array values are in rupees per unit of the underlying contract (verify against the file and Kite); positions are in units; the result is rupees → paise. A per-lot vs per-unit slip makes every margin wrong by the lot size. Pinned by Task 3's hand-computed test and Task 4's Kite comparison (use the dimensional-analysis skill on the calculation).

---

### Task 1: Archive NSE SPAN and ELM files (CAM)

**Files:**
- Create: `stockify/data/nse_margin_files.py`
- Modify: `stockify/paths.py` (add `nse_margin_dir(kind)`), `stockify/__main__.py` (add `nse margin-files` subcommand), `stockify/daemon/__init__.py` + `stockify/daemon/scheduler.py` + `stockify/config/schedule.json` (new job `margin_files`, catch-up like `nse_fetch`)
- Test: `tests/stockify/data/test_nse_margin_files.py`

**Interfaces:**
- Produces:
  - `paths.nse_margin_dir(kind: str) -> Path` → `$LAB/nse/margin/<kind>` (`kind` ∈ `span`, `elm`).
  - `SPAN_SUFFIXES = ("i1", "i2", "i3", "i4", "s")`; `span_url(day: date, suffix: str) -> str` = `https://nsearchives.nseindia.com/archives/nsccl/span/nsccl.{YYYYMMDD}.{suffix}.zip`.
  - `elm_request(day: date) -> str` = the reports-API URL for `[{"name":"F&O-Contract wise ELM file","type":"archives","category":"derivatives","section":"equity"}]`, `date=DD-Mon-YYYY`, `type=equity&mode=single` (warm-up GET of `https://www.nseindia.com/all-reports-derivatives` first).
  - `fetch_day(day: date, http: Http | None = None) -> DayResult` — fetches all SPAN suffixes and the ELM file for `day`; stores each valid file byte-for-byte as `<nse_margin_dir>/span/<YYYY>/nsccl.<YYYYMMDD>.<suffix>.zip` and `<nse_margin_dir>/elm/<YYYY>/F_AEL_OPT_CONTRACTS_<DDMMYYYY>.csv.gz`; writes/updates `<nse_margin_dir>/manifest/<YYYYMMDD>.json` = `{file: {url, fetched_ns, sha256, bytes, created (SPAN <created> as "YYYYMMDDHHMM" or null for ELM)}}`. Idempotent (a file already archived with the same SHA-256 is skipped).
  - `DayResult` dataclass: `day: date, stored: list[str], missing: list[str], rejected: list[str]`.
  - `backfill(start: date, end: date, http=None, *, suffixes=("s",) ) -> list[DayResult]` — resumable (skips days whose files are archived), throttled by `Http`.
  - `in_force_files(day: date) -> list[tuple[str, int]]` — archived SPAN files for `day` as `(path, created_ns)` sorted by `created_ns` (Task 5 consumes).
  - `previous_eod(day: date) -> tuple[str, int] | None` — the latest archived `.s` file of a trading day before `day` (Task 5 consumes).
- CLI: `python -m stockify nse margin-files --date YYYY-MM-DD` | `--backfill FROM TO` | `--elm-window` (try every trading day back 120 days, store the ELM files that exist).

- [ ] **Step 1: Write failing tests** with a fake `Http` returning canned bodies:
  - `test_fetch_day_stores_zip_and_gz_and_manifest` — zip body for `i1`/`s`, gzip body for ELM → files at the paths above, manifest has sha256 and `created` read from a tiny zipped `.spn` (`<created>202611082026</created>`).
  - `test_html_body_with_status_200_is_rejected` — body `b"<!DOCTYPE html>..."` for `i2` → in `DayResult.rejected`, nothing written (Review Focus 4).
  - `test_404_is_missing_not_error` and `test_refetch_same_bytes_is_idempotent`.
  - `test_in_force_files_sorted_by_created` and `test_previous_eod_skips_non_trading_days` (use `stockify.data.calendar`).
- [ ] **Step 2: Run** `.\.venv\Scripts\python.exe tests\run_tests.py test_nse_margin_files` → FAIL (module missing).
- [ ] **Step 3: Implement** the module, paths helper, CLI and the daemon job `margin_files` at `"17:20"` IST with `retry_every_min: 30, until: "22:00"`, in `CATCH_UP`; the job calls `fetch_day(day)`.
- [ ] **Step 4: Run** the tests → PASS; then `.\.venv\Scripts\python.exe -m stockify nse margin-files --date 2026-11-09`, `--date 2026-11-06`, `--date 2026-11-05`, and `--elm-window` against the real NSE (operational; real lab write is the archive this task exists for). Expected: 2026-11-09 stores i1, i2, i3 (+ i4, s once published) and the ELM file; `--elm-window` reports how many ELM days exist.
- [ ] **Step 5: Full suite**, then commit `CAM 4.2: archive NSE's SPAN and ELM margin files daily`.

### Task 2: SPAN 4.00 parser and the trimmed fixture (MEZ)

**Files:**
- Create: `stockify/fno/span_file.py`, `tests/fixtures/span/make_fixture.py` (one-off, documents how the fixture was cut), `tests/fixtures/span/nsccl.20261109.i1.trim.spn` (real values, NIFTY + BANKNIFTY futures and ~40 options around the money for 17-Nov / 23-Nov expiries, all parameter blocks those need)
- Test: `tests/stockify/test_span_file.py`

**Interfaces:**
- Produces:
  - `parse_span(path: str | Path, underlyings: set[str] | None = None) -> SpanFile` — streaming `iterparse` over the zipped or plain `.spn`; keeps only the named combined commodities when given.
  - `SpanFile` dataclass: `created: str` (YYYYMMDDHHMM), `commodities: dict[str, CombinedCommodity]`.
  - `CombinedCommodity`: `code: str`, `contracts: dict[ContractKey, Contract]`, `price_scan` / `vol_scan` per portfolio as read, `som_tiers`, `intra_tiers`, `inter_tiers`, `som_method: str`, plus any field Task 3 needs (decide while reading the file; record each XML element used and its meaning in the module docstring).
  - `ContractKey = tuple[str, str, int | None]` = `(kind "FUT"|"CE"|"PE", expiry "YYYY-MM-DD", strike_paise | None)`.
  - `Contract`: `price: str` (decimal string as in file), `delta: str`, `vol: str | None`, `risk_array: tuple[str, ...]` (16 decimal strings, file order), `composite_delta: str`.
  - Decimal strings stay strings until Task 3 converts them exactly (`stockify.numerics`).

- [ ] **Step 1: Cut the fixture** from the archived `nsccl.20261109.i1.zip` with `make_fixture.py` (keeps the XML structure, drops other commodities and far strikes). Check the fixture file in (< 300 KB).
- [ ] **Step 2: Write failing tests**: `test_parses_created_and_nifty`, `test_contract_key_for_option_and_future` (a CE 24950 on 2026-11-17 → `("CE","2026-11-17",2495000)`), `test_risk_array_has_16_values_in_file_order`, `test_underlying_filter_skips_others`, `test_reads_zip_and_plain`.
- [ ] **Step 3: Run** → FAIL.
- [ ] **Step 4: Implement** `parse_span` (memory: never hold the whole 54 MB tree; clear elements as they finish).
- [ ] **Step 5: Run** → PASS; time `parse_span` on the full real file for `{"NIFTY","BANKNIFTY"}` and note it in the RECON section (Task 10).
- [ ] **Step 6: Full suite**, commit `MEZ 4.6: read NSE's SPAN 4.00 files`.

### Task 3: Exact SPAN calculation in the oracle (MEZ)

**Files:**
- Create: `stockify/fno/span_exact.py`
- Test: `tests/stockify/test_span_exact.py`

**Interfaces:**
- Consumes: `SpanFile`, `CombinedCommodity`, `Contract`, `ContractKey` (Task 2).
- Produces:
  - `span_requirement(cc: CombinedCommodity, legs: list[tuple[ContractKey, int]]) -> SpanResult` — `legs` are (contract, signed units); returns rupees as exact `Decimal`s.
  - `SpanResult` dataclass: `scan: Decimal, intra_spread: Decimal, inter_credit: Decimal, som: Decimal, long_option_value: Decimal, short_option_value: Decimal, span: Decimal, missing: list[ContractKey]` where `span` follows SPAN 4.00 as NSE applies it: `max(scan + intra_spread − inter_credit − long_option_value + short_option_value, som)` floored at 0 (confirm the exact composition against the file's method fields and Task 4's Kite comparison; document it in the module docstring with the element names).
  - `missing` non-empty ⇒ caller must step down (Task 6).
  - `span_paise(result: SpanResult) -> int` — rupees → paise, rounded up to the whole rupee.

- [ ] **Step 1: Write failing tests** on the fixture, each hand-computed in the test's comment:
  - `test_scan_is_worst_of_16_scenarios_for_one_future` — long 65 units of the 23-Nov NIFTY future: scan = max over the 16 risk-array values × 65 (units: rupees per unit; Review Focus 5).
  - `test_short_straddle_scan_and_som` and `test_long_option_value_credit` (a book of only bought options → span 0).
  - `test_missing_contract_listed_not_guessed` — a strike absent from the fixture → `missing == [that key]`.
  - `test_span_paise_rounds_up_to_rupee`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** Use the dimensional-analysis skill on this module before Step 4 (annotate units at each step: rupees/unit, units, rupees, paise).
- [ ] **Step 4: Run** → PASS. **Step 5: Full suite**, commit `MEZ 4.7: exact SPAN from NSE's risk arrays (oracle)`.

### Task 4: 🛑 Validate exact SPAN against the 400 Kite answers (MEZ)

**Files:**
- Create: `stockify/fno/span_validate.py`
- Modify: `stockify/__main__.py` (`margin span-validate --date YYYY-MM-DD`)
- Test: `tests/stockify/test_span_validate.py`

**Interfaces:**
- Consumes: `store.margin_calibrations(underlying)` rows (`portfolio_json` legs with tradingsymbol/transaction_type/quantity, `kite_json` with `final.span`, `final.exposure`, `final.option_premium`, `wall_ns`), `parse_span`, `span_requirement`, Task 1's `in_force_files`.
- Produces: `validate(day: date, underlyings: list[str]) -> ValidationReport` with per-sample `(kite_span, our_span, kite_exposure, our_exposure)` and the gate result; CLI prints a plain-words table per portfolio type and writes `$LAB/margin_calibration/span-validate-<date>.md`.
- Gate (spec §7): SPAN within **₹50 or 0.5 %** (larger) on **≥ 95 %** of samples; exposure within **₹1** using the ELM file % (B1).

- [ ] **Step 1: Failing test** with two synthetic stored rows over the fixture: one matching, one off → report counts 1/2 within, gate false.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** (map each leg's tradingsymbol to a ContractKey via refdata; pick the SPAN file with the latest `created` ≤ the row's `wall_ns`; ELM % from the archived ELM file; exposure = Σ ELM% × notional per the file's convention (futures: notional = units × futures price; options: as NSE defines — verify against Kite's `exposure`)).
- [ ] **Step 4: Run** → PASS. Then run for real: `.\.venv\Scripts\python.exe -m stockify margin span-validate --date 2026-11-09`.
- [ ] **Step 5: 🛑 Gate.** If it passes: record the numbers in the commit message and continue. If it fails: use superpowers:systematic-debugging on the worst samples (units, file choice, NOV composition, spread tiers), fix Task 3, rerun. If after real investigation it still fails, STOP the C++ tasks and report the numbers in the final report (B1 may then not be labelled "exact").
- [ ] **Step 6: Full suite**, commit `MEZ 4.8: exact SPAN matches Kite on 2026-11-09 (<n>/400 within the gate)`.

### Task 5: SPAN parameters in the bundle, no lookahead (CAM)

**Files:**
- Modify: `stockify/bundle.py`, `stockify/runconfig.py`, `stockify/golden.py`, `docs/plan/A1_schemas.md` (bundle §6.2 `span` section)
- Test: `tests/stockify/test_bundle_span.py`

**Interfaces:**
- Consumes: Task 1 `in_force_files`, `previous_eod`; Task 2 `parse_span`.
- Produces:
  - `runconfig`: `margin.model` choices `("flat", "spanlite", "span_exact", "staircase")`; DEFAULTS `margin.model` = `"staircase"`; `golden.build_bundle` pins `"flat"` when a golden doesn't name a model (goldens unchanged).
  - `bundle["span"]` = `{"YYYY-MM-DD": [ {"created_ns": int, "file": "<archive-relative path>", "sha256": str, "commodities": {cc_code: <only the bundle's instruments' contracts + all parameter blocks, decimal strings>} } ... ] }` — every file usable during that day in `created_ns` order, starting with the in-force file at the day's open (D's `i1` if archived, else the previous trading day's `.s`), never D's `.s`.
  - `bundle["elm"]` = `{"YYYY-MM-DD": {"<kind>|<expiry>|<strike_paise>": "<pct decimal string>"}}` for the bundle's option contracts when D's ELM file is archived.
  - Only resolved when `margin.model` ∈ `{"span_exact","staircase"}`.
- [ ] **Step 1: Failing tests**: `test_span_day_uses_i1_then_intraday_never_s` (Review Focus 1: archive fixture files with created times 20:26 prev day, 11:03, 12:32, 16:55 → bundle day lists the first three only), `test_no_day_files_falls_back_to_previous_eod`, `test_only_bundle_instruments_included`, `test_elm_section_from_archived_file`, `test_goldens_pin_flat` (G01 bundle unchanged), `test_default_model_is_staircase`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS; goldens on both impls unchanged.
- [ ] **Step 5: Full suite**, commit `CAM 4.3: the SPAN files in force go into the bundle, without lookahead`.

### Task 6: The staircase model in the oracle, with step recording (MEZ)

**Files:**
- Create: `stockify/fno/staircase.py`
- Modify: `stockify/oracle/runner.py` (model selection), `stockify/oracle/artifacts.py` (`margin_daily.csv` gains `step` column), `docs/plan/A1_schemas.md` §6.3 (+ the blockwatch tie for the header), `engine/src/artifacts.cpp` header string only if blockwatch requires both sides in this commit (otherwise Task 7)
- Test: `tests/stockify/test_staircase.py`

**Interfaces:**
- Consumes: Task 3 `span_requirement`/`span_paise`; `SpanLiteMargin`; `FlatMargin`; bundle `span`/`elm` (Task 5).
- Produces:
  - `SpanExactMargin(bundle, engine)` and `StaircaseMargin(bundle, engine)` with the same methods as `SpanLiteMargin` (`requirement`, `breakdown`, `mis`, `mis_rate`, `labels`).
  - Steps (exact strings, used in labels and the CSV): `"B1_span_nse_elm"`, `"B2_span_rule_elm"`, `"B3_spanlite_calibrated"`, `"B4_spanlite_placeholder"`, `"B5_flat"`.
  - Per underlying per check: the in-force file = latest entry of `bundle["span"][day]` with `created_ns ≤ now`; if any leg is missing from it → step down for that whole underlying (Review Focus 2). B1 needs every option leg's ELM % in `bundle["elm"][day]`; B1 applies only the file's ELM % (no X-9 add-on); B2 applies the dated rules including the X-9 add-on (Review Focus 3). B3 = a `spanlite` row in force whose `calibration_id` ≠ `"placeholder"`; else B4; else B5.
  - `margin_daily.csv` header becomes `trade_date,underlying_id,model,step,scan,somc,elm,addons,total`.
  - Run labels: `margin:<step>` for each step used plus `margin_steps:<step>=<days>` counts.
- [ ] **Step 1: Failing tests**: `test_b1_when_file_and_elm_present`, `test_missing_strike_steps_whole_underlying_down`, `test_b1_does_not_add_expiry_day_addon_b2_does` (expiry-day ELM 4/5 %), `test_b3_requires_non_placeholder_row`, `test_steps_recorded_in_csv_and_labels`, `test_span_exact_model_has_no_fallback` (missing contract → error naming it).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5:** Regenerate nothing silently: run goldens on the oracle; list every golden whose artifacts changed (expected: only the `margin_daily.csv` header) in the commit message. Full suite; commit `MEZ 4.9: the margin staircase B1–B5 in the oracle, steps recorded`.

### Task 7: Exact SPAN and the staircase in the C++ engine (MEZ, RD)

**Files:**
- Create: `engine/include/stocksim/margin/span_exact.hpp`, `engine/src/margin/span_exact.cpp`, `engine/include/stocksim/margin/staircase.hpp`, `engine/src/margin/staircase.cpp`
- Modify: `engine/CMakeLists.txt`, `engine/src/engine.cpp` (model selection), `engine/src/artifacts.cpp` + `engine/include/stocksim/engine.hpp` (`MarginDailyRow.step`), `stockify/tools/fuzz.py` (worlds sometimes carry a synthetic `span`/`elm` section and `margin.model` `staircase`)
- Test: `tests/stockify/test_span_parity.py`, `engine/tests/test_span_exact.cpp`

**Interfaces:**
- Consumes: bundle `span`/`elm` exactly as Task 5 writes them; Task 6's step strings and CSV header.
- Produces: `SpanExactMargin` and `StaircaseMargin` as `MarginModel`s; byte-identical `margin_daily.csv`, labels and all artifacts vs the oracle.
- [ ] **Step 1: Failing tests**: `test_span_exact_parity_on_fixture_worlds` (oracle vs engine artifacts identical on worlds built from the fixture: futures, straddles, flies, bought options, a missing strike), `test_staircase_parity_steps`, plus doctest `exact decimal-to-paise conversion` in `test_span_exact.cpp`.
- [ ] **Step 2: Run** → FAIL (no model). **Step 3: Implement** (decimal strings → exact integer arithmetic; no `double` in the requirement path unless the oracle's composition needs it, then mirror the oracle's operation order; use the modern-cpp and c-review skills on the new files).
- [ ] **Step 4:** `.\run.ps1 -BuildEngine` clean (`/W4 /WX`), ctest, parity tests PASS; P-04 200 seeds with the new world variant agree and the coverage assertion shows the staircase steps reached.
- [ ] **Step 5: Full suite**; commit `MEZ 4.10: exact SPAN and the staircase in the engine, exact parity`.

### Task 8: Pre-trade check and the scorecard (MEZ)

**Files:**
- Create: `stockify/fno/margin_check.py`
- Modify: `stockify/__main__.py` (`margin check`, `margin scorecard`)
- Test: `tests/stockify/test_margin_check.py`

**Interfaces:**
- Consumes: Tasks 1–6 (archive, parser, staircase); `store.margin_calibrations`.
- Produces:
  - `python -m stockify margin check --leg "BUY 65 NIFTY26NOVFUT" [--leg …] [--date YYYY-MM-DD] [--at HH:MM]` → prints the requirement in ₹ (en-IN grouping), its breakdown, and the staircase step; offline from the archive; `--date` defaults to the latest archived day.
  - `python -m stockify margin scorecard` → for every stored Kite answer, each step's median and 90th-percentile error vs Kite by portfolio type, in plain words; flags any case where a lower step beats a higher one.
- [ ] **Step 1: Failing tests**: `test_check_prints_rupees_and_step` (fixture archive), `test_check_rejects_unknown_symbol`, `test_scorecard_flags_inversion` (synthetic rows where B4 beats B1 → flagged).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS; run both for real on 2026-11-09 data and keep the outputs for the final report.
- [ ] **Step 5: Full suite**; commit `MEZ 4.11: margin check and the staircase scorecard`.

### Task 9: Explain the steps in the UI (WP)

**Files:**
- Modify: `web/src/app/tests/ui.tsx` (label meanings), any label fixture; `server/schemas.py` + `web/src/lib/types.ts` only if the margin-by-day DTO needs the `step` field (keep the dto-fields block in sync)
- Test: existing `tests/stockify/test_api_m4.py` extended with a `step` column row; `npm run lint`, `npm run build` (main checkout)

- [ ] **Step 1: Failing test**: the `/api/tests/{id}/margin` rows carry `step`.
- [ ] **Step 2: Implement**: the margin-by-day card shows the step per row in plain words ("exact NSE margin", "NSE margin, exposure from rules", "estimate (calibrated)", "estimate (not calibrated)", "simple estimate"); label meanings for `margin:B*` and `margin_steps:*`. Mobile-first ≤ 390 px.
- [ ] **Step 3:** Tests PASS, lint + build clean; commit `WP 4.5: the margin step per day, in plain words`.

### Task 10: History backfill, docs and final verification (CAM, ENG)

**Files:**
- Modify: `docs/plan/00_overview.md` (decision D-64: the staircase replaces M4.10's stretch; owner's 2026-11-09 approval; the plan is a guide), `docs/plan/M4_fno.md` (M4.10 boxes annotated), `docs/plan/RECON.md` (section with real numbers: archive counts, parse time, validation result, scorecard), `docs/plan/HOWTO_options.md` (staircase + `margin check`)
- [ ] **Step 1:** Start `python -m stockify nse margin-files --backfill 2009-01-01 2026-11-08` in the background (EOD `.s` only; resumable). Record progress; it may outlast the session — the daemon job keeps current days.
- [ ] **Step 2:** Also probe intraday history (`i1`) on a few old dates; record how far back it exists in 01_facts S-7.
- [ ] **Step 3:** Docs as listed; superpowers:requesting-code-review on the whole branch (plus Trail of Bits differential-review on the C++ and the fetch code); fix findings.
- [ ] **Step 4:** superpowers:verification-before-completion: build + ctest, goldens both impls, full suite, `npm run lint`/`build`, blockwatch — all green; commit `ENG 4.3: the margin staircase documented; history backfill started`.

# Candlestick Charts with Trade Reasons Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every backtest, paper session, walk-forward and sweep trial shows a candlestick chart of the bars the strategy actually saw, with buy and sell markers that carry the strategy's own plain-English reason, and the strategy's own indicator lines drawn over the candles.

**Architecture:**
- **Run side (C1–C8).** `ctx.place(..., note=)` and `ctx.plot(...)` exist on both runners. The note is a new last column of `orders.csv` and part of every order record; plots go to a new deterministic file `plots.csv`. The oracle (Python) and the engine (C++) write identical bytes; the shipped strategies get real notes and plots.
- **Read side (C9–C13).** A pure Python module reads the bars from the run's own event pack (a session's from its journal) and joins fills, notes, charges and FIFO trade P&L into one chart model; read-only sync routes serve it.
- **Web (C14–C20).** One client-only component `PriceChart` (Lightweight Charts) is placed on four pages.

**Tech Stack:** Python 3.12 stdlib, C++20 (engine, pybind), FastAPI, Next.js 16 with React 19, `lightweight-charts@5.3.0`.

**Spec:** `docs/superpowers/specs/2026-11-12-candlestick-charts-design.md` (binding). Read with `docs/plan/A1_schemas.md` §6.3–6.4, `docs/plan/A4_engine_semantics.md` §2.2 and §9, `docs/plan/HOWTO_strategy.md`, `web/design.md`, and the audit `.superpowers/audits/lightweight-charts-supply-chain.md`.

**Runs interleaved with:** `docs/superpowers/plans/2026-11-12-m6-reporting-research.md` (the M6 plan). Task numbers "M6 Task n" below are that plan's.

## Scheduling (each C task states its "after")

| Group | Tasks | Runs |
|---|---|---|
| Engine and runner side | C1–C8 | right after **M6 Task 14** (it rebuilds the engine and runs `c-review`; `metrics.py` is also touched by M6 Task 5, already done by then). Start C2 only after the Task 11 implementer's uncommitted edits to `runs.py`, `runconfig.py`, `__main__.py`, `loader.py` are committed. C1 to C7 each rebuild incrementally (`.\run.ps1 -BuildEngine`) as their test step; **one `c-review` over the combined C++ diff of C1–C7 runs in C8**, so the engine is reviewed once (fold it into M6 Task 14's review if the two are run back to back). |
| API | C9–C13 | C9 after C7; C10 after C5 and C9; C11 after **M6 Task 9** (router pattern, `RunResearchDTO` already in `schemas.py`) and C10; C12 after **M6 Task 22** (walk-forward API) and C10; C13 after C9 and C10 (needs nothing from M6). |
| Web | C14–C20 | C14 after **M6 Task 29** (web foundation: `npm` and the web build first run in the worktree) and C11. C15 after C14. C16 (run page) after **M6 Task 30** and C15. C17 (session page) any time after C13 and C15. C18 (walk-forward page) after **M6 Task 36**, C12 and C15. C19 (sweep page) after **M6 Task 35**, C11 and C15. C20 after all. |

## Global Constraints

Copied from the M6 plan where they apply, plus the design's own.

- **Windows only (D-26).** Run everything with `.\.venv\Scripts\python.exe` (system `python` lacks tzdata). No Linux presets, CI, systemd or POSIX-only code.
- **Tests:** stdlib `unittest`, no pytest, no `hypothesis`. Offline and credential-free. `import tests.support` first (it sandboxes `state_dir`/`logs_dir`); use `tests.support.isolated_env({...})`, never `patch.dict(os.environ, ..., clear=True)`; SQLite-backed tests subclass `SandboxedDBTest` with `db_modules`. Run with `.\.venv\Scripts\python.exe tests\run_tests.py <filter>`. The full suite (4–10 min) runs in the background and must not exit 3 (build the engine first with `.\run.ps1 -BuildEngine`) and not exit 2 (a tracked defect fixed: promote the test). New test packages get an `__init__.py` like `tests/stockify/daemon/`.
- **Property loops** (note sanitising, number formatting, plot caps): a seeded stdlib loop (`random.Random(<fixed int>)`, at least 2,000 cases, the seed printed in the failure message), never `hypothesis`. The C++ side is reached through `stockify._engine` bindings so one Python loop compares both implementations.
- **Determinism:** no wall clock, no `std::` random distributions, no unordered iteration in outputs, `/fp:precise`. Notes and plots are run outputs: identical across repeated runs and byte-identical between the oracle and the engine (A4 §9.3). `plots.csv` and the `note` column are never floats; plot values are int64.
- **Money and time:** money int64 paise, quantity units, time UTC epoch ns; IST only at display. The API sends prices as rupee decimal strings converted from paise at the edge (never floats in storage); the UI formats ₹ through `web/src/lib/format.ts` (`Intl.NumberFormat("en-IN")`), never `$`.
- **Skills by what a diff touches (CLAUDE.md):** `engine/` C++ → `modern-cpp` while writing, `c-review` after (once, C8); the strategy API (`ctx.place`/`ctx.plot`) and the strategy guard → `differential-review` (C8); new endpoint, new config file or default → `sharp-edges` and `insecure-defaults` (report only, C11–C13); paise/units in markers and plots → `dimensional-analysis` (report only, never write annotations); note formatting and plot caps → the property-based-testing method with the stdlib; `web/` → Hallmark, `design:design-critique`, `design:accessibility-review`, `design:ux-copy` (the shipped strategies' notes are copy, too); `dataviz` for the chart; `blockwatch` for every tied edit.
- **UI:** desktop only (D-83), readable down to about 1024 px. Every page designed with the Hallmark skill (app-screen flow, `web/design.md` locked), static `node ~/.claude/skills/hallmark/scripts/sloplint.mjs <files> --scope app` (never `--render`), no imagegen. Colours (candle up/down, markers, plot lines) come from the tokens in `web/src/app/tokens.css` (`--color-up`, `--color-down`, `--color-info`, `--color-warn`, `--color-accent`, `--color-muted`, `--color-rule`, `--color-paper*`, `--color-ink*`), read with `getComputedStyle` so dark theme and later token changes follow; no hex literals. Components from `web/src/components/ui.tsx` (`Panel`, `PageHead`, `Stat`, `Table`, `Chip`, `Skeleton`, `Empty`); every data region has loading, empty, error and full states. Next.js 16 has breaking changes: read the relevant pages of `web/node_modules/next/dist/docs/` before writing web code (`searchParams`/`params` are Promises in server pages). `webapp-testing` cannot run (no `playwright`): verification is lint, build, sloplint, pure-function checks (`tests/stockify/*_check.ts` run by a `test_*_checks.py`, as `test_param_checks.py` does) and the owner's look at 1024 px.
- **API:** loopback only (D-77): routes live in routers already wrapped by `server/local_only.py`. `server/schemas.py` ↔ `web/src/lib/types.ts` are tied by the blockwatch block `dto-fields`: append DTOs to both files in the same order and run `~/.tools/blockwatch/blockwatch.exe`. Fixed paths are declared before `/{id}` paths. Sync `def` routes for anything that reads files. Responses serialise with `allow_nan=False` semantics. Any API path carrying a run id or session id passes through `paths.run_path` / the session checks already used by `server/routers/tests.py` (`_row_or_404`, `_folder`): never open a path built from a query parameter.
- **Data safety:** the real lab (`%LOCALAPPDATA%\STOCKIFY`) is only read; tests never touch it. Old run folders are never rewritten; no migration touches `ticks/` or `refdata.sqlite` (I-13). No secret reaches a note, a plot or a log line (I-10). The chart reads only what STOCKIFY already holds: no new data source, no `bars.sqlite`, no re-fetch.
- **Packages (owner-approved 2026-11-12, audit `.superpowers/audits/lightweight-charts-supply-chain.md`: OK with conditions):** the only new package is **`lightweight-charts@5.3.0`, exact pin, no caret** (first approved at 5.2.1; the owner moved the pin to 5.3.0 on 2026-11-13 instead of waiting until 2026-11-24: D-110), and its one dependency **`fancy-canvas@2.1.0`**. Install from the lockfile (`npm ci`); the lockfile entry's integrity starts `sha512-w16fu2/FteUkIJdg` (full value in `web/package-lock.json`). No other package, no Python package.
- **Licence and attribution:** Apache-2.0 plus TradingView attribution. Every chart is created with `layout.attributionLogo: true`; the NOTICE line "TradingView Lightweight Charts (TM) Copyright (c) 2025 TradingView, Inc. https://www.tradingview.com/" appears once in the app footer (`web/src/app/layout.tsx`, controller's call).
- **Client-only component:** `PriceChart.tsx` starts with `"use client"`, creates the chart in `useEffect`, calls `chart.remove()` in cleanup, and is imported with `next/dynamic(..., { ssr: false })`. Pages hand it data and never touch the library.
- **Commits:** `CH 6.<n>: summary` (D-31; provisional position code, the controller rules). The owner's git identity; **no co-author, tool or "agent" lines** (skill `no-agentic-coauthor`). **Stage explicit file paths only** (each commit block lists them; never a directory, never `CLAUDE.md`, D-29): run `git status --short` first and add only what the task touched. Push only on the owner's "push". The pre-commit hook runs gitleaks, then blockwatch; both fail closed, which is why the tied edits in C3 and C4 each land in one commit.
- **D-rows:** next free `D-xx` in `docs/plan/00_overview.md` §5 at the time of the commit (M6 tasks take rows too: check with `grep -o "^| D-[0-9]*" docs/plan/00_overview.md | tail -1`). Written in C2, C4, C9, C10, C14.
- **Bookkeeping:** C1 adds a "Candlestick charts (C1–C20)" checklist to `docs/plan/M6_reporting_research.md`; each task ticks its box in the same commit.

## Review Focus

Failure modes the spec implies that no single item's tests would catch. Each line is pinned by a named test in the owning task.

1. **A hostile note** (commas, quotes, CR/LF, tabs, NUL, a lone surrogate, emoji, right-to-left text, 10,000 characters, text starting with `=`, `<script>`). The owner expects one `orders.csv` row per order, at most 200 characters ending in "…" when cut, identical on both runners, and shown on screen as plain text, never as markup. Pinned by `test_clean_note_matches_engine_on_random_text` (C1), `test_orders_csv_has_one_row_per_order_with_hostile_notes` (C3), `test_card_text_is_never_html` (C14).
2. **An old, empty or pruned run** (an `orders.csv` without `note`, no `plots.csv`, a run that made no trades, a pack removed from disk). The owner expects the candles with no markers, or markers on an empty time axis with "the price data for this run is no longer on disk", never an error page. Pinned by `test_old_orders_csv_without_note_reads_as_empty_notes` and `test_pruned_pack_gives_markers_on_an_empty_axis` and `test_no_trades_run_still_has_bars` (C10), `test_empty_and_error_states` (C15).
3. **A fill with no matching bar, or many fills on one bar** (an `eod_close` fill after the last bar, a fill exactly on a bar boundary, a sliced order's ten children filling in the same minute). The owner expects each fill marked once on the bar that contains its time, in fill order, none dropped silently. Pinned by `test_marker_snaps_to_the_bar_containing_the_fill`, `test_two_fills_in_one_bar_are_both_marked_in_fill_order` (C10).
4. **A long run** (more than 50,000 bars, panning across the page seam). Pinned by `test_pages_join_without_a_gap_or_a_duplicate_bar` (C10) and `test_range_request_matches_the_whole_run_slice` (C11); the client's merge by `test_merge_pages_keeps_bars_unique_and_sorted` (C14).
5. **Bad or excessive `plot` calls** (a float, NaN, an unknown kind, a nine-series run, 200,001 points, the same series twice at one `t_ns`, a name that is empty or 1,000 characters). The owner expects a clear error for a strategy bug and a deterministic log line, not a bigger file or a divergence between the runners. Pinned by `test_plot_rejects_floats_nan_and_bad_kinds`, `test_ninth_series_and_point_200001_are_dropped_with_one_log_line`, `test_same_t_last_value_wins`, `test_engine_and_oracle_write_identical_plots_csv` (C4).

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `stockify/oracle/notes.py` (new), `engine/include/stocksim/notes.hpp`, `engine/src/notes.cpp` (new), `engine/bindings/engine.cpp`, `engine/CMakeLists.txt` (modify), `stockify/strategy.py` (modify) | `clean_note`, `fmt_price`, `fmt_qty`, `to_level` in both languages; strategy-visible helpers | C1 |
| `stockify/oracle/orders.py`, `context.py`, `runner.py`, `artifacts.py`, `stockify/oracle/sod_state.py` (modify), `stockify/bundle.py`, `stockify/config/system_notes.json` (new), `stockify/config/loader.py`, `stockify/runs.py` (modify), `docs/plan/A1_schemas.md` | the `note` field, the column, system notes, old-run compatibility | C2 |
| `engine/include/stocksim/engine.hpp`, `orders.hpp`, `engine/src/engine.cpp`, `engine_trading.cpp`, `artifacts.cpp`, `sod_state.cpp`, `json_bundle.cpp`, `engine/bindings/engine.cpp` (modify), `stockify/oracle/sod_state.py` (field list), `tests/fixtures/sod_state/*` | the same in C++, byte-identical | C3 |
| `ctx.plot` in both runners, `plots.csv`, the three `deterministic-files` lists | indicator overlays | C4 |
| `stockify/oracle/metrics.py` (modify) | public `fifo_trades` | C5 |
| `stockify/strategies/*.py`, `docs/plan/HOWTO_strategy.md` | notes and plots for the Python strategies | C6 |
| `engine/strategies/orb_cpp.cpp`, `sma_cross_cpp.cpp` | the same for the C++ strategies | C7 |
| `stockify/oracle/bars.py` (new), `stockify/oracle/runner.py` (modify), `stockify/reporting/chartbars.py` (new) | shared bucketing, display bars from packs and journals | C9 |
| `stockify/reporting/chart.py` (new) | run chart model: bars, markers, plots, paging | C10 |
| `server/routers/tests.py`, `server/schemas.py`, `web/src/lib/types.ts`, `docs/plan/A2_interfaces.md` §6 | `GET /api/tests/{run_id}/chart`, DTOs | C11 |
| `stockify/reporting/chart_wf.py` (new), `server/routers/research.py` | walk-forward chart | C12 |
| `stockify/reporting/chart_session.py` (new), `server/routers/trade.py` | session chart | C13 |
| `web/package.json`, `web/package-lock.json`, `web/src/lib/chart.ts`, `web/src/app/layout.tsx`, `tests/stockify/chart_check.ts`, `tests/stockify/test_chart_checks.py` | the package, pure helpers, footer notice | C14 |
| `web/src/components/PriceChart.tsx`, `PriceChartLazy.tsx`, `web/src/lib/chart_api.ts`, `web/src/app/base.css` or the existing CSS file the components use | the chart | C15 |
| `web/src/app/tests/[id]/page.tsx`; `web/src/app/trade/[id]/page.tsx`; `web/src/app/research/walkforward/[id]/page.tsx`; `web/src/app/research/sweeps/[id]/page.tsx` | placements | C16–C19 |

## Task graph at a glance

C1 helpers → C2 oracle note → C3 engine note (with the state field lists) → C4 plot (both) → C5 fifo_trades → C6 Python strategies → C7 C++ strategies → C8 engine close → C9 bars → C10 run chart model → C11 run API → C12 walk-forward API, C13 session API → C14 package and helpers → C15 component → C16–C19 pages → C20 close.

---

### Task C1: Note text and number helpers in both languages (CH 6.1)

**After:** M6 Task 14 (engine rebuilt there).

**Files:**
- Create: `stockify/oracle/notes.py`, `engine/include/stocksim/notes.hpp`, `engine/src/notes.cpp`, `engine/tests/test_notes.cpp`, `tests/stockify/test_notes.py`
- Modify: `stockify/strategy.py` (after `paise`, ~line 187), `engine/bindings/engine.cpp` (module-level functions), `engine/CMakeLists.txt` (add `src/notes.cpp` to the core list near line 94 and `tests/test_notes.cpp` to the test list near line 127), `docs/plan/M6_reporting_research.md` (checklist)

**Interfaces:**
- Produces (Python, `stockify.oracle.notes`): `NOTE_MAX = 200`; `clean_note(text: str) -> str`. Rules, in order: (1) each lone surrogate (Python) or invalid UTF-8 sequence (C++) becomes U+FFFD; (2) each Unicode `Cc` character (U+0000–U+001F, U+007F–U+009F) and U+2028, U+2029 becomes one ASCII space; (3) trim ASCII spaces from both ends (no other whitespace is touched, no inner collapsing); (4) if longer than 200 code points, keep the first 199 and append "…" (U+2026), so the result is at most 200 code points; `""` stays `""`; never `None` (a non-`str` argument raises `TypeError`).
- Produces (Python, `stockify.strategy`, allowlisted for strategies by `ALLOWED_IMPORTS`): `fmt_price(paise: int) -> str` (Indian grouping, always two decimals, `-` sign for negatives: `241840` → `"2,418.40"`, `123456780` → `"12,34,567.80"`, `-5` → `"-0.05"`); `fmt_qty(units: int) -> str` (`125000` → `"1,25,000"`); `to_level(x: float) -> int` (`round(x * 10000)` half-even, `54.3217` → `543217`; NaN or infinity raises `ValueError`). Non-`int` arguments to the first two raise `TypeError` (bool refused).
- Produces (C++, `namespace stocksim`, `notes.hpp`): `std::string clean_note(std::string_view)`, `std::string fmt_price(Paise)`, `std::string fmt_qty(Qty)`, `Paise to_level(double)` (`std::nearbyint` in the default rounding mode, `RunError` on non-finite). Bound in `stockify._engine` as `clean_note`, `fmt_price`, `fmt_qty`, `to_level` so Python tests drive both.

- [ ] **Step 1: Write the failing tests.** `tests/stockify/test_notes.py`: `test_clean_note_hand_cases` (control characters, a 201-character string → 199 + "…", exactly 200 stays, `"  x  "` → `"x"`, tab and newline become spaces, U+2028, a lone surrogate, `""`); `test_fmt_price_and_qty_hand_cases` (the values above, `0`, `5` → `"0.05"`, a negative lakh, `2**63 - 1` does not raise); `test_to_level_hand_cases`; `test_helpers_are_importable_by_a_strategy` (`stockify.strategy.check_source` accepts a one-line strategy importing `fmt_price`); `test_clean_note_matches_engine_on_random_text` (seeded loop of 2,000 strings from an alphabet that includes ASCII, controls, `,"\r\n`, emoji, RTL, U+2028, NBSP, `<script>`; both implementations equal, the result has at most 200 code points, no `Cc` character, no leading or trailing space, and `clean_note(clean_note(s)) == clean_note(s)`); `test_fmt_matches_engine_on_random_ints` (random paise over ±10**15, including 0 and multiples of 100). Skipped with a clear message when the engine is not loadable. C++: `engine/tests/test_notes.cpp` doctest with the same hand cases plus invalid UTF-8 (the bytes C3 28 become U+FFFD then "(").
- [ ] **Step 2: Run to verify failure:** `.\.venv\Scripts\python.exe tests\run_tests.py notes` → FAIL (module missing).
- [ ] **Step 3: Implement** the three Python functions and the C++ twins (skill `modern-cpp`: `std::string_view`, no raw loops over bytes where `<ranges>` reads better, `checked` arithmetic for the grouping); register the bindings; add the sources to `CMakeLists.txt`.
- [ ] **Step 4: Rebuild and run:** `.\run.ps1 -BuildEngine` (builds, runs ctest) green; `tests\run_tests.py notes` → PASS; `tests\run_tests.py strategy` still PASS (the guard).
- [ ] **Step 5: Commit** (checklist box ticked).

```bash
git add stockify/oracle/notes.py stockify/strategy.py engine/include/stocksim/notes.hpp engine/src/notes.cpp engine/tests/test_notes.cpp engine/bindings/engine.cpp engine/CMakeLists.txt tests/stockify/test_notes.py docs/plan/M6_reporting_research.md
git commit -m "CH 6.1: note cleaning and Indian-grouped price and quantity text, the same in Python and C++"
```

---

### Task C2: The `note` on every order: the oracle (CH 6.2)

**After:** C1 and the Task 11 implementer's commit.

**Files:**
- Create: `stockify/config/system_notes.json`, `tests/stockify/test_order_note.py`
- Modify: `stockify/oracle/orders.py` (`Order.note: str = ""`, after `tag`), `stockify/oracle/context.py:128` (`place(..., tag: str = "", note: str = "")`), `stockify/oracle/runner.py:579` (`place(..., tag="", parent=0, system=False, note="")`: clean the note once at the top with `notes.clean_note`; the slice recursion at ~line 599 passes `note`; a child with an empty note takes its parent's), `runner.py:1366` (the AUTO_SQOFF placement takes `note=self.system_notes.get("AUTO_SQOFF", "")`), `stockify/oracle/artifacts.py` (`ORDERS` gains `note` as the last column; the row tuple gains `o.note`), `stockify/bundle.py:716` (the bundle gains `"system_notes"`), `stockify/config/loader.py` (schema and validation for `system_notes.json`, next to `broker_policy.json` at line 44), `stockify/runs.py` (see Step 3), `docs/plan/A1_schemas.md` (§6.3 `orders.csv` columns end `…,reason,note`; the order record table in §6.4 gains `note`; bundle §6.2 gains `system_notes`), `stockify/golden.py` `build_bundle` (adds the same `system_notes`), `docs/plan/00_overview.md` (D-row)

**Interfaces:**
- Consumes: `notes.clean_note` (C1).
- Produces: `Order.note`; `Context.place(..., note: str = "") -> int`; `orders.csv` last column `note` (`""` when none); bundle key `system_notes: {tag: text}` (optional for readers: absent means every system note is `""`); `stockify/config/system_notes.json` = `{"schema": "stockify.system_notes/1", "notes": {"AUTO_SQOFF": "Closed by the broker's 15:20 auto square-off", "RMS_SQOFF": "Closed by the broker's risk system", "SETTLEMENT": "Settled at expiry at {price}"}}` (the loader checks every value passes `clean_note` unchanged, every key is in `orders.SYSTEM_TAGS` or is `SETTLEMENT`; only `SETTLEMENT` may and must contain the one placeholder `{price}`, which the chart model (C10) fills with `fmt_price`; no `effective_from`, these are words not market rules). Expiry settlements have no order, so `SETTLEMENT` is read by C10, not by the runners.
- `ctx.place` still validates before it does anything: a non-`str` note raises `TypeError("ctx.place: note must be text")`, as `tag` is handled today.

- [ ] **Step 1: Write the failing tests** in `test_order_note.py`: `test_place_keeps_a_cleaned_note_in_orders_csv` (a stub strategy places with `note="Bought 50: price 2,418.40 broke above 2,412.50"`; the orders.csv header ends `reason,note`; the row's note is the text), `test_no_note_is_an_empty_cell_not_null`, `test_sliced_children_inherit_the_parents_note_and_keep_their_own`, `test_rejected_order_keeps_its_note`, `test_every_fill_of_one_order_belongs_to_one_note` (a join check, not a copy), `test_auto_sqoff_order_gets_the_system_note_from_the_bundle`, `test_bundle_without_system_notes_gives_empty_system_note` (old bundle), `test_system_notes_loader_rejects_unknown_tag_and_uncleaned_text`, `test_orders_csv_has_one_row_per_order_with_hostile_notes` (oracle half; C3 adds the engine half), `test_old_orders_csv_without_note_column_still_imports` (`runs` ingest of a folder written before this change gives `note` `""`), `test_rerun_of_an_old_run_reports_the_new_column_not_a_failure` (the D-64 pattern at `runs.py` ~355–378: when the only differences are `orders.csv` gaining the `note` column with every old column equal, and `plots.csv` being new, the re-run prints `reproduced (orders.csv gained the note column, D-xx)` and exits 0), `test_note_must_be_text`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.** The sod_state order fields are NOT changed here (C3 changes both language lists in one commit, because blockwatch ties them): until C3, a paper session's order notes do not cross a day boundary; no test in this task depends on it. For `runs.py`, mirror the margin-column exception rather than weakening `determinism_diff`.
- [ ] **Step 4: Run** `tests\run_tests.py order_note`, `oracle`, `golden`, `runs`, `bundle`, `config` → PASS; `~/.tools/blockwatch/blockwatch.exe` clean.
- [ ] **Step 5: D-row** (the `note` column; `system_notes.json`; old runs read `""`, are never rewritten; a re-run of an old run is "reproduced with a new column"; golden YAMLs key rows by name so they need no regeneration, hash-pinned fixtures are regenerated in C3/C4). **Commit.**

```bash
git add stockify/oracle/orders.py stockify/oracle/context.py stockify/oracle/runner.py stockify/oracle/artifacts.py stockify/bundle.py stockify/config/system_notes.json stockify/config/loader.py stockify/runs.py stockify/golden.py docs/plan/A1_schemas.md docs/plan/00_overview.md docs/plan/M6_reporting_research.md tests/stockify/test_order_note.py
git commit -m "CH 6.2: every order carries the strategy's own note, written to orders.csv by the oracle"
```

---

### Task C3: The `note` in the C++ engine, the bridge and the carried state (CH 6.3)

**After:** C2.

**Files:**
- Modify: `engine/include/stocksim/engine.hpp:122` (`PlaceRequest.note`), `orders.hpp:35` (`Order::note` after `tag`), `engine/src/engine_trading.cpp` (~line 244 `o.note = clean_note(r.note)`; the slice recursion inherits; ~line 564 `OrderView` is **not** changed: strategies see `tag`, the note is an output), `engine/src/engine.cpp:1022` (AUTO_SQOFF takes the bundle's system note), `engine/src/json_bundle.cpp` (read `system_notes`, optional), `engine/src/artifacts.cpp` (orders.csv header and row), `engine/src/sod_state.cpp:45` (`kOrderFields`) and `stockify/oracle/sod_state.py:43` (`ORDER_FIELDS`) and `docs/plan/A1_schemas.md` §6.4 order-record table (all three gain `note` after `tag`, **in this one commit**: blockwatch ties them), `engine/bindings/engine.cpp:333` (`place` gains `py::arg("note") = ""`, `r.note = py::str(note)` after `tag`), `tests/fixtures/sod_state/day0_state.json` (regenerated by the oracle for `day1_bundle.json`; its orders gain `note`), `tests/stockify/sod_state_hostile.py` (a row for an order without `note`)
- Test: `tests/stockify/test_engine_note.py`, `engine/tests/test_sod_state.cpp` (a note round trip)

**Interfaces:**
- Consumes: `clean_note` (C1), the oracle behaviour of C2.
- Produces: `PlaceRequest::note`, `Order::note`; `Engine.place(..., note="")` in `stockify._engine`; sod_state order records with `note` (a state written before this change, whose orders lack the key, loads with `note` `""` in **both** runners: the one key the "exactly its listed keys" rule treats as optional on read; always written).

- [ ] **Step 1: Write the failing tests** (`test_engine_note.py`; skipped when the engine is not loadable): `test_engine_and_oracle_write_identical_orders_csv_with_notes` (bar run and snapshot run; a sliced order, a rejected order, an IOC, a modified order, an AUTO_SQOFF order), `test_orders_csv_has_one_row_per_order_with_hostile_notes` (engine half; the hostile strings of C1's alphabet), `test_sod_state_carries_notes_byte_for_byte` (day 1 oracle → day 2 engine and the reverse; `eod_state` bytes equal), `test_state_without_note_loads_as_empty_note_in_both_runners`, `test_pending_order_note_survives_the_day_boundary`, `test_note_with_a_nul_in_a_python_strategy_is_cleaned_by_the_bridge`. C++ doctest: a state with a note round-trips; one without the key loads.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** (skill `modern-cpp`). Keep the oracle's `Order.copy()` and the state writer in step with `ORDER_FIELDS`.
- [ ] **Step 4: Rebuild and run:** `.\run.ps1 -BuildEngine`; `tests\run_tests.py engine_note`, `engine_parity`, `sod_state`, `engine_sod_state`, `parity_journal`, `golden` → PASS; blockwatch clean.
- [ ] **Step 5: Parity of stored paper days.** `stockify/parity.py::_first_difference` (line 79): when the table is `orders`, a missing `note` on either side compares as `""` (a stored session day written before C2 has no `note`; the regenerated twin has). Test `test_parity_ignores_a_missing_note_on_an_old_stored_day` in `tests/stockify/test_parity_journal.py`. **Commit.**

```bash
git add engine/include/stocksim/engine.hpp engine/include/stocksim/orders.hpp engine/src/engine_trading.cpp engine/src/engine.cpp engine/src/json_bundle.cpp engine/src/artifacts.cpp engine/src/sod_state.cpp engine/bindings/engine.cpp stockify/oracle/sod_state.py stockify/parity.py docs/plan/A1_schemas.md tests/fixtures/sod_state/day0_state.json tests/stockify/sod_state_hostile.py tests/stockify/test_engine_note.py tests/stockify/test_parity_journal.py engine/tests/test_sod_state.cpp docs/plan/M6_reporting_research.md
git commit -m "CH 6.3: the note in the C++ engine and the carried state, byte for byte with the oracle"
```

---

### Task C4: `ctx.plot` and `plots.csv` on both runners (CH 6.4)

**After:** C3.

**Files:**
- Modify: `stockify/oracle/context.py` (`plot`), `stockify/oracle/runner.py` (the buffer, the cap log lines), `stockify/oracle/artifacts.py` (`DETERMINISTIC` and `write`, the `<block name="deterministic-files">` at line 36), `engine/include/stocksim/engine.hpp` (`Context::plot`), `engine/src/engine.cpp`, `engine/src/artifacts.cpp` (`kDeterministic` at line 29), `engine/bindings/engine.cpp` (`plot`), `docs/plan/A1_schemas.md` §6.3 (a `plots.csv` row with ✔ **placed in the same position in all three lists**: after `margin_daily.csv`, before `metrics.json`), `docs/plan/A4_engine_semantics.md` §9.1 (the list), `tests/stockify/test_engine_parity.py:185` (`CSV_AND_LOG` gains `plots.csv`), `docs/plan/00_overview.md`
- Test: `tests/stockify/test_plot.py`, a doctest case in `engine/tests/test_output.cpp`

**Interfaces:**
- Produces: `ctx.plot(name: str, value: int, *, kind: str = "price", instrument: int = 0) -> None` (Python `Context`, the engine bridge and C++ `Context::plot(std::string_view name, std::int64_t value, std::string_view kind = "price", InstrumentId instrument = 0)`). `value` is a whole int (the existing `_whole` rule: floats, bools and NaN raise `ValueError`/`TypeError` like `ctx.place`'s qty); `kind` ∈ `price` (paise) | `level` (units of 1/10,000, use `strategy.to_level`); `name` is 1–32 characters after `clean_note` and equal to it (else `ValueError`); `instrument` 0 means "the whole run", else it must be an instrument of the bundle (else `ValueError`). A series is `(instrument, name)`; its kind is fixed by its first call, a later different kind raises `ValueError`.
- A call stamps the engine's current event time `t_ns`; a repeat for the same series at the same `t_ns` replaces the earlier value (last wins, the point count is unchanged). Caps are per run: at most **8 series** and **200,000 points**; beyond a cap the call is ignored and **one** line goes to `events_log.txt` per cap, exactly `plot limit reached: more than 8 series` / `plot limit reached: more than 200000 points`, stamped like any engine log line. `manifest.json` is untouched.
- `plots.csv` header `t_ns,instrument_id,name,kind,value`; rows sorted by `(t_ns, instrument_id, name)`; the file exists (header only) when nothing was plotted; names go through the CSV quoting of `artifacts._cell`. A paper session's plot buffer lives for the day's bundle and is not carried in `sod_state` (non-goal).

- [ ] **Step 1: Write the failing tests** (`test_plot.py`): `test_plot_rejects_floats_nan_and_bad_kinds`, `test_plot_name_rules` (empty, 33 characters, a newline), `test_same_t_last_value_wins`, `test_plots_csv_is_sorted_by_time_instrument_name`, `test_a_series_keeps_its_first_kind`, `test_ninth_series_and_point_200001_are_dropped_with_one_log_line` (a stub strategy; exactly one log line per cap even when called a thousand times; the first 200,000 points are kept), `test_plots_csv_is_a_deterministic_file_with_a_hash` (`determinism.json` has the key; a rerun is byte-identical), `test_plot_with_unknown_instrument_raises`, `test_engine_and_oracle_write_identical_plots_csv` (bar run and snapshot run, two instruments, both kinds, a replaced point and a cap hit; byte-identical files and identical `events_log.txt`), `test_run_without_plots_has_a_header_only_file`. Doctest: the caps.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** the Python side, then the C++ side with the same order of checks (skill `modern-cpp`; the buffer is a sorted `std::map` keyed by series with an `std::vector` of points per series, never an unordered container). All three `deterministic-files` lists change in this commit.
- [ ] **Step 4: Rebuild and run:** `.\run.ps1 -BuildEngine`; `tests\run_tests.py plot`, `engine_parity`, `golden`, `oracle`, `runs`, `howto` → PASS; blockwatch clean. A re-run of an old run (no `plots.csv` in its `determinism.json`) must still print the C2 "new column/file" message; extend `test_rerun_of_an_old_run_reports_the_new_column_not_a_failure` to cover `plots.csv` being new.
- [ ] **Step 5: D-row** (`plots.csv` is deterministic; int64 values; the 1/10,000 level unit; the caps, last-wins, the `instrument` keyword added to the design's signature so the file's `instrument_id` column has a source). **Commit.**

```bash
git add stockify/oracle/context.py stockify/oracle/runner.py stockify/oracle/artifacts.py engine/include/stocksim/engine.hpp engine/src/engine.cpp engine/src/artifacts.cpp engine/bindings/engine.cpp engine/tests/test_output.cpp docs/plan/A1_schemas.md docs/plan/A4_engine_semantics.md docs/plan/00_overview.md tests/stockify/test_engine_parity.py tests/stockify/test_plot.py tests/stockify/test_order_note.py docs/plan/M6_reporting_research.md
git commit -m "CH 6.4: ctx.plot and plots.csv, the same bytes from the oracle and the engine"
```

---

### Task C5: `metrics.fifo_trades`, the one FIFO loop (CH 6.5)

**After:** C4 (and M6 Task 5, which also edits `metrics.py`).

**Files:**
- Modify: `stockify/oracle/metrics.py` (the loop at ~lines 99–137 moves into a public function; `compute` calls it)
- Test: `tests/stockify/test_fifo_trades.py`

**Interfaces:**
- Produces:
  - `TradeChunk` (`NamedTuple`): `(close_t_ns: int, close_fill_id: int | None, close_order_id: int | None, instrument_id: int, product: str, open_side: str, qty: int, open_price: int, close_price: int, pnl: int)`. `close_fill_id`/`close_order_id` are `None` for an expiry-settlement close.
  - `fifo_trades(fills, closes, products) -> tuple[list[TradeChunk], int]` returning `(chunks, traded_value)`. `fills`: objects with `.fill_id, .order_id, .t_ns, .instrument_id, .side, .qty, .price` (the oracle's `Fill` and lightweight stand-ins both work); `closes`: `[(t, iid, product, side, units, price), …]` or `None`; `products: Mapping[int, str]` order id → product. The flow order and tie-break are exactly today's (`(t_ns, is_settle, n)`), `pnl = open_sign * k * (price - open_price)`, `traded_value` counts fills only.
- `compute` is unchanged in behaviour: `chunks` → `trades = [c.pnl for c in chunks]`.

- [ ] **Step 1: Write the failing tests:** `test_compute_is_byte_identical_to_the_stored_metrics` (every golden run's `metrics.json` produced by the old code, stored in the test as hashes captured before the edit in this step, equals the new one), `test_partial_exit_is_a_trade_for_the_closed_quantity`, `test_flip_closes_the_old_position_and_opens_a_new_one` (the closing chunk carries the order's fill id), `test_settlement_close_has_no_fill_id`, `test_fifo_order_across_two_opens`, `test_traded_value_ignores_settlements`, `test_chunks_sum_to_metrics_total_pnl` (property loop, 1,000 random fill sequences, seeded).
- [ ] **Step 2: Capture** the pre-change `metrics.json` hashes of the golden runs into the test fixture first, run to verify failure (function missing), then **Step 3: Implement** by moving the loop, nothing else. **Step 4: Run** `tests\run_tests.py fifo`, `metrics`, `golden`, `engine_parity` → PASS. Skill `dimensional-analysis` (report only) over the P&L arithmetic. **Step 5: Commit.**

```bash
git add stockify/oracle/metrics.py tests/stockify/test_fifo_trades.py docs/plan/M6_reporting_research.md
git commit -m "CH 6.5: the FIFO trade loop is a public function the chart shares with the metrics"
```

---

### Task C6: Real notes and plots in the Python strategies (CH 6.6)

**After:** C4.

**Files:**
- Modify: `stockify/strategies/buy_and_hold.py:34`, `sma_cross.py:56,58`, `opening_range_breakout.py:63,67,75,93`, `eod_short_straddle.py:85,108`, `_option_legs.py:140,157,170` (used by `short_straddle_stop.py` and `iron_fly.py`), `_script.py:46` (already forwards `**p`, so `note` works; add a test), `docs/plan/HOWTO_strategy.md` (a section "Say why: `note=` and `ctx.plot`" in §1 with two examples that `test_howto.py` extracts and runs)
- Test: `tests/stockify/test_strategy_notes.py`; `tests/stockify/test_howto.py` (extended automatically)

**Interfaces:**
- Consumes: `stockify.strategy.fmt_price`, `fmt_qty`, `to_level` (C1); `ctx.place(note=)`, `ctx.plot` (C2, C4).
- Produces the exact notes (the C++ twins of C7 must equal them; all numbers via `fmt_price`/`fmt_qty`, symbol from `ctx.resolve`'s own text param `p["symbol"]`):
  - `buy_and_hold`: entry `Bought {fmt_qty(qty)} {symbol} at the first bar and holding`.
  - `sma_cross`: entry `Bought {qty} {symbol}: the {fast}-bar average {fmt_price(fast)} rose above the {slow}-bar average {fmt_price(slow)}`; exit `Sold {qty}: the {fast}-bar average {…} fell below the {slow}-bar average {…}`; flip `Flipped to short {qty}: …`; plots `fast` and `slow` as `price` (`int(avg)`, truncation of a positive average), instrument = the traded one.
  - `opening_range_breakout`: entry `Bought {qty} {symbol}: price {close} broke above the opening range high of {hi} (first {minutes} minutes)`; short mirror; stop `Sold {qty}: stop-loss at {trigger} hit` (the note is on the SL-M order, written when placed: `Stop-loss for the {qty} bought: sells if price falls to {trigger}`; the owner's example "stop-loss at 2,401.00 hit" is the fill-time reading, so the card shows the order's note; the UI never rewrites it); exit `Closed {qty} at the {exit_hhmm} time exit`; plots `range_high`, `range_low` (price) from the first post-range bar of each day.
  - `eod_short_straddle`, `_option_legs` (`wing`, `entry`, `stop`, `time_exit`, `stop` in the other file): one sentence each with the strike, side and the level that decided it (`Sold {qty} {tradingsymbol}: {days}-day short straddle at the {fmt_price(atm)} strike`, `Bought back {qty}: stop at {fmt_price(trigger)}, {pct}% above the sale`, `Closed {qty} at the {hhmm} time exit`); the implementer reads the code and fixes the wording from its real parameters; the plan only fixes: every note names the quantity and the deciding number, is at most 120 characters in the shipped examples, and states a reason, not just a side.
- Skill `design:ux-copy` reviews every sentence (they are UI copy); applied findings are part of this task.

- [ ] **Step 1: Write the failing tests:** per strategy, a run on a fixture bundle from `tests/stockify/run_fixtures.py` with `test_<strategy>_notes_name_quantity_and_the_deciding_number` (the note of each order contains `fmt_qty(qty)` and the formatted trigger numbers, asserted on exact expected strings built from hand-computed fixture prices), `test_every_order_of_a_shipped_strategy_has_a_note` (a loop over the six strategies on their existing fixtures: no empty `note`, every note at most 200 characters), `test_sma_cross_plots_two_series_with_one_point_per_bar`, `test_orb_plots_range_levels_from_the_first_breakout_bar`, `test_script_strategy_forwards_note`, `test_strategy_guard_accepts_the_shipped_strategies` (the existing check still passes).
- [ ] **Step 2: Run to verify failure. Step 3: Implement. Step 4: Run** `tests\run_tests.py strategy_notes`, `howto`, `option_strategies`, `strategies`, `oracle` → PASS; existing golden-style assertions on orders by tag still pass (a note changes no fill). **Step 5: Commit.**

```bash
git add stockify/strategies/buy_and_hold.py stockify/strategies/sma_cross.py stockify/strategies/opening_range_breakout.py stockify/strategies/eod_short_straddle.py stockify/strategies/_option_legs.py docs/plan/HOWTO_strategy.md tests/stockify/test_strategy_notes.py docs/plan/M6_reporting_research.md
git commit -m "CH 6.6: the shipped Python strategies say why they trade and draw their own lines"
```

---

### Task C7: The same notes and plots in `orb_cpp` and `sma_cross_cpp` (CH 6.7)

**After:** C6 (the sentences are fixed there).

**Files:**
- Modify: `engine/strategies/orb_cpp.cpp:102-111` (the `place` helper gains a `note` argument), `engine/strategies/sma_cross_cpp.cpp:71-78`
- Test: `tests/stockify/test_engine_strategies.py` (extend the existing py-vs-cpp comparisons)

**Interfaces:**
- Consumes: `fmt_price`, `fmt_qty`, `to_level` (C1), `PlaceRequest::note`, `Context::plot` (C3, C4); the exact sentences of C6.

- [ ] **Step 1: Write the failing tests:** `test_orb_cpp_and_python_write_the_same_notes` (same bundle, `orders.csv` and `plots.csv` byte-identical between the Python strategy on the oracle, the Python strategy on the engine, and the C++ strategy on the engine, apart from whatever the existing test already allows for `tag`), `test_sma_cross_cpp_and_python_write_the_same_notes_and_plots`, `test_cpp_strategy_notes_are_cleaned_and_at_most_200_characters`.
- [ ] **Step 2: Run to verify failure. Step 3: Implement** (skill `modern-cpp`; `std::string` concatenation through a small local helper, `fmt_price` for every number). **Step 4: Rebuild and run** `.\run.ps1 -BuildEngine`; `tests\run_tests.py engine_strategies`, `engine_parity` → PASS. **Step 5: Commit.**

```bash
git add engine/strategies/orb_cpp.cpp engine/strategies/sma_cross_cpp.cpp tests/stockify/test_engine_strategies.py docs/plan/M6_reporting_research.md
git commit -m "CH 6.7: the C++ strategies write the same notes and plots as their Python twins"
```

---

### Task C8: Engine-side close: one `c-review`, the full suite, the reports (CH 6.8)

**After:** C7. No new feature code; only fixes the reviews demand.

**Files:**
- Modify: `docs/plan/RECON.md` (append `## 2026-11-12 — chart engine side (CH 6.8)`: what the code really looks like now), `docs/plan/M6_reporting_research.md` (ticks); fixes only where a review finds a defect

- [ ] **Step 1: `c-review`** over the combined C++ diff of C1–C7 (`git diff <BASE>..HEAD -- engine/`), findings fixed with a test each.
- [ ] **Step 2: `differential-review`** over C1–C7 (strategy API `place(note=)`, `plot`, the guard allowlist unchanged, `system_notes` read from a bundle: untrusted text goes through `clean_note` before it reaches a file); `sharp-edges` and `insecure-defaults` (report only) over `ctx.plot`, `note` and `system_notes.json`.
- [ ] **Step 3: Full suite** in the background: `.\.venv\Scripts\python.exe tests\run_tests.py` (exit 0; not 2, not 3), `ctest --preset win-msvc-release` green, `~/.tools/blockwatch/blockwatch.exe` clean.
- [ ] **Step 4:** record in RECON that `.\run.ps1 -BuildEngine` is required on every machine before a run is compared, and that C3/C4 changed hashes (the D-rows). **Commit.**

```bash
git add docs/plan/RECON.md docs/plan/M6_reporting_research.md
git commit -m "CH 6.8: chart engine side reviewed and recorded"
```

---

### Task C9: Display bars from packs and journals: shared bucketing (CH 6.9)

**After:** C7.

**Files:**
- Create: `stockify/oracle/bars.py`, `stockify/reporting/chartbars.py`, `tests/stockify/charts/__init__.py`, `tests/stockify/charts/test_chartbars.py`
- Modify: `stockify/oracle/runner.py:1175` (`_build_bars` calls the two shared helpers; **no behaviour change**), `docs/plan/00_overview.md`

**Interfaces:**
- Produces, `stockify.oracle.bars` (the bucketing of A4 §2.2, pure): `bucket_start(anchor_ns: int, t_ns: int, interval_s: int) -> int` (`anchor + (t - anchor) // step * step`, step in ns) and `volume_delta(vol: int | None, prev_vol: int | None) -> int` (exactly the runner's `dv` expression: first snapshot contributes its whole day volume, a decrease contributes 0, `None` contributes 0).
- Produces, `stockify.reporting.chartbars`:
  - `Bar` (`NamedTuple`): `(start_ns, open, high, low, close, volume)` in paise and units.
  - `bars_from_records(records: Iterable[dict], instrument_id: int, *, interval_s: int | None, anchors: Mapping[str, int]) -> tuple[list[Bar], int | None]`: `records` are decoded pack or journal records (`packs.decode` dicts). If the stream holds `BAR` records for the instrument, they are returned as they are (the exact bars the strategy received; `interval_s` None picks the smallest interval present, a daily-only run gives daily bars) and the second value is that interval. Otherwise display bars are built from `TICK_*` and `INDEX` snapshots with `bucket_start`/`volume_delta`, anchored at `anchors[ist_date]` (the group's CONTINUOUS start that day), at `interval_s` or 60; a bucket with no snapshots produces no bar (I-7); built and real bars never mix (ST 5.9: real bars win).
  - `anchors_from_bundle(bundle: dict, instrument_id: int) -> dict[str, int]`: IST date → CONTINUOUS start of the instrument's session group (the runner's `continuous_start`, over `bundle["days"]`, `prev_days`, `next_days`).
  - `records_of_pack(sha256: str) -> list[dict]`; `records_of_journal(path) -> list[dict]` (whole records only; a torn tail is ignored, as `journal.read` does).

- [ ] **Step 1: Write the failing tests:** `test_bucket_start_hand_cases` (anchor 09:15 IST, 60 s, 300 s, a time on the boundary, before the anchor is not asked), `test_volume_delta_hand_cases`, `test_runner_refactor_changes_no_bytes` (golden tick-run artifacts hash-equal before and after: the hashes captured first), `test_display_bars_equal_the_runners_built_bars` (a tick golden run subscribed at 60 s: `bars_from_records(pack records)` equals the engine's `bar_history` for the instrument; both runners), `test_real_bars_win_over_snapshots`, `test_bar_pack_returns_the_exact_bars_and_their_interval`, `test_daily_only_run_gives_daily_bars`, `test_empty_bucket_makes_no_bar`, `test_property_bars_are_ordered_and_ohlc_consistent` (seeded loop: `low <= open,close <= high`, strictly rising `start_ns`, volume sum equals the sum of `volume_delta`s).
- [ ] **Step 2: Run to verify failure. Step 3: Implement.** **Step 4: Run** `tests\run_tests.py chartbars`, `oracle`, `engine_parity`, `golden` → PASS. **Step 5: D-row** (the chart's bars for a tick dataset are built by the helpers the runner also calls; the C++ engine keeps its own bucketing and parity against `bar_history` is the proof). **Commit.**

```bash
git add stockify/oracle/bars.py stockify/oracle/runner.py stockify/reporting/chartbars.py docs/plan/00_overview.md docs/plan/M6_reporting_research.md tests/stockify/charts/__init__.py tests/stockify/charts/test_chartbars.py
git commit -m "CH 6.9: the bars a run saw, read back from its own pack with the runner's own bucketing"
```

---

### Task C10: The run chart model (CH 6.10)

**After:** C5 and C9.

**Files:**
- Create: `stockify/reporting/chart.py`, `stockify/reporting/settlements.py`, `tests/stockify/charts/test_chart_model.py`
- Modify: `docs/plan/00_overview.md`

**Interfaces:**
- Consumes: `chartbars.*` (C9), `metrics.fifo_trades` (C5), `store.run_rows` / the run folder's `orders.csv`, `fills.csv`, `charges.csv`, `plots.csv`, `bundle.json`, `paths.run_path`, `packs.pack_path`.
- Produces: `MAX_BARS = 50_000`.
  - `Marker` (dict, all ints paise/units/ns): `{t_ns, bar_start_ns, side, qty, price_paise, fill_id, order_id, note, tag, tag_is_code, charges_paise | None, trade_pnl_paise | None, chunks: [{qty, open_price_paise, pnl_paise}]}`.
  - `run_chart(run_id: str, instrument_id: int | None, from_ns: int | None, to_ns: int | None) -> dict` returning `{instruments: [{id, symbol}], instrument_id, basis, interval_s, bars: [Bar…], markers, plots: [{name, kind, instrument_id, points: [[t_ns, value]]}], truncated, next_from_ns, data_state: "ok" | "pruned" | "no_pack"}`. `basis` is the pack header's `price_basis`. `instrument_id` None = the first traded instrument (lowest id among fills, else the first instrument of the bundle).
  - Rules: a marker's `bar_start_ns` is the start of the bar containing `t_ns`, or the last bar before it when none contains it (an `eod_close` fill after the last bar); a fill before the first bar or outside the requested range is not returned and is counted in `omitted_markers`. Markers are ordered by `(t_ns, fill_id)`. A marker's note: the order's `note`; empty note → the parent order's note; still empty → `""` with `tag` filled and `tag_is_code: true`; the UI writes the fallback sentence. Costs: the `charges.csv` rows of `kind = "order"` for the order, shown once on the order's **last** fill (by `fill_id`), `None` on the others. `trade_pnl_paise` and `chunks`: from `fifo_trades(fills, None, products)` over **all** the run's fills (never only the visible range), summed per `close_fill_id`; `None` for an opening fill. **Settlement markers (controller ruling, D-45):** each expiry settlement of the run (cash: the `settled … at FSP` lines of `events_log.txt`; physical: the `delivered` lines, the same two patterns `server/routers/tests.py` `_SETTLED`/`_DELIVERED` use, moved by this task into `stockify/reporting/settlements.py::read_settlements(folder) -> list[Settlement]` with `Settlement(t_ns, instrument_id, product, price_paise, kind, side | None, qty | None, source)`, and the router calls it) becomes a marker with `kind: "settlement"` (fills have `kind: "fill"`), `side` = the closing side, `qty` = the units closed, `price_paise` = the settlement price, `fill_id` and `order_id` `None`, `note` = `system_notes["SETTLEMENT"]` with `{price}` filled by `fmt_price` (read from the run's `bundle.json` `system_notes`, else `stockify/config/system_notes.json`; `tag` `""`, `tag_is_code` false), `charges_paise` = the `charges.csv` rows of `kind = "settlement"` for that instrument and trade date, and it snaps to the bar containing `t_ns` like any marker (a daily run: the settlement day's bar). Cash-settled units are not in the log line, so the closing units and side are recovered by FIFO over the run's fills up to `t_ns` (the open lots of that `(instrument, product)`); a settlement with nothing open is skipped. The synthesised closes `[(t, iid, product, closing_side, units, price)]` are passed as `closes` to `fifo_trades(fills, closes, products)`, so a settlement marker's `trade_pnl_paise` and `chunks` come from the same loop as `metrics.json` (`close_fill_id` `None` is matched to its settlement by `(close_t_ns, instrument_id)`). Markers sort by `(t_ns, kind == "settlement", fill_id or 0)`.
  - Paging: bars sorted; at most `MAX_BARS` per call; with no range the call returns the window around the first marker (or the last 5,000 bars when there is none); with a range the first `MAX_BARS` bars whose `start_ns` is in `[from_ns, to_ns)`; `truncated` is true and `next_from_ns` is the next bar's `start_ns` when more remain. A pack that is gone → `data_state: "pruned"`, `bars: []`, markers returned with `bar_start_ns = t_ns` rounded down to the minute. A bundle-less or pack-less folder → `"no_pack"`.
  - Old folders: an `orders.csv` without `note` → `note: ""`; no `plots.csv` → `plots: []`.

- [ ] **Step 1: Write the failing tests:** `test_marker_snaps_to_the_bar_containing_the_fill` (a fill on a boundary belongs to the later bar; an `eod_close` fill after the last bar goes to the last bar), `test_two_fills_in_one_bar_are_both_marked_in_fill_order`, `test_expiry_settlement_gets_a_settlement_marker_with_note_pnl_and_charges` (a short option held to expiry on a golden F&O run: one `kind: "settlement"` marker on the settlement bar, note "Settled at expiry at {fmt_price}", `trade_pnl_paise` equal to the settlement chunk of `fifo_trades`, charges from the `settlement` row; and `sum` of all chunks equals `metrics.json` total P&L), `test_settlement_with_nothing_open_is_skipped`, `test_note_falls_back_to_the_parent_then_to_the_tag`, `test_charges_shown_once_on_the_last_fill_of_a_multi_fill_order`, `test_trade_pnl_for_a_partial_exit_flip_and_full_exit` (hand-computed paise), `test_pnl_uses_all_fills_even_when_the_range_hides_the_opening_fill`, `test_no_trades_run_still_has_bars`, `test_old_orders_csv_without_note_reads_as_empty_notes`, `test_pruned_pack_gives_markers_on_an_empty_axis`, `test_basis_comes_from_the_pack_not_from_a_refetch` (the test deletes `bars.sqlite` access: a guard patches `store`/`history` reads to fail), `test_instrument_picker_lists_every_traded_instrument_including_fo_legs`, `test_pages_join_without_a_gap_or_a_duplicate_bar` (60,000 synthetic minute bars: two pages of 50,000 and 10,000, `next_from_ns` exact), `test_default_window_is_around_the_first_trade`, `test_unknown_run_raises_a_lookup_error`, and a property loop `test_markers_are_a_subset_of_fills_and_sorted` (seeded).
- [ ] **Step 2: Run to verify failure. Step 3: Implement. Step 4: Run** `tests\run_tests.py chart_model` → PASS; skill `dimensional-analysis` (report only) over the paise/units joins. **Step 5: D-row** (what a marker and a trade are, the costs rule, settlement markers from the run's own settlement lines, paging contract). **Commit.**

```bash
git add stockify/reporting/chart.py stockify/reporting/settlements.py docs/plan/00_overview.md docs/plan/M6_reporting_research.md tests/stockify/charts/test_chart_model.py
git commit -m "CH 6.10: a run's chart model: its own bars, a marker per fill with the note, costs and trade profit, and its plotted lines"
```

---

### Task C11: `GET /api/tests/{run_id}/chart` and the DTOs (CH 6.11)

**After:** M6 Task 9 and C10.

**Files:**
- Modify: `server/routers/tests.py` (the route; `_SETTLED`/`_DELIVERED` parsing now calls `stockify/reporting/settlements.py` from C10, behaviour unchanged, `test_settlements_endpoint_is_unchanged` guards it; declared with the other `/api/tests/{run_id}/…` routes, `def` not `async`), `server/schemas.py`, `web/src/lib/types.ts` (the blockwatch pair, appended in the same order), `docs/plan/A2_interfaces.md` §6
- Test: `tests/stockify/test_api_chart.py`

**Interfaces:**
- Consumes: `chart.run_chart` (C10).
- Produces: `GET /api/tests/{run_id}/chart?instrument=&from_ns=&to_ns=` → `RunChartDTO`: `{instruments: [{id, symbol}], instrument_id, basis, interval_s, bars: [{t, d, open, high, low, close, volume}], markers: [MarkerDTO], plots: [{name, kind, instrument_id, points: [{t, value}]}], truncated, next_from_ns, data_state, omitted_markers, plain}`. `t` = UTC seconds (an int; the client shifts by +19,800 s to show IST), `d` = the IST date `YYYY-MM-DD` (daily bars use it); `open/high/low/close` and `price` fields are **rupee decimal strings** from paise (`"2418.40"`, via `stockify.numerics`/`Decimal`, exact); plot `price` series likewise, `level` series as plain decimal strings of `value / 10000`. `MarkerDTO`: `{kind ("fill" | "settlement"), t, bar_t, side, qty, price, price_paise, order_id, fill_id, note, tag, tag_is_code, charges_paise, trade_pnl_paise, chunks}` (paise ints stay ints). `order_id` and `fill_id` are null for a settlement marker. Query validation: `instrument` int ≥ 0 else 422, `from_ns`/`to_ns` ints ≥ 0 with `from_ns < to_ns` else 422. 404 for an unknown run; 200 with `data_state` for a pruned run.

- [ ] **Step 1: Write the failing tests** (TestClient as in `test_api_tests.py`): `test_chart_returns_bars_markers_and_plots`, `test_prices_are_exact_rupee_strings` (paise 241840 → `"2418.40"`; no float anywhere), `test_range_request_matches_the_whole_run_slice`, `test_chart_404_for_unknown_run`, `test_chart_for_a_pruned_run_is_200_with_data_state`, `test_bad_query_is_422` (negative, text, from ≥ to), `test_responses_have_no_nan`, `test_non_loopback_host_refused` (D-77), `test_note_is_returned_verbatim_not_escaped` (the API returns text; escaping is the client's job), `test_run_id_cannot_escape_the_runs_folder` (`..\..\x`).
- [ ] **Step 2–4:** fail, implement, `tests\run_tests.py api_chart api_tests` PASS, `~/.tools/blockwatch/blockwatch.exe`. Skills `sharp-edges`, `insecure-defaults` (report only). **Step 5: Commit.**

```bash
git add server/routers/tests.py server/schemas.py web/src/lib/types.ts docs/plan/A2_interfaces.md docs/plan/M6_reporting_research.md tests/stockify/test_api_chart.py
git commit -m "CH 6.11: API for a run's chart"
```

---

### Task C12: Walk-forward chart endpoint (CH 6.12)

**After:** M6 Task 22 and C10.

**Files:**
- Create: `stockify/reporting/chart_wf.py`, `tests/stockify/charts/test_chart_wf.py`
- Modify: `server/routers/research.py`, `server/schemas.py`, `web/src/lib/types.ts`

**Interfaces:**
- Consumes: the job's `state_json` window list (M6 Task 20: `WindowResult.test_run_id`, window dates), `chart.run_chart` (C10).
- Produces: `walkforward_chart(job_id: str, instrument_id: int | None, from_ns: int | None, to_ns: int | None) -> dict` = a `run_chart`-shaped model assembled from every window's `oos` test run: bars of all windows concatenated in time (windows never overlap, M6 mismatch row 10), every fill of every test run as a marker, plus `windows: [{index, from_ns, to_ns, run_id | None, status}]` (a window whose test run failed or has no folder is listed with its status and no bars) for the shading. Markers keep each run's own FIFO trade P&L (a test run starts flat). `GET /api/research/walkforward/{job_id}/chart?instrument=&from_ns=&to_ns=` → `WalkforwardChartDTO` (the C11 DTO plus `windows`); 404 unknown job.

- [ ] **Step 1: Write the failing tests:** `test_stitched_bars_are_ordered_and_windows_do_not_overlap`, `test_markers_come_only_from_oos_runs` (a `trial` training run's fills are never drawn), `test_failed_window_is_listed_not_hidden`, `test_window_bounds_match_the_state_json`, `test_walkforward_chart_404`, `test_pages_join_across_a_window_seam` (a range cut at a window boundary), `test_non_loopback_host_refused`.
- [ ] **Step 2–5:** fail, implement, run `tests\run_tests.py chart_wf api_research`, blockwatch, skills `sharp-edges`/`insecure-defaults` (report only), **Commit.**

```bash
git add stockify/reporting/chart_wf.py server/routers/research.py server/schemas.py web/src/lib/types.ts docs/plan/M6_reporting_research.md tests/stockify/charts/test_chart_wf.py
git commit -m "CH 6.12: API for a walk-forward's out-of-sample chart"
```

---

### Task C13: Paper-session chart endpoint (CH 6.13)

**After:** C9 and C10.

**Files:**
- Create: `stockify/reporting/chart_session.py`, `tests/stockify/charts/test_chart_session.py`
- Modify: `server/routers/trade.py` (next to `session_fills`, line 393), `server/schemas.py`, `web/src/lib/types.ts`

**Interfaces:**
- Consumes: `paths.journal_file(session_id, day)`, `paths.day_bundle_file(session_id, day)`, `chartbars.records_of_journal`, `store.session_rows(session_id, "orders"|"fills"|"charges", trade_date=…)`, `store.session_days`, `fifo_trades`.
- Produces: `session_chart(session_id: str, trade_date: str | None, instrument_id: int | None) -> dict`, the C10 shape plus `trade_date`, `days: [str]` (for the day picker), `paper: true`, `live: bool` (the day is open). `GET /api/trade/sessions/{session_id}/chart?instrument=&trade_date=` → `SessionChartDTO`. A day with no journal → `data_state: "no_pack"`. Bars come from the day's journal snapshots, the paper fills and notes from the session's own order rows (`note` default `""` for a day finalised before C2). Trade P&L: FIFO over the **whole session's** fills up to the day (earlier days' fills first), so a position carried overnight closes with the right P&L. **Incremental build:** a process-level cache keyed by `(journal path)` holds `(decoded byte offset, accumulated bars)` and decodes only the new whole records on each call (bounded to 8 entries, least recently used), so a 5 s poll costs the new bytes only; a shorter file than the cache's offset resets it.

- [ ] **Step 1: Write the failing tests:** `test_session_chart_builds_bars_from_the_days_journal`, `test_second_call_decodes_only_new_records` (a counter over `packs.decode`), `test_journal_that_shrinks_resets_the_cache`, `test_torn_tail_is_ignored`, `test_overnight_position_closes_with_the_right_pnl`, `test_old_day_without_note_reads_empty`, `test_day_picker_lists_session_days`, `test_unknown_session_404_and_bad_date_422`, `test_non_loopback_host_refused`, `test_response_says_paper` (`paper` is true; `plain` says "Paper trading: no real orders").
- [ ] **Step 2–5:** fail, implement, `tests\run_tests.py chart_session api_trade`, blockwatch, skills `sharp-edges`/`insecure-defaults` (report only), **Commit.**

```bash
git add stockify/reporting/chart_session.py server/routers/trade.py server/schemas.py web/src/lib/types.ts docs/plan/M6_reporting_research.md tests/stockify/charts/test_chart_session.py
git commit -m "CH 6.13: API for a paper session day's chart, read incrementally from its journal"
```

---

### Task C14: The package, the pure chart helpers and the footer notice (CH 6.14)

**After:** M6 Task 29 and C11.

**Files:**
- Create: `web/src/lib/chart.ts`, `tests/stockify/chart_check.ts`, `tests/stockify/test_chart_checks.py`
- Modify: `web/package.json` (`"lightweight-charts": "5.3.0"` in `dependencies`, no caret), `web/package-lock.json` (via `npm install --save-exact lightweight-charts@5.3.0`, then `npm ci` to prove the lockfile), `web/src/app/layout.tsx` (the NOTICE line in the footer, after the existing sentence), `docs/plan/00_overview.md` (the D-row), `docs/plan/M6_reporting_research.md`

**Interfaces:**
- Produces, `web/src/lib/chart.ts` (pure, no DOM, no library import):
  - `toCandle(bar: ChartBar, daily: boolean): {time: number | string; open: number; high: number; low: number; close: number}` (`time` = `t + 19800` seconds for intraday, the `d` date string for daily; prices `Number(string)`).
  - `toVolume(bar, daily)`; `toMarker(m: ChartMarker, daily: boolean, tokens): {time; position: "belowBar" | "aboveBar"; shape: "arrowUp" | "arrowDown"; color: string; text: string; id: string}` (buy below the bar, sell above; a settlement marker is a square (`shape: "square"`, `position: "inBar"`, colour `--color-warn`) with the text `X 50`; the text is only `B 50` / `S 50`, never the note; colours from the passed token values).
  - `cardModel(m: ChartMarker): {title: string; reason: string; reasonIsCode: boolean; lines: {label: string; value: string}[]; pnl: {label: string; value: string; tone: "up" | "down" | "flat"} | null}`: `reason` = the note verbatim, or for an empty note `Sold 50 (no note from the strategy)` with `reasonIsCode` true and the tag shown as a code label; money via `rupees`/`rupeesWhole` from `format.ts`.
  - `mergePages(have: ChartBar[], page: ChartBar[]): ChartBar[]` (unique by `t`, sorted); `rangeAround(tNs: number, interval_s: number, bars: number): {from_ns; to_ns}`; `istShift = 19800`.
- The D-row: the owner approved `lightweight-charts` (exact) and `fancy-canvas@2.1.0` on 2026-11-12, and moved the pin from 5.2.1 to 5.3.0 on 2026-11-13 (D-110; no wait until 2026-11-24); the audit and the fresh advisory check of 5.3.0; attribution on every chart plus the footer NOTICE; the library is loaded client-side only.

- [ ] **Step 1: Read** `web/node_modules/next/dist/docs/` (client components, `next/dynamic`), `web/design.md`; invoke **Hallmark** (app-screen scope; the locked system applies) and name the files first.
- [ ] **Step 2: Write the failing checks** in `chart_check.ts` run by `test_chart_checks.py` (as `test_param_checks.py`; skipped with a clear message when `node` is missing): `toCandle` hand cases (a daily bar uses the date string; an intraday `t` gets +19,800), `toMarker` side → position and shape, `cardModel` for a note, for no note (fallback sentence, code label), for an opening fill (no P&L) and a loss (tone down), `test_card_text_is_never_html` (a note `<img src=x onerror=alert(1)>` comes back as the same string, and no helper returns a string containing markup other than that note), `test_merge_pages_keeps_bars_unique_and_sorted`, `rangeAround`.
- [ ] **Step 3: Install and implement:** `cd web; npm install --save-exact lightweight-charts@5.3.0`; check the lockfile's integrity for it starts `sha512-w16fu2/FteUkIJdg`, that `fancy-canvas` resolves to `2.1.0`, and that `npm ls --all` shows no other new package; `npm ci` from a clean `node_modules`. Add the NOTICE line to the footer.
- [ ] **Step 4: Verify:** `npm run lint; npm run build` pass; `tests\run_tests.py chart_checks` PASS; sloplint over `layout.tsx` no FAIL; skill `supply-chain-risk-auditor` over the lockfile diff (report in `.superpowers/audits/`, not committed by this task).
- [ ] **Step 5: Commit.**

```bash
git add web/package.json web/package-lock.json web/src/lib/chart.ts web/src/app/layout.tsx tests/stockify/chart_check.ts tests/stockify/test_chart_checks.py docs/plan/00_overview.md docs/plan/M6_reporting_research.md
git commit -m "CH 6.14: lightweight-charts pinned at 5.3.0, the pure chart helpers and the attribution notice"
```

---

### Task C15: The `PriceChart` component (CH 6.15)

**After:** C14.

**Files:**
- Create: `web/src/components/PriceChart.tsx` (`"use client"`), `web/src/components/PriceChartLazy.tsx` (the `next/dynamic(..., { ssr: false })` wrapper, itself `"use client"`), `web/src/lib/chart_api.ts`
- Modify: the CSS file the other components' classes live in (`web/src/app/base.css`; classes `stk-pricechart`, `stk-pricechart-card`), `web/src/lib/types.ts` only if C11 left a type out

**Interfaces:**
- Consumes: `chart.ts` (C14), the DTOs (C11–C13).
- Produces: `chart_api.ts`: `getRunChart(runId, opts)`, `getWalkforwardChart(jobId, opts)`, `getSessionChart(sessionId, opts)` (each `{instrument?, from_ns?, to_ns?, trade_date?}`, following `api.ts` conventions; a 404 gives `null`). Component props: `PriceChart({load: (range?: {from_ns: number; to_ns: number}) => Promise<ChartModel | null>; title: string; paperBadge?: boolean; pollMs?: number; windows?: {from_ns: number; to_ns: number; label: string}[]; focusMarkerId?: string; onMarkerClick?: (id: string) => void})`. Behaviour: candles with `--color-up`/`--color-down`, a volume histogram (on by default, in `--color-rule`), markers from `toMarker`, plot lines (`price` series on the price scale; `level` series on their own scale in a lower pane), crosshair, zoom and pan, `attributionLogo: true`; the marker card (a positioned `<div>` below the chart or beside it, rendered from `cardModel`, text only via React children, a button to close, Escape closes, the card also opens from the keyboard through a "Trades on this chart" list of buttons under the chart, so the chart is not the only way to read a reason); an instrument picker (`<select>`) when `instruments.length > 1`; the day picker for sessions; panning to the edge of the loaded range calls `load` for the neighbouring range and merges with `mergePages`; polling when `pollMs` is set, paused while the tab is hidden. The paper badge reads "Paper trading: no real orders". States: loading (`Skeleton`), empty ("This run made no trades" with the candles still drawn; "the price data for this run is no longer on disk" for `data_state` pruned, markers on an empty axis), error (message with a retry button), full. The chart is created in `useEffect` and removed in cleanup; colours read from the CSS variables at mount and on theme change.

- [ ] **Step 1: Read** the Next docs for `next/dynamic` and client components; invoke **Hallmark**; skill `dataviz` (colour and mark rules) read before the styling; name the files first.
- [ ] **Step 2: Write the failing checks:** extend `chart_check.ts` with `test_empty_and_error_states` (the pure `chartViewState(model | null | "error")` returns `loading | error | pruned | empty | full` for each input) and `test_marker_list_is_complete` (every marker has a list row).
- [ ] **Step 3: Implement.** **Step 4: Verify:** `npm run lint; npm run build`; sloplint over both components `--scope app`; skills `design:design-critique`, `design:accessibility-review` (the keyboard path to every reason, contrast of marker colours against `--color-paper`, the text list) and `design:ux-copy`, findings applied; the owner's look at 1024 px is a 🛑 recorded in RECON, not a blocker. **Step 5: Commit.**

```bash
git add web/src/components/PriceChart.tsx web/src/components/PriceChartLazy.tsx web/src/lib/chart_api.ts web/src/app/base.css web/src/lib/chart.ts tests/stockify/chart_check.ts docs/plan/M6_reporting_research.md
git commit -m "CH 6.15: the candlestick chart with buy and sell markers, reason cards and the strategy's own lines"
```

---

### Task C16: The run page (CH 6.16)

**After:** M6 Task 30 and C15.

**Files:**
- Modify: `web/src/app/tests/[id]/page.tsx` (it is a client page today, with `EquityChart` at line 209 and the Fills table at ~214; the chart goes in a new `Panel` above the trades/fills table, below the equity panel), `web/src/lib/tests.ts` (`getRunChart` re-export or use `chart_api`)

**Interfaces:**
- Consumes: `PriceChartLazy` (C15), `getRunChart` (C15).
- Produces: the chart panel titled "Price and trades"; the Fills table gains a "Why" column that shows the order's note (or the tag as a code label) so the table and the chart tell the same story; clicking a fill row calls `focusMarkerId` and scrolls the chart to it; clicking a marker highlights its row. Data: the fills list needs `note` and `tag` per fill, taken from the chart's markers by `fill_id` (no extra endpoint). A run in `queued`/`running`/`failed` status shows the chart region in its empty state with the status, not an error.

- [ ] **Step 1: Read** the page and the Next docs; invoke **Hallmark**; name the files first. **Step 2: Write the failing checks** in `chart_check.ts`: `test_marker_row_join` (a pure `joinFillsAndMarkers(fills, markers)` keeps one row per fill, the note when there is one). **Step 3: Implement.** **Step 4: Verify** `npm run lint; npm run build`; sloplint; `design:design-critique`, `design:accessibility-review`, `design:ux-copy`; the owner's look (🛑 note in RECON). **Step 5: Commit.**

```bash
git add "web/src/app/tests/[id]/page.tsx" web/src/lib/tests.ts web/src/lib/chart.ts tests/stockify/chart_check.ts docs/plan/M6_reporting_research.md
git commit -m "CH 6.16: the run page shows the chart, with a Why column that matches it"
```

---

### Task C17: The paper session page, live (CH 6.17)

**After:** C13 and C15.

**Files:**
- Modify: `web/src/app/trade/[id]/page.tsx` (client page; a new `Panel` near the orders and fills lists)

**Interfaces:**
- Consumes: `PriceChartLazy` with `pollMs={5000}` while the day is open (`live`) and `paperBadge`, `getSessionChart` (C15), `SessionChartDTO.days` for the picker.
- Produces: the session's chart for today by default, a day picker for earlier days, a visible "Paper trading: no real orders" badge on the chart, polling only while the market is open and the tab visible (no WebSocket). A session without a journal today shows the empty state "no recording for this day".

- [ ] **Step 1: Read** the page and docs; invoke **Hallmark**; name the files. **Step 2: Failing check:** `chart_check.ts` `test_poll_interval_only_while_live_and_visible` (a pure `pollDelay(live, hidden, ms)`). **Step 3: Implement. Step 4: Verify** as C16. **Step 5: Commit.**

```bash
git add "web/src/app/trade/[id]/page.tsx" web/src/lib/chart.ts tests/stockify/chart_check.ts docs/plan/M6_reporting_research.md
git commit -m "CH 6.17: the paper session page shows its live chart, marked as paper"
```

---

### Task C18: The walk-forward page (CH 6.18)

**After:** M6 Task 36, C12 and C15.

**Files:**
- Modify: `web/src/app/research/walkforward/[id]/page.tsx` (a server page; the chart is a client island through `PriceChartLazy`), `web/src/lib/research.ts` (a fetcher call if needed)

**Interfaces:**
- Consumes: `getWalkforwardChart` (C15) with the `windows` prop for shading; M6 Task 36's sections.
- Produces: under the stitched curve, a "Out-of-sample trades" chart: all test windows' bars with the shaded test windows (alternating `--color-paper-2` / `--color-paper-3` bands from the `windows` list, each labelled "Window 3 (out of sample)"), the stitched fills as markers. A failed window is shown as a labelled gap. Plain words, no Greek letters.

- [ ] **Step 1–5** as C16 (checks: `test_window_bands_from_windows` pure helper; skills; commit).

```bash
git add "web/src/app/research/walkforward/[id]/page.tsx" web/src/lib/chart.ts tests/stockify/chart_check.ts docs/plan/M6_reporting_research.md
git commit -m "CH 6.18: the walk-forward page shows its out-of-sample trades on the price, window by window"
```

---

### Task C19: The sweep page, the chart of the selected trial (CH 6.19)

**After:** M6 Task 35, C11 and C15.

**Files:**
- Modify: `web/src/app/research/sweeps/[id]/page.tsx` (server page; trial chosen by `searchParams` `trial=<run_id>`, a Promise in Next 16)

**Interfaces:**
- Consumes: the sweep results rows (each has its `run_id`, M6 Task 19), `getRunChart` (C15).
- Produces: a results-table row (and a heat-grid cell) links to `?trial=<run_id>`; the page shows the chart of that trial under the grid, titled "Trial: {parameters in words}"; no `trial` shows the best cell's run; an unknown or pruned trial shows the empty state, not an error; the chart is the run endpoint reused (no new API).

- [ ] **Step 1–5** as C16 (check: `test_trial_param_resolution` pure helper: missing, unknown, valid; commit).

```bash
git add "web/src/app/research/sweeps/[id]/page.tsx" web/src/lib/chart.ts tests/stockify/chart_check.ts docs/plan/M6_reporting_research.md
git commit -m "CH 6.19: the sweep page draws the chart of the trial you pick"
```

---

### Task C20: Chart close (CH 6.20)

**After:** C19.

**Files:**
- Modify: `docs/plan/RECON.md` (append `## 2026-11-12 — candlestick charts close (CH 6.20)`), `docs/plan/M6_reporting_research.md` (ticks), `docs/plan/HOWTO_strategy.md` only if a review finds the new section unclear

- [ ] **Step 1:** skill `spec-to-code-compliance` against `docs/superpowers/specs/2026-11-12-candlestick-charts-design.md` (each Goal, Anti-goal and "Settled" item: code and test named; divergences recorded as D-rows or fixed), `supply-chain-risk-auditor` (dependencies changed: re-run advisories for `lightweight-charts@5.3.0` and `fancy-canvas@2.1.0`), `differential-review` over the branch's chart commits.
- [ ] **Step 2:** full suite in the background (exit 0), `npm run lint; npm run build`, `ctest --preset win-msvc-release`, blockwatch clean; no `CLAUDE.md` staged anywhere.
- [ ] **Step 3: Record** the 🛑 owner steps: look at a run, a session, a walk-forward and a sweep trial at 1024 px; confirm the attribution reads acceptably; rule on the controller's calls listed in the spec and on this plan's gap rulings; decide when to take `5.3.0` (not before 2026-11-24). **Commit.**

```bash
git add docs/plan/RECON.md docs/plan/M6_reporting_research.md
git commit -m "CH 6.20: candlestick charts reviewed against the design and recorded"
```

---

## Self-review (done at writing time)

- **Spec coverage:** note, 200-character rule, `fmt_*`, shipped strategies, system notes, HOWTO → C1, C2, C3, C6, C7; plots, caps, last-wins, int64, deterministic file → C4; `fifo_trades` → C5; bars from the pack with the runner's bucketing, pruned runs, picker, daily → C9, C10; costs and trade P&L, trade definition, note inheritance, carried state, old runs and fixtures → C2, C3, C10; paging contract → C10, C11, C14, C15; walk-forward, sweep, session pages and APIs → C12, C13, C16–C19; package pin, attribution, client-only component, D-row → C14, C15; paper wording → C13, C15, C17. Non-goals (drawing tools, other symbols, new data) have no task by design.
- **Names checked across tasks:** `clean_note`, `fmt_price`, `fmt_qty`, `to_level` (C1) are used by C2, C3, C6, C7; `fifo_trades`/`TradeChunk` (C5) by C10, C13; `bars_from_records`, `anchors_from_bundle`, `records_of_pack`, `records_of_journal` (C9) by C10, C13; `run_chart` (C10) by C11, C12; `toCandle`, `toMarker`, `cardModel`, `mergePages` (C14) by C15–C19.

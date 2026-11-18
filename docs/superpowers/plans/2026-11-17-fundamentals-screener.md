# Fundamentals Screener Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Fundamentals page (linked at the top of Research) listing today's liquid Nifty 500 stocks that look cheap and healthy against their sector, best first, each with a few words of reason and a Details page of graphs and figures, an upcoming-results list on top, and an engine-run test record.

**Architecture:** A `stockify/fundamentals/` package downloads NSE's universe, sector classification, results filings (XBRL) and results calendar through the existing `nse_files.Http`, parses XBRL with the standard library into point-in-time figures (each usable from the trading day after NSE broadcast it), computes sector-relative ratios and an internal order, and builds a monthly ranking history that a "Fundamentals basket" strategy replays in the engine with walk-forward. A daily daemon job refreshes everything after the close; the API and two web pages present it.

**Tech Stack:** Python 3.12 stdlib (xml.etree, json), existing stockify data/research/engine modules, FastAPI, Next.js 16 + lightweight-charts 5.3.0 (installed). No new packages.

**Spec:** `docs/superpowers/specs/2026-11-17-fundamentals-screener-design.md` (owner-approved 2026-11-17; the "Owner's changes" and "Calls made after the builder check" sections override the earlier text).

## Global Constraints

- Windows only; desktop UI (readable to ~1024 px), Hallmark + web/design.md, ₹ via Intl en-IN, plain sentence-case copy, no em dashes in copy.
- Money int64 paise, quantities units, time UTC epoch ns, IST only at the edges. Filed amounts keep their scale (lakhs/crores/units) until converted exactly to paise.
- Universe: today's Nifty 500 whose median daily traded value over the last three months is ≥ ₹8 crore (config value `liquidity_floor_paise = 80000000000`). No delisted companies.
- Filings from 2021-04-01 onward. Each figure usable from the first trading day after NSE's broadcast (IST); restatements are new versions with their own usable day.
- Consolidated figures when the company files them (for its whole history), standalone otherwise; never mixed.
- The page shows no score, no rank number, and no "how the list is ordered" text (owner). Each row: symbol, sector, a few words of reason, Details.
- No scraping of third-party sites; NSE sources only (www.nseindia.com/api JSON with a cookie warm-up, nsearchives static files, niftyindices / nsearchives index files). Polite: the existing Http's ≥ 1 s between requests and retries; every raw response saved with its sha256.
- The research holdout (research.json `holdout_from` 2026-08-01) stays locked: tests end the day before it.
- No orders; the page says "worth a closer look", never "buy".
- All paths via stockify.paths; stores follow the project store pattern (migrations, dbconn.Pool, SandboxedDBTest); a new .sqlite is added to backup.py DATABASES.
- Tests: stdlib unittest, offline (real NSE samples saved as fixtures under tests/fixtures/fundamentals/), `import tests.support` first.
- server/schemas.py ↔ web/src/lib/types.ts dto-fields blocks change together; run ~/.tools/blockwatch/blockwatch.exe.
- Commits: owner's identity, subject `FS <task>.<n>: ...`, no co-author/tool trailers, never the word "agent", never stage CLAUDE.md.

## Review Focus

1. A filing whose XBRL is missing, malformed, in an unknown taxonomy, or whose quarters don't add up to the year: held back with a plain reason, the stock keeps its previous figures or drops out, never a crash or a guessed number (Task 3 tests).
2. NSE answering 403 / HTML instead of JSON (anti-bot) or timing out mid-backfill: the run stops cleanly, keeps what it saved, and resumes from the saved raw files next time (Task 2 test).
3. A stock split or bonus between filings: share counts and per-share figures change, but P/E and P/B computed from market cap stay continuous (Task 4 test).
4. A loss-making company or one with negative equity: no P/E (or P/B) value, flagged in its reasons, still placed by its other figures if it has four of six (Task 5 test).
5. A sector with fewer than five scored stocks: compared within its macro-economic sector instead, and the details page says which peer group was used (Task 5 test).

---

### Task 1: Fundamentals store, NSE JSON client, universe and sectors

**Files:**
- Create: `stockify/fundamentals/__init__.py`, `stockify/fundamentals/store.py`, `stockify/data/migrations/fundamentals/0001_init.sql`, `stockify/fundamentals/nse_api.py`, `stockify/fundamentals/universe.py`, `stockify/config/fundamentals.json`
- Modify: `stockify/paths.py` (`fundamentals_db()`, `fundamentals_raw_dir(kind)` under `$LAB/nse/raw/fundamentals/<kind>/`), `stockify/backup.py` (DATABASES gains `fundamentals.sqlite`; raw dir copied like nse/raw), config loader coverage
- Test: `tests/stockify/fundamentals/test_store.py`, `test_nse_api.py`, `test_universe.py`

**Interfaces:**
- Produces:
  - Tables: `universe(as_of TEXT, symbol TEXT, instrument_id INT, isin TEXT, company TEXT, median_value_paise INT, liquid INT, PRIMARY KEY(as_of, symbol))`; `sectors(symbol TEXT, as_of TEXT, macro TEXT, sector TEXT, industry TEXT, basic_industry TEXT, PRIMARY KEY(symbol, as_of))`; later tasks add their own migration files (0002+).
  - `nse_api.Client(http: nse_files.Http | None = None)` with `.json(path: str, params: dict, *, warmup: str) -> Any` (warms the cookie once per warm-up page, saves the raw body via `save_raw(kind, name, body) -> sha256`, raises `nse_api.Blocked` on 401/403 or an HTML body).
  - `universe.refresh(day: date, *, client: Client, today_list_csv: bytes | None = None) -> list[dict]`: reads today's Nifty 500 list (config `nifty500_list_url`), computes each symbol's median daily `turnover_paise` from refdata `cm_eod` over the last 63 trading days, writes `universe` rows with `liquid = median ≥ liquidity_floor_paise`; `universe.current() -> list[dict]` (latest as_of, liquid only).
  - `universe.refresh_sectors(symbols, day, *, client) -> int`: per symbol, `quote-equity?symbol=` → `industryInfo` {macro, sector, industry, basicIndustry}; writes `sectors`.
  - Config `fundamentals.json`: `nifty500_list_url`, `liquidity_floor_paise`, `liquidity_days` 63, `filings_from` "2021-04-01", `basket_size` 20, `top_kept` 50, API paths and warm-up pages.

- [ ] **Step 1: Failing tests** with saved fixture bodies: `test_blocked_html_raises`, `test_raw_body_saved_with_sha`, `test_liquidity_floor_from_cm_eod` (two symbols, median above/below ₹8 crore), `test_sectors_parsed_from_industry_info`, `test_store_migrates_and_backup_lists_it`.
- [ ] **Step 2:** `.\.venv\Scripts\python.exe tests\run_tests.py fundamentals` → FAIL.
- [ ] **Step 3:** Implement. Save one real `ind_nifty500list.csv` and two real `quote-equity` bodies as fixtures (fetched once by hand; note the date in the fixture folder's README).
- [ ] **Step 4:** Same command → PASS; `tests\run_tests.py test_backup`, `test_paths`, `config_coverage` → PASS.
- [ ] **Step 5:** Commit.

### Task 2: Filings list and XBRL download (resumable)

**Files:**
- Create: `stockify/fundamentals/filings.py`, `stockify/data/migrations/fundamentals/0002_filings.sql`
- Test: `tests/stockify/fundamentals/test_filings.py`

**Interfaces:**
- Consumes: Task 1 `Client`, `universe.current()`, paths.
- Produces:
  - Table `filings(filing_id TEXT PRIMARY KEY, symbol TEXT, period_end TEXT, period_kind TEXT  -- Q|H|A, basis TEXT  -- consolidated|standalone, broadcast_ns INT, xbrl_url TEXT, xbrl_sha256 TEXT, xbrl_file TEXT, status TEXT  -- listed|downloaded|parsed|held, reason TEXT)`; `filing_id` = sha256 of (symbol, period_end, basis, broadcast time, xbrl_url).
  - `filings.list_for(symbol: str, *, client, since: date) -> list[dict]` from the corporate financial-results API (quarterly, half-yearly, annual), broadcast time parsed as IST → UTC ns.
  - `filings.download_pending(*, client, limit: int | None = None, deadline_ns: int | None = None) -> dict` (`{"downloaded", "skipped", "failed"}`): fetches each `listed` filing's XBRL to `fundamentals_raw_dir("xbrl")/<YYYY>/<symbol>_<period_end>_<basis>_<short id>.xml` with atomic write + sha256 sidecar; skips files already on disk with a matching sha; stops cleanly on `Blocked` or the deadline.

- [ ] **Step 1: Failing tests** with fixture JSON: `test_list_parses_periods_basis_and_broadcast_ist`, `test_download_is_resumable` (second run downloads nothing), `test_blocked_stops_cleanly_and_keeps_progress` (Review Focus 2), `test_same_period_new_broadcast_is_a_new_filing`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS. **Step 5:** Commit.

### Task 3: XBRL parser with checks

**Files:**
- Create: `stockify/fundamentals/xbrl.py`, `stockify/fundamentals/taxonomy.json`, `stockify/data/migrations/fundamentals/0003_figures.sql`
- Test: `tests/stockify/fundamentals/test_xbrl.py`, fixtures: at least one real filing each for Ind AS non-bank quarterly (consolidated and standalone), an Ind AS half-year with balance sheet and cash flow, a bank, an NBFC; plus a deliberately broken copy.

**Interfaces:**
- Produces:
  - `FIELDS` (exact names): `revenue_ops, other_income, total_income, total_expenses, finance_costs, depreciation, profit_before_tax, tax, net_profit, profit_owners, eps_basic, eps_diluted, paid_up_equity, face_value, total_equity, borrowings_current, borrowings_noncurrent, total_assets, cash, operating_cash_flow, interest_earned, interest_expended` (banks/NBFCs fill the last two and may leave revenue_ops empty).
  - Table `figures(filing_id TEXT, field TEXT, value_paise INT, value_raw TEXT, PRIMARY KEY(filing_id, field))` (EPS stored in paise per share; face value in paise).
  - `xbrl.parse(data: bytes, *, period_kind: str, basis: str) -> ParseResult(fields: dict[str, int], taxonomy: str, problems: list[str])`: namespace-agnostic element lookup through `taxonomy.json` (element local-names per field per taxonomy family: indas, banking, nbfc), contexts filtered to the filing's own period and basis (no prior-period columns), scale from `decimals`/unit or the declared rounding (lakhs/crores) converted exactly.
  - `xbrl.check(fields, *, period_kind) -> list[str]`: e.g. "profit after tax differs from profit before tax minus tax by more than 1 %", "EPS times shares differs from profit by more than 2 %".
  - `xbrl.ingest_pending() -> dict`: parses `downloaded` filings; clean → figures + status `parsed`; problems → status `held` with the plain reason.

- [ ] **Step 1: Failing tests:** `test_indas_quarter_fields` (asserting exact values from the real fixture), `test_half_year_has_balance_sheet_and_cash_flow`, `test_bank_fields`, `test_scale_lakhs_to_paise_exact`, `test_prior_period_columns_ignored`, `test_broken_filing_is_held_with_reason` (Review Focus 1), `test_unknown_taxonomy_is_held`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS. **Step 5:** Commit.

### Task 4: Point-in-time figures

**Files:**
- Create: `stockify/fundamentals/pit.py`
- Test: `tests/stockify/fundamentals/test_pit.py`

**Interfaces:**
- Consumes: `filings`, `figures`, refdata calendar (`stockify.data.calendar` trading days), refdata `cm_eod` (raw closes).
- Produces:
  - `pit.basis_for(symbol) -> str` ("consolidated" if any consolidated filing exists, else "standalone"; fixed for all dates).
  - `pit.usable_day(broadcast_ns: int) -> date`: the first trading day after the broadcast's IST date.
  - `pit.known_on(symbol: str, day: date) -> dict`: the latest usable version per (period_end, period_kind) as of `day`, returning `{"quarters": [...latest 8 quarters, newest first], "half_years": [...], "annual": [...]}`, each with its fields and filing_id; quarters derived as H − Q for a missing Q2/Q4 only when both are filed with the same basis.
  - `pit.ttm(symbol, day, field) -> int | None`: sum of the latest four consecutive quarters (None if any is missing).
  - `pit.shares(symbol, day) -> int | None`: paid_up_equity ÷ face_value from the latest filing that has both, carried forward; falls back to NSE's issued size from the latest quote fixture/store when absent.
  - `pit.market_cap(symbol, day) -> int | None`: raw `cm_eod` close (series EQ) on `day` × shares.

- [ ] **Step 1: Failing tests:** `test_not_usable_before_the_day_after_broadcast`, `test_restatement_replaces_from_its_own_day`, `test_basis_fixed_per_company`, `test_ttm_needs_four_consecutive_quarters`, `test_split_keeps_market_cap_continuous` (Review Focus 3: shares double, close halves).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS. **Step 5:** Commit.

### Task 5: Ratios, sector comparison, order and reasons

**Files:**
- Create: `stockify/fundamentals/ratios.py`, `stockify/fundamentals/screen.py`, `stockify/data/migrations/fundamentals/0004_screen.sql`
- Test: `tests/stockify/fundamentals/test_ratios.py`, `test_screen.py`

**Interfaces:**
- Consumes: Task 4 functions, Task 1 `universe.current()` and `sectors`.
- Produces:
  - `ratios.for_stock(symbol, day) -> dict` with keys `pe, pb, roe, debt_equity, op_margin, profit_growth` (floats or None; financials: `pb, roe, nii_growth` and None for debt_equity/op_margin): P/E = market cap ÷ TTM profit_owners (None if ≤ 0, flagged "loss-making"); P/B = market cap ÷ latest total_equity (None if ≤ 0); ROE = TTM profit ÷ average of the last two total_equity values; D/E = (borrowings_current + borrowings_noncurrent) ÷ total_equity; op_margin = TTM (total_income − other_income − total_expenses + finance_costs + depreciation) ÷ TTM revenue_ops; profit_growth = latest quarter profit_owners ÷ same quarter a year earlier − 1.
  - `screen.order(day, *, balance: float = 0.5) -> list[dict]`: per liquid universe stock with ≥ 4 of 6 ratios: percentile within its sector (≥ 5 scored peers, else its macro sector), valuation = mean percentile of P/E and P/B (cheaper = higher), quality = mean of ROE, op_margin, profit_growth and inverted D/E; composite = balance × valuation + (1 − balance) × quality; returns rows ordered by composite (descending) with `{symbol, sector, peer_group, ratios, sector_medians, reasons: list[str], short_reason: str}`. The composite is internal: it never leaves this module's API except as the order.
  - `short_reason` (a few words, ≤ 90 chars, e.g. "Cheaper than its sector on earnings, with rising profits and little debt"); `reasons` add value-trap warnings (cheap P/E with falling profits or rising debt).
  - Table `screen_runs(day TEXT, position INT, symbol TEXT, json TEXT, PRIMARY KEY(day, position))`; `screen.save(day, rows)`, `screen.latest() -> (day, rows)`.

- [ ] **Step 1: Failing tests:** `test_ratio_formulas` (hand-computed values), `test_loss_maker_has_no_pe_and_is_flagged` (Review Focus 4), `test_small_sector_uses_macro_peers` (Review Focus 5), `test_needs_four_of_six`, `test_order_prefers_cheap_and_healthy`, `test_value_trap_reason`, `test_no_score_in_public_rows` (rows carry no composite/percentile keys).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS. **Step 5:** Commit.

### Task 6: Ranking history, the basket strategy and the test record

**Files:**
- Create: `stockify/fundamentals/history.py`, `stockify/strategies/fundamentals_basket.py`, `stockify/fundamentals/test_record.py`
- Modify: `stockify/strategies/_catalogue.py` (new PARAM_TYPE `ranking`: a JSON object {label: [{"date": "YYYY-MM-DD", "symbols": [..]}]}, validated for shape; not shown as an editable field), `web/src/components/ParamField.tsx` / `web/src/lib/params.ts` (a `ranking` param renders as "Set by the screener" read-only), `stockify/config/fundamentals.json` (balances `["0.3","0.5","0.7"]`, basket sizes `[10, 20, 30]`)
- Test: `tests/stockify/fundamentals/test_history.py`, `tests/stockify/strategies/test_fundamentals_basket.py`, `tests/stockify/fundamentals/test_test_record.py`

**Interfaces:**
- Consumes: Task 5 `screen.order`, Task 4 (point in time), `stockify.data.history.fetch/bars` (Kite adjusted daily candles), `stockify.research.walkforward`, holdout config.
- Produces:
  - `history.rankings(start: date, end: date) -> dict[str, list[dict]]`: for each balance label, on the first trading day of each month, `screen.order(day, balance=b)` using only figures usable on that day, keeping the top `top_kept` (50) symbols.
  - Strategy `FundamentalsBasket` (DISPLAY_NAME "Fundamentals basket", DESCRIPTION "Holds the best-placed stocks from the fundamentals screen, rebalanced each month."): params `ranking` (type ranking), `balance` (str, one of the labels), `basket_size` (int 5–50); on each month's first daily bar it targets equal weight across the first `basket_size` symbols of that month's list for `balance` (CNC), selling what dropped out; symbols not in the bundle are skipped and logged.
  - `test_record.run(*, end: date) -> dict`: fetches daily bars for every symbol in the rankings plus the benchmark, builds the base run config (bars pack of the union of symbols, digest-style fill model and Zerodha costs), runs the existing walk-forward over `balance × basket_size` (train 365 d, test 90 d, step 90 d) from 2022-01-01 to `end` = holdout_from − 1 day, and stores the result id; benchmark Nifty 500 total-return from NSE Indices history if downloadable, else Kite's "NIFTY 500" index candles labelled "price index, without dividends".

- [ ] **Step 1: Failing tests:** `test_rankings_use_only_figures_known_then` (a filing broadcast after a rebalance day does not affect it), `test_basket_rebalances_monthly_equal_weight` (synthetic 3-stock world through oracle run_bundle, as tests/stockify/strategies/test_swing_strategies.py does), `test_ranking_param_type_validates_shape`, `test_test_record_ends_before_the_holdout`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run `tests\run_tests.py fundamentals`, `tests\run_tests.py test_display`, `tests\run_tests.py strategies` → PASS; `cd web; npm run lint; npx tsc --noEmit -p .` clean. **Step 5:** Commit.

### Task 7: Upcoming results calendar

**Files:**
- Create: `stockify/fundamentals/calendar.py`, `stockify/data/migrations/fundamentals/0005_calendar.sql`
- Test: `tests/stockify/fundamentals/test_calendar.py`

**Interfaces:**
- Consumes: Task 1 `Client`, `universe.current()`.
- Produces: table `results_calendar(symbol TEXT, meeting_date TEXT, purpose TEXT, period_hint TEXT, read_on TEXT, PRIMARY KEY(symbol, meeting_date))`; `calendar.refresh(day, *, client) -> int` reads NSE's board-meetings / event-calendar API for the next 28 days, keeps universe symbols whose purpose mentions financial results, derives `period_hint` ("Jul–Sep quarter") from the meeting month; `calendar.upcoming(day, days: int = 28) -> list[dict]` ordered by date. It runs with the daily job (the owner asked for weekly; daily is a superset and costs one or two requests).

- [ ] **Step 1: Failing tests** (fixture JSON): `test_only_results_meetings_of_universe_stocks`, `test_period_hint_from_month`, `test_past_meetings_drop_off_upcoming`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS. **Step 5:** Commit.

### Task 8: Daily daemon job and the backfill command

**Files:**
- Create: `stockify/fundamentals/job.py`
- Modify: `stockify/config/schedule.json` (`"fundamentals": {"start": "20:40", "retry_every_min": 30, "until": "23:30"}`), `stockify/daemon/scheduler.py` (DayPlan, plan, _due_ns, window_end_ns, CATCH_UP, DAY_ORDER after desk, DEPENDS on nse_fetch, HELD_DURING_SESSION, docstring table), `stockify/daemon/__init__.py` (`elif job == "fundamentals"`), `stockify/__main__.py` (`stockify fundamentals backfill [--from 2021-04-01] [--limit N]` and `stockify fundamentals refresh`)
- Test: `tests/stockify/fundamentals/test_job.py`, `tests/stockify/daemon/test_scheduler_m5.py`

**Interfaces:**
- Consumes: Tasks 1–7.
- Produces: `job.run(day: date, *, client=None, deadline_ns: int | None = None) -> dict`: Fridays (or when the universe is older than 7 days) refresh universe + sectors; every day: calendar refresh, filings list for symbols whose results date has passed since their last filing (or all symbols weekly), download + ingest pending, `screen.save(day, screen.order(day))`; stops at the deadline = 10 minutes before the earlier of refdata/day_bundles (as the desk job does) and before any NSE call after it; `Blocked` → log and return unmarked (retried). The backfill command runs the same steps over all universe symbols from `filings_from`, resumable, printing progress, and also runs `stockify nse backfill` for `cm` from 2021-04-01 (raw closes) if cm_eod lacks those days.

- [ ] **Step 1: Failing tests:** `test_job_skips_nse_calls_after_deadline`, `test_blocked_is_retried_not_marked`, `test_job_saves_todays_order`, `test_fundamentals_job_held_during_session_after_nse_fetch` (scheduler).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run `tests\run_tests.py fundamentals`, `scheduler`, `config_coverage` → PASS. **Step 5:** Commit.

### Task 9: API

**Files:**
- Create: `server/routers/fundamentals.py`
- Modify: `server/main.py`, `server/schemas.py` and `web/src/lib/types.ts` (dto-fields blocks)
- Test: `tests/stockify/fundamentals/test_fundamentals_api.py`

**Interfaces:**
- Produces:
  - `GET /api/fundamentals` → `FundamentalsListDTO{as_of, upcoming: [ResultsDateDTO{symbol, company, date, period_hint}], items: [CandidateDTO{position, symbol, company, sector, short_reason}], held_count, test_record_id | null, notes: [str]}` (`position` orders the list; the page does not print it).
  - `GET /api/fundamentals/{symbol}` → `CandidateDetailDTO{symbol, company, sector, peer_group, reasons, ratios: [RatioDTO{key, label, value, sector_median, unit}], series: [SeriesDTO{key, label, unit, points: [{period_end, value, sector_median}]}], filings: [FilingDTO{period_end, period_kind, basis, broadcast_ist, xbrl_url, status, reason}], as_of}`; 404 for a symbol outside the universe; a held or not-yet-scored stock returns its filings and the reason it is not listed.
  - `GET /api/fundamentals/test-record` → the walk-forward summary (existing research DTOs where possible) plus the survivorship and classification notes.
  - No score, percentile or composite field in any DTO.

- [ ] **Step 1: Failing tests:** `test_list_shape_and_order`, `test_detail_series_and_filings`, `test_no_score_fields_anywhere`, `test_unknown_symbol_404`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS; blockwatch clean. **Step 5:** Commit.

### Task 10: Web: the Fundamentals page and the details page

**Files:**
- Create: `web/src/app/research/fundamentals/page.tsx`, `web/src/app/research/fundamentals/[symbol]/page.tsx`, components beside them (`UpcomingResults.tsx`, `CandidateList.tsx`, `FigureCharts.tsx`), `web/src/lib/fundamentals.ts`
- Modify: `web/src/app/research/page.tsx` (a link card at the top: "Fundamentals: stocks worth a closer look, and results coming up")

**Interfaces:**
- Consumes: Task 9 endpoints and types; `@/components/ui`; lightweight-charts via the existing chart wrapper pattern (line series for a figure vs its sector median; no candles needed).
- Produces: the Fundamentals page: PageHead ("Fundamentals", lede "Stocks that look cheap and healthy against their sector. Ideas to research, not orders."), "Results coming up" (date, symbol, company, quarter), then "Worth a closer look" (best first; each row: SYMBOL, sector, short reason, a Details button), a short link to the test record, a fine-print line with the two limits (today's listed companies only; today's sector classification). The details page: name and sector, the reasons, a ratio table (value vs sector median), one small chart per figure over time vs its sector median, the filings list with dates and links to NSE, and the peer group used. No score, rank number or method text anywhere.

- [ ] **Step 1:** Build against a sandboxed API with fixture data (as the desk's Task 10 did: worktree API and web on spare ports, state sandboxed, no Kite).
- [ ] **Step 2:** `cd web; npm run lint; npx tsc --noEmit -p .; npm run build` → clean.
- [ ] **Step 3:** Screenshots of both pages at 1440 and 1024 px with headless Edge, checked against web/design.md and Hallmark.
- [ ] **Step 4:** Commit.

### Task 11: Docs

**Files:**
- Modify: `README.md` (a "Fundamentals" section: what it shows, sources, the two limits, never "buy"), `docs/plan/00_overview.md` (D-153, CRLF kept), `docs/plan/A1_schemas.md` §7.8 and `docs/plan/M5_paper_daemon.md` (the fundamentals job), `docs/plan/RECON.md` (stockify/fundamentals/).

- [ ] **Step 1:** Write (plain, no em dashes).
- [ ] **Step 2:** Run the full suite in the background → 0 failures beyond tracked defects.
- [ ] **Step 3:** Commit.

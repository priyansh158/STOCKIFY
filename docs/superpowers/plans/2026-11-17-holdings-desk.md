# Holdings Desk Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A page per Kite holding with a live chart, the option chain, nine swing strategies' current signals graded by their record on that stock, one rule-based exit verdict, and priced option ideas; plus a verdict chip on the Portfolio list.

**Architecture:** Seven new ordinary strategies share a small indicator module. A desk package (`stockify/desk/`) replays any strategy's own `on_bar` through a light context to get "what does it say now", grades each strategy nightly from oracle backtests split into yearly windows, combines both into a verdict, and reads/prices the option chain. The daemon runs the nightly evidence job after the close; the API serves the desk and polls the chain once a minute during the session; the web adds `/portfolio/[symbol]` and a chip on the list.

**Tech Stack:** Python 3.12 stdlib + existing stockify modules, FastAPI, Next.js 16 + lightweight-charts 5.3.0 (already installed). No new packages.

**Spec:** `docs/superpowers/specs/2026-11-17-holdings-desk-design.md` (owner-approved 2026-11-17). Interface digest of the existing code it builds on: `C:\Users\priya\.claude\jobs\1e0a9619\tmp\desk-interfaces.md` (copy it into the plan workspace at execution start; it is the source for every existing signature named below).

## Global Constraints

- Windows only; desktop UI (readable to ~1024 px), Hallmark rules, tokens from globals.css; ₹ via Intl en-IN, never `$`.
- Money int64 paise, quantity units, time UTC epoch ns, IST only at the edges. Kite/tick_buffer/portfolio_sync give float rupees and epoch seconds: convert at the boundary (`history._paise`, `timeutil`).
- No orders: only `kite_readonly.client()` calls (`quote`, `ltp`, `historical_data`, `margins`, `basket_order_margins`, …). Holdings come from `portfolio_sync` as today.
- No new packages. Indicators are stdlib integer/float code.
- Strategies obey the source guard (imports: stdlib allowlist, `stockify.strategy`, `stockify.strategies.*`; no wall clock), carry `DISPLAY_NAME`, a one-sentence `DESCRIPTION` ending in ".", `PARAMS` with labels free of "_".
- The recorder (its universe, connection and daemon session code) is not touched. The nightly job is in `HELD_DURING_SESSION`.
- Holdout: research.json `holdout_from` (2026-08-01) stays locked. Nightly evidence backtests end the day before `holdout_from` (or yesterday, whichever is earlier); the live "now" replay is not a backtest and uses all bars. The page states the evidence's end date.
- Desk backtests use `stockify.oracle.run` (writes no `runs` row, so the research registry's trial counts stay clean), never `runs.launch`/`runs.queue`.
- The desk never shows the owner's average cost, P&L, or ₹ outcome of a level (owner, 2026-11-17). Quantity appears only for lot checks.
- Plain names and sentences everywhere ("RSI Pullback", "protective put"); no code names in UI copy.
- Tests: stdlib unittest, offline, `import tests.support` first, `SandboxedDBTest` for stores, `isolated_env`, no network, kiteconnect absent.
- `server/schemas.py` ↔ `web/src/lib/types.ts` dto-fields blocks change together (same position, same field order); run `~/.tools/blockwatch/blockwatch.exe`.
- Every commit: owner's identity, no co-author/tool lines, no word "agent"; never stage CLAUDE.md.

## Review Focus

1. A holding that is not F&O (most small caps): page still works; chain and ideas sections say "No options are listed on NSE for this stock." (Task 7, 11 tests).
2. A holding with under 3 years or under 8 trades of history: every strategy graded none → verdict "No reliable signal" with the reason, never Hold by default (Task 5 test).
3. Kite logged out or the app started mid-session: chain poller and live candle degrade to the last snapshot with its time, no exception reaches the page (Task 9 test).
4. A corporate action (split/bonus) inside the history: bars are adjusted basis; the replay and evidence use the same basis, so a split never looks like a crash (Task 3 test with a 1:2 split fixture).
5. Chain rows with no bid/ask or zero OI: excluded from gauges and ideas, and an idea whose leg has no quote is listed as not possible with the reason (Task 7 test).

---

### Task 1: Swing indicator module and long-only base class

**Files:**
- Create: `stockify/strategies/_swing.py`
- Test: `tests/stockify/strategies/test_swing_indicators.py`

**Interfaces:**
- Produces (pure functions over Python lists; prices are paise ints; results floats, `math.nan` during warm-up):
  - `ema_series(values: list[int], n: int) -> list[float]`
  - `macd(closes: list[int], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[list[float], list[float]]` (macd line, signal line)
  - `bollinger(closes: list[int], n: int = 20, k: float = 2.0) -> tuple[list[float], list[float], list[float]]` (lower, middle, upper; population σ)
  - `donchian(highs: list[int], lows: list[int], n: int) -> tuple[list[float], list[float]]` (highest high, lowest low of the previous n bars, excluding the current bar)
  - `atr_series(highs, lows, closes, n: int) -> list[float]` (Wilder)
  - `supertrend(highs, lows, closes, n: int = 10, mult: float = 3.0) -> tuple[list[float], list[bool]]` (line, is_up)
  - `chandelier(highs, lows, closes, n: int = 22, mult: float = 3.0) -> list[float]` (highest high of n − mult·ATR(n))
  - `class SwingLong(Strategy)`: long-only CNC helper. `on_start` reads `symbol`, `interval_s` (default 86400), subscribes bars; subclasses implement `decide(self, bars: list[Bar]) -> tuple[bool, float | None, str]` (want_long, exit_level in paise or None, plain note) and declare `LOOKBACK: int`. `on_bar` fetches `ctx.bars(iid, isec, LOOKBACK)`, calls `decide`, plots `exit_level` (kind "price") when not None, and moves the position to target with `ctx.place(..., "MARKET", tag="entry"|"exit", note=note)`, sizing as SmaCross does for CNC (`(funds().available*95//100)//(close*103//100)`).
- Every new strategy (Task 2) subclasses `SwingLong`; the desk (Task 3) reads `exit_level` from plots.

- [ ] **Step 1: Write failing tests** with hand-computed fixtures: `test_ema_matches_hand_values` (n=3 over [10,11,12,13,14]·100), `test_macd_signal_cross_on_known_series`, `test_bollinger_middle_is_sma_and_bands_symmetric`, `test_donchian_excludes_current_bar`, `test_atr_wilder_first_value_is_mean_true_range`, `test_supertrend_flips_down_after_a_drop_below_the_lower_band`, `test_chandelier_is_high_minus_3_atr`, `test_warmup_values_are_nan`, and `test_swinglong_passes_the_source_guard` (`strategy.check_source(Path(_swing.__file__).read_text()) == []`).
- [ ] **Step 2:** Run `.\.venv\Scripts\python.exe tests\run_tests.py test_swing_indicators` → FAIL (module missing).
- [ ] **Step 3:** Implement the functions and `SwingLong` in `stockify/strategies/_swing.py` (underscore module: not listed in the catalogue).
- [ ] **Step 4:** Run the same command → PASS.
- [ ] **Step 5:** Commit `stockify/strategies/_swing.py` and the test.

### Task 2: The seven swing strategies

**Files:**
- Create: `stockify/strategies/{rsi_pullback,macd_cross,bollinger_reversion,donchian_breakout,supertrend,ema_trend,chandelier_exit}.py`
- Modify: `tests/stockify/test_display.py` (catalogue count), `tests/stockify/test_api_display_names.py` if it lists names
- Test: `tests/stockify/strategies/test_swing_strategies.py`

**Interfaces:**
- Consumes: Task 1 `SwingLong`, indicator functions.
- Produces (class, DISPLAY_NAME, defaults; `decide` rule; exit level):

| Module:Class | DISPLAY_NAME | Params (defaults) | Long when | exit_level |
|---|---|---|---|---|
| rsi_pullback:RsiPullback | RSI Pullback | rsi_n 14, buy_below 30, sell_above 70 | entered on RSI crossing up through buy_below; left on crossing down through sell_above | price at which RSI would equal sell_above (solve on the last bar; None if unreachable) |
| macd_cross:MacdCross | MACD Crossover | fast 12, slow 26, signal 9 | MACD above signal | None (note gives the gap) |
| bollinger_reversion:BollingerReversion | Bollinger Reversion | n 20, k 2.0 | entered on a close below the lower band; left on a close at or above the middle | middle band |
| donchian_breakout:DonchianBreakout | Donchian Breakout | entry_n 20, exit_n 10 | entered on a close above the entry_n high; left on a close below the exit_n low | exit_n low |
| supertrend:Supertrend | Supertrend | n 10, mult 3.0 | Supertrend is up | Supertrend line |
| ema_trend:EmaTrend | EMA Trend | fast 20, slow 50 | EMA fast above EMA slow | EMA slow |
| chandelier_exit:ChandelierExit | Chandelier Exit | n 22, mult 3.0, entry_n 20 | entered on a close above the entry_n high; left on a close below the chandelier line | chandelier line |

  Every class also has `symbol` (type symbol, required) and `interval_s` (choices as SmaCross). Each `DESCRIPTION` is one plain sentence, e.g. "Buys after the RSI climbs back above 30 and sells when it falls back below 70."
- State-carrying rules (RSI, Bollinger, Donchian, Chandelier "entered/left") derive the state by scanning `bars` inside `decide`, never from instance attributes, so the same bars always give the same answer (the desk replays them).

- [ ] **Step 1: Write failing tests**, one per strategy over synthetic daily bar lists built in the test: `test_<name>_goes_long_on_its_entry_and_flat_on_its_exit` (asserts `decide` on the prefix ending at the entry bar returns True and at the exit bar False), `test_<name>_exit_level` (value against the indicator from Task 1), `test_every_new_strategy_is_in_the_catalogue` (`_catalogue.python_strategies()` includes the 7 refs, each with DISPLAY_NAME, DESCRIPTION ending ".", labels without "_"), `test_each_runs_a_daily_oracle_backtest` (a 300-bar synthetic pack through `oracle.run` with the sample config shape from the digest; `metrics["trades"] >= 1`).
- [ ] **Step 2:** Run `tests\run_tests.py test_swing_strategies` → FAIL.
- [ ] **Step 3:** Write the seven modules.
- [ ] **Step 4:** Run `tests\run_tests.py test_swing_strategies` and `tests\run_tests.py test_display` → PASS.
- [ ] **Step 5:** Commit.

### Task 3: "What does it say now": the replay context

**Files:**
- Create: `stockify/desk/__init__.py`, `stockify/desk/replay.py`
- Test: `tests/stockify/desk/test_replay.py`

**Interfaces:**
- Consumes: `stockify.strategy.load`, `Bar`, `PositionView`, `Funds`, helpers `sma/ema/rsi/atr`.
- Produces:
  - `@dataclass(frozen=True) class Signal: ref: str; display_name: str; in_position: bool; buying_today: bool; exit_level_paise: int | None; note: str`
  - `class ReplayContext` implementing exactly the ctx methods the swing and existing bar strategies use: `now, today, resolve, spec, subscribe_bars, bars, sma, ema, rsi, atr, position, funds, place, plot, log` (others raise `NotImplementedError("not available on the desk")`). `place` fills instantly at the bar's close into an internal position; `plot` records the last value per name.
  - `now(ref: str, params: dict, bars: list[Bar]) -> Signal`: loads the strategy, calls `on_start`, then `on_bar` for each bar in order, and returns the final position (in_position), whether the last bar placed a BUY (buying_today), the last `exit_level` plot (paise), and the last order note.
  - `daily_bars(instrument_id: int, *, until_ns: int | None = None) -> list[Bar]` from `history.bars(iid, "day", ...)`, and `with_live_candle(bars: list[Bar], ltp_paise: int, t_ns: int, day_open_paise: int, day_high_paise: int, day_low_paise: int) -> list[Bar]` appending or replacing today's partial bar (`built=True`).
- Strategies in scope: the 7 new ones plus `sma_cross:SmaCross` with fast 50 / slow 200 (shown as "Golden Cross (50/200)") and `buy_and_hold:BuyAndHold` (baseline only, never voted).

- [ ] **Step 1: Write failing tests**: `test_now_matches_the_last_position_of_a_full_oracle_backtest` (for each of the 8 voting strategies on one synthetic 400-bar series: `now(...).in_position` equals the sign of the last `positions_eod.csv` row of `oracle.run` on the same bars), `test_buying_today_when_the_last_bar_enters`, `test_exit_level_comes_from_the_plot`, `test_live_candle_replaces_todays_partial_bar`, `test_a_split_in_adjusted_history_does_not_trigger_an_exit` (Review Focus 4: 1:2 split fixture, adjusted basis), `test_unsupported_ctx_call_raises_plainly`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement `stockify/desk/replay.py`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit.

### Task 4: Nightly evidence and the desk store

**Files:**
- Create: `stockify/desk/store.py`, `stockify/data/migrations/desk/0001_init.sql`, `stockify/desk/evidence.py`; add `paths.desk_db()` in `stockify/paths.py` (and its C++/`stockify_paths` twin only if paths are mirrored there; check `blockwatch`)
- Test: `tests/stockify/desk/test_evidence.py`, `tests/stockify/desk/test_store.py`

**Interfaces:**
- Consumes: Task 2 refs, `oracle.run`, `holdout` config (`research.json holdout_from`), `history.fetch/bars`.
- Produces:
  - Tables: `evidence(trade_date TEXT, instrument_id INT, ref TEXT, params_json TEXT, from_date TEXT, to_date TEXT, trades INT, cagr REAL, buy_hold_cagr REAL, sharpe REAL, buy_hold_sharpe REAL, max_drawdown REAL, win_rate REAL, windows_json TEXT, windows_won INT, windows_total INT, grade TEXT, note TEXT, PRIMARY KEY(trade_date, instrument_id, ref))`; `views(trade_date TEXT, instrument_id INT, symbol TEXT, verdict TEXT, json TEXT, made_ns INT, PRIMARY KEY(trade_date, instrument_id))`; `chain_snapshots(instrument_id INT, taken_ns INT, json TEXT, PRIMARY KEY(instrument_id, taken_ns))`. Rows are never deleted.
  - Store functions: `put_evidence(rows: list[dict])`, `latest_evidence(instrument_id) -> list[dict]`, `put_view(...)`, `latest_view(instrument_id) -> dict | None`, `latest_views() -> list[dict]`, `put_chain(instrument_id, taken_ns, snapshot: dict)`, `latest_chain(instrument_id) -> dict | None`.
  - `evidence.evidence_end(today: date) -> date` = min(today − 1 day, holdout_from − 1 day).
  - `evidence.grade(windows_won: int, windows_total: int, beat_overall: bool, trades: int, years: float) -> str`: "none" if trades < 8 or years < 3; "strong" if windows_won/windows_total ≥ 0.70 and beat_overall; "some" if ≥ 0.55; else "weak". "Beat" = higher Sharpe than buy-and-hold over the same span (risk-adjusted, per the spec).
  - `evidence.run_for(instrument_id: int, symbol: str, today: date) -> list[dict]`: one oracle run per voting strategy and one buy-and-hold, over all cached daily bars up to `evidence_end`, capital 1,00,00,000 paise, the digest's sample bar fill model and Zerodha costs; yearly windows from `equity.csv` (calendar years, partial first/last kept if ≥ 120 trading days), each window won when the strategy's Sharpe in it beats buy-and-hold's.

- [ ] **Step 1: Write failing tests**: `test_grade_thresholds` (table of cases at 0.69/0.70, 0.54/0.55, 7/8 trades, 2.9/3 years), `test_evidence_end_respects_the_holdout` (today 2026-11-17 → 2026-07-31; today 2026-05-01 → 2026-04-30), `test_run_for_writes_one_row_per_strategy_and_never_a_runs_row` (store.runs count unchanged), `test_store_keeps_every_day` (two days of put_view; both readable).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS. **Step 5:** Commit.

### Task 5: The verdict

**Files:**
- Create: `stockify/desk/thesis.py`
- Test: `tests/stockify/desk/test_thesis.py`

**Interfaces:**
- Consumes: Task 3 `Signal`, Task 4 evidence rows (grade), ATR(14) from Task 1.
- Produces:
  - `WEIGHTS = {"none": 0, "weak": 1, "some": 2, "strong": 3}`
  - `@dataclass(frozen=True) class Verdict: label: str  # "Hold" | "Tighten stop" | "Trim" | "Exit" | "No reliable signal"; exit_share: float; nearest_exit_paise: int | None; reasons: list[str]; for_: list[str]; against: list[str]; counts: str`
  - `decide(signals: list[Signal], grades: dict[str, str], price_paise: int, atr_paise: float) -> Verdict` with the spec's table: a strategy counts Out when `not in_position and not buying_today`; total = Σ weights; total 0 → "No reliable signal"; share ≥ 0.6 and some Out strategy graded some/strong → Exit; 0.4 ≤ share < 0.6 → Trim; else if price within 1 ATR above the nearest weighted exit level → Tighten stop; else Hold. `counts` reads e.g. "4 of 8 tested rules say exit; the 3 with the best record on this stock are among them". `reasons` are plain sentences naming levels in ₹ (price only, never the owner's cost).

- [ ] **Step 1: Write failing tests**: `test_all_none_grades_is_no_reliable_signal` (Review Focus 2), `test_exit_needs_a_some_or_strong_out_vote` (share 0.7 from weak-only → Trim, not Exit), `test_trim_band`, `test_tighten_stop_within_one_atr`, `test_buying_today_counts_in`, `test_reasons_never_mention_cost_or_profit` (no "cost", "profit", "P&L", "gain" in any string).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS. **Step 5:** Commit.

### Task 6: Option chain snapshot and gauge

**Files:**
- Create: `stockify/desk/chain.py`
- Test: `tests/stockify/desk/test_chain.py`

**Interfaces:**
- Consumes: `instruments.expiries/option_chain/token_at/spec_at/derivative`, `kite_readonly.client().quote`, `black76.implied_vol/greeks/year_fraction/rate_on`, refdata `fo_eod` for IV history.
- Produces:
  - `is_fno(underlying: str, day: date) -> bool` = `bool(instruments.expiries(underlying, "FUT", day))`.
  - `lot_size(underlying: str, day: date) -> int | None` via the near FUT's `spec_at(...)["lot_size"]`.
  - `snapshot(underlying: str, spot_paise: int, day: date, now_ns: int, *, client, strikes_each_side: int = 10) -> dict`: near and next monthly expiries, `strikes_each_side` strikes either side of spot, one `quote` call per ≤ 500 names, rows `{instrument_id, tradingsymbol, kind, strike_paise, expiry, bid_paise, ask_paise, ltp_paise, oi, iv, delta}`; rows without bid and ask get `iv=None`.
  - `gauge(snap: dict, spot_paise: int, iv_history: list[float]) -> dict`: `{pcr_oi, call_walls: [strike_paise ×3 above spot by OI], put_walls: [×3 below], max_pain_paise, atm_iv, iv_percentile (0–100 vs the past year, None if < 60 days), sentences: [plain]}`. Rows with zero OI or no quotes are excluded (Review Focus 5).
  - `iv_history(underlying: str, day: date) -> list[float]`: ATM IV per day for the past year from `fo_eod` settle prices via `implied_vol`.

- [ ] **Step 1: Write failing tests** with a fake client returning canned quote dicts: `test_snapshot_batches_and_converts_to_paise`, `test_pcr_and_walls`, `test_max_pain_on_a_known_chain`, `test_rows_without_quotes_are_excluded`, `test_iv_percentile_none_with_short_history`, `test_not_fno_stock_is_detected`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS. **Step 5:** Commit.

### Task 7: Option ideas

**Files:**
- Create: `stockify/desk/ideas.py`
- Test: `tests/stockify/desk/test_ideas.py`

**Interfaces:**
- Consumes: Task 6 snapshot and gauge, Task 5 verdict, `kite_readonly.client().basket_order_margins` and `.margins()` (available cash).
- Produces:
  - `@dataclass(frozen=True) class Idea: name: str  # "Protective put", "Collar", "Covered call", "Bull call spread", "Long call", "Bear put spread", "Long put"; legs: list[dict]  # {side, tradingsymbol, lots, price_paise}; premium_paise: int  # + paid / − received; max_loss_paise: int | None; max_gain_paise: int | None; breakeven_paise: list[int]; margin_paise: int | None; margin_ok: bool | None; why: str; caution: str; possible: bool; not_possible_reason: str`
  - `ideas(verdict: Verdict, snap: dict, gauge: dict, spot_paise: int, lot: int, free_qty: int, *, margin_client=None) -> list[Idea]`:
    - hedges always considered: protective put (strike nearest the nearest exit level, else ATM−1), collar (that put + call at the nearest call wall);
    - covered call only when `free_qty >= lot` (else listed not possible: "You hold fewer shares than one lot (N)");
    - directional: Exit/Trim → bear put spread (ATM, next wall below) and long put; Hold with ≥ 60% In weight → bull call spread (ATM, nearest call wall) and long call;
    - legs priced at ask for buys and bid for sells; a leg without a quote → `possible=False, not_possible_reason="No live quotes for <strike> <CE/PE>"`;
    - `caution` mentions high IV (percentile ≥ 70) for bought options and low IV (≤ 30) for sold ones;
    - margin from `basket_order_margins` when a client is given, `margin_ok = margin ≤ available cash`; None when unavailable.

- [ ] **Step 1: Write failing tests**: `test_covered_call_needs_a_full_lot`, `test_exit_verdict_offers_put_ideas_and_hold_offers_call_ideas`, `test_spread_max_loss_and_gain`, `test_missing_quote_makes_the_idea_not_possible`, `test_margin_above_cash_is_flagged`, `test_high_iv_caution_on_bought_options`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS. **Step 5:** Commit.

### Task 8: The nightly desk job

**Files:**
- Create: `stockify/desk/nightly.py`
- Modify: `stockify/config/schedule.json` (`"desk": {"start": "20:10", "retry_every_min": 30, "until": "23:30"}`), `stockify/daemon/scheduler.py` (DayPlan field, `plan`, `_due_ns`, `CATCH_UP`, `DAY_ORDER` after `daily_summary`, `DEPENDS["desk"] = "nse_fetch"`, `HELD_DURING_SESSION`, docstring table), `stockify/daemon/__init__.py` (`elif job == "desk"`)
- Test: `tests/stockify/desk/test_nightly.py`, `tests/stockify/daemon/test_scheduler_m5.py`

**Interfaces:**
- Consumes: `portfolio_sync.fetch_snapshot`, `instruments.resolve_token`, `history.fetch`, Task 4 `evidence.run_for`, Task 3 `now`, Task 5 `decide`.
- Produces: `nightly.run(day: date, *, client=None) -> dict` (`{"holdings": n, "analysed": m, "skipped": [{symbol, reason}]}`): reads holdings; keeps NSE EQ instruments with ≥ 1 year of daily bars (fetches 6 years of daily bars first, cached); writes evidence and a closing `views` row per holding (verdict from the day's close). Skips with reasons: "BSE only", "not an NSE stock (e.g. SGB)", "less than a year of daily prices". Kite not logged in → logs a skip, returns without marking (retried). The daemon job marks done on success.

- [ ] **Step 1: Write failing tests**: `test_nightly_skips_non_nse_and_short_history_with_reasons`, `test_nightly_writes_a_view_per_holding`, `test_not_logged_in_is_retried_not_marked`, `test_desk_job_is_held_during_the_session_and_after_nse_fetch` (scheduler).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run `tests\run_tests.py desk` and `tests\run_tests.py scheduler` → PASS; run `tests\run_tests.py config_coverage` → PASS. **Step 5:** Commit.

### Task 9: Desk API and the session chain poller

**Files:**
- Create: `server/routers/desk.py`, `server/services/desk_poller.py`
- Modify: `server/main.py` (include router; start/stop the poller in the lifespan like the other services), `server/schemas.py` and `web/src/lib/types.ts` (dto-fields blocks, same position and order)
- Test: `tests/server/test_desk_api.py` (follow the existing server test layout; check `tests/` for where router tests live)

**Interfaces:**
- Consumes: Tasks 3–7, `tick_buffer.latest_ticks_bulk`, `ticker_bridge.ensure_subscribed`, `portfolio_sync.cached_snapshot`.
- Produces:
  - `GET /api/desk` → `DeskListDTO{items: [DeskChipDTO{symbol, exchange, instrument_id, verdict, as_of_ns, live}], skipped: [{symbol, reason}]}`
  - `GET /api/desk/{symbol}` → `DeskViewDTO{symbol, instrument_id, verdict, exit_share, nearest_exit_paise, counts, reasons, signals: [DeskSignalDTO{display_name, in_position, buying_today, exit_level_paise, grade, trades, windows_won, windows_total, cagr, buy_hold_cagr, note}], evidence_to, gauge: DeskGaugeDTO | null, ideas: [DeskIdeaDTO{...Idea fields}], fno, lot, as_of_ns, live, plain}`
  - `GET /api/desk/{symbol}/chart?interval=day|minute` → the existing `RunChartDTO` shape (markers [], plots: each voting strategy's `exit_level` series from the replay, data_state "ok").
  - Live recompute: on request during the session, `now()` with `with_live_candle` from the latest tick (fallback: one `ltp` call), cached 30 s per symbol.
  - `desk_poller`: a thread started by the API, active only while `scheduler.session_in_progress(now)` and Kite is authenticated; every 60 s takes `chain.snapshot` for each F&O holding (one or two quote calls in total), stores it, and subscribes holdings' tokens via `ticker_bridge.ensure_subscribed`. Errors are logged and skipped; the endpoints then return the last snapshot with its time and `live=False` (Review Focus 3).

- [ ] **Step 1: Write failing tests** with fakes for portfolio_sync, tick_buffer and the Kite client: `test_list_returns_a_chip_per_holding_and_the_skips`, `test_view_has_no_cost_or_pnl_fields` (DTO field names exclude average_price, pnl, cost), `test_chart_shape_matches_run_chart`, `test_logged_out_returns_last_snapshot_not_500`, `test_poller_does_nothing_outside_the_session`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run `tests\run_tests.py desk` → PASS; `~/.tools/blockwatch/blockwatch.exe` → clean. **Step 5:** Commit.

### Task 10: Web: verdict chip on the Portfolio list and the holding page

**Files:**
- Create: `web/src/app/portfolio/[symbol]/page.tsx`, `web/src/app/portfolio/[symbol]/DeskChart.tsx`, `web/src/app/portfolio/[symbol]/ThesisPanel.tsx`, `web/src/app/portfolio/[symbol]/SignalsTable.tsx`, `web/src/app/portfolio/[symbol]/ChainGauge.tsx`, `web/src/app/portfolio/[symbol]/IdeasList.tsx`, `web/src/lib/desk.ts`
- Modify: `web/src/app/portfolio/HoldingsList.tsx` (verdict column with a Chip linking to `/portfolio/<symbol>`)

**Interfaces:**
- Consumes: Task 9 endpoints and DTO types; `PriceChartLazy` with `load` returning the chart DTO and `pollMs={60000}` during the session; `@/components/ui`.
- Produces the page: PageHead (symbol, "Exit suggestions, not orders"), chart + ThesisPanel side by side (≥ 1280 px; stacked below), then SignalsTable, ChainGauge, IdeasList. Copy rules: plain sentences; verdict chip colours from tokens (Exit = loss token, Trim/Tighten = warn, Hold = gain, No reliable signal = muted); never cost/P&L. Non-F&O: "No options are listed on NSE for this stock." The evidence line reads "Tested on daily prices from <from> to <evidence_to>; the months after <evidence_to> are kept aside for research and not used to grade."

- [ ] **Step 1:** Build the page against a running API with fixtures (the API tests' fakes) and check every state: live, last snapshot, not F&O, no reliable signal, Kite logged out.
- [ ] **Step 2:** Run `cd web; npm run lint; npm run build` → no errors.
- [ ] **Step 3:** Screenshot the page and the list at 1440 and 1024 px (webapp-testing skill if playwright is available; otherwise the existing screenshot route the review used) and check against web/design.md and Hallmark.
- [ ] **Step 4:** Commit.

### Task 11: Docs and decision log

**Files:**
- Modify: `README.md` (a "Holdings desk" section: what it shows, that it suggests and never orders, the evidence window and holdout note, the 7 new strategies in the strategy list), `docs/plan/00_overview.md` (D-152: holdings desk, with the replay context, oracle-only evidence, holdout handling and the owner's no-cost rule), `docs/plan/RECON.md` if it lists modules, `geo_monitor/README.md` untouched.

- [ ] **Step 1:** Write the sections (humanizer style: plain, no em dashes).
- [ ] **Step 2:** Run the full suite in the background: `.\.venv\Scripts\python.exe tests\run_tests.py > <job tmp>\suite.log 2>&1` → 0 failures beyond tracked defects.
- [ ] **Step 3:** Commit.

# Daily candles + watchlist with recorder alerts

Date: 2026-11-19. Approved by the owner (plan mode). Owner choices: candles for the screener universe plus the watchlist; watchlist alerts run in the recorder; alert types: 52-week high/low and results coming up.

## Context
The paid Kite key has a lot of unused room. The recorder uses 1 of 3 WebSocket connections and about 270 of 3,000 instruments, and the historical API is idle most of the day. The owner wants to use it for two linked features:
- **(A) Nightly daily candles** (Kite, split-adjusted). They give the screener price charts, price context, a reusable test record, and backtests on any stock.
- **(B) A watchlist.** It shows live prices while the app is open and the last close otherwise, and adds email alerts that work with the app closed.

Owner's decisions:
- Candles cover the screener's ~476 liquid stocks plus every watchlist stock.
- Alerts run in the recorder daemon, and watchlist stocks join the recorder's feed, so their sessions are recorded too.
- Two alert types only: a new 52-week high or low (intraday), and results coming up.
- Delivery is through the existing `stockify/daemon/notify.py`.

## Findings that shape the design (verified in code)
1. Session pins (`stockify/daemon/subscriptions.py:211-222`) are always FULL mode. Watchlist stocks therefore go in a new static **QUOTE** tier (`watchlist_quote`), built when the manager is constructed.
2. notify's once-only key is in memory only. A persistent dedupe table is needed, or a daemon restart would send alerts again.
3. A recorder tap that raises counts as `tap_errors`. The watchlist tap is a `deque(maxlen=N).append`: O(1), no lock, cannot fail. Dropping old frames is harmless because quote packets carry the day's running high and low.
4. `fundamentals/calendar.refresh` keeps only universe symbols. It changes to universe ∪ watchlist, at no extra NSE cost.
5. `history.TokenBucket` is per process. The candle job runs at its own 2 req/s with backoff on 429.
6. Evening jobs normally run the next morning, after hibernation, with an expired token. The candle job therefore runs at **16:40 IST**, right after compaction, while the day's token is still valid, and holds sleep until its window ends. A provisional close is corrected the next night (`INSERT OR REPLACE`).
7. Replay must reproduce what it recorded, so the watchlist merge happens only in `run_market_day`'s live branch.
8. `loader.NOTIFY_EVENTS` must match `notify.json`'s keys. bars.sqlite isn't backed up; watchlist.sqlite must be.

## Shared config (dated, validated in `stockify/config/loader.py` with tests)
- `stockify/config/candles.json`: `history_years 6`, `rps "2"`, `rate_limit_backoff_s [1,2,4]`.
- `stockify/config/watchlist.json`:
  - `max_stocks 50`, `lookback_days 365`, `min_history_days 365`
  - `basis_tolerance "0.02"`, `alert_batch_s 60`
  - `max_alert_emails_per_day 6`, `results_lead_trading_days 2`, `tap_frames 64`
- New notify events `watch_high_low` and `watch_results`, in `notify.json` and `NOTIFY_EVENTS`.
- Decision log: D-154 onward in `docs/plan/00_overview.md`. Note the actual state of the code in `docs/plan/RECON.md`.

## Phase 1: nightly daily candles
- **Move the top-up into the data layer.** Move `desk/nightly.ensure_bars` and `_fetched_back_to` into `stockify/data/history.py` as `top_up_daily(iid, day, *, years, client, bkt=None, deadline_ns=None)`. `desk.nightly.ensure_bars` becomes a wrapper, so the desk tests are unchanged. `history.fetch` and `_fetch_range` gain optional `bkt` and `deadline_ns` (checked between chunks). This also narrows the desk's deadline gap.
- **New `stockify/data/daily_candles.py`:**
  - `targets(day)`: `fundamentals.universe.current()` plus `watchlist.store.ids()` (a stub returning [] until phase 2).
  - `up_to_date(iid, day)`: true when `bar_fetch_log` already reaches the day's close.
  - `run(day, *, client, deadline_ns, now)`:
    - **Login and errors:** it checks the login first. An expired token mid-run stops it, unmarked. A 429 means back off, then stop.
    - **Deadline:** checked before each stock and between chunks.
    - **Failures:** one stock failing never stops the rest. Skips have plain reasons: not in instruments, not an NSE stock, no Kite token (delisted or suspended).
- **Daemon job.** `schedule.json` gets `"candles": {"start":"16:40","retry_every_min":15,"until":"18:30"}`. The job is wired in `stockify/daemon/scheduler.py`:
  - `CATCH_UP`; `DAY_ORDER` after `compaction`; `DEPENDS → compaction`
  - `HELD_DURING_SESSION`; `HOLDS_SLEEP_UNTIL_WINDOW_ENDS`
  - `DayPlan` fields, `_due_ns`, `window_end_ns`

  `_candles_job` in `stockify/daemon/__init__.py` follows the shape of `_desk_job`:
  - **Deadline:** next refdata or day_bundles, minus 10 minutes.
  - **Superseded day:** an older day is marked done once a newer run has topped up through it.
  - **Not logged in:** the day is left unmarked, so the morning catch-up runs it after login.
- **Request budget.** First night ≈ 960 requests (~8 min); after that ≈ 960 a night (an adjustment check plus a top-up per stock).
- **1b:** `fundamentals/test_record._fetch_all` uses `top_up_daily` instead of refetching from 2022.
- **Tests:**
  - `tests/stockify/data/test_daily_candles.py` with a fake counting client: first fill; top-up; skip when up to date; deadline between stocks and between chunks; no login; 429 backoff; no_token and not found; an adjustment wipe refills.
  - Scheduler tests in `test_scheduler_m5.py` and config coverage.

## Phase 2: watchlist store, API and page
- **Store.**
  - `paths.watchlist_db()` → `lab/watchlist.sqlite`, added to `backup.DATABASES`.
  - `stockify/watchlist/store.py` follows the pattern of `stockify/fundamentals/store.py`.
  - Migration `stockify/data/migrations/watchlist/0001_init.sql` with three tables:
    - `watchlist(instrument_id PK, symbol, added_ns, position)`
    - `watch_alerts(instrument_id, symbol, kind high52|low52|results, ref, level_paise, prior_paise, prior_date, at_ns, delivery, UNIQUE(instrument_id,kind,ref))`
    - `meta(version)`, bumped on every change
  - API: `add`, `remove`, `rows`, `ids`, `symbols`, `version`, `record_alert() → bool` (True only when inserted), `alerts(since)`. `add` uses `instruments.find("NSE", sym)` with the NSE EQ check and the cap.
- **52-week range.** `stockify/watchlist/ranges.py` holds a pure `prior_range(bars, day, lookback_days, min_history_days)`: high, low and their dates, plus the last close. It returns None on short history and is shared by the API, the evaluator and phase 4.
- **API: new router `server/routers/watchlist.py`**, registered in `server/main.py`:
  - `GET /api/watchlist`; `POST {symbol}` (404, 422, 409); `DELETE /{symbol}`; `GET /alerts?days=30`.
  - DTOs go in the `server/schemas.py` dto-fields block, mirrored in `web/src/lib/types.ts`.
- **Web.**
  - `/watchlist` in `web/src/lib/nav.ts`.
  - `web/src/app/watchlist/page.tsx`, designed with Hallmark for desktop:
    - The add box uses `GET /api/search`.
    - Live prices come from `useTicks` (`web/src/lib/useTicks.ts`) while the app is open, with the last close otherwise.
    - Distance from the 52-week high and low, in ₹ via en-IN.
    - Plain notices: "Recorded and alerted from the next session"; "Daily prices arrive after tonight's fetch".
- **Tests:** the store (SandboxedDBTest), `ranges` (boundaries, short history, leap day), API tests modelled on `test_desk_api.py`, blockwatch, and web lint and build.

## Phase 3: recorder integration and alerts (safety-critical)
- **Feed.**
  - `stockify/watchlist/feed.py` `merged(universe, day)` adds `watchlist_quote` (up to `max_stocks`). It fails soft: any error logs and returns the universe unchanged.
  - It is called in `stockify/daemon/market_day.py` `run_market_day`, live branch only, after universe.json loads.
  - `SubscriptionManager._build_static` handles `watchlist_quote` as QUOTE with reason `watch:SYM`. A stock already in a tier keeps its mode.
  - A test asserts that universe + max_stocks stays under 80% of 3,000.
- **Changes mid-session.** The store is read once, at the subscribe step. A stock added mid-session is recorded and alerted from the next session. A removed stock stops alerting within 60 s (the evaluator polls `store.version()`). Subscribing mid-session is left out of v1, to stay off the ticker thread.
- **Evaluator: `stockify/watchlist/live.py` `HighLowWatch`.**
  - **Before connect:** the token map (`daemon._token_map`) and the ranges from the bars cache are built on the main thread.
  - **Ticker thread:** only the deque append, registered via `recorder.add_tap`.
  - **Own thread, `watch-alerts`, about every 1 s:**
    - Walks the packet headers and decodes only the watched 44- and 184-byte packets. `kite_packets` and `PARSER_VERSION` stay unchanged.
    - Judges only in the CM CONTINUOUS phase and only on positive values.
    - Turns a stock's alerts off for the day when its candles are stale.
    - Turns them off on a basis mismatch (yesterday's close in the packet vs the cached close, beyond the tolerance), which points to a split or bonus ex-date.
  - **Crossing:** the day high above the prior 52-week high (or the low below the low) → `store.record_alert`. Only a new row goes into the batch.
  - **Delivery:** one digest email per `alert_batch_s`, through `notify.post("watch_high_low", key=f"52w:{day}:{n}")`. After the daily cap, alerts are stored as `capped` and the last email points to the page.
  - **Containment:** the whole loop is in try/except. After repeated errors it switches itself off for the day and never touches the recorder. It starts after `recorder.start()` and is stopped before `_shutdown`.
- **Results reminder: `stockify/watchlist/reminders.py`.**
  - Job `watch_results` starts at 21:00, retries every 30 minutes, and depends on `fundamentals`. It isn't held during the session.
  - It covers meetings from after today up to 2 trading days ahead, so a Saturday meeting is caught on Friday. It dedupes by meeting date, so a moved date gets a new reminder, and skips past meetings.
  - One digest email per run through `notify.send`.
- **Tests:**
  - `test_watch_live.py`:
    - the tap never raises, even when full;
    - one alert per side per day, also across a restart;
    - pre-open ignored;
    - basis mismatch and stale candles turn alerts off;
    - batching and the cap;
    - a removed stock stops alerting;
    - an evaluator crash doesn't affect a recorder fed alongside it.
  - Market-day tests: the live merge adds `watch:` quote subs; replay is unchanged; a corrupt watchlist DB still records.
  - Also: the subscription tier, `test_watch_reminders` (weekend and holiday, dedupe, moved date), scheduler and notify.

## Phase 4: price context and charts
- **Shared chart builder.** Move the desk chart builder (`_model`/`_dto` in `server/routers/desk.py`) into `server/services/charts.py`. The desk keeps its exit-level plots.
- **New `server/routers/stocks.py` `GET /api/stocks/{symbol}/chart`.** It returns a `RunChartDTO`:
  - daily candles for about a year, with "52-week high" and "52-week low" lines;
  - cached on the same key as the desk chart;
  - with no bars, a plain no-data notice.
- **Price context on the Details page.** `CandidateDetailDTO` gets an optional `price` (last close and date, 52-week high/low with dates).
- **Web.**
  - The Details page (`web/src/app/research/fundamentals/[symbol]/page.tsx`) gets a facts-only price strip plus `PriceChartLazy`, with no score or rank text.
  - Watchlist rows expand to the same chart.
- **Tests:** the chart DTO (bars, no data, refetch invalidation), the detail field, blockwatch, the web build.

## Phase 5: desk view for watchlist stocks (separable)
- `stockify/desk/nightly.run` gets a second pass after the holdings, over watched stocks that aren't held, with the same deadline. It reuses `grades_for`, `closing_view` and `store.put_view`. It skips `ensure_bars` when the stock is up to date, since candles ran at 16:40.
- A config cap, `desk_watch_new_grades_per_night` (10), spreads the first grading over several nights.
- Each watchlist row shows its verdict and links to `/portfolio/<symbol>`. Check that `GET /api/desk/{symbol}` serves a stock that isn't held.
- **Tests:** the watch pass runs after the holdings, respects the cap and deadline, skips held stocks, and leaves holdings results unchanged.

## Risks
- **Disk:** about 50 quote-mode stocks add roughly 100–200 MB a day of landing data (reported at startup).
- **Recorder safety:**
  - The ticker thread only runs the deque append, and the watchlist path fails soft.
  - Replay, the write path, `PARSER_VERSION` and the landing schema don't change.
- **Kite:** 2 req/s with backoff, and no coordination across processes.

## Execution
Use the superpowers flow, one phase per branch in its own worktree: spec in `docs/superpowers/specs/`, then a plan, then subagent-driven development with a review per task. Every phase gets a final review before merging. Phase 3 also gets differential-review and sharp-edges. Nothing is pushed without the owner's "push".

## Verification
1. **Tests.** The full suite (`tests\run_tests.py`) in the background, or the `stockify`, `watchlist`, `scheduler` and `notify` filters if memory is short. Also `python -m stockify config`, blockwatch, and `npm run lint` and `npm run build` in `web/`.
2. **Phase 1 live.** Run the candle job by hand, logged in. Check about 960 requests, about 8 min, and the `bar_fetch_log` rows. Rerun and confirm about 0 requests. For 3 days, compare the 16:40 close with the NSE bhavcopy close.
3. **Phase 2.** Add, remove, a duplicate, an unknown symbol and the cap. Ticks show while the app is open; the close shows when it is closed.
4. **Phase 3.**
   - A replay run gives unchanged subscriptions.
   - On a live day the subscription log has `watch:` rows and `tap_errors` is 0.
   - A forced alert in a replay is recorded, not emailed (owner, 2026-11-19: a replay never emails); a restart of a live day doesn't resend it.
   - The reminder job works against a fixture calendar.
5. **Phases 4 and 5.** The Details chart, the no-data state, and the desk view for a watched stock that isn't held. `GET /api/health` is OK.

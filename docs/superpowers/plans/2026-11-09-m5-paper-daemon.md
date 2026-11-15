# M5 Paper-Trading Daemon Implementation Plan
> Historical plan, kept as written. The platform is built and merged into `main`; what it really looks like now is in `docs/plan/RECON.md` ("2026-11-15 state") and `docs/plan/00_overview.md`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Paper sessions run unattended every NSE trading day, on the live Kite feed or on a replayed day. Every input is journaled before the engine sees it. Sessions survive disconnects, token expiry and `taskkill /F`, and each day is finalised after NSE's evening files. Every night, journal parity proves that what a session did can be reproduced exactly.

**Architecture:** The M1.5 daemon (`stockify/daemon/`) keeps its single KiteTicker.
- **One queue in.** The recorder's frame stamp is tapped: every frame and heartbeat goes, with the same `(frame_no, recv_ns, mono_ns)`, to the recorder queue and to one FIFO dispatcher queue. All other inputs enter the same FIFO with a stamp taken when they are enqueued: connection gaps, price bands, owner commands and Kite margin answers.
- **One engine thread.** It turns items into `.evt` records (the pack encoder), emits `CLOCK` from the items' own stamps, appends to `journals/<sid>/<YYYYMMDD>.evt`, then calls `Engine.push()` and `drain_outputs()`, and writes `lab.sqlite` in one transaction.
- **One engine per session-day.** Each is built from a day bundle that carries the previous day's full end-of-day state.
- **Recovery.** Whatever can change a decision outside the feed is a journaled `REFDATA` record. A process restart replays the journal and continues. This replaces the M5 doc's in-process reconnect: Twisted's reactor cannot restart.

**Tech Stack:** Python 3.12 (stdlib `unittest`, `sqlite3` WAL, `ctypes`), the C++20 engine `stockify._engine` (MSVC, pybind11), FastAPI, Next.js 16, the PowerShell 7 launcher and Windows Task Scheduler. **No new dependencies:**
- ntfy and Telegram go through `requests`;
- email goes through the geo monitor's SMTP;
- process checks go through `stockify.proc` (ctypes).

**Spec:** `docs/plan/M5_paper_daemon.md` (owner-approved). Read it with:
- `docs/superpowers/specs/2026-11-09-exact-margin-staircase-design.md` §5.2–§5.3: the live staircase L1–L5 and the disagreement log, which belong to M5.6;
- `docs/plan/00_overview.md` §2 and §5: D-16, D-20, D-25, D-26, D-27, D-32, D-47, D-64;
- `A1_schemas.md` §4–§5, §6.3 and §7.8;
- `A2_interfaces.md` §3 and §6;
- `A3_testing.md` E-01–E-03 and U-10;
- `A4_engine_semantics.md` §1.1, §1.4, §1.10 and §4.5.

**Base:** BASE = merge commit of stockify/margin-staircase into stockify/m4-fno. Task 1 checks every code reference below against BASE before work starts.

## Global Constraints

- **Paper only.** No code path may reach a Kite order route:
  - order place, modify, cancel and exit;
  - GTT;
  - mutual-fund orders and SIPs;
  - position conversion.

  `kite_client.get_client()` itself strips these routes for every caller (Task 3). Fills are simulated by the engine. Kite is used only for market data, `POST /margins/basket`, `POST /margins/orders` (K-10), quotes, instruments and profile.
- **`consider_positions` is always False** on every Kite margin call. `ReadOnlyKite` forces it and refuses `True`, so no answer ever mixes in the owner's real account.
- **I-10:** Kite `api_secret` and access tokens stay in `stockify_paths.state_dir()/kite_token.json` and `.env`. They are read only through `kite_client`, and they never appear in:
  - logs, journals, bundles or artifacts;
  - `lab.sqlite`, `daemon_status.json` (which holds at most the token file's mtime);
  - notifications or test fixtures.
- **I-2:** the engine stays deterministic, and every input that can change a decision is in the journal.
  - Neither engine reads the wall clock.
  - `CLOCK` comes from the items' stamps, never from the engine thread's wall clock.
  - Same bundle + same journal ⇒ identical outputs.
- **Ticker callbacks only enqueue** (`put_nowait`). Subscription changes reach the reactor through `reactor.callFromThread`.
- **I-13:** recorded tick data is irreplaceable, and landing and archive files are never rewritten. `lab.sqlite` changes only through numbered migrations: no `DROP`, and no rewrite of existing rows other than derived session rows.
- **Windows only (D-26):** no POSIX-only code, Linux presets, systemd or CI.
- **I-1:** no market rule is hard-coded.
  - Times live in `schedule.json`.
  - Operating knobs live in `stockify/config/paper.json`.
  - Decision-affecting parameters live in the session config, so they reach the bundle.
- **I-3 / I-4:** money is int64 paise, quantities are units, time is UTC epoch ns; IST appears only at the edges. Kite's float rupees are converted to paise with `Decimal(str(x))` (the `calibrate.kite_target` rule) before anything is journaled.
- **I-9:** the API never runs an engine in-process. It talks to the daemon only through `lab.sqlite` and files.
- **I-14:** approximations are labelled: margin steps, `settlement_fallback`, `refdata_stale`, `unbundled_instrument`, and coverage below threshold.
- **Paths** go through `stockify.paths` / `stockify_paths`. Session ids are checked with `paths.safe_segment` before any path is built. Tests sandbox all paths: import `tests.support` first, then use `SandboxedDBTest` and `isolated_env`.
- **Tests** are stdlib `unittest`, offline and credential-free, with fake Kite clients injected. Run them with `.\.venv\Scripts\python.exe tests\run_tests.py <filter>`. Run the full suite in the background (4–10 min); it must not exit 3, so build the engine first with `.\run.ps1 -BuildEngine`.
- **Blockwatch pairs** change together. Check them with `~/.tools/blockwatch/blockwatch.exe`.
  - A1 `event-kinds`/`refdata-subtypes`/`gap-reasons` ↔ `packs.py` ↔ `event.hpp`
  - A1 `margin-models` ↔ `runconfig.py`
  - `margin-daily-columns` ↔ `artifacts.py`/`artifacts.cpp`
  - `dto-fields`: `server/schemas.py` ↔ `web/src/lib/types.ts`
  - `launcher-codes`: `exitcodes.py` ↔ `run_stockify_daemon.ps1`
- **Commits**
  - Title: `<POS> 5.<n>: summary` (D-31).
  - Stage explicit paths only, never `CLAUDE.md` (D-29). Push only on the owner's "push".
  - Each task ticks its box in `docs/plan/M5_paper_daemon.md` in the same commit (README step 4).
  - A deviation gets a `D-xx` row in `00_overview.md` §5 in the same commit (README step 7).
  - **A task commits once its offline tests pass.** Any live or owner step after that is a 🛑 for the owner. It is recorded in RECON as "code done, live pending" and never blocks the next task.
- **Next.js 16:** read `web/node_modules/next/dist/docs/` before writing web code. Layouts must work at ≤390 px: cards, not tables; tap targets ≥44 px; `globals.css` tokens; ₹ via `Intl.NumberFormat("en-IN")`.
- **Roster (provisional, ruled by the controller, owner reviews in RECON):** FB recon · GK supervision, recovery and host · LCB Kite access and login · VOL day state (as M3.8) · BWM pause gate (as M3.5) · REG day bundles (as M2.1) · HOL session lifecycle · DM journal and dispatcher · RM pins (as M1.5) · MEZ live margin model · IWB Kite margin log (as M4.9) · CDM scheduler · RD parity · LW coverage · RW notifications and summary · WP API and UI · XI bookkeeping.

## Review Focus

1. **A login after a mid-day token expiry, or a late login after 09:10 IST.** The owner expects sessions to resume and the gap to show in coverage. The daemon must not die, must not hot-loop on a rejected token, and must not skip the evening finalisation. Pinned by `test_token_expiry_mid_day_pauses_exits_76_and_resumes_after_login` and `test_rejected_token_waits_for_a_newer_token_file` (Task 19), and `test_late_login_after_0910_still_runs_session` (Task 27).
2. **A daemon killed between "journal appended" and "outputs committed", and a power loss that drops the journal tail.** Recovery must neither alarm falsely nor lose orders. Pinned by `test_recovery_writes_rows_missing_from_db` and `test_recovery_db_ahead_of_journal_is_tail_lost_not_parity` (Task 17).
3. **Two records at the same nanosecond, the later one with a lower rank** (a GAP or REFDATA at a tick's or CLOCK's stamp). The engine must never refuse a batch mid-session, and replays must give identical journals. Pinned by `test_lower_rank_at_same_t_is_bumped_one_ns` (Task 10) and `test_two_replays_give_identical_journals` (Task 13).
4. **The laptop hibernates after compaction (D-32), before NSE's evening files arrive.** Finalisation and parity must run on the 06:00 IST wake, before the next day's bundle. If the files never come, the day is still finalised, with the labelled fallback. Pinned by `test_finalise_runs_as_catch_up_before_day_bundles` (Task 27) and `test_finalise_after_cutoff_uses_fallback_label` (Task 16).
5. **Stacked pauses** (an owner pause, then a token expiry, then a login) and **"Stop all" pressed while the daemon is down.** The owner's pause must survive the login and the night, and must apply before any strategy callback runs. Pinned by `test_owner_pause_survives_token_expiry_and_login` (Task 9), `test_pause_carries_across_a_day_boundary` (Task 7) and `test_pending_stop_all_applies_before_first_push_after_restart` (Task 17).

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `docs/plan/RECON.md` (append) | M5.0 findings, the mismatch table, provisional decisions | 1 |
| `stockify/data/migrations/lab/0001_init.sql`, `0002_sessions.sql`, `0003_margin_live.sql` | lab.sqlite schema as migrations | 2, 21 |
| `stockify/store.py` (modify) | `migrate.apply(conn, "lab")`; session, timeline, command and margin_live functions | 2, 9, 12, 21 |
| `kite_client.py` (modify) | `get_client()` returns a client with order routes stripped | 3 |
| `stockify/kite_readonly.py` | the read-only wrapper (`consider_positions=False`) | 3 |
| `stockify/daemon/conn_budget.py` | KiteTicker connection registry (K-1, D-20) | 3 |
| `stockify/runconfig.py`, `stockify/bundle.py` (modify) | `seed_mode`, `day_seed`, `days[].seed`; `resolve_session_day` | 4, 8 |
| `stockify/oracle/sod_state.py`, `engine/include/stocksim/sod_state.hpp`, `engine/src/sod_state.cpp` | the day-to-day state (D-47 closed); constructor applies `bundle["sod_state"]` | 5, 6 |
| `stockify/oracle/runner.py`, `engine/src/engine.cpp`, `engine/bindings/engine.cpp` (modify) | per-day reseed, state, `strategy_control`, live answers, `book()`, `margin_now()` | 4–7, 20 |
| `stockify/sessionconfig.py` | `stockify.session/1` validation | 8 |
| `stockify/daemon/lifecycle.py` | session states, stacked pause reasons | 9 |
| `stockify/daemon/journal.py` | journal writer and reader (torn-tail tolerant) | 10 |
| `stockify/daemon/recorder.py`, `stockify/daemon/ticker.py` (modify) | `Recorder.taps`; `RecorderSession.gap_taps` | 11, 13 |
| `stockify/daemon/encoder.py` | `FrameEncoder` | 11 |
| `stockify/daemon/session_day.py` | `SessionDay`, `session_engine()` | 12 |
| `stockify/daemon/dispatcher.py` | the FIFO, the engine thread, CLOCK from stamps, commands | 13 |
| `stockify/daemon/market_day.py` | `run_market_day`: recorder + sessions, threads, shutdown | 14 |
| `stockify/daemon/finalise.py` | `replay_into`, SETTLE records, `finish()`, EOD rows, next SOD | 16 |
| `stockify/daemon/recovery.py` | journal replay on restart | 17 |
| `stockify/daemon/replay_ticker.py` (modify) | resume after a mark, fault durations, process-level `kill` | 18 |
| `stockify/daemon/token_watch.py`, `scripts/run_stockify_daemon.ps1` (modify) | login wait with catch-up; exit 76 restarts at once | 19 |
| `stockify/fno/live_margin.py`, `engine/src/margin/live.cpp`, `engine/include/stocksim/margin_live.hpp` | `live_with_fallback` (oracle, engine) | 20 |
| `stockify/daemon/margin_poller.py`, `stockify/fno/margin_live_report.py` | Kite basket polling, the disagreement log, its report | 21 |
| `stockify/parity.py` | journal parity (gate) and recorder parity (report) | 22, 23 |
| `stockify/daemon/coverage.py` | per session-day coverage | 24 |
| `stockify/daemon/notify.py`, `stockify/config/notify.json` | notification channels and switches | 25 |
| `stockify/backup.py` | backup of the stores that cannot be rebuilt (ELM archive included) | 26 |
| `stockify/daemon/scheduler.py`, `stockify/daemon/__init__.py` (modify) | M5.9 jobs and catch-up | 27 |
| `stockify/config/paper.json`, `stockify/config/schedule.json` (modify) | operating knobs and job times | 11, 27 |
| `server/routers/trade.py`, `server/schemas.py`, `web/src/lib/types.ts` | `/api/trade` | 28 |
| `web/src/app/trade/…`, `web/src/lib/trade.ts` | `/trade` UI | 29 |
| `tests/stockify/daemon/paper_fixture.py` | the shared fixture day and session helpers | 10, reused later |

---

## Mismatches between the M5 doc and the code

Task 1 records these in RECON as **provisional decisions** (controller ruling; owner review). The task that implements each one writes its D-row.

| # | The M5 doc assumes | The code at BASE | Fix (decision) |
|---|---|---|---|
| 1 | M5.7: after a login the daemon "reconnects, re-subscribes … resumes" in-process; the token store is watched every 15 s | Twisted's reactor cannot restart (RECON M1.0). A 403 means exit 76, and then the launcher's `Wait-FreshToken` blocks: no catch-up job runs, nothing is finalised and the laptop never hibernates. | **D-65.** The daemon waits in-process, polling every 15 s and running catch-up jobs meanwhile, only before the first connect. After a live 403 it journals the pause, records the rejected token file's mtime and exits 76. The launcher restarts it at once, and the new process waits for a *newer* token file, then resumes through recovery. Tasks 19, 27. |
| 2 | M5.3: "`sod_state` = previous day's end-of-day state" | `load_sod_state`/`eod_state` raise `NotImplementedError` (D-47), and the oracle has neither. Per-day seeds (A4 §4.5) make "one day at a time" differ from a whole run. | **D-66.** A full state: ledger, subscriptions, the pause flag and the **full** bar history, with a size warning and no trimming. `seed_mode: per_day`. "Day by day = whole run" holds exactly on both engines. Tasks 4–6. |
| 3 | M5.3: "resolve a day bundle (M2.1)" | `resolve_bundle` takes instruments and dates from the packs, and `runconfig` requires `data.packs`. | `resolve_session_day`, with `packs: []`. Task 8. |
| 4 | M5.5: "Instruments added during the day reach the engine as `REFDATA` records" | Both engines ignore `REFDATA instrument_add`. | **D-67.** The start-of-day bundle holds the whole reachable universe. Strikes NSE lists during the day are not journaled; they get the timeline row and label `unbundled_instrument`. Tasks 8, 15. |
| 5 | A4 §1.1: journal `t = max(intended, last t)` | `LiveBatchSource::push` refuses any `(t, rank, src, seq)` that sorts below the previous record. | **D-68** amends A4 §1.1: a record that would sort before the previous one gets `t = last_t + 1 ns`. Journal `t` may therefore differ from `recv_ns`. Task 10. |
| 6 | M5.6: `live_with_fallback` uses injected `REFDATA margin_requirement` and falls back to SPAN-lite | Nothing consumes that subtype, and `MarginModel` is const and has no clock. D-64 made `staircase` the default, and the spec names steps L1–L5. L2 has no implementation. kiteconnect's `consider_positions` defaults to **True**. | **D-69.** Kite answers are always requested with `consider_positions=False` for the paper book's legs; `ReadOnlyKite` enforces this. A fresh, journaled L1 answer prices the current book's blocked margin, and therefore the available funds that every pre-trade check uses. The pre-trade *increment* (`pair()`, D-64 ruling 5) always uses the staircase. The reason is timing, not capability: the decision is made synchronously at `t_submit`, before any answer for the new "after" book could be in the journal. Waiting for Kite would make fills depend on network latency, or need a held-order state, which is out of scope. The poller may ask Kite about the "after" book once an order is placed, but only for the disagreement log. Fallback order: L3 = B1/B2, L4 = B3/B4, L5 = B5. **L2 is deferred**: it is computed in Python for the log only, and moves into the engine only if the scorecard shows it beats L3. Tasks 3, 20, 21. |
| 7 | "extend the planned `margin_live` table (migration only)" | `lab.sqlite` is not on the migration framework. | Move it onto `migrate.apply("lab")`. Task 2. |
| 8 | M5.12 `server/routers/trade.py`, with pause/resume/stop controls | Neither `/api/trade` nor `/trade` exists, and nothing connects the API to the daemon. | A `session_commands` table, polled by the engine thread. Tasks 2, 9, 28. |
| 9 | M5.2 `PAUSED(reason)`; "Stop all pauses every session immediately" | No engine meaning exists for a pause. A single `pause_reason` cannot hold stacked pauses, and each day's engine would start enabled. | **D-70.** REFDATA subtype 5 `strategy_control {enabled, reasons}`, journaled. While disabled: no new hooks; `on_fill`/`on_order_update` are queued and delivered on enable; due timers are dropped one occurrence at a time. `pause_reasons` is a set, the pause flag is part of the start-of-day state, and `SessionDay.open` journals a correction whenever the database disagrees with it. Tasks 7, 9, 12. |
| 10 | E-02: "same orders and fills as an uninterrupted run" | The replay `kill` only stops the replay thread, and a restarted replay starts from the day's first frame. | **D-71.** `kill` drains the queues, then calls `TerminateProcess` on its own process. The restart resumes after each session's last dispatched frame (`frame_no` and `recv_ns`), with no frame lost and no journal GAP. `disconnect`/`token_expiry` faults with a duration resume at `at + duration`, which closes their GAP. Live restarts during market hours always journal `GAP(process_stop)`. The kill's drain means the real `taskkill /F` loss window is covered only by Task 17's unit tests. Task 18. |
| 11 | M5.9: NSE files, finalisation and parity at 19:30–23:00 IST | D-32 hibernates the laptop at about 16:00 IST and wakes it at 06:00. `CATCH_UP` covers only `nse_fetch` and `margin_files`. | Finalise, parity and summary become catch-up jobs. The finalise cut-off is the next trading day's `day_bundles` time. `day_bundles` waits for that day's `refdata`. Tasks 16, 27. |
| 12 | M5.8 "`notify.json` with one channel (owner picks)" | `notify.json` exists as `{"channels": []}`, and `reminder.py` already sends email through the geo monitor. | The shape becomes `{"channel": null, "events": {…}}`, and the email path is reused. Task 25. |
| 13 | M5.13: Task Scheduler; the power plan never sleeps 19:00–04:30 | Already built differently (D-32): the logon task, `check_power.ps1`, hibernate/wake. No backup exists, and its target was deferred. | M5.13 becomes the backup command and job; the host checks are owner steps. Task 26. |
| 14 | The Kite connection never places orders (implicit) | `get_client()` returns a full `KiteConnect`. | Order routes are stripped inside `get_client()`, plus the wrapper and two source scans. Task 3. |
| 15 | M5.1 fan-out of `(frame_no, recv_ns, payload)` | Frames are stamped inside `Recorder.on_message`. | `Recorder.taps`. Task 11. |
| 16 | M5.3 orders upserted by id | `drain_outputs()["orders"]` holds new orders only. | Status is updated from drained `order_events`, then replaced by `orders.csv` at `finish()`. Tasks 12, 16. |
| 17 | `/trade/[id]`: positions with live LTP (`useTicks`) | `useTicks` keys on `instrument_token`. | The API returns the day's token for each position. Task 28. |
| 18 | A3 U-10 (secrets test) | It does not exist. | Scaffold in Task 3; extended in Tasks 12, 21 and 25. |
| 19 | M5.1 "CLOCK records every second" (unspecified source) | A wall-clock CLOCK on the engine thread would rewrite tick times by the queue lag, and twin runs would differ. | **D-72.** `CLOCK` is derived from the stamps of dequeued frames and heartbeats. Every injected record enters the same FIFO with an enqueue stamp. In replay the poller and band snapshots are off. Task 13. |
| 20 | (C-2) bounded bar history | Indicators run over the whole bar list, so a bound breaks "day by day = whole run". | The state carries the full history (D-66), with a size warning. Task 5. |
| 21 | (I-5) one pause reason | Pauses do not stack and do not carry overnight. | Covered by D-70. Tasks 7, 9. |
| 22 | (I-6) replay restart rule | `ReplayTicker.run` ignores fault durations, and the resume key was undefined. | Covered by D-71. Task 18. |
| 23 | (I-10) connection gaps in journals | `RecorderSession` writes gaps only to the landing file. | `RecorderSession.gap_taps` feed the FIFO. Stall gaps reach journals live only; in replay they are timeline rows. Task 13. |

---

### Task 1: M5.0 recon and provisional decisions (FB 5.1)

**Files:**
- Modify: `docs/plan/RECON.md` (append `## 2026-11-xx — M5.0 recon (FB 5.1)`), `docs/plan/M5_paper_daemon.md` (M5.0 boxes)

**Interfaces:**
- Produces: the RECON record of BASE, the verified mismatch table, the provisional decisions D-65 to D-72 (each task writes its own D-row when it implements it), the provisional roster, and the provisional per-session **stop** rule. The stop rule: `stop` journals `strategy_control(false, ["stopped"])`. Resting orders follow their validity to day end, and open positions are held into the final EOD state, labelled `stopped_with_positions`. The day is finalised, the status becomes STOPPED, and no further day bundles are made.

- [ ] **Step 1: Restate the base.** Run `git log -1 --format="%H %s"` and `git status --short`. Record "BASE = merge commit of stockify/margin-staircase into stockify/m4-fno" and its subject line. Record any uncommitted file in RECON and report it to the controller (not an owner gate).
- [ ] **Step 2: Check the token store** (M5.0 box 1) without reading it: `rg -n "_token_file|load_cached_token|save_token|is_authenticated" kite_client.py server/routers/kite.py stockify scripts`. Expected: a single store at `state_dir()/kite_token.json`, written atomically by `save_token` from `GET /api/kite/callback`; the launcher reads only its mtime. **Do not open the token file or `.env`.**
- [ ] **Step 3: Check the recordings** (M5.0 box 2): run `.\.venv\Scripts\python.exe -c "from stockify.data import ticks; print(ticks.available_days())"` and, for the newest full day, `.\.venv\Scripts\python.exe -m stockify daemon --recorder-only --ticker replay:<day>:max`. Expected: `dropped 0; exit 0`. If no full day exists yet, record box 2 as 🛑 live pending for the owner and continue; every offline test uses fixture days.
- [ ] **Step 4: Verify each mismatch row** with one `rg` per row, for example `rg -n "not_until_m5|instrument_add|class LiveBatchSource|CREATE TABLE IF NOT EXISTS|on_message = recorder|consider_positions" stockify engine kite_client.py`, and record each code location.
- [ ] **Step 5: Write the RECON section.** It holds:
  - the table, the provisional decisions, the roster and the stop rule, each marked "provisional: controller ruling under owner autonomy, for owner review";
  - the T-1 note: PST since 2026-11-01. The login window is 16:30–19:00 PST, a login still works until 19:40 PST, and the market opens at 19:45 PST;
  - the owner-only list (end of this plan).
- [ ] **Step 6: Commit** (no blocking gate)

```bash
git add docs/plan/RECON.md docs/plan/M5_paper_daemon.md
git commit -m "FB 5.1: M5 recon: what the daemon, engine and API really look like"
```

---

### Task 2: lab.sqlite on versioned migrations (XI 5.1)

**Files:**
- Create: `stockify/data/migrations/lab/0001_init.sql` (today's `_SCHEMA` from `store.py`, with `IF NOT EXISTS` kept), `stockify/data/migrations/lab/0002_sessions.sql`
- Modify: `stockify/store.py:18-101`
- Test: `tests/stockify/test_store_migrations.py`

**Interfaces:**
- Produces: `store.SCHEMA_VERSION = 2`. Migration 0002:

```sql
ALTER TABLE sessions ADD COLUMN strategy_label TEXT;
ALTER TABLE sessions ADD COLUMN pause_reasons_json TEXT NOT NULL DEFAULT '[]';   -- sorted set; pause_reason keeps the first, for display
ALTER TABLE session_days ADD COLUMN status TEXT;          -- open | finalised | failed
ALTER TABLE session_days ADD COLUMN finalised_ns INTEGER;
ALTER TABLE session_days ADD COLUMN coverage_json TEXT;
ALTER TABLE session_days ADD COLUMN labels_json TEXT;
ALTER TABLE session_days ADD COLUMN last_frame_no INTEGER;
ALTER TABLE session_days ADD COLUMN last_frame_recv_ns INTEGER;
CREATE INDEX IF NOT EXISTS ix_timeline_session ON session_timeline (session_id, wall_ns);
CREATE TABLE IF NOT EXISTS session_commands (cmd_id INTEGER PRIMARY KEY, session_id TEXT, -- NULL = every session
  command TEXT NOT NULL, args_json TEXT, created_ns INTEGER NOT NULL, applied_ns INTEGER, result TEXT);
```

- [ ] **Step 1: Write the failing tests**

```python
class LabMigrationTests(SandboxedDBTest):
    db_modules = (store,)
    def test_version_equals_number_of_migration_files(self):
        n = len(list((migrate.MIGRATIONS_DIR / "lab").glob("*.sql")))
        self.assertEqual(store.schema_version(), store.SCHEMA_VERSION); self.assertEqual(store.SCHEMA_VERSION, n)
    def test_v1_lab_with_rows_migrates_and_keeps_them(self):
        # a v1 file built from the old _SCHEMA text (copied into the test) + 3 margin_calibration rows + 1 run
        self.assertEqual(len(store.margin_calibrations()), 3); self.assertEqual(store.get_run("r1")["status"], "done")
        self.assertIn("last_frame_no", [r[1] for r in store._connect().execute("PRAGMA table_info(session_days)")])
    def test_edited_migration_is_refused(self): ...   # migrate.MigrationError
```

- [ ] **Step 2: Run them to verify they fail.** `.\.venv\Scripts\python.exe tests\run_tests.py test_store_migrations`. Expected: FAIL.
- [ ] **Step 3: Implement.** `version = migrate.apply(conn, "lab")`, then `INSERT OR REPLACE INTO schema_meta VALUES ('schema_version', str(version))`. Assert `version == SCHEMA_VERSION`.
- [ ] **Step 4: Run** `tests\run_tests.py test_store`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add stockify/data/migrations/lab stockify/store.py tests/stockify/test_store_migrations.py
git commit -m "XI 5.1: lab.sqlite moves onto numbered migrations; sessions get commands and day status"
```

---

### Task 3: Kite access rules: order routes stripped, read-only wrapper, connection budget, secrets scan (LCB 5.1; M5.14)

**Files:**
- Create: `stockify/kite_readonly.py`, `stockify/daemon/conn_budget.py`, `tests/stockify/test_kite_readonly.py`, `tests/stockify/daemon/test_conn_budget.py`, `tests/stockify/test_secrets.py`
- Modify:
  - `kite_client.py:191-218`: `get_client()` builds a stripped client;
  - `stockify/daemon/ticker.py:44-49, 120-128`: the budget, and every close code logged at ERROR, never the websocket URL;
  - `stockify/daemon/__init__.py`, `stockify/data/limits.py`: use `kite_readonly.client()`;
  - `kite_ticker_worker.py`: the API's connection registers as role `api`.

**Interfaces:**
- Produces:
  - `kite_client.ORDER_ROUTE_KEYS: frozenset[str]`: every key in `KiteConnect._routes` that starts with `order.`, `gtt.` or `mf.order`/`mf.sip`, plus `portfolio.positions.convert`. **Build it from the installed SDK's `_routes` by prefix**, then assert it contains `"order.place"`, `"order.modify"`, `"order.cancel"`, `"gtt.place"`, `"gtt.modify"`, `"gtt.delete"`, `"mf.order.place"`, `"mf.order.cancel"` and `"portfolio.positions.convert"`.
  - `kite_client.ORDER_URL_PREFIXES = ("/orders", "/gtt", "/mf/orders", "/mf/sips", "/portfolio/positions")` (for PUT on positions).
  - `class kite_client.OrderRouteForbidden(RuntimeError)`.
  - `get_client()` returns a `KiteConnect` whose instance `_routes` is a copy without `ORDER_ROUTE_KEYS`, and whose `_request` is wrapped. The wrapper raises `OrderRouteForbidden` for any route key in the deny-list or any URL starting with a deny prefix (the positions prefix only for PUT). The order methods (`place_order`, `modify_order`, `cancel_order`, `exit_order`, `place_gtt`, `modify_gtt`, `delete_gtt`, `place_mf_order`, `cancel_mf_order`, `place_mf_sip`, `modify_mf_sip`, `cancel_mf_sip`, `convert_position`) are instance-bound to a function that raises. This applies to every caller: CLI, calibrate, kitecheck, capture_charges, instruments, history and the server.
  - `kite_readonly.ALLOWED = frozenset({"basket_order_margins", "order_margins", "get_virtual_contract_note", "quote", "ltp", "ohlc", "profile", "margins", "historical_data", "instruments"})`.
  - `class ForbiddenKiteCall(RuntimeError)`.
  - `class ReadOnlyKite` (`__slots__ = ()`, client held in a closure). `__getattr__` refuses every name outside `ALLOWED` and every `_`-prefixed name. `basket_order_margins(orders, *, consider_positions=False, mode=None)` and `order_margins(orders, *, mode=None)` force `consider_positions=False` and raise `ForbiddenKiteCall` if `True` is passed.
  - `kite_readonly.client() -> ReadOnlyKite`: wraps `kite_client.get_client()` and passes its `KiteAuthError`s through unchanged.
  - `conn_budget.register(role: str, *, limit: int) -> Ticket`. It writes `paths.lab_dir()/ticker_connections/<role>-<pid>.json` holding `{pid, created, role}`. It refuses when the live tickets are `>= limit - 1`, so one connection stays spare. A ticket whose process `proc.is_alive(pid, created)` says is gone is removed. `Ticket.release()`; `class BudgetExceeded(RuntimeError)`.
  - `test_secrets.plant_secrets(test) -> str` puts `FAKETOKEN-123`:
    - in a sandboxed `kite_token.json` as `access_token`;
    - in `isolated_env({"KITE_API_SECRET": "FAKETOKEN-123", ...})`;
    - in the fake client's `access_token`.

    `assert_no_secret(root: Path, secret: str) -> None` walks every file under the sandbox except `kite_token.json` and fails on the first file that contains the secret.

- [ ] **Step 1: Write the failing tests**

```python
def test_order_methods_unreachable_on_get_client(self):           # fake KiteConnect class patched into kite_client
    k = kite_client.get_client()
    for name in ("place_order", "modify_order", "cancel_order", "exit_order", "place_gtt", "place_mf_order",
                 "convert_position"):
        with self.assertRaises(kite_client.OrderRouteForbidden): getattr(k, name)(variety="regular")
def test_private_request_to_order_route_refused(self):
    k = kite_client.get_client()
    for call in (lambda: k._post("order.place", params={}), lambda: k._request("gtt.place", "POST"),
                 lambda: k._request("/orders/regular", "POST")):
        with self.assertRaises(kite_client.OrderRouteForbidden): call()
    self.assertNotIn("order.place", k._routes)
def test_wrapper_refuses_private_and_order_names(self):
    ro = kite_readonly.ReadOnlyKite(FakeKite())
    for name in ("_client", "_post", "place_order", "__dict__"):
        with self.assertRaises(kite_readonly.ForbiddenKiteCall): getattr(ro, name)
def test_consider_positions_true_is_refused_and_false_is_sent(self):
    fake = FakeKite(); ro = kite_readonly.ReadOnlyKite(fake)
    with self.assertRaises(kite_readonly.ForbiddenKiteCall): ro.basket_order_margins([], consider_positions=True)
    ro.basket_order_margins([]); self.assertIs(fake.calls[-1]["consider_positions"], False)
def test_no_source_calls_an_order_method_or_route(self):
    # every git-tracked *.py and *.ps1 except tests/ and .venv/ (git ls-files): no match for
    # METHODS = r"\b(place_order|modify_order|cancel_order|exit_order|place_gtt|modify_gtt|delete_gtt|place_mf_order|cancel_mf_order|place_mf_sip|modify_mf_sip|cancel_mf_sip|convert_position)\b"
    # (kite_client.py's deny-list definitions are the only allowed occurrences, matched by line)
    # ROUTES = r"['\"](order\.(place|modify|cancel)|gtt\.(place|modify|delete)|mf\.(order|sip)\.[a-z]+|portfolio\.positions\.convert)['\"]"
    # URLS = r"['\"]/(orders|gtt|mf/orders|mf/sips)\b"
    self.assertEqual(matches, [])
def test_third_live_connection_is_refused(self): ...    # api + daemon ok; dev refused
def test_dead_ticket_is_cleaned(self): ...
def test_planted_secret_is_found(self): ...             # assert_no_secret finds FAKETOKEN-123 in a planted x.log
def test_ticker_errors_never_log_the_websocket_url(self): ...   # _on_error/_on_close with a fake ws whose url holds FAKETOKEN-123
```

- [ ] **Step 2: Run them to verify they fail.** `tests\run_tests.py test_kite_readonly test_conn_budget test_secrets`. Expected: FAIL.
- [ ] **Step 3: Implement** the signatures above. If the installed SDK's `_routes` lacks an asserted key, fail loudly; that means re-check the deny-list on a `kiteconnect` upgrade (record this in RECON).
- [ ] **Step 4: Run** the same tests plus `test_kite_client test_calibrate test_kitecheck test_cli`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add kite_client.py stockify/kite_readonly.py stockify/daemon/conn_budget.py stockify/daemon/ticker.py stockify/daemon/__init__.py stockify/data/limits.py kite_ticker_worker.py tests/stockify/test_kite_readonly.py tests/stockify/daemon/test_conn_budget.py tests/stockify/test_secrets.py
git commit -m "LCB 5.1: Kite order routes stripped for every caller; at most two live tickers"
```

---

### Task 4: Per-day seeds (VOL 5.1; part of D-66)

**Files:**
- Modify: `stockify/runconfig.py` (`seed_mode`), `stockify/bundle.py` (`day_seed`, `days[].seed`), `stockify/oracle/runner.py` (reseed), `engine/src/engine.cpp` + `engine/src/json_bundle.cpp` (reseed), `docs/plan/A1_schemas.md` §6.1–§6.2, `docs/plan/A4_engine_semantics.md` §4.5
- Test: `tests/stockify/test_seed_mode.py`

**Interfaces:**
- Produces:
  - `bundle.day_seed(seed: int, day: date) -> int`: the first 8 bytes, little-endian, of `sha256(f"stockify-seed:{seed}:{day:%Y%m%d}".encode("ascii"))`.
  - The run config gets `"seed_mode": "run" | "per_day"`, default `"run"` (so goldens are unchanged).
  - When `per_day`, `bundle["days"][i]["seed"] = day_seed(cfg["seed"], d)`, and both PCG32 streams (slippage 1, strategy 2) are re-created from it before the first event whose IST date is `d`. The top-level `seed` stays the session or run seed; `days[].seed` is the **only** source of a day's seed.

- [ ] **Step 1: Write the failing tests**

```python
def test_day_seed_matches_a4(self):
    self.assertEqual(bundle.day_seed(7, date(2026, 11, 17)),
                     int.from_bytes(hashlib.sha256(b"stockify-seed:7:20261117").digest()[:8], "little"))
def test_per_day_reseeds_both_streams_on_both_engines(self): ...   # fuzz worlds 4000-4049, oracle == engine
def test_seed_mode_run_changes_nothing(self): ...                  # bundle bytes and golden artifacts unchanged
```

- [ ] **Step 2: Run them to verify they fail.** `tests\run_tests.py test_seed_mode`. Expected: FAIL.
- [ ] **Step 3: Implement** in the oracle first, then the engine.
- [ ] **Step 4: Run** `.\run.ps1 -BuildEngine`, then `tests\run_tests.py test_seed_mode test_engine_fuzz`, then `python -m stockify golden run --impl oracle` and `--impl engine`. Expected: PASS, with 15 goldens unchanged.
- [ ] **Step 5: Commit**

```bash
git add stockify/runconfig.py stockify/bundle.py stockify/oracle/runner.py engine/src/engine.cpp engine/src/json_bundle.cpp docs/plan/A1_schemas.md docs/plan/A4_engine_semantics.md tests/stockify/test_seed_mode.py
git commit -m "VOL 5.1: runs can reseed per trading day, as paper sessions do"
```

---

### Task 5: The day-to-day state in the oracle (VOL 5.2; D-66)

**Files:**
- Create: `stockify/oracle/sod_state.py`
- Modify: `stockify/oracle/runner.py` (`eod_state`, `load_sod_state`, constructor applies `bundle["sod_state"]`, the `on_start` rule), `stockify/tools/fuzz.py` (`RandomTrader.save_state`/`load_state`; a long-history world), `docs/plan/A1_schemas.md` (the `sod_state` shape), `docs/plan/A4_engine_semantics.md` §1.10, `docs/plan/00_overview.md` (D-66)
- Test: `tests/stockify/test_sod_state.py`

**Interfaces:**
- Consumes: `seed_mode: per_day` (Task 4).
- Produces:
  - `sod_state.SCHEMA = "stockify.sod_state/1"`; `capture(engine) -> dict`; `restore(engine, state: dict) -> None`.
  - `Engine.eod_state() -> str` (`numerics.dumps_canonical`). `Engine.load_sod_state(text: str) -> None` runs before the first event and raises `RunError` on a missing key or a different schema.
  - **The constructor applies a non-null `bundle["sod_state"]` itself**, so no caller can forget it.
  - State keys, in this order:
    - `schema, trade_date, cash_paise, ledger` (positions in insertion order with qty, avg, realised, style, last_settle);
    - `cnc_releases`;
    - `pending_orders` (queued for the next trading day, A4 §3.10);
    - `timers` (id, t, period);
    - `counters {next_order, next_fill, next_timer, charge_row, event_no}`;
    - `subscriptions {ticks: [iid…], bars: {iid: [interval…]}}`;
    - `strategy_enabled, pause_reasons` (Task 7 fills them; `true` and `[]` until then);
    - `fsp_seen`, `marks {iid: paise}`;
    - `market {iid: {last snapshot fields, band, min_interval, day}}`;
    - `bar_history {"iid:interval": [[start, o, h, l, c, v, oi], …]}`: **the full history, never trimmed**;
    - `strategy` (`save_state()`).
  - **The `on_start` rule:** `on_start` runs only when `sod_state` is null (the session's first day). A resumed day calls `strategy.load_state(state["strategy"])` instead, so restored timers are never duplicated.
  - Size guard: `sod_state.size_warning(text: str, warn_bytes: int) -> str | None`. The callers (Task 16) log WARNING and write a timeline row `sod_state_large` past `paper.json` → `sod_state_warn_mb` (default 64). Nothing is trimmed.
  - **Size note** (in A1 and RECON): one bar row is about 70 bytes of canonical JSON. One minute-bar series on an F&O instrument (09:15–15:40, 385 bars a day, about 21 days) is about **0.57 MB per instrument-month**. A 5-second series is about 12× that, about 6.8 MB. A session with 20 minute-bar instruments grows about 11 MB a month, so 64 MB is reached after about 6 months. The warning gives time to decide on a compact encoding (a later D-entry, both engines).

- [ ] **Step 1: Write the failing tests**

```python
def test_split_equals_whole_on_fuzz_worlds(self):
    for seed in range(4000, 4040):
        world = fuzz.fuzz_world(seed); world["run"]["seed_mode"] = "per_day"
        whole, parts = run_oracle(world), run_day_by_day_oracle(world)
        for f in ("orders.csv", "fills.csv", "charges.csv", "positions_eod.csv", "equity.csv", "margin_daily.csv"):
            self.assertEqual(rows_by_date(whole, f), rows_by_date(parts, f), (seed, f))
def test_long_history_ema_strategy_split_equals_whole(self):
    # fuzz.long_history_world(): 3 days x 2,000 minute bars of one instrument; a strategy trading on ema(50)/rsi(14)/atr(14)
    # whose lookback seeds from the first bar: day-by-day == whole run, every indicator value equal (logged per bar)
def test_on_start_runs_only_on_the_first_day(self): ...
def test_state_round_trip_is_byte_identical(self): ...
def test_constructor_applies_bundle_sod_state(self): ...
def test_load_after_first_event_raises(self): ...
def test_size_warning_past_threshold(self): ...
```

`run_day_by_day_oracle` lives in the test. It slices the bundle into one-day bundles with `sod_state` filled in, slices the pack records by IST date, and concatenates the per-day artifacts.

- [ ] **Step 2: Run them to verify they fail.** `tests\run_tests.py test_sod_state`. Expected: FAIL.
- [ ] **Step 3: Implement.** Per-day metrics (`rejects`, `participation_used`, `open_changes`) are not part of the state.
- [ ] **Step 4: Run** `tests\run_tests.py test_sod_state test_oracle test_bundle`, then `golden run --impl oracle`. Expected: PASS, goldens unchanged.
- [ ] **Step 5: Commit**

```bash
git add stockify/oracle/sod_state.py stockify/oracle/runner.py stockify/tools/fuzz.py docs/plan/A1_schemas.md docs/plan/A4_engine_semantics.md docs/plan/00_overview.md tests/stockify/test_sod_state.py
git commit -m "VOL 5.2: a paper session's full day-to-day state, oracle first"
```

---

### Task 6: The day-to-day state in the engine; D-47 closed (VOL 5.3)

**Files:**
- Create: `engine/include/stocksim/sod_state.hpp`, `engine/src/sod_state.cpp`, `engine/tests/test_sod_state.cpp`, `tests/stockify/test_engine_sod_state.py`
- Modify: `engine/src/engine.cpp` (the constructor applies `sod_state`; the `on_start` rule), `engine/bindings/engine.cpp:187-189, 445-448` (remove `not_until_m5`; add `book()`), `engine/CMakeLists.txt`, `tests/stockify/test_engine_strategies.py:317-320` (the `NotImplementedError` assertions become a round-trip test), `docs/plan/00_overview.md` (D-47: closed by VOL 5.2–5.3)

**Interfaces:**
- Consumes: Task 5's schema.
- Produces:
  - `std::string stocksim::eod_state_json(const Engine&)` and `void stocksim::load_sod_state(Engine&, std::string_view)`.
  - Bindings: `Engine.eod_state() -> str` and `Engine.load_sod_state(str) -> None`.
  - `Engine.book() -> list[tuple[int, str, int]]` (instrument_id, product, signed units), callable only between pushes. Task 15 consumes it to keep open positions pinned.

- [ ] **Step 1: Write the failing tests**

```python
def test_engine_state_equals_oracle_state_byte_for_byte(self):
    for seed in range(4000, 4100): self.assertEqual(oracle_eod(seed, day=0), engine_eod(seed, day=0), seed)
def test_engine_split_equals_whole(self): ...        # 100 seeds + the long-history EMA world
def test_engine_day_by_day_equals_oracle_day_by_day(self): ...
def test_state_round_trips_through_the_binding(self): ...   # replaces test_engine_strategies:317-320
def test_book_lists_open_positions(self): ...
```

The C++ side is covered by doctest `TEST_CASE("sod_state round trip keeps every counter")`.

- [ ] **Step 2: Run them to verify they fail.** Run `.\run.ps1 -BuildEngine`, then `tests\run_tests.py test_engine_sod_state`. Expected: FAIL (`NotImplementedError`).
- [ ] **Step 3: Implement** it field by field after the oracle. Write the JSON with the canonical `json_out` writer.
- [ ] **Step 4: Run** `.\run.ps1 -BuildEngine`, then `tests\run_tests.py test_engine_sod_state test_engine_strategies test_engine_fuzz test_engine_parity`, then `golden run --impl engine`. Expected: PASS, ctest green.
- [ ] **Step 5: Commit**

```bash
git add engine/include/stocksim/sod_state.hpp engine/src/sod_state.cpp engine/src/engine.cpp engine/bindings/engine.cpp engine/CMakeLists.txt engine/tests/test_sod_state.cpp tests/stockify/test_engine_sod_state.py tests/stockify/test_engine_strategies.py docs/plan/00_overview.md
git commit -m "VOL 5.3: the engine carries a session's state day to day, byte-identical to the oracle"
```

---

### Task 7: Pausing a strategy is a journaled input (BWM 5.1; D-70)

**Files:**
- Modify:
  - `docs/plan/A1_schemas.md:269-271`: refdata-subtypes block, `5 strategy_control`;
  - `stockify/data/packs.py:83-85`, `engine/include/stocksim/event.hpp:71-72`;
  - `stockify/oracle/runner.py`, `stockify/oracle/sod_state.py`, `engine/src/engine.cpp`, `engine/src/sod_state.cpp`;
  - `docs/plan/A4_engine_semantics.md`: new §3.11, "Strategy control";
  - `docs/plan/00_overview.md`: D-70.
- Test: `tests/stockify/test_strategy_control.py`

**Interfaces:**
- Produces:
  - `packs.enc_refdata(0, t, "strategy_control", {"enabled": bool, "reasons": [str…] (sorted)})`.
  - **While disabled:**
    - no new hook runs (`on_tick`, `on_bar`, `on_timer`, `on_session_phase`, `on_session_end`; these are dropped, with log line `INFO <hook> skipped: strategy disabled (<reasons>)`);
    - `on_fill` and `on_order_update` are **queued** and delivered in their original order on enable, before any other hook;
    - a one-shot timer due while disabled is dropped;
    - a periodic timer loses only the due occurrence and keeps its period;
    - matching, fills, charges, settlement, MIS square-off and RMS go on;
    - `save_state()` still runs at DAY_END.
  - The flag, its reasons and the queued notifications are part of the state (`strategy_enabled`, `pause_reasons`, plus `queued_notifications` appended to the Task 5 key list), so **a pause carries across a day boundary**.

- [ ] **Step 1: Write the failing tests**

```python
def test_no_new_hook_runs_while_disabled_but_resting_orders_fill(self): ...   # both engines, rows equal
def test_missed_fill_and_order_update_delivered_in_order_on_enable(self):
    self.assertEqual(hooks_after_enable[:2], ["on_order_update 1 COMPLETE", "on_fill 1"])
def test_periodic_timer_loses_only_due_occurrences(self): ...
def test_pause_carries_across_a_day_boundary(self):   # disabled at 15:00 day 1; day 2 (from eod_state) runs no hook until enable
def test_unknown_subtype_still_raises(self): ...
```

- [ ] **Step 2: Run them to verify they fail.** `tests\run_tests.py test_strategy_control`. Expected: FAIL.
- [ ] **Step 3: Implement** in the oracle first, then the engine.
- [ ] **Step 4: Run** `.\run.ps1 -BuildEngine`, then `tests\run_tests.py test_strategy_control test_sod_state test_engine_sod_state test_packs test_engine_packs`, then blockwatch. Expected: PASS, clean.
- [ ] **Step 5: Commit**

```bash
git add docs/plan/A1_schemas.md docs/plan/A4_engine_semantics.md docs/plan/00_overview.md stockify/data/packs.py engine/include/stocksim/event.hpp stockify/oracle/runner.py stockify/oracle/sod_state.py engine/src/engine.cpp engine/src/sod_state.cpp tests/stockify/test_strategy_control.py
git commit -m "BWM 5.1: pause and resume reach the engine as journaled strategy_control records"
```

---

### Task 8: Session config and day bundles (REG 5.1; M5.2 config, M5.3 bundle; D-67)

**Files:**
- Create: `stockify/sessionconfig.py`, `tests/stockify/test_session_bundle.py`
- Modify: `stockify/runconfig.py` (`validate(raw, *, packs_required: bool = True)`), `stockify/bundle.py` (`_resolve_core`, `session_universe`, `resolve_session_day`), `docs/plan/A1_schemas.md` (new §6.4 `session.json`), `docs/plan/00_overview.md` (D-67)

**Interfaces:**
- Consumes: `day_seed`, `days[].seed` (Task 4).
- Produces:
  - `sessionconfig.SCHEMA = "stockify.session/1"`, `class SessionConfigError(ValueError)`, `validate(raw: dict) -> dict`, `load(source) -> dict`. The config is a run config without `data`, plus `universe: list[selector]` (the M1.8 format). Defaults that differ from runs:
    - `fill_model.kind = "snapshot_quote"`;
    - `margin.model = "staircase"` until Task 20, then `"live_with_fallback"`;
    - `seed_mode = "per_day"`, `price_basis = "raw"`;
    - `margin.live_fresh_s = 360`.
  - `bundle.session_universe(selectors: list[dict], day: date) -> set[int]`: the `packs.resolve` ids, with each `chain` selector widened to every strike of its expiry and every listed future of the underlying (D-67).
  - `bundle.resolve_session_day(session_id: str, cfg: dict, day: date, sod_state: dict | None, *, refdata_stale: bool = False) -> dict`:
    - days `[day]` with `days[0].seed`, and `packs: []`;
    - `sod_state` embedded as given;
    - labels as in `resolve_bundle`, plus `session:<session_id>`, plus `refdata_stale` when flagged;
    - SPAN files known at resolve time only. Live L3 uses the files archived by 08:30 IST (i1, or the previous EOD); intraday i2–i4 are labelled `margin_span:sod_files_only`.
    - It raises `BundleError` on a non-trading day or an unsafe `session_id`.

- [ ] **Step 1: Write the failing tests** (`tests.stockify.data.test_packs.PackTest` instruments, DAY 2026-11-17)

```python
def test_day_bundle_holds_every_strike_of_the_selected_expiry(self):
    b = bundle.resolve_session_day("s1", cfg(universe=[{"chain": {"underlying": "NIFTY", "expiry": "nearest", "strikes": 2}}]), DAY, None)
    self.assertEqual(len([i for i in b["instruments"] if i["kind"] == "CE"]), 12)
    self.assertEqual(b["packs"], []); self.assertEqual(b["days"][0]["seed"], bundle.day_seed(5, DAY))
def test_bundle_hash_is_stable(self): ...
def test_sod_state_is_embedded_verbatim(self): ...
def test_session_config_refuses_data_packs(self): ...
def test_holiday_and_unsafe_id_raise(self): ...
def test_existing_run_bundles_unchanged(self): ...
```

- [ ] **Step 2: Run them to verify they fail.** `tests\run_tests.py test_session_bundle`. Expected: FAIL.
- [ ] **Step 3: Implement.** `resolve_bundle` must keep its exact output.
- [ ] **Step 4: Run** `tests\run_tests.py test_session_bundle test_bundle test_bundle_span test_runs`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add stockify/sessionconfig.py stockify/runconfig.py stockify/bundle.py docs/plan/A1_schemas.md docs/plan/00_overview.md tests/stockify/test_session_bundle.py
git commit -m "REG 5.1: session configs and day bundles resolved without packs"
```

---

### Task 9: Sessions, lifecycle with stacked pauses, timeline and commands (HOL 5.1; M5.2)

**Files:**
- Create: `stockify/daemon/lifecycle.py`, `tests/stockify/daemon/test_lifecycle.py`
- Modify: `stockify/store.py`

**Interfaces:**
- Produces (store):
  - `create_session(session_id: str, name: str, cfg: dict, created_ns: int) -> None`, which raises on an unsafe id.
  - `get_session(session_id) -> dict | None`: `pause_reasons` is parsed into a sorted list.
  - `list_sessions(*, status: str | None = None) -> list[dict]`.
  - `set_session_state(session_id, status: str, reasons: list[str]) -> None`.
  - `add_timeline(session_id: str, kind: str, detail: dict, *, wall_ns: int, engine_ns: int | None) -> None`; `timeline(session_id, *, offset=0, limit=200) -> list[dict]`.
  - `upsert_session_day(session_id, trade_date: str, **fields) -> None`; `get_session_day(session_id, trade_date) -> dict | None`.
  - `add_command(session_id: str | None, command: str, args: dict | None, created_ns: int) -> int`; `pending_commands() -> list[dict]` (oldest first); `mark_command(cmd_id: int, applied_ns: int, result: str) -> None`.
- Produces (lifecycle):
  - `STATES = ("CREATED", "ARMED", "RUNNING", "PAUSED", "STOPPED")`.
  - `PAUSE_REASONS = ("owner", "stop_all", "token_expired", "parity_failure", "determinism", "error", "stopped")`.
  - `class LifecycleError(ValueError)`.
  - `pause(session_id, reason, *, wall_ns, engine_ns=None) -> list[str]` adds a reason and returns the set. RUNNING → PAUSED; ARMED stays ARMED with the reason kept.
  - `resume(session_id, reason, *, wall_ns, engine_ns=None) -> list[str]` removes **only** that reason. PAUSED → RUNNING only when the set becomes empty. `owner` also clears `stop_all`.
  - `arm(session_id, *, wall_ns) -> None` covers CREATED/RUNNING/PAUSED → ARMED, with reasons kept.
  - `start_continuous(session_id, *, wall_ns, engine_ns) -> str` covers ARMED → RUNNING if the set is empty, otherwise → PAUSED.
  - `stop(session_id, *, wall_ns) -> None`: STOPPED is reached at finalisation of the day after a `stop` command (Task 1 rule).
  - Every change writes a `state` timeline row `{from, to, reasons}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_day_cycle(self): ...                                          # CREATED→ARMED→RUNNING→ARMED
def test_owner_pause_survives_token_expiry_and_login(self):
    lifecycle.pause("s1", "owner", wall_ns=1); lifecycle.pause("s1", "token_expired", wall_ns=2)
    self.assertEqual(lifecycle.resume("s1", "token_expired", wall_ns=3), ["owner"])
    self.assertEqual(store.get_session("s1")["status"], "PAUSED")
def test_reasons_carry_through_arm_and_start(self): ...                # ARMED keeps owner; start_continuous → PAUSED
def test_stopped_is_final(self): ...
def test_commands_come_back_oldest_first_until_marked(self): ...
```

- [ ] **Step 2: Run them to verify they fail.** `tests\run_tests.py test_lifecycle`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests\run_tests.py test_lifecycle test_store`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add stockify/daemon/lifecycle.py stockify/store.py tests/stockify/daemon/test_lifecycle.py
git commit -m "HOL 5.1: session states with stacked pause reasons, timeline rows and owner commands"
```

---

### Task 10: The journal (DM 5.1; M5.3 journal-first; D-68)

**Files:**
- Create: `stockify/daemon/journal.py`, `tests/stockify/daemon/paper_fixture.py`, `tests/stockify/daemon/test_journal.py`
- Modify: `stockify/paths.py` (`journal_file`, `day_bundle_file`, `journal_day_dir`), `docs/plan/A4_engine_semantics.md:44` (D-68), `docs/plan/00_overview.md` (D-68)

**Interfaces:**
- Produces:
  - `paths.journal_file(session_id: str, day: date) -> Path` = `journals/<sid>/<YYYYMMDD>.evt`; `paths.day_bundle_file(...)` = `….bundle.json`; `paths.journal_day_dir(...)` = `journals/<sid>/<YYYYMMDD>/`. All three check `safe_segment`.
  - `journal.ordered_t(intended_t: int, rank: int, last_t: int | None, last_rank: int | None) -> int` returns `max(intended_t, last_t)`, then `last_t + 1` if the result equals `last_t` and `rank < last_rank`. Journal `t` may therefore differ from `recv_ns`.
  - `class JournalWriter(path: Path, *, fsync_every_mono_ns: int, mono: Callable[[], int], price_basis: str = "raw")`:
    - `append(records: list[tuple[int, int, bytes]]) -> bytes` takes (intended t, rank, record), sets `t` and `seq`, writes, flushes, and returns the exact bytes for `push`;
    - `fsync() -> None`; `close(*, finalise: bool) -> None`;
    - `last_t`, `last_rank`, `next_seq`, `records`.

    The fsync cadence uses the monotonic clock, which is never written into records.
  - `journal.read(path: Path) -> tuple[dict, bytes, int]` returns (header, whole records, torn-tail byte count).
  - `paper_fixture.PaperDayTest(SandboxedDBTest)` with `db_modules = (store, refdata, history)`, DAY 2026-11-17 (a NIFTY expiry):
    - `record(seconds=1200)` records a landing day: index, near future and 12 CE strikes at a 1 s cadence, 09:15–09:35, with heartbeats; plus `fo_eod`/`settlement_prices` rows;
    - `session_cfg(**over) -> dict`, `t(hhmmss) -> int`;
    - a static universe (every fixture strike inside `n_full`), so subscriptions are the same after a restart.

- [ ] **Step 1: Write the failing tests**

```python
def test_lower_rank_at_same_t_is_bumped_one_ns(self):
    self.assertEqual(journal.ordered_t(100, 2, 100, 4), 101); self.assertEqual(journal.ordered_t(100, 4, 100, 4), 100)
    self.assertEqual(journal.ordered_t(90, 5, 100, 4), 100)
def test_appended_bytes_are_accepted_by_the_engine_in_any_batching(self): ...   # batches of 1, 3, all → same drained outputs
def test_torn_tail_is_ignored_and_writer_continues(self):
    # 10 CLOCK records (24 bytes each); truncate 7 bytes: read() gives 9 records and tail 17; the next seq is 10
def test_finalise_sets_count_and_bounds(self): ...
def test_journal_reads_like_a_pack_after_finalise(self): ...
```

- [ ] **Step 2: Run them to verify they fail.** `tests\run_tests.py test_journal`. Expected: FAIL.
- [ ] **Step 3: Implement.** The writer truncates a torn tail before appending.
- [ ] **Step 4: Run** `tests\run_tests.py test_journal test_packs`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add stockify/daemon/journal.py stockify/paths.py tests/stockify/daemon/paper_fixture.py tests/stockify/daemon/test_journal.py docs/plan/A4_engine_semantics.md docs/plan/00_overview.md
git commit -m "DM 5.1: append-only session journals the engine always accepts"
```

---

### Task 11: Frame taps and the frame encoder (DM 5.2; M5.1 fan-out)

**Files:**
- Create: `stockify/daemon/encoder.py`, `stockify/config/paper.json`, `tests/stockify/daemon/test_encoder.py`
- Modify: `stockify/daemon/recorder.py:120-131` (`taps`), `stockify/config/loader.py` (the `stockify.paper/1` check)

**Interfaces:**
- Produces:
  - `Recorder.taps: list[Callable[[tuple], None]]`: each is called in `on_message`, for frames **and** heartbeats, with the stamped `(frame_no, recv_ns, mono_ns, payload, is_binary)`. Each must be a `put_nowait`.
  - `paper.json`:

    ```json
    {"schema": "stockify.paper/1", "clock_every_s": 1, "journal": {"fsync_every_s": 5},
     "dispatcher_queue_max": 200000, "margin_poll": {"debounce_s": 2, "every_s": 300, "max_rps": 10},
     "token_poll_s": 15, "coverage_threshold": "0.98", "commands_every_s": 1, "sod_state_warn_mb": 64}
    ```

  - `class FrameEncoder(token_map: dict[int, int])`; `encode(payload: bytes) -> list[tuple[int, bytes]]` gives `(instrument_id, record)` per data packet. It unpacks `packs.enc_tick({"raw": raw}, iid, recv_ns)` → `(kind, record)` and keeps the record. 8-byte LTP packets and unknown tokens are skipped and counted (`stats.ltp_skipped`, `stats.unknown_tokens`). Heartbeats give `[]`.

- [ ] **Step 1: Write the failing tests**: `test_tap_sees_identical_stamps_to_landing` (replay 50 frames; the landing's `(frame_no, recv_ns)` equals the tapped stamps); `test_heartbeats_are_tapped`; `test_encoder_matches_pack_encoder` (same bytes as `packs.build` for the fixture day's packets); `test_ltp_packets_skipped_and_counted`.
- [ ] **Step 2: Run them to verify they fail.** `tests\run_tests.py test_encoder`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests\run_tests.py test_encoder test_recorder test_config_loader`, then `python -m stockify config`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add stockify/daemon/encoder.py stockify/daemon/recorder.py stockify/config/paper.json stockify/config/loader.py tests/stockify/daemon/test_encoder.py
git commit -m "DM 5.2: one stamped frame to two consumers; the pack encoder for live frames"
```

---

### Task 12: One session-day: journal first, then push and store (DM 5.3; M5.3)

**Files:**
- Create: `stockify/daemon/session_day.py`, `tests/stockify/daemon/test_session_day.py`
- Modify: `stockify/store.py` (`apply_session_outputs`, `session_rows`), `tests/stockify/test_secrets.py` (a session-day scenario)

**Interfaces:**
- Consumes: `JournalWriter` (10), `resolve_session_day` (8), `lifecycle` (9), `strategy_control` (7), the engine constructor applying `sod_state` (6).
- Produces:
  - `session_engine(bundle_path: Path, cfg: dict)` builds an `Engine` from the bundle (which applies `sod_state`) and sets the strategy. It is the **only** place sessions construct an engine; Tasks 16, 17, 22 and 23 use it.
  - `class SessionDay(session_id: str, day: date, cfg: dict, *, mono: Callable[[], int])`:
    - `open(sod_state: dict | None, reasons: list[str], t0: int) -> None` writes the day bundle, builds the engine, and opens the journal. If the state's `strategy_enabled`/`pause_reasons` differ from `reasons`, its first records are `strategy_control(enabled = not reasons, reasons)` at `t0`;
    - `ids: frozenset[int]`;
    - `feed(records: list[tuple[int, int, bytes]], last_frame: tuple[int, int] | None) -> None` appends, pushes, drains, then calls `store.apply_session_outputs`, which also stores `last_frame_no`/`last_frame_recv_ns`;
    - `control(enabled: bool, reasons: list[str], t: int) -> None`; `fsync() -> None`.
  - `store.apply_session_outputs(session_id, trade_date, outputs: dict, *, first_event_no: int, last_frame: tuple[int, int] | None) -> int`. In one transaction it upserts orders, fills and charges by id, numbers order events, updates each order's `status` from its newest event, and stores the frame mark. It returns the next event_no.
  - `store.session_rows(session_id, table, *, trade_date=None, offset=0, limit=500) -> list[dict]`.

- [ ] **Step 1: Write the failing tests** (`PaperDayTest`; records fed directly, without threads; engine required)

```python
def test_journal_is_written_before_push(self): ...      # a patched push asserts the journal already ends with the batch
def test_one_fill_and_rows(self): ...                   # BuyOnFirstTick → one session_fills row; journal tail 0
def test_reapplying_outputs_is_harmless(self): ...
def test_open_journals_control_when_state_disagrees(self): ...   # state enabled, reasons ["owner"] → first record disabled
def test_frame_mark_stored_with_outputs(self): ...
def test_no_secret_in_sandbox_after_a_session_day(self):        # plant_secrets(); assert_no_secret(sandbox, "FAKETOKEN-123")
```

- [ ] **Step 2: Run them to verify they fail.** `tests\run_tests.py test_session_day`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests\run_tests.py test_session_day test_secrets`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add stockify/daemon/session_day.py stockify/store.py tests/stockify/daemon/test_session_day.py tests/stockify/test_secrets.py
git commit -m "DM 5.3: a session-day journals first, then pushes and stores its outputs"
```

---

### Task 13: The dispatcher: one FIFO, CLOCK from stamps, gaps and commands (DM 5.4; M5.1; D-72)

**Files:**
- Create: `stockify/daemon/dispatcher.py`, `tests/stockify/daemon/test_dispatcher.py`
- Modify: `stockify/daemon/ticker.py` (`RecorderSession.gap_taps`), `docs/plan/00_overview.md` (D-72)

**Interfaces:**
- Consumes: `Recorder.taps`, `FrameEncoder` (11); `SessionDay` (12); `store` commands and `lifecycle` (9).
- Produces:
  - `RecorderSession.gap_taps: list[Callable[[str, str, int, int | None], None]]` with arguments `(phase "start"|"end", reason, t_ns, instrument_id | None)`. They are called beside every landing `open_gap`/`close_gap`, with the same `now`, on the thread that saw the event (ticker callbacks, or the main thread for stalls).
  - Queue items:
    - `("frame", stamp_tuple)`;
    - `("gap", phase, reason, t_ns, iid)`;
    - `("inject", t_ns, session_id | None, rank, record)`: bands, margin answers, controls;
    - `("command", t_ns, row)`.

    Every producer stamps with `clock.now_ns()` when it enqueues and calls `put_nowait`.
  - `class Dispatcher(sessions: list[SessionDay], encoder: FrameEncoder, *, clock: Clock, cfg: dict, replay: bool)`:
    - `put_frame(item)` (a Recorder tap), `put_gap(phase, reason, t, iid)` (a gap tap), `put_inject(t, session_id, rank, record)`;
    - `run(stop: threading.Event) -> None` (the engine thread), `drain() -> None`, `stats`.
  - Rules:
    - **CLOCK:** for each dequeued item stamped `s`, if `floor(s / every) * every > last_clock`, emit `CLOCK(that boundary)` before the item's records. Time comes only from stamps, never from the wall clock.
    - Commands are polled from `store.pending_commands()` every `commands_every_s` of **stamp** time and enqueued as `command` items. They are applied in FIFO order: pause/resume/stop → `lifecycle` + `strategy_control`; stop_all → every session.
    - Stall gaps reach session journals only when `replay` is False; in replay they become a timeline row `stall_replay_only`.
    - A full queue counts the drop, raises alarm `dispatcher_queue_full`, and journals a scope-all `GAP(stall)` start/end around the loss, with timeline detail `{"cause": "dispatcher_overflow"}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_two_replays_give_identical_journals(self):   # same fixture day at max, twice, separate sandboxes: journal bytes equal
def test_clock_from_stamps_not_wall(self): ...         # a slow engine thread (sleep in push) gives the same journal
def test_injected_disconnect_gives_a_journal_gap_pair(self): ...   # replay fault disconnect@09:20:00+30s
def test_only_session_instruments_reach_its_journal(self): ...
def test_stop_all_command_journals_strategy_control(self): ...
def test_overflow_is_a_gap_not_silence(self): ...
```

- [ ] **Step 2: Run them to verify they fail.** `tests\run_tests.py test_dispatcher`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests\run_tests.py test_dispatcher test_session test_recorder`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add stockify/daemon/dispatcher.py stockify/daemon/ticker.py docs/plan/00_overview.md tests/stockify/daemon/test_dispatcher.py
git commit -m "DM 5.4: one queue in, one engine thread; time comes from the frames"
```

---

### Task 14: Daemon composition and graceful shutdown (GK 5.1; M5.1)

**Files:**
- Create: `stockify/daemon/market_day.py`, `tests/stockify/daemon/test_market_day.py`
- Modify: `stockify/daemon/__init__.py` (`run_recorder` becomes `run_market_day(session_ids=[])`), `stockify/__main__.py:612-619` (`daemon --sessions all|none|<id,…>`)

**Interfaces:**
- Consumes: `Dispatcher` (13), `SessionDay` (12), `RecorderSession` (existing).
- Produces:
  - `run_market_day(*, ticker_spec: str = "live", faults: list[str] | None = None, stop: threading.Event | None = None, power=None, session_ids: list[str] | None = None, token_fresh: Callable[[], bool] | None = None) -> int`.
  - Threads: the ticker (library), `recorder-writer`, `engine` (Dispatcher), and main (health tick, subscriptions, band snapshots; live only).
  - Shutdown on Ctrl+C or Ctrl+Break (`signal.SIGBREAK`):
    1. stop the ticker;
    2. drain both queues;
    3. fsync the journals;
    4. **if inside the session's market hours**, journal a scope-all `GAP(process_stop)` start;
    5. `close(finalise=False)`;
    6. write `daemon_status.json` (`last_frame_no`, `last_frame_recv_ns`);
    7. return the exit code.

- [ ] **Step 1: Write the failing tests**: `test_replay_day_with_one_session_end_to_end` (E-01 part 1, exit 0); `test_ctrl_break_drains_and_journals_process_stop`; `test_no_process_stop_gap_after_close`; `test_recorder_only_still_works`.
- [ ] **Step 2: Run them to verify they fail.** `tests\run_tests.py test_market_day`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests\run_tests.py stockify.daemon`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add stockify/daemon/market_day.py stockify/daemon/__init__.py stockify/__main__.py tests/stockify/daemon/test_market_day.py
git commit -m "GK 5.1: the daemon runs the recorder and the paper sessions from one ticker"
```

---

### Task 15: Market data for sessions (RM 5.1; M5.5)

**Files:**
- Create: `tests/stockify/daemon/test_session_market_data.py`
- Modify: `stockify/daemon/market_day.py`, `stockify/data/limits.py` (`Snapshotter.on_band`), `stockify/daemon/dispatcher.py`

**Interfaces:**
- Consumes: `SubscriptionManager.pin/unpin`, `Engine.book()` (6), `Dispatcher.put_inject` (13).
- Produces:
  - Pin owner `session:<id>`. At ARM, `SessionDay.ids` is pinned (FULL), so a re-centre never drops a session instrument. After each batch, `book()` positions are kept pinned until flat.
  - At ARM the total subscription count is checked against `kite_limits.json` → `ws_max_instruments` (3000). If it is over, the session stays ARMED with timeline `subscription_cap` and `notify` (Task 25).
  - `Snapshotter.on_band(iid, lower, upper, t)` → `put_inject(t, None, 2, enc_refdata(iid, t, "price_band", …))`. This is live only; replay uses no band snapshots.
  - A tick for an instrument outside the bundle is not journaled. It writes timeline `unbundled_instrument`, once per instrument and day, and adds the label `unbundled_instrument` to the day.

- [ ] **Step 1: Write the failing tests**: `test_recentre_never_drops_a_pinned_strike`; `test_open_position_stays_pinned_until_flat`; `test_price_band_reaches_journal_in_fifo_order`; `test_unbundled_instrument_logged_once_and_labelled`; `test_universe_over_cap_does_not_arm`.
- [ ] **Step 2: Run them to verify they fail.** `tests\run_tests.py test_session_market_data`. Expected: FAIL.
- [ ] **Step 3: Implement.** Pin diffs go through `call_in_ticker`.
- [ ] **Step 4: Run** `tests\run_tests.py test_session_market_data test_subscriptions test_limits`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add stockify/daemon/market_day.py stockify/data/limits.py stockify/daemon/dispatcher.py tests/stockify/daemon/test_session_market_data.py
git commit -m "RM 5.1: sessions pin their instruments; price bands are journaled"
```

---

### Task 16: Finalising a day (DM 5.5; M5.3)

**Files:**
- Create: `stockify/daemon/finalise.py`, `tests/stockify/daemon/test_finalise.py`
- Modify: `stockify/data/packs.py` (public `settle_records(ids: set[int], day: date) -> list[tuple[int, int, bytes]]` wrapping `_settle_events`)

**Interfaces:**
- Consumes: `JournalWriter`, `journal.read` (10), `session_engine` (12), `lifecycle.arm` (9), `sod_state.size_warning` (5).
- Produces:
  - `replay_into(engine, journal_path: Path, batch_records: int = 2000) -> int` pushes the journal's whole records and returns the count. Tasks 17, 22 and 23 also use it.
  - `@dataclass FinaliseResult: session_id: str; day: date; status: str  # finalised | waiting | failed; labels: list[str]; settle_count: int; error: str | None`.
  - `finalise_day(session_id: str, day: date, *, files_complete: bool, cutoff_passed: bool) -> FinaliseResult`:
    - It returns `waiting` while `not files_complete and not cutoff_passed`.
    - Otherwise it reopens the journal and appends the SETTLE records (`ordered_t`, so `t = max(publication, last t)`).
    - It makes a fresh engine via `session_engine` + `replay_into`, unless the live engine is still in this process. Then it runs `finish()` and `write_artifacts(paths.journal_day_dir(...))`.
    - It replaces `session_orders` with the rows of `orders.csv`, and stores `session_positions_eod`, `session_equity`, `eod_state_json`, `status = finalised`, `finalised_ns` and the labels (`settlement_fallback` when the cut-off forced it). It applies the size warning to the state.
    - It calls `close(finalise=True)`, then `lifecycle.arm` (reasons kept), or `stop` if a stop command was applied that day. **It never writes `GAP(process_stop)`.**
  - `next_sod_state(session_id: str, day: date) -> dict | None` returns the previous trading day's `eod_state_json`. It raises `FinaliseError` when that day exists but is not finalised.

- [ ] **Step 1: Write the failing tests**: `test_finalise_settles_expiry_and_stores_eod`; `test_waits_for_files_until_cutoff`; `test_finalise_after_cutoff_uses_fallback_label`; `test_settle_t_is_never_before_last_journal_t`; `test_next_day_refuses_unfinalised_previous_day`; `test_two_day_session_equals_a_two_day_per_day_run`; `test_finalise_in_a_fresh_process_writes_no_process_stop_gap`.
- [ ] **Step 2: Run them to verify they fail.** `tests\run_tests.py test_finalise`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests\run_tests.py test_finalise test_packs test_session_day`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add stockify/daemon/finalise.py stockify/data/packs.py tests/stockify/daemon/test_finalise.py
git commit -m "DM 5.5: each session-day is finalised after NSE's settlement prices"
```

---

### Task 17: Crash recovery (GK 5.2; M5.4)

**Files:**
- Create: `stockify/daemon/recovery.py`, `tests/stockify/daemon/test_recovery.py`
- Modify: `stockify/daemon/market_day.py` (recover before the first push)

**Interfaces:**
- Consumes: `replay_into` (16), `session_engine`, `SessionDay` (12), `lifecycle` (9), store commands (9).
- Produces:
  - `@dataclass RecoveryResult: session_id: str; day: date; records: int; written: int; tail_lost_rows: int; mismatch: str | None`.
  - `recover(session_id: str, day: date) -> tuple[SessionDay, RecoveryResult]`: a fresh engine from the saved day bundle (which carries `sod_state`) plus `replay_into`, then the regenerated outputs are compared with the stored rows:
    - stored rows must equal their regenerated twins;
    - regenerated rows missing from the database are written;
    - database rows beyond the journal (power loss) are deleted and timelined as `journal_tail_lost`;
    - any other difference → `lifecycle.pause(…, "determinism")`, timeline `determinism_mismatch` with the first differing row, and `log.error`. Task 25 adds `notify`.
  - Pending commands are applied, and their `strategy_control` journaled, **before** the first live push.
  - In live mode, a restart inside market hours journals the `GAP(process_stop)` end at the first new frame.

- [ ] **Step 1: Write the failing tests**: `test_recovery_writes_rows_missing_from_db`; `test_recovery_db_ahead_of_journal_is_tail_lost_not_parity`; `test_recovery_mismatch_pauses_with_determinism` (one tampered stored fill); `test_pending_stop_all_applies_before_first_push_after_restart`; `test_day2_recovery_uses_day1_state`.
- [ ] **Step 2: Run them to verify they fail.** `tests\run_tests.py test_recovery`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests\run_tests.py stockify.daemon`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add stockify/daemon/recovery.py stockify/daemon/market_day.py tests/stockify/daemon/test_recovery.py
git commit -m "GK 5.2: a restarted daemon replays today's journal and checks it against the stored rows"
```

---

### Task 18: Replay resume, fault durations and a real kill; E-02 (GK 5.3; M5.4; D-71)

**Files:**
- Create: `tests/stockify/daemon/test_e02_kill.py`
- Modify: `stockify/daemon/replay_ticker.py` (`open_replay(..., resume_after: tuple[int, int] | None)`, fault durations, `on_kill` + `TerminateProcess`), `stockify/daemon/market_day.py` (resume mark), `docs/plan/00_overview.md` (D-71), `docs/plan/M5_paper_daemon.md` (tick M5.4)

**Interfaces:**
- Consumes: `recover` (17); `session_days.last_frame_no/last_frame_recv_ns` (2, 12); `daemon_status.json` (14).
- Produces:
  - **Resume mark.** Each session-day keeps the `(frame_no, recv_ns)` of its last dispatched frame; the process keeps the minimum over its sessions in `daemon_status.json`.
    - On a replay restart the ticker skips source frames with `recv_ns <= process_mark_recv_ns`. Each session also skips frames at or below its own mark.
    - The comparison uses the dispatched frame's **stamp**, not journal `t`, so `ordered_t` bumps do not matter.
    - The kill fires before the first frame with `t >= at`, so frames sharing a stamp are all on one side.
  - **Fault durations.** A `disconnect@T+Ns` or `token_expiry@T+Ns` restart resumes at `T + N`. The frames in between are lost, and the open `disconnect`/`token_expired` GAP closes at the first new frame, with no extra `process_stop` GAP.
  - `kill@T`: `ReplayTicker.on_kill()` (the daemon drains both queues and fsyncs the journals), then `ctypes.windll.kernel32.TerminateProcess(GetCurrentProcess(), 137)`. Nothing is lost (D-71); live restarts always journal `GAP(process_stop)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_kill_then_restart_equals_uninterrupted(self):          # E-02, real subprocesses, timing-independent (C-1)
    twin = run_cli_in_sandbox("twin", f"daemon --ticker replay:{DAY}:max --sessions s1")
    k = run_cli_in_sandbox("k", f"daemon --ticker replay:{DAY}:max --sessions s1 --inject kill@09:25:00")
    self.assertEqual(k.returncode, 137)
    self.assertEqual(run_cli_in_sandbox("k", f"daemon --ticker replay:{DAY}:max --sessions s1").returncode, 0)
    finalise both; self.assertEqual(compare.compare(day_folder("k"), day_folder("twin")), [])
    self.assertEqual(journal_bytes("k"), journal_bytes("twin"))
def test_token_expiry_duration_resumes_at_t_plus_n(self): ...    # GAP token_expired 09:20:00 → first frame ≥ 09:22:00
def test_resume_skips_frames_at_or_below_each_session_mark(self): ...
```

- [ ] **Step 2: Run them to verify they fail.** `tests\run_tests.py test_e02_kill test_replay_ticker`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests\run_tests.py stockify.daemon`. Expected: PASS. Then by hand: run `daemon --ticker replay:<fixture>:10x --inject kill@…`, check that `Get-Process python` is gone, restart, and look for `recovered s1: N records, 0 mismatches` in the log.
- [ ] **Step 5: Commit**

```bash
git add stockify/daemon/replay_ticker.py stockify/daemon/market_day.py tests/stockify/daemon/test_e02_kill.py docs/plan/00_overview.md docs/plan/M5_paper_daemon.md
git commit -m "GK 5.3: a killed replay resumes where it stopped and matches an uninterrupted run"
```

---

### Task 19: Token expiry and the daily login (LCB 5.2; M5.7, E-03; D-65)

**Files:**
- Create: `stockify/daemon/token_watch.py`, `tests/stockify/daemon/test_token_watch.py`, `tests/test_launcher_codes.py`
- Modify:
  - `stockify/daemon/market_day.py`: on a 403, the token_expired GAP start comes through the gap tap, then `strategy_control(false)`, `lifecycle.pause(…, "token_expired")`, the status file, and exit 76;
  - `stockify/daemon/__init__.py` (`run_daemon`);
  - `scripts/run_stockify_daemon.ps1`: on exit 76, restart after `$MinDelay`; remove `Wait-FreshToken`;
  - `stockify/daemon/reminder.py`: the PST/PDT window from `calendar.owner_tz()`;
  - `server/routers/kite.py`: `/api/kite/status` gains `daemon_waiting_for_login`;
  - `docs/plan/00_overview.md` (D-65).

**Interfaces:**
- Produces:
  - `token_watch.write_status(**fields)` / `read_status() -> dict` (`$LAB/daemon_status.json`): `{waiting_for_login, since_ns, sessions_paused, rejected_token_mtime_ns, last_frame_no, last_frame_recv_ns}`. It never holds a token value.
  - `token_watch.token_mtime_ns() -> int | None` stats `kite_client._token_file()` and never opens it.
  - `token_watch.wait_for_login(clock, stop, *, poll_s: float, token_fresh: Callable[[], bool], on_idle: Callable[[], None], until_ns: int | None) -> bool`. It is True when `token_fresh()` is true **and** (`rejected_token_mtime_ns` is unset, or `token_mtime_ns()` is greater than it). `on_idle` runs on every poll: the scheduler's catch-up jobs and the evening finalisation.
  - On resume: the GAP end at the first frame, `lifecycle.resume(…, "token_expired")`, `strategy_control` per the remaining reasons, and timeline `login`.

- [ ] **Step 1: Write the failing tests**

```python
def test_token_expiry_mid_day_pauses_exits_76_and_resumes_after_login(self):
    self.assertEqual(run_market_day(ticker_spec=f"replay:{DAY}:max", faults=["token_expiry@09:20:00+120s"], session_ids=["s1"]),
                     exitcodes.TOKEN_EXPIRED)
    self.assertIn("token_expired", store.get_session("s1")["pause_reasons"])
    self.assertEqual(run_market_day(ticker_spec=f"replay:{DAY}:max", faults=["token_expiry@09:20:00+120s"],
                                    session_ids=["s1"], token_fresh=lambda: True), exitcodes.OK)
    self.assertEqual(gap_reasons(journal("s1")), ["token_expired"])
def test_rejected_token_waits_for_a_newer_token_file(self): ...     # fresh by the 06:00 rule but same mtime → keeps waiting
def test_login_wait_still_runs_catch_up(self): ...                  # on_idle ran finalise, parity, daily_summary for D-1
def test_stale_token_before_first_connect_waits_in_process(self): ...
def test_launcher_restarts_immediately_on_76(self):
    text = Path("scripts/run_stockify_daemon.ps1").read_text()
    self.assertNotIn("Wait-FreshToken", text)
    self.assertRegex(text, r"(?s)\$code -eq \$ExitToken\).*?Start-Sleep -Seconds \$MinDelay.*?continue")
```

- [ ] **Step 2: Run them to verify they fail.** `tests\run_tests.py test_token_watch test_launcher_codes`. Expected: FAIL.
- [ ] **Step 3: Implement.** Keep the `launcher-codes` block and run blockwatch.
- [ ] **Step 4: Run** `tests\run_tests.py stockify.daemon test_launcher_codes test_reminder`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add stockify/daemon/token_watch.py stockify/daemon/market_day.py stockify/daemon/__init__.py stockify/daemon/reminder.py scripts/run_stockify_daemon.ps1 server/routers/kite.py tests/stockify/daemon/test_token_watch.py tests/test_launcher_codes.py docs/plan/00_overview.md
git commit -m "LCB 5.2: token expiry pauses sessions; the next process waits for a newer login"
```

- [ ] **Step 6: 🛑 Live verification (owner).** Before one trading day, skip the evening login.
  - The 06:05 and 08:00 IST reminders arrive.
  - `/trade` shows "Log in to Kite".
  - Log in at about 19:20 PST: the session goes RUNNING, and the timeline shows `login` and the gap.

  Record it in RECON. Until then: "code done, live pending". This step does not block later tasks.

---

### Task 20: The live margin model, oracle and engine (MEZ 5.1; M5.6 determinism; D-69)

**Files:**
- Create: `stockify/fno/live_margin.py`, `engine/src/margin/live.cpp`, `engine/include/stocksim/margin_live.hpp`, `tests/stockify/test_live_margin.py`
- Modify:
  - `stockify/runconfig.py:23-25` and `docs/plan/A1_schemas.md:307-309` (margin-models block: append `live_with_fallback`);
  - `stockify/oracle/runner.py` and `engine/src/engine.cpp`: answers are stored in the Engine (`on_refdata`), and the model gets a lookup;
  - `stockify/oracle/artifacts.py:32-34`, `engine/src/artifacts.cpp:23-26` and A1 §6.3 (the L1 row rule);
  - `engine/bindings/engine.cpp` (`margin_now()`), `engine/CMakeLists.txt`;
  - `stockify/sessionconfig.py` (default `live_with_fallback`);
  - `docs/plan/00_overview.md` (D-69, with the text of mismatch row 6).

**Interfaces:**
- Produces:
  - The `margin_requirement` REFDATA payload, integers only:

    ```
    {"portfolio_sha256": str, "legs": [[iid, units], …] (sorted), "span_paise": int, "exposure_paise": int,
     "additional_paise": int, "option_premium_paise": int, "requested_ns": int, "source": "kite_basket"}
    ```

  - `live_margin.portfolio_sha256(legs: list[tuple[int, int]]) -> str`: sha256 of `dumps_canonical` of the sorted `[[iid, units]]` of exactly the staircase `_group` legs (NRML, kind in FUT/CE/PE, with `underlying_id`, units ≠ 0).
  - `class LiveAnswers` (owned by the Engine): `put(payload, t)`, `fresh(sha: str, now: int, fresh_ns: int) -> dict | None`.
  - `class LiveWithFallbackMargin(StaircaseMargin)(bundle, engine, answers: LiveAnswers)`:
    - `requirement()` is L1 when `answers.fresh(hash, engine.now, live_fresh_s)` exists: `span + exposure + additional` (D-62 excludes premium). Otherwise it is the staircase.
    - Because `funds().available` uses `requirement()`, **L1 reaches every pre-trade check through available funds**.
    - `pair()` (the pre-trade increment) always uses the staircase.
    - It counts L1 checks; labels are `margin:live_with_fallback` and `margin_live:L1_checks=<n>`.
  - The L1 row of `margin_daily.csv` (A1 §6.3): when L1 is fresh at DAY_END, one row `underlying_id = 0, model = live_with_fallback, step = L1_kite, scan = span, somc = 0, elm = exposure, addons = additional, total`, and no per-underlying rows that day. Otherwise there are the staircase rows with their B-step. (With `live_fresh_s` = 360, DAY_END at 23:59 IST is normally staircase.)
  - Binding `Engine.margin_now() -> dict`: `{"legs", "portfolio_sha256", "fallback": [per-underlying rows], "l1": bool}`. It is called **only on the engine thread**, between pushes.

- [ ] **Step 1: Write the failing tests**

```python
def test_fresh_matching_answer_is_used_and_counted(self): ...      # both engines, rows equal
def test_answer_for_another_portfolio_is_ignored(self): ...
def test_stale_answer_falls_back_to_staircase(self): ...
def test_pre_trade_increment_never_uses_l1(self): ...              # pair() before/after both staircase
def test_l1_changes_available_funds_deterministically(self):       # an L1 answer below the staircase lets an order through
    self.assertEqual(rejects(with_answer), []); self.assertEqual(rejects(without_answer), ["R_FUNDS"])
    self.assertEqual(replay_twice(with_answer)[0], replay_twice(with_answer)[1])
def test_l1_row_rule_at_day_end(self): ...
def test_fuzz_worlds_with_random_answers_agree(self): ...          # 100 seeds
```

- [ ] **Step 2: Run them to verify they fail.** `tests\run_tests.py test_live_margin`. Expected: FAIL.
- [ ] **Step 3: Implement** in the oracle first, then the engine.
- [ ] **Step 4: Run** `.\run.ps1 -BuildEngine`, then `tests\run_tests.py test_live_margin test_staircase test_engine_fuzz test_session_bundle`, then blockwatch. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add stockify/fno/live_margin.py engine/src/margin/live.cpp engine/include/stocksim/margin_live.hpp stockify/runconfig.py stockify/oracle/runner.py stockify/oracle/artifacts.py engine/src/engine.cpp engine/src/artifacts.cpp engine/bindings/engine.cpp engine/CMakeLists.txt stockify/sessionconfig.py docs/plan/A1_schemas.md docs/plan/00_overview.md tests/stockify/test_live_margin.py
git commit -m "MEZ 5.1: Kite's journaled margin prices the book; the staircase prices every increment"
```

---

### Task 21: Asking Kite, and the disagreement log (IWB 5.1; M5.6 polling; spec §5.3, §9)

**Files:**
- Create: `stockify/daemon/margin_poller.py`, `stockify/fno/margin_live_report.py`, `stockify/data/migrations/lab/0003_margin_live.sql`, `tests/stockify/daemon/test_margin_poller.py`
- Modify:
  - `stockify/store.py`: `SCHEMA_VERSION = 3`, `add_margin_live`, `margin_live_rows`;
  - `tests/stockify/test_store_migrations.py` (version 3);
  - `stockify/fno/margin_check.py`: the scorecard reads `margin_live`; `check(..., live=True)`;
  - `stockify/__main__.py` (`margin live-report`, `margin check --live`);
  - `stockify/daemon/dispatcher.py`: the snapshot after each batch, the poke on fill.

**Interfaces:**
- Consumes: `kite_readonly.client()` (3), the Task 20 payload and `margin_now()`, `Dispatcher.put_inject` (13).
- Produces:
  - `0003_margin_live.sql` adds these columns to `margin_live` with `ALTER TABLE … ADD COLUMN`: `engine_ns INTEGER, trade_date TEXT, legs_json TEXT, market_json TEXT, span_file TEXT, step TEXT, kite_total_paise INTEGER, ours_total_paise INTEGER, gap_paise INTEGER, gap_ppm INTEGER, request_ns INTEGER, response_ns INTEGER, error TEXT`.
  - **Snapshots.** After each batch the engine thread puts a `MarginSnapshot(session_id, engine_ns, legs, portfolio_sha256, fallback)` from `margin_now()` on `poller.snapshots` (`put_nowait`, keep latest). It does this when the poller has a pending poke or `every_s` of stamp time has passed. **The poller never holds an Engine reference.**
  - `class MarginPoller(threading.Thread)(snapshots: queue.Queue, dispatcher, *, client_factory: Callable[[], ReadOnlyKite], clock: Clock, cfg: dict)`:
    - `poke(session_id)` is debounced by `debounce_s`;
    - at most `max_rps` requests per second;
    - each answer is converted to paise (`Decimal(str(x)) * 100`, the `calibrate` rule), then journaled through `put_inject`, then stored as a `margin_live` row with `model_json = fallback`;
    - errors (403, 429, network, unknown contract) give a row whose `error` text goes through `server.log_redaction.redact`, and no journal record. A 403 also starts Task 19's path;
    - it is disabled in replay;
    - for the disagreement log only, it may also ask Kite about the "after" book once an order is placed (`step = after_order`, not journaled).
  - `margin_live_report.report(*, start: date | None, end: date | None) -> str` groups gaps by portfolio type, underlying, IST hour and SPAN-file age. Its L2 column is computed in Python here only, labelled experimental.

- [ ] **Step 1: Write the failing tests**: `test_fill_debounced_2s_then_one_request`; `test_every_5_minutes_while_positions_open`; `test_answer_is_converted_journaled_and_logged_with_gap`; `test_kite_error_logs_redacted_row_without_journal_record`; `test_never_more_than_10_requests_per_second`; `test_poller_holds_no_engine_reference`; `test_consider_positions_false_on_the_wire`; `test_scorecard_counts_live_rows`; `test_live_report_groups`.
- [ ] **Step 2: Run them to verify they fail.** `tests\run_tests.py test_margin_poller`. Expected: FAIL.
- [ ] **Step 3: Implement.** Network calls happen on the poller thread only.
- [ ] **Step 4: Run** `tests\run_tests.py test_margin_poller test_margin_check test_store_migrations`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add stockify/daemon/margin_poller.py stockify/fno/margin_live_report.py stockify/data/migrations/lab/0003_margin_live.sql stockify/store.py tests/stockify/test_store_migrations.py stockify/fno/margin_check.py stockify/__main__.py stockify/daemon/dispatcher.py tests/stockify/daemon/test_margin_poller.py
git commit -m "IWB 5.1: live Kite margins journaled; every gap to our answer logged"
```

- [ ] **Step 6: 🛑 Live verification (owner + market hours).** Run one live session day with an F&O position. Check that:
  - `margin_live` has rows;
  - the journal has `margin_requirement` records;
  - `margin live-report` prints;
  - journal parity (Task 22) passes that day;
  - `margin check --live --leg …` prints Kite's answer and the staircase's.

  Record the result in RECON. This step does not block later tasks.

---

### Task 22: Nightly journal parity (RD 5.1; M5.10 gate; E-01)

**Files:**
- Create: `stockify/parity.py`, `tests/stockify/test_parity_journal.py`
- Modify: `stockify/__main__.py` (`parity journal --session ID --date YYYY-MM-DD`), `stockify/store.py` (`put_parity_result`)

**Interfaces:**
- Consumes: `session_engine` (12; it applies `sod_state`), `replay_into` (16), `lifecycle.pause` (9).
- Produces:
  - `@dataclass ParityResult: session_id: str; day: date; kind: str; outcome: str  # pass | fail | skipped; summary: dict`.
  - `journal_parity(session_id: str, day: date) -> ParityResult`: a fresh engine from the saved day bundle, `replay_into`, `finish()`, and `write_artifacts` into a temp folder. Orders, order events, fills, charges, positions and equity are compared with the stored rows; all must be identical, and the first difference is named. The result goes into `parity_results` and `session_days.parity`.
  - On `fail`: `lifecycle.pause(…, "parity_failure")`, a timeline row and `log.error` (Task 25 adds `notify`). This is the M5.10 🛑 to the owner (E-01).
  - CLI: exit 0 when identical; exit 1 on a difference, printing one `differs: <table> <first row>`.

- [ ] **Step 1: Write the failing tests**: `test_fixture_session_day_passes` (E-01); `test_day2_parity_passes` (SOD from day 1); `test_tampered_fill_fails_and_pauses`; `test_unfinalised_day_is_skipped`; `test_cli_exit_codes`.
- [ ] **Step 2: Run them to verify they fail.** `tests\run_tests.py test_parity_journal`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add stockify/parity.py stockify/__main__.py stockify/store.py tests/stockify/test_parity_journal.py
git commit -m "RD 5.1: every session-day is re-run from its journal and must match"
```

---

### Task 23: Recorder parity report (RD 5.2; M5.10 report)

**Files:**
- Create: `tests/stockify/test_parity_recorder.py`
- Modify: `stockify/parity.py` (`recorder_parity`), `stockify/__main__.py` (`parity recorder`)

**Interfaces:**
- Produces: `recorder_parity(session_id: str, day: date) -> ParityResult`, kind `recorder`.
  - It builds a pack from the recorder's archive or landing (`packs.build`, with the bundle's ids and a 1 s CLOCK).
  - It runs the same day bundle (same `sod_state`, applied by the constructor) with that pack.
  - It stores `summary = {"fills_live", "fills_recorder", "fills_matched", "pnl_diff_paise", "first_divergence_ns", "first_divergence"}`.
  - The outcome is `pass` or `skipped`. It never pauses a session.

- [ ] **Step 1: Write the failing tests**: `test_identical_inputs_give_zero_diff`; `test_journal_only_margin_records_explain_divergence`; `test_never_pauses`.
- [ ] **Step 2: Run them to verify they fail.** `tests\run_tests.py test_parity_recorder`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add stockify/parity.py stockify/__main__.py tests/stockify/test_parity_recorder.py
git commit -m "RD 5.2: how far a recorded-data backtest drifts from the live session"
```

---

### Task 24: Coverage (LW 5.1; M5.11)

**Files:**
- Create: `stockify/daemon/coverage.py`, `tests/stockify/daemon/test_coverage.py`
- Modify: `stockify/daemon/finalise.py`

**Interfaces:**
- Produces:
  - `@dataclass Coverage: fraction: Fraction; covered_s: int; continuous_s: int; gaps: list[dict]; below_threshold: bool`.
  - `coverage(bundle: dict, journal_bytes: bytes, ids: set[int], threshold: Fraction) -> Coverage`:
    - continuous time is the union of the CONTINUOUS phases of the groups of `ids`;
    - uncovered time is the union of GAP intervals that are scope-all or scoped to an id in `ids`, of any reason; an unclosed gap runs to the end of the phase;
    - the result is stored as `float` in `session_days.coverage` and as exact `"n/d"` in `coverage_json`.

- [ ] **Step 1: Write the failing tests**

```python
def test_full_day_no_gaps_is_1(self): ...
def test_90s_disconnect_in_a_6h25m_day(self):
    self.assertEqual(c.fraction, Fraction(22500 - 90, 22500)); self.assertFalse(c.below_threshold)
def test_gap_on_unrelated_instrument_does_not_count(self): ...
def test_unclosed_token_gap_runs_to_close_and_flags(self): ...
```

- [ ] **Step 2: Run them to verify they fail.** `tests\run_tests.py test_coverage`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests\run_tests.py test_coverage test_finalise`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add stockify/daemon/coverage.py stockify/daemon/finalise.py tests/stockify/daemon/test_coverage.py
git commit -m "LW 5.1: each session-day's coverage, and days with holes flagged"
```

---

### Task 25: Notifications and the daily summary (RW 5.1; M5.8)

**Files:**
- Create: `stockify/daemon/notify.py`, `tests/stockify/daemon/test_notify.py`
- Modify:
  - `stockify/config/notify.json`: shape change `{"channels": []}` → `{"channel": null, "events": {…}}`;
  - `stockify/config/loader.py`: the `stockify.notify/1` check updated to the new shape;
  - `stockify/daemon/reminder.py`: sends through `notify`;
  - `notify.send` call sites in `stockify/daemon/recovery.py`, `stockify/parity.py` and `stockify/daemon/market_day.py` (subscription cap, recorder stall, disk low);
  - `docs/plan/A1_schemas.md` §7.10.

**Interfaces:**
- Produces:
  - `notify.json`: `{"schema": "stockify.notify/1", "channel": null, "events": {"login_needed": true, "session_error": true, "parity_failure": true, "recorder_stall": true, "disk_low": true, "daily_summary": true}}`. `channel` is one of `null | "email" | "ntfy" | "telegram"`.
  - Channel secrets come from `.env` as `STOCKIFY_NTFY_TOPIC`, `STOCKIFY_TELEGRAM_TOKEN` and `STOCKIFY_TELEGRAM_CHAT`, read through `os.environ` and never logged. Email reuses `geo_monitor.email_notify`.
  - `notify.send(event: str, subject: str, body: str, *, sender=None) -> str | None` never raises. With `channel` null, or the event switched off, it only logs.
  - `notify.daily_summary(day: date) -> tuple[str, str]`: P&L (₹), coverage %, parity and labels for each session.

- [ ] **Step 1: Write the failing tests**: `test_default_is_log_only`; `test_event_switch_off_sends_nothing`; `test_ntfy_posts_without_logging_topic`; `test_summary_text_in_rupees`; `test_old_notify_shape_is_refused_by_loader`; `test_determinism_and_parity_failures_notify`.
- [ ] **Step 2: Run them to verify they fail.** `tests\run_tests.py test_notify`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests\run_tests.py test_notify test_reminder test_recovery test_parity_journal`, then `python -m stockify config`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add stockify/daemon/notify.py stockify/config/notify.json stockify/config/loader.py stockify/daemon/reminder.py stockify/daemon/recovery.py stockify/parity.py stockify/daemon/market_day.py docs/plan/A1_schemas.md tests/stockify/daemon/test_notify.py
git commit -m "RW 5.1: one notification channel, per-event switches, a daily summary"
```

- [ ] **Step 6: 🛑 Owner step.** The owner picks the channel, puts its secret in `.env` (the agent never reads `.env`), sets `channel` in `notify.json`, and runs `python -m stockify notify test`. This does not block later tasks.

---

### Task 26: Backups (GK 5.4; M5.13)

**Files:**
- Create: `stockify/backup.py`, `tests/stockify/test_backup.py`
- Modify: `stockify/config/storage.json` (`backup: {"target": null}`), `stockify/__main__.py` (`backup run [--target DIR]`)

**Interfaces:**
- Produces: `backup.run(target: Path, *, day: date) -> BackupResult` and `@dataclass BackupResult: copied: int; skipped: int; bytes: int; errors: list[str]`.
  - `refdata.sqlite` and `lab.sqlite` are copied with `sqlite3.Connection.backup()`.
  - These are copied incrementally, skipping files whose size and SHA-256 match `target/manifest.json`:
    - `journals/`, `ticks/archive/`, `nse/raw/`;
    - **the ELM archive** (`paths.nse_margin_dir("elm")`: NSE keeps only a rolling window, so it cannot be rebuilt).
  - SPAN files can be fetched again; their backup is optional (`storage.json` → `backup.include_span`, default false).
  - Each copy is verified, and nothing is ever deleted from the lab.
  - The daily job is set at 05:45 IST, before the 06:00 wake, so **it always runs as a catch-up on wake.** That is intended.

- [ ] **Step 1: Write the failing tests**: `test_backup_copies_and_verifies`; `test_second_run_skips_unchanged`; `test_live_wal_db_copy_is_consistent`; `test_elm_archive_included_span_optional`; `test_never_touches_landing_or_packs`.
- [ ] **Step 2: Run them to verify they fail.** `tests\run_tests.py test_backup`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add stockify/backup.py stockify/config/storage.json stockify/__main__.py tests/stockify/test_backup.py
git commit -m "GK 5.4: nightly verified backup of the stores that cannot be rebuilt"
```

- [ ] **Step 6: 🛑 Owner host steps** (each recorded in RECON; none blocks later tasks):
  - (a) choose the backup target and set `backup.target`;
  - (b) run `scripts\check_power.ps1` and fix what it reports. The D-32 settings: lid "do nothing" on AC and battery, AC never sleep or hibernate on idle, CPU max 80 %, AC wake timers on. This may need an elevated prompt;
  - (c) run `scripts\run_stockify_daemon.ps1 -SleepTest`;
  - (d) set Windows Update active hours to cover 19:00–04:30 local;
  - (e) run `scripts\manage_stockify_daemon.ps1 -Action Install`;
  - (f) after a week of good backups, the owner may set `retention_days`.

---

### Task 27: Scheduler jobs and catch-up (CDM 5.1; M5.9)

**Files:**
- Create: `tests/stockify/daemon/test_scheduler_m5.py`
- Modify:
  - `stockify/config/schedule.json`: jobs `day_bundles` ("08:30", after `refdata`), `finalise` (`{start: "19:30", retry_every_min: 15}`), `parity`, `daily_summary`, `backup` ("05:45");
  - `stockify/daemon/scheduler.py`: `CATCH_UP += ("finalise", "parity", "daily_summary", "backup")` and the new `DayPlan` fields;
  - `stockify/daemon/__init__.py`: `_run_job` for each new job; `run_daemon` arms sessions and runs `run_market_day`;
  - `docs/plan/M5_paper_daemon.md`: the M5.9 table in PST only, plus the D-32 note;
  - `docs/plan/A1_schemas.md:571-580`.

**Interfaces:**
- Consumes: `finalise_day`, `next_sod_state` (16); `journal_parity`, `recorder_parity` (22, 23); `coverage` (24); `notify` (25); `backup.run` (26); `wait_for_login(on_idle=…)` (19).
- Produces:
  - Catch-up runs per day, oldest day first: `nse_fetch → margin_files → finalise → parity → daily_summary`, then `backup`.
  - The `finalise` cut-off is `plan(next_trading_day).day_bundles_ns`.
  - `day_bundles` for day D runs only after `refdata` is done for D.
    - `refdata` is token-dependent. It retries every 5 minutes until 09:10, and after a later login it runs at once (until the session group's close).
    - If refdata fails for a non-token reason by 09:10, `day_bundles` uses D−1's refdata with `refdata_stale=True` (labelled).
    - A session whose D−1 is not finalised stays ARMED, with timeline `blocked_on_finalise`.
  - `run_daemon`'s login wait passes `on_idle = run missed jobs`.

- [ ] **Step 1: Write the failing tests**: `test_finalise_runs_as_catch_up_before_day_bundles`; `test_cutoff_is_next_day_bundle_time`; `test_day_bundles_waits_for_refdata`; `test_late_login_after_0910_still_runs_session` (login at 10:00 IST → refdata → bundle → RUNNING with a token gap from 09:15); `test_refdata_failure_uses_stale_label`; `test_session_blocked_when_yesterday_not_finalised`; `test_holidays_skipped` (2026-11-10); `test_backup_always_catch_up_after_wake`.
- [ ] **Step 2: Run them to verify they fail.** `tests\run_tests.py test_scheduler_m5`. Expected: FAIL.
- [ ] **Step 3: Implement.** Only `compaction` blocks sleep.
- [ ] **Step 4: Run** `tests\run_tests.py stockify.daemon`, then `python -m stockify config`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add stockify/config/schedule.json stockify/daemon/scheduler.py stockify/daemon/__init__.py docs/plan/M5_paper_daemon.md docs/plan/A1_schemas.md tests/stockify/daemon/test_scheduler_m5.py
git commit -m "CDM 5.1: the paper day's jobs, caught up in order after every wake"
```

---

### Task 28: The paper-trading API (WP 5.1; M5.12 server)

**Files:**
- Create: `server/routers/trade.py`, `tests/stockify/test_api_trade.py`
- Modify: `server/main.py:109-115`, `server/schemas.py` (DTOs inside the `dto-fields` block), `web/src/lib/types.ts`, `docs/plan/A2_interfaces.md` §6

**Interfaces:**
- Consumes: `sessionconfig.validate`, `_catalogue.validate_params`, `store` session functions, `lifecycle`.
- Produces (A2 §6; fixed paths are declared before `/{id}`):
  - `POST /api/trade/sessions` → 201 `{session_id}`, or 422 with a list of problems.
  - `GET /api/trade/sessions` → `[SessionDTO]` `{session_id, name, status, pause_reasons, today_pnl_paise, open_positions, coverage_today, parity_last, labels, created_ist}`.
  - `GET …/{id}` → `SessionDetailDTO`, with statistics twice: `stats_all` and `stats_full_coverage`.
  - `GET …/{id}/positions` (each with `instrument_token` for the day), `/orders`, `/fills`, `/timeline`, `/margin`.
  - `POST …/{id}/pause|resume|stop` → 202 `{cmd_id}`; 409 when the session is STOPPED.
  - `POST /api/trade/stop-all` → 202 `{cmd_id}`.
  - `GET /api/trade/login` → `{waiting_for_login, window_local, last_usable_local, market_open_local}`, computed from `schedule.json` and `calendar.owner_tz()`.

- [ ] **Step 1: Write the failing tests**: `test_create_validates_and_returns_201`; `test_bad_params_return_422_list`; `test_pause_writes_a_command_not_a_status`; `test_stop_all_targets_every_session`; `test_stats_twice_all_days_and_full_coverage`; `test_positions_carry_todays_token`; `test_login_window_follows_owner_tz`.
- [ ] **Step 2: Run them to verify they fail.** `tests\run_tests.py test_api_trade`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests\run_tests.py test_api_trade test_api_tests`, then blockwatch. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add server/routers/trade.py server/main.py server/schemas.py web/src/lib/types.ts docs/plan/A2_interfaces.md tests/stockify/test_api_trade.py
git commit -m "WP 5.1: /api/trade: sessions, details, controls and the kill switch"
```

---

### Task 29: The /trade pages (WP 5.2; M5.12 UI)

**Files:**
- Create: `web/src/lib/trade.ts`, `web/src/app/trade/page.tsx`, `web/src/app/trade/NewSessionForm.tsx`, `web/src/app/trade/[id]/page.tsx`
- Modify: `web/src/app/page.tsx` (menu link)

**Interfaces:**
- Consumes: Task 28 endpoints, `useTicks`, `format.rupees`, `params.ts`.
- Produces:
  - `/trade`:
    - session cards with status and stacked pause reasons in plain words, today's P&L in ₹, open positions, today's coverage and the last parity result;
    - a "New session" form;
    - a sticky **Stop all** button with a confirm dialog;
    - a "Log in to Kite" banner.
  - `/trade/[id]`:
    - positions with live LTP;
    - orders and fills as cards;
    - the timeline;
    - live vs model margin;
    - statistics twice (all days, full-coverage days);
    - the login reminder in the owner's time zone;
    - pause, resume and stop.

- [ ] **Step 1: Read** `web/node_modules/next/dist/docs/` and the `/tests` pages.
- [ ] **Step 2: Implement.** Poll every 2 s while RUNNING.
- [ ] **Step 3: Verify.**
  - Run `cd web; npm run lint; npm run build`. In a worktree, `npm run build` needs a temporary, uncommitted `turbopack.root` (RECON M3.14).
  - Then use the webapp-testing skill against a sandboxed API. Screenshots at 390 px and 1280 px go in the scratchpad and are listed in RECON.
- [ ] **Step 4: Commit**

```bash
git add web/src/lib/trade.ts web/src/app/trade web/src/app/page.tsx
git commit -m "WP 5.2: /trade: session cards, the session page and Stop all"
```

---

### Task 30: End-to-end demo, plan bookkeeping and the live gate (XI 5.2; Verification, DoD)

**Files:**
- Create: `tests/stockify/daemon/test_e03_faults.py`
- Modify: `docs/plan/M5_paper_daemon.md`, `docs/plan/00_overview.md` §4, `docs/plan/RECON.md`, `docs/plan/A3_testing.md` (E-01–E-03, U-10)

- [ ] **Step 1: Write the failing E-03 test.** Use subprocesses, as in Task 18, so it is timing-independent. The fixture day is replayed at `max` with `disconnect@09:20:00+90s`, `token_expiry@09:25:00+60s` (exit 76; the test then touches a newer fake token file and restarts) and `kill@09:30:00` (exit 137; restart). It asserts that:
  - the journal's gap reasons are `["disconnect", "token_expired"]`;
  - coverage is `(continuous − 150 s) / continuous`;
  - journal parity is `pass`;
  - orders, fills and journal bytes are identical to a twin run with the same disconnect and token faults but no kill.
- [ ] **Step 2: Run it to verify it fails; fix what it finds** in the owning module, then re-run that task's tests.
- [ ] **Step 3: Run the M5 demo** on the newest recorded day, or on the fixture day if none is recorded:

```
.\.venv\Scripts\python.exe -m stockify daemon --ticker replay:<day>:10x --sessions <id> --inject disconnect@10:30:00+90s --inject token_expiry@11:00:00+60s --inject kill@13:00:00
.\.venv\Scripts\python.exe -m stockify daemon --ticker replay:<day>:10x --sessions <id> --inject disconnect@10:30:00+90s --inject token_expiry@11:00:00+60s   # after exit 76 (fresh token in the sandbox), then after exit 137
.\.venv\Scripts\python.exe -m stockify parity journal --session <id> --date <day>      # identical
.\.venv\Scripts\python.exe -m stockify parity recorder --session <id> --date <day>     # diff summary
.\.venv\Scripts\python.exe tests\run_tests.py                                            # background; exit 0
cd web; npm run lint; npm run build
~/.tools/blockwatch/blockwatch.exe
```

Expected: journal parity `identical`; the suite exits 0, not 3; lint, build and blockwatch are clean.

- [ ] **Step 4: Commit**

```bash
git add tests/stockify/daemon/test_e03_faults.py docs/plan/M5_paper_daemon.md docs/plan/00_overview.md docs/plan/RECON.md docs/plan/A3_testing.md
git commit -m "XI 5.2: M5 built: replayed day with disconnect, token expiry and kill passes parity"
```

- [ ] **Step 5: 🛑 Live Definition of Done (owner, trading days).**
  - (a) A real session runs for five live trading days with every journal parity passing.
  - (b) Coverage is reported each day.
  - (c) One real token expiry is handled (Task 19 step 6).
  - (d) Live margins are logged (Task 21 step 6).
  - (e) The backup and host steps are done (Task 26 step 6).
  - (f) The API and the daemon run live on two ticker connections, and a third is refused.

  The first possible live day is Wed 2026-11-11 IST. Record each day in RECON; M5 is "built" until then, "done" after.

---

## Owner-only and live-gated items (🛑, none blocks a code task)

| Item | Where | Needs |
|---|---|---|
| Review the provisional decisions D-65–D-72, the roster and the per-session stop rule (recorded in RECON) | Task 1 | owner review (non-blocking) |
| **Daily Kite login**: every evening before an NSE trading day, 16:30–19:00 PST (17:30–20:00 PDT from March); still works until 19:40 PST; market opens 19:45 PST; nothing on holidays | ongoing | owner, browser |
| A full recorded day for M5.0 box 2 and the real-data demo (the recorder going live, an open M1 item) | Task 1 step 3, Task 30 | trading day |
| One real token expiry handled through the login flow | Task 19 step 6 | owner + trading day |
| Live Kite margin answers journaled, `margin_live` rows, `margin check --live` | Task 21 step 6 | owner login + market hours |
| Notification channel and its secret in `.env` | Task 25 step 6 | owner (the agent never reads `.env`) |
| Backup target; `retention_days` after a week of backups | Task 26 step 6 | owner decision |
| D-32 power settings (`check_power.ps1`, may need elevation), `-SleepTest`, Windows Update active hours, `manage_stockify_daemon.ps1 Install` | Task 26 step 6 | owner on the laptop |
| Two live KiteTicker connections observed, a third refused | Task 30 step 5 | trading day |
| Five live trading days with journal parity passing (DoD) | Task 30 step 5 | five trading days |
| A journal parity failure on a real day (M5.10 🛑, E-01) | Task 22 | owner, if it happens |
| Adopting the pending SPAN-lite calibration rows (affects L4/B3) | M4.9 🛑 (existing) | owner review |
| Push to the remote | after review | owner says "push" |

## Self-review notes

- **Spec coverage:**

  | Spec item | Task(s) |
  |---|---|
  | M5.0 | T1 |
  | M5.1 | T11, T13, T14 |
  | M5.2 | T8, T9 |
  | M5.3 | T8, T10, T12, T16 |
  | M5.4 | T17, T18 |
  | M5.5 | T15 |
  | M5.6 | T20, T21 (+ spec §5.2, §5.3, §9 live, §10 live rows) |
  | M5.7 | T19 |
  | M5.8 | T25 |
  | M5.9 | T27 |
  | M5.10 | T22, T23 |
  | M5.11 | T24 |
  | M5.12 | T28, T29 |
  | M5.13 | T26 |
  | M5.14 | T3 (budget, close codes) + the existing `--ticker replay` |
  | Verification and DoD | T30 |
  | D-47 | T5, T6 |

- **Deferred on purpose:** step L2 (D-69) is measured only in `margin live-report`.
- **Names that cross task boundaries:**
  - journal and dispatcher: `SessionDay`, `session_engine`, `Dispatcher`, `FrameEncoder`, `JournalWriter`, `journal.read`, `ordered_t`, `replay_into`;
  - day lifecycle: `finalise_day`, `next_sod_state`, `recover`, `lifecycle.pause/resume/arm/start_continuous/stop`;
  - parity and coverage: `journal_parity`, `recorder_parity`, `ParityResult`, `Coverage`;
  - margins: `MarginPoller`, `MarginSnapshot`, `LiveAnswers`, `LiveWithFallbackMargin`, `portfolio_sha256`;
  - engine bindings: `Engine.book`, `Engine.margin_now`, `Engine.eod_state`, `Engine.load_sod_state`;
  - bundles: `resolve_session_day`, `day_seed`;
  - store: `store.apply_session_outputs`, `store.add_command/pending_commands/mark_command`;
  - token wait: `token_watch.wait_for_login`.

## Pre-flight responses

Every finding from C-1, C-2, I-1 to I-18, the Minors and the 18 concrete edits was applied. The controller's rulings decided four of them:
- C-1: CLOCK comes from the stamps;
- C-2: full bar history, with a size guard and no trimming;
- I-1: the D-69 wording follows the review's §4 (b) text;
- Task 1's 🛑: removed, with the decisions recorded as provisional.

Deviations in detail:

1. **I-6, the resume key.** The review says to resume after the last dispatched `frame_no`. The plan stores both `frame_no` and `recv_ns` of that frame, per session-day and per process, and **compares by `recv_ns`**. The reason: on a replay restart the recorder's `frame_no` continues from the replay landing's maximum, while the replay source keeps its own numbering. The two match only when every source frame is delivered, which subscription filtering breaks. The stamp `recv_ns` is the same on both sides. It is the dispatched frame's stamp, not journal `t`, so `ordered_t` bumps do not matter. It is exact because the kill fires before the first frame with `t >= at`. `frame_no` stays in the mark for live loss detection and diagnostics.
2. **I-5, the pause state.** The review offers two options: the pause flag in `sod_state`, or `SessionDay.open` journaling `strategy_control`. The plan does both, so neither has to be trusted alone. The engine state carries the flag, the reasons and the queued notifications. `SessionDay.open` journals a correction only when the database's reason set (changed by commands while no engine ran) disagrees with the state.
3. **I-17, the notification rule.** Of the two options, the plan chose to queue `on_fill`/`on_order_update` and deliver them on enable, rather than delivering them while disabled with `R_PAUSED` on `place`/`modify`. Queuing keeps the strategy from acting while disabled, and lets it see every event before its first new hook.
4. **I-4, the L1 row.** The `margin_daily.csv` L1 row is defined as asked. Note that with `live_fresh_s = 360` it rarely appears, because DAY_END (23:59 IST) is long after the last answer. The per-check count therefore goes into the label `margin_live:L1_checks=<n>`, so L1 use stays visible (I-14).
5. **Minor, per-session stop.** The review marks it as an owner question. With the owner away, Task 1 records a provisional rule (no new hooks, orders and positions left to day end, labelled `stopped_with_positions`) for owner review rather than leaving it undefined.
6. **Minor, `Engine.book()`.** It is kept, and Task 15 consumes it to keep open positions pinned.

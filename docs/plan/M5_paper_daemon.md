# M5 — Paper-trading daemon

> **Goal**: paper sessions that run unattended every trading day against the live Kite feed, survive crashes and disconnects, handle the daily Kite login honestly, and prove every night that what they did can be reproduced exactly.
> **Depends on**: M1.5–M1.6 (ticker, recorder, replay ticker, minimal scheduler), M3 (engine), M4 for option sessions. **Unblocks**: M6. **Size**: L.
> **Demo**: a session runs through a replayed day at 10× with an injected disconnect, an injected token expiry and a forced kill (`taskkill /F`); it finishes with the same orders and fills as an uninterrupted run, passes journal parity, and its coverage shows the gaps.

Concepts: C-9, C-10, C-17. Decisions: D-15, D-16, D-20. Contract: `A4_engine_semantics.md` §1.1, §1.4, §4.5. Formats: `A1_schemas.md` §4–§5.

---

## M5.0 Recon (S)

- [x] Confirm how the API stores the Kite access token and how a new login updates it (from M0.0). The daemon reads the same store; it never copies or logs the token (I-10).
- [ ] 🛑 Confirm the recorder has run for several days (M1 done) and the replay ticker reproduces them. (Live pending: the recorded day 2026-11-05 replays in a sandboxed lab copy with dropped 0, exit 0 (16 instruments, 7,440 packets, 1,293 frames, 99 heartbeats), but it is a ~20-minute session, not a full one; needs a full live day, and the first one is Mon 16 Nov 2026 IST; see RECON M5.0 and "2026-11-15 state".)

## M5.1 Daemon composition (M)

Files: `stockify/daemon/__init__.py` (the daemon loop and the scheduler's jobs) and `stockify/daemon/market_day.py` (the market day: recorder, dispatcher and sessions from one ticker).

- [x] One process with an injected `Clock` and ticker (live or replay, M1.6): ticker supervisor → **fan-out** of each raw frame `(frame_no, recv_ns, payload)` to (a) the recorder queue and (b) the session dispatcher queue. Both see identical frames and receive times; that is what makes recorder-based replays comparable to live sessions (C-10).
- [x] Threads: the ticker library's thread (listener only enqueues), the recorder writer, one engine thread for all sessions (processed sequentially per batch; data rates are low), the scheduler, the health reporter. Queues between them; no shared mutable state.
- [x] Graceful shutdown on Ctrl+C / Ctrl+Break: stop accepting frames, drain queues, flush journals, and, when the stop is inside market hours, write `GAP(process_stop)` (journaled directly, never through the queue; a stop outside market hours or at the end of the day journals none).

## M5.2 Sessions and their lifecycle (M)

Tables: `sessions`, `session_days`, `session_timeline` (A1 §4).

- [x] A session = strategy reference (name, kind, source hash), parameters, universe selectors, capital, fill model (default `snapshot_quote` for options), margin model (default `live_with_fallback`, M5.6), seed, trial family, created time.
- [x] States: `CREATED → ARMED → RUNNING ⇄ PAUSED(reason) → STOPPED`. Each trading day: after the refdata job the session becomes `ARMED`; at its group's `CONTINUOUS` start, `RUNNING`; after the day is finalised (M5.3), `ARMED` for the next trading day. Holidays are skipped via the calendar.
- [x] Every state change, phase change, gap, login, parity result and error is a `session_timeline` row (engine time and wall time).

## M5.3 One engine per session-day, journal first (M)

Files: `stockify/daemon/session_day.py`, `stockify/daemon/lifecycle.py`, `stockify/daemon/journal.py`.

- [x] The engine side of the day-to-day state (Task 6, VOL 5.3, ST 5.1): `Engine.eod_state()` / `load_sod_state()` write and apply A1 §6.4 byte for byte as the oracle does; the saved state is untrusted input, so `load` checks all of it first and applies it whole or not at all, on both runners, with the same refusals (one table of hostile states, `tests/stockify/sod_state_hostile.py`). The daemon work below uses them.
- [x] Per session per IST trading day: resolve a **day bundle** (M2.1) for the session's instruments with `seed` derived as in A4 §4.5 and `sod_state` = the previous day's end-of-day state; save it as `journals/<session_id>/<YYYYMMDD>.bundle.json`; create a fresh `Engine` from it.
- [x] The dispatcher turns each incoming frame into `.evt` records for the session's instruments (the same encoder as packs, M1.8; `seq` = the dispatcher's per-day counter), adds `CLOCK` records every second and `GAP`/`REFDATA` records as they happen, **appends them to the journal first**, then calls `engine.push(batch)`. Flush per batch; fsync every 5 s and at phase changes.
- [x] Engine outputs from each batch (orders, order events, fills, charges) go to `lab.sqlite` in one transaction, upserted by `(session_id, trade_date, id)` so re-applying them is harmless (`session_orders`, `session_order_events`, `session_fills`, `session_charges`).
- [x] **Finalising a day**: after the evening NSE files arrive (M5.9), append the `SETTLE` records the session needs (daily settlement prices of open futures; final settlement prices on expiry days) with `t_event = max(publication time, last journal t_event)` (A4 §1.1), then call `engine.finish()`. That processes `DAY_END` (A4 §1.10): settlement, `positions_eod`, the NAV row, `save_state()`. Store `session_positions_eod`, `session_equity` and `session_days.eod_state_json`; close the journal. If the files have not arrived by the job's cut-off, finalise anyway: expiring contracts use the labelled fallback (A4 §7.2).

## M5.4 Crash recovery (M)

- [x] **Recovery = replay today's journal from today's start-of-day state.** On restart, the daemon creates a fresh engine from the day bundle (which contains the SOD state), replays the journal (fast: one session's day is small), checks that the regenerated outputs match what is already stored (they must; a mismatch is a determinism bug → pause the session and alert), then continues live. No mid-day engine serialization is needed.
- [x] Test E-02 (GK 5.3, D-71): in replay mode the daemon is killed by the OS mid-day (`--inject kill@HH:MM:SS`, exit 137), restarted, and the final artifacts, journal and end-of-day state equal an uninterrupted run's (`tests/stockify/daemon/test_e02_kill.py`).

## M5.5 Market data for sessions (S)

- [x] Sessions pin their instruments in the subscription manager (M1.5) so the recorder's re-centring never unsubscribes something a session needs.
- [x] A session's day bundle holds the whole reachable universe, resolved before the open (D-67), so no instrument is added during the day; a contract listed after the bundle is made is unknown to that day's engine. Daily price bands snapshotted live arrive as `price_band` `REFDATA` records through the dispatcher's queue.

## M5.6 Live margins without breaking determinism (M)

- [x] After each fill (debounced 2 s) and every 5 minutes, the daemon sends the session's paper portfolio to `POST /margins/basket?consider_positions=false` (K-10) and stores the response in `margin_live`.
- [x] Anything from outside that can change engine decisions must be in the journal. So each Kite margin result is injected as a `REFDATA` record (`margin_requirement`, keyed by a hash of the portfolio). The engine's `live_with_fallback` model uses the latest injected value for the current portfolio and falls back to SPAN-lite (M4.8) when none is fresh. On replay, the same records produce the same decisions.
- [x] Store the model's own estimate next to Kite's for every sample: this continuously extends the M4.9 calibration set.

## M5.7 Token expiry and the daily login (M)

The access token dies at 06:00 IST every day and the official login is a browser flow (K-7). STOCKIFY treats the daily login as a fixed part of operating it, makes missing it visible, and never automates around Zerodha's login.

- [x] Detection: a 403 / `TokenException` from REST or a ticker auth failure ⇒ `GAP(token_expired)` for all instruments, every session `PAUSED(token_expired)`, a banner in the UI ("Log in to Kite").
- [x] Resume: the daemon watches the token store (poll every 15 s: `paper.json` `token_poll_s`, the one place it is set); after a successful login it reconnects, re-subscribes, writes the gap end, and resumes sessions.
- [x] Token-dependent morning jobs retry every 5 minutes until 09:10 IST (`schedule.json`); if the token is still invalid at the refdata time, notify (M5.8).
- [x] The owner's routine in Seattle (01_facts §5): on any evening before an NSE trading day, log in between **17:30 and 20:00 PDT** (16:30–19:00 PST from 2026-11-01), i.e. after the 06:00 IST expiry and before the 08:30 IST refdata job. A login up to 20:40 PDT (19:40 PST) still works through the retries; the market opens at 20:45 PDT (19:45 PST). On NSE holidays there is nothing to do. Show this on the session page and in the reminder text.

## M5.8 Notifications (S)

- [x] `stockify/config/notify.json` with one channel (email only: owner, 2026-11-12, D-82, replacing the ntfy / Telegram / email choice) and per-event switches: login needed, session error, parity failure, recorder stall, disk low, daily summary (P&L, coverage, parity per session). Default: email on once the SMTP settings are set up (until then each notification is only logged), and each switch can be turned off. Secrets for the channel live outside the repo. (Built, RW 5.1-5.2: email only; the channel and its secret are owner-only, Task 25 step 6.)
- [x] The recorder-stall and disk-low switches have senders (RM 7.1): a stalled FULL index (live days only) and the compaction job's disk warnings each email once through `notify.post`.

## M5.9 Scheduler (S)

Extends the M1.5 scheduler; all times from `schedule.json` (IST), skipped on holidays.

| Job (IST) | Seattle (PST) |
|---|---|
| 05:45 verified backup (M5.13; catch-up on every wake; no target = skipped) | 16:15 |
| 08:30 refdata (retries every 5 min to 09:10; a later login runs it at once), then day bundles | 19:00 (to 19:40) |
| 08:58 subscribe recorder universe + pinned session instruments | 19:28 |
| 09:00 pre-open; 09:15 continuous (from `sessions.json`) | 19:30; 19:45 |
| 15:40 F&O close; 15:45 unsubscribe | 02:10; 02:15 next day |
| 16:30 compaction (M1.7) | 03:00 |
| 17:20 NSE margin files (`margin_files`: SPAN + ELM; retried to 22:00; holds sleep until archived or 22:00) | 03:50 |
| 19:30 day finalisation (M5.3; cut off at the next trading day's 08:30), then 19:45 nightly parity (M5.10), 19:50 daily summary; NSE files (M1.3) 19:30–23:00 | 06:00; 06:15; 06:20; 06:00–09:30 |

D-32 note: the laptop hibernates after the close and the day's compaction and wakes at 06:00 IST on the next trading day; none of the jobs above except `compaction` keeps it awake, so what it slept through runs on wake, in the order below.

- [x] **Order**: per trading day, oldest first: `nse_fetch`, `margin_files`, `finalise`, `compaction`, `parity`, `daily_summary`, `desk` (HD 8, D-152: 20:10 IST, every 30 min until 23:30, held during the session), `fundamentals` (FS 8, D-153: 20:40 IST, every 30 min until 23:30, after `nse_fetch`, held during the session, within the day's NSE request budget); then `backup`. Each job gets the daemon clock's time when it starts (not the pass's), so the desk and the fundamentals job judge the next session's deadline on the real time (D-153). While the session is on (subscribe to unsubscribe time) the recording process holds `finalise`, `parity` and `backup` for earlier days until the close (D-1's finalise before the open is `before_open`). `parity` and the summary wait for `finalise` of their day. A failing job is logged and retried at its `retry_every_min`; it never stops another job, the recorder or the daemon. Two schedulers (or the scheduler and the daemon) never run one session-day's finalise at once: it holds a per-day OS-lock claim, a second caller gets `busy`.
- [x] **Stale refdata / blocked start**: if refdata fails for a reason other than the login by 09:10, the day bundle uses D-1's refdata and is labelled `refdata_stale`. A session whose previous trading day is not finalised stays ARMED with the timeline row `blocked_on_finalise`. A login after the open starts the session with a `GAP(token_expired)` intended from 09:15; it is journaled at `max(09:15, the journal's last t)` (D-68), and the timeline row (`gap`, `late_login`) gives that journaled t. Before the sessions open only the refdata and D-1's finalise run (unthrottled, each guarded); the rest of the catch-up runs on a thread once the recorder is up. A blocked session is tried again every minute: once D-1 is finalised the run ends and the daemon's next pass opens it. Skipped parity is tried 8 times a day, then one `parity_gave_up` timeline row; a backup with copy errors is tried 3 times a day; a day on which every session failed is summarised as that.

- [x] **Catch-up**: on start, the scheduler runs every missed non-market job for past dates (NSE fetch, margin files, finalisation, compaction, parity, summary) in date order; compaction of a day the machine slept through runs after that day's finalise and keeps D-78's rules (a verified archive is never replaced, and the only complete copy of a day's packets is never deleted). A machine that slept through the morning jobs therefore catches up when it wakes; only market hours require it to be awake.

## M5.10 Nightly parity (M)

- [x] **Journal parity (gate)**: for each session-day, a fresh engine + day bundle + journal (then `finish()`) must reproduce the stored orders, order events, fills, charges, positions and NAV exactly. Failure ⇒ session `PAUSED(parity_failure)`, alert, 🛑 (it is a bug, E-01).
- [x] **Recorder parity (report)**: build a pack from the recorder's archive for the session's instruments, run it from the same SOD state, and store a diff summary in `parity_results` (fills matched, P&L difference, time of first divergence). This number is how much to trust backtests on recorded data (C-10). It never fails a session.

## M5.11 Coverage (S)

- [x] Per session-day: coverage = share of continuous-phase seconds with the ticker connected, a valid token and no `GAP` affecting the session's instruments. Days under the threshold (default 98%) are flagged.
- [x] Session statistics are shown twice: all days, and full-coverage days only. A paper track record with holes is labelled as such (I-14).

## M5.12 Paper-trading API and UI (M)

Endpoints: `A2_interfaces.md` §6. Server: `server/routers/trade.py`.

- [x] `/trade`: session cards (status and pause reason, today's P&L, open positions, today's coverage, last parity result) and a "New session" form.
- [x] `/trade/[id]`: positions with live LTP (existing `useTicks`), orders, fills, a timeline (phases, gaps, pauses, logins, parity), live vs model margin, the Seattle login reminder.
- [x] Controls: pause, resume, stop; a **Stop all** kill switch (pauses every session immediately). Building the habit now matters if live trading is ever added.

## M5.13 Where the daemon runs (M) — owner chose option A (D-26)

NSE trades 20:30–03:10 in Seattle (PDT; 19:30–02:10 PST), so the host must be awake during that window on every trading day; the morning jobs can catch up later (M5.9).

- **A. The current Windows machine**: Task Scheduler starts `run.ps1 -Daemon` at logon; the power plan never sleeps between 19:00 and 04:30 local; Windows Update active hours avoid that window. Cheapest; most fragile.
- Options B (Linux box) and C (cloud VM) are out of scope: STOCKIFY is Windows only (D-26). The Kite redirect stays on the loopback callback.
- [x] Whichever is chosen (the owner set this up 2026-11-13 to 2026-11-15): nightly backup of `refdata.sqlite`, `lab.sqlite`, `journals/`, `ticks/archive/` and `nse/raw/` (packs and landing files are rebuildable or transient). Once backups have run for a week, the owner may set `storage.json` → `retention_days` (M1.7). (Code done, GK 5.7: nightly verified backup; D-147: the target ships as `~/STOCKIFY-backup` (home-expanded) and only the newest `keep_snapshots` (7) daily `refdata`/`lab` snapshots are kept, so the ~2.5 GB/day snapshot no longer grows without bound. Done by the owner, 2026-11-13 to 2026-11-15 (Task 26 step 6): the D-32 power settings pass `scripts/check_power.ps1`, the recorder's Scheduled Task "STOCKIFY Market Daemon" is installed and starts at logon, and the backup target is set with 7 kept. The first nightly backup run follows the first live recording day. `retention_days` stays the owner's later choice.)

## M5.14 Connection budget and development mode (S)

- [x] Normal operation uses two KiteTicker connections (API for UI prices, daemon), out of three (K-1, D-20). Development runs use `--ticker replay:…`, never a third live connection. The daemon logs every close code and reason loudly. (Code done, LCB 5.1: `conn_budget` tickets for roles `api`, `daemon`, `dev`; a third live connection is refused; close codes at ERROR without the websocket URL. Live pending: two live connections observed and a third refused, Task 30; see RECON.)

---

## Verification

```
python -m stockify daemon --ticker replay:<recorded-date>:10x \
    --inject disconnect@10:30:00+90s --inject token_expiry@11:00:00 --inject kill@13:00:00
python -m stockify parity journal --session <id> --date <recorded-date>      # must report identical
python -m stockify parity recorder --session <id> --date <recorded-date>     # prints the diff summary
.\.venv\Scripts\python.exe tests\run_tests.py
cd web; npm run lint; npm run build
```

## Definition of done

A real session has run for at least five live trading days with every journal-parity check passing, coverage reported, one real token expiry handled through the login flow, and the owner's chosen host and backup in place.

**Status (XI 5.12, 2026-11-12; updated 2026-11-15): M5 is built and merged into `main`; the live Definition of Done is open.** The demo above ran in a sandboxed lab
on the fixture day (RECON, "M5 demo"): disconnect, token expiry (exit 76, fresh fake token in the sandbox) and kill
(exit 137), two restarts, journal parity identical, recorder parity reported. E-03 (`tests/stockify/daemon/test_e03_faults.py`)
holds it as a subprocess test. Open, on real trading days (Task 30 step 5; (e) was done by the owner 2026-11-13 to 2026-11-15):

- [ ] (a) A real session runs for five live trading days with every journal parity passing.
- [ ] (b) Coverage is reported each day.
- [ ] (c) One real token expiry is handled (Task 19 step 6).
- [ ] (d) Live margins are logged (Task 21 step 6).
- [x] (e) The backup and host steps are done (Task 26 step 6): the owner finished them 2026-11-13 to 2026-11-15 (power settings pass, Scheduled Task installed, backup target set, email alerts configured with a test email delivered 2026-11-15, `kite check` passes).
- [ ] (f) The API and the daemon run live on two ticker connections, and a third is refused.
- [ ] M5.0 box 2: a full recorded day replays (the recorder going live).

The first live recording day is Mon 16 Nov 2026 IST; it has not happened yet, and each live day is recorded in RECON. M5 is "built" until the open boxes above are ticked, "done" after.

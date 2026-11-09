# M5 — Paper-trading daemon

> **Goal**: paper sessions that run unattended every trading day against the live Kite feed, survive crashes and disconnects, handle the daily Kite login honestly, and prove every night that what they did can be reproduced exactly.
> **Depends on**: M1.5–M1.6 (ticker, recorder, replay ticker, minimal scheduler), M3 (engine), M4 for option sessions. **Unblocks**: M6. **Size**: L.
> **Demo**: a session runs through a replayed day at 10× with an injected disconnect, an injected token expiry and a forced kill (`taskkill /F`); it finishes with the same orders and fills as an uninterrupted run, passes journal parity, and its coverage shows the gaps.

Concepts: C-9, C-10, C-17. Decisions: D-15, D-16, D-20. Contract: `A4_engine_semantics.md` §1.1, §1.4, §4.5. Formats: `A1_schemas.md` §4–§5.

---

## M5.0 Recon (S)

- [x] Confirm how the API stores the Kite access token and how a new login updates it (from M0.0). The daemon reads the same store; it never copies or logs the token (I-10).
- [ ] 🛑 Confirm the recorder has run for several days (M1 done) and the replay ticker reproduces them. (Live pending: one recorded day, 2026-11-05, exists; replay check to run in a sandboxed lab copy; see RECON M5.0.)

## M5.1 Daemon composition (M)

File: `stockify/daemon/main.py`.

- [ ] One process with an injected `Clock` and ticker (live or replay, M1.6): ticker supervisor → **fan-out** of each raw frame `(frame_no, recv_ns, payload)` to (a) the recorder queue and (b) the session dispatcher queue. Both see identical frames and receive times; that is what makes recorder-based replays comparable to live sessions (C-10).
- [ ] Threads: the ticker library's thread (listener only enqueues), the recorder writer, one engine thread for all sessions (processed sequentially per batch; data rates are low), the scheduler, the health reporter. Queues between them; no shared mutable state.
- [ ] Graceful shutdown on Ctrl+C / Ctrl+Break: stop accepting frames, drain queues, flush journals, write `GAP(process_stop)`.

## M5.2 Sessions and their lifecycle (M)

Tables: `sessions`, `session_days`, `session_timeline` (A1 §4).

- [ ] A session = strategy reference (name, kind, source hash), parameters, universe selectors, capital, fill model (default `snapshot_quote` for options), margin model (default `live_with_fallback`, M5.6), seed, trial family, created time.
- [ ] States: `CREATED → ARMED → RUNNING ⇄ PAUSED(reason) → STOPPED`. Each trading day: after the refdata job the session becomes `ARMED`; at its group's `CONTINUOUS` start, `RUNNING`; after the day is finalised (M5.3), `ARMED` for the next trading day. Holidays are skipped via the calendar.
- [ ] Every state change, phase change, gap, login, parity result and error is a `session_timeline` row (engine time and wall time).

## M5.3 One engine per session-day, journal first (M)

Files: `stockify/daemon/sessions.py`, `stockify/daemon/journal.py`.

- [ ] Per session per IST trading day: resolve a **day bundle** (M2.1) for the session's instruments with `seed` derived as in A4 §4.5 and `sod_state` = the previous day's end-of-day state; save it as `journals/<session_id>/<YYYYMMDD>.bundle.json`; create a fresh `Engine` from it.
- [ ] The dispatcher turns each incoming frame into `.evt` records for the session's instruments (the same encoder as packs, M1.8; `seq` = the dispatcher's per-day counter), adds `CLOCK` records every second and `GAP`/`REFDATA` records as they happen, **appends them to the journal first**, then calls `engine.push(batch)`. Flush per batch; fsync every 5 s and at phase changes.
- [ ] Engine outputs from each batch (orders, order events, fills, charges) go to `lab.sqlite` in one transaction, upserted by `(session_id, trade_date, id)` so re-applying them is harmless (`session_orders`, `session_order_events`, `session_fills`, `session_charges`).
- [ ] **Finalising a day**: after the evening NSE files arrive (M5.9), append the `SETTLE` records the session needs (daily settlement prices of open futures; final settlement prices on expiry days) with `t_event = max(publication time, last journal t_event)` (A4 §1.1), then call `engine.finish()`. That processes `DAY_END` (A4 §1.10): settlement, `positions_eod`, the NAV row, `save_state()`. Store `session_positions_eod`, `session_equity` and `session_days.eod_state_json`; close the journal. If the files have not arrived by the job's cut-off, finalise anyway: expiring contracts use the labelled fallback (A4 §7.2).

## M5.4 Crash recovery (M)

- [ ] **Recovery = replay today's journal from today's start-of-day state.** On restart, the daemon creates a fresh engine from the day bundle (which contains the SOD state), replays the journal (fast: one session's day is small), checks that the regenerated outputs match what is already stored (they must; a mismatch is a determinism bug → pause the session and alert), then continues live. No mid-day engine serialization is needed.
- [ ] Test E-02: in replay mode, `taskkill /F` the daemon mid-day, restart, and the final artifacts equal an uninterrupted run's.

## M5.5 Market data for sessions (S)

- [ ] Sessions pin their instruments in the subscription manager (M1.5) so the recorder's re-centring never unsubscribes something a session needs.
- [ ] Instruments added during the day reach the engine as `REFDATA` records before their first tick; daily price bands arrive the same way.

## M5.6 Live margins without breaking determinism (M)

- [ ] After each fill (debounced 2 s) and every 5 minutes, the daemon sends the session's paper portfolio to `POST /margins/basket?consider_positions=false` (K-10) and stores the response in `margin_live`.
- [ ] Anything from outside that can change engine decisions must be in the journal. So each Kite margin result is injected as a `REFDATA` record (`margin_requirement`, keyed by a hash of the portfolio). The engine's `live_with_fallback` model uses the latest injected value for the current portfolio and falls back to SPAN-lite (M4.8) when none is fresh. On replay, the same records produce the same decisions.
- [ ] Store the model's own estimate next to Kite's for every sample: this continuously extends the M4.9 calibration set.

## M5.7 Token expiry and the daily login (M)

The access token dies at 06:00 IST every day and the official login is a browser flow (K-7). STOCKIFY treats the daily login as a fixed part of operating it, makes missing it visible, and never automates around Zerodha's login.

- [ ] Detection: a 403 / `TokenException` from REST or a ticker auth failure ⇒ `GAP(token_expired)` for all instruments, every session `PAUSED(token_expired)`, a banner in the UI ("Log in to Kite").
- [ ] Resume: the daemon watches the token store (poll every 15 s); after a successful login it reconnects, re-subscribes, writes the gap end, and resumes sessions.
- [ ] Token-dependent morning jobs retry every 5 minutes until 09:10 IST (`schedule.json`); if the token is still invalid at the refdata time, notify (M5.8).
- [ ] The owner's routine in Seattle (01_facts §5): on any evening before an NSE trading day, log in between **17:30 and 20:00 PDT** (16:30–19:00 PST from 2026-11-01), i.e. after the 06:00 IST expiry and before the 08:30 IST refdata job. A login up to 20:40 PDT (19:40 PST) still works through the retries; the market opens at 20:45 PDT (19:45 PST). On NSE holidays there is nothing to do. Show this on the session page and in the reminder text.

## M5.8 Notifications (S)

- [ ] `stockify/config/notify.json` with one channel (🛑 owner picks: ntfy topic, Telegram bot, or email) and per-event switches: login needed, session error, parity failure, recorder stall, disk low, daily summary (P&L, coverage, parity per session). Default: off, log only. Secrets for the channel live outside the repo.

## M5.9 Scheduler (S)

Extends the M1.5 scheduler; all times from `schedule.json` (IST), skipped on holidays.

| Job (IST) | Seattle, PDT | Seattle, PST (from 2026-11-01) |
|---|---|---|
| 08:30 refdata, day bundles, token check (retries to 09:10) | 20:00 (to 20:40) | 19:00 (to 19:40) |
| 08:58 subscribe recorder universe + pinned session instruments | 20:28 | 19:28 |
| 09:00 pre-open; 09:15 continuous (from `sessions.json`) | 20:30; 20:45 | 19:30; 19:45 |
| 15:40 F&O close; 15:45 unsubscribe | 03:10; 03:15 next day | 02:10; 02:15 next day |
| 16:30 compaction (M1.7) | 04:00 | 03:00 |
| 19:30–23:00 NSE files (M1.3), settlement records, day finalisation (M5.3), then nightly parity (M5.10), quality report (M1.9), daily summary | 07:00–10:30 | 06:00–09:30 |

- [ ] **Catch-up**: on start, the scheduler runs every missed non-market job for past dates (compaction, NSE fetch, finalisation, parity) in date order. A machine that slept through the morning jobs therefore catches up when it wakes; only market hours require it to be awake.

## M5.10 Nightly parity (M)

- [ ] **Journal parity (gate)**: for each session-day, a fresh engine + day bundle + journal (then `finish()`) must reproduce the stored orders, order events, fills, charges, positions and NAV exactly. Failure ⇒ session `PAUSED(parity_failure)`, alert, 🛑 (it is a bug, E-01).
- [ ] **Recorder parity (report)**: build a pack from the recorder's archive for the session's instruments, run it from the same SOD state, and store a diff summary in `parity_results` (fills matched, P&L difference, time of first divergence). This number is how much to trust backtests on recorded data (C-10). It never fails a session.

## M5.11 Coverage (S)

- [ ] Per session-day: coverage = share of continuous-phase seconds with the ticker connected, a valid token and no `GAP` affecting the session's instruments. Days under the threshold (default 98%) are flagged.
- [ ] Session statistics are shown twice: all days, and full-coverage days only. A paper track record with holes is labelled as such (I-14).

## M5.12 Paper-trading API and UI (M)

Endpoints: `A2_interfaces.md` §6. Server: `server/routers/trade.py`.

- [ ] `/trade`: session cards (status and pause reason, today's P&L, open positions, today's coverage, last parity result) and a "New session" form.
- [ ] `/trade/[id]`: positions with live LTP (existing `useTicks`), orders, fills, a timeline (phases, gaps, pauses, logins, parity), live vs model margin, the Seattle login reminder.
- [ ] Controls: pause, resume, stop; a **Stop all** kill switch (pauses every session immediately). Building the habit now matters if live trading is ever added.

## M5.13 Where the daemon runs (M) — owner chose option A (D-26)

NSE trades 20:30–03:10 in Seattle (PDT; 19:30–02:10 PST), so the host must be awake during that window on every trading day; the morning jobs can catch up later (M5.9).

- **A. The current Windows machine**: Task Scheduler starts `run.ps1 -Daemon` at logon; the power plan never sleeps between 19:00 and 04:30 local; Windows Update active hours avoid that window. Cheapest; most fragile.
- Options B (Linux box) and C (cloud VM) are out of scope: STOCKIFY is Windows only (D-26). The Kite redirect stays on the loopback callback.
- [ ] Whichever is chosen: nightly backup of `refdata.sqlite`, `lab.sqlite`, `journals/`, `ticks/archive/` and `nse/raw/` (packs and landing files are rebuildable or transient). Once backups have run for a week, the owner may set `storage.json` → `retention_days` (M1.7).

## M5.14 Connection budget and development mode (S)

- [ ] Normal operation uses two KiteTicker connections (API for UI prices, daemon), out of three (K-1, D-20). Development runs use `--ticker replay:…`, never a third live connection. The daemon logs every close code and reason loudly.

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

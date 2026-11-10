# STOCKIFY

**An NSE simulation lab for testing equity, futures and options strategies.** STOCKIFY records Kite Connect market data every trading day, backtests strategies over historical or recorded data with realistic costs, fills and margin, and will paper-trade them live and unattended. It **never places real orders**.

It answers one question honestly: *would this rule have made money after realistic costs, fills and margin, and does it keep doing so live?*

- **Data:** Kite Connect (instruments, historical candles, KiteTicker WebSocket) plus NSE's public files: end-of-day bhavcopies and the exchange's own margin files (SPAN risk files and the extreme-loss-margin file).
- **Engine:** a deterministic C++20 core (`engine/`) for orders, fills, costs, ledger, settlement and margin. It is exposed to Python as `stockify._engine` and runs strategies written in Python (default) or C++.
- **Oracle:** a small, readable Python backtester (`stockify/oracle/`) that implements the same contract. The engine must agree with it to the paisa.
- **App:** a FastAPI backend (`server/`) and a mobile-first Next.js UI (`web/`).
- **Side process:** the geo news monitor (`geo_monitor/`) is unchanged.

Windows only (decision D-26). Money is ₹, stored as integer paise; time is stored as UTC nanoseconds and shown in IST.

---

## Where things stand (2026-11-09)

| Milestone | What it gives you | State |
|---|---|---|
| **M0** Cleanup + toolchain | The old LLM pipeline removed; the C++20 / pybind11 engine builds on Windows (MSVC) | Done |
| **M1** Data layer + recorder | Instruments with history, NSE end-of-day files, the Kite bar cache, the tick recorder, replay, compaction to Parquet, event packs, a data-quality report | Built. Still open: unattended live recording days |
| **M2** Python oracle | The reference backtester, golden scenarios, end-of-day option research | Done |
| **M3** C++ engine (equity) | Clock, orders, three fill models, costs, ledger, metrics, Python and C++ strategies, the `/tests` page | Done |
| **M4** Futures and options | Dated contract rules, Black-76 and implied volatility, option chains, expiry and physical delivery, and margin priced from NSE's own files | Done |
| **M5** Paper daemon | Unattended paper sessions, journals, crash recovery, live margins, nightly parity, the `/trade` page | In progress |
| **M6** Reporting + research | Comparing runs, data coverage, walk-forward, cost and fill sensitivity | Not started |

**Branches.** Work lands on one branch per milestone. `main` holds the M1 baseline; the newest pushed code is on **`stockify/m4-fno`** (M0–M4), and this README describes that branch. Anything below that you cannot find on `main` (the backtest and margin commands, `examples/runs/`, the how-to guides) is there: `git switch stockify/m4-fno`. M5 is being built on `stockify/m5-paper`.

The full plan, every decision (`D-xx`) and the notes on what the code really looks like are in [`docs/plan/`](docs/plan/README.md).

### What works today

- **Record a trading day.** The daemon subscribes to the day's instruments, records every tick with gaps and health noted, compacts the day to verified Parquet, then hibernates the laptop and wakes itself for the next session.
- **Backtest on bars or recorded ticks.** A run is one JSON file (`examples/runs/`). It produces orders, fills, charges, an equity curve, a margin record and a plain report. The same run gives the same bytes every time, and `--rerun` proves it.
- **Costs as Zerodha charges them.** Brokerage, STT, exchange fees, GST, stamp duty, DP charges and expiry-day brokerage come from dated config rows, each with its source. A date with no sourced row is refused, never guessed.
- **Futures and options.** Chains with implied volatility and greeks, rolls, freeze-limit slicing, expiry settlement and physical delivery.
- **Margin from the exchange's own files.** Each day a run uses the best margin it can price, a five-step "staircase": exact SPAN with NSE's extreme-loss file, exact SPAN with the dated rule, a calibrated estimate, a placeholder estimate, then a flat rate. Checked against 405 margin answers from Kite, the exact step agreed on every one, the worst by ₹0.85. The archive holds NSE's end-of-day SPAN file for every trading day from January 2009.
- **Two engines that must agree.** A random-world fuzzer runs the Python oracle and the C++ engine side by side; any difference fails the build.
- **See it in the browser.** `/tests` lists runs and opens one with its equity curve, orders, fills, charges, margin and greeks; `/portfolio` shows the Kite account read-only; `/alerts` shows the geo monitor's alerts.

### Being built now (M5)

Paper sessions that run by themselves through the trading day. Already in place on the M5 branch: the lab database migrations, per-day random seeds, the state a strategy carries from one day to the next, and two safety layers. The Kite client has its order routes removed and can only send the handful of read-only requests on an allowlist. Strategy files are checked before they run and may import only a short list of safe modules.

---

## Quick start

Install **PowerShell 7**, **Python 3.11 or 3.12 (x64)**, and **Node.js 20.9+**. To build the engine you also need **Visual Studio Build Tools** with the *Desktop development with C++* workload and *C++ CMake tools for Windows*. The launcher checks these versions, manages the venv and the deterministic `npm ci` install, and supervises the child processes.

```powershell
.\run.ps1                          # FastAPI :8001 + Next.js :3000
.\run.ps1 -BuildEngine             # build + test the C++ engine, then exit
.\run.ps1 -Daemon                  # API + web + STOCKIFY market daemon (restarts on crash)
.\run.ps1 -DaemonOnly              # STOCKIFY market daemon only
.\run.ps1 -Reload                  # API auto-reloads on code changes (its log opens in its own window)
.\run.ps1 -GeoMonitor              # API + web + geo monitor
.\run.ps1 -MonitorOnly             # geo monitor only
.\run.ps1 -Fresh                   # rebuild .venv + reinstall Python deps
.\run.ps1 -InstallWeb              # force `npm ci` in web/
.\run.ps1 -ApiOnly | -WebOnly
.\run.ps1 -ApiPort 8002 -WebPort 3001
```

Then open **http://127.0.0.1:3000**. The Next.js dev server proxies `/api/*` to `STOCKIFY_API_ORIGIN`, and the backend uses `STOCKIFY_WEB_ORIGIN` for CORS and the post-OAuth redirect. Both origins must be loopback addresses.

To run the two processes directly once `.venv` and `web/node_modules` exist, use two terminals:

```powershell
# Terminal 1 — backend
$env:STOCKIFY_WEB_ORIGIN="http://127.0.0.1:3000"
.\.venv\Scripts\python.exe -m uvicorn server.main:app --reload --host 127.0.0.1 --port 8001

# Terminal 2 — frontend
$env:STOCKIFY_API_ORIGIN="http://127.0.0.1:8001"
cd web; npm run dev -- --hostname 127.0.0.1 --port 3000
```

Credentials go in `.env` at the repo root:

```
KITE_API_KEY=...           # Kite Connect app (₹500/month plan covers live + historical data)
KITE_API_SECRET=...
GOOGLE_AI_API_KEY=...      # Gemini — geo monitor classification

# Email alerts from the geo monitor
ALERT_EMAIL_TO=you@example.com
SMTP_HOST=...
SMTP_PORT=587
SMTP_USER=...
SMTP_PASSWORD=...
SMTP_FROM=stockify@example.com
GEO_MONITOR_INTERVAL_SEC=600
GEO_MONITOR_WHEN=1h
```

The daily Kite access token is **not** stored in `.env`. Get it with the "Login to Kite" button on `/portfolio`; it is cached at `%LOCALAPPDATA%\STOCKIFY\kite\kite_token.json`. In the Zerodha developer console, register `http://127.0.0.1:8001/api/kite/callback` as the redirect URL.

### Building the engine

If the C++ workload is missing, add it to the installed Build Tools. This needs admin rights and is a large download:

```powershell
& "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\setup.exe" modify `
  --installPath "<Build Tools install path>" `
  --add Microsoft.VisualStudio.Workload.VCTools `
  --add Microsoft.VisualStudio.Component.VC.CMake.Project --includeRecommended --passive
```

`.\run.ps1 -BuildEngine` then does the following:

1. Enters the VS developer shell.
2. Configures the `win-msvc-release` preset (Ninja) against the venv's Python and pybind11.
3. Builds, and copies `_engine*.pyd` into `stockify/`.
4. Runs `ctest`.
5. Prints the engine build info.

The engine is built with `/fp:precise` (the same arithmetic everywhere) and with Control Flow Guard and the SDL checks switched on.

### Recording every trading day (laptop, lid closed)

```powershell
.\scripts\check_power.ps1                       # lid/sleep/wake-timer/CPU settings the recorder needs (D-32)
.\scripts\manage_stockify_daemon.ps1 Install     # start the daemon at logon (Scheduled Task)
.\scripts\manage_stockify_daemon.ps1 Status      # task state, next self-wake, log location
.\scripts\run_stockify_daemon.ps1 -SleepTest     # owner check: hibernate now, wake by itself in 5 min
python -m stockify calendar                       # upcoming sessions and Kite login windows in your time zone
```

Each trading day the daemon records the session, compacts it, hibernates the laptop and schedules a self-wake for 06:00 IST on the next trading day. Log in to Kite each trading day inside the login window `calendar` shows. Logs: `%LOCALAPPDATA%\STOCKIFY\logs\stockify-daemon.log`.

### Geo monitor

The geo monitor runs **outside** FastAPI, so news watching continues when the API is down:

```powershell
.\scripts\run_geo_monitor.ps1 -Once -DryRun
.\scripts\manage_geo_monitor.ps1 Install | Start | Status   # per-user Scheduled Task
```

See [`geo_monitor/README.md`](geo_monitor/README.md).

---

## Using the lab

Every command is `.\.venv\Scripts\python.exe -m stockify …` (shortened to `stockify …` below). `stockify --help` lists them all.

### Getting data in

```powershell
stockify refdata --help                                   # instruments with history (Kite dumps)
stockify nse backfill --from 2024-01-01                   # NSE end-of-day files for every trading day (resumable)
stockify nse margin-files --backfill 2009-01-02 2026-11-06   # NSE's end-of-day SPAN margin files (resumable)
stockify nse margin-files --date 2026-11-09               # one day: every SPAN file plus the ELM file
stockify bars fetch --help                                # Kite historical candles into the bar cache
stockify compact --help                                   # a recorded day → verified Parquet
stockify pack --help                                      # event packs the engine reads
```

### Running a backtest

```powershell
stockify run --config examples\runs\sma_reliance_daily.json      # on the C++ engine
stockify run --config examples\runs\sma_reliance_daily.json --impl oracle
stockify run --rerun <run id>                                     # must reproduce the run byte for byte
stockify tools compare-strategies --config <file>                 # several strategies on the same days, side by side
```

The run configs in `examples/runs/` cover a daily moving-average cross, a one-minute opening-range breakout (in Python and in C++), an end-of-day short straddle with two margin models, and a straddle against an iron fly on recorded option chains. Results land under `%LOCALAPPDATA%\STOCKIFY\kite\lab\runs\` and show up on the `/tests` page, where a run can also be started.

Strategies that ship in `stockify/strategies/`: buy and hold, moving-average cross, opening-range breakout, end-of-day short straddle, short straddle with a stop, and iron fly.

- Writing, running and porting a strategy: [`docs/plan/HOWTO_strategy.md`](https://github.com/priyansh158/STOCKIFY/blob/stockify/m4-fno/docs/plan/HOWTO_strategy.md).
- Options, chains, margin and expiry: [`docs/plan/HOWTO_options.md`](https://github.com/priyansh158/STOCKIFY/blob/stockify/m4-fno/docs/plan/HOWTO_options.md).

### Margin tools

```powershell
stockify margin check --leg "SELL 65 NIFTY26N1724950CE" --leg "SELL 65 NIFTY26N1724950PE" --date 2026-11-09 --at 10:00
stockify margin scorecard          # how far each staircase step is from Kite's stored answers
stockify margin span-validate --date 2026-11-09   # the exact SPAN calculator against Kite's stored answers for that day
stockify margin calibrate          # market hours, fresh Kite login; asks Kite for margins only, places nothing
```

---

## Safety

- **No real orders, ever.** STOCKIFY reads quotes, candles, the portfolio and margin answers from Kite. Nothing in it places, changes or cancels an order.
- **Secrets stay put.** Kite keys live in `.env`; the daily token lives only in the token store under `%LOCALAPPDATA%\STOCKIFY\`. Neither is written to logs, files or output. A pre-commit hook runs a secret scan and fails closed.
- **Recorded ticks are irreplaceable.** Kite keeps no data for expired options, so the stores change only by migration, never by dropping data.
- **No invented market rules.** Charges, sessions, contract rules, freeze limits and margin settings are dated config rows with a source (`stockify/config/`, facts in `docs/plan/01_facts.md`).
- **Loopback only.** The API and the UI bind to `127.0.0.1`.

---

## Verification

```powershell
.\.venv\Scripts\python.exe tests\run_tests.py            # offline, credential-free (stdlib unittest); 4–10 minutes
.\.venv\Scripts\python.exe tests\run_tests.py stockify     # only test ids containing "stockify"
ctest --preset win-msvc-release                          # from engine/, inside the VS dev shell
.\.venv\Scripts\python.exe -m stockify config              # validate stockify/config/
.\.venv\Scripts\python.exe -m stockify engine              # engine build info
.\.venv\Scripts\python.exe -m stockify golden run          # every golden scenario
.\.venv\Scripts\python.exe -m stockify tools fuzz --help   # oracle against engine on random worlds
cd web; npm run lint; npm run build
```

The test runner exits 3 when the C++ engine cannot be loaded, so a run that silently skipped every engine test cannot pass; build the engine, or pass `--python-only` when a Python-only run is what you want.

`GET http://127.0.0.1:8001/api/health` → `{"ok":true,"service":"stockify-api"}`.

---

## Project layout

```
server/          FastAPI: auth, search, portfolio, ticks WS, alerts, /api/tests (runs), /api/strategies
web/             Next.js 16 UI (mobile-first; types mirrored in web/src/lib/types.ts)
stockify/        Python package and CLI (`python -m stockify`)
  config/        dated market rules (charges, sessions, contract rules, freeze limits, margin, calendar)
  data/          instruments, NSE files, margin-file archive, bar cache, ticks, compaction, packs, quality
  daemon/        recorder, scheduler, keep-awake, replay ticker, login reminder
  oracle/        the reference Python backtester
  fno/           Black-76, rolls, SPAN file reader, exact SPAN, the margin staircase, calibration
  strategies/    the strategies that ship with the lab
  tools/         benchmark, fuzzer, charge capture, strategy comparison, replay
engine/          C++20 core (stocksim_core), pybind module stockify._engine, stocksim_run CLI, doctest tests
  third_party/   vendored single headers (see VERSIONS.md)
examples/runs/   ready-to-run backtest configs
geo_monitor/     standalone news monitor (Gemini) → geo_alerts.sqlite
gnews/           Google News RSS client
docs/plan/       the STOCKIFY plan, decisions, facts, how-to guides and RECON notes
tests/           offline unittest suite (tests/stockify/ for the stockify package)
kite_client.py · kite_ticker_worker.py · tick_buffer.py · portfolio_sync.py
geo_alerts.py · india_utils.py · stockify_paths.py
```

All mutable state lives under `%LOCALAPPDATA%\STOCKIFY\` (resolved by `stockify_paths.py`). The simulation stores live under `…\STOCKIFY\kite\lab\`.

# STOCKIFY

**An NSE simulation lab for testing equity, futures and options strategies.** STOCKIFY records Kite Connect market data every trading day, backtests strategies over historical or recorded data with realistic costs, fills and margin, and paper-trades them live and unattended. It **never places real orders**.

It answers one question honestly: *would this rule have made money after realistic costs, fills and margin, and does it keep doing so live?*

- **Data:** Kite Connect (instruments, historical candles, KiteTicker WebSocket) plus NSE's public end-of-day files.
- **Engine:** a deterministic C++20 core (`engine/`) for orders, fills, costs, ledger, settlement and margin. It is exposed to Python as `stockify._engine` and runs strategies written in Python (default) or C++.
- **App:** a FastAPI backend (`server/`) and a mobile-first Next.js UI (`web/`).
- **Side process:** the geo news monitor (`geo_monitor/`) is unchanged.

The full execution plan is in [`docs/plan/`](docs/plan/README.md), milestones M0–M6. **Status:** M0 (cleanup + toolchain) is done, except that the engine has not yet been compiled on the dev machine (see [`docs/plan/RECON.md`](docs/plan/RECON.md)).

Windows only (decision D-26). Money is ₹, stored as integer paise; time is stored as UTC nanoseconds and shown in IST.

---

## Quick start

Install **PowerShell 7**, **Python 3.11 or 3.12 (x64)**, and **Node.js 20.9+**. To build the engine you also need **Visual Studio Build Tools** with the *Desktop development with C++* workload and *C++ CMake tools for Windows*. The launcher checks these versions, manages the venv and the deterministic `npm ci` install, and supervises the child processes.

```powershell
.\run.ps1                          # FastAPI :8001 + Next.js :3000
.\run.ps1 -BuildEngine             # build + test the C++ engine, then exit
.\run.ps1 -Daemon                  # API + web + STOCKIFY market daemon (restarts on crash)
.\run.ps1 -DaemonOnly              # STOCKIFY market daemon only
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

### Geo monitor

The geo monitor runs **outside** FastAPI, so news watching continues when the API is down:

```powershell
.\scripts\run_geo_monitor.ps1 -Once -DryRun
.\scripts\manage_geo_monitor.ps1 Install | Start | Status   # per-user Scheduled Task
```

See [`geo_monitor/README.md`](geo_monitor/README.md).

---

## Verification

```powershell
.\.venv\Scripts\python.exe tests\run_tests.py            # offline, credential-free (stdlib unittest)
.\.venv\Scripts\python.exe tests\run_tests.py stockify     # only test ids containing "stockify"
ctest --preset win-msvc-release                          # from engine/, inside the VS dev shell
.\.venv\Scripts\python.exe -m stockify config              # validate stockify/config/
.\.venv\Scripts\python.exe -m stockify engine              # engine build info
cd web; npm run lint; npm run build
```

`GET http://127.0.0.1:8001/api/health` → `{"ok":true,"service":"stockify-api"}`.

---

## Project layout

```
server/          FastAPI: auth, search, portfolio, ticks WS, alerts (tests/trade/data APIs from M3)
web/             Next.js 16 UI (mobile-first; types mirrored in web/src/lib/types.ts)
stockify/          Python: paths, numerics, store, config + loader, CLI, daemon, data layer
  config/        dated market rules (charges, sessions, contract rules, …) — no hard-coded rules
engine/          C++20 core (stocksim_core), pybind module stockify._engine, stocksim_run CLI
  third_party/   vendored single headers (see VERSIONS.md)
geo_monitor/     standalone news monitor (Gemini) → geo_alerts.sqlite
gnews/           Google News RSS client
docs/plan/     the STOCKIFY plan, decisions and RECON notes
tests/           offline unittest suite (tests/stockify/ for the stockify package)
kite_client.py · kite_ticker_worker.py · tick_buffer.py · portfolio_sync.py
geo_alerts.py · india_utils.py · stockify_paths.py
```

All mutable state lives under `%LOCALAPPDATA%\STOCKIFY\` (resolved by `stockify_paths.py`). The simulation stores live under `…\STOCKIFY\kite\lab\`.

Not investment advice. Simulation only.

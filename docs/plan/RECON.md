# RECON — what the code actually looks like

Append one dated section per milestone. Line numbers refer to the tree at the time of writing.

## 2026-11-02 — M0 recon (pre-deletion tree, commit `b019983` + uncommitted work)

### Items the plan relies on

| Plan reference | Actual | Notes |
|---|---|---|
| `kite_client.refresh_instruments` `:279` | `kite_client.py:279` | Keeps only segments `{"NSE","BSE","INDICES"}` (`:303`), so **NFO is dropped**. Table keyed by `instrument_token`, fully replaced on each refresh (`:220-232`, `:321`). `refresh_instruments_async` (`:336`) has no callers, so nothing schedules a refresh. → M1.1 |
| `get_instrument_token` `:445`, `quote` `:500`, `historical` `:513` | same lines | `historical` swallows every non-auth error and returns `[]` (`:513-535`). M1.4's "halve the chunk on a range-limit `InputException`" can't see that error through it. → M1.4 should call `kite.historical_data` directly or the wrapper must re-raise. |
| `is_authenticated`, `get_client`, `KiteAuthError` | `:179`, `:191`, `:60-68` | as described |
| Kite token store + login callback | `stockify_paths.state_dir()/kite_token.json` (`kite_client.py:50`); `GET /api/kite/callback` → `kite_client.exchange_request_token` (`server/routers/kite.py:51`) | The daemon reads it through `kite_client.load_cached_token()` / `get_client()` (I-10). |
| `kite_ticker_worker._Supervisor` `:67-225` | `:67` | **No reconnect or backoff logic to reuse.** `_on_reconnect` only logs (KiteTicker reconnects internally); the `_on_close` comment says auth errors stop reconnecting, but no code does that. It uses parsed `on_ticks`, not raw `on_message`. Re-subscribe on connect exists; unknown tokens default to QUOTE, which contradicts the class comment. → M1.5 writes its own supervisor. |
| `geo_alerts.py:56-78` store template | `:56-78` | singleton, WAL, `check_same_thread=False`, `Row` factory: as described |
| `geo_monitor/worker.py` `run_forever`, `load_env`; `__main__.py` | present | Logs to stdout only; the PowerShell launcher tees to `%LOCALAPPDATA%\STOCKIFY\logs\geo-monitor.log`. |
| `run.ps1` child supervision `:164-188` | `:164-190` | **No restart or backoff:** any child exit stops everything. → D-27 adds restart for the daemon only. |
| `tests/support.py` | `tests/support/__init__.py` (package) | `SandboxedDBTest` `:247`, `isolated_env` `:127` |
| `tests/run_tests.py` | present | Discovers subpackages that have `__init__.py`; exit 2 means a tracked defect started passing. |
| Tracked defect #1 `candles_since` | `tick_buffer.py:200-228` | Had no callers; **removed in M0.1** together with its expected-failure tests. |
| `stockify_paths.state_dir()` | `stockify_paths.py` | `local_appdata()` raises when `LOCALAPPDATA` is unset. Fine on Windows only (D-26). |

### Deletion import graph (M0.1)

- `geo_monitor/` imports nothing from `server/services/data.py`. No kept module (geo_monitor, gnews, alerts, search, kite, portfolio, ticks) imports `anthropic` or `playwright`. Neither 🛑 triggered.
- Importers outside the deleted set: `server/main.py:40` (`analysis_history` in lifespan); tests `test_known_defects`, `test_portfolio_sync` (`_position`), `test_module_health`, and `tests/support` (anthropic stub). All edited, not deleted.
- Web links into deleted routes: `web/src/app/page.tsx` (`/history`), `HoldingsList.tsx` (`/analyze/…`), `SearchBox.tsx` (`/analyze/…`).
- Dependencies no longer used: `anthropic`, `playwright`, `pypdf`, `sse-starlette` (removed). Kept: `yfinance` (search fallback), `requests` (gnews, geo monitor).

### Owner decisions at the M0.0 🛑

- D-03 stands: Python strategies by default, C++ optional.
- **Windows only, permanently (D-26)**: no Linux presets, CI, systemd or cloud host.
- Interim UI: STOCKIFY landing page; the search box shows instrument details without navigating; the HoldingsList analyze link is removed.
- Daemon restart with backoff (D-27). One commit for the whole overhaul (D-28). `CLAUDE.md` is never committed (D-29).
- Also removed as unused: `assets/favicon.png` (Streamlit era), `web/public/*.svg` (Next starter), `tests/geo_monitor/` (5 tests duplicated in `tests/test_geo_monitor.py`), dead helpers `portfolio_sync.{holding_for, anonymous_position_blurb, ui_caption_for}`, `india_utils.fetch_history_with_fallback`, `tick_buffer.{candles_since, macro_snapshot}`, `kite_ticker_worker.{resolve_macro_tokens, ensure_macro_subscriptions}`.

### Toolchain on the dev machine (blocks the M0.3 build)

- Python 3.12.10 venv; `pybind11 3.1.0` installed.
- **Visual Studio Build Tools 2026 (v18.4) is installed without the C++ workload**; no `cl`, no CMake, no Ninja. `run.ps1 -BuildEngine` stops with the fix. To install (admin, large download):
  ```powershell
  & "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\setup.exe" modify `
    --installPath "${env:ProgramFiles(x86)}\Microsoft Visual Studio\18\BuildTools" `
    --add Microsoft.VisualStudio.Workload.VCTools `
    --add Microsoft.VisualStudio.Component.VC.CMake.Project --includeRecommended --passive
  ```
  This brings `cl`, CMake and Ninja; `run.ps1 -BuildEngine` enters the VS developer shell itself. The presets use the Ninja generator, so they don't depend on the VS 2022 vs 2026 generator name.

### Open issues found in the plan docs (not blocking M0)

1. A1 §7.2 "VERIFY" rows vs M1.2's "exactly one row per component for 2024 sample dates": FO `exchange_txn` has no row before 2024-10-01, so a 2024-01 date fails. C-11's figures (said to equal G09) don't hold against the shipped config, because the current exchange rates resolve from `observed_on` = 2026-10-29.
2. `market_protection_auto_pct` = `"2"` meaning 2% (A1 §7.5) vs the fraction convention, and A4 §3.3's `market_protection_auto_bp`.
3. `run.json` policy keys `preopen` / `cas` / `freeze` (A1 §6.1) vs A4's `preopen_policy` / `cas_policy` / `freeze_policy`. `max_slices` appears in both `run.json` and `broker_policy.json`; its source R-1 says nothing about slices.
4. `DAY_END`: A4 §1.2 says 23:59:59.999999999 IST; `schedule.json` says `"23:59:59"`.
5. F&O pre-open scope: A1 §7.3 and A4 §4.7 put all `FO_FUT` in PREOPEN; X-6 says near month only (plus next month in the last 5 days).
6. G10 ownership: written in M2.5 and required on the engine by the M3 definition of done, but exercise STT and brokerage arrive only in M4.5.
7. M3 demo "byte-identical artifacts" vs A4 §9.3's 1e-12 / 100-paise tolerances for oracle vs engine.
8. `margin_flat.cpp` at `src/` (M3.8) vs `src/margin/` (A2 §1, M4.8). A2 §5 lacks `underlying_of` (M1.1). A2 §6 lists `/api/research/families`, which M6.4 never mentions. G12 asserts the reduce-only lot exception that A4 §3.2 marks ⚠.
9. M6.1 (six runs side by side) and the M6.10 heatmap don't say how they fit a ≤390px screen.

### Facts that are due now

- **T-1:** US daylight saving ended 2026-11-01. The Kite login window is now 16:30–19:00 PST; NSE trades 19:30–02:10 PST (pre-open included). Update T-1 and the PDT wording in M5 before M5.
- **Muhurat trading on 2026-11-08** (M1.2 ⚠) is unverified, and the recorder (M1.5) can't be ready by then.
- **Python 3.11 vs 3.12:** float `sum()` changed in 3.12 (A4 §0.3), and `run.ps1` accepts both. The engine records the Python it was built for, and `stockify.engine` refuses a mismatch. M2 must enforce the no-`sum()` rule.

# STOCKIFY

**AI-driven stock analysis for Indian equities.** A FastAPI backend + Next.js frontend that takes a company name, resolves it to its NSE/BSE listing via the Kite Connect instruments dump, and runs a 5-stage Claude-Opus pipeline — overview → financials → geopolitical risk → technicals → synthesized verdict. Every completed analysis is persisted to a local SQLite history store; you can revisit, filter, and replay past runs without spending more LLM tokens.

Live market data is sourced from **Kite Connect** when configured (instant LTP, accurate intraday OHLCV, WebSocket streaming, holdings sync) with `yfinance` as the offline fallback. Fundamentals stay on **NSE Corporate Announcements → Screener.in → IR-page Playwright fallback** — Kite has no fundamentals API.

Scope is hard-restricted to **NSE / BSE-listed Indian companies**. Currency is rendered in ₹ Cr / ₹ L; fiscal years use Indian FY notation (`FY25` = Apr 2024 – Mar 2025).

---

## Quick start

The PowerShell launcher manages both the venv and the `web/` npm install, runs `playwright install chromium` on first launch, and boots FastAPI + Next.js as parallel jobs.

```powershell
.\run.ps1                          # FastAPI :8001 + Next.js :3000
.\run.ps1 -Fresh                   # rebuild .venv + reinstall Python deps
.\run.ps1 -InstallWeb              # force `npm install` in web/
.\run.ps1 -ApiOnly                 # FastAPI only (curl tests)
.\run.ps1 -WebOnly                 # Next.js only
.\run.ps1 -ApiPort 8002 -WebPort 3001
```

Then open **http://localhost:3000**. The Next.js dev server proxies `/api/*` to the FastAPI origin (configurable via `STOCKIFY_API_ORIGIN`, default `http://127.0.0.1:8001`).

Direct invocation (after `.\venv` + `web/node_modules` exist) — two terminals:

```powershell
# Terminal 1 — backend
.\.venv\Scripts\python.exe -m uvicorn server.main:app --reload --port 8001

# Terminal 2 — frontend
cd web; npm run dev
```

You'll need an Anthropic API key in `.env` at the repo root, plus (optionally) Kite Connect credentials to unlock live market data:

```
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_AI_API_KEY=...      # Gemini — required for always-on geo monitor classification
NEWSAPI_KEY=...            # optional fallback if Google News returns sparse results
RAPIDAPI_KEY=...           # optional, enables Wall Street Journal article search
KITE_API_KEY=...           # optional, unlocks live market data + portfolio sync
KITE_API_SECRET=...        # paired with KITE_API_KEY

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

The daily Kite access token is **not** placed in `.env` — it is exchanged via the "Login to Kite" button on `/portfolio` and cached at `%LOCALAPPDATA%\STOCKIFY\kite\kite_token.json`.

### Always-on geo monitor

Runs **outside** FastAPI so news watching continues when the API is down:

```bash
python -m geo_monitor --once   # single cycle (test)
python -m geo_monitor          # loop forever
```

It polls Google News (`gnews/`) for macro themes + Kite holdings, classifies with Gemini, stores alerts, and emails on HIGH/CRITICAL. See [`geo_monitor/README.md`](geo_monitor/README.md). Analyze-stage geo also uses Google News and injects recent monitor alerts into the prompt.

---

## Architecture

```
┌──────────── Next.js 16 / TypeScript / Tailwind  (web/) ──────────────┐
│  /                       landing + debounced search                  │
│  /analyze/[ticker]       5-tab live analysis (SSE)                   │
│  /history                card list                                   │
│  /history/[id]           stored analysis replay (no LLM re-call)     │
│  /portfolio              read-only holdings + live LTPs              │
│  /kite/callback          OAuth landing                               │
└───────────────────────┬─────────────────────────────────────────────┘
                        │  /api/* proxied
                        │  (REST · SSE · WebSocket)
┌───────────────────────┴────────────  FastAPI / uvicorn (server/) ────┐
│  routers/                                                            │
│    search.py            GET  /api/search?q=…                         │
│    analysis.py          POST /api/analyze/{ticker}                   │
│                         GET  /api/analyze/{id}/state                 │
│                         GET  /api/analyze/{id}/stage/{key}/stream    │
│    history.py           GET/DELETE /api/history(/{id})               │
│    portfolio.py         GET  /api/portfolio/snapshot                 │
│    kite.py              GET  /api/kite/{login_url, callback, status} │
│                         POST /api/kite/{exchange, logout}            │
│    ticks.py             WS   /api/ws/ticks?tokens=…                  │
│  services/                                                           │
│    stream_claude.py            anthropic.AsyncAnthropic SSE bridge   │
│    prompts.py                  5 pure prompt builders                │
│    data.py                     yf_info + scrape_financials + macro   │
│    analysis_pipeline.py        AnalysisState + 5 streaming stages    │
│    ticker_bridge.py            KiteTicker thread → asyncio.Queue     │
│                                                                      │
│  Existing Python modules — imported by routers/services:             │
│   financial_analyst · geopolitical_risk_agent · technical_analyst    │
│   kite_client · kite_ticker_worker · tick_buffer                     │
│   portfolio_sync · analysis_history · india_utils · stock_agent      │
└──────────────────────────────────────────────────────────────────────┘
```

### Pipeline

The 5 stages run sequentially per analysis (one `AnalysisState` keyed by `analysis_id` in an in-process dict):

- **Stage 1 — Overview.** Company intelligence profile (sector, business model, supply chain, key markets, geopolitical exposures, risk keywords). Yfinance metadata seeds the prompt; Claude returns structured JSON.
- **Stage 2 — Financial Analyst.** Scores against a 22-item checklist framed for Ind-AS / SEBI / ICAI: profitability (EBIT/PAT trajectory, OPM trend), balance-sheet ratios, cash-flow adequacy, ROE/ROCE benchmarks, ICFR sign-off and unqualified auditor opinion under SA 700, SEBI Reg 30 litigation disclosure, related-party arm's-length under Reg 23. Inputs: NSE Corporate Announcements PDFs → Screener.in → IR-page Playwright fallback, plus yfinance DataFrames. Pass requires score ≥ 60. When the analyzed ticker is in your Zerodha holdings, the prompt receives an *anonymized* position-context block — no owner attribution, to avoid LLM confirmation-bias.
- **Stage 3 — Geopolitical Risk.** Looks for *collateral* risk from Indian-domestic and global events — RBI / MPC moves, FII/DII flows, INR vs USD, crude and commodity prices, monsoon, Indian elections, US Fed second-order EM effects, China+1 supply-chain shifts. News is fetched primarily via **Google News** (`gnews/`); NewsAPI/WSJ are fallbacks. Recent HIGH/CRITICAL alerts from the always-on monitor are prepended when relevant. Also receives a **live macro snapshot** (NIFTY 50, BANK NIFTY, sector indices, USD-INR) via `kite.quote()` when authenticated. Pass requires `risk_score ≤ 55`.
- **Stage 4 — Technical Analyst.** Tries Kite `historical_data` first (accurate intraday OHLCV); falls back to yfinance. Classifies into `DAY_TRADING / SWING_TRADING / LONG_TERM / AVOID` with confidence. Trading session is 09:15–15:30 IST.
- **Stage 5 — Summary.** Pure synthesis — no new data. Computes the verdict, generates the bottom-line narrative via a short Claude call (with a templated fallback), and writes the full analysis to the SQLite history store.

### SSE wire format

Each `GET /api/analyze/{id}/stage/{key}/stream` connection emits JSON events as `data: <json>\n\n` frames:

```
{"type": "delta", "text": "<full buffer so far>"}        // many, during streaming
{"type": "done",  "result": { ...parsed JSON... }}        // terminal, on success
{"type": "error", "message": "<friendly text>", "raw": "..."} // terminal, on failure
```

Friendly error translation (credit exhausted, rate limit, missing key, overloaded) lives in `server.services.stream_claude.friendly_claude_error`. The frontend's `src/lib/sse.ts` is a tiny `fetch`-based SSE parser (used instead of `EventSource` so the request can share the Next.js proxy rewrites).

### Live ticks

`KiteTicker` runs on a daemon thread inside `kite_ticker_worker.py`. `server/services/ticker_bridge.py` registers a tick listener at FastAPI startup; each tick batch is fanned out to every registered `asyncio.Queue` via `loop.call_soon_threadsafe(...)`. `WS /api/ws/ticks` drains the queue and sends one JSON frame per tick. The frontend's `useTicks(tokens)` hook opens the WS, reconnects every 3 s on disconnect, and returns a map of `instrument_token → latest tick`. Used by the analyze-page header LTP chip and the portfolio holdings list.

---

## Tech stack

- **FastAPI + uvicorn** for the Python backend; **Pydantic v2** response models; **sse-starlette** for Server-Sent Events; native FastAPI WebSocket for live ticks.
- **Next.js 16 (App Router) + TypeScript + Tailwind v4** for the frontend; React 19 Client Components for live state, Server Components for history pages.
- **Anthropic Claude Opus** (`claude-opus-4-6`, configurable in `server/services/stream_claude.py:DEFAULT_MODEL`). The pipeline uses `thinking_mode="adaptive"` for overview / geo / technical stages and `"off"` for the strict-JSON financials + bottom-line callers.
- **Kite Connect (`kiteconnect`)** for the live track — REST `historical_data` / `ltp` / `quote` / `holdings` / `positions` plus `KiteTicker` WebSocket streaming.
- **yfinance** as the offline fallback for OHLCV and basic ticker metadata when Kite isn't configured.
- **SQLite (WAL)** for three local stores: instruments dump, tick ringbuffer, analysis history.
- **Playwright (Chromium)** for last-resort IR-page scraping when both NSE Corporate Announcements and Screener.in return nothing.

There is no test suite or lint config yet — verification today means hitting endpoints with `curl`, opening pages in a browser, and import-checking modules (`python -c "import server.main"` / `cd web && npm run build`).

---

## Mobile-first

STOCKIFY is accessed from both phone and laptop browsers as primary clients. Every UI change is checked at ≤390px (iPhone) viewport.

Concrete design rules (Tailwind + Next.js):

- Single-column flow preferred; `grid-cols-*` capped at 2-3 columns with responsive prefixes (`sm:grid-cols-2`) so mobile collapses to one column.
- Card lists (history, portfolio) instead of wide HTML tables on mobile.
- Brand tokens live in `web/src/app/globals.css` — use the CSS variables (`var(--color-green)`, `var(--font-mono)`, etc.) rather than reintroducing hex literals.
- Tap targets stay ≥44px; full-width buttons are the default on narrow viewports.
- Charts pass `responsive` / `100%` width and avoid hover-only information — mobile has no hover.
- No `:has()` selector hacks — they were a Streamlit-era workaround; with React you control the DOM directly.

---

## Conventions

- **Tickers.** Canonical form is the bare NSE symbol (`RELIANCE`, `TCS`). The Kite instruments cache is the authoritative resolver when configured. yfinance suffixes are appended only at API call boundaries via `india_utils.to_yfinance_ticker(...)`; the Kite format `NSE:RELIANCE` via `india_utils.to_kite_symbol(...)`.
- **Currency.** Headlines use `india_utils.format_inr_compact` (`₹X Cr` / `₹X L`); tables use `format_inr_full` (Indian-comma grouping `1,23,45,678`); quoted prices use `format_price` (`₹X,XXX.XX`). Frontend uses `Intl.NumberFormat("en-IN", {style:"currency",currency:"INR"})`. No `$` in user-facing strings.
- **Fiscal year.** Prompts and commentary use Indian FY notation (`FY25` = Apr 2024 – Mar 2025). Quarter shorthand: `Q2 FY25`. US calendar quarters are not used in user-facing output.
- **Result schemas.** Every stage's parsed JSON, plus `AnalysisRecord`, `HoldingDTO`, `PortfolioSnapshotDTO`, `SearchHit`, `HistoryRecord` — all have stable shapes defined in `server/schemas.py` (Pydantic) and mirrored in `web/src/lib/types.ts` (TypeScript). When a shape changes, update both.

---

## Two-track architecture (Kite live + Screener fundamentals)

The live track is built up through analysis (Phases 0–4 of the original roadmap); the execution track (Phases 5–6) is deliberately not implemented — STOCKIFY is analysis-only.

- **Track A — Kite Connect** (live data): NSE/BSE symbol canonicalization via the daily instruments dump, REST `ltp` / `quote` / `historical_data`, `KiteTicker` WebSocket streaming, read-only `holdings` / `positions` sync.
- **Track B — Screener.in** (fundamentals): quarterly + annual P&L, balance sheet, cash flows, ratios, multi-quarter trend tables. Source of truth for fundamentals — Kite has no fundamentals API, so the two tracks are not interchangeable.

The two tracks meet at one place only: the **canonical NSE symbol**, derived from Kite's instruments dump when configured, and consumed by every downstream module.

### Why the symbol-resolution unlock matters

Empirical reliability test of the Screener.in scraper against 18 real Indian listings (run May 2026):

| Metric | Result |
|---|---|
| HTTP success on valid symbols | 14 / 16 raw → 16 / 16 with correct symbol |
| All 5 sections present (Quarterly, P&L, BS, CF, Ratios) | 100% when page loads |
| Data freshness | Mar 2026 (Q4 FY26) ingested for every ticker, ~3 weeks after the filing window opened |
| Mean latency | ~1.3 s per ticker |

The two failures were both upstream symbol-mismatch events: `ZOMATO` (renamed to `ETERNAL` in 2024) and `LIC` (actual NSE symbol is `LICI`). Both worked instantly under their correct current symbols. The Kite instruments cache resolves these renames at the source.

### What is implemented today

| Phase | What it does |
|---|---|
| **0 — Symbol resolver** | Daily instruments dump cached to SQLite; `/api/search` is Kite-first with yfinance fallback. |
| **1 — Market data swap** | `kite.historical_data()` is the primary OHLCV source in Stage 4; yfinance is the offline fallback. |
| **2 — Streaming ticks** | `KiteTicker` runs on a daemon thread (module-singleton), writes into a SQLite ringbuffer keyed on `(instrument_token, ts)`. Browser clients receive ticks live via `WS /api/ws/ticks` (fanout in `ticker_bridge.py`). Reconnect-storm idempotency guaranteed by `INSERT OR REPLACE`. |
| **3 — Macro injection** | NIFTY 50, BANK NIFTY, sector indices, USD-INR fetched via `kite.quote()` once at Stage 3 start and injected into the geo-risk prompt under "CURRENT INDIAN MARKET SNAPSHOT". Snapshot is persisted to the History record. |
| **4 — Portfolio sync** | Read-only `kite.holdings()` + `kite.positions()` with a 60 s cache. `/portfolio` lists holdings with live LTPs via `useTicks`. The Stage 2 (financials) prompt receives an **anonymized** position block when the analyzed ticker matches a holding. |

### Anonymous-position framing rule

When the analyzed ticker is in your Zerodha holdings, the Stage 2 (financials) prompt receives a block titled `OBSERVED POSITION FOR CONTEXT` with cost basis, P&L, and an explicit instruction *not* to assume the analysis recipient owns this. The strings "user", "you", "your" never appear in connection with holdings. This avoids the well-known LLM confirmation-bias failure mode where knowing the user holds X biases the analysis toward flattering the position. Stages 1, 3, 4, and 5 stay fully position-blind.

### Not implemented (and not on the roadmap)

- **Order placement** (GTT or active): owner has chosen analysis-only scope, no order-related code surfaces exist.
- **Multi-user**: single-user only. Kite token, tick buffer, and history store are all single-SQLite per machine. User auth on the FastAPI side is not implemented; the backend assumes localhost-only access.

### Known risks

- Daily Kite token rotation (~6 a.m. IST) — the portfolio page detects expiry and prompts re-login.
- Kite rate limits: 10 req/sec non-order — mitigated by streaming-first design and the 60 s portfolio cache.
- `KiteTicker` reconnect storms re-fire ticks; all consumers are idempotent on `(token, ts)` at the SQL level.

---

## Project layout

```
server/                        FastAPI backend
  main.py                      uvicorn entry + CORS + lifespan (ticker bridge)
  schemas.py                   Pydantic response models
  routers/                     one module per resource (search, analysis, ...)
  services/                    stream_claude · prompts · data · analysis_pipeline · ticker_bridge

web/                           Next.js 16 frontend
  src/app/                     route pages (App Router)
  src/components/              SearchBox, StageTabs, StagePane, stage-views, ...
  src/lib/                     api · sse · types · useTicks · json-helpers

financial_analyst.py           Stage 2 — Ind-AS / SEBI scored checklist
geopolitical_risk_agent.py     Stage 3 — collateral macro risk + live market context
technical_analyst.py           Stage 4 — Kite OHLCV (yfinance fallback) classification
stock_agent.py                 Importable programmatic equivalent of the pipeline
kite_client.py                 Auth + instruments cache + REST wrappers
kite_ticker_worker.py          Module-singleton KiteTicker daemon-thread supervisor
tick_buffer.py                 SQLite WAL ringbuffer for live ticks
portfolio_sync.py              Holdings sync + anonymous-position prompt block
analysis_history.py            SQLite-backed history of completed analyses
india_utils.py                 ₹ formatters + NSE/BSE ticker helpers
assets/favicon.png             Browser tab icon (candlestick-styled S)
run.ps1                        PowerShell launcher (venv + npm + FastAPI + Next.js)
requirements.txt               Python dependencies
CLAUDE.md                      Operational context for Claude Code sessions
```

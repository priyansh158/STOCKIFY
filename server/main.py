"""FastAPI entry point.

Run with:
    uvicorn server.main:app --reload --port 8000

The `.env` at repo root is loaded once at import time using the same pattern
as the legacy `app.py` (override=True, explicit dotenv_path) so the block-env
hook still applies and the existing key-resolution code is unchanged.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv

# Load .env BEFORE importing kite_client / analysts (they read env at import).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(dotenv_path=os.path.join(_REPO_ROOT, ".env"), override=True)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from server.routers import (
    alerts,
    analysis,
    history,
    kite as kite_router,
    portfolio,
    search,
    ticks,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Lazy import so module-level state is initialized after env loads.
    import analysis_history
    import tick_buffer
    from server.services import ticker_bridge

    analysis_history.init_db()
    tick_buffer.init_db()
    try:
        import geo_alerts

        geo_alerts.init_db()
    except Exception:
        pass
    # Register the KiteTicker → WS fanout bridge. The supervisor is started
    # on-demand by the /api/ws/ticks endpoint when a client connects.
    # Note: the always-on geo monitor runs as `python -m geo_monitor` separately
    # so news watching continues even when this API process is down.
    ticker_bridge.attach()
    yield
    try:
        ticker_bridge.detach()
    except Exception:
        pass
    # Graceful KiteTicker shutdown on app exit.
    try:
        import kite_ticker_worker
        kite_ticker_worker.shutdown()
    except Exception:
        pass


app = FastAPI(
    title="STOCKIFY API",
    version="1.0.0",
    description="Indian-equity analysis backend (Claude Opus pipeline + Kite Connect).",
    lifespan=lifespan,
)

# CORS — the Next.js dev server runs on :3000; allow it explicitly.
# Production origin(s) will be added when deployment target is chosen.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> dict:
    """Liveness probe. Hit by run-dev.ps1 + Next.js dev proxy."""
    return {"ok": True, "service": "stockify-api"}


# Routers
app.include_router(search.router)
app.include_router(analysis.router)
app.include_router(history.router)
app.include_router(portfolio.router)
app.include_router(kite_router.router)
app.include_router(ticks.router)
app.include_router(alerts.router)

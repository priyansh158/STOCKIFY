"""FastAPI entry point.

Run with:
    uvicorn server.main:app --reload --host 127.0.0.1 --port 8001

The `.env` at repo root is loaded once at import time using the same pattern
as the legacy `app.py` (override=True, explicit dotenv_path) so the block-env
hook still applies and the existing key-resolution code is unchanged.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from dotenv import load_dotenv
import stockify_paths

# Load .env BEFORE importing kite_client (it reads env at import).
load_dotenv(dotenv_path=stockify_paths.env_file(), override=True)
_WEB_ORIGIN = stockify_paths.loopback_origin(
    "STOCKIFY_WEB_ORIGIN", "http://127.0.0.1:3000"
)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from server.routers import (
    alerts,
    kite as kite_router,
    portfolio,
    search,
    ticks,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Lazy import so module-level state is initialized after env loads.
    import tick_buffer
    from server.services import ticker_bridge

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
    description="STOCKIFY backend: NSE simulation lab on Kite Connect data.",
    lifespan=lifespan,
)

# CORS is restricted to the configured Windows-loopback web origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[_WEB_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> dict:
    """Liveness probe used by run.ps1 and the Next.js dev proxy."""
    return {"ok": True, "service": "stockify-api"}


# Routers
app.include_router(search.router)
app.include_router(portfolio.router)
app.include_router(kite_router.router)
app.include_router(ticks.router)
app.include_router(alerts.router)

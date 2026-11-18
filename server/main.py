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

import kite_client  # noqa: E402, F401  — patches the Kite SDK's client class to paper only (LCB 5.7)
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from server.routers import (
    alerts,
    data as data_router,
    desk as desk_router,
    fundamentals as fundamentals_router,
    kite as kite_router,
    portfolio,
    research as research_router,
    jobs as jobs_router,
    search,
    tests as tests_router,
    trade as trade_router,
    ticks,
)
from server import local_only, log_redaction

log_redaction.install()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # uvicorn (re)configures its loggers before startup; make sure the
    # access-log redaction is still attached.
    log_redaction.install()
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
    # Strategy tests (M3.14): re-enqueue runs left queued, fail runs a previous API process left running.
    try:
        from stockify import store
        from server.services.test_queue import get_queue

        store.init_db()
        get_queue().recover()
        from server.services import web_jobs
        web_jobs.recover()          # a sweep this API started and a restart cut short is marked interrupted
    except Exception:
        import logging

        logging.getLogger(__name__).exception("test queue recovery failed")
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
# The API serves this machine's own UI only (server/local_only.py). The middleware added last
# runs first: the Host is checked, then the X-Forwarded-Host the web server's rewrite adds, then
# the Origin, and only then CORS and the routes.
app.add_middleware(local_only.WebOriginOnly, web_origin=_WEB_ORIGIN)
app.add_middleware(local_only.ForwardedHostOnly)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(local_only.LOOPBACK_HOSTS), www_redirect=False)


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
app.include_router(data_router.router)
app.include_router(tests_router.router)
app.include_router(research_router.router)
app.include_router(jobs_router.router)
app.include_router(trade_router.router)
app.include_router(desk_router.router)
app.include_router(fundamentals_router.router)

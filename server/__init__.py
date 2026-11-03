"""STOCKIFY SimLab FastAPI backend.

Serves Kite auth, instrument search, portfolio, the live-ticks WebSocket and
geo alerts. The repo-root modules (kite_client, kite_ticker_worker,
tick_buffer, portfolio_sync, geo_alerts, india_utils) are imported as services.
"""

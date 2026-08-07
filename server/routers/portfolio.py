"""GET /api/portfolio/snapshot — read-only Kite holdings + P&L summary."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter

import portfolio_sync
from server.schemas import HoldingDTO, PortfolioSnapshotDTO

log = logging.getLogger("stockify.portfolio")

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


def _to_dto(snap: portfolio_sync.PortfolioSnapshot) -> PortfolioSnapshotDTO:
    holdings = [
        HoldingDTO(
            symbol=h.tradingsymbol,
            exchange=h.exchange,
            quantity=h.quantity,
            average_price=h.average_price,
            last_price=h.last_price,
            pnl=h.pnl,
            pnl_pct=h.pnl_pct,
            instrument_token=int(h.instrument_token or 0),
        )
        for h in snap.holdings
    ]
    return PortfolioSnapshotDTO(
        holdings=holdings,
        total_value=snap.total_current,
        total_pnl=snap.total_pnl,
        fetched_at=snap.fetched_at,
        stale=False,
        error=snap.error,
    )


@router.get("/snapshot", response_model=PortfolioSnapshotDTO)
async def snapshot() -> PortfolioSnapshotDTO:
    """Returns the cached (≤60 s old) portfolio snapshot. When Kite isn't
    authenticated, `error` is set and `holdings` is empty — clients should
    treat that as a 'log in first' state, not a 500."""
    # `portfolio_sync.cached_snapshot` is sync + hits the Kite REST API on a
    # cache miss — bounce to a worker thread so we don't block the loop.
    snap = await asyncio.to_thread(portfolio_sync.cached_snapshot, 60)
    return _to_dto(snap)

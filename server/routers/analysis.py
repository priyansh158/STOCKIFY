"""Analysis endpoints — pipeline kick-off + per-stage SSE streams.

POST /api/analyze/{ticker}
    Initialises an `AnalysisState`, returns its analysis_id + the ordered
    stage keys the client should consume.

GET  /api/analyze/{analysis_id}/stage/{stage_key}/stream
    Server-Sent-Events stream for one stage. Yields:
      data: {"type": "delta", "text": "<full buffer so far>"}
      data: {"type": "done",  "result": {...}}                  (terminal)
      data: {"type": "error", "message": "<friendly>", "raw": "..."}  (terminal)
    The client consumes stages in order; advancing to the next stage is the
    client's job (the pipeline is staged + gated, and gating decisions live
    in the verdict logic on stage 5, not in the SSE flow).
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from server.services import analysis_pipeline
from server.services.analysis_pipeline import STAGE_ORDER, STAGE_STREAMERS

log = logging.getLogger("stockify.analysis")

router = APIRouter(prefix="/api/analyze", tags=["analysis"])


@router.post("/{ticker}")
async def start_analysis(ticker: str) -> dict:
    """Create a fresh AnalysisState and return its id + stage plan.

    The client then issues sequential SSE GETs against the stage endpoints
    below, in STAGE_ORDER. Each stage's result lands in state and unblocks
    the next.
    """
    try:
        state = analysis_pipeline.create_analysis(ticker)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Resolve the Kite instrument_token for live-tick subscription. Best-effort
    # — when Kite isn't configured we return 0 and the UI falls back to the
    # static last close (Stage 3's `current_price`).
    instrument_token = 0
    try:
        import kite_client
        instrument_token = (
            kite_client.get_instrument_token(state.ticker, "NSE")
            or kite_client.get_instrument_token(state.ticker, "BSE")
            or 0
        )
    except Exception:
        pass

    return {
        "analysis_id":      state.analysis_id,
        "ticker":           state.ticker,
        "company_name":     state.company_name,
        "sector":           state.sector,
        "stages":           list(STAGE_ORDER),
        "instrument_token": int(instrument_token or 0),
    }


@router.get("/{analysis_id}/state")
async def get_analysis_state(analysis_id: str) -> dict:
    """Return the current state for an analysis_id.

    Used for resumption + debugging. Includes per-stage status, results, and
    error messages. Does NOT include raw prompts (those are server-side only).
    """
    state = analysis_pipeline.get_state(analysis_id)
    if state is None:
        raise HTTPException(status_code=404, detail="analysis_id not found")
    return {
        "analysis_id":      state.analysis_id,
        "ticker":           state.ticker,
        "company_name":     state.company_name,
        "sector":           state.sector,
        "stages":           list(STAGE_ORDER),
        "stage_status":     dict(state.stage_status),
        "stage_errors":     dict(state.stage_errors),
        "results": {
            "overview":   state.overview,
            "financials": state.financials,
            "geo":        state.geo,
            "technical":  state.technical,
            "summary":    state.summary,
        },
        "verdict":          state.verdict,
        "verdict_reason":   state.verdict_reason,
        "bottom_line":      state.bottom_line,
        "macro_snapshot":   state.macro_snapshot,
        "source_tag":       state.source_tag,
    }


@router.get("/{analysis_id}/stage/{stage_key}/stream")
async def stage_stream(analysis_id: str, stage_key: str) -> EventSourceResponse:
    """SSE stream of a single stage's progress.

    Events are emitted as JSON objects (one per `data:` line). The client is
    expected to JSON.parse each event and switch on `event.type`.
    """
    if stage_key not in STAGE_STREAMERS:
        raise HTTPException(status_code=400, detail=f"unknown stage_key: {stage_key}")
    state = analysis_pipeline.get_state(analysis_id)
    if state is None:
        raise HTTPException(status_code=404, detail="analysis_id not found")

    # Guard: every stage except overview needs at least the previous stage to
    # have populated state. We don't strictly enforce gating here (the verdict
    # logic in `compute_verdict` handles fail-cases at stage 5) — but the
    # technical stage doesn't actually depend on overview at all, while geo
    # and summary do. Keeping the ordering check soft to let the UI retry a
    # single stage in isolation if it errored.
    streamer = STAGE_STREAMERS[stage_key]

    async def gen():
        try:
            async for event in streamer(state):
                yield {"event": "message", "data": json.dumps(event)}
        except asyncio.CancelledError:
            log.info("SSE %s/%s cancelled by client", analysis_id, stage_key)
            raise
        except Exception as e:  # pragma: no cover — last-ditch
            log.exception("SSE %s/%s crashed", analysis_id, stage_key)
            yield {
                "event": "message",
                "data": json.dumps({
                    "type": "error",
                    "message": f"Stage crashed: {e!s}",
                    "raw": repr(e),
                }),
            }

    return EventSourceResponse(gen())

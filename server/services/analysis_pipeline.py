"""5-stage analysis pipeline orchestration (FastAPI side).

Pure async equivalent of the old Streamlit `_run_stage_*` family in `app.py`.
The shape of the pipeline is unchanged — overview → financials → geo →
technical → summary — but state is keyed by `analysis_id` in an in-process
dict rather than `st.session_state`, and each stage exposes a streaming
generator that yields SSE-shaped events.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from technical_analyst import TechnicalAnalyst
import portfolio_sync

from server.services import data, prompts
from server.services.stream_claude import (
    friendly_claude_error,
    parse_json,
    stream_claude_events,
)

log = logging.getLogger("stockify.pipeline")

# Order is load-bearing — each stage may depend on prior stages' results.
STAGE_ORDER: tuple[str, ...] = (
    "overview",
    "financials",
    "geo",
    "technical",
    "summary",
)

FIN_PASS_THRESHOLD_DEFAULT = 60
GEO_FAIL_THRESHOLD_DEFAULT = 55


@dataclass
class AnalysisState:
    analysis_id: str
    ticker: str  # bare NSE symbol
    company_name: str = ""
    sector: str = ""
    description: str = ""
    yf_info: dict = field(default_factory=dict)
    # Stage results (None until completed)
    overview: dict | None = None
    financials: dict | None = None
    geo: dict | None = None
    technical: dict | None = None
    summary: dict | None = None
    verdict: str = ""
    verdict_reason: str = ""
    bottom_line: str = ""
    # Misc context
    macro_snapshot: dict | None = None
    position_context: dict | None = None
    source_tag: str = ""
    # Per-stage status: pending | loading | ready | error
    stage_status: dict[str, str] = field(default_factory=lambda: {k: "pending" for k in STAGE_ORDER})
    stage_errors: dict[str, str] = field(default_factory=dict)


# In-memory store of active analyses. Single-user / single-process is fine for
# now; multi-instance deployments would need Redis or SQLite-backed state.
_STATE: dict[str, AnalysisState] = {}


def create_analysis(ticker: str) -> AnalysisState:
    """Initialise a new analysis. Resolves yfinance metadata up-front so the
    overview stage has data to work with."""
    from india_utils import to_yfinance_ticker

    bare = ticker.strip().upper()
    if not bare:
        raise ValueError("ticker is required")

    info = data.get_yf_info(to_yfinance_ticker(bare))
    state = AnalysisState(
        analysis_id=uuid.uuid4().hex,
        ticker=bare,
        company_name=info.get("name") or bare,
        sector=info.get("sector", ""),
        description=info.get("description", ""),
        yf_info=info,
    )
    # Pre-populate position context (anonymised, never owner-attributed).
    try:
        holding = portfolio_sync.holding_for(bare)
        if holding:
            state.position_context = holding.to_dict()
    except Exception:
        state.position_context = None

    _STATE[state.analysis_id] = state
    return state


def get_state(analysis_id: str) -> AnalysisState | None:
    return _STATE.get(analysis_id)


# ── Verdict + summary builders (pure, no Claude) ──────────────────────────────

def _safe_subfield(d: dict, parent: str, child: str) -> str:
    v = d.get(parent)
    if v is None:
        return ""
    if isinstance(v, dict):
        sub = v.get(child)
        return str(sub) if sub not in (None, "") else ""
    if isinstance(v, str):
        return v
    return str(v)


def _exposure_text(d: dict, *keys: str) -> str:
    for k in keys:
        v = d.get(k)
        if v is None or v == "":
            continue
        if isinstance(v, str):
            return v
        if isinstance(v, dict):
            for inner in ("note", "description", "summary", "level"):
                inner_v = v.get(inner)
                if isinstance(inner_v, str) and inner_v.strip():
                    return inner_v
            return "; ".join(f"{k2}: {v2}" for k2, v2 in v.items() if v2)
    return ""


def compute_verdict(
    fin: dict, geo: dict, tech: dict,
    fin_threshold: int = FIN_PASS_THRESHOLD_DEFAULT,
    geo_threshold: int = GEO_FAIL_THRESHOLD_DEFAULT,
) -> tuple[str, str]:
    fin_score = int(fin.get("score", 0) or 0)
    geo_score = int(geo.get("risk_score", 0) or 0)
    tech_rec = (tech.get("recommendation") or "").upper().strip()

    if fin and fin_score < fin_threshold:
        return ("AVOID",
                f"Failed financial screen ({fin_score}/100, "
                f"threshold {fin_threshold}/100).")
    if geo and geo_score > geo_threshold:
        level = geo.get("risk_level", "HIGH")
        return ("AVOID",
                f"Failed geopolitical screen (risk {geo_score}/100, "
                f"level {level}, threshold {geo_threshold}).")
    if tech_rec == "AVOID":
        return ("AVOID",
                "Technical analysis flagged no safe entry — wait for a clean setup.")

    rec_map = {
        "DAY_TRADING":   "BUY (Day Trading)",
        "SWING_TRADING": "BUY (Swing Trading)",
        "LONG_TERM":     "BUY (Long-Term Investment)",
    }
    verdict = rec_map.get(tech_rec, "REVIEW")
    if verdict == "REVIEW":
        return (verdict, "Stages passed but technical recommendation is unclear.")
    return (verdict, "All three stages cleared.")


def build_summary_data(
    profile: dict, fin: dict, geo: dict, tech: dict,
    *,
    ticker: str, company_name: str,
    exchange: str = "", country: str = "", sector: str = "",
) -> dict:
    return {
        "company": {
            "name":     company_name or profile.get("company_name", ""),
            "ticker":   ticker,
            "exchange": exchange,
            "country":  country,
            "sector":   sector or profile.get("sector", ""),
        },
        "profile": {
            "industry":              profile.get("industry", ""),
            "hq_country":            profile.get("hq_country", ""),
            "business_model":        profile.get("business_model", ""),
            "key_products":          profile.get("key_products", []) or [],
            "key_markets":           profile.get("key_markets", []) or [],
            "supply_chain":          profile.get("supply_chain", []) or [],
            "geopolitical_exposures": profile.get("geopolitical_exposures", []) or [],
            "risk_keywords":         profile.get("risk_keywords", []) or [],
        },
        "financial": {
            "score":                 int(fin.get("score", 0) or 0),
            "passed":                bool(fin.get("passed", False)),
            "source_tag":            fin.get("source_tag", ""),
            "revenue_status":        _safe_subfield(fin, "revenue_trend", "status"),
            "cash_flow_status":      _safe_subfield(fin, "cash_flow", "status"),
            "margin_trend":          _safe_subfield(fin, "profitability", "trend"),
            "balance_sheet_status":  _safe_subfield(fin, "balance_sheet", "status"),
            "key_metrics":           fin.get("key_metrics", {}) or {},
            "green_flags":           fin.get("green_flags", []) or [],
            "red_flags":             fin.get("red_flags", []) or [],
            "deep_analysis":         fin.get("deep_analysis", {}) or {},
            "summary":               fin.get("summary", ""),
            "recommendation":        fin.get("recommendation", ""),
        },
        "geo": {
            "risk_score":         int(geo.get("risk_score", 0) or 0),
            "risk_level":         geo.get("risk_level", ""),
            "passed":             bool(geo.get("passed", False)),
            "exposures": {
                "sector":             _exposure_text(geo, "sector_exposure", "sector_risk"),
                "geographic":         _exposure_text(geo, "geographic_exposure", "geographic_risk"),
                "supply_chain":       _exposure_text(geo, "supply_chain_exposure", "supply_chain_risk"),
                "regulatory":         _exposure_text(geo, "regulatory_exposure", "regulatory_risk"),
                "consumer_sentiment": _exposure_text(geo, "consumer_sentiment_exposure", "consumer_sentiment_risk"),
            },
            "collateral_risks":   geo.get("collateral_risks", []) or [],
            "safe_signals":       geo.get("safe_signals", []) or [],
            "relevant_articles":  geo.get("relevant_articles", []) or [],
            "summary":            geo.get("summary", ""),
            "recommendation":     geo.get("recommendation", ""),
        },
        "technical": {
            "recommendation":     tech.get("recommendation", ""),
            "confidence":         int(tech.get("confidence", 0) or 0),
            "trend":              tech.get("trend", ""),
            "timeframe":          tech.get("timeframe", ""),
            "current_price":      tech.get("current_price", ""),
            "period_change":      tech.get("period_change", ""),
            "entry_point":        tech.get("entry_point", ""),
            "target_price":       tech.get("target_price", ""),
            "stop_loss":          tech.get("stop_loss", ""),
            "risk_reward_ratio":  tech.get("risk_reward_ratio", ""),
            "patterns_found":     tech.get("patterns_found", []) or [],
            "support_levels":     tech.get("support_levels", []) or [],
            "resistance_levels":  tech.get("resistance_levels", []) or [],
            "volume_analysis":    tech.get("volume_analysis", ""),
            "momentum":           tech.get("momentum", ""),
            "trading_notes":      tech.get("trading_notes", []) or [],
            "summary":            tech.get("summary", ""),
        },
    }


def _fallback_bottom_line(summary: dict, verdict: str) -> str:
    fin = summary.get("financial", {})
    geo = summary.get("geo", {})
    tech = summary.get("technical", {})
    return (
        f"Verdict: {verdict}. Financial {fin.get('score', 0)}/100 "
        f"({'pass' if fin.get('passed') else 'fail'}), geopolitical risk "
        f"{geo.get('risk_score', 0)}/100 ({geo.get('risk_level', '—')}), "
        f"technical {tech.get('recommendation', '—')} at "
        f"{tech.get('current_price', '—')}."
    )


# ── Per-stage streaming generators ────────────────────────────────────────────
#
# Each `stream_<stage>` is an async generator that yields the same shape of
# events as `stream_claude_events`, AND mutates `state` with the parsed result
# upon successful completion. Errors are stored in `state.stage_errors[key]`.

async def _stream_claude_stage(
    state: AnalysisState,
    stage_key: str,
    prompt: str,
    *,
    thinking_mode: str = "adaptive",
    max_tokens: int = 16_000,
) -> AsyncIterator[dict]:
    """Internal helper — runs Claude, accumulates events, and on `done`
    attempts JSON-parse + returns one extra `done` event with the parsed
    result so the SSE endpoint can ship it to the client in the same stream."""
    state.stage_status[stage_key] = "loading"
    state.stage_errors.pop(stage_key, None)

    final_text = ""
    error_event: dict | None = None

    async for event in stream_claude_events(
        prompt, thinking_mode=thinking_mode, max_tokens=max_tokens,  # type: ignore[arg-type]
    ):
        if event["type"] == "delta":
            yield event
        elif event["type"] == "done":
            final_text = event["text"]
        elif event["type"] == "error":
            error_event = event

    if error_event is not None:
        state.stage_status[stage_key] = "error"
        state.stage_errors[stage_key] = error_event["message"]
        yield error_event
        return

    parsed = parse_json(final_text)
    if not parsed:
        msg = "Model returned unparseable JSON for the " + stage_key + " stage."
        state.stage_status[stage_key] = "error"
        state.stage_errors[stage_key] = msg
        yield {"type": "error", "message": msg, "raw": final_text[:500]}
        return

    # The caller post-processes the parsed dict and writes it to state.
    yield {"type": "_parsed", "result": parsed}


async def stream_overview(state: AnalysisState) -> AsyncIterator[dict]:
    prompt = prompts.build_profile_prompt(state.ticker, state.yf_info)
    async for event in _stream_claude_stage(state, "overview", prompt):
        if event["type"] != "_parsed":
            yield event
            continue
        profile = event["result"]
        profile.setdefault("company_name", state.company_name or state.ticker)
        profile.setdefault("sector", state.sector)
        state.overview = profile
        # Backfill state fields the next stages depend on.
        if profile.get("company_name"):
            state.company_name = profile["company_name"]
        if profile.get("sector"):
            state.sector = profile["sector"]
        state.stage_status["overview"] = "ready"
        yield {"type": "done", "result": profile}


async def stream_financials(state: AnalysisState) -> AsyncIterator[dict]:
    from india_utils import to_yfinance_ticker

    # The scraping pipeline is sync + IO-bound — push to a thread so we don't
    # block the asyncio loop while it talks to NSE / Screener / Playwright.
    website = (state.yf_info or {}).get("website", "")
    scraped, yf_df, source_tag = await asyncio.to_thread(
        data.scrape_financials, website, to_yfinance_ticker(state.ticker),
    )
    state.source_tag = source_tag

    position_block = ""
    try:
        from server.services._position import anonymous_position_block_for
        position_block = anonymous_position_block_for(state.ticker)
    except Exception:
        position_block = ""

    prompt = prompts.build_financials_prompt(
        state.company_name or state.ticker, scraped, yf_df,
        position_block=position_block,
    )
    async for event in _stream_claude_stage(state, "financials", prompt, thinking_mode="off"):
        if event["type"] != "_parsed":
            yield event
            continue
        fin = event["result"]
        if "score" not in fin:
            fin["score"] = 0
        fin["source_tag"] = source_tag
        fin["passed"] = int(fin.get("score") or 0) >= FIN_PASS_THRESHOLD_DEFAULT
        state.financials = fin
        state.stage_status["financials"] = "ready"
        yield {"type": "done", "result": fin}


async def stream_geo(state: AnalysisState) -> AsyncIterator[dict]:
    profile = state.overview or {}
    keywords = profile.get("risk_keywords") or [state.ticker, state.company_name, state.sector]
    queries = list(profile.get("news_search_queries") or [])
    # Always include company + ticker queries for Google News coverage
    for extra in (
        f"{state.company_name} {state.ticker}".strip(),
        state.ticker,
        f"{state.ticker} OR \"{state.company_name}\"" if state.company_name else "",
    ):
        if extra and extra not in queries:
            queries.append(extra)
    if not queries:
        queries = [state.ticker or state.company_name or "India markets"]

    articles, _errs = await asyncio.to_thread(data.fetch_stage2_articles, queries)
    monitor_alerts = await asyncio.to_thread(
        data.recent_geo_alerts_for_ticker, state.ticker or ""
    )
    # Prepend monitor alerts so the model sees live disruption signals first
    merged = list(monitor_alerts) + list(articles)
    articles_text = "\n".join(
        (
            f"---\n"
            f"{'[MONITOR ALERT '+str(a.get('severity',''))+'] ' if a.get('from_monitor') else ''}"
            f"{a.get('title','')}\n{a.get('content','')}"
        )
        for a in merged
    )
    if not articles_text.strip():
        articles_text = "(No recent Google News articles or monitor alerts found.)"

    macro = await asyncio.to_thread(data.build_macro_context)
    state.macro_snapshot = macro

    prompt = prompts.build_geo_prompt(
        state.company_name or state.ticker, state.ticker,
        state.sector, state.description,
        keywords, articles_text,
        threshold=GEO_FAIL_THRESHOLD_DEFAULT, profile=profile,
        market_context=macro,
    )
    async for event in _stream_claude_stage(state, "geo", prompt):
        if event["type"] != "_parsed":
            yield event
            continue
        geo = event["result"]
        try:
            risk_score = int(geo.get("risk_score", 100))
        except (TypeError, ValueError):
            risk_score = 100
        geo["risk_score"] = risk_score
        geo["passed"] = risk_score <= GEO_FAIL_THRESHOLD_DEFAULT
        geo["news_source"] = "google_news+monitor"
        geo["monitor_alert_count"] = len(monitor_alerts)
        state.geo = geo
        state.stage_status["geo"] = "ready"
        yield {"type": "done", "result": geo}


async def stream_technical(state: AnalysisState) -> AsyncIterator[dict]:
    ta = TechnicalAnalyst()
    try:
        candles, timeframe = await asyncio.to_thread(
            ta.fetch_candles, state.ticker, "3mo", "1d",
        )
    except Exception as e:
        msg = f"Failed to fetch candlestick data for {state.ticker}: {e}"
        state.stage_status["technical"] = "error"
        state.stage_errors["technical"] = msg
        yield {"type": "error", "message": msg, "raw": str(e)}
        return

    if not candles:
        msg = f"No candlestick data available for {state.ticker} from Kite or yfinance."
        state.stage_status["technical"] = "error"
        state.stage_errors["technical"] = msg
        yield {"type": "error", "message": msg}
        return

    stats = ta._compute_basic_stats(candles)
    display_candles = candles[-60:] if len(candles) > 60 else candles
    candle_table = "\n".join(c.to_str() for c in display_candles)

    prompt = prompts.build_tech_prompt(
        state.ticker, state.company_name or state.ticker,
        candle_table, stats, timeframe,
    )
    async for event in _stream_claude_stage(state, "technical", prompt):
        if event["type"] != "_parsed":
            yield event
            continue
        tech = event["result"]
        tech.setdefault("current_price", stats.get("current_price"))
        tech.setdefault("pct_change", stats.get("pct_change"))
        tech["timeframe"] = timeframe
        state.technical = tech
        state.stage_status["technical"] = "ready"
        yield {"type": "done", "result": tech}


async def stream_summary(state: AnalysisState) -> AsyncIterator[dict]:
    """Stage 5 — synthesizes verdict + bottom-line narrative.

    Calls Claude for the bottom line, with a templated fallback so this stage
    survives credit exhaustion even when the model is unreachable.
    """
    state.stage_status["summary"] = "loading"
    state.stage_errors.pop("summary", None)

    summary = build_summary_data(
        profile=state.overview or {},
        fin=state.financials or {},
        geo=state.geo or {},
        tech=state.technical or {},
        ticker=state.ticker,
        company_name=state.company_name,
        sector=state.sector,
    )
    verdict, verdict_reason = compute_verdict(
        fin=state.financials or {},
        geo=state.geo or {},
        tech=state.technical or {},
    )
    state.summary = summary
    state.verdict = verdict
    state.verdict_reason = verdict_reason

    # Bottom-line via Claude (one shot, no streaming UX needed) — fallback on
    # error so the verdict still ships.
    prompt = prompts.build_bottom_line_prompt(summary, verdict)
    full_text = ""
    err: dict | None = None
    async for event in stream_claude_events(
        prompt, thinking_mode="off", max_tokens=400,
    ):
        if event["type"] == "delta":
            yield event
        elif event["type"] == "done":
            full_text = event["text"]
        elif event["type"] == "error":
            err = event

    if err is not None or not full_text.strip():
        state.bottom_line = _fallback_bottom_line(summary, verdict)
    else:
        text = full_text.strip().strip('"').strip("'").strip()
        if text.lower().startswith("bottom line:"):
            text = text.split(":", 1)[1].strip()
        state.bottom_line = text

    state.stage_status["summary"] = "ready"

    # Persist to the SQLite history store exactly once, on summary success.
    # Best-effort: a write failure must not break the SSE response.
    history_id: int | None = None
    try:
        import analysis_history
        history_id = analysis_history.save_analysis(
            ticker=state.ticker,
            company_name=state.company_name,
            sector=state.sector,
            verdict=verdict,
            verdict_reason=verdict_reason,
            bottom_line=state.bottom_line,
            profile=state.overview or {},
            financials=state.financials or {},
            geo=state.geo or {},
            technical=state.technical or {},
            summary=summary,
            macro_snapshot=state.macro_snapshot,
            position_context=state.position_context,
        ) or None
    except Exception as e:
        log.warning("history save failed for %s: %s", state.ticker, e)

    yield {
        "type": "done",
        "result": {
            "summary": summary,
            "verdict": verdict,
            "verdict_reason": verdict_reason,
            "bottom_line": state.bottom_line,
            "history_id": history_id,
        },
    }


STAGE_STREAMERS = {
    "overview":   stream_overview,
    "financials": stream_financials,
    "geo":        stream_geo,
    "technical":  stream_technical,
    "summary":    stream_summary,
}

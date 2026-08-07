"""Pure prompt builders for the 5-stage analysis pipeline.

Lifted from `app.py` verbatim — these are pure string functions with zero
Streamlit dependencies. Keeping them in one place so prompt diffs stay
auditable.
"""
from __future__ import annotations


# ── Stage 1: Company intelligence profile ─────────────────────────────────────

def build_profile_prompt(ticker: str, yf_info: dict) -> str:
    name = yf_info.get("name", ticker)
    sector = yf_info.get("sector", "Unknown")
    industry = yf_info.get("industry", "Unknown")
    country = yf_info.get("country", "Unknown")
    desc = (yf_info.get("description") or "")[:600]

    return f"""You are a financial intelligence analyst. Build a comprehensive company intelligence profile for {name} ({ticker}).

KNOWN DATA:
- Sector: {sector}
- Industry: {industry}
- Country: {country}
- Business description: {desc}

Return ONLY valid JSON (no markdown, no extra text):
{{
  "company_name": "full legal name",
  "hq_country": "two-letter ISO 3166-1 alpha-2 country code for company HQ (e.g. US, IN, GB)",
  "sector": "specific sector",
  "business_model": "2 sentences: how they make money and who their customers are",
  "key_markets": ["top 3-5 geographic markets or customer segments by revenue importance"],
  "supply_chain": ["critical dependencies — real supplier names, manufacturing regions, raw materials"],
  "leadership": "1-2 sentences on the CEO and management team — reputation and track record",
  "key_products": ["top 3-5 revenue-driving products or services"],
  "risk_keywords": ["12-15 news monitoring keywords — include: specific geographies, key supplier names, regulators, competitors, macro themes like tariffs/interest rates/AI regulation"],
  "news_search_queries": ["3-4 specific search queries that would surface news materially relevant to this company — be precise and varied, not just the company name"],
  "geopolitical_exposures": ["specific geopolitical risks that could materially affect this company — be concrete"],
  "what_moves_the_stock": ["4-5 key catalysts or macro factors that are known to drive this stock's price"]
}}

Use your knowledge of this company. Be specific — cite real supplier names, real markets, real risk factors. Do not be generic."""


# ── Stage 2: Financial analysis ───────────────────────────────────────────────

def build_financials_prompt(
    company_name: str,
    scraped_text: str,
    yf_data: dict,
    position_block: str = "",
) -> str:
    def _fmt(label: str, data_dict: dict) -> str:
        if not data_dict:
            return f"### {label}\nNot available.\n"
        lines = [f"### {label}"]
        for period, metrics in list(data_dict.items())[:4]:
            lines.append(f"\n**{period}**")
            if isinstance(metrics, dict):
                for metric, value in list(metrics.items())[:25]:
                    if value is not None:
                        try:
                            lines.append(f"  {metric}: {float(value):,.0f}")
                        except (TypeError, ValueError):
                            lines.append(f"  {metric}: {value}")
        return "\n".join(lines)

    yf_sections = "\n\n".join([
        _fmt("Annual Income Statement",    yf_data.get("income_statement", {})),
        _fmt("Quarterly Income Statement", yf_data.get("quarterly_income_statement", {})),
        _fmt("Annual Balance Sheet",       yf_data.get("balance_sheet", {})),
        _fmt("Quarterly Balance Sheet",    yf_data.get("quarterly_balance_sheet", {})),
        _fmt("Annual Cash Flow",           yf_data.get("cash_flow", {})),
        _fmt("Quarterly Cash Flow",        yf_data.get("quarterly_cash_flow", {})),
    ])

    scraped_section = (
        f"## INVESTOR RELATIONS PAGE\n{scraped_text}\n"
        if scraped_text.strip()
        else "## INVESTOR RELATIONS PAGE\nNot available — analysis based on yfinance data only.\n"
    )

    return f"""You are a senior buy-side financial analyst. Produce a comprehensive financial analysis of {company_name}.

{scraped_section}

## YFINANCE FINANCIAL DATA
{yf_sections}

{position_block}

Analyze: revenue trajectory, profitability margins, earnings quality, balance sheet strength, cash flow, capital allocation, risks, forward outlook.

Return ONLY valid JSON (no markdown, no extra text):
{{
  "score": <integer 0-100>,
  "passed": <true if score >= 60>,
  "revenue_trend": {{"status": "GROWING"|"FLAT"|"DECLINING", "cagr_estimate": "...", "note": "one sentence with numbers"}},
  "profitability": {{"gross_margin": "...", "operating_margin": "...", "net_margin": "...", "trend": "EXPANDING"|"STABLE"|"COMPRESSING"}},
  "earnings_quality": {{"status": "HIGH"|"MEDIUM"|"LOW", "note": "one sentence"}},
  "balance_sheet": {{"debt_to_equity": "...", "current_ratio": "...", "net_debt": "...", "status": "HEALTHY"|"STRETCHED"|"OVERLEVERAGED"}},
  "cash_flow": {{"operating_cf": "...", "free_cf": "...", "capex": "...", "status": "STRONG"|"ADEQUATE"|"WEAK"|"NEGATIVE"}},
  "key_metrics": {{
    "revenue_ttm": "...", "net_income_ttm": "...", "eps_ttm": "...",
    "gross_margin_latest": "...", "operating_margin_latest": "...",
    "debt_to_equity": "...", "current_ratio": "...",
    "operating_cash_flow_ttm": "...", "free_cash_flow_ttm": "..."
  }},
  "green_flags": ["specific positive with numbers", "..."],
  "red_flags": ["specific concern with numbers", "..."],
  "summary": "3-4 sentence executive summary",
  "deep_analysis": {{
    "revenue_section": "2-3 paragraphs on revenue trajectory with numbers",
    "profitability_section": "2-3 paragraphs on margin structure",
    "balance_sheet_section": "2-3 paragraphs on debt and liquidity",
    "cash_flow_section": "2-3 paragraphs on cash generation",
    "risks_section": "2-3 paragraphs on key financial risks",
    "outlook_section": "1-2 paragraphs on forward trajectory"
  }},
  "recommendation": "one sentence investment-grade financial health assessment"
}}

Be specific. Quote actual numbers from the data. If a metric is unavailable, use null."""


# ── Stage 3: Geopolitical / macro risk ────────────────────────────────────────

def format_market_context_block(ctx: dict | None) -> str:
    """Render a live macro snapshot for inclusion in the geo prompt."""
    if not ctx:
        return ""
    indices = ctx.get("indices") or {}
    currency = ctx.get("currency") or {}
    state = ctx.get("market_state") or ""
    nice = {
        "NIFTY_50": "NIFTY 50", "BANK_NIFTY": "BANK NIFTY",
        "NIFTY_IT": "NIFTY IT", "NIFTY_PHARMA": "NIFTY PHARMA",
        "NIFTY_AUTO": "NIFTY AUTO", "NIFTY_METAL": "NIFTY METAL",
        "NIFTY_FMCG": "NIFTY FMCG", "NIFTY_ENERGY": "NIFTY ENERGY",
        "USDINR": "USD/INR",
    }

    def fmt_row(label: str, d: dict) -> str:
        ltp = d.get("ltp") or 0
        pct = d.get("change_pct")
        if pct is None:
            return f"  - {label}: {ltp:,.2f}"
        arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "·")
        return f"  - {label}: {ltp:,.2f}  ({arrow}{abs(pct):.2f}%)"

    lines: list[str] = []
    for k, d in indices.items():
        lines.append(fmt_row(nice.get(k, k), d))
    for k, d in currency.items():
        lines.append(fmt_row(nice.get(k, k), d))
    if not lines:
        return ""
    header = "\n## CURRENT INDIAN MARKET SNAPSHOT (live via Kite Connect)\n"
    if state:
        header += f"Market state: {state}\n"
    return header + "\n".join(lines) + "\n"


def build_geo_prompt(
    company: str,
    ticker: str,
    sector: str,
    description: str,
    keywords: list[str],
    articles_text: str,
    threshold: int = 55,
    profile: dict | None = None,
    concerns: str = "",
    market_context: dict | None = None,
) -> str:
    kw_str = ", ".join(keywords) if keywords else "auto-detected from company description"
    extra = (
        f"\n\nSPECIFIC RISKS THE TRADER IS CONCERNED ABOUT:\n{concerns}"
        if concerns and concerns.strip() else ""
    )

    profile_context = ""
    if profile:
        exposures = "\n".join(f"  - {e}" for e in profile.get("geopolitical_exposures", []))
        supply = "\n".join(f"  - {s}" for s in profile.get("supply_chain", []))
        movers = "\n".join(f"  - {m}" for m in profile.get("what_moves_the_stock", []))
        markets = ", ".join(profile.get("key_markets", []))
        profile_context = f"""
COMPANY INTELLIGENCE PROFILE:
- Business model: {profile.get("business_model", "")}
- Key markets: {markets}
- Supply chain dependencies:
{supply}
- Known geopolitical exposures:
{exposures}
- What moves this stock:
{movers}
"""

    market_block = format_market_context_block(market_context)

    return f"""You are a geopolitical and macro risk analyst for equity investors.

COMPANY: {company} ({ticker})
SECTOR: {sector}
DESCRIPTION: {description}
KEY EXPOSURE KEYWORDS: {kw_str}{profile_context}{market_block}{extra}

Your job: using the company intelligence profile above, determine if this company faces
COLLATERAL RISK from the news articles below — even if the company is NOT directly mentioned.
Cross-reference each article against the company's known supply chain, key markets,
geopolitical exposures, and stock-moving factors.
Look for: supply chain disruptions, tariffs, sanctions, sector-wide pressure,
currency risk, consumer sentiment shifts, regulatory changes, geographic conflict.

## NEWS ARTICLES
{articles_text}

Return ONLY valid JSON (no markdown, no extra text):
{{
  "risk_score": <integer 0-100, 0=no risk, 100=extremely high risk>,
  "risk_level": "LOW"|"MEDIUM"|"HIGH"|"CRITICAL",
  "collateral_risks": ["specific risk: how this news could hurt the company", ...],
  "safe_signals": ["specific reason this news does NOT affect the company", ...],
  "sector_exposure": "one sentence",
  "geographic_exposure": "one sentence",
  "supply_chain_risk": "one sentence",
  "regulatory_risk": "one sentence",
  "consumer_sentiment_risk": "one sentence",
  "relevant_articles": [
    {{"title": "...", "source": "...", "relevance": "HIGH"|"MEDIUM"|"LOW", "impact": "NEGATIVE"|"POSITIVE"|"NEUTRAL", "explanation": "..."}}
  ],
  "summary": "2-3 sentence executive summary",
  "recommendation": "one sentence: does this pass the risk screen (threshold: risk_score <= {threshold}) and why"
}}"""


# ── Stage 4: Technical analysis ───────────────────────────────────────────────

def build_tech_prompt(
    ticker: str,
    company: str,
    candle_table: str,
    stats: dict,
    timeframe: str,
    trading_style: str = "Let AI Decide",
) -> str:
    style_hint = ""
    if trading_style != "Let AI Decide":
        style_hint = (
            f"\nThe trader prefers {trading_style}. Weight your analysis "
            "toward setups that suit this style.\n"
        )
    avg_vol = stats.get("avg_volume", "N/A")
    avg_vol_str = f"{avg_vol:,}" if isinstance(avg_vol, (int, float)) else str(avg_vol)

    return f"""You are a professional technical analyst covering Indian-listed equities (NSE / BSE). Trading session is 09:15–15:30 IST; no extended-hours session beyond the 09:00–09:15 IST pre-open auction. All prices are in Indian rupees (₹).

## STOCK: {company} ({ticker})
## TIMEFRAME: {timeframe}{style_hint}

## KEY STATISTICS
- Current Price: ₹{stats.get('current_price', 'N/A')}
- Period Performance: {stats.get('pct_change', 'N/A')}% ({stats.get('period_start_price', '?')} → {stats.get('current_price', '?')})
- Period Range: ₹{stats.get('period_low', '?')} — ₹{stats.get('period_high', '?')}
- SMA(10): ₹{stats.get('sma_10', 'N/A')}, SMA(20): ₹{stats.get('sma_20', 'N/A')}, SMA(50): ₹{stats.get('sma_50', 'N/A')}
- Avg Volume: {avg_vol_str}, Latest Vol vs Avg: {stats.get('volume_vs_avg_pct', 'N/A')}%
- Daily Volatility: {stats.get('daily_volatility_pct', 'N/A')}%

## CANDLESTICK DATA (Date | ▲/▼ | O H L C | Volume)
{candle_table}

Return ONLY valid JSON (no markdown, no extra text):
{{
  "recommendation": "DAY_TRADING"|"SWING_TRADING"|"LONG_TERM"|"AVOID",
  "confidence": <integer 0-100>,
  "trend": "UPTREND"|"DOWNTREND"|"SIDEWAYS"|"REVERSAL_BULLISH"|"REVERSAL_BEARISH",
  "patterns_found": ["Pattern name on date: significance", ...],
  "support_levels": [<price>, <price>],
  "resistance_levels": [<price>, <price>],
  "entry_point": "specific price or condition",
  "target_price": "price target with brief reasoning",
  "stop_loss": "stop loss level with brief reasoning",
  "risk_reward_ratio": "e.g. 1:2.5",
  "volume_analysis": "one sentence",
  "momentum": "one sentence",
  "summary": "2-3 sentence summary of technical picture",
  "trading_notes": ["specific note for the trader", ...]
}}"""


# ── Stage 5: Bottom-line narrative ────────────────────────────────────────────

def build_bottom_line_prompt(summary: dict, verdict: str) -> str:
    company = summary.get("company", {})
    fin = summary.get("financial", {})
    geo = summary.get("geo", {})
    tech = summary.get("technical", {})

    def _flat(items, n=3):
        return "; ".join(str(x) for x in (items or [])[:n] if x) or "none"

    return f"""You are a senior equity analyst writing the closing line of an investment note.

Company: {company.get('name')} ({company.get('ticker')}, {company.get('exchange')}, {company.get('country')})
Sector:  {company.get('sector')}

Stage 1 Financial:    {fin.get('score', 0)}/100 ({'PASS' if fin.get('passed') else 'FAIL'})
  Top green flags: {_flat(fin.get('green_flags'))}
  Top red flags:   {_flat(fin.get('red_flags'))}
  Revenue: {fin.get('revenue_status')}, Margins: {fin.get('margin_trend')}, CF: {fin.get('cash_flow_status')}

Stage 2 Geopolitical: {geo.get('risk_score', 0)}/100 ({geo.get('risk_level')}, {'PASS' if geo.get('passed') else 'FAIL'})
  Top collateral risks: {_flat(geo.get('collateral_risks'))}
  Top safe signals:     {_flat(geo.get('safe_signals'))}

Stage 3 Technical:    {tech.get('recommendation')} (confidence {tech.get('confidence')}%)
  Trend: {tech.get('trend')}; Entry {tech.get('entry_point')}, Target {tech.get('target_price')}, Stop {tech.get('stop_loss')}, R/R {tech.get('risk_reward_ratio')}

Final verdict: {verdict}

Write a 2 to 3 sentence "Bottom Line" suitable to close the report. Tie the
fundamentals, macro/geopolitical posture, and technical setup together. Be
specific — quote one or two numbers — and end with what the trader should
actually do (enter, wait for setup, avoid). Do NOT use markdown, headers, or
bullet points. Plain prose only. Max 80 words."""

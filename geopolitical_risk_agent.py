"""
Stage 2: Geopolitical & News Risk Analysis Agent
- Takes news articles (from newspapers / feeds)
- Evaluates if the company could take collateral fire from world events
- Matches news to the company's target keywords, sector, geography, audience
- Returns pass/fail with a risk level
"""

import anthropic
import json
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GeopoliticalRiskResult:
    passed: bool
    risk_level: str                     # "LOW", "MEDIUM", "HIGH", "CRITICAL"
    risk_score: int                     # 0-100 (higher = more risky)
    collateral_risks: list[str] = field(default_factory=list)
    safe_signals: list[str] = field(default_factory=list)
    relevant_articles: list[dict] = field(default_factory=list)
    sector_exposure: str = ""
    geographic_exposure: str = ""
    supply_chain_risk: str = ""
    regulatory_risk: str = ""
    consumer_sentiment_risk: str = ""
    summary: str = ""
    recommendation: str = ""
    raw_response: str = ""

    def to_dict(self) -> dict:
        return {
            "stage": "Geopolitical & News Risk",
            "passed": self.passed,
            "risk_level": self.risk_level,
            "risk_score": self.risk_score,
            "collateral_risks": self.collateral_risks,
            "safe_signals": self.safe_signals,
            "relevant_articles": self.relevant_articles,
            "sector_exposure": self.sector_exposure,
            "geographic_exposure": self.geographic_exposure,
            "supply_chain_risk": self.supply_chain_risk,
            "regulatory_risk": self.regulatory_risk,
            "consumer_sentiment_risk": self.consumer_sentiment_risk,
            "summary": self.summary,
            "recommendation": self.recommendation,
        }


class GeopoliticalRiskAgent:
    """
    AI agent that analyzes news articles and world events to determine
    whether an Indian-listed company faces collateral risk — even if it is
    not directly named.

    The key insight: a company doesn't have to be in the news to be affected.
    An IT services exporter suffers when US Fed signals slower spend.
    A bank suffers when RBI shifts the repo rate or sectoral risk weights.
    A chemicals exporter suffers when the rupee strengthens or oil spikes.
    A consumer-staples company suffers when monsoon disappoints rural India.
    """

    # Score above this = HIGH/CRITICAL risk, don't trade
    FAIL_THRESHOLD = 55

    def __init__(self, fail_threshold: int = None):
        self.client = anthropic.Anthropic()
        if fail_threshold is not None:
            self.FAIL_THRESHOLD = fail_threshold

    def _build_company_profile(
        self,
        company: str,
        ticker: str,
        sector: str,
        target_keywords: list[str],
        company_description: str,
    ) -> str:
        keywords_str = ", ".join(target_keywords) if target_keywords else "not specified"
        return f"""COMPANY PROFILE
- Name: {company} ({ticker})
- Sector: {sector}
- Target audience / key markets: {keywords_str}
- Description: {company_description}"""

    @staticmethod
    def _format_market_context(ctx: dict | None) -> str:
        """Render the live macro snapshot as a prompt block. Empty when ctx falsy."""
        if not ctx:
            return ""
        indices = ctx.get("indices") or {}
        currency = ctx.get("currency") or {}
        state = ctx.get("market_state") or ""
        lines: list[str] = []
        # Friendly labels for the model
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
                return f"- {label}: {ltp:,.2f}"
            arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "·")
            return f"- {label}: {ltp:,.2f}  ({arrow}{abs(pct):.2f}%)"
        for key, data in indices.items():
            lines.append(fmt_row(nice.get(key, key), data))
        for key, data in currency.items():
            lines.append(fmt_row(nice.get(key, key), data))
        if not lines:
            return ""
        header = "## CURRENT INDIAN MARKET SNAPSHOT (live, from Kite Connect)\n"
        if state:
            header += f"Market state: {state}\n"
        return header + "\n".join(lines) + "\n"

    def _build_prompt(
        self,
        company_profile: str,
        news_articles: list[dict],
        market_context: dict | None = None,
    ) -> str:
        articles_str = ""
        for i, article in enumerate(news_articles, 1):
            title = article.get("title", "No title")
            source = article.get("source", "Unknown")
            content = article.get("content", article.get("summary", ""))
            articles_str += f"\n--- Article {i} [{source}] ---\n{title}\n{content}\n"

        market_block = self._format_market_context(market_context)

        return f"""You are an expert macro and geopolitical risk analyst covering **Indian equities listed on NSE/BSE**.

Your job is NOT to check if a company is directly mentioned in the news.
Your job is to analyze whether world and Indian domestic events could INDIRECTLY
harm or help the company through risk dimensions that matter for Indian listed
businesses specifically:

- **Domestic monetary / fiscal policy:** RBI repo rate decisions, CRR/SLR, MPC
  commentary, fiscal deficit trajectory, Union Budget impact, GST changes.
- **Capital flows:** FII (foreign institutional investor) buying/selling, DII
  flows, MSCI / FTSE index re-weighting, Nifty / Sensex sentiment.
- **Currency:** INR vs USD/EUR/CNY moves — exporters benefit from a weaker INR,
  importers and dollar-debt issuers suffer.
- **Commodity prices:** crude oil (India imports ~85% of its oil — OMCs, paints,
  airlines, logistics directly affected), gold, edible oil, steel, copper.
- **Regulatory & political:** SEBI rulings/circulars, MoEF/CCI/CCI scrutiny,
  state vs union elections, central-government policy (PLI schemes, China+1,
  semiconductor mission), bilateral trade deals.
- **Domestic demand drivers:** monsoon / rainfall (rural FMCG, two-wheelers,
  tractors, fertilisers), urban consumer confidence, festival-season demand.
- **External shocks:** US Fed policy and dollar strength (EM flows), Chinese
  growth / China+1 supply-chain shifts, Middle East oil disruption,
  Taiwan-related semiconductor supply, EU and UK economic health for IT
  services, Russia-Ukraine impact on commodities and remittances.
- **Sector-specific:** banking NPA cycle, IT client-spend cycles, pharma USFDA
  inspections, auto chip supply, real-estate rates, telecom AGR rulings.

{company_profile}

{market_block}
## NEWS ARTICLES TO ANALYZE
{articles_str}

## YOUR TASK
Evaluate the COLLATERAL RISK to this Indian-listed company from the news above.
Return a JSON object with EXACTLY this structure (no extra text, just the JSON):
{{
  "risk_score": <integer 0-100, where 0=no risk and 100=extremely high risk>,
  "risk_level": "LOW" | "MEDIUM" | "HIGH" | "CRITICAL",
  "collateral_risks": [
    "Specific risk: how this news event could hurt the company — be concrete",
    ...
  ],
  "safe_signals": [
    "Specific reason this news does NOT affect the company or may even help it",
    ...
  ],
  "relevant_articles": [
    {{
      "title": "...",
      "source": "...",
      "relevance": "HIGH" | "MEDIUM" | "LOW",
      "impact": "NEGATIVE" | "POSITIVE" | "NEUTRAL",
      "explanation": "Why this article matters for this company"
    }},
    ...
  ],
  "sector_exposure": "<assessment of sector-level risk from current news>",
  "geographic_exposure": "<assessment of geographic risk from current news>",
  "supply_chain_risk": "<assessment of supply chain risk from current news>",
  "regulatory_risk": "<assessment of regulatory/policy risk from current news>",
  "consumer_sentiment_risk": "<assessment of consumer or B2B sentiment risk>",
  "summary": "<2-3 sentence executive summary of the geopolitical risk picture>",
  "recommendation": "<1 sentence: does this company pass or fail the geopolitical risk screen, and why>"
}}

Risk thresholds:
- LOW (0-30): Company is largely insulated from current news events
- MEDIUM (31-54): Some exposure but manageable, proceed with caution
- HIGH (55-74): Significant collateral risk, trading may be inadvisable
- CRITICAL (75-100): Company is directly in the blast radius of current events

Be specific. Connect the dots between world / Indian-domestic events and this company's business model. Quote ₹ amounts where the article cites them. Reference the Indian fiscal year (FY25 = Apr 2024 – Mar 2025) when discussing forward periods."""

    def analyze(
        self,
        company: str,
        ticker: str,
        sector: str,
        target_keywords: list[str],
        company_description: str,
        news_articles: list[dict],
        market_context: dict | None = None,
        verbose: bool = True,
    ) -> GeopoliticalRiskResult:
        """
        Run the geopolitical risk analysis agent.

        Args:
            company:             Company name
            ticker:              Stock ticker
            sector:              Industry sector (e.g., "Semiconductors", "Consumer Electronics")
            target_keywords:     Words describing the company's audience/geography/products
                                 e.g., ["China", "US consumers", "TSMC", "AI chips", "Taiwan"]
            company_description: Brief description of what the company does and who it sells to
            news_articles:       List of dicts with keys: title, source, content/summary
            verbose:             Print progress and results
        """
        if verbose:
            print(f"\n{'='*65}")
            print(f"  STAGE 2: GEOPOLITICAL & NEWS RISK — {ticker}")
            print(f"{'='*65}")
            print(f"  Articles analyzed: {len(news_articles)}")
            print(f"  Target keywords:   {', '.join(target_keywords[:5])}")
            print(f"  Fail threshold:    risk_score > {self.FAIL_THRESHOLD}")
            print(f"  Analyzing with Claude claude-opus-4-6 (adaptive thinking)...")
            print()

        company_profile = self._build_company_profile(
            company, ticker, sector, target_keywords, company_description
        )
        prompt = self._build_prompt(company_profile, news_articles,
                                    market_context=market_context)

        full_response = ""
        try:
            with self.client.messages.stream(
                model="claude-opus-4-6",
                max_tokens=8000,
                thinking={"type": "adaptive"},
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                for event in stream:
                    if (
                        hasattr(event, "type")
                        and event.type == "content_block_delta"
                        and hasattr(event, "delta")
                    ):
                        delta = event.delta
                        if hasattr(delta, "type") and delta.type == "text_delta":
                            full_response += delta.text
                            if verbose:
                                print(delta.text, end="", flush=True)

            if verbose:
                print()

        except Exception as e:
            if verbose:
                print(f"\n  [Error during streaming: {e}]")
            return GeopoliticalRiskResult(
                passed=False,
                risk_level="UNKNOWN",
                risk_score=100,
                summary=f"Analysis failed: {e}",
                recommendation="Unable to analyze — check API key and news data.",
                raw_response="",
            )

        result = self._parse_response(full_response, verbose)
        return result

    def _parse_response(self, raw: str, verbose: bool) -> GeopoliticalRiskResult:
        raw = raw.strip()
        start = raw.find("{")
        end = raw.rfind("}") + 1

        if start == -1 or end == 0:
            if verbose:
                print("  [Warning: Could not find JSON in response]")
            return GeopoliticalRiskResult(
                passed=False,
                risk_level="UNKNOWN",
                risk_score=100,
                summary="Failed to parse analysis response.",
                recommendation="Manual review required.",
                raw_response=raw,
            )

        try:
            data = json.loads(raw[start:end])
        except json.JSONDecodeError as e:
            if verbose:
                print(f"  [Warning: JSON parse error: {e}]")
            return GeopoliticalRiskResult(
                passed=False,
                risk_level="UNKNOWN",
                risk_score=100,
                summary="Failed to parse JSON response.",
                recommendation="Manual review required.",
                raw_response=raw,
            )

        risk_score = int(data.get("risk_score", 100))
        risk_level = data.get("risk_level", "HIGH")
        passed = risk_score <= self.FAIL_THRESHOLD

        result = GeopoliticalRiskResult(
            passed=passed,
            risk_level=risk_level,
            risk_score=risk_score,
            collateral_risks=data.get("collateral_risks", []),
            safe_signals=data.get("safe_signals", []),
            relevant_articles=data.get("relevant_articles", []),
            sector_exposure=data.get("sector_exposure", ""),
            geographic_exposure=data.get("geographic_exposure", ""),
            supply_chain_risk=data.get("supply_chain_risk", ""),
            regulatory_risk=data.get("regulatory_risk", ""),
            consumer_sentiment_risk=data.get("consumer_sentiment_risk", ""),
            summary=data.get("summary", ""),
            recommendation=data.get("recommendation", ""),
            raw_response=raw,
        )

        if verbose:
            self._print_results(result)

        return result

    def _print_results(self, result: GeopoliticalRiskResult):
        risk_icons = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🟠", "CRITICAL": "🔴", "UNKNOWN": "⚪"}
        icon = risk_icons.get(result.risk_level, "⚪")
        passed_label = "PASSED" if result.passed else "FAILED"
        verdict_icon = "✅" if result.passed else "❌"

        print(f"\n{'-'*65}")
        print(f"  RISK SCORE: {result.risk_score}/100  {icon} {result.risk_level}  {verdict_icon} {passed_label}")
        print(f"{'-'*65}")

        if result.collateral_risks:
            print(f"\n  COLLATERAL RISKS ({len(result.collateral_risks)}):")
            for risk in result.collateral_risks:
                print(f"    ! {risk}")

        if result.safe_signals:
            print(f"\n  SAFE SIGNALS ({len(result.safe_signals)}):")
            for signal in result.safe_signals:
                print(f"    + {signal}")

        if result.sector_exposure:
            print(f"\n  SECTOR EXPOSURE:    {result.sector_exposure}")
        if result.geographic_exposure:
            print(f"  GEOGRAPHIC EXPOSURE: {result.geographic_exposure}")
        if result.supply_chain_risk:
            print(f"  SUPPLY CHAIN RISK:   {result.supply_chain_risk}")

        if result.summary:
            print(f"\n  SUMMARY: {result.summary}")

        if result.recommendation:
            print(f"\n  RECOMMENDATION: {result.recommendation}")

        verdict = "PROCEED to Stage 3" if result.passed else "PIPELINE STOPPED — Geopolitical risk too high"
        print(f"\n  VERDICT: {verdict}")
        print(f"{'='*65}\n")


# ── Demo / standalone test ────────────────────────────────────────────────────
if __name__ == "__main__":
    SAMPLE_NEWS = [
        {
            "title": "RBI Holds Repo Rate at 6.5%, Signals Caution on Inflation",
            "source": "Economic Times",
            "content": "The Reserve Bank of India's MPC kept the repo rate unchanged at 6.5% for "
                       "the eleventh consecutive meeting, citing sticky food inflation. Bank Nifty "
                       "ended flat as analysts pushed expected rate cuts to Q4 FY25. Lower-for-longer "
                       "rates compress NIMs for banks while delaying treasury gains.",
        },
        {
            "title": "FIIs Sell ₹22,000 Cr in Indian Equities in October as Dollar Strengthens",
            "source": "Mint",
            "content": "Foreign portfolio investors withdrew ₹22,000 crore from Indian equities in "
                       "October, the heaviest monthly outflow in two years, as the US dollar index "
                       "rose past 105. Banks and large-caps bore the brunt of the selling. DII "
                       "purchases of ₹18,500 Cr partially offset the impact.",
        },
        {
            "title": "Indian Banks Asked to Raise Risk Weights on Unsecured Personal Loans",
            "source": "Business Standard",
            "content": "RBI directed lenders to assign higher risk weights of 125% to unsecured "
                       "personal loans and credit-card receivables, citing rapid growth and rising "
                       "delinquencies. Several private banks said the move would compress capital "
                       "ratios by 50–80 bps.",
        },
        {
            "title": "Monsoon Surplus Boosts Rural Demand Outlook for FY25",
            "source": "Livemint",
            "content": "India recorded a 6% above-normal southwest monsoon, the best in five years. "
                       "Rural lending demand and agri-input sales are expected to rise, supporting "
                       "private sector banks with strong rural footprints.",
        },
    ]

    agent = GeopoliticalRiskAgent()
    result = agent.analyze(
        company="HDFC Bank Ltd",
        ticker="HDFCBANK",
        sector="Banking / Private-Sector Lender",
        target_keywords=[
            "RBI repo rate", "FII flows", "Bank Nifty", "NPAs", "credit growth",
            "unsecured retail", "NIM", "rural India", "monsoon", "MSCI weight",
        ],
        company_description=(
            "HDFC Bank is India's largest private-sector bank by assets and market cap, listed on "
            "NSE/BSE. Post-merger with HDFC Ltd it has a large mortgage book alongside corporate, "
            "SME and retail lending. NIMs are sensitive to RBI policy; deposit franchise and rural "
            "expansion drive growth; FII ownership is among the highest on NSE."
        ),
        news_articles=SAMPLE_NEWS,
    )

    print(f"\nFinal result dict:\n{json.dumps(result.to_dict(), indent=2)}")

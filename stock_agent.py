"""
STOCKIFY — Personal AI Stock Analysis Agent
============================================
Three-stage gated pipeline powered by Claude claude-opus-4-6:

  Stage 1: Financial Report Analysis
           → Evaluates earnings/financials against your checklist
           → Must PASS (score ≥ threshold) to continue

  Stage 2: Geopolitical & News Risk Analysis
           → Scans newspapers for collateral risks to the company
           → Must PASS (risk score ≤ threshold) to continue

  Stage 3: Technical Analysis
           → Reads candlestick data and identifies patterns
           → Outputs trading recommendation: Day / Swing / Long-Term / Avoid

Each stage gates the next. If a company fails Stage 1, Stages 2 & 3 are skipped.

Usage
-----
  python stock_agent.py

  Or import and use programmatically:

    from stock_agent import StockAgent, CompanyInput
    agent = StockAgent()
    report = agent.run(company_input)
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from financial_analyst import FinancialAnalyst, FinancialAnalysisResult, DEFAULT_CHECKLIST
from geopolitical_risk_agent import GeopoliticalRiskAgent, GeopoliticalRiskResult
from technical_analyst import TechnicalAnalyst, TechnicalAnalysisResult, CandleStick

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False


# ── Input / Config dataclasses ─────────────────────────────────────────────────

@dataclass
class CompanyInput:
    """All the information needed to run a full analysis on one company."""

    # Basic info
    ticker: str
    company_name: str
    sector: str
    description: str                # What the company does and who it sells to

    # Stage 1 — Financial Report
    financial_report: str           # Paste the earnings text, 10-K summary, etc.

    # Stage 2 — Geopolitical Risk
    news_articles: list[dict]       # Each dict: {title, source, content/summary}
    target_keywords: list[str]      # Words describing company's markets/audience/supply chain

    # Stage 3 — Technical Analysis (optional — uses yfinance by default)
    candles: Optional[list[CandleStick]] = None   # Provide manually if no internet
    chart_period: str = "3mo"       # yfinance period: '1mo', '3mo', '6mo', '1y'
    chart_interval: str = "1d"      # yfinance interval: '1d', '1wk', '1h'

    # Pipeline config
    financial_checklist: list[str] = field(default_factory=lambda: DEFAULT_CHECKLIST)
    financial_pass_threshold: int = 60   # score / 100
    geo_fail_threshold: int = 55         # risk_score / 100 (above = fail)


@dataclass
class PipelineReport:
    """Final output of the full three-stage pipeline."""
    ticker: str
    company_name: str
    timestamp: str
    overall_verdict: str                # "BUY (Swing)" | "BUY (Long-Term)" | "BUY (Day)" | "AVOID" | "INCOMPLETE"
    stages_completed: int               # 0, 1, 2, or 3
    stage1_result: Optional[FinancialAnalysisResult] = None
    stage2_result: Optional[GeopoliticalRiskResult] = None
    stage3_result: Optional[TechnicalAnalysisResult] = None
    stop_reason: str = ""               # Why the pipeline stopped early (if it did)

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "company_name": self.company_name,
            "timestamp": self.timestamp,
            "overall_verdict": self.overall_verdict,
            "stages_completed": self.stages_completed,
            "stop_reason": self.stop_reason,
            "stage_1_financial": self.stage1_result.to_dict() if self.stage1_result else None,
            "stage_2_geopolitical": self.stage2_result.to_dict() if self.stage2_result else None,
            "stage_3_technical": self.stage3_result.to_dict() if self.stage3_result else None,
        }

    def save(self, filepath: str = None):
        if not filepath:
            safe_ticker = self.ticker.replace("/", "_")
            filepath = f"report_{safe_ticker}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(filepath, "w") as f:
            json.dump(self.to_dict(), f, indent=2, default=str)
        print(f"\n  Report saved to: {filepath}")
        return filepath


# ── Main Agent ─────────────────────────────────────────────────────────────────

class StockAgent:
    """
    Orchestrates the three-stage AI analysis pipeline.
    Each stage gates the next — a failure stops the pipeline immediately.
    """

    def __init__(self):
        self.financial_analyst = FinancialAnalyst()
        self.geo_agent = GeopoliticalRiskAgent()
        self.tech_analyst = TechnicalAnalyst()

    def run(
        self,
        inp: CompanyInput,
        verbose: bool = True,
        save_report: bool = False,
        report_path: str = None,
    ) -> PipelineReport:
        """
        Run the full three-stage analysis pipeline.
        Returns a PipelineReport with all results.
        """
        timestamp = datetime.now().isoformat()
        report = PipelineReport(
            ticker=inp.ticker,
            company_name=inp.company_name,
            timestamp=timestamp,
            overall_verdict="INCOMPLETE",
            stages_completed=0,
        )

        if verbose:
            self._print_header(inp)

        # ── STAGE 1: FINANCIAL ANALYSIS ────────────────────────────────────────
        self.financial_analyst.checklist = inp.financial_checklist
        self.financial_analyst.PASS_THRESHOLD = inp.financial_pass_threshold

        stage1 = self.financial_analyst.analyze(
            company=inp.company_name,
            ticker=inp.ticker,
            report_text=inp.financial_report,
            verbose=verbose,
        )
        report.stage1_result = stage1
        report.stages_completed = 1

        if not stage1.passed:
            report.overall_verdict = "AVOID"
            report.stop_reason = (
                f"Failed financial screening (score {stage1.score}/100, "
                f"threshold {inp.financial_pass_threshold}/100). "
                f"Red flags: {'; '.join(stage1.red_flags[:2])}"
            )
            if verbose:
                self._print_pipeline_stop(report.stop_reason)
            if save_report:
                report.save(report_path)
            return report

        # ── STAGE 2: GEOPOLITICAL RISK ─────────────────────────────────────────
        self.geo_agent.FAIL_THRESHOLD = inp.geo_fail_threshold

        stage2 = self.geo_agent.analyze(
            company=inp.company_name,
            ticker=inp.ticker,
            sector=inp.sector,
            target_keywords=inp.target_keywords,
            company_description=inp.description,
            news_articles=inp.news_articles,
            verbose=verbose,
        )
        report.stage2_result = stage2
        report.stages_completed = 2

        if not stage2.passed:
            report.overall_verdict = "AVOID"
            report.stop_reason = (
                f"Failed geopolitical risk screen (risk score {stage2.risk_score}/100, "
                f"level: {stage2.risk_level}, threshold: {inp.geo_fail_threshold}). "
                f"Top risk: {stage2.collateral_risks[0] if stage2.collateral_risks else 'see analysis'}"
            )
            if verbose:
                self._print_pipeline_stop(report.stop_reason)
            if save_report:
                report.save(report_path)
            return report

        # ── STAGE 3: TECHNICAL ANALYSIS ────────────────────────────────────────
        if inp.candles:
            # Use manually provided candle data
            stage3 = self.tech_analyst.analyze(
                ticker=inp.ticker,
                company=inp.company_name,
                candles=inp.candles,
                timeframe="custom data",
                verbose=verbose,
            )
        elif YFINANCE_AVAILABLE:
            # Fetch live data via yfinance
            try:
                stage3 = self.tech_analyst.analyze_ticker(
                    ticker=inp.ticker,
                    company=inp.company_name,
                    period=inp.chart_period,
                    interval=inp.chart_interval,
                    verbose=verbose,
                )
            except Exception as e:
                if verbose:
                    print(f"\n  [Stage 3 Error: {e}]")
                    print("  Could not fetch chart data. Install yfinance: pip install yfinance")
                report.overall_verdict = "INCOMPLETE"
                report.stop_reason = f"Stage 3 failed to fetch chart data: {e}"
                report.stages_completed = 2
                if save_report:
                    report.save(report_path)
                return report
        else:
            if verbose:
                print("\n  [Stage 3 skipped — yfinance not installed and no manual candles provided]")
                print("  Install yfinance: pip install yfinance")
            report.overall_verdict = "INCOMPLETE"
            report.stop_reason = "Stage 3 skipped — yfinance not available"
            if save_report:
                report.save(report_path)
            return report

        report.stage3_result = stage3
        report.stages_completed = 3

        # ── FINAL VERDICT ──────────────────────────────────────────────────────
        rec = stage3.recommendation
        if rec == "AVOID":
            verdict = "AVOID"
        elif rec == "DAY_TRADING":
            verdict = "BUY (Day Trading)"
        elif rec == "SWING_TRADING":
            verdict = "BUY (Swing Trading)"
        elif rec == "LONG_TERM":
            verdict = "BUY (Long-Term Investment)"
        else:
            verdict = "REVIEW"

        report.overall_verdict = verdict

        if verbose:
            self._print_final_verdict(report)

        if save_report:
            report.save(report_path)

        return report

    # ── Display helpers ────────────────────────────────────────────────────────

    def _print_header(self, inp: CompanyInput):
        print("\n" + "█" * 65)
        print(f"  STOCKIFY — AI STOCK ANALYSIS AGENT")
        print(f"  {inp.company_name} ({inp.ticker}) | {inp.sector}")
        print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("█" * 65)
        print("""
  Pipeline:
    [1] Financial Report Analysis  →  Red/green flag checklist
    [2] Geopolitical Risk Analysis →  News collateral risk screen
    [3] Technical Analysis         →  Candlestick patterns + trade rec
""")

    def _print_pipeline_stop(self, reason: str):
        print("\n" + "!" * 65)
        print("  PIPELINE STOPPED EARLY")
        print(f"  Reason: {reason}")
        print("  FINAL VERDICT: AVOID")
        print("!" * 65 + "\n")

    def _print_final_verdict(self, report: PipelineReport):
        verdict_icons = {
            "BUY (Day Trading)": "⚡",
            "BUY (Swing Trading)": "📊",
            "BUY (Long-Term Investment)": "🏦",
            "AVOID": "🚫",
            "REVIEW": "🔍",
            "INCOMPLETE": "⚠️",
        }
        icon = verdict_icons.get(report.overall_verdict, "?")

        print("\n" + "█" * 65)
        print(f"  STOCKIFY — FINAL REPORT: {report.ticker}")
        print("█" * 65)

        s1 = report.stage1_result
        s2 = report.stage2_result
        s3 = report.stage3_result

        if s1:
            s1_icon = "✅" if s1.passed else "❌"
            print(f"  Stage 1 Financial:    {s1_icon} {s1.score}/100")

        if s2:
            s2_icon = "✅" if s2.passed else "❌"
            print(f"  Stage 2 Geo Risk:     {s2_icon} {s2.risk_score}/100 ({s2.risk_level})")

        if s3:
            print(f"  Stage 3 Technical:    {s3.recommendation} (confidence: {s3.confidence}%)")
            if s3.entry_point:
                print(f"    Entry:    {s3.entry_point}")
            if s3.target_price:
                print(f"    Target:   {s3.target_price}")
            if s3.stop_loss:
                print(f"    Stop:     {s3.stop_loss}")
            if s3.risk_reward_ratio:
                print(f"    R/R:      {s3.risk_reward_ratio}")

        print(f"\n  {icon} VERDICT: {report.overall_verdict}")
        print("█" * 65 + "\n")


# ── Demo ───────────────────────────────────────────────────────────────────────

def run_demo():
    """
    Full demo using Tata Consultancy Services (TCS) with sample financial
    report and news. Live candlestick data is fetched via yfinance if available.
    """
    print("\nRunning STOCKIFY demo with Tata Consultancy Services (TCS)...")

    tcs_input = CompanyInput(
        ticker="TCS",
        company_name="Tata Consultancy Services Ltd",
        sector="Information Technology / IT Services",
        description=(
            "TCS is India's largest IT services exporter, listed on NSE/BSE and part of the "
            "Tata Group. It provides consulting, application development, BPO and digital "
            "transformation services to Fortune 500 clients. Top verticals are BFSI, retail "
            "and manufacturing; top regions are North America (~50% of revenue) and Europe. "
            "Revenue is in USD/EUR/GBP but reported in ₹ — it benefits from a weaker rupee "
            "and is exposed to US/EU client-spend cycles, H-1B / immigration policy, and AI-led "
            "deflation in legacy services."
        ),

        # --- STAGE 1: Financial Report (Ind-AS, NSE/BSE filing) ---
        financial_report="""
        Tata Consultancy Services Ltd (TCS) — Q2 FY2025 Results
        Filed with NSE/BSE per SEBI LODR Reg 33; reported under Ind-AS.

        Revenue from Operations (consolidated): ₹64,259 Cr (+7.6% YoY).
        Operating Margin (EBIT): ₹15,866 Cr → 24.7% (vs 24.3% PYQ).
        Net Profit (PAT): ₹11,909 Cr (+5% YoY). Diluted EPS: ₹32.93.

        Balance Sheet (as on 30 Sep 2024):
        Cash + current investments: ₹46,200 Cr
        Total borrowings: ₹830 Cr (mostly leases) — D/E ≈ 0.01
        Total equity: ₹93,150 Cr
        Current ratio: 2.7×

        Cash Flow (H1 FY2025):
        Operating cash flow: ₹19,400 Cr; Free cash flow: ₹17,800 Cr; Capex: ₹1,600 Cr.

        Returns (TTM): ROE 49.8%, ROCE 64.6%

        Management commentary: TCV of $8.6B in Q2; BFSI vertical recovering; FY25 guidance
        maintained. Statutory auditors B S R & Co. LLP: unqualified opinion under SA 700;
        ICFR effective. No emphasis-of-matter. No material related-party transactions outside
        arm's length per Reg 23. No pending material litigation per Reg 30 disclosures.
        """,

        # --- STAGE 2: Indian-market news for Geopolitical Analysis ---
        news_articles=[
            {
                "title": "US Fed Signals Slower Rate-Cut Path; IT Stocks Slip",
                "source": "Economic Times",
                "content": (
                    "The Fed's hawkish dot plot pushed the dollar index above 105 and "
                    "weighed on the Nifty IT index. Indian IT exporters earn the bulk of "
                    "their revenue from US BFSI clients whose discretionary tech spend is "
                    "rate-sensitive."
                ),
            },
            {
                "title": "RBI Holds Repo Rate at 6.5%, Watches Sticky Food Inflation",
                "source": "Mint",
                "content": (
                    "The MPC held rates for the eleventh straight meeting. Indian large-caps "
                    "with high FII ownership saw mixed reactions; INR closed at 84.10 vs USD."
                ),
            },
            {
                "title": "AI-Led Deflation Forces Indian IT to Reprice Long-Tail Deals",
                "source": "Business Standard",
                "content": (
                    "Tier-1 IT firms are renegotiating multi-year contracts as AI tooling "
                    "compresses billable hours by 15-25% on legacy work. Margin pressure is "
                    "expected to persist through FY26 even as TCV grows."
                ),
            },
            {
                "title": "H-1B Visa Backlog Eases; Onsite Costs Stabilise",
                "source": "Livemint",
                "content": (
                    "USCIS cleared a long-pending backlog of H-1B extensions. Indian IT firms "
                    "with large US delivery footprints expect onsite cost pressure to ease "
                    "next quarter."
                ),
            },
        ],

        # Keywords describing TCS's exposure areas (used by geo risk agent)
        target_keywords=[
            "US Fed rates", "BFSI tech spend", "INR/USD", "AI deflation",
            "H-1B visa", "Nifty IT", "FII flows", "EU client demand",
            "TCV", "wage inflation", "outsourcing", "RBI policy",
        ],

        # --- STAGE 3: Technical Analysis (TCS via yfinance .NS auto-suffix) ---
        candles=None,
        chart_period="3mo",
        chart_interval="1d",

        # --- Pipeline thresholds (customize to your standards) ---
        financial_pass_threshold=60,   # Financial score must be ≥ 60 to proceed
        geo_fail_threshold=55,         # Geo risk score must be ≤ 55 to proceed
    )

    agent = StockAgent()
    report = agent.run(tcs_input, verbose=True, save_report=True)

    print(f"\nPipeline complete. Stages completed: {report.stages_completed}/3")
    print(f"Verdict: {report.overall_verdict}")

    return report


def run_custom():
    """
    Template for running with your own company data.
    Edit the fields below and call python stock_agent.py --custom
    """

    my_input = CompanyInput(
        ticker="YOUR_TICKER",
        company_name="Your Company Name",
        sector="Your Sector",
        description="Describe what this company does and who it sells to.",

        financial_report="""
        Paste your financial report text here.
        Include: revenue, margins, EPS, debt, cash flow, guidance.
        """,

        news_articles=[
            {
                "title": "Paste a news headline here",
                "source": "Source Name",
                "content": "Paste the article content here...",
            },
            # Add more articles...
        ],

        target_keywords=[
            "keyword1", "keyword2",  # e.g., "China", "oil prices", "interest rates"
        ],

        # Thresholds
        financial_pass_threshold=60,
        geo_fail_threshold=55,
    )

    agent = StockAgent()
    return agent.run(my_input, verbose=True, save_report=True)


if __name__ == "__main__":
    import sys

    if "--custom" in sys.argv:
        run_custom()
    else:
        run_demo()

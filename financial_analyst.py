"""
Stage 1: Financial Report Analyst Agent
- Extracts key financial metrics from a report
- Evaluates against a user-defined checklist
- Flags red flags and green flags
- Returns pass/fail to gate the pipeline
"""

import anthropic
import json
from dataclasses import dataclass, field
from typing import Optional


DEFAULT_CHECKLIST = [
    # Profitability (Ind-AS reporting)
    "Revenue growth is positive (YoY)",
    "Gross margin is stable or improving",
    "Operating income (EBIT) is positive",
    "Net profit (PAT) is positive or improving trend",
    "EPS (Earnings Per Share) is growing",

    # Balance Sheet Health
    "Debt-to-equity ratio is below 2.0",
    "Current ratio is above 1.0 (can cover short-term liabilities)",
    "Cash and equivalents are sufficient for operations",
    "No significant goodwill impairment under Ind-AS 36",

    # Cash Flow
    "Operating cash flow is positive",
    "Free cash flow is positive",
    "Cash flow from operations exceeds net profit (earnings quality)",

    # Efficiency & Returns
    "Return on Equity (ROE) is above 15% (Indian large-cap benchmark)",
    "Return on Capital Employed (ROCE) is above 15%",
    "Inventory turnover is healthy for the sector",

    # Risk Indicators (SEBI / ICAI disclosures)
    "No mention of going concern doubts in the auditor's report",
    "No material restatements of prior financials",
    "Auditor's opinion is unqualified (clean) and ICFR is reported as adequate",
    "No material pending litigation per SEBI Regulation 30 disclosures",
    "No qualifications or emphases of matter under SA 705 / SA 706",
    "Management guidance is maintained or raised (not lowered)",
    "No related-party transactions outside the ordinary course / arm's length",
]


@dataclass
class FinancialAnalysisResult:
    passed: bool
    score: int                          # 0-100
    green_flags: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    checklist_results: dict = field(default_factory=dict)   # item -> pass/fail/na
    key_metrics: dict = field(default_factory=dict)
    summary: str = ""
    recommendation: str = ""
    raw_response: str = ""

    def to_dict(self) -> dict:
        return {
            "stage": "Financial Analysis",
            "passed": self.passed,
            "score": self.score,
            "green_flags": self.green_flags,
            "red_flags": self.red_flags,
            "checklist_results": self.checklist_results,
            "key_metrics": self.key_metrics,
            "summary": self.summary,
            "recommendation": self.recommendation,
        }


class FinancialAnalyst:
    """
    AI agent that analyzes a company's financial report against a checklist.
    Uses Claude with adaptive thinking for nuanced analysis.
    """

    PASS_THRESHOLD = 60     # score out of 100 needed to pass

    def __init__(self, checklist: list[str] = None, pass_threshold: int = None):
        self.client = anthropic.Anthropic()
        self.checklist = checklist or DEFAULT_CHECKLIST
        if pass_threshold is not None:
            self.PASS_THRESHOLD = pass_threshold

    def _build_prompt(self, company: str, ticker: str, report_text: str) -> str:
        checklist_str = "\n".join(f"  {i+1}. {item}" for i, item in enumerate(self.checklist))
        return f"""You are an expert sell-side financial analyst covering Indian equities listed on NSE/BSE.

The company below is a SEBI-regulated listed entity. It reports under **Ind-AS** (Indian Accounting Standards, broadly converged with IFRS), files quarterly results with stock exchanges per SEBI LODR Regulations, and its statutory audit follows **ICAI** Standards on Auditing (SA 700 / 705 / 706 etc.). Currency is the Indian rupee (₹). Numbers in Indian filings are commonly stated in **lakhs (1L = 100,000) or crores (1Cr = 10,000,000)** — preserve the unit when quoting.

Analyze the following financial report for {company} ({ticker}) and evaluate it against the provided checklist.

## FINANCIAL REPORT
{report_text}

## EVALUATION CHECKLIST
Evaluate each item below. For each item, determine: PASS, FAIL, or N/A (if not enough information).
{checklist_str}

## YOUR TASK
Return a JSON object with EXACTLY this structure (no extra text, just the JSON):
{{
  "key_metrics": {{
    "revenue_growth_yoy": "...",
    "gross_margin": "...",
    "net_profit_pat": "...",
    "eps": "...",
    "debt_to_equity": "...",
    "current_ratio": "...",
    "operating_cash_flow": "...",
    "free_cash_flow": "...",
    "roe": "...",
    "roce": "...",
    "any_other_notable_metric": "..."
  }},
  "checklist_results": {{
    "<exact checklist item text>": "PASS" | "FAIL" | "N/A",
    ...
  }},
  "green_flags": [
    "Specific positive finding with numbers from the report (state ₹ amounts in Cr/L as filed)",
    ...
  ],
  "red_flags": [
    "Specific concerning finding with numbers or quotes from the report",
    ...
  ],
  "score": <integer 0-100 representing overall financial health>,
  "summary": "<2-3 sentence executive summary of the financial health>",
  "recommendation": "<1 sentence: whether this company passes financial screening and why>"
}}

Be specific. Quote actual numbers from the report (₹ Cr / ₹ L preserved). If a metric isn't in the report, mark it N/A."""

    def analyze(
        self,
        company: str,
        ticker: str,
        report_text: str,
        verbose: bool = True,
    ) -> FinancialAnalysisResult:
        """
        Run the financial analysis agent.
        Streams the response and parses the JSON result.
        """
        if verbose:
            print(f"\n{'='*65}")
            print(f"  STAGE 1: FINANCIAL REPORT ANALYSIS — {ticker}")
            print(f"{'='*65}")
            print(f"  Checklist items: {len(self.checklist)}")
            print(f"  Pass threshold:  {self.PASS_THRESHOLD}/100")
            print(f"  Analyzing with Claude claude-opus-4-6 (adaptive thinking)...")
            print()

        prompt = self._build_prompt(company, ticker, report_text)

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
                        if hasattr(delta, "type"):
                            if delta.type == "text_delta":
                                full_response += delta.text
                                if verbose:
                                    print(delta.text, end="", flush=True)
                            elif delta.type == "thinking_delta" and verbose:
                                pass  # Suppress thinking blocks in output

            if verbose:
                print()

        except Exception as e:
            if verbose:
                print(f"\n  [Error during streaming: {e}]")
            return FinancialAnalysisResult(
                passed=False,
                score=0,
                summary=f"Analysis failed: {e}",
                recommendation="Unable to analyze — check report format and API key.",
                raw_response="",
            )

        # Parse JSON from response
        result = self._parse_response(full_response, verbose)
        return result

    def _parse_response(self, raw: str, verbose: bool) -> FinancialAnalysisResult:
        """Extract and parse JSON from Claude's response."""
        # Find JSON block
        raw = raw.strip()
        start = raw.find("{")
        end = raw.rfind("}") + 1

        if start == -1 or end == 0:
            if verbose:
                print("  [Warning: Could not find JSON in response]")
            return FinancialAnalysisResult(
                passed=False,
                score=0,
                summary="Failed to parse analysis response.",
                recommendation="Manual review required.",
                raw_response=raw,
            )

        try:
            data = json.loads(raw[start:end])
        except json.JSONDecodeError as e:
            if verbose:
                print(f"  [Warning: JSON parse error: {e}]")
            return FinancialAnalysisResult(
                passed=False,
                score=0,
                summary="Failed to parse JSON response.",
                recommendation="Manual review required.",
                raw_response=raw,
            )

        score = int(data.get("score", 0))
        passed = score >= self.PASS_THRESHOLD

        result = FinancialAnalysisResult(
            passed=passed,
            score=score,
            green_flags=data.get("green_flags", []),
            red_flags=data.get("red_flags", []),
            checklist_results=data.get("checklist_results", {}),
            key_metrics=data.get("key_metrics", {}),
            summary=data.get("summary", ""),
            recommendation=data.get("recommendation", ""),
            raw_response=raw,
        )

        if verbose:
            self._print_results(result)

        return result

    def _print_results(self, result: FinancialAnalysisResult):
        passed_label = "PASSED" if result.passed else "FAILED"
        icon = "✅" if result.passed else "❌"

        print(f"\n{'-'*65}")
        print(f"  FINANCIAL SCORE: {result.score}/100  {icon} {passed_label}")
        print(f"{'-'*65}")

        if result.green_flags:
            print(f"\n  GREEN FLAGS ({len(result.green_flags)}):")
            for flag in result.green_flags:
                print(f"    + {flag}")

        if result.red_flags:
            print(f"\n  RED FLAGS ({len(result.red_flags)}):")
            for flag in result.red_flags:
                print(f"    - {flag}")

        if result.summary:
            print(f"\n  SUMMARY: {result.summary}")

        if result.recommendation:
            print(f"\n  RECOMMENDATION: {result.recommendation}")

        # Checklist breakdown
        if result.checklist_results:
            passes = sum(1 for v in result.checklist_results.values() if v == "PASS")
            fails = sum(1 for v in result.checklist_results.values() if v == "FAIL")
            nas = sum(1 for v in result.checklist_results.values() if v == "N/A")
            print(f"\n  CHECKLIST: {passes} pass / {fails} fail / {nas} N/A")

        verdict = "PROCEED to Stage 2" if result.passed else "PIPELINE STOPPED — Financial check failed"
        print(f"\n  VERDICT: {verdict}")
        print(f"{'='*65}\n")


# ── Demo / standalone test ────────────────────────────────────────────────────
if __name__ == "__main__":
    SAMPLE_REPORT = """
    Tata Consultancy Services Ltd (TCS) — Q2 FY2025 Standalone & Consolidated Results
    (Filed with NSE & BSE per SEBI LODR Reg 33; reported under Ind-AS)

    Revenue from Operations: ₹64,259 Cr (consolidated), up 7.6% YoY.
    Operating Margin (EBIT): ₹15,866 Cr → 24.7% (vs 24.3% prior year quarter).
    Net Profit (PAT): ₹11,909 Cr, up 5% YoY. Diluted EPS: ₹32.93 (vs ₹31.40).

    Balance Sheet (consolidated, as on 30 Sep 2024):
    - Cash, bank balances and current investments: ₹46,200 Cr
    - Total borrowings: ₹830 Cr (lease + working capital). D/E ≈ 0.01.
    - Total equity attributable to shareholders: ₹93,150 Cr.
    - Current ratio: 2.7×.

    Cash Flow (H1 FY2025):
    - Operating cash flow: ₹19,400 Cr
    - Free cash flow: ₹17,800 Cr
    - Capex: ₹1,600 Cr.

    Returns (TTM):
    - ROE: 49.8%
    - ROCE: 64.6%

    Management commentary: deal pipeline strong (TCV of $8.6B in Q2); BFSI vertical
    showing recovery; guidance for FY25 maintained.
    Statutory auditors (B S R & Co. LLP, ICAI registration in good standing): unqualified
    opinion under SA 700; ICFR reported as operating effectively. No emphasis-of-matter
    paragraphs. No material related-party transactions outside arm's length per SEBI
    Regulation 23. No pending material litigation per Reg 30 disclosures.
    """

    analyst = FinancialAnalyst()
    result = analyst.analyze(
        company="Tata Consultancy Services Ltd",
        ticker="TCS",
        report_text=SAMPLE_REPORT,
    )

    print(f"\nFinal result dict:\n{json.dumps(result.to_dict(), indent=2)}")

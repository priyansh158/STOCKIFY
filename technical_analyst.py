"""
Stage 3: Technical Analysis Agent
- Fetches OHLCV (candlestick) data for a stock via yfinance
- Identifies candlestick patterns, trends, support/resistance
- Recommends: Day Trading, Swing Trading, Long-Term Investment, or Avoid
- Uses Claude with adaptive thinking to reason over the chart data
"""

import anthropic
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False

from india_utils import (
    fetch_history_with_fallback,
    format_price,
    strip_exchange_suffix,
    to_yfinance_ticker,
)

try:
    import kite_client
    KITE_AVAILABLE = True
except ImportError:
    kite_client = None  # type: ignore
    KITE_AVAILABLE = False


# STOCKIFY interval string → Kite Connect interval string
_INTERVAL_TO_KITE = {
    "1d":  "day",
    "1wk": "week",
    "1mo": "month",
    "1h":  "60minute",
    "30m": "30minute",
    "15m": "15minute",
    "5m":  "5minute",
    "3m":  "3minute",
    "1m":  "minute",
}

# STOCKIFY period string → approximate days for Kite's from_date
_PERIOD_TO_DAYS = {
    "5d":  5,
    "1mo": 32,
    "3mo": 95,
    "6mo": 190,
    "1y":  370,
    "2y":  740,
    "5y":  1830,
    "max": 3650,
}


@dataclass
class CandleStick:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: int

    def body_size(self) -> float:
        return abs(self.close - self.open)

    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    def is_bullish(self) -> bool:
        return self.close > self.open

    def is_bearish(self) -> bool:
        return self.close < self.open

    def to_str(self) -> str:
        direction = "▲" if self.is_bullish() else "▼"
        return (
            f"{self.date}  {direction}  O:{self.open:.2f}  H:{self.high:.2f}  "
            f"L:{self.low:.2f}  C:{self.close:.2f}  Vol:{self.volume:,}"
        )


@dataclass
class TechnicalAnalysisResult:
    recommendation: str                 # "DAY_TRADING" | "SWING_TRADING" | "LONG_TERM" | "AVOID"
    confidence: int                     # 0-100
    timeframe_analyzed: str            # e.g. "3 months daily"
    patterns_found: list[str] = field(default_factory=list)
    trend: str = ""                     # "UPTREND" | "DOWNTREND" | "SIDEWAYS" | "REVERSAL"
    support_levels: list[float] = field(default_factory=list)
    resistance_levels: list[float] = field(default_factory=list)
    entry_point: str = ""
    target_price: str = ""
    stop_loss: str = ""
    risk_reward_ratio: str = ""
    volume_analysis: str = ""
    momentum: str = ""
    summary: str = ""
    trading_notes: list[str] = field(default_factory=list)
    raw_response: str = ""

    def to_dict(self) -> dict:
        return {
            "stage": "Technical Analysis",
            "recommendation": self.recommendation,
            "confidence": self.confidence,
            "timeframe_analyzed": self.timeframe_analyzed,
            "patterns_found": self.patterns_found,
            "trend": self.trend,
            "support_levels": self.support_levels,
            "resistance_levels": self.resistance_levels,
            "entry_point": self.entry_point,
            "target_price": self.target_price,
            "stop_loss": self.stop_loss,
            "risk_reward_ratio": self.risk_reward_ratio,
            "volume_analysis": self.volume_analysis,
            "momentum": self.momentum,
            "summary": self.summary,
            "trading_notes": self.trading_notes,
        }


class TechnicalAnalyst:
    """
    AI agent that reads candlestick data, detects patterns,
    and recommends the best trading approach.
    """

    def __init__(self):
        self.client = anthropic.Anthropic()

    # ── Data fetching ──────────────────────────────────────────────────────────

    def fetch_candles(
        self,
        ticker: str,
        period: str = "3mo",
        interval: str = "1d",
    ) -> tuple[list[CandleStick], str]:
        """
        Fetch OHLCV data — Kite Connect first (live + accurate intraday),
        yfinance fallback.

        period:   '1mo', '3mo', '6mo', '1y', '2y'
        interval: '1d', '1wk', '1h', '15m'
        Returns (candles, timeframe_description).
        """
        candles: list[CandleStick] = []

        # ── Path A: Kite Connect (preferred when authed) ──────────────────────
        if KITE_AVAILABLE and kite_client.is_authenticated():
            try:
                candles = self._fetch_candles_kite(ticker, period, interval)
            except Exception:
                candles = []

        # ── Path B: yfinance fallback ────────────────────────────────────────
        if not candles:
            if not YFINANCE_AVAILABLE:
                raise ImportError(
                    "yfinance is not installed and Kite Connect is not authenticated. "
                    "Either install yfinance or log in to Kite."
                )
            df, _resolved = fetch_history_with_fallback(
                to_yfinance_ticker(ticker), period=period, interval=interval
            )
            if df is None or df.empty:
                raise ValueError(
                    f"No data returned for {ticker} on NSE (.NS) or BSE (.BO)"
                )
            for idx, row in df.iterrows():
                date_str = idx.strftime("%Y-%m-%d") if interval == "1d" else idx.strftime("%Y-%m-%d %H:%M")
                candles.append(CandleStick(
                    date=date_str,
                    open=round(float(row["Open"]), 2),
                    high=round(float(row["High"]), 2),
                    low=round(float(row["Low"]), 2),
                    close=round(float(row["Close"]), 2),
                    volume=int(row["Volume"]),
                ))

        period_labels = {
            "1mo": "1 month", "3mo": "3 months", "6mo": "6 months",
            "1y": "1 year", "2y": "2 years",
        }
        interval_labels = {
            "1d": "daily", "1wk": "weekly", "1h": "hourly", "15m": "15-minute",
        }
        timeframe = f"{period_labels.get(period, period)} {interval_labels.get(interval, interval)}"
        return candles, timeframe

    # ── Kite Connect path ─────────────────────────────────────────────────────

    def _fetch_candles_kite(
        self,
        ticker: str,
        period: str,
        interval: str,
    ) -> list[CandleStick]:
        """Pull OHLCV via kite.historical_data and convert to CandleStick list.

        Returns an empty list when the symbol cannot be resolved to a Kite
        instrument token or when the API returns no rows — caller handles
        fallback.
        """
        bare = strip_exchange_suffix(ticker)
        kite_interval = _INTERVAL_TO_KITE.get(interval)
        if not kite_interval:
            return []
        days = _PERIOD_TO_DAYS.get(period, 95)
        from_dt = datetime.now() - timedelta(days=days)
        to_dt   = datetime.now()

        # Try NSE then BSE
        token = None
        for exch in ("NSE", "BSE"):
            token = kite_client.get_instrument_token(bare, exchange=exch)
            if token:
                break
        if not token:
            return []

        rows = kite_client.historical(token, from_dt, to_dt, kite_interval)
        return self._kite_rows_to_candles(rows, interval)

    @staticmethod
    def _kite_rows_to_candles(rows: list[dict], interval: str) -> list[CandleStick]:
        """Convert Kite historical_data rows to CandleStick list.

        Identical shape to the yfinance path: date format depends on interval,
        all OHLC values rounded to 2 decimals, volume coerced to int.
        """
        out: list[CandleStick] = []
        for r in rows:
            dt = r.get("date")
            if not dt:
                continue
            try:
                date_str = (dt.strftime("%Y-%m-%d") if interval == "1d"
                            else dt.strftime("%Y-%m-%d %H:%M"))
            except AttributeError:
                # If Kite ever returns a string instead of a datetime
                date_str = str(dt)[:16]
            try:
                out.append(CandleStick(
                    date=date_str,
                    open=round(float(r.get("open", 0)), 2),
                    high=round(float(r.get("high", 0)), 2),
                    low=round(float(r.get("low", 0)), 2),
                    close=round(float(r.get("close", 0)), 2),
                    volume=int(r.get("volume", 0) or 0),
                ))
            except (TypeError, ValueError):
                continue
        return out

    # ── Simple pre-analysis for context ───────────────────────────────────────

    def _compute_basic_stats(self, candles: list[CandleStick]) -> dict:
        if not candles:
            return {}

        closes = [c.close for c in candles]
        volumes = [c.volume for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]

        # Moving averages
        def sma(values, n):
            if len(values) < n:
                return None
            return round(sum(values[-n:]) / n, 2)

        # Price change
        price_change = closes[-1] - closes[0]
        pct_change = (price_change / closes[0]) * 100

        # Volatility (std dev of daily returns)
        returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]
        avg_return = sum(returns) / len(returns) if returns else 0
        variance = sum((r - avg_return) ** 2 for r in returns) / len(returns) if returns else 0
        volatility = (variance ** 0.5) * 100

        return {
            "current_price": closes[-1],
            "period_start_price": closes[0],
            "price_change": round(price_change, 2),
            "pct_change": round(pct_change, 2),
            "period_high": max(highs),
            "period_low": min(lows),
            "sma_10": sma(closes, 10),
            "sma_20": sma(closes, 20),
            "sma_50": sma(closes, 50),
            "avg_volume": int(sum(volumes) / len(volumes)),
            "recent_volume": volumes[-1],
            "volume_vs_avg_pct": round((volumes[-1] / (sum(volumes) / len(volumes)) - 1) * 100, 1),
            "daily_volatility_pct": round(volatility, 2),
            "candle_count": len(candles),
        }

    # ── Prompt builder ────────────────────────────────────────────────────────

    def _build_prompt(
        self,
        ticker: str,
        company: str,
        candles: list[CandleStick],
        stats: dict,
        timeframe: str,
    ) -> str:
        # Show last 60 candles in the prompt (enough context without overloading)
        display_candles = candles[-60:] if len(candles) > 60 else candles
        candle_table = "\n".join(c.to_str() for c in display_candles)

        # Last 5 candles highlighted
        recent = candles[-5:]
        recent_str = "\n".join(c.to_str() for c in recent)

        return f"""You are a professional technical analyst specializing in candlestick patterns and price action trading.

## STOCK: {company} ({ticker})  — Indian listed equity (NSE / BSE)
## TIMEFRAME: {timeframe}
## CURRENT PRICE: ₹{stats.get('current_price', 'N/A')}

## KEY STATISTICS
- Period Performance: {stats.get('pct_change', 'N/A')}% ({stats.get('period_start_price', '?')} → {stats.get('current_price', '?')})
- Period Range: ₹{stats.get('period_low', '?')} — ₹{stats.get('period_high', '?')}
- SMA(10): ₹{stats.get('sma_10', 'N/A')}, SMA(20): ₹{stats.get('sma_20', 'N/A')}, SMA(50): ₹{stats.get('sma_50', 'N/A')}
- Avg Daily Volume: {stats.get('avg_volume', 'N/A'):,}
- Latest Volume vs Avg: {stats.get('volume_vs_avg_pct', 'N/A')}%
- Daily Volatility: {stats.get('daily_volatility_pct', 'N/A')}%

## CANDLESTICK DATA (Format: Date | Direction | O H L C | Volume)
{candle_table}

## MOST RECENT 5 CANDLES
{recent_str}

## YOUR TASK
Analyze the candlestick data above. Identify patterns, trend structure, and key levels.
Then provide a specific trading recommendation.

Return a JSON object with EXACTLY this structure (no extra text, just the JSON):
{{
  "recommendation": "DAY_TRADING" | "SWING_TRADING" | "LONG_TERM" | "AVOID",
  "confidence": <integer 0-100>,
  "trend": "UPTREND" | "DOWNTREND" | "SIDEWAYS" | "REVERSAL_BULLISH" | "REVERSAL_BEARISH",
  "patterns_found": [
    "Pattern name: description and significance (e.g. 'Bullish Engulfing on 2024-11-15: strong reversal signal')",
    ...
  ],
  "support_levels": [<price>, <price>],
  "resistance_levels": [<price>, <price>],
  "entry_point": "<specific price or condition, e.g. 'Buy on break above ₹2,850 with volume confirmation'>",
  "target_price": "<price target with reasoning, e.g. '₹3,050 (next resistance / measured move)'>",
  "stop_loss": "<stop loss level with reasoning, e.g. '₹2,720 (below recent swing low)'>",
  "risk_reward_ratio": "<e.g. '1:2.5'>",
  "volume_analysis": "<analysis of volume patterns — is volume confirming the move?>",
  "momentum": "<assessment of momentum: building, fading, diverging?>",
  "summary": "<2-3 sentence summary of the technical picture and why you chose this recommendation>",
  "trading_notes": [
    "Specific note or caveat a trader should know",
    ...
  ]
}}

RECOMMENDATION GUIDE:
- DAY_TRADING: Clear intraday setups within the 9:15–15:30 IST session, high volatility, strong momentum — enter and exit same day
- SWING_TRADING: 3–10 day hold, clear pattern with defined entry/target/stop, trend confirmation
- LONG_TERM: Strong uptrend, accumulation pattern, healthy base — hold for weeks/months
- AVOID: No clear setup, choppy/sideways action, weak volume, or bearish breakdown

Be specific with prices in ₹. Reference actual candle dates and levels from the data above. Note: NSE/BSE has no extended/pre-market session beyond the 9:00–9:15 IST pre-open auction."""

    # ── Main analysis ──────────────────────────────────────────────────────────

    def analyze(
        self,
        ticker: str,
        company: str,
        candles: list[CandleStick],
        timeframe: str,
        verbose: bool = True,
    ) -> TechnicalAnalysisResult:
        """Core analysis method — accepts pre-loaded candles."""

        stats = self._compute_basic_stats(candles)

        if verbose:
            print(f"\n{'='*65}")
            print(f"  STAGE 3: TECHNICAL ANALYSIS — {ticker}")
            print(f"{'='*65}")
            print(f"  Timeframe:     {timeframe}")
            print(f"  Candles:       {len(candles)}")
            print(f"  Current price: ₹{stats.get('current_price', 'N/A')}")
            print(f"  Period perf:   {stats.get('pct_change', 'N/A')}%")
            print(f"  Analyzing with Claude claude-opus-4-6 (adaptive thinking)...")
            print()

        prompt = self._build_prompt(ticker, company, candles, stats, timeframe)

        full_response = ""
        try:
            with self.client.messages.stream(
                model="claude-opus-4-6",
                max_tokens=6000,
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
            return TechnicalAnalysisResult(
                recommendation="AVOID",
                confidence=0,
                timeframe_analyzed=timeframe,
                summary=f"Analysis failed: {e}",
                raw_response="",
            )

        result = self._parse_response(full_response, timeframe, verbose)
        return result

    def analyze_ticker(
        self,
        ticker: str,
        company: str = None,
        period: str = "3mo",
        interval: str = "1d",
        verbose: bool = True,
    ) -> TechnicalAnalysisResult:
        """Convenience method: fetches data via yfinance then analyzes."""
        if not YFINANCE_AVAILABLE:
            raise ImportError("yfinance not installed. Run: pip install yfinance")

        company = company or ticker
        if verbose:
            print(f"  Fetching {period} {interval} data for {ticker}...")

        candles, timeframe = self.fetch_candles(ticker, period, interval)

        if verbose:
            print(f"  Fetched {len(candles)} candles.")

        return self.analyze(ticker, company, candles, timeframe, verbose)

    # ── Parse response ─────────────────────────────────────────────────────────

    def _parse_response(self, raw: str, timeframe: str, verbose: bool) -> TechnicalAnalysisResult:
        raw = raw.strip()
        start = raw.find("{")
        end = raw.rfind("}") + 1

        if start == -1 or end == 0:
            if verbose:
                print("  [Warning: Could not find JSON in response]")
            return TechnicalAnalysisResult(
                recommendation="AVOID",
                confidence=0,
                timeframe_analyzed=timeframe,
                summary="Failed to parse analysis response.",
                raw_response=raw,
            )

        try:
            data = json.loads(raw[start:end])
        except json.JSONDecodeError as e:
            if verbose:
                print(f"  [Warning: JSON parse error: {e}]")
            return TechnicalAnalysisResult(
                recommendation="AVOID",
                confidence=0,
                timeframe_analyzed=timeframe,
                summary="Failed to parse JSON response.",
                raw_response=raw,
            )

        result = TechnicalAnalysisResult(
            recommendation=data.get("recommendation", "AVOID"),
            confidence=int(data.get("confidence", 0)),
            timeframe_analyzed=timeframe,
            trend=data.get("trend", ""),
            patterns_found=data.get("patterns_found", []),
            support_levels=[float(x) for x in data.get("support_levels", [])],
            resistance_levels=[float(x) for x in data.get("resistance_levels", [])],
            entry_point=data.get("entry_point", ""),
            target_price=data.get("target_price", ""),
            stop_loss=data.get("stop_loss", ""),
            risk_reward_ratio=data.get("risk_reward_ratio", ""),
            volume_analysis=data.get("volume_analysis", ""),
            momentum=data.get("momentum", ""),
            summary=data.get("summary", ""),
            trading_notes=data.get("trading_notes", []),
            raw_response=raw,
        )

        if verbose:
            self._print_results(result)

        return result

    def _print_results(self, result: TechnicalAnalysisResult):
        rec_icons = {
            "DAY_TRADING": "⚡",
            "SWING_TRADING": "📊",
            "LONG_TERM": "🏦",
            "AVOID": "🚫",
        }
        trend_icons = {
            "UPTREND": "📈",
            "DOWNTREND": "📉",
            "SIDEWAYS": "➡️",
            "REVERSAL_BULLISH": "↗️",
            "REVERSAL_BEARISH": "↘️",
        }

        icon = rec_icons.get(result.recommendation, "?")
        trend_icon = trend_icons.get(result.trend, "?")

        print(f"\n{'-'*65}")
        print(f"  RECOMMENDATION: {icon} {result.recommendation}  (confidence: {result.confidence}%)")
        print(f"  TREND: {trend_icon} {result.trend}")
        print(f"{'-'*65}")

        if result.patterns_found:
            print(f"\n  PATTERNS FOUND ({len(result.patterns_found)}):")
            for pattern in result.patterns_found:
                print(f"    * {pattern}")

        if result.support_levels:
            print(f"\n  SUPPORT:    {', '.join(f'₹{s}' for s in result.support_levels)}")
        if result.resistance_levels:
            print(f"  RESISTANCE: {', '.join(f'₹{r}' for r in result.resistance_levels)}")

        if result.entry_point:
            print(f"\n  ENTRY:           {result.entry_point}")
        if result.target_price:
            print(f"  TARGET:          {result.target_price}")
        if result.stop_loss:
            print(f"  STOP LOSS:       {result.stop_loss}")
        if result.risk_reward_ratio:
            print(f"  RISK/REWARD:     {result.risk_reward_ratio}")

        if result.volume_analysis:
            print(f"\n  VOLUME:   {result.volume_analysis}")
        if result.momentum:
            print(f"  MOMENTUM: {result.momentum}")

        if result.summary:
            print(f"\n  SUMMARY: {result.summary}")

        if result.trading_notes:
            print(f"\n  TRADING NOTES:")
            for note in result.trading_notes:
                print(f"    > {note}")

        print(f"{'='*65}\n")


# ── Demo / standalone test ────────────────────────────────────────────────────
if __name__ == "__main__":
    analyst = TechnicalAnalyst()

    if YFINANCE_AVAILABLE:
        # Live test with Reliance Industries (NSE)
        result = analyst.analyze_ticker(
            ticker="RELIANCE",
            company="Reliance Industries Ltd",
            period="3mo",
            interval="1d",
        )
    else:
        # Manual candle test (fallback) — synthetic ₹ levels around RELIANCE range
        print("yfinance not available — using sample candles")
        sample_candles = [
            CandleStick("2024-10-01", 2810.0, 2855.0, 2802.0, 2848.0, 5_400_000),
            CandleStick("2024-10-02", 2848.0, 2880.0, 2840.0, 2872.5, 6_200_000),
            CandleStick("2024-10-03", 2872.5, 2895.0, 2860.0, 2865.0, 4_700_000),
            CandleStick("2024-10-04", 2865.0, 2870.0, 2820.0, 2828.0, 7_100_000),
            CandleStick("2024-10-07", 2828.0, 2842.0, 2810.0, 2835.0, 4_900_000),
            CandleStick("2024-10-08", 2835.0, 2890.0, 2830.0, 2885.0, 6_800_000),
            CandleStick("2024-10-09", 2885.0, 2915.0, 2878.0, 2908.0, 7_500_000),
            CandleStick("2024-10-10", 2908.0, 2930.0, 2895.0, 2912.0, 5_100_000),
        ]
        result = analyst.analyze(
            ticker="RELIANCE",
            company="Reliance Industries Ltd",
            candles=sample_candles,
            timeframe="sample data",
        )

    print(f"\nFinal result dict:\n{json.dumps(result.to_dict(), indent=2)}")

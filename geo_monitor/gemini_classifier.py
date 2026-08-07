"""Heuristic prefilter + Gemini disruptiveness classifier."""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

import requests

log = logging.getLogger("geo_monitor.gemini")

# Title/snippet cues that warrant a Gemini look
_DISRUPT_PATTERNS = [
    r"\bwar\b", r"\binvasion\b", r"\bsanction", r"\bembargo\b", r"\btariff",
    r"\bban\b", r"\bcrash\b", r"\bdefault\b", r"\bcoup\b", r"\bhike\b",
    r"\bcut\b", r"\bemergency\b", r"\bexplode", r"\bmissile\b",
    r"\brepo rate\b", r"\bRBI\b", r"\bFed\b", r"\bFOMC\b", r"\bcrude\b",
    r"\boil price", r"\brupee\b", r"\bUSD.?INR\b", r"\bSEBI\b",
    r"\bhalt\b", r"\bsuspend", r"\bprobe\b", r"\bfraud\b", r"\bscam\b",
    r"\brecession\b", r"\binflation\b", r"\bmonsoon\b", r"\bearthquake\b",
]

_DISRUPT_RE = re.compile("|".join(_DISRUPT_PATTERNS), re.IGNORECASE)

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")


def resolve_gemini_api_key() -> str:
    return (
        os.environ.get("GOOGLE_AI_API_KEY", "")
        or os.environ.get("GEMINI_API_KEY", "")
        or os.environ.get("GOOGLE_API_KEY", "")
    ).strip()


def heuristic_suspect(
    title: str,
    description: str = "",
    *,
    holding_terms: Optional[list[str]] = None,
) -> bool:
    text = f"{title} {description}"
    if _DISRUPT_RE.search(text):
        return True
    for term in holding_terms or []:
        t = (term or "").strip()
        if len(t) >= 2 and re.search(re.escape(t), text, re.IGNORECASE):
            return True
    return False


def _extract_json(text: str) -> Any:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}|\[[\s\S]*\]", text)
        if m:
            return json.loads(m.group(0))
        raise


def classify_batch(
    articles: list[dict[str, Any]],
    *,
    holding_tickers: Optional[list[str]] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Classify articles for equity-market disruptiveness via Gemini.

    Each input: {guid, title, source, description, url, query}
    Each output adds: severity, disruption_score, scope, affected_tickers,
    why_disruptive, investor_action, confidence
    """
    key = (api_key or resolve_gemini_api_key()).strip()
    if not key:
        raise RuntimeError(
            "Missing GOOGLE_AI_API_KEY / GEMINI_API_KEY — cannot classify"
        )
    if not articles:
        return []

    model_id = model or DEFAULT_MODEL
    holdings_blurb = ", ".join(holding_tickers or []) or "(none configured)"

    payload_articles = []
    for i, a in enumerate(articles):
        payload_articles.append(
            {
                "id": i,
                "guid": a.get("guid", ""),
                "title": a.get("title", ""),
                "source": a.get("source", ""),
                "description": (a.get("description") or a.get("content") or "")[:500],
                "query": a.get("query", ""),
            }
        )

    prompt = f"""You are a geopolitical and macro risk analyst for Indian equity investors (NSE/BSE).

Watched holdings/tickers: {holdings_blurb}

Score each headline for DISRUPTIVE market impact — collateral risk to Indian
equities or to watched holdings, even if the company is not named.

Return ONLY a JSON array (no markdown). One object per input id:
[
  {{
    "id": <int matching input>,
    "severity": "LOW"|"MEDIUM"|"HIGH"|"CRITICAL",
    "disruption_score": <0-100>,
    "scope": "macro"|"holding"|"both",
    "affected_tickers": ["TCS", ...],
    "why_disruptive": "1-2 sentences",
    "investor_action": "one sentence for the investor",
    "confidence": <0-1>
  }}
]

Rules:
- HIGH/CRITICAL only for material, time-sensitive shocks (policy surprise, war escalation,
  major commodity spike, banking stress, sanctions, fraud probes on held names, etc.).
- Routine commentary / old news → LOW or MEDIUM.
- Prefer PRECISION over recall for HIGH/CRITICAL.

ARTICLES:
{json.dumps(payload_articles, ensure_ascii=False)}
"""

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model_id}:generateContent?key={key}"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }
    resp = requests.post(url, json=body, timeout=60)
    if resp.status_code >= 400:
        raise RuntimeError(f"Gemini HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Unexpected Gemini response: {e}") from e

    parsed = _extract_json(text)
    if isinstance(parsed, dict) and "results" in parsed:
        parsed = parsed["results"]
    if not isinstance(parsed, list):
        raise RuntimeError("Gemini did not return a JSON array")

    by_id: dict[int, dict] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            by_id[int(item["id"])] = item
        except (KeyError, TypeError, ValueError):
            continue

    out: list[dict[str, Any]] = []
    for i, a in enumerate(articles):
        c = by_id.get(i, {})
        severity = str(c.get("severity") or "LOW").upper()
        if severity not in ("LOW", "MEDIUM", "HIGH", "CRITICAL"):
            severity = "LOW"
        try:
            score = int(c.get("disruption_score", 0))
        except (TypeError, ValueError):
            score = 0
        score = max(0, min(100, score))
        tickers = c.get("affected_tickers") or []
        if not isinstance(tickers, list):
            tickers = []
        try:
            conf = float(c.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        merged = dict(a)
        merged.update(
            {
                "severity": severity,
                "disruption_score": score,
                "scope": str(c.get("scope") or "macro"),
                "affected_tickers": [str(t).upper() for t in tickers if t],
                "why_disruptive": str(c.get("why_disruptive") or ""),
                "investor_action": str(c.get("investor_action") or ""),
                "confidence": conf,
            }
        )
        out.append(merged)
    return out


def should_alert(classification: dict[str, Any], *, score_floor: int = 70) -> bool:
    sev = str(classification.get("severity") or "").upper()
    if sev in ("HIGH", "CRITICAL"):
        return True
    try:
        return int(classification.get("disruption_score") or 0) >= score_floor
    except (TypeError, ValueError):
        return False

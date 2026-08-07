"""Async Claude streaming bridge.

Replaces the synchronous `stream_claude` from `app.py`. The pattern is the
same — pull from `anthropic.AsyncAnthropic.messages.stream`, accumulate text
deltas — but instead of mutating a Streamlit placeholder, the generator
yields structured events the SSE endpoint forwards directly to the browser:

  {"type": "delta", "text": "<full accumulated buffer so far>"}
  {"type": "done",  "text": "<final full buffer>"}
  {"type": "error", "message": "<friendly>", "raw": "<verbatim error>"}

The friendly-error translation lives here too so every caller (analysis,
summary, future stages) gets the same UX message for credit-balance, rate-
limit, API-key, and overload failures.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import AsyncIterator, Literal

import anthropic

log = logging.getLogger("stockify.stream_claude")

# The model the existing pipeline targets. Keep in sync with CLAUDE.md and
# the legacy `app.py` settings.
DEFAULT_MODEL = "claude-opus-4-6"
DEFAULT_MAX_TOKENS = 16_000

ThinkingMode = Literal["adaptive", "off"]


def _resolve_api_key() -> str:
    """Resolve Anthropic API key from environment.

    Matches the legacy `_resolve_api_key()` in app.py but drops the
    Streamlit-specific session_state / st.secrets sources — FastAPI is
    process-level and reads only env.
    """
    raw = os.environ.get("ANTHROPIC_API_KEY", "")
    return (raw or "").strip()


def friendly_claude_error(raw: str | BaseException) -> str:
    """Turn raw model/API errors into a user-actionable message.

    Mirrors `_friendly_claude_error` from app.py so the user sees the same
    actionable text regardless of which surface (Streamlit or FastAPI)
    surfaced the failure.
    """
    err = str(raw or "").strip()
    low = err.lower()
    if "credit balance is too low" in low or "credit balance" in low:
        return ("Anthropic API credits are exhausted. Add credits at "
                "console.anthropic.com/settings/billing and retry.")
    if "rate" in low and ("429" in err or "limit" in low):
        return "Anthropic API rate limit hit — wait a minute and retry."
    if "no api key" in low or ("api key" in low and "not" in low):
        return ("Anthropic API key not configured — set ANTHROPIC_API_KEY in "
                ".env and restart the server.")
    if "overloaded" in low or "503" in err:
        return "Anthropic API is temporarily overloaded — retry in a moment."
    return err or "Stage failed with an unknown error."


# JSON repair lifted from app.py:parse_json. Pure function — no Streamlit.
def parse_json(text: str) -> dict | None:
    """Extract and parse a JSON object out of model output.

    Handles two common Claude quirks:
      1. Wrapping the JSON in prose / markdown — we grab the first '{' and
         last '}'.
      2. Trailing commas and unescaped newlines inside string values — we
         repair and retry once.
    """
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        return None
    raw = text[start:end]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    repaired = re.sub(r",\s*([}\]])", r"\1", raw)
    repaired = re.sub(
        r'(?<=": ")(.*?)(?="[,}\]])',
        lambda m: m.group(0).replace("\n", "\\n"),
        repaired,
        flags=re.DOTALL,
    )
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        return None


async def stream_claude_events(
    prompt: str,
    *,
    thinking_mode: ThinkingMode = "adaptive",
    max_tokens: int = DEFAULT_MAX_TOKENS,
    model: str = DEFAULT_MODEL,
) -> AsyncIterator[dict]:
    """Stream Claude responses as a sequence of SSE-shaped event dicts.

    The caller (analysis SSE endpoint) wraps each yielded dict in
    `data: {json}\\n\\n` and pushes to the browser.

    Yield contract:
      * Many `{"type":"delta","text": full_buffer_so_far}` events
      * Exactly one terminal event:
          - `{"type":"done","text": full_buffer}` on success
          - `{"type":"error","message": friendly, "raw": str(err)}` on any failure
            (auth, network, rate, credit, empty response, etc).

    Errors are not raised — every failure path yields an error event so the
    caller doesn't need to wrap in try/except.
    """
    api_key = _resolve_api_key()
    if not api_key:
        yield {
            "type": "error",
            "message": friendly_claude_error("no API key configured"),
            "raw": "no API key configured",
        }
        return

    client = anthropic.AsyncAnthropic(api_key=api_key)
    stream_kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if thinking_mode == "adaptive":
        stream_kwargs["thinking"] = {"type": "adaptive"}

    full_text = ""
    try:
        async with client.messages.stream(**stream_kwargs) as stream:
            async for event in stream:
                t = getattr(event, "type", "")
                if t != "content_block_delta":
                    continue
                delta = getattr(event, "delta", None)
                if delta is None:
                    continue
                if getattr(delta, "type", "") != "text_delta":
                    continue
                full_text += getattr(delta, "text", "")
                yield {"type": "delta", "text": full_text}
    except Exception as e:
        log.warning("Claude stream failed: %s", e)
        yield {
            "type": "error",
            "message": friendly_claude_error(e),
            "raw": str(e),
        }
        return

    if not full_text.strip():
        yield {
            "type": "error",
            "message": friendly_claude_error("empty response from model"),
            "raw": "empty response from model",
        }
        return

    yield {"type": "done", "text": full_text}

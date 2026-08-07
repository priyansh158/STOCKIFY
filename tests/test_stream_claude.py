"""Unit tests for server.services.stream_claude.

Two independent surfaces:

  * `parse_json` — the JSON-repair path that every stage's result flows
    through. If it silently returns None, the stage is reported as failed even
    though the model answered correctly, so its repair rules get exact tests.

  * `stream_claude_events` — the SSE event contract. Its documented promise is
    that it NEVER raises: every failure path yields a terminal error event.
    That is tested by driving a fake Anthropic client through each failure.

No network: the Anthropic client is replaced wholesale.
"""
from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import patch

import tests.support  # noqa: F401  — must precede project imports

from server.services import stream_claude
from server.services.stream_claude import (
    friendly_claude_error,
    parse_json,
    stream_claude_events,
)


# ── parse_json ───────────────────────────────────────────────────────────────

class ParseJsonTests(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(parse_json('{"a": 1}'), {"a": 1})

    def test_empty_and_none_input(self):
        self.assertIsNone(parse_json(""))
        self.assertIsNone(parse_json(None))

    def test_text_without_braces(self):
        self.assertIsNone(parse_json("I cannot answer that."))

    def test_unclosed_object(self):
        self.assertIsNone(parse_json('{"a": 1'))

    def test_object_wrapped_in_prose(self):
        self.assertEqual(parse_json('Here you go:\n{"a": 1}\nHope that helps.'), {"a": 1})

    def test_object_in_a_markdown_fence(self):
        self.assertEqual(parse_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_nested_objects_span_to_the_last_brace(self):
        payload = {"a": {"b": {"c": [1, 2, 3]}}, "d": "e"}
        self.assertEqual(parse_json(json.dumps(payload)), payload)

    def test_trailing_comma_in_an_object_is_repaired(self):
        self.assertEqual(parse_json('{"a": 1,}'), {"a": 1})

    def test_trailing_comma_in_an_array_is_repaired(self):
        self.assertEqual(parse_json('{"a": [1, 2,]}'), {"a": [1, 2]})

    def test_multiple_trailing_commas_are_repaired(self):
        self.assertEqual(parse_json('{"a": [1,], "b": {"c": 2,},}'),
                         {"a": [1], "b": {"c": 2}})

    def test_literal_newline_inside_a_string_is_escaped(self):
        self.assertEqual(parse_json('{"a": "line1\nline2"}'), {"a": "line1\nline2"})

    def test_unicode_and_rupee_symbols_survive(self):
        payload = {"target": "₹3,050", "note": "भारतीय बाज़ार"}
        self.assertEqual(parse_json(json.dumps(payload, ensure_ascii=False)), payload)

    def test_escaped_quotes_inside_strings(self):
        self.assertEqual(parse_json('{"a": "he said \\"hi\\""}'), {"a": 'he said "hi"'})

    def test_top_level_array_yields_its_first_object(self):
        # The extractor is brace-anchored, so a JSON array of objects collapses
        # to the span between the first '{' and the last '}'.
        self.assertEqual(parse_json('[{"a": 1}]'), {"a": 1})

    def test_irreparable_json_returns_none(self):
        self.assertIsNone(parse_json('{"a": 1 "b": 2}'))

    def test_empty_object(self):
        self.assertEqual(parse_json("{}"), {})

    def test_realistic_stage_payload(self):
        payload = {
            "risk_score": 30, "risk_level": "LOW",
            "collateral_risks": [], "safe_signals": ["Domestic demand"],
            "summary": "Benign.", "recommendation": "Passes.",
        }
        raw = "Let me analyse.\n```json\n" + json.dumps(payload) + "\n```\nDone."
        self.assertEqual(parse_json(raw), payload)


# ── friendly_claude_error ────────────────────────────────────────────────────

class FriendlyErrorTests(unittest.TestCase):
    def test_credit_balance(self):
        msg = friendly_claude_error("Your credit balance is too low to access the API")
        self.assertIn("credits are exhausted", msg)
        self.assertIn("console.anthropic.com", msg)

    def test_rate_limit_by_status_code(self):
        self.assertIn("rate limit", friendly_claude_error("429 rate_limit_error"))

    def test_rate_limit_by_wording(self):
        self.assertIn("rate limit", friendly_claude_error("Rate limit exceeded"))

    def test_missing_api_key(self):
        msg = friendly_claude_error("no api key provided")
        self.assertIn("ANTHROPIC_API_KEY", msg)

    def test_overloaded(self):
        self.assertIn("overloaded", friendly_claude_error("Error: overloaded_error"))

    def test_service_unavailable(self):
        self.assertIn("overloaded", friendly_claude_error("503 Service Unavailable"))

    def test_accepts_an_exception_object(self):
        self.assertIn("credits are exhausted",
                      friendly_claude_error(RuntimeError("credit balance is too low")))

    def test_unknown_error_passes_through_verbatim(self):
        self.assertEqual(friendly_claude_error("something odd happened"),
                         "something odd happened")

    def test_empty_error_gets_a_generic_message(self):
        self.assertEqual(friendly_claude_error(""), "Stage failed with an unknown error.")
        self.assertEqual(friendly_claude_error(None), "Stage failed with an unknown error.")

    def test_credit_balance_takes_precedence_over_rate_wording(self):
        msg = friendly_claude_error("429: credit balance is too low, rate limited")
        self.assertIn("credits are exhausted", msg)

    def test_every_branch_returns_a_non_empty_actionable_string(self):
        for raw in ("credit balance is too low", "429 rate limit", "no api key",
                    "overloaded", "503", "weird", ""):
            with self.subTest(raw=raw):
                msg = friendly_claude_error(raw)
                self.assertTrue(msg and msg.strip())


# ── Fake Anthropic streaming client ──────────────────────────────────────────

class _Delta:
    def __init__(self, text, type="text_delta"):
        self.text = text
        self.type = type


class _Event:
    def __init__(self, type, delta=None):
        self.type = type
        self.delta = delta


def text_events(*chunks):
    return [_Event("content_block_delta", _Delta(c)) for c in chunks]


class _FakeStream:
    def __init__(self, events, enter_error=None, iter_error=None):
        self._events = events
        self._enter_error = enter_error
        self._iter_error = iter_error

    async def __aenter__(self):
        if self._enter_error:
            raise self._enter_error
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        events, iter_error = self._events, self._iter_error

        async def gen():
            for e in events:
                yield e
            if iter_error:
                raise iter_error

        return gen()


class _FakeMessages:
    def __init__(self, stream_obj):
        self._stream_obj = stream_obj
        self.calls = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return self._stream_obj


class _FakeClient:
    def __init__(self, stream_obj):
        self.messages = _FakeMessages(stream_obj)


def run(agen):
    async def collect():
        return [event async for event in agen]
    return asyncio.get_event_loop().run_until_complete(collect())


class StreamEventsTest(unittest.TestCase):
    def setUp(self):
        # asyncio.get_event_loop() is deprecated-but-present; give each test a
        # fresh loop so a failure can't poison the next.
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.addCleanup(self.loop.close)
        self._env = patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def drive(self, stream_obj, **kwargs):
        client = _FakeClient(stream_obj)
        with patch.object(stream_claude.anthropic, "AsyncAnthropic", return_value=client):
            events = run(stream_claude_events("prompt", **kwargs))
        return events, client


class StreamSuccessTests(StreamEventsTest):
    def test_deltas_then_done(self):
        events, _ = self.drive(_FakeStream(text_events("Hello", " world")))
        self.assertEqual([e["type"] for e in events], ["delta", "delta", "done"])

    def test_delta_text_is_the_cumulative_buffer(self):
        events, _ = self.drive(_FakeStream(text_events("Hello", " world")))
        self.assertEqual(events[0]["text"], "Hello")
        self.assertEqual(events[1]["text"], "Hello world")

    def test_done_carries_the_full_text(self):
        events, _ = self.drive(_FakeStream(text_events("Hello", " world")))
        self.assertEqual(events[-1]["text"], "Hello world")

    def test_exactly_one_terminal_event(self):
        events, _ = self.drive(_FakeStream(text_events("a", "b", "c")))
        terminal = [e for e in events if e["type"] in ("done", "error")]
        self.assertEqual(len(terminal), 1)

    def test_non_delta_events_are_ignored(self):
        stream = _FakeStream([
            _Event("message_start"),
            _Event("content_block_start"),
            *text_events("Hi"),
            _Event("content_block_stop"),
            _Event("message_stop"),
        ])
        events, _ = self.drive(stream)
        self.assertEqual([e["type"] for e in events], ["delta", "done"])

    def test_thinking_deltas_are_excluded_from_the_text_buffer(self):
        stream = _FakeStream([
            _Event("content_block_delta", _Delta("internal reasoning", type="thinking_delta")),
            *text_events("Answer"),
        ])
        events, _ = self.drive(stream)
        self.assertEqual(events[-1]["text"], "Answer")

    def test_events_without_a_delta_attribute_are_skipped(self):
        stream = _FakeStream([_Event("content_block_delta", None), *text_events("Hi")])
        events, _ = self.drive(stream)
        self.assertEqual(events[-1]["text"], "Hi")

    def test_unicode_streams_intact(self):
        events, _ = self.drive(_FakeStream(text_events("Target ₹3,050 ", "— भारतीय")))
        self.assertEqual(events[-1]["text"], "Target ₹3,050 — भारतीय")


class StreamRequestShapeTests(StreamEventsTest):
    def test_defaults_match_the_documented_model_and_budget(self):
        _, client = self.drive(_FakeStream(text_events("x")))
        kwargs = client.messages.calls[0]
        self.assertEqual(kwargs["model"], stream_claude.DEFAULT_MODEL)
        self.assertEqual(kwargs["max_tokens"], stream_claude.DEFAULT_MAX_TOKENS)

    def test_prompt_is_sent_as_a_single_user_message(self):
        _, client = self.drive(_FakeStream(text_events("x")))
        self.assertEqual(client.messages.calls[0]["messages"],
                         [{"role": "user", "content": "prompt"}])

    def test_adaptive_thinking_is_requested_by_default(self):
        _, client = self.drive(_FakeStream(text_events("x")))
        self.assertEqual(client.messages.calls[0]["thinking"], {"type": "adaptive"})

    def test_thinking_can_be_disabled(self):
        _, client = self.drive(_FakeStream(text_events("x")), thinking_mode="off")
        self.assertNotIn("thinking", client.messages.calls[0])

    def test_model_and_max_tokens_are_overridable(self):
        _, client = self.drive(_FakeStream(text_events("x")),
                               model="claude-sonnet-5", max_tokens=99)
        self.assertEqual(client.messages.calls[0]["model"], "claude-sonnet-5")
        self.assertEqual(client.messages.calls[0]["max_tokens"], 99)


class StreamFailureTests(StreamEventsTest):
    """The generator must never raise — every failure is a terminal error event."""

    def test_missing_api_key_short_circuits_before_any_client_is_built(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}), \
             patch.object(stream_claude.anthropic, "AsyncAnthropic",
                          side_effect=AssertionError("must not build a client")):
            events = run(stream_claude_events("prompt"))
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertIn("ANTHROPIC_API_KEY", events[0]["message"])

    def test_whitespace_only_api_key_is_treated_as_missing(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "   "}):
            events = run(stream_claude_events("prompt"))
        self.assertEqual(events[0]["type"], "error")

    def test_connection_failure_yields_an_error_event(self):
        events, _ = self.drive(_FakeStream([], enter_error=RuntimeError("connection reset")))
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertEqual(events[0]["raw"], "connection reset")

    def test_mid_stream_failure_keeps_the_deltas_then_errors(self):
        stream = _FakeStream(text_events("partial "), iter_error=RuntimeError("socket closed"))
        events, _ = self.drive(stream)
        self.assertEqual([e["type"] for e in events], ["delta", "error"])
        self.assertEqual(events[0]["text"], "partial ")

    def test_error_events_carry_both_friendly_and_raw_text(self):
        events, _ = self.drive(
            _FakeStream([], enter_error=RuntimeError("credit balance is too low"))
        )
        self.assertIn("credits are exhausted", events[0]["message"])
        self.assertIn("credit balance is too low", events[0]["raw"])

    def test_empty_response_is_an_error_not_a_success(self):
        events, _ = self.drive(_FakeStream([]))
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertIn("empty response", events[0]["raw"])

    def test_whitespace_only_response_is_an_error(self):
        events, _ = self.drive(_FakeStream(text_events("   ", "\n")))
        self.assertEqual(events[-1]["type"], "error")

    def test_every_error_event_has_the_full_key_set(self):
        cases = [
            _FakeStream([], enter_error=RuntimeError("boom")),
            _FakeStream([]),
            _FakeStream(text_events("x"), iter_error=RuntimeError("mid")),
        ]
        for stream in cases:
            with self.subTest(stream=stream):
                events, _ = self.drive(stream)
                err = [e for e in events if e["type"] == "error"][0]
                self.assertEqual(set(err), {"type", "message", "raw"})

    def test_generator_terminates_after_an_error(self):
        events, _ = self.drive(_FakeStream([], enter_error=RuntimeError("boom")))
        self.assertEqual(len(events), 1)


if __name__ == "__main__":
    unittest.main()

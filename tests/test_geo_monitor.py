"""Unit tests for the geo_monitor package — query building, the Gemini
classifier, email notification, and one full worker cycle.

Three hard safety guards, because this package is the one that spends money
and talks to the outside world:

  * **No Gemini calls.** `requests.post` is patched in every classifier test.
  * **No email.** `smtplib.SMTP` / `SMTP_SSL` are patched in every email test.
  * **No real .env.** `worker.load_env()` calls `load_dotenv(..., override=True)`
    against the repo's real .env, which would pull live API keys and SMTP
    credentials into the test process. It is patched out in every worker test.

`geo_alerts` writes go to the sandboxed temp DB as usual.
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import traceback
import unittest
from unittest.mock import MagicMock, patch

import requests
import requests.adapters

import tests.support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest, isolated_env

import geo_alerts
from geo_monitor import email_notify, gemini_classifier, queries, worker
from geo_monitor.gemini_classifier import (
    _extract_json,
    classify_batch,
    heuristic_suspect,
    resolve_gemini_api_key,
    should_alert,
)
from geo_monitor.queries import MACRO_QUERIES, build_monitor_queries, holding_queries


# ── Query catalogue ──────────────────────────────────────────────────────────

class MacroQueryTests(unittest.TestCase):
    def test_catalogue_is_non_empty(self):
        self.assertGreater(len(MACRO_QUERIES), 5)

    def test_no_blank_or_duplicate_entries(self):
        self.assertTrue(all(q.strip() for q in MACRO_QUERIES))
        self.assertEqual(len(MACRO_QUERIES), len(set(MACRO_QUERIES)))

    def test_covers_the_core_india_macro_drivers(self):
        blob = " ".join(MACRO_QUERIES).lower()
        for term in ("rbi", "fed", "rupee", "crude", "sebi", "monsoon", "tariff"):
            with self.subTest(term=term):
                self.assertIn(term, blob)

    def test_quotes_are_balanced(self):
        # An odd double-quote silently changes Google News operator parsing.
        for q in MACRO_QUERIES:
            with self.subTest(query=q):
                self.assertEqual(q.count('"') % 2, 0)


class HoldingQueryTests(unittest.TestCase):
    def test_symbol_only(self):
        self.assertEqual(holding_queries([{"tradingsymbol": "RELIANCE"}]), ["RELIANCE"])

    def test_name_and_symbol_are_or_ed(self):
        got = holding_queries([{"tradingsymbol": "TCS", "name": "Tata Consultancy"}])
        self.assertEqual(got, ['"Tata Consultancy" OR TCS'])

    def test_name_only_is_quoted(self):
        self.assertEqual(holding_queries([{"name": "Tata Consultancy"}]),
                         ['"Tata Consultancy"'])

    def test_alternate_field_names_are_accepted(self):
        self.assertEqual(holding_queries([{"ticker": "TCS"}]), ["TCS"])
        self.assertEqual(holding_queries([{"company_name": "Infosys"}]), ['"Infosys"'])

    def test_symbol_is_upper_cased_and_trimmed(self):
        self.assertEqual(holding_queries([{"tradingsymbol": "  reliance "}]), ["RELIANCE"])

    def test_rows_without_symbol_or_name_are_skipped(self):
        self.assertEqual(holding_queries([{}, {"tradingsymbol": ""}, {"name": "  "}]), [])

    def test_empty_input(self):
        self.assertEqual(holding_queries([]), [])

    def test_max_holdings_caps_the_query_count(self):
        rows = [{"tradingsymbol": f"SYM{i}"} for i in range(100)]
        self.assertEqual(len(holding_queries(rows)), 40)
        self.assertEqual(len(holding_queries(rows, max_holdings=5)), 5)

    def test_generators_are_accepted(self):
        gen = ({"tradingsymbol": s} for s in ("A", "B"))
        self.assertEqual(holding_queries(gen), ["A", "B"])


class BuildMonitorQueryTests(unittest.TestCase):
    def test_macro_only_by_default(self):
        self.assertEqual(build_monitor_queries(), [q.strip() for q in MACRO_QUERIES])

    def test_macro_can_be_disabled(self):
        got = build_monitor_queries([{"tradingsymbol": "TCS"}], include_macro=False)
        self.assertEqual(got, ["TCS"])

    def test_holdings_are_appended_after_macro(self):
        got = build_monitor_queries([{"tradingsymbol": "TCS"}])
        self.assertEqual(got[-1], "TCS")
        self.assertEqual(len(got), len(MACRO_QUERIES) + 1)

    def test_duplicates_are_removed_case_insensitively(self):
        got = build_monitor_queries(
            [{"tradingsymbol": "TCS"}, {"tradingsymbol": "tcs"}], include_macro=False)
        self.assertEqual(got, ["TCS"])

    def test_order_is_preserved_through_dedupe(self):
        got = build_monitor_queries(
            [{"tradingsymbol": "B"}, {"tradingsymbol": "A"}, {"tradingsymbol": "B"}],
            include_macro=False)
        self.assertEqual(got, ["B", "A"])

    def test_none_holdings_is_macro_only(self):
        self.assertEqual(build_monitor_queries(None), build_monitor_queries())

    def test_empty_holdings_list_is_macro_only(self):
        self.assertEqual(len(build_monitor_queries([])), len(MACRO_QUERIES))

    def test_everything_disabled_returns_empty(self):
        self.assertEqual(build_monitor_queries([], include_macro=False), [])

    def test_result_has_no_blank_entries(self):
        got = build_monitor_queries([{"tradingsymbol": "TCS"}])
        self.assertTrue(all(q.strip() for q in got))


# ── Heuristic prefilter ──────────────────────────────────────────────────────

class HeuristicSuspectTests(unittest.TestCase):
    def test_disruption_keyword_in_title(self):
        self.assertTrue(heuristic_suspect("RBI hikes repo rate by 25bps"))

    def test_disruption_keyword_in_description(self):
        self.assertTrue(heuristic_suspect("Market update", "New sanctions announced"))

    def test_matching_is_case_insensitive(self):
        self.assertTrue(heuristic_suspect("rbi holds rates"))

    def test_benign_headline_is_not_suspect(self):
        self.assertFalse(heuristic_suspect("Company opens new showroom in Pune"))

    def test_empty_input_is_not_suspect(self):
        self.assertFalse(heuristic_suspect(""))
        self.assertFalse(heuristic_suspect("", ""))

    def test_each_catalogued_cue_fires(self):
        for headline in ("War escalates", "New tariff on steel", "Rupee slides",
                         "Crude spikes", "SEBI probe opened", "Trading halt",
                         "Inflation rises", "FOMC meets", "Monsoon delayed"):
            with self.subTest(headline=headline):
                self.assertTrue(heuristic_suspect(headline))

    def test_word_boundaries_prevent_substring_false_positives(self):
        # "\bwar\b" must not fire on "warehouse" / "software".
        self.assertFalse(heuristic_suspect("New warehouse opens"))
        self.assertFalse(heuristic_suspect("Software rollout completed"))

    def test_holding_term_makes_an_otherwise_benign_headline_suspect(self):
        self.assertFalse(heuristic_suspect("Quiet day for the retailer"))
        self.assertTrue(heuristic_suspect("Quiet day for RELIANCE",
                                          holding_terms=["RELIANCE"]))

    def test_holding_term_matching_is_case_insensitive(self):
        self.assertTrue(heuristic_suspect("reliance in focus", holding_terms=["RELIANCE"]))

    def test_single_character_holding_terms_are_ignored(self):
        self.assertFalse(heuristic_suspect("A quiet session", holding_terms=["A"]))

    def test_blank_holding_terms_are_ignored(self):
        self.assertFalse(heuristic_suspect("Quiet session", holding_terms=["", "  ", None]))

    def test_regex_metacharacters_in_tickers_are_escaped(self):
        # M&M, BAJAJ-AUTO etc. must be matched literally, not as patterns.
        self.assertTrue(heuristic_suspect("M&M posts results", holding_terms=["M&M"]))
        self.assertFalse(heuristic_suspect("MXM posts results", holding_terms=["M&M"]))

    def test_holding_terms_do_not_match_inside_unrelated_words(self):
        self.assertFalse(heuristic_suspect("Growth in digital services",
                                           holding_terms=["IT"]))


# ── JSON extraction ──────────────────────────────────────────────────────────

class ExtractJsonTests(unittest.TestCase):
    def test_plain_array(self):
        self.assertEqual(_extract_json('[{"id": 0}]'), [{"id": 0}])

    def test_plain_object(self):
        self.assertEqual(_extract_json('{"a": 1}'), {"a": 1})

    def test_json_fence(self):
        self.assertEqual(_extract_json('```json\n[{"id": 0}]\n```'), [{"id": 0}])

    def test_bare_fence(self):
        self.assertEqual(_extract_json('```\n[{"id": 0}]\n```'), [{"id": 0}])

    def test_surrounding_whitespace(self):
        self.assertEqual(_extract_json('\n\n  {"a": 1}  \n'), {"a": 1})

    def test_array_embedded_in_prose(self):
        self.assertEqual(_extract_json('Here you go: [{"id": 0}] done'), [{"id": 0}])

    def test_object_embedded_in_prose(self):
        self.assertEqual(_extract_json('Result: {"a": 1} ok'), {"a": 1})

    def test_unicode_survives(self):
        self.assertEqual(_extract_json('{"t": "₹ भारतीय"}'), {"t": "₹ भारतीय"})

    def test_unparseable_text_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            _extract_json("no json at all")

    def test_empty_text_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            _extract_json("")


# ── API key resolution ───────────────────────────────────────────────────────

class GeminiKeyTests(unittest.TestCase):
    def setUp(self):
        self._env = isolated_env()
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_no_key_returns_empty(self):
        self.assertEqual(resolve_gemini_api_key(), "")

    def test_each_accepted_variable(self):
        for var in ("GOOGLE_AI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
            with self.subTest(var=var):
                with isolated_env({var: "k"}):
                    self.assertEqual(resolve_gemini_api_key(), "k")

    def test_precedence_order(self):
        with isolated_env({"GOOGLE_AI_API_KEY": "first",
                                     "GEMINI_API_KEY": "second",
                                     "GOOGLE_API_KEY": "third"}):
            self.assertEqual(resolve_gemini_api_key(), "first")

    def test_key_is_stripped(self):
        with isolated_env({"GEMINI_API_KEY": "  k  "}):
            self.assertEqual(resolve_gemini_api_key(), "k")


# ── Gemini classification ────────────────────────────────────────────────────

def gemini_response(payload) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]
    }
    return resp


ARTICLE = {"guid": "g1", "title": "RBI hikes repo rate", "source": "Reuters",
           "description": "Policy surprise", "url": "https://x", "query": "RBI"}


class ClassifyBatchTests(unittest.TestCase):
    def setUp(self):
        self._env = isolated_env({"GEMINI_API_KEY": "test-key"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def classify(self, payload, articles=None, **kw):
        with patch.object(gemini_classifier.requests, "post",
                          return_value=gemini_response(payload)) as post:
            out = classify_batch(articles if articles is not None else [dict(ARTICLE)], **kw)
        return out, post

    def test_missing_key_raises(self):
        with isolated_env():
            with self.assertRaises(RuntimeError):
                classify_batch([dict(ARTICLE)])

    def test_missing_key_is_checked_before_the_empty_shortcut(self):
        # Documented ordering: an empty batch with no key still raises.
        with isolated_env():
            with self.assertRaises(RuntimeError):
                classify_batch([])

    def test_empty_batch_makes_no_http_call(self):
        with patch.object(gemini_classifier.requests, "post",
                          side_effect=AssertionError("must not call Gemini")):
            self.assertEqual(classify_batch([]), [])

    def test_happy_path_merges_classification_onto_the_article(self):
        out, _ = self.classify([{
            "id": 0, "severity": "HIGH", "disruption_score": 85, "scope": "macro",
            "affected_tickers": ["HDFCBANK"], "why_disruptive": "Rate shock",
            "investor_action": "Trim duration", "confidence": 0.9,
        }])
        self.assertEqual(len(out), 1)
        got = out[0]
        self.assertEqual(got["guid"], "g1")             # original fields preserved
        self.assertEqual(got["title"], "RBI hikes repo rate")
        self.assertEqual(got["severity"], "HIGH")
        self.assertEqual(got["disruption_score"], 85)
        self.assertEqual(got["affected_tickers"], ["HDFCBANK"])
        self.assertAlmostEqual(got["confidence"], 0.9)

    def test_output_length_always_matches_input_length(self):
        arts = [dict(ARTICLE, guid=f"g{i}") for i in range(3)]
        out, _ = self.classify([{"id": 0, "severity": "HIGH"}], articles=arts)
        self.assertEqual(len(out), 3)

    def test_unclassified_articles_get_safe_defaults(self):
        arts = [dict(ARTICLE, guid=f"g{i}") for i in range(2)]
        out, _ = self.classify([{"id": 0, "severity": "CRITICAL"}], articles=arts)
        self.assertEqual(out[1]["severity"], "LOW")
        self.assertEqual(out[1]["disruption_score"], 0)
        self.assertEqual(out[1]["affected_tickers"], [])

    def test_results_are_matched_by_id_not_by_position(self):
        arts = [dict(ARTICLE, guid="g0"), dict(ARTICLE, guid="g1")]
        out, _ = self.classify(
            [{"id": 1, "severity": "CRITICAL"}, {"id": 0, "severity": "LOW"}],
            articles=arts)
        self.assertEqual(out[0]["severity"], "LOW")
        self.assertEqual(out[1]["severity"], "CRITICAL")

    def test_unknown_severity_falls_back_to_low(self):
        out, _ = self.classify([{"id": 0, "severity": "APOCALYPTIC"}])
        self.assertEqual(out[0]["severity"], "LOW")

    def test_severity_is_upper_cased(self):
        out, _ = self.classify([{"id": 0, "severity": "high"}])
        self.assertEqual(out[0]["severity"], "HIGH")

    def test_score_is_clamped_to_0_100(self):
        for raw, expected in ((999, 100), (-5, 0), (50, 50)):
            with self.subTest(raw=raw):
                out, _ = self.classify([{"id": 0, "disruption_score": raw}])
                self.assertEqual(out[0]["disruption_score"], expected)

    def test_non_numeric_score_becomes_zero(self):
        out, _ = self.classify([{"id": 0, "disruption_score": "very high"}])
        self.assertEqual(out[0]["disruption_score"], 0)

    def test_non_numeric_confidence_falls_back_to_half(self):
        out, _ = self.classify([{"id": 0, "confidence": "high"}])
        self.assertAlmostEqual(out[0]["confidence"], 0.5)

    def test_non_list_tickers_become_empty_list(self):
        out, _ = self.classify([{"id": 0, "affected_tickers": "TCS"}])
        self.assertEqual(out[0]["affected_tickers"], [])

    def test_tickers_are_upper_cased_and_blanks_dropped(self):
        out, _ = self.classify([{"id": 0, "affected_tickers": ["tcs", "", None, "infy"]}])
        self.assertEqual(out[0]["affected_tickers"], ["TCS", "INFY"])

    def test_results_wrapper_object_is_unwrapped(self):
        out, _ = self.classify({"results": [{"id": 0, "severity": "HIGH"}]})
        self.assertEqual(out[0]["severity"], "HIGH")

    def test_non_array_response_raises(self):
        with self.assertRaises(RuntimeError):
            self.classify({"unexpected": "shape"})

    def test_malformed_items_are_skipped_not_fatal(self):
        out, _ = self.classify(["not a dict", {"no_id": 1}, {"id": 0, "severity": "HIGH"}])
        self.assertEqual(out[0]["severity"], "HIGH")

    def test_http_error_raises_with_the_status(self):
        resp = MagicMock()
        resp.status_code = 429
        resp.text = "rate limited"
        with patch.object(gemini_classifier.requests, "post", return_value=resp):
            with self.assertRaises(RuntimeError) as ctx:
                classify_batch([dict(ARTICLE)])
        self.assertIn("429", str(ctx.exception))

    def test_unexpected_response_envelope_raises(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"nope": True}
        with patch.object(gemini_classifier.requests, "post", return_value=resp):
            with self.assertRaises(RuntimeError):
                classify_batch([dict(ARTICLE)])

    def test_prompt_carries_the_articles_and_watched_tickers(self):
        _, post = self.classify([{"id": 0}], holding_tickers=["TCS", "INFY"])
        prompt = post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("RBI hikes repo rate", prompt)
        self.assertIn("TCS, INFY", prompt)
        self.assertIn("NSE/BSE", prompt)

    def test_prompt_notes_when_no_holdings_are_configured(self):
        _, post = self.classify([{"id": 0}])
        prompt = post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("(none configured)", prompt)

    def test_api_key_is_sent_and_model_is_selectable(self):
        _, post = self.classify([{"id": 0}], model="gemini-custom")
        url = post.call_args[0][0]
        self.assertIn("gemini-custom", url)
        self.assertEqual(post.call_args.kwargs["headers"], {"x-goog-api-key": "test-key"})
        self.assertNotIn("test-key", url)
        self.assertNotIn("key=", url)

    def test_model_environment_is_resolved_at_call_time(self):
        with patch.dict(os.environ, {"GEMINI_MODEL": "gemini-from-env"}):
            _, post = self.classify([{"id": 0}])
        self.assertIn("gemini-from-env", post.call_args[0][0])

    def test_explicit_api_key_overrides_the_environment(self):
        with patch.object(gemini_classifier.requests, "post",
                          return_value=gemini_response([{"id": 0}])) as post:
            classify_batch([dict(ARTICLE)], api_key="explicit")
        self.assertEqual(post.call_args.kwargs["headers"], {"x-goog-api-key": "explicit"})
        self.assertNotIn("explicit", post.call_args[0][0])

    def test_descriptions_are_truncated_before_sending(self):
        long_article = dict(ARTICLE, description="x" * 5000)
        _, post = self.classify([{"id": 0}], articles=[long_article])
        prompt = post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        self.assertNotIn("x" * 501, prompt)

    def test_request_asks_for_deterministic_json(self):
        _, post = self.classify([{"id": 0}])
        cfg = post.call_args.kwargs["json"]["generationConfig"]
        self.assertEqual(cfg["responseMimeType"], "application/json")
        self.assertLessEqual(cfg["temperature"], 0.3)


# ── The Gemini key never reaches a URL, a log line or an error text ──────────

SECRET = "not-a-real-key-7f3c9a51"          # a fake; nothing here reaches the network


@contextlib.contextmanager
def _every_log_line():
    """Capture every log line of the process, tracebacks included.

    The suite runner switches logging off (`logging.disable`), so `assertLogs` sees nothing there;
    this turns it on for the block and puts everything back.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    root = logging.getLogger()
    was_disabled, was_level = root.manager.disable, root.level
    logging.disable(logging.NOTSET)
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.setLevel(was_level)
        logging.disable(was_disabled)


def _formatted(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def _chain(exc: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    while exc is not None and exc not in seen:
        seen.append(exc)
        exc = exc.__cause__ or exc.__context__
    return seen


class _Wire:
    """Stands in for the socket: `requests` prepares the real request, nothing is sent."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.requests = []

    def __call__(self, request, **kwargs):
        self.requests.append(request)
        return self.outcome(request)


def _refused_like_urllib3(request):
    # The text urllib3 and requests really produce: it carries the request's path and query.
    raise requests.exceptions.ConnectionError(
        f"HTTPSConnectionPool(host='generativelanguage.googleapis.com', port=443): Max retries exceeded "
        f"with url: {request.path_url} (Caused by NewConnectionError('Failed to establish a new connection'))",
        request=request)


def _answer(status: int, text: str):
    def outcome(request):
        resp = requests.Response()
        resp.status_code = status
        resp._content = text.encode("utf-8")
        resp.encoding = "utf-8"
        resp.request = request
        return resp
    return outcome


class GeminiKeySecrecyTests(unittest.TestCase):
    def setUp(self):
        self._env = isolated_env({"GEMINI_API_KEY": SECRET})
        self._env.start()
        self.addCleanup(self._env.stop)

    def call(self, outcome, **kw):
        wire = _Wire(outcome)
        with patch.object(requests.adapters.HTTPAdapter, "send", wire), _every_log_line() as logs:
            logging.getLogger("geo_monitor.test").debug("capture started")
            try:
                out = classify_batch([dict(ARTICLE)], **kw)
                err = None
            except Exception as e:  # noqa: BLE001 — the test inspects whatever comes out
                out, err = None, e
        self.assertIn("capture started", logs.getvalue())
        return out, err, wire, logs.getvalue()

    def assert_clean(self, err, logs, secret=SECRET):
        self.assertIsNotNone(err)
        for e in _chain(err):
            self.assertNotIn(secret, str(e))
            self.assertNotIn(secret, repr(e))
            self.assertIsNone(getattr(e, "request", None), "the failed request (and its headers) is kept alive")
        self.assertNotIn(secret, _formatted(err))
        self.assertNotIn(secret, logs)

    def test_the_key_travels_in_the_header_and_never_in_the_url(self):
        ok = json.dumps({"candidates": [{"content": {"parts": [{"text": '[{"id": 0, "severity": "HIGH"}]'}]}}]})
        out, err, wire, logs = self.call(_answer(200, ok))
        self.assertIsNone(err)
        self.assertEqual(out[0]["severity"], "HIGH")
        (sent,) = wire.requests
        self.assertEqual(sent.headers["x-goog-api-key"], SECRET)
        self.assertNotIn(SECRET, sent.url)
        self.assertNotIn("key=", sent.url)
        self.assertNotIn("?", sent.url)
        self.assertNotIn(SECRET.encode(), sent.body)
        self.assertNotIn(SECRET, logs)

    def test_a_connection_error_does_not_carry_the_key(self):
        _, err, wire, logs = self.call(_refused_like_urllib3)
        self.assertEqual(len(wire.requests), 1)
        self.assertIsInstance(err, RuntimeError)
        self.assertIn("ConnectionError", str(err))
        self.assertIn("generativelanguage.googleapis.com", str(err))      # still says what failed
        self.assert_clean(err, logs)

    def test_an_explicit_key_is_kept_out_of_a_connection_error_too(self):
        explicit = "explicit-fake-key-55aa"
        _, err, wire, logs = self.call(_refused_like_urllib3, api_key=explicit)
        self.assertEqual(wire.requests[0].headers["x-goog-api-key"], explicit)
        self.assert_clean(err, logs, secret=explicit)

    def test_an_error_text_that_names_the_key_is_scrubbed(self):
        def echo(request):
            raise requests.exceptions.ConnectionError(
                f"proxy refused: x-goog-api-key: {request.headers['x-goog-api-key']} at /generateContent?key="
                f"{request.headers['x-goog-api-key']}", request=request)
        _, err, _, logs = self.call(echo)
        self.assert_clean(err, logs)
        self.assertIn("[redacted]", str(err))

    def test_an_error_body_that_echoes_the_key_is_scrubbed_before_it_is_cut(self):
        for pad in (0, 290, 300 - len(SECRET) // 2):          # the cut at 300 must not leave half a key
            with self.subTest(pad=pad):
                _, err, _, logs = self.call(_answer(400, "x" * pad + f"API key {SECRET} is not valid" + "y" * 400))
                self.assertIn("400", str(err))
                self.assert_clean(err, logs)
                for n in range(8, len(SECRET) + 1):
                    self.assertNotIn(SECRET[:n], str(err))

    def test_a_key_that_cannot_be_a_header_is_refused_without_being_shown(self):
        for bad in ("line\nbreak-fake-key", "has space fake key", "tab\tfake-key", "clé-fake-key", "nul\x00fake-key"):
            with self.subTest(bad=bad):
                _, err, wire, logs = self.call(_refused_like_urllib3, api_key=bad)
                self.assertEqual(wire.requests, [], "nothing may be sent")
                self.assertIsInstance(err, RuntimeError)
                for piece in (bad, repr(bad)[1:-1], "break-fake-key", "fake key", "fake-key"):
                    self.assertNotIn(piece, str(err))
                    self.assertNotIn(piece, _formatted(err))
                    self.assertNotIn(piece, logs)

    def test_scrub_removes_every_configured_key_in_every_spelling(self):
        with isolated_env({"GOOGLE_AI_API_KEY": "first-fake/+key", "GEMINI_API_KEY": "second-fake-key",
                           "GOOGLE_API_KEY": "  third-fake-key  "}):
            text = gemini_classifier.scrub(
                "a first-fake/+key b second-fake-key c third-fake-key d first-fake%2F%2Bkey e extra-fake-key",
                "extra-fake-key")
        self.assertEqual(text, "a [redacted] b [redacted] c [redacted] d [redacted] e [redacted]")

    def test_scrub_without_a_key_changes_nothing(self):
        with isolated_env():
            self.assertEqual(gemini_classifier.scrub("nothing to hide"), "nothing to hide")
            self.assertEqual(gemini_classifier.scrub("nothing to hide", "", None), "nothing to hide")


class WorkerKeySecrecyTests(SandboxedDBTest):
    db_modules = (geo_alerts,)

    def setUp(self):
        super().setUp()
        p = patch.object(worker, "load_env", lambda: None)      # never the developer's real env file
        p.start()
        self.addCleanup(p.stop)
        self._env = isolated_env({"GOOGLE_AI_API_KEY": SECRET})
        self._env.start()
        self.addCleanup(self._env.stop)

    def cycle(self, target, name, value):
        client = FakeNewsClient(FakeSearchResult([FakeArticle("g1", "RBI hikes repo rate")]))
        with patch.object(worker, "_holdings_for_queries", return_value=([], [])), \
             patch.object(worker, "try_send_alert_email", return_value="email not configured"), \
             patch.object(target, name, value), \
             _every_log_line() as logs:
            stats = worker.run_once(client=client)
            logging.getLogger("geo_monitor.worker").info("Cycle done: %s", stats)      # what run_forever logs
        return stats, logs.getvalue()

    def test_a_failed_gemini_call_leaves_no_key_in_the_log_or_the_stats(self):
        wire = _Wire(_refused_like_urllib3)
        stats, logs = self.cycle(requests.adapters.HTTPAdapter, "send", wire)
        self.assertEqual(len(wire.requests), 1)
        self.assertEqual(wire.requests[0].headers["x-goog-api-key"], SECRET)
        self.assertEqual(stats["classified"], 0)
        self.assertEqual(len(stats["errors"]), 1)
        self.assertIn("ConnectionError", stats["errors"][0])
        self.assertIn("Gemini classification failed", logs)
        self.assertNotIn(SECRET, logs)
        self.assertNotIn(SECRET, repr(stats))

    def test_an_error_that_names_the_key_is_scrubbed_by_the_worker_as_well(self):
        def boom(*_a, **_k):
            try:
                raise ValueError(f"inner {SECRET}")
            except ValueError as inner:
                raise RuntimeError(f"outer {SECRET}") from inner
        stats, logs = self.cycle(worker, "classify_batch", boom)
        self.assertEqual(stats["errors"], ["outer [redacted]"])
        self.assertIn("Gemini classification failed", logs)
        self.assertIn("Traceback", logs)                     # the trace is still there to debug with
        self.assertIn("inner [redacted]", logs)
        self.assertNotIn(SECRET, logs)
        self.assertNotIn(SECRET, repr(stats))

    def test_a_crashed_cycle_is_logged_without_the_key(self):
        def crash(*_a, **_k):
            raise RuntimeError(f"cycle broke near {SECRET}")
        with patch.object(worker, "run_once", crash), _every_log_line() as logs:
            worker.run_forever(once=True)
        text = logs.getvalue()
        self.assertIn("Monitor cycle crashed", text)
        self.assertIn("Traceback", text)
        self.assertIn("cycle broke near [redacted]", text)
        self.assertNotIn(SECRET, text)


class ShouldAlertTests(unittest.TestCase):
    def test_high_and_critical_always_alert(self):
        self.assertTrue(should_alert({"severity": "HIGH", "disruption_score": 0}))
        self.assertTrue(should_alert({"severity": "CRITICAL", "disruption_score": 0}))

    def test_severity_is_case_insensitive(self):
        self.assertTrue(should_alert({"severity": "high"}))

    def test_low_severity_with_a_high_score_still_alerts(self):
        self.assertTrue(should_alert({"severity": "LOW", "disruption_score": 90}))

    def test_score_floor_is_inclusive(self):
        self.assertTrue(should_alert({"severity": "LOW", "disruption_score": 70}))
        self.assertFalse(should_alert({"severity": "LOW", "disruption_score": 69}))

    def test_custom_score_floor(self):
        self.assertTrue(should_alert({"severity": "LOW", "disruption_score": 50},
                                     score_floor=50))

    def test_quiet_classification_does_not_alert(self):
        self.assertFalse(should_alert({"severity": "MEDIUM", "disruption_score": 30}))

    def test_empty_and_malformed_classifications_do_not_alert(self):
        self.assertFalse(should_alert({}))
        self.assertFalse(should_alert({"severity": None, "disruption_score": None}))
        self.assertFalse(should_alert({"disruption_score": "high"}))


# ── Email notification ───────────────────────────────────────────────────────

ALERT = {
    "id": 1, "severity": "HIGH", "disruption_score": 85, "scope": "macro",
    "title": "RBI hikes repo rate by 50bps", "source": "Reuters",
    "url": "https://example.com/a", "published_at": "2025-08-06T10:00:00Z",
    "why_disruptive": "Rate shock repricing.", "investor_action": "Trim duration.",
    "confidence": 0.9, "affected_tickers": ["HDFCBANK", "SBIN"],
}

SMTP_ENV = {
    "ALERT_EMAIL_TO": "me@example.com",
    "SMTP_FROM": "bot@example.com",
    "SMTP_HOST": "smtp.example.com",
    "SMTP_PORT": "587",
    "SMTP_USER": "bot",
    "SMTP_PASSWORD": "pw",
}


class EmailConfigTests(unittest.TestCase):
    def test_fully_configured(self):
        with isolated_env(SMTP_ENV):
            self.assertTrue(email_notify.email_configured())

    def test_each_required_variable_is_required(self):
        for missing in ("ALERT_EMAIL_TO", "SMTP_FROM", "SMTP_HOST"):
            with self.subTest(missing=missing):
                env = dict(SMTP_ENV)
                env[missing] = ""
                with isolated_env(env):
                    self.assertFalse(email_notify.email_configured())

    def test_unconfigured(self):
        with isolated_env():
            self.assertFalse(email_notify.email_configured())


class SendAlertEmailTests(unittest.TestCase):
    def setUp(self):
        self._env = isolated_env(SMTP_ENV)
        self._env.start()
        self.addCleanup(self._env.stop)

    def send(self, alert=None, env=None):
        with patch.dict(os.environ, env or {}), \
             patch.object(email_notify.smtplib, "SMTP") as smtp, \
             patch.object(email_notify.smtplib, "SMTP_SSL") as smtp_ssl:
            email_notify.send_alert_email(alert or dict(ALERT))
        return smtp, smtp_ssl

    def sent_message(self, smtp):
        return smtp.return_value.__enter__.return_value.send_message.call_args[0][0]

    def test_starttls_path_on_port_587(self):
        smtp, smtp_ssl = self.send()
        smtp.assert_called_once()
        smtp_ssl.assert_not_called()
        smtp.return_value.__enter__.return_value.starttls.assert_called_once()

    def test_starttls_failure_aborts_before_login_or_delivery(self):
        with patch.object(email_notify.smtplib, "SMTP") as smtp:
            connection = smtp.return_value.__enter__.return_value
            connection.starttls.side_effect = email_notify.smtplib.SMTPException(
                "TLS unavailable"
            )
            with self.assertRaises(email_notify.smtplib.SMTPException):
                email_notify.send_alert_email(dict(ALERT))
        connection.login.assert_not_called()
        connection.send_message.assert_not_called()

    def test_implicit_tls_path_on_port_465(self):
        smtp, smtp_ssl = self.send(env={"SMTP_PORT": "465"})
        smtp_ssl.assert_called_once()
        smtp.assert_not_called()

    def test_default_port_is_587_when_unset(self):
        smtp, _ = self.send(env={"SMTP_PORT": ""})
        self.assertEqual(smtp.call_args[0][1], 587)

    def test_login_is_performed_when_credentials_are_present(self):
        smtp, _ = self.send()
        smtp.return_value.__enter__.return_value.login.assert_called_once_with("bot", "pw")

    def test_login_is_skipped_without_a_user(self):
        smtp, _ = self.send(env={"SMTP_USER": ""})
        smtp.return_value.__enter__.return_value.login.assert_not_called()

    def test_subject_carries_severity_and_headline(self):
        smtp, _ = self.send()
        subject = self.sent_message(smtp)["Subject"]
        self.assertIn("[STOCKIFY HIGH]", subject)
        self.assertIn("RBI hikes repo rate", subject)

    def test_long_headline_is_truncated_in_the_subject(self):
        smtp, _ = self.send(dict(ALERT, title="X" * 500))
        self.assertLess(len(self.sent_message(smtp)["Subject"]), 200)

    def test_envelope_addresses(self):
        smtp, _ = self.send()
        msg = self.sent_message(smtp)
        self.assertEqual(msg["To"], "me@example.com")
        self.assertEqual(msg["From"], "bot@example.com")

    def test_body_carries_the_actionable_fields(self):
        smtp, _ = self.send()
        body = self.sent_message(smtp).get_content()
        for token in ("HIGH", "85/100", "macro", "HDFCBANK, SBIN",
                      "Rate shock repricing.", "Trim duration.",
                      "https://example.com/a"):
            with self.subTest(token=token):
                self.assertIn(token, body)

    def test_missing_tickers_render_a_dash(self):
        smtp, _ = self.send(dict(ALERT, affected_tickers=[]))
        self.assertIn("Affected tickers: —", self.sent_message(smtp).get_content())

    def test_sparse_alert_does_not_raise(self):
        smtp, _ = self.send({"title": "Something happened"})
        self.assertIn("Something happened", self.sent_message(smtp).get_content())

    def test_unconfigured_raises(self):
        with patch.dict(os.environ, {"SMTP_HOST": ""}), \
             patch.object(email_notify.smtplib, "SMTP") as smtp:
            with self.assertRaises(RuntimeError):
                email_notify.send_alert_email(dict(ALERT))
        smtp.assert_not_called()


class TrySendAlertEmailTests(unittest.TestCase):
    def test_unconfigured_returns_the_skip_sentinel(self):
        with isolated_env():
            self.assertEqual(email_notify.try_send_alert_email(dict(ALERT)),
                             "email not configured (skipped)")

    def test_success_returns_none(self):
        with isolated_env(SMTP_ENV), \
             patch.object(email_notify, "send_alert_email"):
            self.assertIsNone(email_notify.try_send_alert_email(dict(ALERT)))

    def test_smtp_failure_is_returned_not_raised(self):
        with isolated_env(SMTP_ENV), \
             patch.object(email_notify, "send_alert_email",
                          side_effect=RuntimeError("connection refused")):
            self.assertEqual(email_notify.try_send_alert_email(dict(ALERT)),
                             "connection refused")

    def test_skip_sentinel_matches_the_string_the_worker_special_cases(self):
        # worker.run_once compares against this literal to decide whether the
        # skip is an error worth reporting.
        with isolated_env():
            sentinel = email_notify.try_send_alert_email(dict(ALERT))
        self.assertEqual(sentinel, "email not configured (skipped)")


# ── Worker cycle ─────────────────────────────────────────────────────────────

class FakeArticle:
    def __init__(self, guid, title, description="", source="Reuters",
                 link="https://example.com/a", published_at="2025-08-06", query="RBI"):
        self.guid = guid
        self.title = title
        self.description = description
        self.source = source
        self.link = link
        self.loaded_url = link
        self.published_at = published_at
        self.query = query


class FakeSearchResult:
    def __init__(self, articles, errors=None):
        self.articles = articles
        self.errors = errors or []
        self.unique_count = len(articles)


class FakeNewsClient:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def search_many(self, queries, **kwargs):
        self.calls.append((queries, kwargs))
        return self._result


class WorkerCycleTests(SandboxedDBTest):
    db_modules = (geo_alerts,)

    def setUp(self):
        super().setUp()
        # Never load the developer's real .env into the test process.
        p = patch.object(worker, "load_env", lambda: None)
        p.start()
        self.addCleanup(p.stop)
        self._env = isolated_env({"GEMINI_API_KEY": "test-key"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def run_cycle(self, articles, classified=None, holdings=(), tickers=(),
                  dry_run=False, email_error="email not configured (skipped)"):
        client = FakeNewsClient(FakeSearchResult(list(articles)))
        classify = patch.object(
            worker, "classify_batch",
            return_value=list(classified) if classified is not None else [])
        with patch.object(worker, "_holdings_for_queries",
                          return_value=(list(holdings), list(tickers))), \
             patch.object(worker, "try_send_alert_email", return_value=email_error), \
             classify as m_classify:
            stats = worker.run_once(client=client, dry_run=dry_run)
        return stats, client, m_classify

    def test_no_articles_yields_a_zeroed_cycle(self):
        stats, _, m_classify = self.run_cycle([])
        self.assertEqual(stats["fetched"], 0)
        self.assertEqual(stats["new"], 0)
        self.assertEqual(stats["suspects"], 0)
        self.assertEqual(stats["alerts"], 0)
        m_classify.assert_not_called()

    def test_stats_keys_are_stable(self):
        stats, _, _ = self.run_cycle([])
        self.assertEqual(
            set(stats),
            {"queries", "fetched", "new", "suspects", "classified", "alerts",
             "emailed", "errors"})

    def test_macro_queries_are_issued(self):
        _, client, _ = self.run_cycle([])
        issued = client.calls[0][0]
        self.assertEqual(issued, build_monitor_queries([], include_macro=True))

    def test_holdings_extend_the_query_set(self):
        _, client, _ = self.run_cycle(
            [], holdings=[{"tradingsymbol": "TCS"}], tickers=["TCS"])
        self.assertIn("TCS", client.calls[0][0])

    def test_benign_articles_are_marked_seen_but_not_classified(self):
        stats, _, m_classify = self.run_cycle(
            [FakeArticle("g1", "New showroom opens in Pune")])
        self.assertEqual(stats["new"], 1)
        self.assertEqual(stats["suspects"], 0)
        m_classify.assert_not_called()
        self.assertTrue(geo_alerts.is_seen("g1"))

    def test_suspect_articles_are_classified(self):
        stats, _, m_classify = self.run_cycle(
            [FakeArticle("g1", "RBI hikes repo rate")],
            classified=[{"guid": "g1", "title": "RBI hikes repo rate",
                         "severity": "HIGH", "disruption_score": 85}])
        self.assertEqual(stats["suspects"], 1)
        self.assertEqual(stats["classified"], 1)
        m_classify.assert_called_once()
        self.assertTrue(geo_alerts.is_seen("g1"))

    def test_a_second_cycle_over_the_same_article_is_a_no_op(self):
        art = FakeArticle("g1", "RBI hikes repo rate")
        first, _, _ = self.run_cycle([art], classified=[
            {"guid": "g1", "severity": "HIGH", "title": "RBI hikes repo rate"}])
        second, _, m_classify = self.run_cycle([art])
        self.assertEqual(first["new"], 1)
        self.assertEqual(second["new"], 0)
        self.assertEqual(second["suspects"], 0)
        m_classify.assert_not_called()

    def test_alerting_classification_is_persisted(self):
        stats, _, _ = self.run_cycle(
            [FakeArticle("g1", "RBI hikes repo rate")],
            classified=[{"guid": "g1", "title": "RBI hikes repo rate",
                         "source": "Reuters", "url": "https://x",
                         "severity": "CRITICAL", "disruption_score": 95,
                         "scope": "macro", "affected_tickers": ["HDFCBANK"],
                         "why_disruptive": "Shock", "investor_action": "Trim",
                         "confidence": 0.9}])
        self.assertEqual(stats["alerts"], 1)
        saved = geo_alerts.list_alerts()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].severity, "CRITICAL")
        self.assertEqual(saved[0].affected_tickers, ["HDFCBANK"])

    def test_quiet_classification_is_not_persisted(self):
        stats, _, _ = self.run_cycle(
            [FakeArticle("g1", "RBI holds repo rate")],
            classified=[{"guid": "g1", "severity": "LOW", "disruption_score": 10}])
        self.assertEqual(stats["alerts"], 0)
        self.assertEqual(geo_alerts.list_alerts(), [])

    def test_dry_run_counts_alerts_without_persisting(self):
        stats, _, _ = self.run_cycle(
            [FakeArticle("g1", "RBI hikes repo rate")],
            classified=[{"guid": "g1", "severity": "HIGH", "disruption_score": 90}],
            dry_run=True)
        self.assertEqual(stats["alerts"], 1)
        self.assertEqual(geo_alerts.list_alerts(), [])
        self.assertFalse(geo_alerts.is_seen("g1"))

    def test_missing_gemini_key_stops_before_classification(self):
        with isolated_env():
            stats, _, m_classify = self.run_cycle(
                [FakeArticle("g1", "RBI hikes repo rate")])
        self.assertEqual(stats["suspects"], 1)
        self.assertEqual(stats["classified"], 0)
        m_classify.assert_not_called()
        self.assertTrue(any("Gemini" in e for e in stats["errors"]))
        self.assertFalse(geo_alerts.is_seen("g1"))

    def test_classification_failure_is_recorded_not_raised(self):
        client = FakeNewsClient(FakeSearchResult([FakeArticle("g1", "RBI hikes repo rate")]))
        with patch.object(worker, "_holdings_for_queries", return_value=([], [])), \
             patch.object(worker, "classify_batch", side_effect=RuntimeError("gemini 500")):
            stats = worker.run_once(client=client)
        self.assertEqual(stats["alerts"], 0)
        self.assertTrue(any("gemini 500" in e for e in stats["errors"]))
        self.assertFalse(geo_alerts.is_seen("g1"))

    def test_news_fetch_errors_are_surfaced(self):
        client = FakeNewsClient(FakeSearchResult([], errors=["rss timeout"]))
        with patch.object(worker, "_holdings_for_queries", return_value=([], [])):
            stats = worker.run_once(client=client)
        self.assertIn("rss timeout", stats["errors"])

    def test_successful_email_marks_the_alert_emailed(self):
        stats, _, _ = self.run_cycle(
            [FakeArticle("g1", "RBI hikes repo rate")],
            classified=[{"guid": "g1", "severity": "HIGH", "disruption_score": 90}],
            email_error=None)
        self.assertEqual(stats["emailed"], 1)
        self.assertIsNotNone(geo_alerts.list_alerts()[0].emailed_at)

    def test_email_failure_does_not_lose_the_alert(self):
        stats, _, _ = self.run_cycle(
            [FakeArticle("g1", "RBI hikes repo rate")],
            classified=[{"guid": "g1", "severity": "HIGH", "disruption_score": 90}],
            email_error="connection refused")
        self.assertEqual(stats["alerts"], 1)
        self.assertEqual(stats["emailed"], 0)
        self.assertEqual(len(geo_alerts.list_alerts()), 1)
        self.assertTrue(any("connection refused" in e for e in stats["errors"]))

    def test_duplicate_alert_guid_is_not_double_counted(self):
        geo_alerts.save_alert(
            severity="HIGH", disruption_score=90, scope="macro", title="t",
            source="s", url="u", published_at="p", guid="g1", why_disruptive="w",
            investor_action="i", confidence=0.5, affected_tickers=[], article={})
        stats, _, _ = self.run_cycle(
            [FakeArticle("g1", "RBI hikes repo rate")],
            classified=[{"guid": "g1", "severity": "HIGH", "disruption_score": 90}])
        self.assertEqual(stats["alerts"], 0)
        self.assertEqual(len(geo_alerts.list_alerts()), 1)

    def test_articles_without_a_guid_fall_back_to_the_link(self):
        art = FakeArticle("", "RBI hikes repo rate")
        art.link = "https://example.com/fallback"
        stats, _, _ = self.run_cycle([art])
        self.assertEqual(stats["new"], 1)
        self.assertTrue(geo_alerts.is_seen("https://example.com/fallback"))

    def test_classification_is_chunked_for_large_batches(self):
        arts = [FakeArticle(f"g{i}", "RBI hikes repo rate") for i in range(30)]
        stats, _, m_classify = self.run_cycle(arts, classified=[])
        self.assertEqual(stats["suspects"], 30)
        self.assertEqual(m_classify.call_count, 3)   # ceil(30 / 12)
        for call in m_classify.call_args_list:
            self.assertLessEqual(len(call[0][0]), 12)


class WorkerConfigTests(unittest.TestCase):
    def test_interval_default(self):
        with isolated_env():
            self.assertEqual(worker._interval_sec(), 600)

    def test_interval_from_env(self):
        with isolated_env({"GEO_MONITOR_INTERVAL_SEC": "900"}):
            self.assertEqual(worker._interval_sec(), 900)

    def test_interval_has_a_60_second_floor(self):
        with isolated_env({"GEO_MONITOR_INTERVAL_SEC": "5"}):
            self.assertEqual(worker._interval_sec(), 60)

    def test_malformed_interval_falls_back_to_the_default(self):
        with isolated_env({"GEO_MONITOR_INTERVAL_SEC": "soon"}):
            self.assertEqual(worker._interval_sec(), 600)

    def test_window_default(self):
        with isolated_env():
            self.assertEqual(worker._open_ended(), "1h")

    def test_window_from_env_is_stripped(self):
        with isolated_env({"GEO_MONITOR_WHEN": "  6h "}):
            self.assertEqual(worker._open_ended(), "6h")

    def test_blank_window_falls_back(self):
        with isolated_env({"GEO_MONITOR_WHEN": ""}):
            self.assertEqual(worker._open_ended(), "1h")


class HoldingsResolutionTests(unittest.TestCase):
    def test_portfolio_failure_degrades_to_macro_only(self):
        import portfolio_sync
        with patch.object(portfolio_sync, "cached_snapshot",
                          side_effect=RuntimeError("kite down")):
            holdings, tickers = worker._holdings_for_queries()
        self.assertEqual(holdings, [])
        self.assertEqual(tickers, [])

    def test_zero_quantity_holdings_are_excluded(self):
        import portfolio_sync
        from portfolio_sync import Holding, PortfolioSnapshot

        def h(sym, qty):
            return Holding(tradingsymbol=sym, exchange="NSE", quantity=qty,
                           average_price=1.0, last_price=1.0, pnl=0.0,
                           day_change_pct=0.0, instrument_token=1)

        snap = PortfolioSnapshot(holdings=[h("RELIANCE", 10), h("TCS", 0)])
        with patch.object(portfolio_sync, "cached_snapshot", return_value=snap):
            holdings, tickers = worker._holdings_for_queries()
        self.assertEqual(tickers, ["RELIANCE"])
        self.assertEqual([r["tradingsymbol"] for r in holdings], ["RELIANCE"])


if __name__ == "__main__":
    unittest.main()

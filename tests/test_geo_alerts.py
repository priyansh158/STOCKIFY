"""Unit tests for geo_alerts — the seen-article ledger and disruption alert store.

Two dedupe guarantees carry the always-on monitor:

  * `mark_seen` must report first-sight exactly once per guid, or the monitor
    re-analyses (and re-bills) the same article on every poll.
  * `save_alert` must reject a second alert for the same guid, or the user gets
    duplicate emails.

Both are enforced at the SQLite level, so they are tested through the real DB
(sandboxed to a temp dir, never the developer's geo_alerts.sqlite).
"""
from __future__ import annotations

import json
import time
import unittest
from unittest.mock import patch

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import geo_alerts


def alert(**overrides) -> dict:
    payload = dict(
        severity="HIGH",
        disruption_score=78,
        scope="SECTOR",
        title="Red Sea shipping disrupted",
        source="Reuters",
        url="https://example.com/a",
        published_at="2025-08-06T10:00:00Z",
        guid="guid-1",
        why_disruptive="Freight costs spike for import-dependent refiners.",
        investor_action="Watch OMC margins.",
        confidence=0.82,
        affected_tickers=["RELIANCE", "BPCL"],
        article={"title": "Red Sea shipping disrupted", "source": "Reuters"},
    )
    payload.update(overrides)
    return payload


class GeoAlertsTest(SandboxedDBTest):
    db_modules = (geo_alerts,)


class InitTests(GeoAlertsTest):
    def test_init_db_is_idempotent(self):
        geo_alerts.init_db()
        geo_alerts.init_db()
        self.assertEqual(geo_alerts.list_alerts(), [])

    def test_db_file_lands_in_sandbox(self):
        support.assert_sandboxed(geo_alerts._db_path())
        self.assertEqual(geo_alerts._db_path().name, "geo_alerts.sqlite")


class SeenArticleTests(GeoAlertsTest):
    def test_first_sight_returns_true(self):
        self.assertTrue(geo_alerts.mark_seen(guid="g1", title="T"))

    def test_second_sight_returns_false(self):
        geo_alerts.mark_seen(guid="g1")
        self.assertFalse(geo_alerts.mark_seen(guid="g1"))

    def test_repeated_polls_only_report_first_sight_once(self):
        results = [geo_alerts.mark_seen(guid="g1") for _ in range(10)]
        self.assertEqual(results.count(True), 1)

    def test_distinct_guids_are_independent(self):
        self.assertTrue(geo_alerts.mark_seen(guid="g1"))
        self.assertTrue(geo_alerts.mark_seen(guid="g2"))

    def test_empty_guid_is_rejected(self):
        self.assertFalse(geo_alerts.mark_seen(guid=""))
        self.assertFalse(geo_alerts.mark_seen(guid=None))

    def test_is_seen_reflects_mark_seen(self):
        self.assertFalse(geo_alerts.is_seen("g1"))
        geo_alerts.mark_seen(guid="g1")
        self.assertTrue(geo_alerts.is_seen("g1"))

    def test_is_seen_for_empty_guid_is_false(self):
        self.assertFalse(geo_alerts.is_seen(""))
        self.assertFalse(geo_alerts.is_seen(None))

    def test_metadata_is_persisted(self):
        geo_alerts.mark_seen(guid="g1", title="T", url="u", source="Reuters",
                             published_at="2025-08-06", query="crude")
        row = geo_alerts._connect().execute(
            "SELECT * FROM seen_articles WHERE guid = 'g1'").fetchone()
        self.assertEqual(row["title"], "T")
        self.assertEqual(row["source"], "Reuters")
        self.assertEqual(row["query"], "crude")

    def test_first_seen_at_is_recorded(self):
        before = int(time.time())
        geo_alerts.mark_seen(guid="g1")
        row = geo_alerts._connect().execute(
            "SELECT first_seen_at FROM seen_articles WHERE guid = 'g1'").fetchone()
        self.assertGreaterEqual(row["first_seen_at"], before)

    def test_re_marking_does_not_overwrite_the_original_metadata(self):
        geo_alerts.mark_seen(guid="g1", title="original")
        geo_alerts.mark_seen(guid="g1", title="changed")
        row = geo_alerts._connect().execute(
            "SELECT title FROM seen_articles WHERE guid = 'g1'").fetchone()
        self.assertEqual(row["title"], "original")

    def test_hostile_guid_is_stored_literally(self):
        evil = "'; DROP TABLE seen_articles;--"
        self.assertTrue(geo_alerts.mark_seen(guid=evil))
        self.assertTrue(geo_alerts.is_seen(evil))

    def test_unicode_guid_and_title(self):
        self.assertTrue(geo_alerts.mark_seen(guid="गाइड-१", title="भारतीय बाज़ार"))
        self.assertTrue(geo_alerts.is_seen("गाइड-१"))


class SaveAlertTests(GeoAlertsTest):
    def test_returns_a_positive_id(self):
        self.assertGreater(geo_alerts.save_alert(**alert()), 0)

    def test_duplicate_guid_is_rejected(self):
        self.assertIsNotNone(geo_alerts.save_alert(**alert(guid="g1")))
        self.assertIsNone(geo_alerts.save_alert(**alert(guid="g1", title="different")))

    def test_duplicate_rejection_does_not_create_a_row(self):
        geo_alerts.save_alert(**alert(guid="g1"))
        geo_alerts.save_alert(**alert(guid="g1"))
        self.assertEqual(len(geo_alerts.list_alerts()), 1)

    def test_distinct_guids_both_persist(self):
        geo_alerts.save_alert(**alert(guid="g1"))
        geo_alerts.save_alert(**alert(guid="g2"))
        self.assertEqual(len(geo_alerts.list_alerts()), 2)

    def test_empty_guid_is_rejected_without_inserting(self):
        self.assertIsNone(geo_alerts.save_alert(**alert(guid="")))
        self.assertEqual(geo_alerts.list_alerts(), [])

    def test_round_trip_preserves_every_field(self):
        rid = geo_alerts.save_alert(**alert())
        got = geo_alerts.get_alert(rid)
        self.assertEqual(got.severity, "HIGH")
        self.assertEqual(got.disruption_score, 78)
        self.assertEqual(got.scope, "SECTOR")
        self.assertEqual(got.title, "Red Sea shipping disrupted")
        self.assertEqual(got.source, "Reuters")
        self.assertEqual(got.url, "https://example.com/a")
        self.assertEqual(got.guid, "guid-1")
        self.assertAlmostEqual(got.confidence, 0.82)
        self.assertEqual(got.affected_tickers, ["RELIANCE", "BPCL"])
        self.assertEqual(got.article["source"], "Reuters")

    def test_new_alerts_are_unemailed_and_unacknowledged(self):
        got = geo_alerts.get_alert(geo_alerts.save_alert(**alert()))
        self.assertIsNone(got.emailed_at)
        self.assertIsNone(got.acknowledged_at)

    def test_created_at_is_stamped(self):
        before = int(time.time())
        got = geo_alerts.get_alert(geo_alerts.save_alert(**alert()))
        self.assertGreaterEqual(got.created_at, before)

    def test_empty_ticker_list_round_trips(self):
        got = geo_alerts.get_alert(geo_alerts.save_alert(**alert(affected_tickers=[])))
        self.assertEqual(got.affected_tickers, [])

    def test_none_collections_normalise_to_empty(self):
        got = geo_alerts.get_alert(
            geo_alerts.save_alert(**alert(affected_tickers=None, article=None)))
        self.assertEqual(got.affected_tickers, [])
        self.assertEqual(got.article, {})

    def test_nested_article_payload_round_trips(self):
        article = {"title": "T", "meta": {"tags": ["shipping", "crude"], "n": 3}}
        got = geo_alerts.get_alert(geo_alerts.save_alert(**alert(article=article)))
        self.assertEqual(got.article, article)

    def test_unicode_content_round_trips(self):
        got = geo_alerts.get_alert(
            geo_alerts.save_alert(**alert(title="भारतीय बाज़ार ₹", why_disruptive="—")))
        self.assertEqual(got.title, "भारतीय बाज़ार ₹")

    def test_numeric_strings_are_coerced(self):
        got = geo_alerts.get_alert(
            geo_alerts.save_alert(**alert(disruption_score="78", confidence="0.5")))
        self.assertEqual(got.disruption_score, 78)
        self.assertAlmostEqual(got.confidence, 0.5)

    def test_corrupt_ticker_json_degrades_to_empty_list(self):
        rid = geo_alerts.save_alert(**alert())
        conn = geo_alerts._connect()
        conn.execute("UPDATE geo_alerts SET affected_tickers_json = ? WHERE id = ?",
                     ("[not json", rid))
        conn.commit()
        self.assertEqual(geo_alerts.get_alert(rid).affected_tickers, [])

    def test_non_list_ticker_json_degrades_to_empty_list(self):
        rid = geo_alerts.save_alert(**alert())
        conn = geo_alerts._connect()
        conn.execute("UPDATE geo_alerts SET affected_tickers_json = ? WHERE id = ?",
                     (json.dumps({"a": 1}), rid))
        conn.commit()
        self.assertEqual(geo_alerts.get_alert(rid).affected_tickers, [])

    def test_to_dict_is_json_serialisable_and_complete(self):
        d = geo_alerts.get_alert(geo_alerts.save_alert(**alert())).to_dict()
        json.dumps(d)
        self.assertEqual(
            set(d),
            {"id", "created_at", "severity", "disruption_score", "scope", "title",
             "source", "url", "published_at", "guid", "why_disruptive",
             "investor_action", "confidence", "affected_tickers", "article",
             "emailed_at", "acknowledged_at"},
        )


class GetAlertTests(GeoAlertsTest):
    def test_unknown_id_returns_none(self):
        self.assertIsNone(geo_alerts.get_alert(999_999))

    def test_zero_id_returns_none(self):
        self.assertIsNone(geo_alerts.get_alert(0))


class MarkEmailedTests(GeoAlertsTest):
    def test_sets_emailed_at(self):
        rid = geo_alerts.save_alert(**alert())
        before = int(time.time())
        geo_alerts.mark_emailed(rid)
        self.assertGreaterEqual(geo_alerts.get_alert(rid).emailed_at, before)

    def test_unknown_id_is_a_safe_no_op(self):
        geo_alerts.mark_emailed(999_999)

    def test_does_not_touch_acknowledgement(self):
        rid = geo_alerts.save_alert(**alert())
        geo_alerts.mark_emailed(rid)
        self.assertIsNone(geo_alerts.get_alert(rid).acknowledged_at)

    def test_only_affects_the_target_alert(self):
        a = geo_alerts.save_alert(**alert(guid="g1"))
        b = geo_alerts.save_alert(**alert(guid="g2"))
        geo_alerts.mark_emailed(a)
        self.assertIsNotNone(geo_alerts.get_alert(a).emailed_at)
        self.assertIsNone(geo_alerts.get_alert(b).emailed_at)


class AcknowledgeTests(GeoAlertsTest):
    def test_returns_true_and_stamps(self):
        rid = geo_alerts.save_alert(**alert())
        before = int(time.time())
        self.assertTrue(geo_alerts.acknowledge(rid))
        self.assertGreaterEqual(geo_alerts.get_alert(rid).acknowledged_at, before)

    def test_unknown_id_returns_false(self):
        self.assertFalse(geo_alerts.acknowledge(999_999))

    def test_re_acknowledging_still_reports_success(self):
        rid = geo_alerts.save_alert(**alert())
        geo_alerts.acknowledge(rid)
        self.assertTrue(geo_alerts.acknowledge(rid))


class ListAlertsTests(GeoAlertsTest):
    def seed_severities(self):
        for i, sev in enumerate(("LOW", "MEDIUM", "HIGH", "CRITICAL")):
            with patch.object(geo_alerts.time, "time", return_value=1000 + i):
                geo_alerts.save_alert(**alert(guid=f"g-{sev}", severity=sev))

    def test_empty_store(self):
        self.assertEqual(geo_alerts.list_alerts(), [])

    def test_newest_first(self):
        self.seed_severities()
        self.assertEqual([a.severity for a in geo_alerts.list_alerts()],
                         ["CRITICAL", "HIGH", "MEDIUM", "LOW"])

    def test_no_filter_returns_everything(self):
        self.seed_severities()
        self.assertEqual(len(geo_alerts.list_alerts()), 4)

    def test_min_severity_high_includes_critical(self):
        self.seed_severities()
        got = {a.severity for a in geo_alerts.list_alerts(min_severity="HIGH")}
        self.assertEqual(got, {"HIGH", "CRITICAL"})

    def test_min_severity_critical_is_the_narrowest(self):
        self.seed_severities()
        got = {a.severity for a in geo_alerts.list_alerts(min_severity="CRITICAL")}
        self.assertEqual(got, {"CRITICAL"})

    def test_min_severity_medium(self):
        self.seed_severities()
        got = {a.severity for a in geo_alerts.list_alerts(min_severity="MEDIUM")}
        self.assertEqual(got, {"MEDIUM", "HIGH", "CRITICAL"})

    def test_min_severity_low_includes_everything(self):
        self.seed_severities()
        self.assertEqual(len(geo_alerts.list_alerts(min_severity="LOW")), 4)

    def test_min_severity_is_case_insensitive(self):
        self.seed_severities()
        self.assertEqual(len(geo_alerts.list_alerts(min_severity="high")), 2)

    def test_unrecognised_severity_disables_the_filter(self):
        # Falls through to "no WHERE clause" rather than returning nothing.
        self.seed_severities()
        self.assertEqual(len(geo_alerts.list_alerts(min_severity="BOGUS")), 4)

    def test_none_severity_disables_the_filter(self):
        self.seed_severities()
        self.assertEqual(len(geo_alerts.list_alerts(min_severity=None)), 4)

    def test_limit(self):
        self.seed_severities()
        self.assertEqual(len(geo_alerts.list_alerts(limit=2)), 2)

    def test_offset_paginates_without_overlap(self):
        self.seed_severities()
        page1 = geo_alerts.list_alerts(limit=2, offset=0)
        page2 = geo_alerts.list_alerts(limit=2, offset=2)
        self.assertEqual([a.severity for a in page1], ["CRITICAL", "HIGH"])
        self.assertEqual([a.severity for a in page2], ["MEDIUM", "LOW"])

    def test_offset_past_the_end(self):
        self.seed_severities()
        self.assertEqual(geo_alerts.list_alerts(offset=100), [])

    def test_limit_and_severity_filter_combine(self):
        self.seed_severities()
        got = geo_alerts.list_alerts(limit=1, min_severity="HIGH")
        self.assertEqual([a.severity for a in got], ["CRITICAL"])

    def test_rows_are_fully_hydrated(self):
        geo_alerts.save_alert(**alert())
        a = geo_alerts.list_alerts()[0]
        self.assertEqual(a.affected_tickers, ["RELIANCE", "BPCL"])
        self.assertEqual(a.article["source"], "Reuters")


class MonitorFlowTests(GeoAlertsTest):
    """The seen-ledger and the alert store are independent gates."""

    def test_marking_seen_does_not_create_an_alert(self):
        geo_alerts.mark_seen(guid="g1")
        self.assertEqual(geo_alerts.list_alerts(), [])

    def test_alerting_does_not_mark_the_article_seen(self):
        geo_alerts.save_alert(**alert(guid="g1"))
        self.assertFalse(geo_alerts.is_seen("g1"))

    def test_a_full_poll_cycle_is_idempotent(self):
        # Two polls over the same article: analysed once, alerted once.
        analysed = 0
        for _ in range(2):
            if geo_alerts.mark_seen(guid="g1", title="T"):
                analysed += 1
                geo_alerts.save_alert(**alert(guid="g1"))
        self.assertEqual(analysed, 1)
        self.assertEqual(len(geo_alerts.list_alerts()), 1)


if __name__ == "__main__":
    unittest.main()

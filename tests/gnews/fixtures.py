"""Minimal Google News RSS fixture for offline unit tests."""

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Google News</title>
    <item>
      <title>RBI keeps repo rate unchanged — Economic Times</title>
      <link>https://news.google.com/rss/articles/CBMiTestGuidAAA?oc=5</link>
      <guid isPermaLink="false">CBMiTestGuidAAA</guid>
      <pubDate>Wed, 06 Aug 2025 10:15:00 GMT</pubDate>
      <description><![CDATA[<a href="https://news.google.com/rss/articles/CBMiTestGuidAAA">RBI keeps repo rate unchanged</a>&nbsp;&nbsp;<font>The Economic Times</font>]]></description>
      <source url="https://economictimes.indiatimes.com">The Economic Times</source>
    </item>
    <item>
      <title>Fed signals slower cuts — Reuters</title>
      <link>https://news.google.com/rss/articles/CBMiTestGuidBBB?oc=5</link>
      <guid isPermaLink="false">CBMiTestGuidBBB</guid>
      <pubDate>Wed, 06 Aug 2025 08:00:00 GMT</pubDate>
      <description>Federal Reserve commentary on rates</description>
      <source url="https://www.reuters.com">Reuters</source>
    </item>
    <item>
      <title>RBI keeps repo rate unchanged — Economic Times</title>
      <link>https://news.google.com/rss/articles/CBMiTestGuidAAA?oc=5</link>
      <guid isPermaLink="false">CBMiTestGuidAAA</guid>
      <pubDate>Wed, 06 Aug 2025 10:15:00 GMT</pubDate>
      <description>duplicate should be dropped by guid</description>
      <source url="https://economictimes.indiatimes.com">The Economic Times</source>
    </item>
  </channel>
</rss>
"""

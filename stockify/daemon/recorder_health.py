"""When the recorder reports its health, and when a report is too old to be current (D-139).

The recorder (inside the market-day loop) writes one `recorder_status` row every HEALTH_EVERY_S seconds from the pre-open
to the close, and nothing outside that window: the daemon sleeps between trading days. A row older than STALE_AFTER_S is
therefore from a recorder that stopped (or from the last session). Both the writer and the API read this one place."""
from __future__ import annotations

HEALTH_EVERY_S = 5.0
MISSED_REPORTS_FOR_STALE = 12            # a minute of silence at the 5 s interval: a stalled write loop, not a slow disk
STALE_AFTER_S = HEALTH_EVERY_S * MISSED_REPORTS_FOR_STALE

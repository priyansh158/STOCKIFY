// Runs the pure helpers of web/src/lib/monitor.ts on fixed inputs and prints one JSON object. Called by
// tests/stockify/test_geo_check_checks.py, which holds the hand-checked answers.
import { checkBlockedReason, lastCheckWords } from "../../web/src/lib/monitor.ts";

const when = "13 Nov 2026, 10:05 IST";
console.log(JSON.stringify({
  unread: lastCheckWords(null, "—"),
  none: lastCheckWords({ checking: false }, "—"),
  page: lastCheckWords({ checking: false, last_check_at: 1, last_by: "page", last_plain: "8 stories checked, no new alerts." }, when),
  background: lastCheckWords({ checking: false, last_check_at: 1, last_by: "background", last_plain: null }, when),
  blocked: [checkBlockedReason(false, false), checkBlockedReason(false, true), checkBlockedReason(true, true)],
}));

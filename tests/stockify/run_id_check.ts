// Prints runIdTail answers for a few ids as one JSON array; called by tests/stockify/test_run_id_tail.py.
import { runIdTail } from "../../web/src/lib/runid.ts";

process.stdout.write(JSON.stringify([
  runIdTail("orb_reliance_minute-ada883a0319e", "orb_reliance_minute"),
  runIdTail("orb_reliance_minute-ada883a0319e-2", "orb_reliance_minute"),
  runIdTail("orb_reliance_minute-ada883a0319e-rerun", "orb_reliance_minute"),
  runIdTail("orb_reliance_minute_cpp-367f6479f8c0", "orb_reliance_minute"),
  runIdTail("abc", null),
  runIdTail("x-", "x"),
]));

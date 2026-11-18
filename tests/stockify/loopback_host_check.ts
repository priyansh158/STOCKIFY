// Runs web/src/lib/loopbackHost.ts on the Host header values given as one JSON array in argv[2] and
// prints one JSON array of booleans, in order. Node ≥ 22.18 runs this file directly (type stripping);
// called by tests/stockify/test_web_loopback_host.py.
//   argv[3] absent:       isLoopbackHost(host)
//   argv[3] = "setting":  remoteHostSetting(value) for each value (printed as the string or null)
//   argv[3] = "request":  argv[2] is a list of [headers, setting]; prints refusesRequest(new Headers(headers), setting)
//   argv[3] = JSON value: isServedHost(host, that STOCKIFY_REMOTE_HOST value)
import { isLoopbackHost, isServedHost, refusesRequest, remoteHostSetting } from "../../web/src/lib/loopbackHost.ts";

const values = JSON.parse(process.argv[2]);
const mode = process.argv[3];
let out: unknown[];
if (mode === undefined) out = (values as (string | null)[]).map((h) => isLoopbackHost(h));
else if (mode === "setting") out = (values as (string | null)[]).map((v) => remoteHostSetting(v));
else if (mode === "request") {
  out = (values as [Record<string, string>, string | null][]).map(([headers, setting]) =>
    refusesRequest(new Headers(headers), setting),
  );
} else {
  const setting = JSON.parse(mode) as string | null;
  out = (values as (string | null)[]).map((h) => isServedHost(h, setting));
}
process.stdout.write(JSON.stringify(out));

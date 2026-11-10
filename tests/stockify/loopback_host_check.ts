// Runs web/src/lib/loopbackHost.ts on the Host header values given as one JSON array in argv[2] and
// prints one JSON array of booleans, in order. Node ≥ 22.18 runs this file directly (type stripping);
// called by tests/stockify/test_web_loopback_host.py.
import { isLoopbackHost } from "../../web/src/lib/loopbackHost.ts";

const hosts = JSON.parse(process.argv[2]) as (string | null)[];
process.stdout.write(JSON.stringify(hosts.map((h) => isLoopbackHost(h))));

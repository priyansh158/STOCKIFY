/**
 * Which `Host` header values name this machine.
 *
 * The web server serves the owner's browser on this machine and nothing else. A page at a DNS
 * name that an attacker rebinds to 127.0.0.1 would otherwise be served, and could read the API
 * through the /api rewrite (which forwards any Host) and through server-rendered pages (which
 * fetch the API themselves). `src/proxy.ts` applies this rule to every request before anything
 * renders or is proxied. There is no setting to relax it.
 */

const LOOPBACK_HOSTS = ["127.0.0.1", "localhost", "[::1]"];

// A host name or IPv4 literal, or a bracketed IPv6 literal, each with an optional port.
const HOST_WITH_PORT = /^(\[[0-9a-f:.]+\]|[a-z0-9.-]+)(?::\d{1,5})?$/i;

/** True when `host` (the raw `Host` header) names a loopback host, with or without a port. */
export function isLoopbackHost(host: string | null | undefined): boolean {
  if (typeof host !== "string") return false;
  // Only the whitespace HTTP allows around a header value (SP, HTAB) is ignored; nothing else is.
  const match = HOST_WITH_PORT.exec(host.replace(/^[ \t]+|[ \t]+$/g, ""));
  if (!match) return false;
  return LOOPBACK_HOSTS.includes(match[1].toLowerCase());
}

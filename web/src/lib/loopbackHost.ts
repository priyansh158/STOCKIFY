/**
 * Which `Host` header values name this machine.
 *
 * The web server serves the owner's browser on this machine and nothing else. A page at a DNS
 * name that an attacker rebinds to 127.0.0.1 would otherwise be served, and could read the API
 * through the /api rewrite (which forwards any Host) and through server-rendered pages (which
 * fetch the API themselves). `src/proxy.ts` applies this rule to every request before anything
 * renders or is proxied.
 *
 * One setting widens it, and only by one name: `STOCKIFY_REMOTE_HOST`, the laptop's private
 * Tailscale address (`<machine>.<tailnet>.ts.net`), so the owner can do the Kite login from their
 * phone (docs/superpowers/specs/2026-11-18-phone-login-tailscale-design.md). `remoteHostSetting`
 * checks the value by the same rule as the API (stockify_paths.remote_host); anything else is
 * ignored and only loopback is served. The API keeps its own guards (server/local_only.py).
 */

const LOOPBACK_HOSTS = ["127.0.0.1", "localhost", "[::1]"];

// A host name or IPv4 literal, or a bracketed IPv6 literal, each with an optional port.
const HOST_WITH_PORT = /^(\[[0-9a-f:.]+\]|[a-z0-9.-]+)(?::\d{1,5})?$/i;

// `<machine>.<tailnet>.ts.net`, lower case; each label 1-63 letters, digits or hyphens, no hyphen at either end.
const TS_LABEL = "[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?";
const REMOTE_HOST_RULE = new RegExp(`^(?:${TS_LABEL}\\.){2,}ts\\.net$`);

/** The Tailscale host name in `raw` (the setting's value), or null when it is unset, empty or not a valid one. */
export function remoteHostSetting(raw: string | null | undefined): string | null {
  if (typeof raw !== "string") return null;
  const value = raw.trim();
  if (!value || value.length > 253 || !REMOTE_HOST_RULE.test(value)) return null;
  return value;
}

/** True when `host` (the raw `Host` header) names a loopback host, with or without a port. */
export function isLoopbackHost(host: string | null | undefined): boolean {
  return servedName(host, null);
}

/**
 * `isLoopbackHost`, or `host` names the setting's host exactly (with or without a port).
 * `remoteSetting` is the raw setting; it is checked here, so an invalid one only serves loopback.
 */
export function isServedHost(host: string | null | undefined, remoteSetting: string | null | undefined): boolean {
  return servedName(host, remoteHostSetting(remoteSetting));
}

function servedName(host: string | null | undefined, remoteHost: string | null): boolean {
  if (typeof host !== "string") return false;
  // Only the whitespace HTTP allows around a header value (SP, HTAB) is ignored; nothing else is.
  const match = HOST_WITH_PORT.exec(host.replace(/^[ \t]+|[ \t]+$/g, ""));
  if (!match) return false;
  if (LOOPBACK_HOSTS.includes(match[1].toLowerCase())) return true;
  // The remote name is compared exactly: a browser sends it in lower case, as the setting holds it.
  return remoteHost !== null && match[1] === remoteHost;
}

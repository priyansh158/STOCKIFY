/**
 * Next 16 request interception (the `proxy` file convention): runs before any page renders and
 * before the /api rewrite proxies a request to the API.
 *
 * A request whose `Host` is not loopback (or the one Tailscale host in STOCKIFY_REMOTE_HOST, when
 * it is set and valid) is refused with 400 and nothing else happens: no page, no proxied /api call,
 * no asset. See `src/lib/loopbackHost.ts` for why. No `matcher` on purpose: the check covers every route.
 */
import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { isServedHost } from "@/lib/loopbackHost";

export function proxy(request: NextRequest) {
  if (!isServedHost(request.headers.get("host"), process.env.STOCKIFY_REMOTE_HOST)) {
    return new NextResponse("Invalid host header", { status: 400 });
  }
  return NextResponse.next();
}

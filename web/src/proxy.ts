/**
 * Next 16 request interception (the `proxy` file convention): runs before any page renders and
 * before the /api rewrite proxies a request to the API.
 *
 * A request whose `Host` is not loopback is refused with 400 and nothing else happens: no page,
 * no proxied /api call, no asset. See `src/lib/loopbackHost.ts` for why. No `matcher` on purpose:
 * the check covers every route.
 */
import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { isLoopbackHost } from "@/lib/loopbackHost";

export function proxy(request: NextRequest) {
  if (!isLoopbackHost(request.headers.get("host"))) {
    return new NextResponse("Invalid host header", { status: 400 });
  }
  return NextResponse.next();
}

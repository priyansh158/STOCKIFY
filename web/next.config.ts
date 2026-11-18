import type { NextConfig } from "next";

import { remoteHostSetting } from "./src/lib/loopbackHost";

const FASTAPI_ORIGIN = process.env.STOCKIFY_API_ORIGIN ?? "http://127.0.0.1:8001";

// The laptop's Tailscale address, for the Kite login from the phone (README, "Log in from your phone").
// Unset or invalid = loopback only, as before.
const REMOTE_HOST = remoteHostSetting(process.env.STOCKIFY_REMOTE_HOST);
if (process.env.STOCKIFY_REMOTE_HOST?.trim() && !REMOTE_HOST) {
  console.warn(
    "STOCKIFY_REMOTE_HOST is ignored: it must be one Tailscale host name such as laptop.tailnet.ts.net " +
      "(lower case, no scheme, no port, no wildcard); only loopback is served.",
  );
}

const nextConfig: NextConfig = {
  // No dev-mode route badge on screen (D-141); compile and runtime errors are still shown.
  devIndicators: false,
  // The dev server refuses its own assets to other origins; the one Tailscale host is let through, exactly.
  allowedDevOrigins: REMOTE_HOST ? [REMOTE_HOST] : [],
  // Forward /api/* to the FastAPI backend during dev so the browser hits a
  // same-origin URL (avoids CORS preflight noise). In production the same
  // rewrite pattern works behind a reverse proxy.
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${FASTAPI_ORIGIN}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;

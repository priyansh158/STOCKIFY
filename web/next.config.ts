import type { NextConfig } from "next";

const FASTAPI_ORIGIN = process.env.STOCKIFY_API_ORIGIN ?? "http://127.0.0.1:8001";

const nextConfig: NextConfig = {
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

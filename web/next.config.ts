import type { NextConfig } from "next";

// Production: a static export (ADR-002), served by nginx next to the API on the same origin.
// Development: `next dev` proxies /api to the local FastAPI, so the browser also sees one origin
// and the SameSite=Strict session cookie works exactly as in production.
const isDev = process.env.NODE_ENV !== "production";

const config: NextConfig = {
  reactStrictMode: true,
  devIndicators: false,
  trailingSlash: true, // /login/ -> out/login/index.html: plain files nginx can serve
  skipTrailingSlashRedirect: true, // never redirect /api/x -> /api/x/ (FastAPI has no slashes)
  images: { unoptimized: true },
  ...(isDev
    ? {
        async rewrites() {
          return [{ source: "/api/:path*", destination: "http://127.0.0.1:8000/api/:path*" }];
        },
      }
    : { output: "export" as const }),
};

export default config;

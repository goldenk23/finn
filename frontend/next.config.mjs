// FINN_STATIC_EXPORT=1 -> static export to frontend/out, served by FastAPI on the same origin (see Dockerfile).
// Otherwise (local dev / next start) /api/* is proxied to the backend.
const isStaticExport = process.env.FINN_STATIC_EXPORT === "1";

// One env var for the backend location, shared with src/lib/api.ts. Empty = same origin.
const apiUrl = process.env.NEXT_PUBLIC_API_URL || "";

/** @type {import('next').NextConfig} */
const nextConfig = isStaticExport
  ? {
      reactStrictMode: true,
      // Static export has no server, so rewrites and the proxy timeout don't apply here.
      output: "export",
    }
  : {
      reactStrictMode: true,
      experimental: {
        // ponytail: raise Next.js rewrite proxy timeout from 30s default to 180s so cold Pull multi-PDF scrape + Gemini Map-Reduce synthesis never gets cut off with a bare HTTP 500.
        proxyTimeout: 180000,
      },
      async rewrites() {
        // ponytail: proxy /api/* through the Next.js dev server so the browser stays same-origin; ceiling: long SSE streams buffer in the Next rewrite proxy; production uses the static export served by FastAPI instead.
        return [
          {
            source: "/api/:path*",
            destination: `${apiUrl || "http://127.0.0.1:8000"}/api/:path*`,
          },
        ];
      },
    };

export default nextConfig;

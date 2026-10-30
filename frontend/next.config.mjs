/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  experimental: {
    // ponytail: raise Next.js rewrite proxy timeout from 30s default to 180s so cold Pull multi-PDF scrape + Gemini Map-Reduce synthesis never gets cut off with a bare HTTP 500.
    proxyTimeout: 180000,
  },
  async rewrites() {
    // ponytail: proxy /api/* through Next.js dev/prod server so mobile/desktop clients hit a single origin without CORS issues; ceiling: long SSE streams buffer in Next rewrite proxy; upgrade path: Caddy reverse proxy in production (see ARCHITECTURE.md).
    const backendUrl = process.env.NEXT_PUBLIC_API_BASE || "http://127.0.0.1:8000";
    return [
      {
        source: "/api/:path*",
        destination: `${backendUrl}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;


/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
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


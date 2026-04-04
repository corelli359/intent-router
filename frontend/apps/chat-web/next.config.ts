import type { NextConfig } from "next";

const routerApiOrigin = process.env.INTENT_ROUTER_API_ORIGIN ?? "http://127.0.0.1:8011";

const nextConfig: NextConfig = {
  basePath: "/chat",
  reactStrictMode: true,
  transpilePackages: ["@intent-router/ui", "@intent-router/api-client", "@intent-router/shared-types"],
  env: {
    NEXT_PUBLIC_ROUTER_BASE_URL: "/chat/api/router"
  },
  async rewrites() {
    return [
      {
        source: "/api/router/:path*",
        destination: `${routerApiOrigin}/api/router/:path*`
      }
    ];
  }
};

export default nextConfig;

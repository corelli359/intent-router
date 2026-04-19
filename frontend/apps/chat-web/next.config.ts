import path from "node:path";
import type { NextConfig } from "next";

const chatBasePath = process.env.INTENT_CHAT_BASE_PATH ?? "/chat";
const routerApiOrigin = process.env.INTENT_ROUTER_API_ORIGIN ?? "http://127.0.0.1:8011";
const routerBrowserBaseUrl = process.env.NEXT_PUBLIC_ROUTER_BASE_URL ?? `${chatBasePath}/api/router`;

const nextConfig: NextConfig = {
  basePath: chatBasePath,
  output: "standalone",
  outputFileTracingRoot: path.join(__dirname, "../.."),
  reactStrictMode: true,
  transpilePackages: ["@intent-router/ui", "@intent-router/api-client", "@intent-router/shared-types"],
  env: {
    NEXT_PUBLIC_ROUTER_BASE_URL: routerBrowserBaseUrl
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

import path from "node:path";
import type { NextConfig } from "next";

const adminBasePath = process.env.INTENT_ADMIN_BASE_PATH ?? "/admin";
const adminApiOrigin = process.env.INTENT_ADMIN_API_ORIGIN ?? "http://127.0.0.1:8012";
const adminBrowserBaseUrl = process.env.NEXT_PUBLIC_ADMIN_BASE_URL ?? `${adminBasePath}/api/admin`;

const nextConfig: NextConfig = {
  basePath: adminBasePath,
  output: "standalone",
  outputFileTracingRoot: path.join(__dirname, "../.."),
  reactStrictMode: true,
  transpilePackages: ["@intent-router/ui", "@intent-router/api-client", "@intent-router/shared-types"],
  env: {
    NEXT_PUBLIC_ADMIN_BASE_URL: adminBrowserBaseUrl
  },
  async rewrites() {
    return [
      {
        source: "/api/admin/:path*",
        destination: `${adminApiOrigin}/api/admin/:path*`
      }
    ];
  }
};

export default nextConfig;

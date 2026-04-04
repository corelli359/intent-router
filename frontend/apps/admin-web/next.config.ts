import type { NextConfig } from "next";

const adminApiOrigin = process.env.INTENT_ADMIN_API_ORIGIN ?? "http://127.0.0.1:8012";

const nextConfig: NextConfig = {
  basePath: "/admin",
  reactStrictMode: true,
  transpilePackages: ["@intent-router/ui", "@intent-router/api-client", "@intent-router/shared-types"],
  env: {
    NEXT_PUBLIC_ADMIN_BASE_URL: "/admin/api/admin"
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

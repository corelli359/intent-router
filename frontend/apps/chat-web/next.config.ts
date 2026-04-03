import type { NextConfig } from "next";

const backendOrigin = process.env.INTENT_BACKEND_ORIGIN ?? "http://127.0.0.1:8011";

const nextConfig: NextConfig = {
  transpilePackages: ["@intent-router/ui", "@intent-router/api-client", "@intent-router/shared-types"],
  async rewrites() {
    return [
      {
        source: "/api/router/:path*",
        destination: `${backendOrigin}/api/router/:path*`
      },
      {
        source: "/api/admin/:path*",
        destination: `${backendOrigin}/api/admin/:path*`
      }
    ];
  }
};

export default nextConfig;

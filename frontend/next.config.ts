import type { NextConfig } from "next";

const allowedDevOrigin = process.env.TARA_ALLOWED_DEV_ORIGIN?.trim();

const nextConfig: NextConfig = {
  agentRules: false,
  allowedDevOrigins: allowedDevOrigin ? [allowedDevOrigin] : [],
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: "http://127.0.0.1:8000/api/:path*",
      },
    ];
  },
};

export default nextConfig;

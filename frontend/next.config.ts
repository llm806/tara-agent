import type { NextConfig } from "next";

const allowedDevOrigin = process.env.TARA_ALLOWED_DEV_ORIGIN?.trim();
const apiProxyTarget = process.env.TARA_API_PROXY_TARGET?.trim() || "http://127.0.0.1:8000";
const allowedDevOrigins = [
  "localhost",
  "127.0.0.1",
  ...(allowedDevOrigin ? [allowedDevOrigin] : []),
];

const nextConfig: NextConfig = {
  agentRules: false,
  // 生成容器运行所需的文件；本地开发仍可使用 pnpm dev。
  output: "standalone",
  allowedDevOrigins,
  async rewrites() {
    // 容器中的 /api 由 Caddy 转发，避免请求容器自己的 8000 端口。
    if (process.env.TARA_API_PROXY_ENABLED === "false") return [];
    return [
      {
        source: "/api/:path*",
        destination: `${apiProxyTarget}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;

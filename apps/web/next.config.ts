import type { NextConfig } from "next";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

type SecurityHeader = Readonly<{
  key: string;
  value: string;
}>;

function repositoryRoot(moduleUrl: string): string {
  // Vitest represents import.meta.url as a Windows path while Node and Next
  // expose a file URL. Supporting both keeps the config testable without
  // making Turbopack depend on the process working directory.
  const modulePath = /^[A-Za-z]:[\\/]/.test(moduleUrl) ? moduleUrl : fileURLToPath(moduleUrl);
  return resolve(dirname(modulePath), "../..");
}

export function createSecurityHeaders(): SecurityHeader[] {
  return [
    { key: "X-Content-Type-Options", value: "nosniff" },
    { key: "X-Frame-Options", value: "DENY" },
    { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
    {
      key: "Permissions-Policy",
      value:
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()",
    },
  ];
}

const nextConfig: NextConfig = {
  output: "standalone",
  // Keep the default deploy path, while allowing an isolated ignored build
  // directory for QA when another process is reading `.next` on Windows.
  distDir: process.env.NEXT_DIST_DIR ?? ".next",
  reactStrictMode: true,
  typedRoutes: true,
  turbopack: {
    root: repositoryRoot(import.meta.url),
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: createSecurityHeaders(),
      },
    ];
  },
};

export default nextConfig;

import type { NextConfig } from "next";

type SecurityHeader = Readonly<{
  key: string;
  value: string;
}>;

function configuredApiOrigin(apiUrl: string | undefined): string | undefined {
  if (!apiUrl) return undefined;

  try {
    const url = new URL(apiUrl);
    return url.protocol === "http:" || url.protocol === "https:" ? url.origin : undefined;
  } catch {
    return undefined;
  }
}

/**
 * The static Next.js header cannot carry a per-request nonce. Consequently,
 * Next's inline bootstrap and React's inline styles need the two narrowly
 * scoped unsafe-inline allowances below. All network destinations remain
 * explicit, and unsafe-eval is enabled only for the development compiler.
 */
export function buildContentSecurityPolicy(
  apiUrl = process.env.NEXT_PUBLIC_API_URL,
  development = process.env.NODE_ENV === "development",
): string {
  const apiOrigin = configuredApiOrigin(apiUrl);
  const connectSources = ["'self'", ...(apiOrigin ? [apiOrigin] : []), ...(development ? ["ws:", "wss:"] : [])];
  const scriptSources = ["'self'", "'unsafe-inline'", ...(development ? ["'unsafe-eval'"] : [])];

  return [
    "default-src 'self'",
    "base-uri 'self'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "frame-src 'none'",
    "form-action 'self'",
    `script-src ${scriptSources.join(" ")}`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    `connect-src ${connectSources.join(" ")}`,
    "worker-src 'self' blob:",
    "media-src 'self' blob:",
    "manifest-src 'self'",
  ].join("; ");
}

export function createSecurityHeaders(
  apiUrl = process.env.NEXT_PUBLIC_API_URL,
  development = process.env.NODE_ENV === "development",
): SecurityHeader[] {
  return [
    { key: "Content-Security-Policy", value: buildContentSecurityPolicy(apiUrl, development) },
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
  reactStrictMode: true,
  typedRoutes: true,
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

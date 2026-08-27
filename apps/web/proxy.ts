import { Buffer } from "node:buffer";
import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import { loadPublicRuntimeConfig, type PublicRuntimeConfig } from "./lib/runtime-config";

// Next Image's `fill` mode emits this one fixed positioning attribute during
// SSR. Keep the exception content-addressed instead of permitting arbitrary
// inline styles. Runtime canvas sizing is applied through the CSSOM and does
// not require a CSP source expression.
export const NEXT_IMAGE_FILL_STYLE_HASH =
  "'sha256-ZDrxqUOB4m/L0JWL/+gS52g1CRH0l/qwMhjTw5Z/Fsc='" as const;

function configuredOrigin(
  value: string | undefined,
  { httpsOnly = false }: { httpsOnly?: boolean } = {},
): string | undefined {
  if (!value) return undefined;

  try {
    const url = new URL(value);
    const allowedProtocol = httpsOnly
      ? url.protocol === "https:"
      : url.protocol === "http:" || url.protocol === "https:";
    return allowedProtocol && !url.username && !url.password ? url.origin : undefined;
  } catch {
    return undefined;
  }
}

export function createNonce(uuid = crypto.randomUUID()): string {
  return Buffer.from(uuid).toString("base64");
}

export function buildContentSecurityPolicy(
  nonce: string,
  runtimeConfig: PublicRuntimeConfig = loadPublicRuntimeConfig(),
  development = process.env.NODE_ENV === "development",
): string {
  const configuredOrigins = [
    configuredOrigin(runtimeConfig.apiUrl),
    configuredOrigin(runtimeConfig.oidcIssuer, { httpsOnly: true }),
  ].filter((origin): origin is string => Boolean(origin));
  const connectSources = [
    "'self'",
    ...new Set(configuredOrigins),
    ...(development ? ["ws:", "wss:"] : []),
  ];
  const nonceSource = `'nonce-${nonce}'`;
  const scriptSources = [
    "'self'",
    nonceSource,
    "'strict-dynamic'",
    ...(development ? ["'unsafe-eval'"] : []),
  ];
  const styleSources = [
    "'self'",
    ...(development ? ["'unsafe-inline'"] : [nonceSource]),
  ];

  return [
    "default-src 'self'",
    "base-uri 'self'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "frame-src 'none'",
    "form-action 'self'",
    `script-src ${scriptSources.join(" ")}`,
    "script-src-attr 'none'",
    `style-src ${styleSources.join(" ")}`,
    ...(development
      ? []
      : [`style-src-attr 'unsafe-hashes' ${NEXT_IMAGE_FILL_STYLE_HASH}`]),
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    `connect-src ${connectSources.join(" ")}`,
    "worker-src 'self' blob:",
    "media-src 'self' blob:",
    "manifest-src 'self'",
  ].join("; ");
}

export function proxy(request: NextRequest) {
  const nonce = createNonce();
  // Parsing is intentionally repeated at the document boundary: an invalid
  // production runtime must fail before a weaker CSP or client config is served.
  const runtimeConfig = loadPublicRuntimeConfig();
  const contentSecurityPolicy = buildContentSecurityPolicy(nonce, runtimeConfig);
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", contentSecurityPolicy);

  const response = NextResponse.next({
    request: { headers: requestHeaders },
  });
  response.headers.set("Content-Security-Policy", contentSecurityPolicy);
  return response;
}

export const config = {
  matcher: [
    {
      source: "/((?!api|_next/static|_next/image|favicon.ico).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};

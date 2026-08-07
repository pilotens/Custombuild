import { describe, expect, it } from "vitest";
import nextConfig, { buildContentSecurityPolicy, createSecurityHeaders } from "./next.config";

describe("Next.js security headers", () => {
  it("applies the security policy to every route", async () => {
    expect(nextConfig.headers).toBeTypeOf("function");

    const routes = await nextConfig.headers!();

    expect(routes).toHaveLength(1);
    expect(routes[0]?.source).toBe("/:path*");
    expect(routes[0]?.headers).toEqual(createSecurityHeaders());
  });

  it("allows only the configured API origin for production connections", () => {
    const policy = buildContentSecurityPolicy("https://api.example.test/v1", false);

    expect(policy).toContain("connect-src 'self' https://api.example.test");
    expect(policy).not.toContain("https://api.example.test/v1");
    expect(policy).not.toContain("'unsafe-eval'");
    expect(policy).not.toContain("ws:");
    expect(policy).toContain("worker-src 'self' blob:");
    expect(policy).toContain("img-src 'self' data: blob:");
  });

  it("supports the Next.js development compiler without weakening production", () => {
    const policy = buildContentSecurityPolicy("http://localhost:8000", true);

    expect(policy).toContain("script-src 'self' 'unsafe-inline' 'unsafe-eval'");
    expect(policy).toContain("connect-src 'self' http://localhost:8000 ws: wss:");
  });

  it("ignores malformed or non-HTTP API destinations", () => {
    expect(buildContentSecurityPolicy("javascript:alert(1)", false)).toContain("connect-src 'self'");
    expect(buildContentSecurityPolicy("javascript:alert(1)", false)).not.toContain("javascript:");
    expect(buildContentSecurityPolicy("not a URL", false)).toContain("connect-src 'self'");
  });

  it("sets anti-sniffing, anti-framing, referrer, and device policies", () => {
    expect(Object.fromEntries(createSecurityHeaders("https://api.example.test", false).map(({ key, value }) => [key, value]))).toMatchObject({
      "X-Content-Type-Options": "nosniff",
      "X-Frame-Options": "DENY",
      "Referrer-Policy": "strict-origin-when-cross-origin",
      "Permissions-Policy": expect.stringContaining("camera=()"),
      "Content-Security-Policy": expect.stringContaining("frame-ancestors 'none'"),
    });
  });
});

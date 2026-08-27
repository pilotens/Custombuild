import { describe, expect, it } from "vitest";
import nextConfig, { createSecurityHeaders } from "./next.config";

describe("Next.js security headers", () => {
  it("applies the security policy to every route", async () => {
    expect(nextConfig.headers).toBeTypeOf("function");

    const routes = await nextConfig.headers!();

    expect(routes).toHaveLength(1);
    expect(routes[0]?.source).toBe("/:path*");
    expect(routes[0]?.headers).toEqual(createSecurityHeaders());
  });

  it("sets anti-sniffing, anti-framing, referrer, and device policies", () => {
    expect(Object.fromEntries(createSecurityHeaders().map(({ key, value }) => [key, value]))).toMatchObject({
      "X-Content-Type-Options": "nosniff",
      "X-Frame-Options": "DENY",
      "Referrer-Policy": "strict-origin-when-cross-origin",
      "Permissions-Policy": expect.stringContaining("camera=()"),
    });
  });

  it("leaves the request-specific CSP to proxy.ts", () => {
    expect(createSecurityHeaders()).not.toContainEqual(
      expect.objectContaining({ key: "Content-Security-Policy" }),
    );
  });
});

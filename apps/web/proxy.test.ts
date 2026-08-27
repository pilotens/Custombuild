import { createHash } from "node:crypto";
import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  buildContentSecurityPolicy,
  config,
  createNonce,
  NEXT_IMAGE_FILL_STYLE_HASH,
  proxy,
} from "./proxy";

const TEST_NONCE = createNonce("00000000-0000-4000-8000-000000000000");
const NEXT_IMAGE_FILL_STYLE =
  "position:absolute;height:100%;width:100%;left:0;top:0;right:0;bottom:0;color:transparent";

afterEach(() => vi.unstubAllEnvs());

function directive(policy: string, name: string): string {
  return policy.split("; ").find((candidate) => candidate.startsWith(`${name} `)) ?? "";
}

describe("request-specific Content Security Policy", () => {
  it("binds the only static style exception to Next Image's emitted fill style", () => {
    const digest = createHash("sha256").update(NEXT_IMAGE_FILL_STYLE).digest("base64");
    expect(NEXT_IMAGE_FILL_STYLE_HASH).toBe(`'sha256-${digest}'`);
  });

  it("uses a production nonce without either unsafe escape hatch", () => {
    const policy = buildContentSecurityPolicy(
      TEST_NONCE,
      {
        appEnv: "production",
        apiUrl: "https://api.example.test",
        oidcIssuer: "https://identity.example.test/realms/custombuild",
      },
      false,
    );

    expect(policy).not.toContain("'unsafe-inline'");
    expect(policy).not.toContain("'unsafe-eval'");
    expect(directive(policy, "script-src")).toBe(
      `script-src 'self' 'nonce-${TEST_NONCE}' 'strict-dynamic'`,
    );
    expect(directive(policy, "style-src")).toBe(
      `style-src 'self' 'nonce-${TEST_NONCE}'`,
    );
    expect(policy).toContain("script-src-attr 'none'");
    expect(directive(policy, "style-src-attr")).toBe(
      `style-src-attr 'unsafe-hashes' ${NEXT_IMAGE_FILL_STYLE_HASH}`,
    );
    expect(directive(policy, "style-src-attr").match(/'sha256-/g)).toHaveLength(1);
    expect(directive(policy, "style-src-attr")).not.toContain("'unsafe-inline'");
    expect(policy).toContain(
      "connect-src 'self' https://api.example.test https://identity.example.test",
    );
    expect(policy).not.toContain("ws:");
  });

  it("keeps only the allowances required by the local development compiler", () => {
    const policy = buildContentSecurityPolicy(
      TEST_NONCE,
      { appEnv: "development", apiUrl: "http://localhost:8000" },
      true,
    );

    expect(directive(policy, "script-src")).toContain("'unsafe-eval'");
    expect(directive(policy, "script-src")).not.toContain("'unsafe-inline'");
    expect(directive(policy, "style-src")).toContain("'unsafe-inline'");
    expect(policy).toContain("connect-src 'self' http://localhost:8000 ws: wss:");
  });

  it("does not add destinations when the validated runtime has none", () => {
    expect(buildContentSecurityPolicy(
      TEST_NONCE,
      { appEnv: "test" },
      false,
    )).toContain("connect-src 'self'");
  });

  it("deduplicates API and OIDC destinations on the same origin", () => {
    const policy = buildContentSecurityPolicy(
      TEST_NONCE,
      {
        appEnv: "production",
        apiUrl: "https://platform.example.test",
        oidcIssuer: "https://platform.example.test/identity",
      },
      false,
    );

    expect(policy.match(/https:\/\/platform\.example\.test/g)).toHaveLength(1);
  });

  it("returns a fresh nonce-bearing policy for each document request", () => {
    const request = new NextRequest("https://app.example.test/");
    const first = proxy(request).headers.get("Content-Security-Policy");
    const second = proxy(request).headers.get("Content-Security-Policy");

    expect(first).toMatch(/'nonce-[A-Za-z0-9+/]+={0,2}'/);
    expect(second).toMatch(/'nonce-[A-Za-z0-9+/]+={0,2}'/);
    expect(first).not.toBe(second);
  });

  it("fails before serving a document with invalid production runtime config", () => {
    vi.stubEnv("APP_ENV", "production");
    vi.stubEnv("CUSTOMBUILD_WEB_API_URL", "http://api.example.test");
    vi.stubEnv("CUSTOMBUILD_WEB_DEMO_TOKEN", "");
    vi.stubEnv("CUSTOMBUILD_WEB_OIDC_ISSUER", "https://identity.example.test");
    vi.stubEnv("CUSTOMBUILD_WEB_OIDC_CLIENT_ID", "custombuild-web");
    vi.stubEnv("CUSTOMBUILD_WEB_OIDC_REDIRECT_URI", "https://app.example.test/");

    expect(() => proxy(new NextRequest("https://app.example.test/"))).toThrow(
      "tillåten säker URL",
    );
  });

  it("covers document routes but skips static assets and prefetch requests", () => {
    expect(config.matcher[0]?.source).toContain("_next/static");
    expect(config.matcher[0]?.source).toContain("_next/image");
    expect(config.matcher[0]?.missing).toContainEqual({
      type: "header",
      key: "next-router-prefetch",
    });
  });
});

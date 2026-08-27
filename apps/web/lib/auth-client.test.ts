import { afterEach, describe, expect, it } from "vitest";
import {
  clearOidcSession,
  getStoredAccessToken,
  oidcConfigured,
  oidcRedirectUri,
  validateOidcDiscovery,
} from "./auth-client";

const TOKEN_KEY = "custombuild:oidc:access-token";

afterEach(() => {
  clearOidcSession();
  window.sessionStorage.clear();
});

describe("OIDC browser session", () => {
  it("keeps a live access token in session storage only", () => {
    window.sessionStorage.setItem(TOKEN_KEY, JSON.stringify({
      accessToken: "signed-access-token-value",
      expiresAt: Date.now() + 120_000,
    }));
    expect(getStoredAccessToken()).toBe("signed-access-token-value");
    expect(window.localStorage.getItem(TOKEN_KEY)).toBeNull();
  });

  it("rejects and removes expired or malformed tokens", () => {
    window.sessionStorage.setItem(TOKEN_KEY, JSON.stringify({
      accessToken: "expired-access-token-value",
      expiresAt: Date.now() - 1,
    }));
    expect(getStoredAccessToken()).toBeUndefined();
    expect(window.sessionStorage.getItem(TOKEN_KEY)).toBeNull();

    window.sessionStorage.setItem(TOKEN_KEY, "not-json");
    expect(getStoredAccessToken()).toBeUndefined();
    expect(window.sessionStorage.getItem(TOKEN_KEY)).toBeNull();
  });

  it("accepts only discovery endpoints on the configured issuer origin", () => {
    expect(validateOidcDiscovery({
      issuer: "https://identity.example.test/realms/custombuild/",
      authorization_endpoint: "https://identity.example.test/authorize",
      token_endpoint: "https://identity.example.test/oauth/token",
    }, "https://identity.example.test/realms/custombuild")).toMatchObject({
      issuer: "https://identity.example.test/realms/custombuild",
      token_endpoint: "https://identity.example.test/oauth/token",
    });

    expect(() => validateOidcDiscovery({
      issuer: "https://identity.example.test/realms/custombuild",
      authorization_endpoint: "https://login.attacker.test/authorize",
      token_endpoint: "https://identity.example.test/oauth/token",
    }, "https://identity.example.test/realms/custombuild")).toThrow(
      "OIDC-endpoints måste använda samma betrodda origin",
    );
    expect(() => validateOidcDiscovery({
      issuer: "https://identity.example.test/realms/custombuild",
      authorization_endpoint: "https://identity.example.test/authorize",
      token_endpoint: "https://token.attacker.test/exchange",
    }, "https://identity.example.test/realms/custombuild")).toThrow(
      "OIDC-endpoints måste använda samma betrodda origin",
    );
  });

  it("rejects malformed, insecure, credentialed, or mismatched discovery metadata", () => {
    const valid = {
      issuer: "https://identity.example.test",
      authorization_endpoint: "https://identity.example.test/authorize",
      token_endpoint: "https://identity.example.test/token",
    };

    expect(() => validateOidcDiscovery(valid, "not a URL")).toThrow("giltig HTTPS-adress");
    expect(() => validateOidcDiscovery(valid, "http://identity.example.test")).toThrow(
      "säker HTTPS-adress",
    );
    expect(() => validateOidcDiscovery(valid, "https://user:secret@identity.example.test")).toThrow(
      "säker HTTPS-adress",
    );
    expect(() => validateOidcDiscovery(
      { ...valid, issuer: "https://other.example.test" },
      "https://identity.example.test",
    )).toThrow("issuer matchar inte");
  });

  it("uses only a complete runtime OIDC tuple", () => {
    const config = {
      appEnv: "production" as const,
      oidcIssuer: "https://identity.example.test",
      oidcClientId: "custombuild-web",
      oidcRedirectUri: "https://app.example.test/",
    };
    expect(oidcConfigured(config)).toBe(true);
    expect(oidcConfigured({ ...config, oidcClientId: undefined })).toBe(false);
  });

  it("binds the configured callback to the current browser HTTPS root", () => {
    const config = {
      appEnv: "production" as const,
      oidcIssuer: "https://identity.example.test",
      oidcClientId: "custombuild-web",
      oidcRedirectUri: "https://app.example.test/",
    };
    expect(oidcRedirectUri(config, "https://app.example.test")).toBe(
      "https://app.example.test/",
    );
    expect(() => oidcRedirectUri(config, "https://other.example.test")).toThrow(
      "exakta HTTPS-rot",
    );
    expect(() => oidcRedirectUri(
      { ...config, oidcRedirectUri: "https://app.example.test/callback" },
      "https://app.example.test",
    )).toThrow("exakta HTTPS-rot");
  });
});

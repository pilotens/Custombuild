import { describe, expect, it } from "vitest";
import {
  parsePublicRuntimeConfig,
  PublicRuntimeConfigError,
} from "./runtime-config";

const productionEnvironment = {
  APP_ENV: "production",
  CUSTOMBUILD_WEB_API_URL: "https://api.example.test",
  CUSTOMBUILD_WEB_DEMO_TOKEN: "",
  CUSTOMBUILD_WEB_OIDC_ISSUER: "https://identity.example.test/realms/custombuild/",
  CUSTOMBUILD_WEB_OIDC_CLIENT_ID: "custombuild-web",
  CUSTOMBUILD_WEB_OIDC_REDIRECT_URI: "https://app.example.test/",
} as const;

describe("public runtime configuration", () => {
  it("normalizes a complete production configuration", () => {
    expect(parsePublicRuntimeConfig(productionEnvironment)).toEqual({
      appEnv: "production",
      apiUrl: "https://api.example.test",
      oidcIssuer: "https://identity.example.test/realms/custombuild",
      oidcClientId: "custombuild-web",
      oidcRedirectUri: "https://app.example.test/",
    });
  });

  it("keeps the local API and explicitly public demo credential in development", () => {
    expect(parsePublicRuntimeConfig({
      APP_ENV: "development",
      CUSTOMBUILD_WEB_API_URL: "http://localhost:8000/",
      CUSTOMBUILD_WEB_DEMO_TOKEN: "demo-nordic-owner",
    })).toEqual({
      appEnv: "development",
      apiUrl: "http://localhost:8000",
      developmentToken: "demo-nordic-owner",
    });
  });

  it("allows an unconfigured deterministic fallback outside production", () => {
    expect(parsePublicRuntimeConfig({ APP_ENV: "test" })).toEqual({ appEnv: "test" });
    expect(parsePublicRuntimeConfig({})).toEqual({ appEnv: "development" });
  });

  it("defaults a direct production server to fail-closed production mode", () => {
    expect(() => parsePublicRuntimeConfig({ NODE_ENV: "production" })).toThrow(
      "CUSTOMBUILD_WEB_API_URL krävs i production",
    );
    expect(() => parsePublicRuntimeConfig({
      APP_ENV: "",
      NODE_ENV: "production",
    })).toThrow("APP_ENV får inte vara tomt");
  });

  it.each([
    [{ ...productionEnvironment, CUSTOMBUILD_WEB_API_URL: "http://api.example.test" }, "säker URL"],
    [{ ...productionEnvironment, CUSTOMBUILD_WEB_API_URL: "https://api.example.test/v1" }, "exakt origin"],
    [{ ...productionEnvironment, CUSTOMBUILD_WEB_API_URL: "https://user:secret@api.example.test" }, "säker URL"],
    [{ ...productionEnvironment, CUSTOMBUILD_WEB_API_URL: "https://@api.example.test" }, "säker URL"],
    [{ ...productionEnvironment, CUSTOMBUILD_WEB_API_URL: "https://api.example.test?" }, "säker URL"],
    [{ ...productionEnvironment, CUSTOMBUILD_WEB_API_URL: "https://api.example.test\n.evil.test" }, "säker URL"],
    [{ ...productionEnvironment, CUSTOMBUILD_WEB_API_URL: "\u0000https://api.example.test" }, "säker URL"],
    [{ ...productionEnvironment, CUSTOMBUILD_WEB_API_URL: " https://api.example.test" }, "omgivande blanksteg"],
    [{ ...productionEnvironment, CUSTOMBUILD_WEB_API_URL: "https://api.example.test\\evil.test" }, "säker URL"],
    [{ ...productionEnvironment, CUSTOMBUILD_WEB_OIDC_ISSUER: "http://identity.example.test" }, "säker URL"],
    [{ ...productionEnvironment, CUSTOMBUILD_WEB_OIDC_ISSUER: "https://identity.example.test#tenant" }, "säker URL"],
    [{ ...productionEnvironment, CUSTOMBUILD_WEB_OIDC_ISSUER: "https://identity.example.test\t.evil.test" }, "säker URL"],
    [{ ...productionEnvironment, CUSTOMBUILD_WEB_OIDC_ISSUER: "https://identity.example.test\\evil.test" }, "säker URL"],
    [{ ...productionEnvironment, CUSTOMBUILD_WEB_OIDC_REDIRECT_URI: "https://app.example.test/callback" }, "exakta rot"],
    [{ ...productionEnvironment, CUSTOMBUILD_WEB_OIDC_REDIRECT_URI: "https://app.example.test/?code=leak" }, "säker URL"],
    [{ ...productionEnvironment, CUSTOMBUILD_WEB_OIDC_REDIRECT_URI: "https://app.example.test/\r.evil.test" }, "säker URL"],
    [{ ...productionEnvironment, CUSTOMBUILD_WEB_OIDC_REDIRECT_URI: "https://app.example.test\\evil.test" }, "säker URL"],
    [{ ...productionEnvironment, CUSTOMBUILD_WEB_OIDC_CLIENT_ID: " custombuild-web" }, "client-id"],
    [{ ...productionEnvironment, CUSTOMBUILD_WEB_DEMO_TOKEN: "demo-owner" }, "måste vara tom"],
    [{ ...productionEnvironment, CUSTOMBUILD_WEB_API_URL: "" }, "krävs"],
  ] as const)("rejects an unsafe production variant %#", (environment, message) => {
    expect(() => parsePublicRuntimeConfig(environment)).toThrow(message);
  });

  it("rejects partial OIDC configuration in every environment", () => {
    expect(() => parsePublicRuntimeConfig({
      APP_ENV: "development",
      CUSTOMBUILD_WEB_OIDC_ISSUER: "https://identity.example.test",
    })).toThrow("måste ange issuer, client-id och redirect-URI tillsammans");
  });

  it("never copies unrelated server secrets into the public object or an error", () => {
    const secret = "must-never-cross-the-server-client-boundary";
    const config = parsePublicRuntimeConfig({
      APP_ENV: "development",
      DATABASE_URL: `postgresql://user:${secret}@postgres/custombuild`,
      S3_SECRET_KEY: secret,
      CUSTOMBUILD_WEB_API_URL: "http://localhost:8000",
    });
    expect(JSON.stringify(config)).not.toContain(secret);
    expect(config).toEqual({ appEnv: "development", apiUrl: "http://localhost:8000" });

    let message = "";
    try {
      parsePublicRuntimeConfig({
        ...productionEnvironment,
        CUSTOMBUILD_WEB_API_URL: `https://user:${secret}@api.example.test`,
      });
    } catch (error) {
      expect(error).toBeInstanceOf(PublicRuntimeConfigError);
      message = String(error);
    }
    expect(message).not.toContain(secret);
  });

  it("rejects unknown deployment modes instead of silently weakening production", () => {
    expect(() => parsePublicRuntimeConfig({ APP_ENV: "prod" })).toThrow("APP_ENV");
  });
});

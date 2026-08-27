import { describe, expect, it } from "vitest";
import { parsePublicRuntimeConfig } from "./lib/runtime-config";
import playwrightConfig, {
  PLAYWRIGHT_PRODUCTION_RUNTIME_ENV,
} from "./playwright.config";

describe("Playwright production web server runtime", () => {
  it("supplies a complete, non-secret production runtime only to the managed server", () => {
    expect(parsePublicRuntimeConfig(PLAYWRIGHT_PRODUCTION_RUNTIME_ENV)).toEqual({
      appEnv: "production",
      apiUrl: "https://api.playwright.invalid",
      oidcIssuer: "https://identity.playwright.invalid",
      oidcClientId: "custombuild-web-playwright",
      oidcRedirectUri: "https://app.playwright.invalid/",
    });
    expect(Object.keys(PLAYWRIGHT_PRODUCTION_RUNTIME_ENV).sort()).toEqual([
      "APP_ENV",
      "CUSTOMBUILD_WEB_API_URL",
      "CUSTOMBUILD_WEB_DEMO_TOKEN",
      "CUSTOMBUILD_WEB_OIDC_CLIENT_ID",
      "CUSTOMBUILD_WEB_OIDC_ISSUER",
      "CUSTOMBUILD_WEB_OIDC_REDIRECT_URI",
    ]);

    expect(playwrightConfig.webServer).toMatchObject({
      command: "node scripts/start.mjs",
      env: expect.objectContaining(PLAYWRIGHT_PRODUCTION_RUNTIME_ENV),
    });
  });

  it("does not weaken the production fail-closed contract", () => {
    expect(() => parsePublicRuntimeConfig({ NODE_ENV: "production" })).toThrow(
      "CUSTOMBUILD_WEB_API_URL krävs i production",
    );
  });
});

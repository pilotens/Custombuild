import { describe, expect, it } from "vitest";
import { parsePublicRuntimeConfig } from "./lib/runtime-config";
import playwrightConfig, {
  PLAYWRIGHT_OFFLINE_RUNTIME_ENV,
  PLAYWRIGHT_PRODUCTION_RUNTIME_ENV,
} from "./playwright.config";

describe("Playwright managed web server runtime", () => {
  it("serves the production build with an explicit isolated offline test runtime", () => {
    expect(parsePublicRuntimeConfig(PLAYWRIGHT_OFFLINE_RUNTIME_ENV)).toEqual({
      appEnv: "test",
    });
    expect(PLAYWRIGHT_OFFLINE_RUNTIME_ENV).toEqual({
      APP_ENV: "test",
      CUSTOMBUILD_WEB_API_URL: "",
      CUSTOMBUILD_WEB_DEMO_TOKEN: "",
      CUSTOMBUILD_WEB_OIDC_ISSUER: "",
      CUSTOMBUILD_WEB_OIDC_CLIENT_ID: "",
      CUSTOMBUILD_WEB_OIDC_REDIRECT_URI: "",
    });

    expect(playwrightConfig.webServer).toMatchObject({
      command: "node scripts/start.mjs",
      env: expect.objectContaining({
        NODE_ENV: "production",
        ...PLAYWRIGHT_OFFLINE_RUNTIME_ENV,
      }),
    });
  });

  it("keeps a complete non-secret production fixture separate from browser snapshots", () => {
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
    expect(playwrightConfig.webServer).not.toMatchObject({
      env: expect.objectContaining(PLAYWRIGHT_PRODUCTION_RUNTIME_ENV),
    });
  });

  it("does not weaken the production fail-closed contract", () => {
    expect(() => parsePublicRuntimeConfig({ NODE_ENV: "production" })).toThrow(
      "CUSTOMBUILD_WEB_API_URL krävs i production",
    );
  });
});

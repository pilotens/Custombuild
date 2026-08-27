import { defineConfig, devices } from "@playwright/test";
import type { RuntimeEnvironment } from "./lib/runtime-config";

const port = Number(process.env.PLAYWRIGHT_PORT ?? 3100);
const externalBaseUrl = process.env.PLAYWRIGHT_BASE_URL;
const baseURL = externalBaseUrl ?? `http://127.0.0.1:${port}`;
const dockerHost = process.env.PLAYWRIGHT_DOCKER_HOST;

export const PLAYWRIGHT_PRODUCTION_RUNTIME_ENV = {
  APP_ENV: "production",
  CUSTOMBUILD_WEB_API_URL: "https://api.playwright.invalid",
  CUSTOMBUILD_WEB_DEMO_TOKEN: "",
  CUSTOMBUILD_WEB_OIDC_ISSUER: "https://identity.playwright.invalid/",
  CUSTOMBUILD_WEB_OIDC_CLIENT_ID: "custombuild-web-playwright",
  CUSTOMBUILD_WEB_OIDC_REDIRECT_URI: "https://app.playwright.invalid/",
} as const satisfies RuntimeEnvironment;

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.spec.ts",
  // WebGL rendering and reference-image analysis are intentionally serialized:
  // parallel browser contexts compete for the same software GPU in CI and can
  // turn a functional assertion into a host-load timing test.
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  failOnFlakyTests: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  workers: 1,
  reporter: process.env.CI
    ? [["github"], ["html", { open: "never" }]]
    : [["list"], ["html", { open: "never" }]],
  outputDir: "test-results",
  use: {
    baseURL,
    locale: "sv-SE",
    timezoneId: "Europe/Stockholm",
    colorScheme: "light",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    launchOptions: dockerHost
      ? { args: [`--host-resolver-rules=MAP localhost ${dockerHost}`] }
      : undefined,
  },
  projects: [
    {
      name: "chromium-desktop",
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "firefox-desktop",
      use: { ...devices["Desktop Firefox"] },
    },
    {
      name: "webkit-desktop",
      use: { ...devices["Desktop Safari"] },
    },
  ],
  webServer: externalBaseUrl
    ? undefined
    : {
        command: "node scripts/start.mjs",
        url: baseURL,
        env: {
          HOSTNAME: "127.0.0.1",
          PORT: String(port),
          NODE_ENV: "production",
          ...PLAYWRIGHT_PRODUCTION_RUNTIME_ENV,
        },
        reuseExistingServer: !process.env.CI,
        timeout: 120_000,
        stdout: "pipe",
        stderr: "pipe",
      },
});

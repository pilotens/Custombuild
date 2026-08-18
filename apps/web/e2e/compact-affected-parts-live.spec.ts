import { expect, test } from "@playwright/test";
import {
  provisionLiveProject,
  selectProjectBeforeNavigation,
  waitForSuccessfulProjectDraftSave,
} from "./live-helpers";
import { chooseTemplateAndCreate } from "./planning-helpers";

test.skip(
  process.env.PLAYWRIGHT_REAL_API !== "1",
  "Requires the complete Compose API and authenticated TEST workspace.",
);
test.skip(
  ({ browserName }) => browserName !== "chromium",
  "The deployed compact-parts visual acceptance runs once in Chromium.",
);

test("kontrollregler är kompakta, begripliga och fokuserar modellen", async ({
  page,
  request,
}, testInfo) => {
  test.setTimeout(2 * 60_000);
  await page.setViewportSize({ width: 1_280, height: 1_100 });
  const project = await provisionLiveProject(request, testInfo, "compact-parts");
  await selectProjectBeforeNavigation(page, project);

  const pageErrors: string[] = [];
  const consoleErrors: string[] = [];
  const failedRequests: string[] = [];
  const failedApiResponses: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("requestfailed", (candidate) => {
    failedRequests.push(`${candidate.method()} ${candidate.url()}: ${candidate.failure()?.errorText}`);
  });
  page.on("response", (response) => {
    const path = new URL(response.url()).pathname;
    if (path.startsWith("/v1/") && response.status() >= 400) {
      failedApiResponses.push(`${response.request().method()} ${path}: ${response.status()}`);
    }
  });

  const response = await page.goto("/", { waitUntil: "domcontentloaded" });
  expect(response?.status()).toBe(200);
  await expect(page.getByRole("heading", { name: "Vad vill du skapa?" })).toBeVisible({ timeout: 30_000 });

  const initialDraft = waitForSuccessfulProjectDraftSave(page, project.project.id, {
    furniture_type: "wall_library",
    width_mm: 4_200,
    height_mm: 2_400,
    depth_mm: 320,
  });
  await chooseTemplateAndCreate(page, "Väggbibliotek", {
    widthMm: 4_200,
    heightMm: 2_400,
    depthMm: 320,
  });
  await initialDraft;
  await expect(page.getByText("Sparad på servern", { exact: true })).toBeVisible({ timeout: 30_000 });

  await page.getByRole("navigation", { name: "Produktlägen" })
    .getByRole("button", { name: /Kontroll/ }).click();
  const panel = page.locator(".validation-panel");
  await expect(panel.getByRole("heading", { name: "Kontrollera konstruktionen" }))
    .toBeVisible({ timeout: 30_000 });
  const compactRule = panel
    .locator("article.validation-card")
    .filter({ hasText: "Tipprisk och krav på väggförankring" });
  await expect(compactRule).toHaveCount(1);
  const technicalDetails = compactRule.locator("details.validation-technical");
  await expect(technicalDetails).not.toHaveAttribute("open", "");
  await technicalDetails.getByText("Tekniska detaljer", { exact: true }).click();
  await expect(technicalDetails).toHaveAttribute("open", "");
  await expect(compactRule.getByText("Orsak", { exact: true })).toBeVisible();
  await expect(compactRule.getByText("Rekommenderad lösning", { exact: true })).toBeVisible();
  const focusModel = compactRule.getByRole("button", { name: /^Fokusera delen .* i modellen$/ }).first();
  await expect(focusModel).toBeVisible();
  await focusModel.click();
  await expect(page.getByLabel("Interaktiv 3D-modell av möbeln")).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("01-desktop-control-details-open.png"), fullPage: true });

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(compactRule).toBeVisible();
  await expect(focusModel).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("02-mobile-control-details-open.png"), fullPage: true });

  await page.setViewportSize({ width: 1_024, height: 900 });
  await expect(compactRule).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBe(0);
  await page.screenshot({ path: testInfo.outputPath("03-tablet-control-zero-overflow.png"), fullPage: true });

  expect(pageErrors).toEqual([]);
  expect(consoleErrors).toEqual([]);
  expect(failedApiResponses).toEqual([]);
  expect(failedRequests.filter((failure) => !(
    failure.endsWith(": net::ERR_ABORTED") && failure.includes("/v1/designs/autofix")
  ))).toEqual([]);
});

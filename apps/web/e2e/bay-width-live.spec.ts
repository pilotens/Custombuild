import { expect, test } from "@playwright/test";
import {
  provisionLiveProject,
  selectProjectBeforeNavigation,
  waitForSuccessfulProjectDraftSave,
} from "./live-helpers";
import { chooseTemplateAndCreate } from "./planning-helpers";

async function saveDraftExplicitly(
  page: Parameters<typeof waitForSuccessfulProjectDraftSave>[0],
  projectId: string,
  expectedWorkspaceSpec: Record<string, unknown>,
) {
  const saveButton = page.getByRole("button", { name: "Spara utkast", exact: true });
  await expect(saveButton).toBeEnabled({ timeout: 30_000 });
  const saved = waitForSuccessfulProjectDraftSave(page, projectId, expectedWorkspaceSpec);
  await saveButton.click();
  await saved;
  await expect(page.getByText("Sparad på servern", { exact: true })).toBeVisible();
}

test.skip(
  process.env.PLAYWRIGHT_REAL_API !== "1",
  "Requires the complete Compose API and authenticated workspace.",
);
test.skip(
  ({ browserName }) => browserName !== "chromium",
  "The deployed bay-width acceptance runs once in Chromium.",
);

test("300 mm fackbredd maximerar antalet fack och följer senare måttändringar", async ({
  page,
  request,
}, testInfo) => {
  test.setTimeout(2 * 60_000);
  await page.setViewportSize({ width: 1_280, height: 1_000 });
  const project = await provisionLiveProject(request, testInfo, "bay-width");
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

  await chooseTemplateAndCreate(page, "Väggbibliotek", {
    widthMm: 4_200,
    heightMm: 2_400,
    depthMm: 320,
  });
  await saveDraftExplicitly(page, project.project.id, {
    furniture_type: "wall_library",
    width_mm: 4_200,
    height_mm: 2_400,
  });

  const studioPanel = page.locator(".cb-context-panel");
  await expect(page.getByRole("group", { name: "Hur vill du bestämma facken?" })).toBeVisible();
  const gridStatus = studioPanel.getByRole("status").filter({ hasText: "fri bredd per fack" });

  // The whole visible segment is the native radio hit target.
  const targetMode = studioPanel.getByRole("radio", { name: "Minsta bredd", exact: true });
  await targetMode.check();
  await expect(targetMode).toBeChecked();
  const targetInput = studioPanel.getByRole("spinbutton", { name: "Minsta fria fackbredd" });
  await targetInput.fill("300");
  await targetInput.press("Enter");

  await expect(gridStatus).toContainText("13 fack");
  await expect(gridStatus).toContainText("Cirka 303,9 mm fri bredd per fack");
  await expect(page.locator(".viewer-status-strip")).toContainText("13 bärande fack");
  await saveDraftExplicitly(page, project.project.id, {
    bay_sizing_mode: "target_width",
    target_bay_width_mm: 300,
    divider_count: 12,
    base_cabinet_count: 13,
    reinforcement_mode: "auto",
  });
  await page.screenshot({ path: testInfo.outputPath("01-target-300mm-13-bays.png"), fullPage: true });

  const widthInput = page.getByRole("spinbutton", { name: "Bredd", exact: true });
  await widthInput.fill("4500");
  await widthInput.press("Enter");

  await expect(studioPanel.getByRole("radio", { name: "Minsta bredd", exact: true })).toBeChecked();
  await expect(targetInput).toHaveValue("300");
  await expect(gridStatus).toContainText("14 fack");
  await expect(gridStatus).toContainText("Cirka 302,4 mm fri bredd per fack");
  await saveDraftExplicitly(page, project.project.id, {
    width_mm: 4_500,
    bay_sizing_mode: "target_width",
    target_bay_width_mm: 300,
    divider_count: 13,
    base_cabinet_count: 14,
  });

  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.getByRole("navigation", { name: "Produktlägen" }).getByRole("button", { name: /Studio/ }))
    .toHaveAttribute("aria-current", "page", { timeout: 30_000 });
  await expect(page.locator(".cb-context-panel").getByRole("radio", { name: "Minsta bredd", exact: true }))
    .toBeChecked();
  await expect(page.getByRole("spinbutton", { name: "Minsta fria fackbredd" })).toHaveValue("300");
  await expect(gridStatus).toContainText("14 fack");

  await page.setViewportSize({ width: 390, height: 844 });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBe(0);
  await page.screenshot({ path: testInfo.outputPath("02-target-width-mobile.png"), fullPage: true });

  expect(pageErrors).toEqual([]);
  expect(consoleErrors).toEqual([]);
  expect(failedApiResponses).toEqual([]);
  expect(failedRequests.filter((failure) => !failure.endsWith(": net::ERR_ABORTED"))).toEqual([]);
});

import { expect, test, type Request } from "@playwright/test";
import {
  provisionLiveProject,
  selectProjectBeforeNavigation,
  waitForSuccessfulProjectDraftSave,
} from "./live-helpers";

test.skip(
  process.env.PLAYWRIGHT_REAL_API !== "1",
  "Requires the complete Compose API and an authenticated server workspace.",
);
test.skip(
  ({ browserName }) => browserName !== "chromium",
  "The state-mutating real-API regression runs once in Chromium.",
);

function requestPath(request: Request): string {
  return new URL(request.url()).pathname;
}

function isVersionCreate(request: Request): boolean {
  return request.method() === "POST"
    && /^\/v1\/projects\/[^/]+\/versions$/.test(requestPath(request));
}

function isVersionValidation(request: Request): boolean {
  return request.method() === "POST"
    && /^\/v1\/projects\/[^/]+\/versions\/\d+\/validate$/.test(requestPath(request));
}

function isVersionGeneration(request: Request): boolean {
  return request.method() === "POST"
    && /^\/v1\/projects\/[^/]+\/versions\/\d+\/generate$/.test(requestPath(request));
}

test("väggbiblioteket sparas som 4200 × 2400-koncept men kan inte skapa produktionsrevision", async ({
  page,
  request,
}, testInfo) => {
  test.setTimeout(3 * 60_000);
  await page.setViewportSize({ width: 1_280, height: 1_100 });
  const project = await provisionLiveProject(request, testInfo, "wall-concept-save");
  await selectProjectBeforeNavigation(page, project);

  const pageErrors: string[] = [];
  const consoleErrors: string[] = [];
  const failedRequests: string[] = [];
  const failedApiResponses: string[] = [];
  const versionCreates: Request[] = [];
  const validations: Request[] = [];
  const generations: Request[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("request", (request) => {
    if (isVersionCreate(request)) versionCreates.push(request);
    if (isVersionValidation(request)) validations.push(request);
    if (isVersionGeneration(request)) generations.push(request);
  });
  page.on("requestfailed", (request) => {
    failedRequests.push(`${request.method()} ${request.url()}: ${request.failure()?.errorText}`);
  });
  page.on("response", (response) => {
    const responsePath = new URL(response.url()).pathname;
    if (responsePath.startsWith("/v1/") && response.status() >= 400) {
      failedApiResponses.push(`${response.request().method()} ${responsePath}: ${response.status()}`);
    }
  });

  const response = await page.goto("/", { waitUntil: "domcontentloaded" });
  expect(response?.status()).toBe(200);
  const explore = page.locator("section.template-picker[data-presentation='embedded']");
  await expect(explore.getByRole("heading", { name: "Vad vill du skapa?" })).toBeVisible({ timeout: 30_000 });

  // The direct gallery route must use the selected template's advertised
  // dimensions. Generic planning defaults must not mutate this 4200 mm model.
  const initialDraftSave = waitForSuccessfulProjectDraftSave(page, project.project.id, {
    furniture_type: "wall_library",
    width_mm: 4_200,
    height_mm: 2_400,
    depth_mm: 320,
  });
  await explore.getByRole("button", { name: /Välj en design/ }).click();
  await expect(explore.getByRole("heading", { name: "Välj en startmodell att forma vidare." }))
    .toBeVisible({ timeout: 30_000 });
  const wallLibrary = explore.locator("button.template-card", { hasText: /Väggbibliotek/ });
  await expect(wallLibrary).toHaveCount(1, { timeout: 30_000 });
  await wallLibrary.click();
  await explore.getByRole("button", { name: "Öppna Väggbibliotek i Studio" }).click();
  await initialDraftSave;
  await expect(page.getByText("Sparad på servern", { exact: true })).toBeVisible({ timeout: 30_000 });

  await expect(page.getByRole("spinbutton", { name: "Bredd", exact: true })).toHaveValue("4200");
  await expect(page.getByRole("spinbutton", { name: "Höjd", exact: true })).toHaveValue("2400");
  await expect(page.getByRole("spinbutton", { name: "Djup", exact: true })).toHaveValue("320");
  const viewer = page.getByTestId("furniture-viewer");
  await expect(viewer).toBeVisible();
  await expect(viewer.locator("canvas, [data-testid='front-projection-fallback']").first()).toBeVisible();

  const modes = page.getByRole("navigation", { name: "Produktlägen" });
  await modes.getByRole("button", { name: /Underlag/ }).click();
  const drawer = page.locator("section.production-drawer-embedded");
  await expect(drawer).toBeVisible();
  await expect(drawer.getByRole("heading", {
    name: "Den här mallen är fortfarande en konceptmodell",
  })).toBeVisible();
  await expect(drawer).toContainText("gångjärn, beslag, borrbilder, frontspel");
  await expect(drawer).toContainText("limfria mekaniska retention");
  await expect(drawer).toContainText("inget designgranskningspaket skapas");
  await expect(drawer.getByRole("button", { name: /Spara.*revision/i })).toHaveCount(0);
  await expect(drawer.getByRole("button", { name: /Skapa underlag/i })).toHaveCount(0);
  await expect(drawer.getByRole("button", { name: "Fortsätt utforma" })).toBeVisible();

  const screenshot = await drawer.screenshot({
    path: testInfo.outputPath("01-wall-library-concept-gate.png"),
  });
  await testInfo.attach("01-wall-library-concept-gate.png", {
    body: screenshot,
    contentType: "image/png",
  });

  // Effects and server hydration must not bypass the client safety boundary.
  await page.waitForTimeout(900);
  expect(versionCreates).toHaveLength(0);
  expect(validations).toHaveLength(0);
  expect(generations).toHaveLength(0);
  expect(pageErrors).toEqual([]);
  expect(consoleErrors).toEqual([]);
  expect(failedApiResponses).toEqual([]);
  const unexpectedFailedRequests = failedRequests.filter((failure) => !(
    failure.endsWith(": net::ERR_ABORTED")
    && failure.includes("/v1/designs/autofix")
  ));
  expect(unexpectedFailedRequests).toEqual([]);
});

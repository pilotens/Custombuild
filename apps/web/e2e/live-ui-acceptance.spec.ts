import { writeFile } from "node:fs/promises";
import { expect, test, type Page } from "@playwright/test";
import {
  provisionLiveProject,
  selectProjectBeforeNavigation,
  waitForSuccessfulProjectDraftSave,
} from "./live-helpers";
import { openReferencePlanning } from "./planning-helpers";

test.skip(
  process.env.PLAYWRIGHT_REAL_API !== "1",
  "Requires the complete Compose API and authenticated server workspaces.",
);
test.skip(
  ({ browserName }) => browserName !== "chromium",
  "The state-mutating live UI journey runs once in Chromium.",
);

function requestPath(url: string): string {
  return new URL(url).pathname;
}

async function waitForServerSave(page: Page): Promise<void> {
  await expect(page.getByText("Sparad på servern", { exact: true })).toBeVisible({ timeout: 30_000 });
}

async function expectNoHorizontalOverflow(page: Page): Promise<void> {
  const widths = await page.evaluate(() => ({
    viewport: window.innerWidth,
    documentWidth: document.documentElement.scrollWidth,
    bodyWidth: document.body.scrollWidth,
  }));
  expect(widths.documentWidth).toBeLessThanOrEqual(widths.viewport);
  expect(widths.bodyWidth).toBeLessThanOrEqual(widths.viewport);
}

async function expectPanelsDoNotOverlap(page: Page): Promise<void> {
  const viewer = await page.getByRole("region", { name: "Konstruktionsvy" }).boundingBox();
  const rail = await page.locator(".right-rail.configurator-rail").boundingBox();
  expect(viewer).not.toBeNull();
  expect(rail).not.toBeNull();
  if (!viewer || !rail) return;
  const overlapWidth = Math.min(viewer.x + viewer.width, rail.x + rail.width) - Math.max(viewer.x, rail.x);
  const overlapHeight = Math.min(viewer.y + viewer.height, rail.y + rail.height) - Math.max(viewer.y, rail.y);
  if (page.viewportSize()?.width && page.viewportSize()!.width <= 920) {
    // The small-screen Studio intentionally presents the inspector as a
    // bottom-sheet surface over the lower edge of the model. Keep the live
    // assertion aligned with the deterministic responsive contract: the
    // sheet must stay inside the model width and below its upper half.
    expect(rail.y).toBeGreaterThan(viewer.y + viewer.height * 0.45);
    expect(rail.x).toBeGreaterThanOrEqual(viewer.x - 1);
    expect(rail.x + rail.width).toBeLessThanOrEqual(viewer.x + viewer.width + 1);
  } else {
    expect(overlapWidth > 1 && overlapHeight > 1).toBe(false);
  }
}

async function waitForTwoAnimationFrames(page: Page): Promise<void> {
  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));
}

async function configureWallLibrary(page: Page): Promise<void> {
  const explore = page.locator("section.template-picker[data-presentation='embedded']");
  await expect(explore.getByRole("heading", { name: "Vad vill du skapa?" })).toBeVisible({ timeout: 30_000 });
  await explore.getByRole("button", { name: /Skapa med Custombuild/ }).click();
  await explore.getByRole("spinbutton", { name: "Planerad bredd" }).fill("4200");
  await explore.getByRole("spinbutton", { name: "Planerad höjd" }).fill("2400");
  await explore.getByRole("spinbutton", { name: "Planerad djup" }).fill("320");
  await explore.getByRole("button", { name: /^Mot en vägg/ }).click();
  await explore.getByRole("button", { name: /^Öppet och dolt/ }).click();
  await explore.getByRole("button", { name: /^Balans/ }).click();
  await explore.getByRole("button", { name: /^Naturligt trä/ }).click();
  await explore.getByRole("button", { name: "Visa tre startförslag" }).click();
  const wallLibrary = explore.locator("button", { hasText: /Väggbibliotek/ });
  await wallLibrary.last().click();
  await explore.getByRole("button", { name: "Öppna vald modell i Studio" }).click();
  await expect(page.getByRole("navigation", { name: "Produktlägen" }).getByRole("button", { name: /Studio/ }))
    .toHaveAttribute("aria-current", "page");
}

async function createReferencePng(page: Page, accent: string): Promise<Buffer> {
  const dataUrl = await page.evaluate((fill) => {
    const canvas = document.createElement("canvas");
    canvas.width = 360;
    canvas.height = 260;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("Canvas context is unavailable.");
    context.fillStyle = "#f5f2e9";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.fillStyle = "#b79868";
    context.fillRect(30, 20, 300, 215);
    context.fillStyle = fill;
    context.fillRect(30, 174, 300, 61);
    context.fillStyle = "#462d1e";
    for (const x of [30, 90, 150, 210, 270, 330]) context.fillRect(x - 2, 20, 4, 215);
    for (const y of [20, 58, 96, 134, 174, 235]) context.fillRect(30, y - 2, 300, 4);
    return canvas.toDataURL("image/png");
  }, accent);
  return Buffer.from(dataUrl.split(",")[1]!, "base64");
}

async function pasteReferenceImage(page: Page, buffer: Buffer, fileName: string): Promise<void> {
  const pasteArea = page.getByLabel("Uppladdningsruta för referensbild");
  await expect(pasteArea).toHaveAttribute("aria-keyshortcuts", "Control+V Meta+V");
  await expect(pasteArea).toContainText("Klistra in skärmklipp");
  await pasteArea.evaluate((element, payload) => {
    const bytes = Uint8Array.from(atob(payload.base64), (character) => character.charCodeAt(0));
    const file = new File([bytes], payload.fileName, { type: "image/png" });
    const transfer = new DataTransfer();
    transfer.items.add(file);
    element.dispatchEvent(new ClipboardEvent("paste", {
      bubbles: true,
      cancelable: true,
      clipboardData: transfer,
    }));
  }, { base64: buffer.toString("base64"), fileName });
}

test("current-source live UI acceptance covers Explore, responsive Studio and reference import", async ({
  page,
  request,
}, testInfo) => {
  test.setTimeout(6 * 60_000);
  const wallProject = await provisionLiveProject(request, testInfo, "ui-wall");
  const referenceProject = await provisionLiveProject(request, testInfo, "ui-reference");
  expect(referenceProject.principal).toEqual(wallProject.principal);
  await selectProjectBeforeNavigation(page, wallProject);

  const pageErrors: string[] = [];
  const consoleErrors: string[] = [];
  const failedRequests: string[] = [];
  const failedApiResponses: string[] = [];
  let referenceInspections = 0;
  let wallPreviewHeld = false;
  let releaseWallPreview!: () => void;
  const wallPreviewGate = new Promise<void>((resolve) => {
    releaseWallPreview = resolve;
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("request", (candidate) => {
    if (candidate.method() === "POST" && /\/v1\/projects\/[^/]+\/imports\/inspect$/.test(requestPath(candidate.url()))) {
      referenceInspections += 1;
    }
  });
  page.on("requestfailed", (candidate) => {
    failedRequests.push(`${candidate.method()} ${candidate.url()}: ${candidate.failure()?.errorText ?? "unknown"}`);
  });
  page.on("response", (response) => {
    const path = requestPath(response.url());
    if (path.startsWith("/v1/") && response.status() >= 400) {
      failedApiResponses.push(`${response.request().method()} ${path}: ${response.status()}`);
    }
  });
  await page.route("**/v1/designs/autofix", async (route) => {
    const candidate = route.request();
    const payload = candidate.method() === "POST"
      ? candidate.postDataJSON() as Record<string, unknown>
      : undefined;
    const isInitialWallPreview = !wallPreviewHeld
      && payload?.furniture_type === "wall_library"
      && payload.width_mm === 4_200
      && payload.height_mm === 2_400;
    if (isInitialWallPreview) {
      wallPreviewHeld = true;
      await wallPreviewGate;
    }
    await route.continue();
  });

  await page.setViewportSize({ width: 1_280, height: 900 });
  const response = await page.goto("/", { waitUntil: "domcontentloaded" });
  expect(response?.status()).toBe(200);
  await expect(page.getByRole("combobox", { name: "Aktivt projekt" })).toHaveValue(wallProject.project.id, {
    timeout: 30_000,
  });
  const initialDraftSave = waitForSuccessfulProjectDraftSave(page, wallProject.project.id, {
    furniture_type: "wall_library",
    width_mm: 4_200,
    height_mm: 2_400,
    depth_mm: 320,
  });
  const initialWallPreview = page.waitForRequest((candidate) => {
    if (candidate.method() !== "POST" || requestPath(candidate.url()) !== "/v1/designs/autofix") return false;
    const payload = candidate.postDataJSON() as Record<string, unknown>;
    return payload.furniture_type === "wall_library"
      && payload.width_mm === 4_200
      && payload.height_mm === 2_400;
  });
  await configureWallLibrary(page);
  const wallPreviewRequest = await initialWallPreview;
  await expect(page.getByRole("spinbutton", { name: "Bredd", exact: true })).toHaveValue("4200");
  await expect(page.getByRole("spinbutton", { name: "Höjd", exact: true })).toHaveValue("2400");

  const threeDimensionalView = page.getByRole("button", { name: "3D", exact: true });
  const frontView = page.getByRole("button", { name: "Front", exact: true });
  const partPicker = page.getByRole("combobox", { name: "Välj möbeldel att inspektera" });
  await expect(threeDimensionalView).toHaveAttribute("aria-pressed", "true");
  await expect(frontView).toHaveAttribute("aria-pressed", "false");
  expect(await partPicker.getByRole("option", { name: /Hylla/ }).count()).toBeGreaterThanOrEqual(5);
  expect(await partPicker.getByRole("option", { name: /Skåpsfront/ }).count()).toBeGreaterThanOrEqual(4);
  const model = page.locator(".canvas-shell canvas, [data-testid='front-projection-fallback']").first();
  await expect(model).toBeVisible();
  await waitForTwoAnimationFrames(page);
  const firstFrontFrame = await model.screenshot({ path: testInfo.outputPath("01-front-first-3d.png") });
  expect(firstFrontFrame.byteLength).toBeGreaterThan(5_000);
  releaseWallPreview();
  const wallPreviewResponse = await wallPreviewRequest.response();
  expect(wallPreviewResponse?.status()).toBe(200);
  await initialDraftSave;
  await waitForServerSave(page);
  await expect(page.getByText("Servermodell", { exact: true })).toBeVisible({ timeout: 30_000 });
  await page.waitForTimeout(2_000);
  const settledFrontFrame = await model.screenshot({ path: testInfo.outputPath("02-front-after-2s.png") });
  expect(settledFrontFrame.byteLength).toBeGreaterThan(5_000);
  await expect(threeDimensionalView).toHaveAttribute("aria-pressed", "true");
  await expect(frontView).toHaveAttribute("aria-pressed", "false");
  await page.unroute("**/v1/designs/autofix");

  expect(await partPicker.getByRole("option", { name: /Hylla/ }).count()).toBeGreaterThanOrEqual(5);
  expect(await partPicker.getByRole("option", { name: /Skåpsfront/ }).count()).toBeGreaterThanOrEqual(4);
  await frontView.click();
  await expect(frontView).toHaveAttribute("aria-pressed", "true");
  await waitForTwoAnimationFrames(page);
  const orthographicFront = await model.screenshot({ path: testInfo.outputPath("03-explicit-front.png") });
  expect(orthographicFront.byteLength).toBeGreaterThan(5_000);
  await threeDimensionalView.click();

  for (const viewport of [
    { width: 390, height: 844 },
    { width: 768, height: 900 },
    { width: 1_024, height: 900 },
    { width: 1_280, height: 900 },
  ]) {
    await page.setViewportSize(viewport);
    await expect(page.getByRole("region", { name: "Konstruktionsvy" })).toBeVisible();
    await expect(page.locator(".save-state")).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await expectPanelsDoNotOverlap(page);
    const modes = page.getByRole("navigation", { name: "Produktlägen" });
    await expect(modes).toBeVisible();
    await expect(modes.getByRole("button")).toHaveCount(4);
    await expect(page.locator(".side-nav nav, .mobile-nav")).toHaveCount(0);
    await page.screenshot({
      path: testInfo.outputPath(`responsive-${viewport.width}x${viewport.height}.png`),
      fullPage: true,
    });
  }

  const modes = page.getByRole("navigation", { name: "Produktlägen" });
  await modes.getByRole("button", { name: /Underlag/ }).click();
  const productionDialog = page.locator("section.production-drawer-embedded");
  await expect(productionDialog).toBeVisible();
  await expect(productionDialog.getByRole("heading", {
    name: "Den här mallen är fortfarande en konceptmodell",
  })).toBeVisible();
  await expect(productionDialog).toContainText("gångjärn, beslag, borrbilder, frontspel");
  await expect(productionDialog).toContainText("inget designgranskningspaket skapas");
  await expect(productionDialog.getByRole("button", { name: /Spara.*revision/i })).toHaveCount(0);
  await expect(productionDialog.getByRole("button", { name: /Skapa underlag/i })).toHaveCount(0);
  await expect(productionDialog.getByLabel("Övergripande designgranskning")).toHaveCount(0);
  await expect(productionDialog.getByLabel("CAM-granskarens motivering")).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("04-concept-production-gate.png"), fullPage: true });
  await modes.getByRole("button", { name: /Studio/ }).click();
  await expect(productionDialog).toBeHidden();

  const projectSelect = page.getByRole("combobox", { name: "Aktivt projekt" });
  await projectSelect.selectOption(referenceProject.project.id);
  await expect(projectSelect).toHaveValue(referenceProject.project.id, { timeout: 30_000 });
  await expect(page.getByRole("heading", { name: "Vad vill du skapa?" })).toBeVisible({ timeout: 30_000 });
  await openReferencePlanning(page, { widthMm: 1_800, heightMm: 2_100, depthMm: 320 });
  await expect(page.getByRole("heading", { name: "Skapa från referensbild" })).toBeVisible();

  const uploadBuffer = await createReferencePng(page, "#6f361c");
  const uploadInspectionPromise = page.waitForResponse((candidate) => (
    candidate.request().method() === "POST"
    && /\/v1\/projects\/[^/]+\/imports\/inspect$/.test(requestPath(candidate.url()))
  ));
  await page.getByLabel("Välj referensbild").setInputFiles({
    name: "reference-upload.png",
    mimeType: "image/png",
    buffer: uploadBuffer,
  });
  const uploadInspection = await uploadInspectionPromise;
  expect(uploadInspection.status()).toBe(200);
  await expect(page.getByRole("heading", { name: "Kontrollera tolkningen" })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/Källbilden är sparad oföränderligt/)).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("05-reference-upload-analysis.png"), fullPage: true });
  const uploadDraftSave = waitForSuccessfulProjectDraftSave(page, referenceProject.project.id);
  await page.getByRole("button", { name: "Skapa konceptmodell" }).click();
  await uploadDraftSave;
  await expect(page.getByText(/Bildkoncept · reference-upload.png/)).toBeVisible();
  await expect(page.getByRole("heading", { name: "Vad vill du skapa?" })).toHaveCount(0);
  await waitForServerSave(page);

  await openReferencePlanning(page);
  await expect(page.getByRole("heading", { name: "Skapa från referensbild" })).toBeVisible();
  const pasteBuffer = await createReferencePng(page, "#355c55");
  const pasteInspectionPromise = page.waitForResponse((candidate) => (
    candidate.request().method() === "POST"
    && /\/v1\/projects\/[^/]+\/imports\/inspect$/.test(requestPath(candidate.url()))
  ));
  await pasteReferenceImage(page, pasteBuffer, "reference-paste.png");
  const pasteInspection = await pasteInspectionPromise;
  expect(pasteInspection.status()).toBe(200);
  await expect(page.getByRole("heading", { name: "Kontrollera tolkningen" })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("img", { name: "Referensbild med detekterade möbellinjer" })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("06-reference-paste-analysis.png"), fullPage: true });
  const pasteDraftSave = waitForSuccessfulProjectDraftSave(page, referenceProject.project.id);
  await page.getByRole("button", { name: "Skapa konceptmodell" }).click();
  await pasteDraftSave;
  await expect(page.getByText(/Bildkoncept · reference-paste.png/)).toBeVisible();
  await waitForServerSave(page);

  await page.waitForTimeout(1_000);
  const expectedAborts = failedRequests.filter((failure) => (
    failure.includes("/v1/designs/autofix") && failure.endsWith(": net::ERR_ABORTED")
  ));
  const unexpectedFailedRequests = failedRequests.filter((failure) => !expectedAborts.includes(failure));
  const audit = {
    baseUrl: process.env.PLAYWRIGHT_BASE_URL,
    wallProject: wallProject.project,
    referenceProject: referenceProject.project,
    referenceInspections,
    pageErrors,
    consoleErrors,
    failedApiResponses,
    failedRequests,
    expectedAborts,
    unexpectedFailedRequests,
  };
  await writeFile(testInfo.outputPath("browser-error-audit.json"), `${JSON.stringify(audit, null, 2)}\n`, "utf8");

  expect(referenceInspections).toBe(2);
  expect(pageErrors).toEqual([]);
  expect(consoleErrors).toEqual([]);
  expect(failedApiResponses).toEqual([]);
  expect(unexpectedFailedRequests).toEqual([]);
});

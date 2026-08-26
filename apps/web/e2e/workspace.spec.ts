import { createHash } from "node:crypto";
import { expect, test } from "@playwright/test";
import { goToPlanningStart, startWithEmptyPlanningStorage } from "./planning-helpers";

test.skip(
  process.env.PLAYWRIGHT_REAL_API === "1",
  "Offline workspace smoke is replaced by the live production workflow in Compose.",
);

function imageHash(image: Buffer): string {
  return createHash("sha256").update(image).digest("hex");
}

test("mallbilder och dynamisk 3D-vy passerar den visuella kvalitetskontrollen", async ({
  page,
}, testInfo) => {
  // Software-rendered WebGL in the isolated Linux acceptance container is
  // intentionally slower than a workstation GPU. Keep the interaction gate
  // strict, but give its six image captures enough deterministic headroom.
  test.setTimeout(90_000);
  const pageErrors: string[] = [];
  const failedRequests: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("requestfailed", (request) => {
    failedRequests.push(`${request.method()} ${request.url()}: ${request.failure()?.errorText}`);
  });

  await startWithEmptyPlanningStorage(page);
  const response = await page.goto("/", { waitUntil: "networkidle" });
  expect(response?.status()).toBe(200);
  await expect(page.locator("html")).toHaveAttribute("lang", "sv");
  await expect(page).toHaveTitle("Custombuild · Konstruktionsarbetsyta");
  const planner = await goToPlanningStart(page, {
    widthMm: 4_200,
    heightMm: 2_400,
    depthMm: 340,
  });
  await planner.getByRole("button", { name: /Välj en design/ }).click();

  const previews = page.locator("button.template-card img");
  await expect(previews).toHaveCount(3);
  for (let index = 0; index < await previews.count(); index += 1) {
    const preview = previews.nth(index);
    const bounds = await preview.boundingBox();
    expect(bounds?.width).toBeGreaterThan(100);
    expect(bounds?.height).toBeGreaterThan(80);
    const image = await preview.screenshot();
    expect(image.byteLength).toBeGreaterThan(1_000);
  }

  const galleryImage = await page.locator(".template-picker").screenshot();
  await testInfo.attach("validerat-mallgalleri.png", { body: galleryImage, contentType: "image/png" });

  await planner.locator("button.template-card", { hasText: /Väggbibliotek/ }).click();
  await planner.getByRole("button", { name: "Öppna Väggbibliotek i Studio" }).click();
  await expect(page.getByRole("heading", { name: "Möbel", exact: true })).toBeVisible();
  await expect(page.getByText("Dra även måtthandtagen direkt i modellen.", { exact: true })).toBeVisible();
  const viewer = page.getByTestId("furniture-viewer");
  await expect(viewer).toHaveAttribute("data-renderer", /^(webgl|front-projection)$/);
  const renderedModel = viewer.locator("canvas, [data-testid='front-projection-fallback']").first();
  await expect(renderedModel).toBeVisible();
  await expect(page.getByRole("button", { name: "Anpassa vy" })).toBeVisible();

  const canvas = renderedModel;
  await page.waitForTimeout(250);
  const beforeImage = await canvas.screenshot();
  expect(beforeImage.byteLength).toBeGreaterThan(5_000);

  const widthInput = page.getByRole("spinbutton", { name: "Bredd", exact: true });
  await expect(widthInput).toHaveValue("4200");
  const widthHandle = page.getByRole("button", { name: "Dra för att ändra bredd" });
  await widthHandle.focus();
  await widthHandle.press("ArrowRight");

  await expect(widthInput).not.toHaveValue("4200");
  const beforeHash = imageHash(beforeImage);
  const changedWidth = await widthInput.inputValue();
  await expect(page.locator(".canvas-dimensions")).toContainText(`${changedWidth} mm`);
  await expect.poll(
    async () => imageHash(await canvas.screenshot()),
    { message: "Modellvyn ska rita den ändrade bredden", timeout: 15_000 },
  ).not.toBe(beforeHash);
  const afterImage = await canvas.screenshot();
  expect(afterImage.byteLength).toBeGreaterThan(5_000);
  expect(imageHash(afterImage)).not.toBe(beforeHash);
  await testInfo.attach("3d-fore-drag.png", { body: beforeImage, contentType: "image/png" });
  await testInfo.attach("3d-efter-drag.png", { body: afterImage, contentType: "image/png" });

  await expect(page.getByRole("heading", { name: "Fack och hyllor", exact: true })).toBeVisible();
  await page.getByRole("radio", { name: "Minsta bredd" }).check();
  const targetBayWidth = page.getByRole("spinbutton", { name: "Minsta fria fackbredd" });
  await targetBayWidth.fill("300");
  await targetBayWidth.press("Enter");
  await expect(page.locator(".cb-context-panel").getByRole("status")).toContainText(/\d+ fack/);
  await expect(page.locator(".cb-context-panel").getByRole("status")).toContainText(/Cirka .* mm fri bredd per fack/);

  const readabilityTargets = [
    [".cb-workspace-navigation button", 14],
    [".cb-context-panel__header h2", 20],
    [".cb-workspace-navigation button small", 10],
    [".cb-dimension-input > label", 12],
    [".cb-dimension-input input", 13],
    [".cb-context-panel section header p", 11],
  ] as const;
  for (const [selector, minimumPx] of readabilityTargets) {
    const size = await page.locator(selector).first().evaluate((element) => Number.parseFloat(getComputedStyle(element).fontSize));
    expect(size, `${selector} ska vara minst ${minimumPx}px`).toBeGreaterThanOrEqual(minimumPx);
  }

  expect(pageErrors).toEqual([]);
  expect(failedRequests).toEqual([]);
});

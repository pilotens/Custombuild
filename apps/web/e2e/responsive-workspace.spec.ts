import { expect, test, type Page } from "@playwright/test";
import { chooseTemplateAndCreate, openPlanning, startWithEmptyPlanningStorage } from "./planning-helpers";

test.skip(
  process.env.PLAYWRIGHT_REAL_API === "1",
  "The deterministic responsive checks use the offline production build.",
);

async function expectNoHorizontalOverflow(page: Page) {
  const widths = await page.evaluate(() => ({
    viewport: window.innerWidth,
    documentWidth: document.documentElement.scrollWidth,
    bodyWidth: document.body.scrollWidth,
  }));
  expect(widths.documentWidth).toBeLessThanOrEqual(widths.viewport);
  expect(widths.bodyWidth).toBeLessThanOrEqual(widths.viewport);
}

async function expectPanelsDoNotOverlap(page: Page) {
  const viewer = await page.getByRole("region", { name: "Konstruktionsvy" }).boundingBox();
  const rail = await page.locator(".right-rail.configurator-rail").boundingBox();
  expect(viewer).not.toBeNull();
  expect(rail).not.toBeNull();
  if (!viewer || !rail) return;
  const overlapWidth = Math.min(viewer.x + viewer.width, rail.x + rail.width) - Math.max(viewer.x, rail.x);
  const overlapHeight = Math.min(viewer.y + viewer.height, rail.y + rail.height) - Math.max(viewer.y, rail.y);
  if (page.viewportSize()?.width && page.viewportSize()!.width <= 920) {
    // The small-screen Studio intentionally presents the inspector as a
    // bottom-sheet surface over the lower edge of the model.
    expect(rail.y).toBeGreaterThan(viewer.y + viewer.height * 0.45);
    expect(rail.x).toBeGreaterThanOrEqual(viewer.x - 1);
    expect(rail.x + rail.width).toBeLessThanOrEqual(viewer.x + viewer.width + 1);
  } else {
    expect(overlapWidth > 1 && overlapHeight > 1).toBe(false);
  }
}

async function expectExploreFitsViewport(page: Page) {
  const viewport = page.viewportSize();
  const explore = await page.locator("section.template-picker[data-presentation='embedded']").boundingBox();
  expect(viewport).not.toBeNull();
  expect(explore).not.toBeNull();
  if (!viewport || !explore) return;
  expect(explore.x).toBeGreaterThanOrEqual(0);
  expect(explore.x + explore.width).toBeLessThanOrEqual(viewport.width + 1);
}

test("embedded Explore and all persistent workspace modes reflow without document overflow", async ({ page }) => {
  test.setTimeout(120_000);
  await page.setViewportSize({ width: 1280, height: 900 });
  await startWithEmptyPlanningStorage(page);
  await page.goto("/", { waitUntil: "networkidle" });
  await openPlanning(page);

  for (const viewport of [
    { width: 1280, height: 900 },
    { width: 1440, height: 900 },
    { width: 1680, height: 900 },
    { width: 1920, height: 1080 },
    { width: 1024, height: 900 },
    { width: 768, height: 900 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    await expect(page.getByRole("heading", { name: "Vad vill du skapa?" })).toBeVisible();
    await expect(page.getByRole("dialog", { name: "Vad vill du skapa?" })).toHaveCount(0);
    await expectNoHorizontalOverflow(page);
    await expectExploreFitsViewport(page);
  }

  await page.setViewportSize({ width: 1280, height: 900 });
  await chooseTemplateAndCreate(page, "Väggbibliotek");
  const modes = page.getByRole("navigation", { name: "Produktlägen" });
  const model = page.getByLabel("Interaktiv 3D-modell av möbeln");
  await model.evaluate((element) => element.setAttribute("data-responsive-model-instance", "persistent"));

  for (const viewport of [
    { width: 1280, height: 900 },
    { width: 1440, height: 900 },
    { width: 1680, height: 900 },
    { width: 1920, height: 1080 },
    { width: 1024, height: 900 },
    { width: 768, height: 900 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    for (const mode of ["Studio", "Kontroll", "Underlag"] as const) {
      if (viewport.width <= 390 && mode === "Underlag") {
        // The compact mode strip is horizontally scrollable; ensure the last
        // mode is brought into view before exercising it.
        await modes.getByRole("button", { name: /Underlag/ }).scrollIntoViewIfNeeded();
      }
      await modes.getByRole("button", { name: new RegExp(mode) }).click();
      await expect(modes.getByRole("button", { name: new RegExp(mode) })).toHaveAttribute("aria-current", "page");
      await expect(page.getByRole("region", { name: "Konstruktionsvy" })).toBeVisible();
      await expect(model).toHaveAttribute("data-responsive-model-instance", "persistent");
      await expect(page.locator(".save-state")).toBeVisible();
      await expectNoHorizontalOverflow(page);
      await expectPanelsDoNotOverlap(page);
    }

    // The redesigned workspace keeps one persistent, horizontally scrollable
    // product-mode navigation on every viewport instead of duplicating mobile
    // and desktop navigation trees.
    await expect(modes).toBeVisible();
    await expect(modes.getByRole("button")).toHaveCount(4);
    await expect(page.locator(".side-nav nav, .mobile-nav")).toHaveCount(0);
  }
});

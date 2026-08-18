import { expect, test, type Page } from "@playwright/test";
import { openPlanning, startWithEmptyPlanningStorage } from "./planning-helpers";

test.skip(
  process.env.PLAYWRIGHT_REAL_API === "1",
  "Visual regression uses one deterministic offline project.",
);

const visualCases = [
  { name: "desktop-1440x960", viewport: { width: 1_440, height: 960 } },
  { name: "mobile-390x844", viewport: { width: 390, height: 844 } },
] as const;

const screenshotOptions = {
  animations: "disabled" as const,
  caret: "hide" as const,
  maxDiffPixels: 0,
  scale: "css" as const,
  threshold: 0.1,
};

const persistentCanvasIdentity = "p19-shared-viewer-canvas";

interface ProjectedModelMetrics {
  heightRatio: number;
  pixelRatio: number;
  widthRatio: number;
}

test.use({ video: "off" });

async function freezeVisualMotion(page: Page): Promise<void> {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.evaluate(async () => document.fonts.ready);
}

async function settleViewport(page: Page, width: number, withModel = false): Promise<void> {
  await expect(page.locator(".save-state")).toContainText("Sparad lokalt");
  await page.evaluate(async () => document.fonts.ready);
  await page.evaluate(() => window.scrollTo(0, 0));

  if (withModel) {
    const model = page.getByLabel("Interaktiv 3D-modell av möbeln");
    const canvas = model.locator("canvas");
    await expect(model).toBeVisible();
    await expect(canvas).toBeVisible();
    await expect.poll(async () => canvas.evaluate((element) => {
      const renderCanvas = element as HTMLCanvasElement;
      return renderCanvas.height * renderCanvas.width;
    })).toBeGreaterThan(0);
  }

  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
}

async function projectedModelMetrics(page: Page): Promise<ProjectedModelMetrics> {
  const canvas = page.getByLabel("Interaktiv 3D-modell av möbeln").locator("canvas");
  const screenshot = await canvas.screenshot({ animations: "disabled", scale: "css" });
  return page.evaluate(async (encodedPng) => {
    const binary = atob(encodedPng);
    const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
    const bitmap = await createImageBitmap(new Blob([bytes], { type: "image/png" }));
    const scratch = document.createElement("canvas");
    scratch.width = bitmap.width;
    scratch.height = bitmap.height;
    const context = scratch.getContext("2d", { willReadFrequently: true });
    context?.drawImage(bitmap, 0, 0);
    bitmap.close();
    const pixels = context?.getImageData(0, 0, scratch.width, scratch.height).data;
    if (!pixels) throw new Error("Canvas screenshot could not be decoded for projected-model verification.");

    let count = 0;
    let minX = scratch.width;
    let minY = scratch.height;
    let maxX = -1;
    let maxY = -1;
    for (let index = 0; index < pixels.length; index += 4) {
      const red = pixels[index] ?? 0;
      const green = pixels[index + 1] ?? 0;
      const blue = pixels[index + 2] ?? 0;
      const isWarmModelPixel = red - blue >= 18
        && red >= 70
        && green >= 55
        && blue < 190
        && red >= green - 3;
      if (!isWarmModelPixel) continue;
      const pixel = index / 4;
      const x = pixel % scratch.width;
      const y = Math.floor(pixel / scratch.width);
      count += 1;
      minX = Math.min(minX, x);
      minY = Math.min(minY, y);
      maxX = Math.max(maxX, x);
      maxY = Math.max(maxY, y);
    }

    return {
      heightRatio: maxY >= minY ? (maxY - minY + 1) / scratch.height : 0,
      pixelRatio: count / (scratch.width * scratch.height),
      widthRatio: maxX >= minX ? (maxX - minX + 1) / scratch.width : 0,
    };
  }, screenshot.toString("base64"));
}

async function verifyPersistentVisibleModel(page: Page): Promise<void> {
  const canvas = page.getByLabel("Interaktiv 3D-modell av möbeln").locator("canvas");
  await expect(canvas).toHaveAttribute("data-visual-regression-identity", persistentCanvasIdentity);
  const box = await canvas.boundingBox();
  expect(box).not.toBeNull();
  if (!box) throw new Error("The shared model canvas has no layout box.");
  expect(box.height).toBeGreaterThanOrEqual(240);
  expect(box.width).toBeGreaterThanOrEqual(300);

  const projection = await projectedModelMetrics(page);
  expect(projection.widthRatio).toBeGreaterThan(0.45);
  expect(projection.heightRatio).toBeGreaterThan(0.35);
  expect(projection.pixelRatio).toBeGreaterThan(0.05);
}

async function openDeterministicWallLibrary(page: Page): Promise<void> {
  const explore = await openPlanning(page);
  await explore.getByRole("button", { name: /Välj en design/ }).click();
  await expect(explore.getByRole("heading", { name: "Välj en startmodell att forma vidare." })).toBeVisible();

  const wallLibrary = explore.locator("button.template-card", { hasText: /Väggbibliotek/ });
  await expect(wallLibrary).toHaveCount(1);
  await wallLibrary.click();
  await explore.getByRole("button", { name: "Öppna Väggbibliotek i Studio" }).click();

  const closeChangeSummary = page.getByRole("button", { name: "Stäng ändringsöversikt" });
  if (await closeChangeSummary.isVisible()) {
    if ((page.viewportSize()?.width ?? 0) <= 820) {
      const closeBox = await closeChangeSummary.boundingBox();
      expect(closeBox).not.toBeNull();
      if (!closeBox) throw new Error("The mobile change-summary close control has no pointer box.");
      const pointerTarget = await page.evaluate(({ x, y }) => (
        document.elementFromPoint(x, y)?.closest("button")?.getAttribute("aria-label") ?? null
      ), {
        x: closeBox.x + closeBox.width / 2,
        y: closeBox.y + closeBox.height / 2,
      });
      expect(pointerTarget).toBe("Stäng ändringsöversikt");
    }
    await closeChangeSummary.click();
  }
  await expect(closeChangeSummary).toHaveCount(0);
}

async function verifyMobileViewerToolbar(page: Page): Promise<void> {
  if ((page.viewportSize()?.width ?? Number.POSITIVE_INFINITY) > 820) return;

  const toolbar = page.getByRole("toolbar", { name: "Visningsverktyg" });
  const controls = page.getByLabel("Visningskontroller, horisontellt rullningsbara");
  const buttons = controls.getByRole("button");
  const targets = await buttons.evaluateAll((elements) => elements.map((element) => {
    const box = element.getBoundingClientRect();
    return {
      bottom: box.bottom,
      clientWidth: element.clientWidth,
      height: box.height,
      left: box.left,
      right: box.right,
      scrollWidth: element.scrollWidth,
      top: box.top,
      width: box.width,
    };
  }));

  expect(targets.length).toBeGreaterThanOrEqual(8);
  for (const target of targets) {
    expect(target.width).toBeGreaterThanOrEqual(44);
    expect(target.height).toBeGreaterThanOrEqual(44);
    expect(target.scrollWidth).toBeLessThanOrEqual(target.clientWidth);
  }
  for (let leftIndex = 0; leftIndex < targets.length; leftIndex += 1) {
    for (let rightIndex = leftIndex + 1; rightIndex < targets.length; rightIndex += 1) {
      const left = targets[leftIndex];
      const right = targets[rightIndex];
      if (!left || !right) continue;
      const overlapsVertically = left.top < right.bottom && right.top < left.bottom;
      const overlapsHorizontally = left.left < right.right && right.left < left.right;
      expect(overlapsVertically && overlapsHorizontally).toBe(false);
    }
  }

  const overflow = await controls.evaluate((element) => ({
    clientWidth: element.clientWidth,
    scrollWidth: element.scrollWidth,
  }));
  expect(overflow.scrollWidth).toBeGreaterThan(overflow.clientWidth);

  const front = controls.getByRole("button", { name: "Front", exact: true });
  const frontBox = await front.boundingBox();
  expect(frontBox).not.toBeNull();
  if (!frontBox) throw new Error("The mobile Front control has no pointer box.");
  const frontPointerTarget = await page.evaluate(({ x, y }) => (
    document.elementFromPoint(x, y)?.closest("button")?.textContent?.trim() ?? null
  ), {
    x: frontBox.x + frontBox.width / 2,
    y: frontBox.y + frontBox.height / 2,
  });
  expect(frontPointerTarget).toBe("Front");
  await front.click();
  await expect(front).toHaveAttribute("aria-pressed", "true");
  const perspective = controls.getByRole("button", { name: "3D", exact: true });
  await perspective.click();
  await expect(perspective).toHaveAttribute("aria-pressed", "true");

  await controls.evaluate((element) => { element.scrollLeft = 0; });
  await controls.focus();
  await page.keyboard.press("ArrowRight");
  await expect.poll(() => controls.evaluate((element) => element.scrollLeft)).toBeGreaterThan(0);
  await controls.evaluate((element) => {
    element.scrollLeft = 0;
    (element as HTMLElement).blur();
  });

  const toolbarBox = await toolbar.boundingBox();
  const healthBox = await toolbar.locator(".design-health-pill").boundingBox();
  expect(toolbarBox).not.toBeNull();
  expect(healthBox).not.toBeNull();
  if (!toolbarBox || !healthBox) throw new Error("The mobile construction status has no visible box.");
  expect(healthBox.x).toBeGreaterThanOrEqual(toolbarBox.x);
  expect(healthBox.x + healthBox.width).toBeLessThanOrEqual(toolbarBox.x + toolbarBox.width);
}

async function verifyMobileComponentPalette(page: Page): Promise<void> {
  if ((page.viewportSize()?.width ?? Number.POSITIVE_INFINITY) > 760) return;

  const palette = page.getByRole("region", { name: "Lägg till delar" });
  const shelf = palette.getByRole("button", { name: /Hyllplan/ });
  const divider = palette.getByRole("button", { name: /Avdelare/ });
  const row = shelf.locator("..");
  await expect(palette).toBeVisible();
  await expect(shelf).toBeVisible();
  await expect(divider).toBeVisible();

  const paletteBox = await palette.boundingBox();
  const shelfBox = await shelf.boundingBox();
  const dividerBox = await divider.boundingBox();
  expect(paletteBox).not.toBeNull();
  expect(shelfBox).not.toBeNull();
  expect(dividerBox).not.toBeNull();
  if (!paletteBox || !shelfBox || !dividerBox) throw new Error("The mobile component row has no visible layout box.");
  expect(paletteBox.height).toBeGreaterThanOrEqual(110);
  for (const box of [shelfBox, dividerBox]) {
    expect(box.width).toBeGreaterThanOrEqual(44);
    expect(box.height).toBeGreaterThanOrEqual(64);
    expect(box.y).toBeGreaterThanOrEqual(paletteBox.y);
    expect(box.y + box.height).toBeLessThanOrEqual(paletteBox.y + paletteBox.height);
  }

  const overflow = await row.evaluate((element) => ({
    clientWidth: element.clientWidth,
    scrollWidth: element.scrollWidth,
  }));
  expect(overflow.scrollWidth).toBeGreaterThan(overflow.clientWidth);

  const pointerTarget = await page.evaluate(({ x, y }) => (
    document.elementFromPoint(x, y)?.closest("button")?.textContent?.trim() ?? null
  ), {
    x: shelfBox.x + shelfBox.width / 2,
    y: shelfBox.y + shelfBox.height / 2,
  });
  expect(pointerTarget).toContain("Hyllplan");

  await shelf.focus();
  await expect(shelf).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(divider).toBeFocused();
  await divider.evaluate((element) => element.blur());
}

for (const visualCase of visualCases) {
  test.describe(visualCase.name, () => {
    test.use({
      viewport: visualCase.viewport,
    });

    test("locks Utforska, Studio, Kontroll and Underlag", async ({ page }) => {
      test.setTimeout(120_000);
      await startWithEmptyPlanningStorage(page);
      await page.goto("/", { waitUntil: "networkidle" });
      await freezeVisualMotion(page);

      const explore = await openPlanning(page);
      await expect(explore.getByRole("heading", { name: "Vad vill du skapa?" })).toBeVisible();
      await expect(explore).toHaveAttribute("data-presentation", "embedded");
      await settleViewport(page, visualCase.viewport.width);
      await expect(page).toHaveScreenshot(`utforska-${visualCase.name}.png`, screenshotOptions);

      await openDeterministicWallLibrary(page);
      const modes = page.getByRole("navigation", { name: "Produktlägen" });
      await expect(modes.getByRole("button", { name: /Studio/ })).toHaveAttribute("aria-current", "page");
      await expect(page.getByRole("heading", { name: "Möbel", exact: true })).toBeVisible();
      await page.getByLabel("Interaktiv 3D-modell av möbeln").locator("canvas").evaluate((canvas, identity) => {
        canvas.setAttribute("data-visual-regression-identity", identity);
      }, persistentCanvasIdentity);
      await verifyMobileViewerToolbar(page);
      await verifyMobileComponentPalette(page);
      await settleViewport(page, visualCase.viewport.width, true);
      await verifyPersistentVisibleModel(page);
      await expect(page).toHaveScreenshot(`studio-${visualCase.name}.png`, screenshotOptions);

      await modes.getByRole("button", { name: /Kontroll/ }).click();
      await expect(modes.getByRole("button", { name: /Kontroll/ })).toHaveAttribute("aria-current", "page");
      await expect(page.getByLabel("Kontrollera konstruktionen").getByRole("heading", {
        name: "Kontrollera konstruktionen",
      })).toBeVisible();
      await settleViewport(page, visualCase.viewport.width, true);
      await verifyPersistentVisibleModel(page);
      await expect(page).toHaveScreenshot(`kontroll-${visualCase.name}.png`, screenshotOptions);

      await modes.getByRole("button", { name: /Underlag/ }).click();
      await expect(modes.getByRole("button", { name: /Underlag/ })).toHaveAttribute("aria-current", "page");
      await expect(page.getByRole("dialog", { name: "Skapa underlag" })).toHaveCount(0);
      await expect(page.getByLabel("Underlagets innehåll")).toBeVisible();
      await settleViewport(page, visualCase.viewport.width, true);
      await verifyPersistentVisibleModel(page);
      await expect(page).toHaveScreenshot(`underlag-${visualCase.name}.png`, screenshotOptions);
    });
  });
}

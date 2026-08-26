import { expect, test, type Locator, type Page } from "@playwright/test";
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

const persistentViewerIdentity = "p19-shared-furniture-viewer";
const viewerRenderCommitAttribute = "data-custombuild-render-commit";
const viewerRenderQuietWindowMs = 300;

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

function renderedModelSurface(page: Page) {
  return page.getByTestId("furniture-viewer")
    .locator("canvas, [data-testid='front-projection-fallback'] svg")
    .first();
}

async function waitForRenderedModelToSettle(surface: Locator): Promise<void> {
  const tagName = await surface.evaluate((element) => element.tagName);
  if (tagName !== "CANVAS") return;

  await expect(surface).toHaveAttribute(viewerRenderCommitAttribute, /^[1-9]\d*$/);
  // Drei Bounds invalidates the demand renderer while its camera fit is still
  // interpolating. Wait on the renderer's commit contract so a view switch
  // cannot preserve a load-dependent intermediate OrbitControls target.
  await surface.evaluate((element, { attribute, quietWindowMs }) => new Promise<void>((resolve, reject) => {
    let quietTimer = 0;
    const timeoutTimer = window.setTimeout(() => {
      observer.disconnect();
      window.clearTimeout(quietTimer);
      reject(new Error(`The WebGL renderer did not settle within 5 seconds (${attribute}).`));
    }, 5_000);

    const finish = () => {
      observer.disconnect();
      window.clearTimeout(timeoutTimer);
      resolve();
    };
    const scheduleFinish = () => {
      window.clearTimeout(quietTimer);
      quietTimer = window.setTimeout(finish, quietWindowMs);
    };
    const observer = new MutationObserver((mutations) => {
      if (mutations.some((mutation) => mutation.attributeName === attribute)) scheduleFinish();
    });
    observer.observe(element, { attributeFilter: [attribute], attributes: true });
    scheduleFinish();
  }), {
    attribute: viewerRenderCommitAttribute,
    quietWindowMs: viewerRenderQuietWindowMs,
  });
}

async function settleViewport(page: Page, width: number, withModel = false): Promise<void> {
  await expect(page.locator(".save-state")).toContainText("Sparad lokalt");
  await page.evaluate(async () => document.fonts.ready);
  await page.evaluate(() => window.scrollTo(0, 0));

  if (withModel) {
    const viewer = page.getByTestId("furniture-viewer");
    const surface = renderedModelSurface(page);
    await expect(viewer).toBeVisible();
    await expect(viewer).toHaveAttribute("data-renderer", /^(webgl|front-projection)$/);
    await expect(surface).toBeVisible();
    await expect.poll(async () => surface.evaluate((element) => {
      if (element instanceof HTMLCanvasElement) return element.height * element.width;
      const bounds = element.getBoundingClientRect();
      return bounds.height * bounds.width;
    })).toBeGreaterThan(0);
    await waitForRenderedModelToSettle(surface);
  }

  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
}

async function waitForHorizontalScrollToSettle(scroller: Locator): Promise<void> {
  await scroller.evaluate((element) => new Promise<void>((resolve) => {
    let settleTimer = 0;

    function finish() {
      element.removeEventListener("scroll", scheduleFinish);
      resolve();
    }

    function scheduleFinish() {
      window.clearTimeout(settleTimer);
      settleTimer = window.setTimeout(finish, 150);
    }

    element.addEventListener("scroll", scheduleFinish, { passive: true });
    scheduleFinish();
  }));
}

async function projectedModelMetrics(page: Page): Promise<ProjectedModelMetrics> {
  const screenshot = await renderedModelSurface(page).screenshot({ animations: "disabled", scale: "css" });
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
    if (!pixels) throw new Error("Model-surface screenshot could not be decoded for projected-model verification.");

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
  const viewer = page.getByTestId("furniture-viewer");
  const surface = renderedModelSurface(page);
  await expect(viewer).toHaveAttribute("data-visual-regression-identity", persistentViewerIdentity);
  const box = await surface.boundingBox();
  expect(box).not.toBeNull();
  if (!box) throw new Error("The shared model surface has no layout box.");
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

  const surface = renderedModelSurface(page);
  await expect(surface).toBeVisible();
  await waitForRenderedModelToSettle(surface);

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
  await waitForRenderedModelToSettle(surface);
  const perspective = controls.getByRole("button", { name: "3D", exact: true });
  await perspective.click();
  await expect(perspective).toHaveAttribute("aria-pressed", "true");
  await waitForRenderedModelToSettle(surface);

  await controls.evaluate((element) => {
    (element as HTMLElement).style.scrollBehavior = "auto";
    element.scrollLeft = 0;
  });
  await expect.poll(() => controls.evaluate((element) => element.scrollLeft)).toBe(0);
  await controls.focus();
  await page.keyboard.press("ArrowRight");
  await expect.poll(() => controls.evaluate((element) => element.scrollLeft)).toBeGreaterThan(0);
  await waitForHorizontalScrollToSettle(controls);
  await controls.evaluate((element) => {
    const controlsElement = element as HTMLElement;
    controlsElement.blur();
    controlsElement.style.scrollBehavior = "auto";
    controlsElement.scrollLeft = 0;
  });
  await expect.poll(() => controls.evaluate((element) => element.scrollLeft)).toBe(0);

  const toolbarBox = await toolbar.boundingBox();
  const healthBox = await toolbar.locator(".design-health-pill").boundingBox();
  expect(toolbarBox).not.toBeNull();
  expect(healthBox).not.toBeNull();
  if (!toolbarBox || !healthBox) throw new Error("The mobile construction status has no visible box.");
  expect(healthBox.x).toBeGreaterThanOrEqual(toolbarBox.x);
  expect(healthBox.x + healthBox.width).toBeLessThanOrEqual(toolbarBox.x + toolbarBox.width);
}

async function verifyMobileViewerOverlayLayout(page: Page): Promise<void> {
  if ((page.viewportSize()?.width ?? Number.POSITIVE_INFINITY) > 760) return;

  const modelLabel = page.getByTestId("current-design-label");
  const dimensions = page.getByLabel("Aktuella yttermått");
  await expect(modelLabel).toBeHidden();
  const dimensionsBox = await dimensions.boundingBox();
  expect(dimensionsBox).not.toBeNull();
  if (!dimensionsBox) throw new Error("The mobile model dimensions have no visible layout box.");

  const serverBanner = page.locator(".offline-banner:visible");
  if (await serverBanner.count()) {
    const serverBannerBox = await serverBanner.first().boundingBox();
    expect(serverBannerBox).not.toBeNull();
    if (!serverBannerBox) throw new Error("The mobile server banner has no visible layout box.");
    expect(dimensionsBox.y).toBeGreaterThanOrEqual(serverBannerBox.y + serverBannerBox.height + 4);
  }

  const partSelector = page.getByLabel("Välj möbeldel att inspektera");
  const placeholderFit = await partSelector.evaluate((element) => {
    if (!(element instanceof HTMLSelectElement)) throw new Error("The part selector is not a select element.");
    const style = getComputedStyle(element);
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d");
    if (!context) throw new Error("Could not measure the part-selector placeholder.");
    context.font = style.font || `${style.fontSize} ${style.fontFamily}`;
    const text = element.selectedOptions[0]?.text ?? "";
    const availableWidth = element.clientWidth
      - Number.parseFloat(style.paddingLeft)
      - Number.parseFloat(style.paddingRight);
    return { availableWidth, text, textWidth: context.measureText(text).width };
  });
  expect(placeholderFit.text).toBe("Välj del");
  expect(placeholderFit.textWidth).toBeLessThanOrEqual(placeholderFit.availableWidth);
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
  await row.evaluate((element) => {
    (element as HTMLElement).style.scrollBehavior = "auto";
    element.scrollLeft = 0;
  });
  await expect.poll(() => row.evaluate((element) => element.scrollLeft)).toBe(0);
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
      await expect.soft(page).toHaveScreenshot(`utforska-${visualCase.name}.png`, screenshotOptions);

      await openDeterministicWallLibrary(page);
      const modes = page.getByRole("navigation", { name: "Produktlägen" });
      await expect(modes.getByRole("button", { name: /Studio/ })).toHaveAttribute("aria-current", "page");
      await expect(page.getByRole("heading", { name: "Möbel", exact: true })).toBeVisible();
      await page.getByTestId("furniture-viewer").evaluate((viewer, identity) => {
        viewer.setAttribute("data-visual-regression-identity", identity);
      }, persistentViewerIdentity);
      await verifyMobileViewerToolbar(page);
      await verifyMobileComponentPalette(page);
      await settleViewport(page, visualCase.viewport.width, true);
      await verifyPersistentVisibleModel(page);
      await verifyMobileViewerOverlayLayout(page);
      await expect.soft(page).toHaveScreenshot(`studio-${visualCase.name}.png`, screenshotOptions);

      await modes.getByRole("button", { name: /Kontroll/ }).click();
      await expect(modes.getByRole("button", { name: /Kontroll/ })).toHaveAttribute("aria-current", "page");
      await expect(page.getByLabel("Kontrollera konstruktionen").getByRole("heading", {
        name: "Kontrollera konstruktionen",
      })).toBeVisible();
      await settleViewport(page, visualCase.viewport.width, true);
      await verifyPersistentVisibleModel(page);
      await verifyMobileViewerOverlayLayout(page);
      await expect.soft(page).toHaveScreenshot(`kontroll-${visualCase.name}.png`, screenshotOptions);

      await modes.getByRole("button", { name: /Underlag/ }).click();
      await expect(modes.getByRole("button", { name: /Underlag/ })).toHaveAttribute("aria-current", "page");
      await expect(page.getByRole("dialog", { name: "Skapa underlag" })).toHaveCount(0);
      await expect(page.getByLabel("Underlagets innehåll")).toBeVisible();
      await settleViewport(page, visualCase.viewport.width, true);
      await verifyPersistentVisibleModel(page);
      await verifyMobileViewerOverlayLayout(page);
      await expect.soft(page).toHaveScreenshot(`underlag-${visualCase.name}.png`, screenshotOptions);
    });
  });
}

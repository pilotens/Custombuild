import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Locator, type Page, type TestInfo } from "@playwright/test";
import { chooseTemplateAndCreate, openPlanning, startWithEmptyPlanningStorage } from "./planning-helpers";

const WCAG_22_AA_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22a", "wcag22aa"];
const MODE_NAVIGATION_NAME = "Produktlägen";
const WORKSPACE_SCOPE = "#workspace";

const SURFACES = [
  { name: "desktop", viewport: { width: 1_440, height: 960 } },
  { name: "mobile", viewport: { width: 390, height: 844 } },
] as const;

async function waitForStableAccessibilityTree(page: Page) {
  await page.evaluate(async () => {
    await document.fonts.ready;
    await new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
  });
}

async function expectNoWcagViolations(
  page: Page,
  testInfo: TestInfo,
  evidenceName: string,
  include = WORKSPACE_SCOPE,
) {
  await waitForStableAccessibilityTree(page);
  const results = await new AxeBuilder({ page })
    .include(include)
    .withTags(WCAG_22_AA_TAGS)
    .analyze();
  await testInfo.attach(`${evidenceName}-axe-wcag-2.2-aa.json`, {
    body: Buffer.from(JSON.stringify(results, null, 2), "utf8"),
    contentType: "application/json",
  });
  const summary = results.violations.map((violation) => ({
    id: violation.id,
    impact: violation.impact,
    help: violation.help,
    nodes: violation.nodes.map((node) => ({ target: node.target, failureSummary: node.failureSummary })),
  }));
  expect(summary, JSON.stringify(summary, null, 2)).toEqual([]);
}

async function tabTo(page: Page, target: Locator, options?: { backwards?: boolean; limit?: number }) {
  const backwards = options?.backwards ?? false;
  const limit = options?.limit ?? 160;
  await expect(target).toBeVisible();
  for (let index = 0; index < limit; index += 1) {
    await page.keyboard.press(backwards ? "Shift+Tab" : "Tab");
    if (await target.evaluate((element) => element === document.activeElement)) return;
  }
  throw new Error(`Could not reach keyboard target after ${limit} ${backwards ? "reverse " : ""}Tab presses.`);
}

async function expectNoHorizontalDocumentOverflow(page: Page) {
  const widths = await page.evaluate(() => ({
    viewport: window.innerWidth,
    document: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
  }));
  expect(widths.document).toBeLessThanOrEqual(widths.viewport);
  expect(widths.body).toBeLessThanOrEqual(widths.viewport);
}

test.skip(
  process.env.PLAYWRIGHT_REAL_API === "1",
  "The deterministic accessibility gate runs against the offline production build.",
);
test.skip(
  ({ browserName }) => browserName !== "chromium",
  "The deterministic WCAG evidence is captured once in Chromium.",
);

test("embedded Explore is keyboard-safe and never traps focus like a modal", async ({ page }) => {
  await startWithEmptyPlanningStorage(page);
  await page.goto("/", { waitUntil: "networkidle" });

  const explore = await openPlanning(page);
  await expect(explore).toHaveAttribute("data-presentation", "embedded");
  await expect(explore).not.toHaveAttribute("role", "dialog");
  await expect(page.locator(".cb-planner-backdrop")).toHaveCount(0);
  await expect(explore.getByRole("button", { name: /Välj en design/ })).toBeVisible();

  await explore.getByRole("button", { name: /Välj en design/ }).focus();
  await page.keyboard.press("Enter");
  await expect(explore.getByRole("heading", { name: "Välj en startmodell att forma vidare." })).toBeVisible();
  await explore.getByRole("button", { name: "Till start" }).focus();
  await page.keyboard.press("Enter");
  await expect(explore.getByRole("heading", { name: "Vad vill du skapa?" })).toBeVisible();

  await explore.getByRole("button", { name: /Utgå från en bild/ }).focus();
  await page.keyboard.press("Enter");
  const importer = page.getByRole("dialog", { name: "Skapa från referensbild" });
  await expect(importer).toBeVisible();
  await expect.poll(() => importer.evaluate((element) => element.contains(document.activeElement))).toBe(true);
  await page.keyboard.press("Escape");
  await expect(importer).toBeHidden();
  await expect(explore.getByRole("heading", { name: "Vad vill du skapa?" })).toBeVisible();
});

for (const surface of SURFACES) {
  test(`axe WCAG 2.2 AA covers all four product modes on ${surface.name}`, async ({ page }, testInfo) => {
    test.setTimeout(120_000);
    await page.setViewportSize(surface.viewport);
    await startWithEmptyPlanningStorage(page);
    await page.goto("/", { waitUntil: "networkidle" });

    await openPlanning(page);
    await expectNoWcagViolations(page, testInfo, `${surface.name}-utforska`);

    await chooseTemplateAndCreate(page, "Hyllsystem");
    const modes = page.getByRole("navigation", { name: MODE_NAVIGATION_NAME });
    await expectNoWcagViolations(page, testInfo, `${surface.name}-studio`);

    for (const mode of [
      { button: /Kontroll/, evidence: "kontroll" },
      { button: /Underlag/, evidence: "underlag" },
    ] as const) {
      const modeButton = modes.getByRole("button", { name: mode.button });
      await modeButton.scrollIntoViewIfNeeded();
      await modeButton.click();
      await expect(modeButton).toHaveAttribute("aria-current", "page");
      await expectNoWcagViolations(page, testInfo, `${surface.name}-${mode.evidence}`);
    }
  });
}

test("keyboard-only journey reaches the Underlag design-review surface from Explore", async ({ page }) => {
  test.setTimeout(120_000);
  await page.setViewportSize({ width: 1_440, height: 960 });
  await startWithEmptyPlanningStorage(page);
  await page.goto("/", { waitUntil: "networkidle" });

  const explore = page.locator("section.template-picker[data-presentation='embedded']");
  const chooseDesign = explore.getByRole("button", { name: /Välj en design/ });
  await tabTo(page, chooseDesign);
  await expect(chooseDesign).toBeFocused();
  await page.keyboard.press("Enter");

  const template = explore.getByRole("button", { name: /^Inspirationsbild för Hyllsystem/ });
  await tabTo(page, template);
  await expect(template).toBeFocused();
  await page.keyboard.press("Enter");

  const openInStudio = explore.getByRole("button", { name: /Öppna Hyllsystem i Studio/ });
  await tabTo(page, openInStudio);
  await expect(openInStudio).toBeFocused();
  await page.keyboard.press("Enter");

  const checkAction = page.getByRole("button", { name: "Kontrollera konstruktionen" });
  await tabTo(page, checkAction);
  await expect(checkAction).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#workspace-mode-heading")).toHaveText("Kontrollera konstruktionen");

  const modes = page.getByRole("navigation", { name: MODE_NAVIGATION_NAME });
  const underlagMode = modes.getByRole("button", { name: /Underlag/ });
  await tabTo(page, underlagMode, { backwards: true, limit: 8 });
  await expect(underlagMode).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#workspace-mode-heading")).toHaveText("Designgranska och exportera underlag");

  const designReviewSurface = page.getByLabel("Underlagets innehåll");
  await tabTo(page, designReviewSurface);
  await expect(designReviewSurface).toBeFocused();
  await expect(designReviewSurface).toContainText(/Underlag är inte tillgängligt|Nästa steg|Kan inte skapa underlag/);
});

test("400-percent equivalent reflow keeps every mode within a 320 CSS-pixel viewport", async ({ page }) => {
  test.setTimeout(120_000);
  await page.setViewportSize({ width: 320, height: 800 });
  await startWithEmptyPlanningStorage(page);
  await page.goto("/", { waitUntil: "networkidle" });

  await openPlanning(page);
  await expectNoHorizontalDocumentOverflow(page);
  await expect(page.getByRole("heading", { name: "Vad vill du skapa?" })).toBeVisible();

  await chooseTemplateAndCreate(page, "Hyllsystem");
  const modes = page.getByRole("navigation", { name: MODE_NAVIGATION_NAME });
  for (const mode of ["Studio", "Kontroll", "Underlag"] as const) {
    const modeButton = modes.getByRole("button", { name: new RegExp(mode) });
    await modeButton.scrollIntoViewIfNeeded();
    await modeButton.click();
    await expect(modeButton).toHaveAttribute("aria-current", "page");
    await expect(page.getByRole("region", { name: "Konstruktionsvy" })).toBeVisible();
    await expectNoHorizontalDocumentOverflow(page);
  }
});

test("forced colors preserve focus and active-mode semantics", async ({ page }, testInfo) => {
  await page.emulateMedia({ forcedColors: "active" });
  await startWithEmptyPlanningStorage(page);
  await page.goto("/", { waitUntil: "networkidle" });
  await chooseTemplateAndCreate(page, "Hyllsystem");
  await waitForStableAccessibilityTree(page);

  expect(await page.evaluate(() => matchMedia("(forced-colors: active)").matches)).toBe(true);
  const studioMode = page.getByRole("navigation", { name: MODE_NAVIGATION_NAME })
    .getByRole("button", { name: /Studio/ });
  await tabTo(page, studioMode, { backwards: true, limit: 4 });
  await expect(studioMode).toBeFocused();
  await expect(studioMode).toHaveAttribute("aria-current", "page");
  const focusStyle = await studioMode.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      outlineStyle: style.outlineStyle,
      outlineWidth: style.outlineWidth,
      borderStyle: style.borderTopStyle,
    };
  });
  expect(focusStyle.outlineStyle).not.toBe("none");
  expect(Number.parseFloat(focusStyle.outlineWidth)).toBeGreaterThanOrEqual(2);
  expect(focusStyle.borderStyle).not.toBe("none");
  await testInfo.attach("forced-colors-studio.png", {
    body: await page.screenshot({ fullPage: true }),
    contentType: "image/png",
  });
});

test("reduced-motion preference suppresses smooth scrolling and component transitions", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await startWithEmptyPlanningStorage(page);
  await page.goto("/", { waitUntil: "networkidle" });
  const explore = await openPlanning(page);
  await waitForStableAccessibilityTree(page);

  expect(await page.evaluate(() => matchMedia("(prefers-reduced-motion: reduce)").matches)).toBe(true);
  expect(await page.evaluate(() => getComputedStyle(document.documentElement).scrollBehavior)).toBe("auto");
  const routeTransitionSeconds = await explore.getByRole("button", { name: /Välj en design/ })
    .evaluate((element) => getComputedStyle(element).transitionDuration
      .split(",")
      .map((duration) => Number.parseFloat(duration) * (duration.trim().endsWith("ms") ? 0.001 : 1)));
  expect(Math.max(...routeTransitionSeconds)).toBeLessThanOrEqual(0.000_01);

  await chooseTemplateAndCreate(page, "Hyllsystem");
  const studioMode = page.getByRole("navigation", { name: MODE_NAVIGATION_NAME })
    .getByRole("button", { name: /Studio/ });
  const modeTransitionSeconds = await studioMode.evaluate((element) => getComputedStyle(element).transitionDuration
    .split(",")
    .map((duration) => Number.parseFloat(duration) * (duration.trim().endsWith("ms") ? 0.001 : 1)));
  expect(Math.max(...modeTransitionSeconds)).toBeLessThanOrEqual(0.000_01);
});

test("dimensions, bay dividers and shelves all expose keyboard-operable numeric alternatives", async ({ page }) => {
  test.setTimeout(120_000);
  await startWithEmptyPlanningStorage(page);
  await page.goto("/", { waitUntil: "networkidle" });
  await chooseTemplateAndCreate(page, "Väggbibliotek");

  for (const name of ["Bredd", "Höjd", "Djup"] as const) {
    await expect(page.getByRole("spinbutton", { name, exact: true })).toBeVisible();
  }

  const width = page.getByRole("spinbutton", { name: "Bredd", exact: true });
  const widthBefore = await width.inputValue();
  const widthHandle = page.getByRole("button", { name: "Dra för att ändra bredd" });
  await widthHandle.focus();
  await page.keyboard.press("ArrowRight");
  await expect(width).not.toHaveValue(widthBefore);

  const height = page.getByRole("spinbutton", { name: "Höjd", exact: true });
  const heightBefore = await height.inputValue();
  const heightHandle = page.getByRole("button", { name: "Dra för att ändra höjd" });
  await heightHandle.focus();
  await page.keyboard.press("ArrowUp");
  await expect(height).not.toHaveValue(heightBefore);

  const detailedLayout = page.locator("summary", { hasText: "Detaljerad indelning" });
  await detailedLayout.focus();
  await page.keyboard.press("Enter");

  const bayWidth = page.getByRole("slider", { name: "Bredd för fack 1" });
  await expect(bayWidth).toBeVisible();
  const bayBefore = await bayWidth.getAttribute("aria-valuenow") ?? await bayWidth.inputValue();
  await bayWidth.focus();
  await page.keyboard.press("ArrowRight");
  await expect.poll(async () => await bayWidth.getAttribute("aria-valuenow") ?? await bayWidth.inputValue())
    .not.toBe(bayBefore);

  const shelfHeight = page.getByRole("slider", { name: "Höjd för hylla 1" });
  await expect(shelfHeight).toBeVisible();
  const shelfBefore = await shelfHeight.getAttribute("aria-valuenow") ?? await shelfHeight.inputValue();
  await shelfHeight.focus();
  await page.keyboard.press("ArrowRight");
  await expect.poll(async () => await shelfHeight.getAttribute("aria-valuenow") ?? await shelfHeight.inputValue())
    .not.toBe(shelfBefore);
});

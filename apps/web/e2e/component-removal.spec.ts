import { expect, test, type Locator, type Page } from "@playwright/test";
import { openPlanning, startWithEmptyPlanningStorage } from "./planning-helpers";

test.skip(
  process.env.PLAYWRIGHT_REAL_API === "1",
  "This regression uses an isolated offline design draft.",
);

test.use({
  video: "off",
  actionTimeout: 10_000,
  navigationTimeout: 30_000,
  contextOptions: { reducedMotion: "reduce" },
});

async function openWallLibrary(page: Page): Promise<void> {
  await startWithEmptyPlanningStorage(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  const planner = await openPlanning(page);
  await planner.getByRole("button", { name: /Välj en design/ }).click();
  await planner.locator("button.template-card", { hasText: /Väggbibliotek/ }).click();
  await planner.getByRole("button", { name: "Öppna Väggbibliotek i Studio" }).click();
  await expect(page.getByRole("region", { name: "Hantera delar" })).toBeVisible();
  const dismiss = page.getByRole("button", { name: "Stäng ändringsöversikt" });
  if (await dismiss.isVisible()) await dismiss.click();
}

async function componentCount(card: Locator): Promise<number> {
  return Number.parseInt(await card.locator("em").innerText(), 10);
}

async function assertContainedControl(
  page: Page,
  palette: Locator,
  control: Locator,
  minimumHeight: number,
): Promise<void> {
  const paletteBox = await palette.boundingBox();
  const box = await control.boundingBox();
  if (!paletteBox || !box) throw new Error("The component palette or its control has no layout box.");
  expect(box.width).toBeGreaterThanOrEqual(44);
  expect(box.height).toBeGreaterThanOrEqual(minimumHeight);
  expect(box.y).toBeGreaterThanOrEqual(paletteBox.y);
  expect(box.y + box.height).toBeLessThanOrEqual(paletteBox.y + paletteBox.height);
  expect(await control.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    const target = document.elementFromPoint(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
    return target !== null && (target === element || element.contains(target));
  })).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
}

for (const viewport of [
  { name: "desktop", width: 1_440, height: 960 },
  { name: "mobile", width: 390, height: 844 },
]) {
  test.describe(viewport.name, () => {
    test.use({ viewport: { width: viewport.width, height: viewport.height } });

    test("palette removes selected dividers, permits addition and preserves undo", async ({ page }, testInfo) => {
      test.setTimeout(90_000);
      const pageErrors: string[] = [];
      page.on("pageerror", (error) => pageErrors.push(error.message));
      await openWallLibrary(page);
      const palette = page.getByRole("region", { name: "Hantera delar" });
      const divider = palette.getByRole("group", { name: "Avdelare", exact: true });
      const shelf = palette.getByRole("group", { name: "Hyllplan", exact: true });
      const addDivider = divider.getByRole("button", { name: /Avdelare/ });
      const removeDivider = divider.getByRole("button", { name: "Ta bort avdelare", exact: true });
      const removeShelf = shelf.getByRole("button", { name: "Ta bort hyllplan", exact: true });
      // The starting model has only necessary supports. Add optional ones before
      // exercising removal; the last required support must remain protected.
      await expect(removeDivider).toBeDisabled();
      const requiredCount = await componentCount(divider);
      await addDivider.click();
      await expect.poll(() => componentCount(divider)).toBeGreaterThan(requiredCount);
      const partSelector = page.getByRole("combobox", { name: "Välj möbeldel att inspektera" });
      await partSelector.selectOption("divider-1");
      await expect(removeDivider).toBeEnabled();

      if (viewport.name === "mobile") {
        await assertContainedControl(page, palette, addDivider, 64);
        await assertContainedControl(page, palette, removeDivider, 44);
        await assertContainedControl(page, palette, removeShelf, 44);
        const addShelf = shelf.getByRole("button", { name: /Hyllplan/ });
        await addShelf.focus();
        await page.keyboard.press("Tab");
        await expect(removeShelf).toBeFocused();
        await page.keyboard.press("Tab");
        await expect(addDivider).toBeFocused();
        await page.keyboard.press("Tab");
        await expect(removeDivider).toBeFocused();
      }

      const initialCount = await componentCount(divider);
      expect(initialCount).toBeGreaterThan(1);
      await expect(partSelector).toHaveValue("divider-1");
      await removeDivider.click();
      await expect.poll(() => componentCount(divider)).toBe(initialCount - 1);
      await expect(partSelector).toHaveValue("");
      await expect(partSelector.locator("option[value^='divider-']")).toHaveCount(initialCount - 1);
      await expect(page.getByText("Avdelaren togs bort.", { exact: true })).toBeVisible();

      const undo = page.getByRole("button", { name: "Ångra", exact: true });
      const redo = page.getByRole("button", { name: "Gör om", exact: true });
      await undo.click();
      await expect.poll(() => componentCount(divider)).toBe(initialCount);
      await expect(partSelector.locator("option[value^='divider-']")).toHaveCount(initialCount);
      await redo.click();
      await expect.poll(() => componentCount(divider)).toBe(initialCount - 1);

      await addDivider.click();
      await expect.poll(() => componentCount(divider)).toBeGreaterThan(initialCount - 1);
      await undo.click();
      await expect.poll(() => componentCount(divider)).toBe(initialCount - 1);
      await undo.click();
      await expect.poll(() => componentCount(divider)).toBe(initialCount);
      expect(pageErrors).toEqual([]);
      await page.screenshot({ path: testInfo.outputPath(`component-palette-${viewport.name}.png`), animations: "disabled" });
    });
  });
}

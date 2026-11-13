import { expect, test, type Page } from "@playwright/test";
import { DEFAULT_DESIGN_SPEC, type DesignSpec } from "../lib/design-types";

const DRAFT_KEY = "custombuild:workspace:v3:anonymous:project:local-draft:draft";
const sourceSpec: DesignSpec = {
  ...DEFAULT_DESIGN_SPEC,
  design_id: "e2e-material-planning-wall-library",
  furniture_type: "wall_library",
  width_mm: 2_400,
  height_mm: 2_400,
  depth_mm: 340,
  divider_count: 3,
  base_cabinet_count: 4,
  base_cabinet_height_mm: 680,
  base_cabinet_depth_mm: 340,
  reinforcement_mode: "manual",
};

test.skip(process.env.PLAYWRIGHT_REAL_API === "1", "Uses an isolated offline draft, not workshop inventory.");
test.skip(({ browserName }) => browserName !== "chromium", "Runs once in serialized Chromium.");
test.use({
  video: "off",
  actionTimeout: 10_000,
  navigationTimeout: 20_000,
  contextOptions: { reducedMotion: "reduce" },
});

async function installDraft(page: Page, spec: DesignSpec = sourceSpec): Promise<void> {
  await page.addInitScript(({ key, spec }) => {
    const marker = "custombuild:e2e:material-planning-initialized";
    if (window.sessionStorage.getItem(marker) === "1") return;
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.localStorage.setItem(key, JSON.stringify({
      version: 3,
      spec,
      templateId: spec.furniture_type === "wall_library" ? "wall-library" : "shelving",
      workspaceSelected: true,
      uiState: {
        schemaVersion: 2,
        mode: "check",
        viewMode: "front",
        exploded: false,
        transparent: false,
        isolateSelection: false,
        panels: { componentLibraryOpen: true, contextPanelOpen: true, advancedPanelOpen: false },
      },
      updatedAt: "2026-09-25T00:00:00.000Z",
    }));
    window.sessionStorage.setItem(marker, "1");
  }, { key: DRAFT_KEY, spec });
}

async function savedSpec(page: Page): Promise<DesignSpec> {
  return page.evaluate((key) => {
    const value = window.localStorage.getItem(key);
    if (!value) throw new Error("The isolated material-planning draft disappeared.");
    return (JSON.parse(value) as { spec: DesignSpec }).spec;
  }, DRAFT_KEY);
}

async function expectPlannedStock(page: Page): Promise<void> {
  await expect.poll(async () => (await savedSpec(page)).stock_count).toBe(6);
  const spec = await savedSpec(page);
  expect(spec.stock_count).toBe(6);
  for (const key of [
    "back_stock_count", "stock_width_mm", "stock_height_mm",
    "back_stock_width_mm", "back_stock_height_mm", "wall_anchor_verified",
  ] as const) expect(spec[key], `Planning must not mutate ${key}`).toBe(sourceSpec[key]);
  expect(spec.workshop_context).toBeUndefined();
}

for (const viewport of [
  { name: "desktop", width: 1_440, height: 960 },
  { name: "mobile", width: 390, height: 844 },
]) {
  test.describe(viewport.name, () => {
    test.use({ viewport: { width: viewport.width, height: viewport.height } });

    test("adds supports for a wider bookcase and restores the complete design through undo and reload", async ({ page }) => {
      const pageErrors: string[] = [];
      page.on("pageerror", (error) => pageErrors.push(error.message));
      await installDraft(page, { ...DEFAULT_DESIGN_SPEC, width_mm: 600, divider_count: 0, reinforcement_mode: "manual" });
      await page.goto("/", { waitUntil: "domcontentloaded" });
      const modes = page.getByRole("navigation", { name: "Produktlägen" });
      await modes.getByRole("button", { name: /Studio/ }).click();
      const width = page.getByRole("spinbutton", { name: "Bredd", exact: true });
      await expect(width).toHaveValue("600");
      await width.fill("2200");
      await width.press("Enter");
      await expect.poll(async () => (await savedSpec(page)).width_mm).toBe(2200);
      const wide = await savedSpec(page);
      expect(wide.divider_count).toBeGreaterThan(0);
      expect(wide.load_per_shelf_kg).toBe(DEFAULT_DESIGN_SPEC.load_per_shelf_kg);
      await expect(page.getByRole("button", { name: "Ta bort avdelare", exact: true })).toBeDisabled();
      await page.getByRole("button", { name: "Ångra", exact: true }).click();
      await expect.poll(async () => (await savedSpec(page)).width_mm).toBe(600);
      expect((await savedSpec(page)).divider_count).toBe(0);
      await page.getByRole("button", { name: "Gör om", exact: true }).click();
      await expect.poll(async () => (await savedSpec(page)).width_mm).toBe(2200);
      expect((await savedSpec(page)).divider_count).toBe(wide.divider_count);
      await page.reload({ waitUntil: "domcontentloaded" });
      await expect(width).toHaveValue("2200");
      await modes.getByRole("button", { name: /Kontroll/ }).click();
      await expect(page.getByText("Inga blockerande designfel", { exact: true })).toBeVisible();
      expect(pageErrors).toEqual([]);
    });

    test("plans enough material before saving and prevents unsupported edits", async ({ page }, testInfo) => {
      test.setTimeout(75_000);
      const pageErrors: string[] = [];
      const apiMutations: string[] = [];
      page.on("pageerror", (error) => pageErrors.push(error.message));
      page.on("request", (request) => {
        if (new URL(request.url()).pathname.startsWith("/v1/") && !["GET", "HEAD", "OPTIONS"].includes(request.method())) {
          apiMutations.push(`${request.method()} ${request.url()}`);
        }
      });
      await installDraft(page);
      const response = await page.goto("/?mode=check", { waitUntil: "domcontentloaded" });
      expect(response?.status()).toBe(200);
      const panel = page.getByRole("region", { name: "Kontrollera konstruktionen", exact: true });
      const modes = page.getByRole("navigation", { name: "Produktlägen" });
      const stock = panel.getByRole("article", { name: "Delar ryms i råmaterial", exact: true });
      await expect(panel).toBeVisible();
      await expect(panel.locator(".validation-block")).toHaveCount(0);
      await expect(panel.getByText("Inga blockerande designfel", { exact: true })).toBeVisible();
      await expectPlannedStock(page);
      await panel.screenshot({ path: testInfo.outputPath(`material-planning-panel-${viewport.name}.png`), animations: "disabled" });
      await page.screenshot({ path: testInfo.outputPath(`material-planning-${viewport.name}.png`), fullPage: true, animations: "disabled" });
      await panel.locator("summary").filter({ hasText: /^Godkända kontroller/ }).click();
      await expect(stock.getByText("Beräknat behov: 8 skivor", { exact: true })).toBeVisible();
      await expect(stock.getByTestId("validation-state")).toHaveText("Godkänt");
      await expect(stock.getByText(/Tillgången är inte verifierad/)).toBeVisible();
      await expect(panel.getByText("Kontrollen är inte ett tillverkningstillstånd.", { exact: true })).toBeVisible();

      await stock.getByText("Visa detaljer", { exact: true }).click();
      const materialPlan = stock.getByRole("region", { name: "Automatiskt beräknat materialbehov" });
      await expect(materialPlan).toBeVisible();
      await expect(materialPlan.getByText("6 skivor à 2440 × 1220 mm. Angivet antal: 6.", { exact: true })).toBeVisible();
      await expect(materialPlan.getByText("2 skivor à 2440 × 1220 mm. Angivet antal: 2.", { exact: true })).toBeVisible();
      await expect(materialPlan).toContainText("Inga lageruppgifter ändras.");
      await stock.getByText("Visa detaljer", { exact: true }).click();

      for (const removal of [
        { button: "Ta bort hyllplan", field: "shelf_count", before: 5, after: 4 },
      ] as const) {
        await modes.getByRole("button", { name: /Studio/ }).click();
        const palette = page.getByRole("region", { name: "Hantera delar" });
        await palette.getByRole("button", { name: removal.button, exact: true }).click();
        await expect.poll(async () => (await savedSpec(page))[removal.field]).toBe(removal.after);
        expect((await savedSpec(page)).topology_baseline).toBeDefined();
        await modes.getByRole("button", { name: /Kontroll/ }).click();
        await expect(panel).toBeVisible();
        await expect(panel.getByText("Kontrollera ändrade delar", { exact: true })).toHaveCount(0);
        await expect(panel.getByText("Återställ saknade delar", { exact: true })).toHaveCount(0);
        await expect(panel.getByText(/Regel PART-CUSTOM-001/)).toHaveCount(0);
        await expect(panel.getByRole("button", { name: /Visa förslag:.*återställ/i })).toHaveCount(0);
        await expect(panel.locator(".validation-block")).toHaveCount(0);
        await expectPlannedStock(page);
        await page.getByRole("button", { name: "Ångra", exact: true }).click();
        await expect.poll(async () => (await savedSpec(page))[removal.field]).toBe(removal.before);
      }

      await modes.getByRole("button", { name: /Studio/ }).click();
      await expect(page.getByRole("button", { name: "Ta bort avdelare", exact: true })).toBeDisabled();
      await expect(page.getByRole("button", { name: "Ta bort bakstycke", exact: true })).toBeDisabled();
      expect((await savedSpec(page)).back_panel).toBe(true);

      const width = page.getByRole("spinbutton", { name: "Bredd", exact: true });
      await width.fill("5000");
      await width.press("Enter");
      await expect(width).toHaveValue("2400");
      await expect(width).toHaveAttribute("aria-invalid", "true");
      expect((await savedSpec(page)).width_mm).toBe(2400);
      await width.fill("100000");
      await width.press("Enter");
      await expect(width).toHaveAttribute("aria-invalid", "true");
      expect((await savedSpec(page)).width_mm).toBe(2400);
      await width.press("Escape");
      await modes.getByRole("button", { name: /Kontroll/ }).click();
      await expect(panel.locator(".validation-block")).toHaveCount(0);
      await expectPlannedStock(page);
      expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
      await panel.screenshot({ path: testInfo.outputPath(`material-planning-updated-${viewport.name}.png`), animations: "disabled" });
      expect(apiMutations).toEqual([]);
      expect(pageErrors).toEqual([]);
    });
  });
}

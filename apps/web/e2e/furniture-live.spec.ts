import { readFile } from "node:fs/promises";
import { expect, test } from "@playwright/test";
import { newFurnitureWorkspace, type FurnitureFamily } from "../lib/furniture-workspace";
import { provisionLiveProject, selectProjectBeforeNavigation } from "./live-helpers";

test.describe("möbelfamiljer med verklig API, databas, kö och CAD-worker", () => {
  test.skip(process.env.PLAYWRIGHT_REAL_API !== "1", "Requires the complete Compose stack.");
  for (const family of ["table", "chest_of_drawers", "shelving"] as FurnitureFamily[]) {
    test(`${family}: spara, byta profil, öppna igen och hämta CAD`, async ({ page, request }, info) => {
      test.setTimeout(240_000);
      const provisioned = await provisionLiveProject(request, info, `furniture-${family}`);
      const base = process.env.PLAYWRIGHT_API_URL!.replace(/\/$/, "");
      const headers = { Authorization: `Bearer ${process.env.PLAYWRIGHT_DEMO_TOKEN || "demo-nordic-owner"}` };
      const path = `${base}/v1/furniture/projects/${provisioned.project.id}`;
      const saved = await request.put(`${path}/draft`, { headers,
        data: { expected_revision: 0, workspace: newFurnitureWorkspace(family) } });
      expect(saved.status(), await saved.text()).toBe(200);
      await selectProjectBeforeNavigation(page, provisioned);
      await page.goto("/furniture");
      const projectSelect = page.getByRole("combobox", { name: "Öppna möbelprojekt" });
      await expect(projectSelect.locator(`option[value="${provisioned.project.id}"]`)).toHaveCount(1);
      await projectSelect.selectOption(provisioned.project.id);
      await expect(page.getByLabel("Möbeltyp", { exact: true })).toHaveValue(family);
      await expect(page.getByRole("button", { name: "Skapa granskningspaket" })).toBeEnabled();

      await page.getByLabel("Uppmätt skivtjocklek (mm)", { exact: true }).fill("17.801");
      await page.getByRole("button", { name: "Kontrollera profilbyte" }).click();
      await expect(page.getByText("Konsekvenser av profilbytet")).toBeVisible();
      await page.getByRole("button", { name: "Använd profilbytet i designen" }).click();
      await expect(page.getByRole("button", { name: "Skapa granskningspaket" })).toBeDisabled();
      await expect(page.getByRole("button", { name: "Spara revision" })).toBeEnabled();
      await page.getByRole("button", { name: "Spara revision" }).click();
      await expect(page.getByText("Revision 2 är sparad.")).toBeVisible();
      await page.reload();
      await expect(projectSelect.locator(`option[value="${provisioned.project.id}"]`)).toHaveCount(1);
      await projectSelect.selectOption(provisioned.project.id);
      await expect(page.getByLabel("Uppmätt skivtjocklek (mm)", { exact: true })).toHaveValue("17.801");
      await expect(page.getByRole("button", { name: /^Revision 1 ·/ })).toBeVisible();
      if (family === "chest_of_drawers") {
        await page.getByRole("button", { name: "Öppna lådorna" }).click();
        await expect(page.getByRole("button", { name: "Stäng lådorna" })).toHaveAttribute("aria-pressed", "true");
      }
      await page.getByRole("button", { name: "Skapa granskningspaket" }).click();
      const link = page.getByRole("link", { name: /Hämta STEP, GLB, DXF/ });
      await expect(link).toBeVisible({ timeout: 200_000 });
      const downloadEvent = page.waitForEvent("download");
      await link.click();
      const download = await downloadEvent;
      expect(await download.failure()).toBeNull();
      expect(download.suggestedFilename()).toBe("custombuild-design-review-2.zip");
      const bytes = await readFile((await download.path())!);
      expect(bytes.subarray(0, 4)).toEqual(Buffer.from([0x50, 0x4b, 0x03, 0x04]));
      expect(bytes.length).toBeGreaterThan(1_000);
      await info.attach(`${family}-studio`, { body: await page.screenshot({ fullPage: true }), contentType: "image/png" });
      await page.setViewportSize({ width: 390, height: 844 });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      await info.attach(`${family}-mobile`, { body: await page.screenshot({ fullPage: true }), contentType: "image/png" });
    });
  }
});

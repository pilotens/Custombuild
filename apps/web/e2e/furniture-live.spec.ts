import { readFile } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type TestInfo } from "@playwright/test";
import { newFurnitureWorkspace, type FurnitureFamily } from "../lib/furniture-workspace";
import { provisionLiveProject, selectProjectBeforeNavigation } from "./live-helpers";

async function attachView(page: Page, info: TestInfo, name: string) {
  const path = info.outputPath(`${name}.png`);
  await page.screenshot({ path, fullPage: true });
  await info.attach(name, { path, contentType: "image/png" });
}

test.describe("möbelfamiljer med verklig API, databas, kö och CAD-worker", () => {
  test.skip(process.env.PLAYWRIGHT_REAL_API !== "1", "Requires the complete Compose stack.");
  test.afterEach(async ({ page }, info) => {
    if (info.status !== info.expectedStatus && await page.locator("main").count()) {
      console.log("Furniture workspace at failure:", await page.locator("main").innerText());
    }
  });
  test("kundmått 4340 × 2540 × 280: arbetsfil, återöppning och spårbart CAD-underlag", async ({ page, request }, info) => {
    test.setTimeout(300_000);
    const provisioned = await provisionLiveProject(request, info, "customer-bookcase");
    await selectProjectBeforeNavigation(page, provisioned);
    await page.goto("/furniture");
    await expect(page.getByRole("button", { name: "Spara revision" })).toBeEnabled();
    await page.getByLabel("Läs arbetsfil (JSON)").setInputFiles(fileURLToPath(new URL(
      "../../../examples/furniture/bookcase-4340x2540x280.json", import.meta.url)));
    await expect(page.getByText(/Arbetsfilen har kontrollerats/)).toBeVisible();
    await expect(page.getByLabel("Bredd (mm)", { exact: true })).toHaveValue("4340");
    await expect(page.getByLabel("Höjd (mm)", { exact: true })).toHaveValue("2540");
    await expect(page.getByLabel("Djup (mm)", { exact: true })).toHaveValue("280");
    const created = page.waitForResponse(r => r.request().method() === "POST" && new URL(r.url()).pathname === "/v1/projects");
    await page.getByRole("button", { name: "Spara revision" }).click();
    const response = await created;
    expect(response.status()).toBe(201);
    const project = await response.json();
    await expect(page.getByText("Revision 1 är sparad.")).toBeVisible();
    await page.reload();
    const projects = page.getByRole("combobox", { name: "Öppna möbelprojekt" });
    await expect(projects.locator(`option[value="${project.id}"]`)).toHaveCount(1);
    await projects.selectOption(project.id);
    await page.getByText("Kundens yttermått och listutrymme", { exact: true }).click();
    await expect(page.getByLabel("Kundlängd inklusive reserverat utrymme (mm)")).toHaveValue("4340");
    await expect(page.getByLabel("Längden inkluderar list", { exact: true })).toBeChecked();
    await expect(page.getByLabel("Listhöjd (mm)", { exact: true })).toHaveValue("90");
    await expect(page.getByLabel("Listbredd/utstick (mm)", { exact: true })).toHaveValue("20");
    await expect(page.getByLabel("Listens funktion", { exact: true })).toHaveValue("unassigned");
    await expect(page.getByLabel("Vänster · reserverat (mm)")).toBeEmpty();
    await expect(page.getByRole("button", { name: "Förbered tillverkning" })).toBeDisabled();
    await page.getByText("Råformat att stämma av med verkstaden", { exact: true }).click();
    await expect(page.getByRole("table")).toHaveCount(2);
    await page.getByRole("button", { name: "Skapa granskningspaket" }).click();
    const link = page.getByRole("link", { name: /Hämta STEP, GLB, DXF/ });
    await expect(link).toBeVisible({ timeout: 200_000 });
    const downloadEvent = page.waitForEvent("download");
    await link.click();
    const download = await downloadEvent;
    expect(await download.failure()).toBeNull();
    const exported = JSON.parse(execFileSync("python3", ["-c", [
      "import csv,hashlib,io,json,sys,zipfile",
      "with zipfile.ZipFile(sys.argv[1]) as z:",
      " manifest=json.loads(z.read('manifest.json'))",
      " for entry in manifest['files']:",
      "  assert hashlib.sha256(z.read(entry['path'])).hexdigest()==entry['sha256']",
      " assert z.read('design/model.step').startswith(b'ISO-10303-21;')",
      " assert manifest['physical_cutting_authorized'] is False",
      " rows=list(csv.DictReader(io.StringIO(z.read('inspection/first-article-checks.csv').decode('utf-8-sig'))))",
      " assert rows and all(not r['measured'] and not r['result'] for r in rows)",
      " print(z.read('manufacturing/workshop-handoff.json').decode())",
    ].join("\n"), (await download.path())!], { encoding: "utf8" }));
    expect(exported.dimensions.installation.width_um).toBe(4_340_000);
    expect(exported.dimensions.installation.height_um).toBe(2_540_000);
    expect(exported.dimensions.installation.depth_um).toBe(280_000);
    expect(exported.dimensions.installation.trim_profile).toEqual({
      height_um: 90_000, width_um: 20_000, use: "unassigned", walls: [],
    });
    expect(exported.dimensions.state).toBe("requires_resolution");
    await attachView(page, info, "customer-bookcase-dimensions");
    await page.setViewportSize({ width: 390, height: 844 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await attachView(page, info, "customer-bookcase-mobile");
  });
  for (const family of ["table", "chest_of_drawers", "shelving"] as FurnitureFamily[]) {
    test(`${family}: spara, byta profil, öppna igen och hämta CAD`, async ({ page, request }, info) => {
      test.setTimeout(family === "shelving" ? 480_000 : 240_000);
      const provisioned = await provisionLiveProject(request, info, `furniture-${family}`);
      const base = process.env.PLAYWRIGHT_API_URL!.replace(/\/$/, "");
      const headers = { Authorization: `Bearer ${process.env.PLAYWRIGHT_DEMO_TOKEN || "demo-nordic-owner"}` };
      const path = `${base}/v1/furniture/projects/${provisioned.project.id}`;
      const initial = newFurnitureWorkspace(family);
      if (family === "shelving") {
        initial.design.intent = { ...initial.design.intent, width_um: 700_001,
          height_um: 1_000_003, shelf_count: 2, divider_count: 0 };
        initial.manufacturing = { machine_profile_id: "custombuild-router-1325-linuxcnc",
          machine_profile_version: "1.0.0-validation", stock_width_um: 2_440_000,
          stock_height_um: 1_220_000, stock_grain_axis: "x" };
      }
      const saved = await request.put(`${path}/draft`, { headers,
        data: { expected_revision: 0, workspace: initial } });
      expect(saved.status(), await saved.text()).toBe(200);
      const apiFailures: string[] = [];
      page.on("response", response => {
        if (new URL(response.url()).pathname.startsWith("/v1/") && response.status() >= 400) {
          apiFailures.push(`${response.request().method()} ${new URL(response.url()).pathname}: ${response.status()}`);
        }
      });
      page.on("requestfailed", request => {
        if (new URL(request.url()).pathname.startsWith("/v1/")
          && !request.failure()?.errorText.includes("ERR_ABORTED")) {
          apiFailures.push(`${request.method()} ${new URL(request.url()).pathname}: ${request.failure()?.errorText}`);
        }
      });
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
      await expect(page.locator("canvas")).toHaveAttribute("data-custombuild-render-commit", /^[1-9]\d*$/, { timeout: 30_000 });
      await attachView(page, info, `${family}-assembled`);
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
      await attachView(page, info, `${family}-studio`);
      await page.setViewportSize({ width: 390, height: 844 });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      await attachView(page, info, `${family}-mobile`);
      if (family === "shelving") {
        await page.setViewportSize({ width: 1440, height: 1000 });
        await page.getByRole("button", { name: "Förbered tillverkning" }).click();
        await expect(page.getByRole("heading", { name: "Förbered tillverkningen" })).toBeVisible();
        const savedVersion = page.waitForResponse(r => r.request().method() === "POST"
          && new URL(r.url()).pathname === `/v1/projects/${provisioned.project.id}/versions`);
        await page.getByRole("button", { name: "Spara och kontrollera" }).click();
        const versionResponse = await savedVersion;
        expect(versionResponse.status(), await versionResponse.text()).toBe(201);
        const version = await versionResponse.json();
        expect(version.spec_json.parameters.actual_thickness_um).toBe(17_801);
        expect(version.spec_json.parameters.width_um).toBe(700_001);
        expect(version.result_json.source_furniture.workspace.design.revision).toBe(2);
        await page.getByRole("checkbox", { name: "Jag har läst och kontrollerat varningarna ovan." }).check();
        await page.getByRole("button", { name: "Godkänn designkontroll", exact: true }).click();
        await expect(page.getByRole("button", { name: "Skapa underlag", exact: true })).toBeEnabled();
        await page.getByRole("button", { name: "Skapa underlag", exact: true }).click();
        const downloadButton = page.getByRole("button", { name: "Ladda ned granskningspaket (.zip)", exact: true });
        await expect(downloadButton).toBeVisible({ timeout: 200_000 });
        const fullDownloadEvent = page.waitForEvent("download");
        await downloadButton.click();
        const fullDownload = await fullDownloadEvent;
        expect(await fullDownload.failure()).toBeNull();
        const productionBytes = await readFile((await fullDownload.path())!);
        expect(productionBytes.length).toBeGreaterThan(1_000);
        const exportedSource = JSON.parse(execFileSync("python3", ["-c", [
          "import hashlib,json,sys,zipfile",
          "with zipfile.ZipFile(sys.argv[1]) as z:",
          " data=z.read('design/furniture-source.json')",
          " manifest=json.loads(z.read('manifest.json'))",
          " entry=next(e for e in manifest['artifacts'] if e['path']=='design/furniture-source.json')",
          " assert hashlib.sha256(data).hexdigest()==entry['sha256']",
          " assert manifest['physical_cutting_authorized'] is False",
          " print(data.decode())",
        ].join("\n"), (await fullDownload.path())!], { encoding: "utf8" }));
        expect(exportedSource).toEqual(version.result_json.source_furniture);
        await attachView(page, info, "shelving-production");
        await page.reload();
        await projectSelect.selectOption(provisioned.project.id);
        await page.getByRole("button", { name: "Förbered tillverkning" }).click();
        await expect(downloadButton).toBeVisible({ timeout: 30_000 });
        const restored = await request.get(`${base}/v1/projects/${provisioned.project.id}/production-state`, { headers });
        expect(restored.ok()).toBe(true);
        expect((await restored.json()).version.id).toBe(version.id);
        await page.setViewportSize({ width: 390, height: 844 });
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
        await attachView(page, info, "shelving-production-mobile");
      }
      expect(apiFailures).toEqual([]);
    });
  }
});

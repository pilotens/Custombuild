import { expect, test } from "@playwright/test";
import { newFurnitureWorkspace } from "../lib/furniture-workspace";
import { provisionLiveProject } from "./live-helpers";

test("separata råformat: granska, byt maskin, spara och öppna samma materialval", async ({ page, request }, info) => {
  test.skip(process.env.PLAYWRIGHT_REAL_API !== "1", "Requires a real furniture API.");
  test.setTimeout(150_000);
  const provisioned = await provisionLiveProject(request, info, "material-stock");
  const base = process.env.PLAYWRIGHT_API_URL!.replace(/\/$/, "");
  const headers = { Authorization: `Bearer ${process.env.PLAYWRIGHT_DEMO_TOKEN || "demo-nordic-owner"}` };
  const path = `${base}/v1/furniture/projects/${provisioned.project.id}`;
  const workspace = newFurnitureWorkspace("shelving");
  Object.assign(workspace.design.intent, { width_um: 600_000, height_um: 800_000, depth_um: 300_000 });
  workspace.manufacturing = { machine_profile_id: "custombuild-router-1325-linuxcnc",
    machine_profile_version: "1.0.0-validation", stock_width_um: 310_000, stock_height_um: 810_000,
    stock_grain_axis: "y", edge_margin_um: 5_000 };
  const initial = await request.put(`${path}/draft`, { headers, data: { expected_revision: 0, workspace } });
  expect(initial.status(), await initial.text()).toBe(200);
  await page.goto("/furniture");
  await page.getByLabel("Öppna möbelprojekt").selectOption(provisioned.project.id);
  await expect(page.getByLabel("Bredd (mm)", { exact: true })).toHaveValue("600");
  const back = page.getByRole("group", { name: "birch-plywood-6 · 6 mm", exact: true });
  await back.getByRole("checkbox").check();
  await back.getByLabel("Råskivans bredd (mm)").fill("786");
  await back.getByLabel("Råskivans höjd (mm)").fill("294.95");
  await back.getByLabel("Fiberriktning på råskivan").selectOption("x");
  await page.getByLabel("Tillverkningsprofil").selectOption("custombuild-router-5125-linuxcnc");
  await expect(back.getByLabel("Råskivans bredd (mm)")).toHaveValue("786");
  await expect(page.getByRole("button", { name: "Spara revision", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "Kontrollera profilbyte" }).click();
  await expect(page.getByText("Konsekvenser av profilbytet")).toBeVisible();
  const before = await request.get(`${path}/draft`, { headers });
  expect(before.status(), await before.text()).toBe(200);
  expect((await before.json()).workspace.manufacturing.material_stocks).toBeUndefined();
  await page.getByRole("button", { name: "Använd profilbytet i designen" }).click();
  await page.getByRole("button", { name: "Spara revision", exact: true }).click();
  await expect(page.getByText("Revision 2 är sparad.")).toBeVisible();
  const savedResponse = await request.get(`${path}/draft`, { headers });
  expect(savedResponse.status(), await savedResponse.text()).toBe(200);
  const saved = await savedResponse.json();
  expect(saved.workspace.design.intent).toEqual((await initial.json()).workspace.design.intent);
  expect(saved.workspace.manufacturing.material_stocks).toEqual([{ material_id: "birch-plywood-6",
    material_version: "screening-2026.1", measured_thickness_um: 6_000, stock_width_um: 786_000,
    stock_height_um: 294_950, stock_grain_axis: "x", edge_margin_um: 5_000 }]);
  expect(saved.preview.manufacturing.geometry_compatible).toBe(true);
  await page.reload();
  await page.getByLabel("Öppna möbelprojekt").selectOption(provisioned.project.id);
  await expect(back.getByRole("checkbox")).toBeChecked();
  await expect(back.getByLabel("Råskivans höjd (mm)")).toHaveValue("294.95");
  for (const [name, width, height] of [["desktop", 1440, 1000], ["mobile", 390, 844]] as const) {
    await page.setViewportSize({ width, height });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    const screenshot = info.outputPath(`material-stock-${name}.png`);
    await back.screenshot({ path: screenshot });
    await info.attach(`material-stock-${name}`, { path: screenshot, contentType: "image/png" });
  }
});

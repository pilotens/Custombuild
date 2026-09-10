import { readFileSync } from "node:fs";
import { expect, test } from "@playwright/test";
import type { FurnitureWorkspace } from "../lib/furniture-workspace";
import { provisionLiveProject } from "./live-helpers";

test("fackförslag: granska, tillämpa, spara och behåll revision vid oförändrad sparning", async ({ page, request }, info) => {
  test.skip(process.env.PLAYWRIGHT_REAL_API !== "1", "Requires a real furniture API.");
  const project = await provisionLiveProject(request, info, "shelf-suggestion");
  const base = process.env.PLAYWRIGHT_API_URL!.replace(/\/$/, "");
  const headers = { Authorization: `Bearer ${process.env.PLAYWRIGHT_DEMO_TOKEN || "demo-nordic-owner"}` };
  const path = `${base}/v1/furniture/projects/${project.project.id}`;
  const workspace = JSON.parse(readFileSync(new URL("../../../examples/furniture/bookcase-4340x2540x280.json", import.meta.url), "utf8")) as FurnitureWorkspace;
  workspace.design.intent.shelf_load_basis = "per_metre";
  workspace.design.intent.shelf_load_per_metre_n = 300;
  const saved = await request.put(`${path}/draft`, { headers, data: { expected_revision: 0, workspace } });
  expect(saved.status(), await saved.text()).toBe(200);
  await page.goto("/furniture");
  await page.getByRole("combobox", { name: "Öppna möbelprojekt" }).selectOption(project.project.id);
  await expect(page.getByLabel("Bredd (mm)", { exact: true })).toHaveValue("4340");
  const panel = page.getByRole("region", { name: "Förslag på hyllfack" });
  await panel.getByRole("button", { name: "Föreslå fackindelning" }).click();
  await expect(panel.getByRole("table")).toBeVisible();
  await expect(page.getByLabel("Avdelare", { exact: true })).toHaveValue("1");
  expect((await request.get(`${path}/draft`, { headers })).status()).toBe(200);
  expect((await (await request.get(`${path}/history`, { headers })).json()).items).toHaveLength(1);
  await panel.screenshot({ path: info.outputPath("shelf-proposal-desktop.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await panel.screenshot({ path: info.outputPath("shelf-proposal-mobile.png") });
  await panel.getByRole("button", { name: "Använd föreslagen fackindelning" }).click();
  await expect(page.getByText(/Fackindelningen är ändrad/)).toBeVisible();
  expect(Number(await page.getByLabel("Avdelare", { exact: true }).inputValue())).toBeGreaterThan(1);
  await expect(page.getByLabel("Bredd (mm)", { exact: true })).toHaveValue("4340");
  await expect(page.getByLabel("Höjd (mm)", { exact: true })).toHaveValue("2540");
  await expect(page.getByLabel("Djup (mm)", { exact: true })).toHaveValue("280");
  await expect(page.getByRole("button", { name: "Förbered tillverkning" })).toBeDisabled();
  await page.getByRole("button", { name: "Spara revision" }).click();
  await expect(page.getByText("Revision 2 är sparad.")).toBeVisible();
  // The browser uses the public API host; the test client uses the runner host.
  const repeat = page.waitForResponse(response =>
    response.request().method() === "PUT" &&
    new URL(response.url()).pathname === `/v1/furniture/projects/${project.project.id}/draft`,
  );
  await page.getByRole("button", { name: "Spara revision" }).click();
  const repeatedSave = await repeat;
  expect(repeatedSave.status()).toBe(200);
  expect((await repeatedSave.json()).revision).toBe(2);
  expect((await (await request.get(`${path}/history`, { headers })).json()).items).toHaveLength(2);
  for (const name of ["shelf-proposal-desktop", "shelf-proposal-mobile"]) {
    await info.attach(name, { path: info.outputPath(`${name}.png`), contentType: "image/png" });
  }
});

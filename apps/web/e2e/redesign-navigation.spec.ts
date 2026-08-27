import { expect, test } from "@playwright/test";
import { chooseTemplateAndCreate, startWithEmptyPlanningStorage } from "./planning-helpers";

test.skip(
  process.env.PLAYWRIGHT_REAL_API === "1",
  "The deterministic navigation and persistence check runs against the offline production build.",
);
test.skip(
  ({ browserName }) => browserName !== "chromium",
  "The focused UI-state journey runs once in Chromium; shared navigation has cross-engine unit coverage.",
);

test("fyra produktlägen delar samma modell och återställs efter omladdning", async ({ page }) => {
  test.setTimeout(90_000);
  await startWithEmptyPlanningStorage(page);
  await page.goto("/", { waitUntil: "networkidle" });

  const skipLink = page.getByRole("link", { name: "Hoppa till huvudinnehåll" });
  await page.keyboard.press("Tab");
  await expect(skipLink).toBeFocused();
  await skipLink.press("Enter");
  await expect(page.locator("#workspace-mode-heading")).toBeFocused();

  const explore = page.locator("section.template-picker[data-presentation='embedded']");
  await expect(explore.getByRole("heading", { name: "Vad vill du skapa?" })).toBeVisible();
  for (const route of ["Välj en design", "Skapa med Custombuild", "Utgå från en bild", "Börja tomt"]) {
    await expect(explore.getByRole("button", { name: new RegExp(route) })).toHaveCount(1);
  }
  await expect(page.getByRole("dialog", { name: "Vad vill du skapa?" })).toHaveCount(0);
  await expect(page.locator(".side-nav nav, .mobile-nav")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /^(Start|Projekt|Mallar|Material)$/ })).toHaveCount(0);

  await chooseTemplateAndCreate(page, "Väggbibliotek", {
    widthMm: 2_400,
    heightMm: 2_400,
    depthMm: 340,
  });

  const modes = page.getByRole("navigation", { name: "Produktlägen" });
  await expect(modes).toBeVisible();
  for (const label of ["Utforska", "Studio", "Kontroll", "Underlag"]) {
    await expect(modes.getByRole("button", { name: new RegExp(label) })).toHaveCount(1);
  }
  await expect(page.getByRole("group", { name: "Arbetssätt" })).toHaveCount(0);

  const model = page.getByLabel("Interaktiv 3D-modell av möbeln");
  await expect(model).toBeVisible();
  const modelMarker = `playwright-model-${Date.now()}`;
  await model.evaluate((element, marker) => element.setAttribute("data-playwright-model-instance", marker), modelMarker);

  const viewGroup = page.getByRole("group", { name: "Vy" });
  await viewGroup.getByRole("button", { name: "Front", exact: true }).click();
  await expect(viewGroup.getByRole("button", { name: "Front", exact: true })).toHaveAttribute("aria-pressed", "true");

  for (const [mode, heading] of [
    ["Kontroll", "Kontrollera konstruktionen"],
    ["Underlag", "Designgranska och exportera underlag"],
  ] as const) {
    await modes.getByRole("button", { name: new RegExp(mode) }).click();
    await expect(modes.getByRole("button", { name: new RegExp(mode) })).toHaveAttribute("aria-current", "page");
    await expect(page.locator("#workspace-mode-heading")).toHaveText(heading);
    await expect(page.locator("#workspace-mode-heading")).toBeFocused();
    await expect(model).toBeVisible();
    await expect(model).toHaveAttribute("data-playwright-model-instance", modelMarker);
  }
  await expect(page.getByRole("dialog", { name: "Skapa underlag" })).toHaveCount(0);
  await expect(page.getByLabel("Underlagets innehåll")).toBeVisible();

  // Mode and camera state are local-only and independent of a server revision.
  await page.waitForTimeout(1_000);
  await page.reload({ waitUntil: "networkidle" });

  const restoredModes = page.getByRole("navigation", { name: "Produktlägen" });
  await expect(restoredModes.getByRole("button", { name: /Underlag/ })).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("group", { name: "Vy" }).getByRole("button", { name: "Front" }))
    .toHaveAttribute("aria-pressed", "true");
  await expect(page.getByLabel("Interaktiv 3D-modell av möbeln")).toBeVisible();
  await expect(page.getByLabel("Underlagets innehåll")).toBeVisible();
  await expect(page.locator("#workspace-mode-heading")).not.toBeFocused();

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(restoredModes).toBeVisible();
  await expect(page.locator(".mobile-nav")).toHaveCount(0);
  await restoredModes.getByRole("button", { name: /Studio/ }).click();
  await expect(restoredModes.getByRole("button", { name: /Studio/ })).toHaveAttribute("aria-current", "page");
  await expect(page.locator("#workspace-mode-heading")).toHaveText("Forma din möbel i Studio");
  await expect(page.locator("#workspace-mode-heading")).toBeFocused();
});

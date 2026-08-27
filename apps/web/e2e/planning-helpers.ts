import { expect, type Locator, type Page } from "@playwright/test";

interface PlanningDimensions {
  widthMm: number;
  heightMm: number;
  depthMm: number;
}

function escapeRegularExpression(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export async function startWithEmptyPlanningStorage(page: Page) {
  await page.addInitScript(() => {
    const marker = "custombuild:e2e:storage-initialized";
    if (window.sessionStorage.getItem(marker) === "1") return;
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.sessionStorage.setItem(marker, "1");
  });
}

/** Opens the persistent Explore surface. Kept under its former name for shared E2E callers. */
export async function openPlanning(page: Page): Promise<Locator> {
  const heading = page.getByRole("heading", { name: "Vad vill du skapa?" });
  const modes = page.getByRole("navigation", { name: "Produktlägen" });
  const launch = page.getByRole("button", { name: "Utforska design" });

  // Server hydration can resolve to any of these three stable entry surfaces.
  // Wait for the state that actually materializes instead of committing to a
  // locator based on an instantaneous visibility check during hydration.
  const entry = await Promise.race([
    heading.waitFor({ state: "visible", timeout: 30_000 }).then(() => "explore" as const),
    modes.waitFor({ state: "visible", timeout: 30_000 }).then(() => "workspace" as const),
    launch.waitFor({ state: "visible", timeout: 30_000 }).then(() => "landing" as const),
  ]);
  if (entry === "workspace") {
    await modes.getByRole("button", { name: /Utforska/ }).click();
  } else if (entry === "landing") {
    await launch.click();
  }

  await expect(heading).toBeVisible({ timeout: 30_000 });
  const explore = page.locator("section.template-picker[data-presentation='embedded']");
  await expect(explore).toBeVisible({ timeout: 30_000 });
  return explore;
}

/**
 * Stores a structured brief and returns to Explore's four-route start surface.
 * There is deliberately no wizard-step navigation in this helper.
 */
export async function goToPlanningStart(page: Page, dimensions?: PlanningDimensions): Promise<Locator> {
  const explore = await openPlanning(page);
  await explore.getByRole("button", { name: /Skapa med Custombuild/ }).click();
  await expect(explore.getByRole("heading", { name: "Vad ska möbeln göra för dig?" })).toBeVisible();
  if (dimensions) {
    await explore.getByRole("spinbutton", { name: "Planerad bredd" }).fill(String(dimensions.widthMm));
    await explore.getByRole("spinbutton", { name: "Planerad höjd" }).fill(String(dimensions.heightMm));
    await explore.getByRole("spinbutton", { name: "Planerad djup" }).fill(String(dimensions.depthMm));
  }
  await explore.getByRole("button", { name: "Visa tre startförslag" }).click();
  await expect(explore.getByText("Matchade startpunkter", { exact: true })).toBeVisible();
  await explore.getByRole("button", { name: "Till start" }).click();
  await expect(explore.getByRole("heading", { name: "Vad vill du skapa?" })).toBeVisible();
  return explore;
}

export async function chooseTemplateAndCreate(
  page: Page,
  templateName: string,
  dimensions?: PlanningDimensions,
) {
  const explore = await goToPlanningStart(page, dimensions);
  await explore.getByRole("button", { name: /Välj en design/ }).click();
  await expect(explore.getByRole("heading", { name: "Välj en startmodell att forma vidare." }))
    .toBeVisible({ timeout: 30_000 });

  const card = explore.locator("button.template-card", {
    hasText: new RegExp(escapeRegularExpression(templateName)),
  });
  await expect(card).toHaveCount(1, { timeout: 30_000 });
  await card.click();
  await explore.getByRole("button", {
    name: new RegExp(`Öppna ${escapeRegularExpression(templateName)} i Studio`),
  }).click();

  const modes = page.getByRole("navigation", { name: "Produktlägen" });
  await expect(modes.getByRole("button", { name: /Studio/ })).toHaveAttribute("aria-current", "page");
  await expect(page.getByLabel("Interaktiv 3D-modell av möbeln")).toBeVisible();
}

export async function openReferencePlanning(page: Page, dimensions?: PlanningDimensions) {
  const explore = dimensions
    ? await goToPlanningStart(page, dimensions)
    : await openPlanning(page);
  await explore.getByRole("button", { name: /Utgå från en bild/ }).click();
  const importer = page.getByRole("dialog", { name: "Skapa från referensbild" });
  await expect(importer).toBeVisible();
  return importer;
}

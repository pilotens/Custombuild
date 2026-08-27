import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { chooseTemplateAndCreate, startWithEmptyPlanningStorage } from "./planning-helpers";

test.skip(
  process.env.PLAYWRIGHT_REAL_API === "1",
  "The deterministic contextual-selection journey runs against the offline production build.",
);
test.skip(
  ({ browserName }) => browserName !== "chromium",
  "The focused keyboard journey runs once in Chromium; the controls use native button semantics.",
);

test("vald fysisk del behåller modellkontext och har en tydlig väg tillbaka", async ({ page }) => {
  test.setTimeout(90_000);
  await startWithEmptyPlanningStorage(page);
  await page.goto("/", { waitUntil: "networkidle" });
  await chooseTemplateAndCreate(page, "Väggbibliotek", {
    widthMm: 2_400,
    heightMm: 2_400,
    depthMm: 340,
  });

  const modes = page.getByRole("navigation", { name: "Produktlägen" });
  const context = page.getByRole("group", { name: "Redigeringskontext" });
  const furnitureContext = context.getByRole("button", { name: /Möbel Hela konstruktionen/ });
  const partContext = context.getByRole("button", { name: /Vald del/ });
  const partPicker = page.getByRole("combobox", { name: "Välj möbeldel att inspektera" });
  const isolate = page.getByRole("button", { name: /Isolera/ });
  const front = page.getByRole("group", { name: "Vy" }).getByRole("button", { name: "Front", exact: true });

  await expect(furnitureContext).toHaveAttribute("aria-pressed", "true");
  await expect(partContext).toBeDisabled();

  await partPicker.selectOption("side-left");
  await expect(partContext).toBeEnabled();
  await expect(partContext).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("region", { name: "Redigera fysisk del Vänster gavel" })).toBeVisible();
  await expect(page.getByText("Deltyp: Sidostycke")).toBeVisible();
  const accessibility = await new AxeBuilder({ page })
    .include(".right-rail")
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22a", "wcag22aa"])
    .analyze();
  const accessibilitySummary = accessibility.violations.map((violation) => ({
    id: violation.id,
    nodes: violation.nodes.map((node) => ({
      target: node.target,
      failureSummary: node.failureSummary,
    })),
  }));
  expect(accessibilitySummary, JSON.stringify(accessibilitySummary, null, 2)).toEqual([]);

  await isolate.click();
  await front.click();
  await expect(isolate).toHaveAttribute("aria-pressed", "true");

  await furnitureContext.focus();
  await furnitureContext.press("Enter");
  await expect(furnitureContext).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("heading", { name: "Möbel", exact: true })).toBeVisible();
  await expect(partPicker).toHaveValue("side-left");
  await expect(isolate).toHaveAttribute("aria-pressed", "true");

  await partContext.focus();
  await partContext.press("Space");
  await expect(page.getByRole("region", { name: "Redigera fysisk del Vänster gavel" })).toBeVisible();

  await modes.getByRole("button", { name: /Kontroll/ }).click();
  await expect(partPicker).toHaveValue("side-left");
  await expect(front).toHaveAttribute("aria-pressed", "true");
  await expect(isolate).toHaveAttribute("aria-pressed", "true");
  await modes.getByRole("button", { name: /Studio/ }).click();
  await expect(partContext).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("region", { name: "Redigera fysisk del Vänster gavel" })).toBeVisible();

  await modes.getByRole("button", { name: /Underlag/ }).click();
  await expect(page.getByLabel("Underlagets innehåll")).toBeVisible();
  await expect(partPicker).toHaveValue("side-left");
  await modes.getByRole("button", { name: /Studio/ }).click();

  await context.getByRole("button", { name: "Avmarkera Vänster gavel" }).click();
  await expect(partPicker).toHaveValue("");
  await expect(partContext).toBeDisabled();
  await expect(furnitureContext).toHaveAttribute("aria-pressed", "true");
  await expect(isolate).toBeDisabled();
});

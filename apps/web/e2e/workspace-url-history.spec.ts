import { expect, test } from "@playwright/test";
import { chooseTemplateAndCreate, startWithEmptyPlanningStorage } from "./planning-helpers";

test.skip(
  process.env.PLAYWRIGHT_REAL_API === "1",
  "Deterministic URL/history coverage runs against the anonymous offline workspace.",
);

function productModes(page: import("@playwright/test").Page) {
  return page.getByRole("navigation", { name: "Produktlägen" });
}

test("a canonical mode deep link survives reload without putting design state in the URL", async ({ page }) => {
  await startWithEmptyPlanningStorage(page);
  await page.goto("/?return=kept&mode=studio#workspace", { waitUntil: "networkidle" });

  await expect(productModes(page).getByRole("button", { name: /Studio/ }))
    .toHaveAttribute("aria-current", "page");
  await expect(page).toHaveURL(/\?return=kept&mode=studio#workspace$/);
  expect(new URL(page.url()).searchParams.has("project")).toBe(false);

  await page.reload({ waitUntil: "networkidle" });
  await expect(productModes(page).getByRole("button", { name: /Studio/ }))
    .toHaveAttribute("aria-current", "page");
  expect([...new URL(page.url()).searchParams.keys()]).toEqual(["return", "mode"]);
});

test("back and forward restore modes through popstate without stealing focus", async ({ page }) => {
  await startWithEmptyPlanningStorage(page);
  await page.goto("/?mode=explore", { waitUntil: "networkidle" });
  const modes = productModes(page);

  await modes.getByRole("button", { name: /Studio/ }).click();
  await expect(page).toHaveURL(/\?mode=studio$/);
  await modes.getByRole("button", { name: /Kontroll/ }).click();
  await expect(page).toHaveURL(/\?mode=check$/);

  const focusAnchor = page.getByRole("button", { name: /Spara utkast/ });
  await focusAnchor.focus();
  await expect(focusAnchor).toBeFocused();

  await page.goBack();
  await expect(page).toHaveURL(/\?mode=studio$/);
  await expect(modes.getByRole("button", { name: /Studio/ }))
    .toHaveAttribute("aria-current", "page");
  await expect(focusAnchor).toBeFocused();

  await page.goForward();
  await expect(page).toHaveURL(/\?mode=check$/);
  await expect(modes.getByRole("button", { name: /Kontroll/ }))
    .toHaveAttribute("aria-current", "page");
  await expect(focusAnchor).toBeFocused();
});

test("malformed mode and unavailable project fall back to local v3 mode and are sanitized", async ({ page }) => {
  await startWithEmptyPlanningStorage(page);
  await page.goto("/?mode=explore", { waitUntil: "networkidle" });
  await chooseTemplateAndCreate(page, "Hyllsystem");
  await productModes(page).getByRole("button", { name: /Underlag/ }).click();
  await expect(productModes(page).getByRole("button", { name: /Underlag/ }))
    .toHaveAttribute("aria-current", "page");
  await page.waitForTimeout(600);

  await page.goto("/?project=not-authorized&mode=verification&return=kept#workspace", {
    waitUntil: "networkidle",
  });

  await expect(productModes(page).getByRole("button", { name: /Underlag/ }))
    .toHaveAttribute("aria-current", "page");
  await expect(page).toHaveURL(/\?return=kept&mode=build#workspace$/);
  const canonical = new URL(page.url());
  expect(canonical.searchParams.has("project")).toBe(false);
  expect(canonical.searchParams.has("revision")).toBe(false);
  expect(canonical.searchParams.has("selectedPartId")).toBe(false);
});

import { expect, test } from "@playwright/test";

test.skip(
  process.env.PLAYWRIGHT_REAL_API === "1",
  "Offline workspace smoke is replaced by the live production workflow in Compose.",
);

test("den byggda svenska konstruktionsarbetsytan fungerar utan nätverksmockar", async ({
  page,
}) => {
  const pageErrors: string[] = [];
  const failedRequests: string[] = [];

  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("requestfailed", (request) => {
    failedRequests.push(`${request.method()} ${request.url()}: ${request.failure()?.errorText}`);
  });

  const response = await page.goto("/", { waitUntil: "networkidle" });
  expect(response?.status()).toBe(200);
  await expect(page.locator("html")).toHaveAttribute("lang", "sv");
  await expect(page).toHaveTitle("Custombuild · Konstruktionsarbetsyta");
  await expect(page.getByRole("heading", { name: "Arkitektväggen" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Parametrar" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Validering" })).toBeVisible();
  await expect(page.getByText("Lokalt läge", { exact: true })).toBeVisible();
  await expect(page.locator("canvas").first()).toBeVisible();

  await expect(page.getByRole("heading", { name: "Dra in i modellen" })).toBeVisible();
  await page.getByRole("button", { name: /Hyllrad/ }).click();
  await expect(page.getByText("En hyllrad lades till via hyllfack 1.")).toBeVisible();
  await expect(page.getByText(/shelf_count: 5 → 6/)).toBeVisible();

  const width = page.getByRole("spinbutton", { name: "Bredd mm", exact: true });
  await expect(width).toHaveValue("1200");
  await width.fill("1350");
  await width.blur();
  await expect(width).toHaveValue("1350");
  await expect(page.getByText("Sparad", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Front", exact: true }).click();
  await expect(page.getByRole("button", { name: "Front", exact: true })).toHaveAttribute(
    "aria-pressed",
    "true",
  );

  await page.getByRole("tab", { name: /^BOM/ }).click();
  await expect(page.getByRole("columnheader", { name: "Artikel" })).toBeVisible();
  await expect(page.getByText(/Hash/).first()).toBeVisible();

  expect(pageErrors).toEqual([]);
  expect(failedRequests).toEqual([]);
});

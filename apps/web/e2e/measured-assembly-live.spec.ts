import { expect, test } from "@playwright/test";

test("en ändrad tapp kan granskas, exporteras och återställas", async ({
  page,
}) => {
  test.skip(
    process.env.PLAYWRIGHT_REAL_API !== "1",
    "Requires the live assembly API",
  );
  await page.goto("/assembly");
  await page.getByRole("button", { name: "Prova ett ramhörn" }).click();
  const select = page.getByLabel("Välj del i ritningen");
  await select.selectOption("rail-1");
  const sheet = page.getByRole("article", { name: "Måttsatt delblad" });
  await expect(sheet.getByText("Djupreserv 1 mm.")).toBeVisible();
  await page.getByRole("button", { name: "Visa utan delen" }).click();
  await expect(page.getByRole("status")).toContainText(
    "1 registrerade förband",
  );
  await sheet.getByText("Pröva en ändring av en bearbetning").click();
  await sheet.getByLabel("U (mm)").fill("15,001");
  await sheet
    .getByRole("button", { name: "Kontrollera ändringen i monteringen" })
    .click();
  await expect(sheet.getByText("Avviker · fog-1")).toBeVisible();
  const receipt = page.waitForEvent("download");
  await sheet
    .getByRole("button", { name: "Hämta utskrivbart delblad med alla ytor" })
    .click();
  expect((await receipt).suggestedFilename()).toBe("rail-1-delblad.html");
  await page.getByRole("button", { name: "Prova ett ramhörn" }).click();
  await expect(sheet.getByText("Nominell passning · fog-1")).toBeVisible();
});

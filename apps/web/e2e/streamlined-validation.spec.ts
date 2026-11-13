import { expect, test, type Page } from "@playwright/test";
import { openPlanning, startWithEmptyPlanningStorage } from "./planning-helpers";

test.skip(
  process.env.PLAYWRIGHT_REAL_API === "1",
  "This regression uses an isolated offline design draft.",
);
test.skip(
  ({ browserName }) => browserName !== "chromium",
  "The compact validation regression runs once in Chromium.",
);

test.use({
  video: "off",
  actionTimeout: 10_000,
  navigationTimeout: 20_000,
  contextOptions: { reducedMotion: "reduce" },
});

async function openWallLibrary(page: Page): Promise<void> {
  await startWithEmptyPlanningStorage(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  const planner = await openPlanning(page);
  await planner.getByRole("button", { name: /Välj en design/ }).click();
  await planner.locator("button.template-card", { hasText: /Väggbibliotek/ }).click();
  await planner.getByRole("button", { name: "Öppna Väggbibliotek i Studio" }).click();
  await expect(page.getByRole("region", { name: "Hantera delar" })).toBeVisible();
  const dismiss = page.getByRole("button", { name: "Stäng ändringsöversikt" });
  if (await dismiss.isVisible()) await dismiss.click();
  await page.getByRole("navigation", { name: "Produktlägen" })
    .getByRole("button", { name: /Kontroll/ }).click();
}

for (const viewport of [
  { name: "desktop", width: 1_440, height: 960 },
  { name: "mobile", width: 390, height: 844 },
]) {
  test.describe(viewport.name, () => {
    test.use({ viewport: { width: viewport.width, height: viewport.height } });

    test("keeps checks compact without hiding their status or evidence", async ({ page }, testInfo) => {
      test.setTimeout(28_000);
      const pageErrors: string[] = [];
      page.on("pageerror", (error) => pageErrors.push(error.message));
      await openWallLibrary(page);
      const panel = page.getByRole("region", { name: "Kontrollera konstruktionen", exact: true });
      await expect(panel).toBeVisible();
      const pending = panel.locator("article.validation-card:not(.validation-pass)");
      expect(await pending.count()).toBeGreaterThan(0);

      const tip = panel.getByRole("article", { name: "Tipprisk och väggförankring", exact: true });
      const hardware = panel.getByRole("article", { name: "Beslag och borrbild för underskåp", exact: true, includeHidden: true });
      const joint = panel.getByRole("article", { name: "Lokalt upplag i hyllspår och hyllbärare", exact: true, includeHidden: true });
      const capabilities = panel.getByRole("region", { name: "Tillverkningsstödet är inte färdigt" });
      const capabilityDisclosure = capabilities.getByText("Visa 2 kvarstående krav", { exact: true });
      await expect(tip.getByTestId("validation-state")).toHaveText("Behöver beslut");
      await expect(hardware.getByTestId("validation-state")).toHaveText("Behöver beslut");
      await expect(joint.getByTestId("validation-state")).toHaveText("Behöver beslut");
      await expect(capabilities).toBeVisible();
      await expect(capabilityDisclosure.locator("..")).not.toHaveAttribute("open", "");
      await expect(hardware).not.toBeVisible();
      await expect(joint).not.toBeVisible();
      await expect(panel.getByRole("button", { name: /^Öppna underlag för/ })).toHaveCount(0);

      const technical = tip.locator("details.validation-technical");
      await expect(technical).not.toHaveAttribute("open", "");
      await expect(technical.getByText("Orsak", { exact: true })).not.toBeVisible();
      await expect(technical.locator(".validation-evidence")).not.toBeVisible();
      await expect(technical.locator(".validation-part-actions")).not.toBeVisible();
      await expect(technical.getByText(/Regel STAB-TIP-002/)).not.toBeVisible();
      await expect(panel.locator(".validation-part-actions button:visible")).toHaveCount(0);
      await expect(panel.locator(".calculation-trace:visible")).toHaveCount(0);

      const approvedSummary = panel.locator("summary")
        .filter({ hasText: /^Godkända kontroller \(\d+\)$/ });
      const approved = approvedSummary.locator("..");
      await expect(approvedSummary).toHaveCount(1);
      await expect(approved).not.toHaveAttribute("open", "");
      const approvedCards = approved.locator("article.validation-pass");
      expect(await approvedCards.count()).toBeGreaterThan(0);
      await expect(approvedCards.first()).not.toBeVisible();

      await page.screenshot({
        path: testInfo.outputPath(`streamlined-validation-${viewport.name}.png`),
        animations: "disabled",
        fullPage: true,
      });
      await panel.screenshot({
        path: testInfo.outputPath(`streamlined-validation-panel-${viewport.name}.png`),
        animations: "disabled",
      });

      await capabilityDisclosure.click();
      await expect(capabilityDisclosure.locator("..")).toHaveAttribute("open", "");
      await expect(hardware).toBeVisible();
      await expect(joint).toBeVisible();
      await expect(hardware.locator(".validation-evidence")).not.toBeVisible();
      await expect(joint.locator(".calculation-trace")).not.toBeVisible();
      const cardHeights = await pending.evaluateAll((cards) => cards.map((card) => ({
        title: card.getAttribute("aria-label"),
        height: card.getBoundingClientRect().height,
      })));
      for (const card of cardHeights) {
        expect(card.height, `${viewport.name}: ${card.title} should be a compact action card`).toBeLessThan(200);
      }
      expect(await page.evaluate(() => (
        document.documentElement.scrollWidth - document.documentElement.clientWidth
      ))).toBeLessThanOrEqual(0);
      const hardwareTechnical = hardware.locator("details.validation-technical");
      await hardwareTechnical.getByText("Visa detaljer", { exact: true }).click();
      await expect(hardwareTechnical.locator(".validation-evidence")).toBeVisible();
      await expect(hardwareTechnical.getByText(/Regel CB-HARDWARE-001/)).toBeVisible();
      await expect(hardwareTechnical.getByText("Ingen borrbild för gångjärn skickas till CAM i detta läge.", { exact: true })).toBeVisible();
      await hardwareTechnical.getByText("Visa detaljer", { exact: true }).click();
      const jointTechnical = joint.locator("details.validation-technical");
      await jointTechnical.getByText("Visa detaljer", { exact: true }).click();
      await expect(jointTechnical.locator(".calculation-trace")).toBeVisible();
      await expect(jointTechnical.getByText(/Regel CB-JOINT-001/)).toBeVisible();
      await expect(jointTechnical.getByText("Ett vanligt DADO-spår är inte självlåsande.", { exact: true })).toBeVisible();
      await jointTechnical.getByText("Visa detaljer", { exact: true }).click();
      await capabilityDisclosure.click();
      await expect(hardware).not.toBeVisible();
      await expect(joint).not.toBeVisible();

      await technical.getByText("Visa detaljer", { exact: true }).click();
      await expect(technical).toHaveAttribute("open", "");
      await expect(technical.getByText("Orsak", { exact: true })).toBeVisible();
      await expect(technical.getByText("Rekommenderad lösning", { exact: true })).toBeVisible();
      await expect(technical.locator(".validation-evidence")).toBeVisible();
      await expect(technical.getByText(/Regel STAB-TIP-002/)).toBeVisible();
      const focusPart = technical.getByRole("button", { name: "Fokusera delen side-left i modellen", exact: true });
      await expect(focusPart).toBeVisible();
      await focusPart.click();
      await expect(page.getByRole("combobox", { name: "Välj möbeldel att inspektera" }))
        .toHaveValue("side-left");
      await technical.getByText("Visa detaljer", { exact: true }).click();
      await expect(technical).not.toHaveAttribute("open", "");
      await approvedSummary.click();
      await expect(approved).toHaveAttribute("open", "");
      await expect(approvedCards.first()).toBeVisible();
      expect(pageErrors).toEqual([]);
    });
  });
}

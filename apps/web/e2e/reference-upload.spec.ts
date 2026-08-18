import { expect, test } from "@playwright/test";
import { openReferencePlanning } from "./planning-helpers";

test.skip(
  process.env.PLAYWRIGHT_REAL_API === "1",
  "Den serverbundna bildimporten verifieras i live-ui-acceptance.spec.ts.",
);

test("en referensbild kräver ett oföränderligt serverunderlag", async ({ page }) => {
  test.setTimeout(90_000);
  await page.goto("/", { waitUntil: "networkidle" });
  await openReferencePlanning(page);
  await expect(page.getByRole("heading", { name: "Skapa från referensbild" })).toBeVisible();

  const dataUrl = await page.evaluate(() => {
    const canvas = document.createElement("canvas");
    canvas.width = 360;
    canvas.height = 260;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("Canvas saknas");
    context.fillStyle = "#f6f6f2";
    context.fillRect(0, 0, 360, 260);
    context.fillStyle = "#c2a26f";
    context.fillRect(30, 20, 300, 215);
    context.fillStyle = "#6f361c";
    context.fillRect(30, 174, 300, 61);
    context.fillStyle = "#462d1e";
    for (const x of [30, 90, 150, 210, 270, 330]) context.fillRect(x - 2, 20, 4, 215);
    for (const y of [20, 50, 80, 110, 140, 174, 235]) context.fillRect(30, y - 2, 300, 4);
    return canvas.toDataURL("image/png");
  });
  await page.getByLabel("Välj referensbild").setInputFiles({
    name: "referensbibliotek.png",
    mimeType: "image/png",
    buffer: Buffer.from(dataUrl.split(",")[1]!, "base64"),
  });

  const alert = page.getByRole("dialog", { name: "Skapa från referensbild" }).getByRole("alert");
  await expect(alert).toContainText(
    "Referensbilden måste först sparas som ett oföränderligt projektunderlag på servern.",
  );
  await expect(alert).toContainText(
    "Logga in, öppna ett serverprojekt och försök ladda upp bilden igen.",
  );
  await expect(page.getByRole("heading", { name: "Kontrollera tolkningen" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Skapa konceptmodell" })).toHaveCount(0);
});

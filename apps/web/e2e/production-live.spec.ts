import { expect, test } from "@playwright/test";

test.skip(
  process.env.PLAYWRIGHT_REAL_API !== "1",
  "Requires the complete Compose API, worker, database, queue and object storage.",
);

test("det verkliga produktionsflödet kan frisläppa och hämta en låst revision", async ({
  page,
}, testInfo) => {
  test.setTimeout(6 * 60_000);
  const pageErrors: string[] = [];
  const failedRequests: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("requestfailed", (request) => {
    failedRequests.push(`${request.method()} ${request.url()}: ${request.failure()?.errorText}`);
  });

  const response = await page.goto("/", { waitUntil: "domcontentloaded" });
  expect(response?.status()).toBe(200);
  await expect(page.getByText("Servermodell", { exact: true })).toBeVisible({ timeout: 30_000 });

  const dimensions = [
    ["Bredd", "700"],
    ["Höjd", "1000"],
    ["Hyllor", "2"],
    ["Last / hylla", "10"],
  ] as const;
  for (const [label, value] of dimensions) {
    const input = page.getByLabel(label, { exact: true });
    await input.fill(value);
    await input.blur();
  }

  await expect(page.getByText("Servermodell", { exact: true })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId("status-badge").first()).toContainText("Godkänd", {
    timeout: 30_000,
  });
  await page.getByRole("tab", { name: "Frisläppning" }).click();

  const save = page.getByRole("button", { name: "Spara revision" });
  await expect(save).toBeEnabled({ timeout: 30_000 });
  await save.click();
  await expect(page.getByText(/Rev \d+ · Utkast/)).toBeVisible({ timeout: 30_000 });

  const validate = page.getByRole("button", { name: "Validera design" });
  await expect(validate).toBeEnabled();
  await validate.click();
  await expect(page.getByText(/Rev \d+ · Design validerad/)).toBeVisible({ timeout: 30_000 });

  await page
    .getByLabel("Designgranskarens motivering")
    .fill("Konstruktion, regelspår och antaganden granskade i liveacceptansen.");
  const approveDesign = page.getByRole("button", { name: /^Godkänn design/ });
  await expect(approveDesign).toBeEnabled();
  await approveDesign.click();

  const generate = page.getByRole("button", { name: "Generera paket" });
  await expect(generate).toBeEnabled({ timeout: 30_000 });
  await generate.click();

  await page
    .getByLabel("CAM-granskarens motivering")
    .fill("Setup, verktyg, valideringskod och backplot granskade för exakt jobb.");
  const approveCam = page.getByRole("button", { name: "Godkänn CAM för detta jobb" });
  await expect(approveCam).toBeEnabled({ timeout: 4 * 60_000 });
  await approveCam.click();

  const releaseBase = (process.env.PLAYWRIGHT_RELEASE_NUMBER ?? "UI-LIVE-1").toUpperCase();
  const releaseNumber = `${releaseBase.slice(0, 34)}-R${testInfo.retry}`;
  await page.getByLabel("Release-nummer").fill(releaseNumber);
  await page.getByLabel(/Jag bekräftar att revisionen ska låsas/).check();
  const release = page.getByRole("button", { name: "Frisläpp revision" });
  await expect(release).toBeEnabled({ timeout: 30_000 });
  await release.click();
  await expect(page.getByText(`${releaseNumber} frisläppt`)).toBeVisible({ timeout: 30_000 });

  const downloadPromise = page.waitForEvent("download", { timeout: 60_000 });
  await page.getByRole("button", { name: /Ladda ned ZIP/ }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/^custombuild-rev-\d+\.zip$/);
  expect(await download.failure()).toBeNull();
  const stream = await download.createReadStream();
  const firstChunk = await new Promise<Buffer>((resolve, reject) => {
    stream.once("data", (chunk: Buffer) => resolve(chunk));
    stream.once("error", reject);
  });
  expect(firstChunk.subarray(0, 2).toString("ascii")).toBe("PK");

  expect(pageErrors).toEqual([]);
  expect(failedRequests).toEqual([]);
});

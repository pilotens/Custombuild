import { readFileSync } from "node:fs";
import { writeFile } from "node:fs/promises";
import { expect, test, type Locator, type Page } from "@playwright/test";
import { chooseTemplateAndCreate, startWithEmptyPlanningStorage } from "./planning-helpers";

interface PerformanceBudget {
  schema_version: number;
  fixtures: {
    normal_5x5: {
      width_mm: number;
      height_mm: number;
      depth_mm: number;
      bay_count: number;
      shelf_count: number;
      base_cabinet_count: number;
    };
  };
  budgets: {
    chromium_interaction: {
      selection_p95_ms: number;
      mode_switch_p95_ms: number;
      numeric_edit_5mm_p95_ms: number;
    };
  };
  server_preview: {
    debounce_ms: number;
    network_latency_is_gated: boolean;
    reason: string;
  };
}

const budget = JSON.parse(
  readFileSync(new URL("../performance/performance-budget.json", import.meta.url), "utf8"),
) as PerformanceBudget;

test.skip(
  process.env.PLAYWRIGHT_REAL_API === "1",
  "The deterministic interaction baseline excludes network and backend scheduling.",
);
test.skip(
  ({ browserName }) => browserName !== "chromium",
  "The interaction baseline is intentionally recorded once in Chromium.",
);

interface InteractionDistribution {
  median_ms: number;
  p95_ms: number;
  samples_ms: number[];
}

function distribution(samples: number[]): InteractionDistribution {
  const sorted = [...samples].sort((left, right) => left - right);
  const valueAt = (fraction: number) => sorted[Math.max(0, Math.ceil(sorted.length * fraction) - 1)] ?? 0;
  return {
    median_ms: Number(valueAt(0.5).toFixed(2)),
    p95_ms: Number(valueAt(0.95).toFixed(2)),
    samples_ms: sorted.map((value) => Number(value.toFixed(2))),
  };
}

async function waitForTwoFrames(page: Page): Promise<void> {
  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));
}

async function measureAction(
  page: Page,
  action: () => Promise<void>,
  committed: () => Promise<void>,
): Promise<number> {
  await waitForTwoFrames(page);
  const started = await page.evaluate(() => performance.now());
  await action();
  await committed();
  await waitForTwoFrames(page);
  const finished = await page.evaluate(() => performance.now());
  return finished - started;
}

async function selectPart(partPicker: Locator, partId: string): Promise<void> {
  await partPicker.selectOption(partId);
}

test("records the warm interaction baseline without timing first WebGL compilation", async ({ page }, testInfo) => {
  test.setTimeout(120_000);
  await startWithEmptyPlanningStorage(page);
  await page.goto("/", { waitUntil: "networkidle" });
  await chooseTemplateAndCreate(page, "Väggbibliotek", {
    widthMm: budget.fixtures.normal_5x5.width_mm,
    heightMm: budget.fixtures.normal_5x5.height_mm,
    depthMm: budget.fixtures.normal_5x5.depth_mm,
  });

  const modes = page.getByRole("navigation", { name: "Produktlägen" });
  const studio = modes.getByRole("button", { name: /Studio/ });
  const check = modes.getByRole("button", { name: /Kontroll/ });
  const partPicker = page.getByRole("combobox", { name: "Välj möbeldel att inspektera" });
  const model = page.getByLabel("Interaktiv 3D-modell av möbeln");
  await expect(model).toBeVisible();
  const bayCount = page.locator("output[aria-label='Vertikala fack']");
  while (Number(await bayCount.textContent()) < budget.fixtures.normal_5x5.bay_count) {
    await page.getByRole("button", { name: "Öka vertikala fack" }).click();
  }
  while (Number(await bayCount.textContent()) > budget.fixtures.normal_5x5.bay_count) {
    await page.getByRole("button", { name: "Minska vertikala fack" }).click();
  }
  await expect(bayCount).toHaveText(String(budget.fixtures.normal_5x5.bay_count));
  await expect(page.locator("output[aria-label='Hyllnivåer']"))
    .toHaveText(String(budget.fixtures.normal_5x5.shelf_count));
  await expect(partPicker.locator("option")).toHaveCount(50);
  await waitForTwoFrames(page);

  // One complete warmup per path keeps shader compilation and first lazy render out of samples.
  await selectPart(partPicker, "side-left");
  await expect(page.getByRole("region", { name: /Redigera fysisk del/ })).toBeVisible();
  await selectPart(partPicker, "");
  await check.click();
  await expect(page.locator("#validation-heading")).toBeVisible();
  await studio.click();
  await expect(studio).toHaveAttribute("aria-current", "page");

  const selectionSamples: number[] = [];
  const modeSamples: number[] = [];
  const numericEditSamples: number[] = [];
  for (let index = 0; index < 7; index += 1) {
    selectionSamples.push(await measureAction(
      page,
      () => selectPart(partPicker, "side-left"),
      async () => { await expect(page.getByRole("region", { name: /Redigera fysisk del/ })).toBeVisible(); },
    ));
    await selectPart(partPicker, "");
    await expect(page.getByRole("region", { name: /Redigera fysisk del/ })).toHaveCount(0);

    modeSamples.push(await measureAction(
      page,
      () => check.click(),
      async () => {
        await expect(check).toHaveAttribute("aria-current", "page");
        await expect(page.locator("#validation-heading")).toBeVisible();
      },
    ));
    await studio.click();
    await expect(studio).toHaveAttribute("aria-current", "page");
  }

  await selectPart(partPicker, "divider-1");
  const partEditor = page.getByRole("region", { name: /Redigera fysisk del/ });
  await expect(partEditor).toBeVisible();
  const dividerPosition = partEditor.getByRole("spinbutton", { name: "Avdelarcentrum från vänster (X)" });
  const resetPart = partEditor.getByRole("button", { name: "Återställ del" });
  await dividerPosition.fill(String(Number(await dividerPosition.inputValue()) + 5));
  await expect(resetPart).toBeEnabled();

  for (let index = 0; index < 7; index += 1) {
    const beforePosition = await dividerPosition.inputValue();
    const requestedPosition = Number(beforePosition) + 5;
    numericEditSamples.push(await measureAction(
      page,
      () => dividerPosition.fill(String(requestedPosition)),
      async () => {
        await expect(dividerPosition).not.toHaveValue(beforePosition);
        await expect(resetPart).toBeEnabled();
      },
    ));
  }

  const measurements = {
    selection: distribution(selectionSamples),
    mode_switch: distribution(modeSamples),
    numeric_edit_5mm: distribution(numericEditSamples),
  };
  const evidence = {
    schema_version: budget.schema_version,
    clock: "window.performance.now",
    fixture: {
      ...budget.fixtures.normal_5x5,
      rendered_part_count: await partPicker.locator("option").count() - 1,
    },
    warmup: "completed before sampling; initial WebGL shader compilation excluded",
    sample_count: 7,
    measurements,
    server_preview: budget.server_preview,
  };
  const evidenceBody = `${JSON.stringify(evidence, null, 2)}\n`;
  await writeFile(testInfo.outputPath("performance-baseline.json"), evidenceBody, "utf8");
  await testInfo.attach("performance-baseline.json", { body: evidenceBody, contentType: "application/json" });

  const limits = budget.budgets.chromium_interaction;
  expect(measurements.selection.p95_ms).toBeLessThanOrEqual(limits.selection_p95_ms);
  expect(measurements.mode_switch.p95_ms).toBeLessThanOrEqual(limits.mode_switch_p95_ms);
  expect(measurements.numeric_edit_5mm.p95_ms).toBeLessThanOrEqual(limits.numeric_edit_5mm_p95_ms);
});

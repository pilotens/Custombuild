import { createHash } from "node:crypto";
import { expect, test, type Locator, type Page, type TestInfo } from "@playwright/test";
import { DEFAULT_DESIGN_SPEC, type DesignSpec } from "../lib/design-types";

interface StorageMutation {
  storage: "local" | "session";
  operation: "set" | "remove" | "clear";
  key: string | null;
}

interface ValidationGhostProbe {
  storageMutations: StorageMutation[];
  historyPushes: number;
  historyReplaces: number;
}

interface ViewerCheckpoint {
  canvasIdentity: string | null;
  modelRoot: string;
  renderCommit: number;
}

interface PageHealth {
  apiRequests: string[];
  failedRequests: string[];
  pageErrors: string[];
}

interface WebGlPreviewPixelEvidence {
  analysisPixelCount: number;
  canvasHeight: number;
  canvasWidth: number;
  dominantQuantizedColorRatio: number;
  lumaStandardDeviation: number;
  modelHeightRatio: number;
  modelPixelRatio: number;
  modelWidthRatio: number;
  proposedGhostChangedPixelCount: number;
  proposedGhostPixelCount: number;
  sourceGhostChangedPixelCount: number;
  sourceGhostPixelCount: number;
}

declare global {
  interface Window {
    __custombuildValidationGhostProbe?: ValidationGhostProbe;
  }
}

const DRAFT_KEY = "custombuild:workspace:v3:anonymous:project:local-draft:draft";
const CANVAS_IDENTITY = "validation-ghost-preview-canvas";
const CANVAS_IDENTITY_ATTRIBUTE = "data-validation-ghost-canvas";
const RENDER_COMMIT_ATTRIBUTE = "data-custombuild-render-commit";
const MODEL_ROOT_ATTRIBUTE = "data-custombuild-model-root";
const FIX_TRIGGER_NAME = "Åtgärda problem för Lodrät lastväg genom underskåpen: Rikta in 5 underskåpsmoduler";
const GHOST_PIXEL_TOLERANCE = 24;
// The bounded in-flow server-state row intentionally shortens the canvas. A
// fitted wall library therefore occupies about 67.4% of the cropped analysis
// region. The 75% cap preserves at least 25% non-warm analysis pixels, while
// the extent, luma, dominant-color and ghost checks reject blank, flat, or
// false-comparison renders.
const MAX_FITTED_MODEL_PIXEL_RATIO = 0.75;

const sourceSpec = {
  ...DEFAULT_DESIGN_SPEC,
  design_id: "e2e-validation-ghost-wall-library",
  furniture_type: "wall_library",
  width_mm: 4_200,
  height_mm: 2_600,
  depth_mm: 320,
  divider_count: 4,
  bay_sizing_mode: "count",
  bay_width_ratios: [],
  shelf_height_ratios: [],
  symmetry_locked: true,
  base_cabinet_height_mm: 720,
  base_cabinet_depth_mm: 320,
  base_cabinet_count: 4,
  reinforcement_mode: "manual",
  back_panel: true,
  plinth: true,
  stock_width_mm: 5_000,
  stock_height_mm: 2_500,
  stock_count: 24,
  back_stock_width_mm: 5_000,
  back_stock_height_mm: 2_500,
  back_stock_count: 4,
  machine_profile_id: "custombuild-router-5125-linuxcnc",
} satisfies DesignSpec;

const draftSnapshot = {
  version: 3,
  spec: sourceSpec,
  templateId: "wall-library",
  workspaceSelected: true,
  uiState: {
    schemaVersion: 2,
    mode: "check",
    viewMode: "front",
    exploded: false,
    transparent: false,
    isolateSelection: false,
    selectedPartId: "side-left",
    panels: {
      componentLibraryOpen: true,
      contextPanelOpen: true,
      advancedPanelOpen: false,
    },
  },
  updatedAt: "2026-08-15T00:00:00.000Z",
};

function sha256(value: string | Buffer): string {
  return createHash("sha256").update(value).digest("hex");
}

async function analyzeWebGlPreviewPixels(
  page: Page,
  beforeImage: Buffer,
  previewImage: Buffer,
): Promise<WebGlPreviewPixelEvidence> {
  return page.evaluate(async ({ beforePng, previewPng, tolerance }) => {
    const decodePng = async (encodedPng: string) => {
      const binary = atob(encodedPng);
      const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
      const bitmap = await createImageBitmap(new Blob([bytes], { type: "image/png" }));
      const scratch = document.createElement("canvas");
      scratch.width = bitmap.width;
      scratch.height = bitmap.height;
      const context = scratch.getContext("2d", { willReadFrequently: true });
      context?.drawImage(bitmap, 0, 0);
      bitmap.close();
      const pixels = context?.getImageData(0, 0, scratch.width, scratch.height).data;
      if (!pixels) throw new Error("The WebGL evidence PNG could not be decoded.");
      return { height: scratch.height, pixels, width: scratch.width };
    };

    const [before, preview] = await Promise.all([decodePng(beforePng), decodePng(previewPng)]);
    if (before.width !== preview.width || before.height !== preview.height) {
      throw new Error("The fitted baseline and comparison preview must use the same canvas extent.");
    }

    // Ignore HTML status overlays at the top and the unchanged WebGL axis gizmo
    // at bottom-right. The remaining pixels can only prove model/ghost rendering.
    const minX = Math.floor(preview.width * 0.04);
    const maxX = Math.ceil(preview.width * 0.9);
    const minY = Math.floor(preview.height * 0.24);
    const maxY = Math.ceil(preview.height * 0.92);
    const sourceColor = [169, 104, 36] as const;
    const proposedColor = [8, 127, 140] as const;
    const quantizedColors = new Map<number, number>();
    let analysisPixelCount = 0;
    let lumaSum = 0;
    let lumaSquaredSum = 0;
    let modelPixelCount = 0;
    let modelMinX = preview.width;
    let modelMinY = preview.height;
    let modelMaxX = -1;
    let modelMaxY = -1;
    let sourceGhostPixelCount = 0;
    let proposedGhostPixelCount = 0;
    let sourceGhostChangedPixelCount = 0;
    let proposedGhostChangedPixelCount = 0;
    const matchesColor = (red: number, green: number, blue: number, color: readonly number[]) => (
      Math.abs(red - (color[0] ?? 0)) <= tolerance
      && Math.abs(green - (color[1] ?? 0)) <= tolerance
      && Math.abs(blue - (color[2] ?? 0)) <= tolerance
    );

    for (let y = minY; y < maxY; y += 1) {
      for (let x = minX; x < maxX; x += 1) {
        const pixel = y * preview.width + x;
        const offset = pixel * 4;
        const red = preview.pixels[offset] ?? 0;
        const green = preview.pixels[offset + 1] ?? 0;
        const blue = preview.pixels[offset + 2] ?? 0;
        const beforeRed = before.pixels[offset] ?? 0;
        const beforeGreen = before.pixels[offset + 1] ?? 0;
        const beforeBlue = before.pixels[offset + 2] ?? 0;
        const changed = Math.max(
          Math.abs(red - beforeRed),
          Math.abs(green - beforeGreen),
          Math.abs(blue - beforeBlue),
        ) >= 15;
        const sourceMatch = matchesColor(red, green, blue, sourceColor);
        const proposedMatch = matchesColor(red, green, blue, proposedColor);
        if (sourceMatch) {
          sourceGhostPixelCount += 1;
          if (changed) sourceGhostChangedPixelCount += 1;
        }
        if (proposedMatch) {
          proposedGhostPixelCount += 1;
          if (changed) proposedGhostChangedPixelCount += 1;
        }

        const isWarmModelPixel = beforeRed - beforeBlue >= 18
          && beforeRed >= 70
          && beforeGreen >= 55
          && beforeBlue < 190
          && beforeRed >= beforeGreen - 3;
        if (isWarmModelPixel) {
          modelPixelCount += 1;
          modelMinX = Math.min(modelMinX, x);
          modelMinY = Math.min(modelMinY, y);
          modelMaxX = Math.max(modelMaxX, x);
          modelMaxY = Math.max(modelMaxY, y);
        }

        const luma = 0.2126 * beforeRed + 0.7152 * beforeGreen + 0.0722 * beforeBlue;
        lumaSum += luma;
        lumaSquaredSum += luma * luma;
        analysisPixelCount += 1;
        const quantized = (Math.floor(beforeRed / 16) << 8)
          | (Math.floor(beforeGreen / 16) << 4)
          | Math.floor(beforeBlue / 16);
        quantizedColors.set(quantized, (quantizedColors.get(quantized) ?? 0) + 1);
      }
    }

    const dominantQuantizedColorCount = Math.max(0, ...quantizedColors.values());
    const meanLuma = analysisPixelCount > 0 ? lumaSum / analysisPixelCount : 0;
    const lumaVariance = analysisPixelCount > 0
      ? Math.max(0, lumaSquaredSum / analysisPixelCount - meanLuma * meanLuma)
      : 0;
    return {
      analysisPixelCount,
      canvasHeight: preview.height,
      canvasWidth: preview.width,
      dominantQuantizedColorRatio: analysisPixelCount > 0
        ? dominantQuantizedColorCount / analysisPixelCount
        : 1,
      lumaStandardDeviation: Math.sqrt(lumaVariance),
      modelHeightRatio: modelMaxY >= modelMinY
        ? (modelMaxY - modelMinY + 1) / preview.height
        : 0,
      modelPixelRatio: analysisPixelCount > 0 ? modelPixelCount / analysisPixelCount : 0,
      modelWidthRatio: modelMaxX >= modelMinX
        ? (modelMaxX - modelMinX + 1) / preview.width
        : 0,
      proposedGhostChangedPixelCount,
      proposedGhostPixelCount,
      sourceGhostChangedPixelCount,
      sourceGhostPixelCount,
    };
  }, {
    beforePng: beforeImage.toString("base64"),
    previewPng: previewImage.toString("base64"),
    tolerance: GHOST_PIXEL_TOLERANCE,
  });
}

function observePageHealth(page: Page): PageHealth {
  const health: PageHealth = { apiRequests: [], failedRequests: [], pageErrors: [] };
  page.on("pageerror", (error) => health.pageErrors.push(error.message));
  page.on("request", (request) => {
    const pathname = new URL(request.url()).pathname;
    if (pathname.startsWith("/v1/")) {
      health.apiRequests.push(`${request.method()} ${pathname}`);
    }
  });
  page.on("requestfailed", (request) => {
    health.failedRequests.push(
      `${request.method()} ${request.url()}: ${request.failure()?.errorText ?? "unknown"}`,
    );
  });
  return health;
}

function apiMutationRequests(health: PageHealth): string[] {
  return health.apiRequests.filter((request) => !/^(GET|HEAD|OPTIONS) /.test(request));
}

function expectNoApiMutations(health: PageHealth): void {
  expect(apiMutationRequests(health)).toEqual([]);
}

function unexpectedFailedRequests(health: PageHealth): string[] {
  return health.failedRequests.filter((request) => !/^GET .+\/v1\/me: /.test(request));
}

async function installDeterministicDraft(page: Page): Promise<void> {
  await page.addInitScript(({ draftKey, snapshot }) => {
    const probe: ValidationGhostProbe = {
      storageMutations: [],
      historyPushes: 0,
      historyReplaces: 0,
    };
    window.__custombuildValidationGhostProbe = probe;

    const nativeSetItem = Storage.prototype.setItem;
    const nativeRemoveItem = Storage.prototype.removeItem;
    const nativeClear = Storage.prototype.clear;
    const storageName = (storage: Storage): "local" | "session" => (
      storage === window.localStorage ? "local" : "session"
    );

    Object.defineProperties(Storage.prototype, {
      setItem: {
        configurable: true,
        value(this: Storage, key: string, value: string) {
          probe.storageMutations.push({
            storage: storageName(this),
            operation: "set",
            key,
          });
          return Reflect.apply(nativeSetItem, this, [key, value]);
        },
      },
      removeItem: {
        configurable: true,
        value(this: Storage, key: string) {
          probe.storageMutations.push({
            storage: storageName(this),
            operation: "remove",
            key,
          });
          return Reflect.apply(nativeRemoveItem, this, [key]);
        },
      },
      clear: {
        configurable: true,
        value(this: Storage) {
          probe.storageMutations.push({
            storage: storageName(this),
            operation: "clear",
            key: null,
          });
          return Reflect.apply(nativeClear, this, []);
        },
      },
    });

    const nativePushState = History.prototype.pushState;
    const nativeReplaceState = History.prototype.replaceState;
    History.prototype.pushState = function pushState(
      data: unknown,
      unused: string,
      url?: string | URL | null,
    ) {
      probe.historyPushes += 1;
      return nativePushState.call(this, data, unused, url);
    };
    History.prototype.replaceState = function replaceState(
      data: unknown,
      unused: string,
      url?: string | URL | null,
    ) {
      probe.historyReplaces += 1;
      return nativeReplaceState.call(this, data, unused, url);
    };

    Reflect.apply(nativeClear, window.localStorage, []);
    Reflect.apply(nativeClear, window.sessionStorage, []);
    Reflect.apply(nativeSetItem, window.localStorage, [draftKey, JSON.stringify(snapshot)]);
  }, { draftKey: DRAFT_KEY, snapshot: draftSnapshot });
}

async function forceSvgFallback(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const nativeGetContext = HTMLCanvasElement.prototype.getContext;
    Object.defineProperty(HTMLCanvasElement.prototype, "getContext", {
      configurable: true,
      value(this: HTMLCanvasElement, contextId: string, ...args: unknown[]) {
        if (
          contextId === "webgl"
          || contextId === "webgl2"
          || contextId === "experimental-webgl"
        ) {
          return null;
        }
        return Reflect.apply(nativeGetContext, this, [contextId, ...args]);
      },
    });
  });
}

async function readProbe(page: Page): Promise<ValidationGhostProbe> {
  return page.evaluate(() => {
    const probe = window.__custombuildValidationGhostProbe;
    if (!probe) throw new Error("The validation ghost mutation probe was not installed.");
    return {
      storageMutations: [...probe.storageMutations],
      historyPushes: probe.historyPushes,
      historyReplaces: probe.historyReplaces,
    };
  });
}

async function resetProbe(page: Page): Promise<void> {
  await page.evaluate(() => {
    const probe = window.__custombuildValidationGhostProbe;
    if (!probe) throw new Error("The validation ghost mutation probe was not installed.");
    probe.storageMutations.length = 0;
    probe.historyPushes = 0;
    probe.historyReplaces = 0;
  });
}

async function settleInitialAutosave(page: Page): Promise<void> {
  await expect.poll(async () => (await readProbe(page)).storageMutations.filter(
    (mutation) => mutation.storage === "local"
      && mutation.operation === "set"
      && mutation.key === DRAFT_KEY,
  ).length, {
    message: "The hydrated offline draft must complete its initial autosave.",
    timeout: 5_000,
  }).toBeGreaterThan(0);
  await page.waitForTimeout(550);
  await resetProbe(page);
}

async function readDraft(page: Page): Promise<string> {
  const raw = await page.evaluate((key) => window.localStorage.getItem(key), DRAFT_KEY);
  if (raw === null) throw new Error("The deterministic offline draft disappeared.");
  return raw;
}

function draftBaseCabinetCount(raw: string): number | undefined {
  const parsed = JSON.parse(raw) as { spec?: { base_cabinet_count?: unknown } };
  return typeof parsed.spec?.base_cabinet_count === "number"
    ? parsed.spec.base_cabinet_count
    : undefined;
}

async function readViewerCheckpoint(canvas: Locator): Promise<ViewerCheckpoint | null> {
  return canvas.evaluate((element, attributes) => {
    const modelRoot = element.getAttribute(attributes.modelRoot);
    const rawCommit = element.getAttribute(attributes.renderCommit);
    const renderCommit = rawCommit === null ? Number.NaN : Number(rawCommit);
    if (!modelRoot || !Number.isSafeInteger(renderCommit) || renderCommit < 1) return null;
    return {
      canvasIdentity: element.getAttribute(attributes.canvasIdentity),
      modelRoot,
      renderCommit,
    };
  }, {
    canvasIdentity: CANVAS_IDENTITY_ATTRIBUTE,
    modelRoot: MODEL_ROOT_ATTRIBUTE,
    renderCommit: RENDER_COMMIT_ATTRIBUTE,
  });
}

async function waitForViewerReady(canvas: Locator): Promise<ViewerCheckpoint> {
  await expect.poll(async () => (await readViewerCheckpoint(canvas))?.renderCommit ?? 0, {
    message: "The real WebGL viewer must expose a committed model root.",
    timeout: 15_000,
  }).toBeGreaterThan(0);
  const checkpoint = await readViewerCheckpoint(canvas);
  if (!checkpoint) throw new Error("The viewer checkpoint disappeared after WebGL became ready.");
  return checkpoint;
}

async function waitForNextViewerCommit(
  canvas: Locator,
  afterCommit: number,
  expectedModelRoot: string,
): Promise<ViewerCheckpoint> {
  await expect.poll(async () => (await readViewerCheckpoint(canvas))?.renderCommit ?? 0, {
    intervals: [16, 32, 64, 100],
    message: `The WebGL viewer must commit a revision after ${afterCommit}.`,
    timeout: 15_000,
  }).toBeGreaterThan(afterCommit);
  const checkpoint = await readViewerCheckpoint(canvas);
  if (!checkpoint) throw new Error("The viewer checkpoint disappeared after the committed frame.");
  expect(checkpoint.canvasIdentity).toBe(CANVAS_IDENTITY);
  expect(checkpoint.modelRoot).toBe(expectedModelRoot);
  return checkpoint;
}

function comparisonLegend(page: Page): Locator {
  return page.locator("aside[role='status']").filter({
    hasText: "Lokalt beräknad förhandsvisning",
  });
}

async function readLegendStyle(
  legend: Locator,
  label: "Nuvarande" | "Föreslagen",
): Promise<{ borderTopColor: string; borderTopStyle: string }> {
  return legend.locator("dt", { hasText: label }).locator("i").evaluate((element) => {
    const style = window.getComputedStyle(element);
    return {
      borderTopColor: style.borderTopColor,
      borderTopStyle: style.borderTopStyle,
    };
  });
}

async function expectComparisonLegend(legend: Locator): Promise<{
  proposed: { borderTopColor: string; borderTopStyle: string };
  source: { borderTopColor: string; borderTopStyle: string };
}> {
  await expect(legend).toHaveCount(1);
  await expect(legend).toContainText("Regel BASE-SUPPORT-001 · version 1.0.0");
  await expect(legend).toContainText("streckad ockra kontur");
  await expect(legend).toContainText("prickad turkos kontur");
  const source = await readLegendStyle(legend, "Nuvarande");
  const proposed = await readLegendStyle(legend, "Föreslagen");
  expect(source).toEqual({ borderTopColor: "rgb(169, 104, 36)", borderTopStyle: "dashed" });
  expect(proposed).toEqual({ borderTopColor: "rgb(8, 127, 140)", borderTopStyle: "dotted" });
  return { proposed, source };
}

async function attachJson(testInfo: TestInfo, name: string, value: unknown): Promise<void> {
  await testInfo.attach(name, {
    body: Buffer.from(`${JSON.stringify(value, null, 2)}\n`, "utf8"),
    contentType: "application/json",
  });
}

test.skip(
  process.env.PLAYWRIGHT_REAL_API === "1",
  "The validation ghost acceptance is deterministic and runs against the offline production build.",
);
test.skip(
  ({ browserName }) => browserName !== "chromium",
  "The real WebGL and forced-fallback contracts run once in serialized Chromium.",
);
test.use({ viewport: { width: 1_440, height: 960 } });

test("previews and confirms one real local fix without mutating before consent", async ({ page }, testInfo) => {
  test.setTimeout(90_000);
  const health = observePageHealth(page);
  await installDeterministicDraft(page);

  const response = await page.goto("/?mode=check", { waitUntil: "domcontentloaded" });
  expect(response?.status()).toBe(200);
  const model = page.getByLabel("Interaktiv 3D-modell av möbeln");
  const canvas = model.locator("canvas");
  const fallback = page.getByTestId("front-projection-fallback");
  const toolbar = page.getByRole("toolbar", { name: "Visningsverktyg" });
  const front = toolbar.getByRole("button", { name: "Front", exact: true });
  const fitView = toolbar.getByRole("button", { name: "Anpassa vy", exact: true });
  const partPicker = page.getByRole("combobox", { name: "Välj möbeldel att inspektera" });
  const undo = page.getByRole("button", { name: "Ångra", exact: true });
  const trigger = page.getByRole("button", { name: FIX_TRIGGER_NAME, exact: true });

  await expect(model).toBeVisible();
  await expect(canvas).toBeVisible();
  await expect(fallback).toHaveCount(0);
  await expect(front).toHaveAttribute("aria-pressed", "true");
  await expect(partPicker).toHaveValue("side-left");
  await expect(page.locator(".canvas-shell")).toHaveClass(/part-selected/);
  await expect(undo).toBeDisabled();
  await expect(trigger).toBeVisible();
  await canvas.evaluate((element, identity) => {
    element.setAttribute("data-validation-ghost-canvas", identity);
  }, CANVAS_IDENTITY);
  const initialCheckpoint = await waitForViewerReady(canvas);
  expect(initialCheckpoint.canvasIdentity).toBe(CANVAS_IDENTITY);
  const webGlHealth = await canvas.evaluate((element) => {
    const renderCanvas = element as HTMLCanvasElement;
    const context = renderCanvas.getContext("webgl2") ?? renderCanvas.getContext("webgl");
    return context ? { context: "WebGL2RenderingContext" in window && context instanceof WebGL2RenderingContext ? "webgl2" : "webgl", lost: context.isContextLost() } : null;
  });
  expect(webGlHealth).not.toBeNull();
  expect(webGlHealth?.lost).toBe(false);

  await fitView.click();
  await waitForNextViewerCommit(
    canvas,
    initialCheckpoint.renderCommit,
    initialCheckpoint.modelRoot,
  );
  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));
  const fitCheckpoint = await readViewerCheckpoint(canvas);
  if (!fitCheckpoint) throw new Error("The fitted Front-view checkpoint disappeared.");
  expect(fitCheckpoint.canvasIdentity).toBe(CANVAS_IDENTITY);
  expect(fitCheckpoint.modelRoot).toBe(initialCheckpoint.modelRoot);
  expect(fitCheckpoint.renderCommit).toBeGreaterThan(initialCheckpoint.renderCommit);
  await expect(front).toHaveAttribute("aria-pressed", "true");
  await expect(partPicker).toHaveValue("side-left");
  await expect(page.locator(".canvas-shell")).toHaveClass(/part-selected/);

  await settleInitialAutosave(page);
  const baselineDraft = await readDraft(page);
  const baselineUrl = page.url();
  const beforeImage = await canvas.screenshot({ animations: "disabled", scale: "css" });

  await trigger.click();
  const previewRegion = page.getByRole("region", { name: "Kontrollera ändringen före tillämpning" });
  await expect(previewRegion).toBeVisible();
  await expect(previewRegion.locator("dl > div").filter({ hasText: "base_cabinet_count" }))
    .toContainText(/Före\s*4\s*→\s*Efter\s*5/);
  const previewCheckpoint = await waitForNextViewerCommit(
    canvas,
    fitCheckpoint.renderCommit,
    initialCheckpoint.modelRoot,
  );
  const legendStyles = await expectComparisonLegend(comparisonLegend(page));
  await expect(front).toHaveAttribute("aria-pressed", "true");
  await expect(partPicker).toHaveValue("side-left");
  await expect(page.locator(".canvas-shell")).toHaveClass(/part-selected/);
  await expect(undo).toBeDisabled();
  expect(await readDraft(page)).toBe(baselineDraft);
  expect(await readProbe(page)).toEqual({
    storageMutations: [],
    historyPushes: 0,
    historyReplaces: 0,
  });
  expect(page.url()).toBe(baselineUrl);
  expectNoApiMutations(health);
  const previewImage = await canvas.screenshot({ animations: "disabled", scale: "css" });
  const pixelEvidence = await analyzeWebGlPreviewPixels(page, beforeImage, previewImage);
  await attachJson(testInfo, "validation-ghost-webgl-pixel-evidence.json", pixelEvidence);
  expect(previewImage.byteLength).toBeGreaterThan(10 * 1_024);
  expect(pixelEvidence.modelWidthRatio).toBeGreaterThan(0.45);
  expect(pixelEvidence.modelWidthRatio).toBeLessThan(0.92);
  expect(pixelEvidence.modelHeightRatio).toBeGreaterThan(0.35);
  expect(pixelEvidence.modelHeightRatio).toBeLessThan(0.92);
  expect(pixelEvidence.modelPixelRatio).toBeGreaterThan(0.05);
  expect(pixelEvidence.modelPixelRatio).toBeLessThan(MAX_FITTED_MODEL_PIXEL_RATIO);
  expect(pixelEvidence.lumaStandardDeviation).toBeGreaterThan(10);
  expect(pixelEvidence.dominantQuantizedColorRatio).toBeLessThan(0.65);
  expect(pixelEvidence.sourceGhostPixelCount).toBeGreaterThan(20);
  expect(pixelEvidence.sourceGhostChangedPixelCount).toBeGreaterThan(10);
  expect(pixelEvidence.proposedGhostPixelCount).toBeGreaterThan(20);
  expect(pixelEvidence.proposedGhostChangedPixelCount).toBeGreaterThan(10);
  expect(sha256(previewImage)).not.toBe(sha256(beforeImage));
  await testInfo.attach("validation-ghost-webgl.png", {
    body: previewImage,
    contentType: "image/png",
  });

  await previewRegion.getByRole("button", { name: "Avbryt" }).click();
  await expect(trigger).toBeFocused();
  await expect(previewRegion).toHaveCount(0);
  await expect(comparisonLegend(page)).toHaveCount(0);
  const cancelCheckpoint = await waitForNextViewerCommit(
    canvas,
    previewCheckpoint.renderCommit,
    initialCheckpoint.modelRoot,
  );
  expect(await readDraft(page)).toBe(baselineDraft);
  expect(await readProbe(page)).toEqual({
    storageMutations: [],
    historyPushes: 0,
    historyReplaces: 0,
  });
  expect(page.url()).toBe(baselineUrl);
  expectNoApiMutations(health);
  await expect(front).toHaveAttribute("aria-pressed", "true");
  await expect(partPicker).toHaveValue("side-left");

  await trigger.click();
  await expect(previewRegion).toBeVisible();
  const reopenedCheckpoint = await waitForNextViewerCommit(
    canvas,
    cancelCheckpoint.renderCommit,
    initialCheckpoint.modelRoot,
  );
  const confirm = previewRegion.getByRole("button", { name: "Bekräfta och tillämpa" });
  await confirm.focus();
  await expect(confirm).toBeFocused();
  await confirm.press("Enter");
  await expect(previewRegion).toHaveCount(0);
  const confirmCheckpoint = await waitForNextViewerCommit(
    canvas,
    reopenedCheckpoint.renderCommit,
    initialCheckpoint.modelRoot,
  );
  await expect.poll(async () => draftBaseCabinetCount(await readDraft(page)), {
    message: "Keyboard confirmation must persist the exact five-module proposal.",
    timeout: 5_000,
  }).toBe(5);
  await expect.poll(async () => (await readProbe(page)).storageMutations.length, {
    message: "Confirmation must create exactly one offline autosave.",
    timeout: 5_000,
  }).toBe(1);
  await page.waitForTimeout(550);
  const confirmProbe = await readProbe(page);
  expect(confirmProbe).toEqual({
    storageMutations: [{ storage: "local", operation: "set", key: DRAFT_KEY }],
    historyPushes: 0,
    historyReplaces: 0,
  });
  await expect(page.getByText("Sparad lokalt", { exact: true })).toBeVisible();
  await expect(undo).toBeEnabled();
  await expect(front).toHaveAttribute("aria-pressed", "true");
  await expect(partPicker).toHaveValue("side-left");
  expectNoApiMutations(health);

  await resetProbe(page);
  await undo.click();
  const undoCheckpoint = await waitForNextViewerCommit(
    canvas,
    confirmCheckpoint.renderCommit,
    initialCheckpoint.modelRoot,
  );
  await expect.poll(async () => draftBaseCabinetCount(await readDraft(page)), {
    message: "One undo must restore the exact four-module source draft.",
    timeout: 5_000,
  }).toBe(4);
  await expect.poll(async () => (await readProbe(page)).storageMutations.length, {
    message: "Undo must create exactly one offline autosave.",
    timeout: 5_000,
  }).toBe(1);
  await page.waitForTimeout(550);
  const undoProbe = await readProbe(page);
  expect(undoProbe).toEqual({
    storageMutations: [{ storage: "local", operation: "set", key: DRAFT_KEY }],
    historyPushes: 0,
    historyReplaces: 0,
  });
  await expect(undo).toBeDisabled();
  await expect(front).toHaveAttribute("aria-pressed", "true");
  await expect(partPicker).toHaveValue("side-left");
  expect(page.url()).toBe(baselineUrl);
  expectNoApiMutations(health);
  expect(unexpectedFailedRequests(health)).toEqual([]);
  expect(health.pageErrors).toEqual([]);

  await attachJson(testInfo, "validation-ghost-webgl.json", {
    schema_version: 1,
    scope: {
      browser: "chromium",
      offline: true,
      physical_cutting_authorized: false,
    },
    fixture: {
      id: sourceSpec.design_id,
      rule_id: "BASE-SUPPORT-001",
      source_base_cabinet_count: 4,
      proposed_base_cabinet_count: 5,
    },
    checkpoints: {
      initial: initialCheckpoint,
      fit: fitCheckpoint,
      preview: previewCheckpoint,
      cancel: cancelCheckpoint,
      reopen: reopenedCheckpoint,
      confirm: confirmCheckpoint,
      undo: undoCheckpoint,
    },
    state: {
      canvas_identity_preserved: undoCheckpoint.canvasIdentity === CANVAS_IDENTITY,
      model_root_preserved: [
        previewCheckpoint,
        cancelCheckpoint,
        reopenedCheckpoint,
        confirmCheckpoint,
        undoCheckpoint,
      ].every((checkpoint) => checkpoint.modelRoot === initialCheckpoint.modelRoot),
      view_mode: "front",
      selected_part_id: await partPicker.inputValue(),
      baseline_draft_sha256: sha256(baselineDraft),
      restored_draft_sha256: sha256(await readDraft(page)),
      url: page.url(),
    },
    mutations: {
      before_consent: {
        api_requests: health.apiRequests,
        api_mutation_requests: apiMutationRequests(health),
        storage_mutations: [],
        history_pushes: 0,
        history_replaces: 0,
      },
      confirm: confirmProbe,
      undo: undoProbe,
      api_requests: health.apiRequests,
    },
    rendering: {
      webgl: webGlHealth,
      before_png_bytes: beforeImage.byteLength,
      before_png_sha256: sha256(beforeImage),
      preview_png_bytes: previewImage.byteLength,
      preview_png_sha256: sha256(previewImage),
      pixel_evidence: {
        ...pixelEvidence,
        ghost_rgb_tolerance: GHOST_PIXEL_TOLERANCE,
      },
      legend: legendStyles,
    },
    health,
  });
});

test("keeps the same real fix truthful and non-interactive in the forced SVG fallback", async ({ page }, testInfo) => {
  test.setTimeout(45_000);
  const health = observePageHealth(page);
  await installDeterministicDraft(page);
  await forceSvgFallback(page);

  const response = await page.goto("/?mode=check", { waitUntil: "domcontentloaded" });
  expect(response?.status()).toBe(200);
  const fallback = page.getByTestId("front-projection-fallback");
  const trigger = page.getByRole("button", { name: FIX_TRIGGER_NAME, exact: true });
  await expect(fallback).toBeVisible();
  await expect(fallback.locator("svg")).toBeVisible();
  await expect(page.locator("canvas")).toHaveCount(0);
  await expect(trigger).toBeVisible();
  await settleInitialAutosave(page);
  const baselineDraft = await readDraft(page);
  const baselineUrl = page.url();

  await trigger.click();
  const previewRegion = page.getByRole("region", { name: "Kontrollera ändringen före tillämpning" });
  await expect(previewRegion).toBeVisible();
  const legendStyles = await expectComparisonLegend(comparisonLegend(page));
  const ghost = page.getByTestId("comparison-ghost-outlines");
  await expect(ghost).toBeVisible();
  await expect(ghost).toHaveAttribute("aria-hidden", "true");
  await expect(ghost).toHaveAttribute("pointer-events", "none");
  expect(await ghost.locator("rect").count()).toBeGreaterThan(0);
  await expect(ghost.locator("[tabindex]")).toHaveCount(0);
  await expect(ghost.getByRole("button")).toHaveCount(0);
  expect(await readDraft(page)).toBe(baselineDraft);
  expect(await readProbe(page)).toEqual({
    storageMutations: [],
    historyPushes: 0,
    historyReplaces: 0,
  });
  expectNoApiMutations(health);
  const fallbackImage = await fallback.screenshot({ animations: "disabled", scale: "css" });
  await testInfo.attach("validation-ghost-svg.png", {
    body: fallbackImage,
    contentType: "image/png",
  });

  await previewRegion.getByRole("button", { name: "Avbryt" }).click();
  await expect(trigger).toBeFocused();
  await expect(previewRegion).toHaveCount(0);
  await expect(ghost).toHaveCount(0);
  await expect(comparisonLegend(page)).toHaveCount(0);
  expect(await readDraft(page)).toBe(baselineDraft);
  expect(await readProbe(page)).toEqual({
    storageMutations: [],
    historyPushes: 0,
    historyReplaces: 0,
  });
  expect(page.url()).toBe(baselineUrl);
  expectNoApiMutations(health);
  expect(unexpectedFailedRequests(health)).toEqual([]);
  expect(health.pageErrors).toEqual([]);

  await attachJson(testInfo, "validation-ghost-svg.json", {
    schema_version: 1,
    scope: {
      browser: "chromium",
      forced_svg_fallback: true,
      offline: true,
      physical_cutting_authorized: false,
    },
    fixture: {
      id: sourceSpec.design_id,
      rule_id: "BASE-SUPPORT-001",
      source_base_cabinet_count: 4,
      proposed_base_cabinet_count: 5,
    },
    state: {
      baseline_draft_sha256: sha256(baselineDraft),
      cancelled_draft_sha256: sha256(await readDraft(page)),
      url: page.url(),
    },
    mutations: {
      api_requests: health.apiRequests,
      storage_mutations: (await readProbe(page)).storageMutations,
      history_pushes: (await readProbe(page)).historyPushes,
      history_replaces: (await readProbe(page)).historyReplaces,
    },
    rendering: {
      fallback_png_bytes: fallbackImage.byteLength,
      fallback_png_sha256: sha256(fallbackImage),
      legend: legendStyles,
    },
    health,
  });
});

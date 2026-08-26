import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { writeFile } from "node:fs/promises";
import { expect, test, type Locator, type Page } from "@playwright/test";
import { DEFAULT_DESIGN_SPEC, type DesignSpec } from "../lib/design-types";

interface FullCeilingBrowserBudget {
  schema_version: 1;
  fixture: {
    id: string;
    width_mm: number;
    height_mm: number;
    depth_mm: number;
    shelf_count: number;
    divider_count: number;
    bay_count: number;
    base_cabinet_count: number;
    base_cabinet_height_mm: number;
    base_cabinet_depth_mm: number;
    expected_rendered_part_count: number;
  };
  sampling: {
    warm_frame_count: number;
    interaction_sample_count: number;
    frame_collection_timeout_ms: number;
  };
  budgets: {
    cold_webgl_ready_ms: number;
    warm_frame_interval_p95_ms: number;
    warm_frame_interval_max_ms: number;
    selection_p95_ms: number;
    view_switch_p95_ms: number;
    transparency_toggle_p95_ms: number;
    warm_long_task_max_ms: number;
  };
  scope: {
    browser: "chromium";
    network_and_server_preview_included: false;
    physical_cutting_authorized: false;
    reason: string;
  };
}

interface Distribution {
  median_ms: number;
  p95_ms: number;
  max_ms: number;
  samples_ms: number[];
}

interface BrowserProbe {
  navigation_started_at_ms: number;
  webgl_context_losses: number;
  long_tasks_ms: number[];
  observer?: PerformanceObserver;
  renderer_commit_observations: RendererCommitProbe[];
  renderer_commit_observer?: MutationObserver;
}

type BrowserHealthProbe = Pick<
  BrowserProbe,
  "navigation_started_at_ms" | "webgl_context_losses" | "long_tasks_ms"
>;

interface WebGlDetails {
  antialias: boolean | null;
  context: "webgl" | "webgl2";
  drawing_buffer_height: number;
  drawing_buffer_width: number;
  is_context_lost: boolean;
  renderer: string;
  unmasked_renderer: string | null;
  unmasked_vendor: string | null;
  vendor: string;
  version: string;
}

interface RendererCommitProbe {
  canvas_identity: string | null;
  model_root: string;
  observed_at_ms: number;
  render_commit: number;
}

interface ImageVariation {
  color_bucket_count: number;
  luminance_range: number;
  sampled_pixel_count: number;
}

interface MeasuredAction<T> {
  committed: T;
  duration_ms: number;
}

interface OrbitDragStart {
  inward_x: -1 | 1;
  inward_y: -1 | 1;
  x: number;
  y: number;
}

const DRAFT_KEY = "custombuild:workspace:v3:anonymous:project:local-draft:draft";
const CANVAS_IDENTITY = "b1-full-ceiling-webgl-canvas";
const DIAGNOSTIC_ACTION_TIMEOUT_MS = 15_000;

function record(value: unknown, path: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${path} must be an object.`);
  }
  return value as Record<string, unknown>;
}

function positiveNumber(value: unknown, path: string, integer = false): number {
  if (
    typeof value !== "number"
    || !Number.isFinite(value)
    || value <= 0
    || (integer && !Number.isInteger(value))
  ) {
    throw new Error(`${path} must be a positive${integer ? " integer" : " finite number"}.`);
  }
  return value;
}

function parseBudget(value: unknown): FullCeilingBrowserBudget {
  const root = record(value, "budget");
  const fixture = record(root.fixture, "budget.fixture");
  const sampling = record(root.sampling, "budget.sampling");
  const budgets = record(root.budgets, "budget.budgets");
  const scope = record(root.scope, "budget.scope");
  if (root.schema_version !== 1) throw new Error("budget.schema_version must be 1.");
  if (typeof fixture.id !== "string" || !fixture.id) throw new Error("budget.fixture.id is required.");
  if (
    scope.browser !== "chromium"
    || scope.network_and_server_preview_included !== false
    || scope.physical_cutting_authorized !== false
    || typeof scope.reason !== "string"
    || !scope.reason
  ) {
    throw new Error("budget.scope must keep the Chromium-only, offline, non-production boundary.");
  }

  const parsed: FullCeilingBrowserBudget = {
    schema_version: 1,
    fixture: {
      id: fixture.id,
      width_mm: positiveNumber(fixture.width_mm, "budget.fixture.width_mm", true),
      height_mm: positiveNumber(fixture.height_mm, "budget.fixture.height_mm", true),
      depth_mm: positiveNumber(fixture.depth_mm, "budget.fixture.depth_mm", true),
      shelf_count: positiveNumber(fixture.shelf_count, "budget.fixture.shelf_count", true),
      divider_count: positiveNumber(fixture.divider_count, "budget.fixture.divider_count", true),
      bay_count: positiveNumber(fixture.bay_count, "budget.fixture.bay_count", true),
      base_cabinet_count: positiveNumber(fixture.base_cabinet_count, "budget.fixture.base_cabinet_count", true),
      base_cabinet_height_mm: positiveNumber(
        fixture.base_cabinet_height_mm,
        "budget.fixture.base_cabinet_height_mm",
        true,
      ),
      base_cabinet_depth_mm: positiveNumber(
        fixture.base_cabinet_depth_mm,
        "budget.fixture.base_cabinet_depth_mm",
        true,
      ),
      expected_rendered_part_count: positiveNumber(
        fixture.expected_rendered_part_count,
        "budget.fixture.expected_rendered_part_count",
        true,
      ),
    },
    sampling: {
      warm_frame_count: positiveNumber(sampling.warm_frame_count, "budget.sampling.warm_frame_count", true),
      interaction_sample_count: positiveNumber(
        sampling.interaction_sample_count,
        "budget.sampling.interaction_sample_count",
        true,
      ),
      frame_collection_timeout_ms: positiveNumber(
        sampling.frame_collection_timeout_ms,
        "budget.sampling.frame_collection_timeout_ms",
        true,
      ),
    },
    budgets: {
      cold_webgl_ready_ms: positiveNumber(budgets.cold_webgl_ready_ms, "budget.budgets.cold_webgl_ready_ms"),
      warm_frame_interval_p95_ms: positiveNumber(
        budgets.warm_frame_interval_p95_ms,
        "budget.budgets.warm_frame_interval_p95_ms",
      ),
      warm_frame_interval_max_ms: positiveNumber(
        budgets.warm_frame_interval_max_ms,
        "budget.budgets.warm_frame_interval_max_ms",
      ),
      selection_p95_ms: positiveNumber(budgets.selection_p95_ms, "budget.budgets.selection_p95_ms"),
      view_switch_p95_ms: positiveNumber(budgets.view_switch_p95_ms, "budget.budgets.view_switch_p95_ms"),
      transparency_toggle_p95_ms: positiveNumber(
        budgets.transparency_toggle_p95_ms,
        "budget.budgets.transparency_toggle_p95_ms",
      ),
      warm_long_task_max_ms: positiveNumber(
        budgets.warm_long_task_max_ms,
        "budget.budgets.warm_long_task_max_ms",
      ),
    },
    scope: {
      browser: "chromium",
      network_and_server_preview_included: false,
      physical_cutting_authorized: false,
      reason: scope.reason,
    },
  };

  const exactFixture = parsed.fixture;
  if (
    exactFixture.width_mm !== 6_000
    || exactFixture.height_mm !== 4_000
    || exactFixture.depth_mm !== 1_200
    || exactFixture.shelf_count !== 40
    || exactFixture.divider_count !== 16
    || exactFixture.bay_count !== 17
    || exactFixture.base_cabinet_count !== 17
    || exactFixture.base_cabinet_height_mm !== 680
    || exactFixture.base_cabinet_depth_mm !== exactFixture.depth_mm
    || exactFixture.expected_rendered_part_count !== 752
  ) {
    throw new Error("The browser fixture must remain bound to the exact canonical B1 full ceiling.");
  }
  return parsed;
}

const budget = parseBudget(JSON.parse(
  readFileSync(new URL("../performance/full-ceiling-browser-budget.json", import.meta.url), "utf8"),
));

const fullCeilingSpec = {
  ...DEFAULT_DESIGN_SPEC,
  design_id: "e2e-b1-full-ceiling-wall-library",
  furniture_type: "wall_library",
  width_mm: budget.fixture.width_mm,
  height_mm: budget.fixture.height_mm,
  depth_mm: budget.fixture.depth_mm,
  shelf_count: budget.fixture.shelf_count,
  divider_count: budget.fixture.divider_count,
  bay_sizing_mode: "count",
  bay_width_ratios: [],
  shelf_height_ratios: [],
  symmetry_locked: true,
  part_overrides: {},
  removed_part_ids: [],
  base_cabinet_height_mm: budget.fixture.base_cabinet_height_mm,
  base_cabinet_depth_mm: budget.fixture.base_cabinet_depth_mm,
  base_cabinet_count: budget.fixture.base_cabinet_count,
  reinforcement_mode: "manual",
  back_panel: true,
  plinth: true,
} satisfies DesignSpec;

function distribution(samples: readonly number[]): Distribution {
  if (samples.length === 0) throw new Error("A performance distribution cannot be empty.");
  const sorted = [...samples].sort((left, right) => left - right);
  const at = (fraction: number) => sorted[Math.max(0, Math.ceil(sorted.length * fraction) - 1)] ?? 0;
  return {
    median_ms: Number(at(0.5).toFixed(2)),
    p95_ms: Number(at(0.95).toFixed(2)),
    max_ms: Number((sorted.at(-1) ?? 0).toFixed(2)),
    samples_ms: sorted.map((sample) => Number(sample.toFixed(2))),
  };
}

function imageHash(image: Buffer): string {
  return createHash("sha256").update(image).digest("hex");
}

async function installFullCeilingDraft(page: Page): Promise<void> {
  await page.addInitScript(({ draftKey, snapshot }) => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.localStorage.setItem(draftKey, JSON.stringify(snapshot));

    const probe: BrowserProbe = {
      navigation_started_at_ms: performance.now(),
      webgl_context_losses: 0,
      long_tasks_ms: [],
      renderer_commit_observations: [],
    };
    if (PerformanceObserver.supportedEntryTypes.includes("longtask")) {
      probe.observer = new PerformanceObserver((entries) => {
        probe.long_tasks_ms.push(...entries.getEntries().map((entry) => entry.duration));
      });
      probe.observer.observe({ type: "longtask", buffered: true });
    }
    window.addEventListener("webglcontextlost", () => {
      probe.webgl_context_losses += 1;
    }, { capture: true });
    (window as typeof window & { __custombuildFullCeilingProbe?: BrowserProbe })
      .__custombuildFullCeilingProbe = probe;
  }, {
    draftKey: DRAFT_KEY,
    snapshot: {
      version: 3,
      spec: fullCeilingSpec,
      templateId: "wall-library",
      workspaceSelected: true,
      uiState: {
        schemaVersion: 2,
        mode: "studio",
        viewMode: "perspective",
        exploded: false,
        transparent: false,
        isolateSelection: false,
        panels: {
          componentLibraryOpen: true,
          contextPanelOpen: true,
          advancedPanelOpen: false,
        },
      },
      updatedAt: "2026-08-15T00:00:00.000Z",
    },
  });
}

async function waitForFrames(page: Page, frameCount: number): Promise<void> {
  await page.evaluate((count) => new Promise<void>((resolve) => {
    let remaining = count;
    const next = () => {
      remaining -= 1;
      if (remaining <= 0) resolve();
      else requestAnimationFrame(next);
    };
    requestAnimationFrame(next);
  }), frameCount);
}

async function installRendererCommitObserver(canvas: Locator): Promise<void> {
  await canvas.evaluate((element) => {
    const probe = (window as typeof window & { __custombuildFullCeilingProbe?: BrowserProbe })
      .__custombuildFullCeilingProbe;
    if (!probe) throw new Error("The full-ceiling browser probe was not installed.");
    probe.renderer_commit_observer?.disconnect();
    probe.renderer_commit_observations.length = 0;
    const capture = () => {
      const modelRoot = element.getAttribute("data-custombuild-model-root");
      const rawCommit = element.getAttribute("data-custombuild-render-commit");
      const renderCommit = rawCommit === null ? Number.NaN : Number(rawCommit);
      if (!modelRoot || !Number.isSafeInteger(renderCommit) || renderCommit < 1) return;
      const latest = probe.renderer_commit_observations.at(-1);
      if (latest && renderCommit <= latest.render_commit) return;
      probe.renderer_commit_observations.push({
        canvas_identity: element.getAttribute("data-full-ceiling-canvas"),
        model_root: modelRoot,
        observed_at_ms: performance.now(),
        render_commit: renderCommit,
      });
    };
    probe.renderer_commit_observer = new MutationObserver((mutations) => {
      if (mutations.some((mutation) => mutation.attributeName === "data-custombuild-render-commit")) capture();
    });
    probe.renderer_commit_observer.observe(element, {
      attributeFilter: ["data-custombuild-render-commit"],
      attributes: true,
    });
  });
}

async function resetRendererCommitObservations(page: Page): Promise<void> {
  await page.evaluate(() => {
    const probe = (window as typeof window & { __custombuildFullCeilingProbe?: BrowserProbe })
      .__custombuildFullCeilingProbe;
    if (!probe) throw new Error("The full-ceiling browser probe was not installed.");
    probe.renderer_commit_observations.length = 0;
  });
}

async function waitForObservedRendererCommit(
  canvas: Locator,
  afterRevision: number,
  startedAtMs: number,
  expectedModelRoot: string,
): Promise<RendererCommitProbe> {
  const observation = async () => canvas.evaluate((element, {
    after,
    canvasIdentity,
    expectedRoot,
    startedAt,
  }) => {
    const probe = (window as typeof window & { __custombuildFullCeilingProbe?: BrowserProbe })
      .__custombuildFullCeilingProbe;
    if (!probe) throw new Error("The full-ceiling browser probe was not installed.");
    const committed = probe.renderer_commit_observations.find(
      (candidate) => candidate.render_commit > after && candidate.observed_at_ms >= startedAt,
    ) ?? null;
    if (!committed) return null;
    if (committed.canvas_identity !== canvasIdentity) {
      throw new Error("The WebGL canvas was replaced during the OrbitControls drag.");
    }
    if (committed.model_root !== expectedRoot) {
      throw new Error("The stable model-root identity changed during the OrbitControls drag.");
    }
    const currentRoot = element.getAttribute("data-custombuild-model-root");
    const rawCurrentCommit = element.getAttribute("data-custombuild-render-commit");
    const currentCommit = rawCurrentCommit === null ? Number.NaN : Number(rawCurrentCommit);
    if (!Number.isSafeInteger(currentCommit) || currentCommit < committed.render_commit) {
      throw new Error("The canvas renderer revision regressed after the observed OrbitControls commit.");
    }
    if (element.getAttribute("data-full-ceiling-canvas") !== canvasIdentity || currentRoot !== expectedRoot) {
      throw new Error("The canvas or model root changed after the observed OrbitControls commit.");
    }
    return committed;
  }, {
    after: afterRevision,
    canvasIdentity: CANVAS_IDENTITY,
    expectedRoot: expectedModelRoot,
    startedAt: startedAtMs,
  });
  const observed: { committed: RendererCommitProbe | null } = { committed: null };
  await expect.poll(async () => {
    observed.committed = await observation();
    return observed.committed?.render_commit ?? 0;
  }, {
    intervals: [16, 32, 64, 100],
    message: `OrbitControls must commit a renderer revision after ${afterRevision}.`,
    timeout: DIAGNOSTIC_ACTION_TIMEOUT_MS,
  }).toBeGreaterThan(afterRevision);
  const committed = observed.committed;
  if (!committed) throw new Error("The timestamped renderer commit disappeared after observation.");
  return committed;
}

async function orbitDragStart(canvas: Locator): Promise<OrbitDragStart> {
  return canvas.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    const margin = Math.max(8, Math.min(16, Math.floor(Math.min(bounds.width, bounds.height) / 20)));
    const candidates: OrbitDragStart[] = [
      { x: bounds.left + margin, y: bounds.top + margin, inward_x: 1, inward_y: 1 },
      { x: bounds.right - margin, y: bounds.top + margin, inward_x: -1, inward_y: 1 },
      { x: bounds.left + margin, y: bounds.bottom - margin, inward_x: 1, inward_y: -1 },
      { x: bounds.right - margin, y: bounds.bottom - margin, inward_x: -1, inward_y: -1 },
    ];
    const candidate = candidates.find(({ x, y }) => document.elementFromPoint(x, y) === element);
    if (!candidate) throw new Error("No unobstructed near-margin canvas background point was found.");
    return candidate;
  });
}

async function measureAction<T>(
  page: Page,
  action: () => Promise<unknown>,
  committed: () => Promise<T>,
): Promise<MeasuredAction<T>> {
  await waitForFrames(page, 2);
  const startedAt = await page.evaluate(() => performance.now());
  await action();
  const committedValue = await committed();
  await waitForFrames(page, 2);
  return {
    committed: committedValue,
    duration_ms: await page.evaluate((started) => performance.now() - started, startedAt),
  };
}

async function readRendererCommit(canvas: Locator): Promise<RendererCommitProbe | null> {
  return canvas.evaluate((element) => {
    const modelRoot = element.getAttribute("data-custombuild-model-root");
    const rawCommit = element.getAttribute("data-custombuild-render-commit");
    const renderCommit = rawCommit === null ? Number.NaN : Number(rawCommit);
    if (!modelRoot || !Number.isSafeInteger(renderCommit) || renderCommit < 1) return null;
    return {
      canvas_identity: element.getAttribute("data-full-ceiling-canvas"),
      model_root: modelRoot,
      observed_at_ms: performance.now(),
      render_commit: renderCommit,
    } satisfies RendererCommitProbe;
  });
}

async function waitForRendererCommit(
  canvas: Locator,
  afterRevision: number,
  expectedModelRoot?: string,
): Promise<RendererCommitProbe> {
  await expect.poll(async () => {
    const probe = await readRendererCommit(canvas);
    return probe?.render_commit ?? 0;
  }, {
    intervals: [16, 32, 64, 100],
    message: `WebGL renderer must commit a revision after ${afterRevision}.`,
    timeout: DIAGNOSTIC_ACTION_TIMEOUT_MS,
  }).toBeGreaterThan(afterRevision);
  const probe = await readRendererCommit(canvas);
  if (!probe) throw new Error("The renderer commit attributes disappeared after the committed frame.");
  if (probe.canvas_identity !== CANVAS_IDENTITY) {
    throw new Error("The WebGL canvas was replaced while waiting for a renderer commit.");
  }
  if (expectedModelRoot !== undefined && probe.model_root !== expectedModelRoot) {
    throw new Error("The stable model-root identity changed during a visual interaction.");
  }
  return probe;
}

async function imageVariation(page: Page, image: Buffer): Promise<ImageVariation> {
  return page.evaluate(async (encodedPng) => {
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
    if (!pixels) throw new Error("The transparent canvas screenshot could not be decoded.");

    const buckets = new Set<number>();
    let minimumLuminance = 255;
    let maximumLuminance = 0;
    const pixelCount = pixels.length / 4;
    const stridePixels = Math.max(1, Math.floor(pixelCount / 8_192));
    let sampledPixelCount = 0;
    for (let pixel = 0; pixel < pixelCount; pixel += stridePixels) {
      const offset = pixel * 4;
      const red = pixels[offset] ?? 0;
      const green = pixels[offset + 1] ?? 0;
      const blue = pixels[offset + 2] ?? 0;
      const luminance = Math.round(0.2126 * red + 0.7152 * green + 0.0722 * blue);
      minimumLuminance = Math.min(minimumLuminance, luminance);
      maximumLuminance = Math.max(maximumLuminance, luminance);
      buckets.add((red >> 4) * 256 + (green >> 4) * 16 + (blue >> 4));
      sampledPixelCount += 1;
    }
    return {
      color_bucket_count: buckets.size,
      luminance_range: maximumLuminance - minimumLuminance,
      sampled_pixel_count: sampledPixelCount,
    } satisfies ImageVariation;
  }, image.toString("base64"));
}

async function inspectWebGl(canvas: Locator): Promise<WebGlDetails | null> {
  return canvas.evaluate((element) => {
    const renderCanvas = element as HTMLCanvasElement;
    const webgl2 = renderCanvas.getContext("webgl2");
    const context = webgl2 ?? renderCanvas.getContext("webgl");
    if (!context) return null;
    const debug = context.getExtension("WEBGL_debug_renderer_info") as {
      UNMASKED_RENDERER_WEBGL: number;
      UNMASKED_VENDOR_WEBGL: number;
    } | null;
    return {
      antialias: context.getContextAttributes()?.antialias ?? null,
      context: webgl2 ? "webgl2" : "webgl",
      drawing_buffer_height: context.drawingBufferHeight,
      drawing_buffer_width: context.drawingBufferWidth,
      is_context_lost: context.isContextLost(),
      renderer: String(context.getParameter(context.RENDERER)),
      unmasked_renderer: debug ? String(context.getParameter(debug.UNMASKED_RENDERER_WEBGL)) : null,
      unmasked_vendor: debug ? String(context.getParameter(debug.UNMASKED_VENDOR_WEBGL)) : null,
      vendor: String(context.getParameter(context.VENDOR)),
      version: String(context.getParameter(context.VERSION)),
    } satisfies WebGlDetails;
  });
}

async function readProbe(page: Page): Promise<BrowserHealthProbe> {
  return page.evaluate(() => {
    const probe = (window as typeof window & { __custombuildFullCeilingProbe?: BrowserProbe })
      .__custombuildFullCeilingProbe;
    if (!probe) throw new Error("The full-ceiling browser probe was not installed.");
    return {
      navigation_started_at_ms: probe.navigation_started_at_ms,
      webgl_context_losses: probe.webgl_context_losses,
      long_tasks_ms: [...probe.long_tasks_ms],
    };
  });
}

async function resetWarmLongTasks(page: Page): Promise<void> {
  await page.evaluate(() => new Promise<void>((resolve) => window.setTimeout(resolve, 0)));
  await page.evaluate(() => {
    const probe = (window as typeof window & { __custombuildFullCeilingProbe?: BrowserProbe })
      .__custombuildFullCeilingProbe;
    if (!probe) throw new Error("The full-ceiling browser probe was not installed.");
    probe.long_tasks_ms.length = 0;
  });
}

test.skip(
  process.env.PLAYWRIGHT_REAL_API === "1",
  "The full-ceiling browser gate is deterministic and excludes live API or server-preview timing.",
);
test.skip(
  ({ browserName }) => browserName !== "chromium",
  "The full-ceiling WebGL gate is calibrated only for the serialized Chromium project.",
);
test.use({ viewport: { width: 1_440, height: 960 }, video: "off" });

test("keeps the canonical 752-part ceiling bounded in the actual WebGL workspace", async ({ page }, testInfo) => {
  // This is evidence-collection headroom, not a product budget. Every measured
  // threshold remains owned by the immutable budget file and is asserted below.
  test.setTimeout(360_000);
  const pageErrors: string[] = [];
  const failedRequests: string[] = [];
  const rendererCommits: RendererCommitProbe[] = [];
  const selectionSamples: number[] = [];
  const viewSwitchSamples: number[] = [];
  const transparencySamples: number[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("requestfailed", (request) => {
    failedRequests.push(`${request.method()} ${request.url()}: ${request.failure()?.errorText ?? "unknown"}`);
  });

  await installFullCeilingDraft(page);
  // A persisted local draft never overrides an explicit navigation intent.
  // Request Studio through the public URL contract so this concept-only
  // wall-library fixture exercises rendering without gaining production
  // authority from its template id or from local storage.
  const response = await page.goto("/?mode=studio", { waitUntil: "domcontentloaded" });
  expect(response?.status()).toBe(200);
  await expect(page).toHaveURL(/\?mode=studio$/);

  const model = page.getByLabel("Interaktiv 3D-modell av möbeln");
  const canvas = model.locator("canvas");
  const fallback = page.getByTestId("front-projection-fallback");
  const partPicker = page.getByRole("combobox", { name: "Välj möbeldel att inspektera" });
  await expect(model).toBeVisible();
  await expect(canvas).toBeVisible();
  await expect(fallback).toHaveCount(0);
  await expect.poll(async () => canvas.evaluate((element) => {
    const renderCanvas = element as HTMLCanvasElement;
    return renderCanvas.width * renderCanvas.height;
  })).toBeGreaterThan(0);
  await canvas.evaluate((element, identity) => {
    element.setAttribute("data-full-ceiling-canvas", identity);
  }, CANVAS_IDENTITY);
  await installRendererCommitObserver(canvas);

  const navigationProbe = await readProbe(page);
  let latestRendererCommit = await waitForRendererCommit(canvas, 0);
  rendererCommits.push(latestRendererCommit);
  const coldWebGlReadyMs = latestRendererCommit.observed_at_ms - navigationProbe.navigation_started_at_ms;

  await expect(partPicker.locator("option")).toHaveCount(
    budget.fixture.expected_rendered_part_count + 1,
  );
  await expect(page.getByText(`${budget.fixture.expected_rendered_part_count} delar`, { exact: true })).toBeVisible();
  const coldImage = await canvas.screenshot({ animations: "disabled", scale: "css" });
  const webgl = await inspectWebGl(canvas);
  const canvasMetrics = await canvas.evaluate((element) => {
    const renderCanvas = element as HTMLCanvasElement;
    const bounds = renderCanvas.getBoundingClientRect();
    return {
      css_height: Number(bounds.height.toFixed(2)),
      css_width: Number(bounds.width.toFixed(2)),
      pixel_height: renderCanvas.height,
      pixel_width: renderCanvas.width,
    };
  });

  const toolbar = page.getByRole("toolbar", { name: "Visningsverktyg" });
  const perspective = toolbar.getByRole("button", { name: "3D", exact: true });
  const front = toolbar.getByRole("button", { name: "Front", exact: true });
  const transparent = toolbar.getByRole("button", { name: "Transparent", exact: true });
  const partEditor = page.getByRole("region", { name: /Redigera fysisk del/ });

  const rememberRendererCommit = (probe: RendererCommitProbe): RendererCommitProbe => {
    latestRendererCommit = probe;
    rendererCommits.push(probe);
    return probe;
  };

  // Complete one untimed pass so shader compilation and lazy interaction paths
  // cannot be mistaken for steady-state interaction latency.
  let beforeCommit = latestRendererCommit.render_commit;
  await partPicker.selectOption("side-left", { timeout: DIAGNOSTIC_ACTION_TIMEOUT_MS });
  await expect(partEditor).toBeVisible();
  rememberRendererCommit(await waitForRendererCommit(
    canvas,
    beforeCommit,
    rendererCommits[0]!.model_root,
  ));
  beforeCommit = latestRendererCommit.render_commit;
  await partPicker.selectOption("", { timeout: DIAGNOSTIC_ACTION_TIMEOUT_MS });
  await expect(partEditor).toHaveCount(0);
  rememberRendererCommit(await waitForRendererCommit(
    canvas,
    beforeCommit,
    rendererCommits[0]!.model_root,
  ));
  beforeCommit = latestRendererCommit.render_commit;
  await front.click({ timeout: DIAGNOSTIC_ACTION_TIMEOUT_MS });
  await expect(front).toHaveAttribute("aria-pressed", "true");
  rememberRendererCommit(await waitForRendererCommit(
    canvas,
    beforeCommit,
    rendererCommits[0]!.model_root,
  ));
  beforeCommit = latestRendererCommit.render_commit;
  await perspective.click({ timeout: DIAGNOSTIC_ACTION_TIMEOUT_MS });
  await expect(perspective).toHaveAttribute("aria-pressed", "true");
  rememberRendererCommit(await waitForRendererCommit(
    canvas,
    beforeCommit,
    rendererCommits[0]!.model_root,
  ));
  beforeCommit = latestRendererCommit.render_commit;
  await transparent.click({ timeout: DIAGNOSTIC_ACTION_TIMEOUT_MS });
  await expect(transparent).toHaveAttribute("aria-pressed", "true");
  rememberRendererCommit(await waitForRendererCommit(
    canvas,
    beforeCommit,
    rendererCommits[0]!.model_root,
  ));
  const transparentImage = await canvas.screenshot({ animations: "disabled", scale: "css" });
  beforeCommit = latestRendererCommit.render_commit;
  await transparent.click({ timeout: DIAGNOSTIC_ACTION_TIMEOUT_MS });
  await expect(transparent).toHaveAttribute("aria-pressed", "false");
  rememberRendererCommit(await waitForRendererCommit(
    canvas,
    beforeCommit,
    rendererCommits[0]!.model_root,
  ));
  await waitForFrames(page, 2);
  const transparentVariation = await imageVariation(page, transparentImage);

  const orbitRenderSamples: number[] = [];
  const dragStart = await orbitDragStart(canvas);
  await expect(partPicker).toHaveValue("");
  await page.mouse.move(dragStart.x, dragStart.y);
  try {
    await page.mouse.down({ button: "left" });
    await expect(partPicker).toHaveValue("");
    await page.evaluate(() => Promise.resolve());
    const afterPointerDown = await readRendererCommit(canvas);
    if (!afterPointerDown) throw new Error("The renderer identity disappeared when OrbitControls started.");
    if (
      afterPointerDown.canvas_identity !== CANVAS_IDENTITY
      || afterPointerDown.model_root !== rendererCommits[0]!.model_root
    ) {
      throw new Error("The canvas or model root changed when OrbitControls started.");
    }
    if (afterPointerDown.render_commit > latestRendererCommit.render_commit) {
      rememberRendererCommit(afterPointerDown);
    }
    await resetRendererCommitObservations(page);
    const sequenceStartedAt = await page.evaluate(() => performance.now());
    for (let index = 0; index < budget.sampling.warm_frame_count; index += 1) {
      const cycle = Math.floor(index / 24);
      const positionInCycle = index % 24;
      const alongMargin = cycle % 2 === 0 ? positionInCycle + 1 : 24 - positionInCycle;
      const acrossMargin = 4 + cycle % 5;
      const x = dragStart.x + dragStart.inward_x * alongMargin;
      const y = dragStart.y + dragStart.inward_y * acrossMargin;
      const previousRevision = latestRendererCommit.render_commit;
      const startedAt = await page.evaluate(() => performance.now());
      await page.mouse.move(x, y, { steps: 1 });
      const committed = await waitForObservedRendererCommit(
        canvas,
        previousRevision,
        startedAt,
        rendererCommits[0]!.model_root,
      );
      const duration = committed.observed_at_ms - startedAt;
      if (!Number.isFinite(duration) || duration < 0) {
        throw new Error(`OrbitControls sample ${index + 1} produced an invalid commit duration.`);
      }
      orbitRenderSamples.push(duration);
      rememberRendererCommit(committed);
      if (committed.observed_at_ms - sequenceStartedAt > budget.sampling.frame_collection_timeout_ms) {
        const failureReason = (
          `Only ${index + 1}/${budget.sampling.warm_frame_count} OrbitControls commits arrived within `
          + `${budget.sampling.frame_collection_timeout_ms} ms.`
        );
        const failureCheckpoint = {
          schema_version: budget.schema_version,
          complete: false,
          recorded_at: new Date().toISOString(),
          phase: "cold_and_orbit_render",
          failure_reason: failureReason,
          fixture: budget.fixture,
          measurements: {
            clock: "window.performance.now",
            warm_frame_measurement: "orbit_controls_pointer_move_to_observed_post-render_commit",
            cold_webgl_ready_ms: Number(coldWebGlReadyMs.toFixed(2)),
            expected_orbit_samples: budget.sampling.warm_frame_count,
            completed_orbit_samples: orbitRenderSamples.length,
            orbit_render_samples_ms: orbitRenderSamples.map((sample) => Number(sample.toFixed(2))),
          },
          renderer: {
            initial: rendererCommits[0],
            latest: latestRendererCommit,
            observed_commits: rendererCommits,
            model_root_preserved: rendererCommits.every(
              (probe) => probe.model_root === rendererCommits[0]?.model_root,
            ),
          },
          budgets: budget.budgets,
          scope: budget.scope,
        };
        await writeFile(
          testInfo.outputPath("full-ceiling-webgl-performance.partial.json"),
          `${JSON.stringify(failureCheckpoint, null, 2)}\n`,
          "utf8",
        );
        throw new Error(failureReason);
      }
    }
  } finally {
    await page.mouse.up({ button: "left" });
  }
  await expect(partPicker).toHaveValue("");
  expect(orbitRenderSamples).toHaveLength(budget.sampling.warm_frame_count);
  const frameIntervals = distribution(orbitRenderSamples);
  await resetWarmLongTasks(page);
  const writePartialEvidence = async (
    phase: "cold_and_orbit_render" | "selection" | "view_switch" | "transparency",
    completedSample?: number,
  ): Promise<void> => {
    const checkpoint = {
      schema_version: budget.schema_version,
      complete: false,
      recorded_at: new Date().toISOString(),
      phase,
      ...(completedSample === undefined ? {} : {
        completed_sample: completedSample,
        sample_count: budget.sampling.interaction_sample_count,
      }),
      fixture: budget.fixture,
      measurements: {
        clock: "window.performance.now",
        warm_frame_measurement: "orbit_controls_pointer_move_to_observed_post-render_commit",
        cold_webgl_ready_ms: Number(coldWebGlReadyMs.toFixed(2)),
        warm_frame_interval: frameIntervals,
        selection_samples_ms: selectionSamples.map((sample) => Number(sample.toFixed(2))),
        view_switch_samples_ms: viewSwitchSamples.map((sample) => Number(sample.toFixed(2))),
        transparency_toggle_samples_ms: transparencySamples.map((sample) => Number(sample.toFixed(2))),
      },
      renderer: {
        initial: rendererCommits[0],
        latest: latestRendererCommit,
        model_root_preserved: rendererCommits.every(
          (probe) => probe.model_root === rendererCommits[0]?.model_root,
        ),
      },
      budgets: budget.budgets,
      scope: budget.scope,
    };
    await writeFile(
      testInfo.outputPath("full-ceiling-webgl-performance.partial.json"),
      `${JSON.stringify(checkpoint, null, 2)}\n`,
      "utf8",
    );
  };
  await writePartialEvidence("cold_and_orbit_render");

  for (let index = 0; index < budget.sampling.interaction_sample_count; index += 1) {
    const measured = await test.step(
      `selection sample ${index + 1}/${budget.sampling.interaction_sample_count}`,
      async () => {
        const revision = latestRendererCommit!.render_commit;
        return measureAction(
          page,
          () => partPicker.selectOption("side-left", { timeout: DIAGNOSTIC_ACTION_TIMEOUT_MS }),
          async () => {
            await expect(partEditor).toBeVisible();
            return waitForRendererCommit(canvas, revision, rendererCommits[0]!.model_root);
          },
        );
      },
    );
    selectionSamples.push(measured.duration_ms);
    rememberRendererCommit(measured.committed);
    const resetRevision = latestRendererCommit.render_commit;
    await partPicker.selectOption("", { timeout: DIAGNOSTIC_ACTION_TIMEOUT_MS });
    await expect(partEditor).toHaveCount(0);
    rememberRendererCommit(await waitForRendererCommit(
      canvas,
      resetRevision,
      rendererCommits[0]!.model_root,
    ));
  }
  await writePartialEvidence("selection");

  let targetIsFront = true;
  for (let index = 0; index < budget.sampling.interaction_sample_count; index += 1) {
    const target = targetIsFront ? front : perspective;
    const measured = await test.step(
      `view-switch sample ${index + 1}/${budget.sampling.interaction_sample_count}`,
      async () => {
        const revision = latestRendererCommit!.render_commit;
        return measureAction(
          page,
          () => target.click({ timeout: DIAGNOSTIC_ACTION_TIMEOUT_MS }),
          async () => {
            await expect(target).toHaveAttribute("aria-pressed", "true");
            return waitForRendererCommit(canvas, revision, rendererCommits[0]!.model_root);
          },
        );
      },
    );
    viewSwitchSamples.push(measured.duration_ms);
    rememberRendererCommit(measured.committed);
    targetIsFront = !targetIsFront;
  }
  if (await perspective.getAttribute("aria-pressed") !== "true") {
    const resetRevision = latestRendererCommit.render_commit;
    await perspective.click({ timeout: DIAGNOSTIC_ACTION_TIMEOUT_MS });
    await expect(perspective).toHaveAttribute("aria-pressed", "true");
    rememberRendererCommit(await waitForRendererCommit(
      canvas,
      resetRevision,
      rendererCommits[0]!.model_root,
    ));
  }
  await writePartialEvidence("view_switch");

  let targetTransparent = true;
  for (let index = 0; index < budget.sampling.interaction_sample_count; index += 1) {
    const expected = String(targetTransparent);
    const measured = await test.step(
      `transparency sample ${index + 1}/${budget.sampling.interaction_sample_count}`,
      async () => {
        const revision = latestRendererCommit!.render_commit;
        return measureAction(
          page,
          () => transparent.click({ timeout: DIAGNOSTIC_ACTION_TIMEOUT_MS }),
          async () => {
            await expect(transparent).toHaveAttribute("aria-pressed", expected);
            return waitForRendererCommit(canvas, revision, rendererCommits[0]!.model_root);
          },
        );
      },
    );
    transparencySamples.push(measured.duration_ms);
    rememberRendererCommit(measured.committed);
    await writePartialEvidence("transparency", index + 1);
    targetTransparent = !targetTransparent;
  }
  if (await transparent.getAttribute("aria-pressed") !== "false") {
    const resetRevision = latestRendererCommit.render_commit;
    await transparent.click({ timeout: DIAGNOSTIC_ACTION_TIMEOUT_MS });
    await expect(transparent).toHaveAttribute("aria-pressed", "false");
    rememberRendererCommit(await waitForRendererCommit(
      canvas,
      resetRevision,
      rendererCommits[0]!.model_root,
    ));
  }

  const perspectiveImage = await canvas.screenshot({ animations: "disabled", scale: "css" });
  beforeCommit = latestRendererCommit.render_commit;
  await front.click({ timeout: DIAGNOSTIC_ACTION_TIMEOUT_MS });
  await expect(front).toHaveAttribute("aria-pressed", "true");
  rememberRendererCommit(await waitForRendererCommit(
    canvas,
    beforeCommit,
    rendererCommits[0]!.model_root,
  ));
  const frontImage = await canvas.screenshot({ animations: "disabled", scale: "css" });
  beforeCommit = latestRendererCommit.render_commit;
  await perspective.click({ timeout: DIAGNOSTIC_ACTION_TIMEOUT_MS });
  await expect(perspective).toHaveAttribute("aria-pressed", "true");
  rememberRendererCommit(await waitForRendererCommit(
    canvas,
    beforeCommit,
    rendererCommits[0]!.model_root,
  ));
  await expect(canvas).toHaveAttribute("data-full-ceiling-canvas", CANVAS_IDENTITY);
  await expect(canvas).toHaveAttribute("data-custombuild-model-root", rendererCommits[0]!.model_root);

  await page.evaluate(() => new Promise<void>((resolve) => window.setTimeout(resolve, 0)));
  const finalProbe = await readProbe(page);
  const selection = distribution(selectionSamples);
  const viewSwitch = distribution(viewSwitchSamples);
  const transparencyToggle = distribution(transparencySamples);
  const warmLongTasks = finalProbe.long_tasks_ms.length > 0
    ? distribution(finalProbe.long_tasks_ms)
    : undefined;
  const browserMemory = await page.evaluate(() => {
    const memory = (performance as Performance & {
      memory?: { jsHeapSizeLimit: number; totalJSHeapSize: number; usedJSHeapSize: number };
    }).memory;
    return memory ? {
      js_heap_size_limit_bytes: memory.jsHeapSizeLimit,
      total_js_heap_size_bytes: memory.totalJSHeapSize,
      used_js_heap_size_bytes: memory.usedJSHeapSize,
    } : null;
  });

  const evidence = {
    schema_version: budget.schema_version,
    complete: true,
    recorded_at: new Date().toISOString(),
    fixture: {
      ...budget.fixture,
      hydrated_furniture_type: fullCeilingSpec.furniture_type,
      rendered_part_count: await partPicker.locator("option").count() - 1,
    },
    runtime: {
      browser_project: testInfo.project.name,
      browser_version: page.context().browser()?.version() ?? null,
      user_agent: await page.evaluate(() => navigator.userAgent),
      device_pixel_ratio: await page.evaluate(() => window.devicePixelRatio),
      viewport: page.viewportSize(),
      canvas: canvasMetrics,
      webgl,
      memory: browserMemory,
      renderer: {
        initial_commit: rendererCommits[0],
        final_commit: latestRendererCommit,
        observed_commits: rendererCommits,
        model_root_preserved: rendererCommits.every(
          (probe) => probe.model_root === rendererCommits[0]?.model_root,
        ),
      },
    },
    measurements: {
      clock: "window.performance.now",
      warm_frame_measurement: "orbit_controls_pointer_move_to_observed_post-render_commit",
      cold_webgl_ready_ms: Number(coldWebGlReadyMs.toFixed(2)),
      warm_frame_interval: frameIntervals,
      selection,
      view_switch: viewSwitch,
      transparency_toggle: transparencyToggle,
      warm_long_tasks: warmLongTasks ?? {
        median_ms: 0,
        p95_ms: 0,
        max_ms: 0,
        samples_ms: [],
      },
    },
    rendering_evidence: {
      cold_png_bytes: coldImage.byteLength,
      transparent_png_bytes: transparentImage.byteLength,
      perspective_png_bytes: perspectiveImage.byteLength,
      front_png_bytes: frontImage.byteLength,
      transparent_sha256: imageHash(transparentImage),
      transparent_variation: transparentVariation,
      perspective_sha256: imageHash(perspectiveImage),
      front_sha256: imageHash(frontImage),
      canvas_identity_preserved: await canvas.getAttribute("data-full-ceiling-canvas") === CANVAS_IDENTITY,
    },
    health: {
      webgl_context_losses: finalProbe.webgl_context_losses,
      page_errors: pageErrors,
      failed_requests: failedRequests,
    },
    sampling: budget.sampling,
    budgets: budget.budgets,
    scope: budget.scope,
  };
  const evidenceBody = `${JSON.stringify(evidence, null, 2)}\n`;
  await writeFile(testInfo.outputPath("full-ceiling-webgl-performance.json"), evidenceBody, "utf8");
  await testInfo.attach("full-ceiling-webgl-performance.json", {
    body: evidenceBody,
    contentType: "application/json",
  });

  expect(webgl).not.toBeNull();
  expect(webgl?.is_context_lost).toBe(false);
  expect(coldImage.byteLength).toBeGreaterThan(10 * 1_024);
  expect(transparentImage.byteLength).toBeGreaterThan(10 * 1_024);
  expect(transparentVariation.color_bucket_count).toBeGreaterThan(4);
  expect(transparentVariation.luminance_range).toBeGreaterThan(10);
  expect(perspectiveImage.byteLength).toBeGreaterThan(10 * 1_024);
  expect(imageHash(transparentImage)).not.toBe(imageHash(perspectiveImage));
  expect(frontImage.byteLength).toBeGreaterThan(10 * 1_024);
  expect(imageHash(frontImage)).not.toBe(imageHash(perspectiveImage));
  expect(rendererCommits.every(
    (probe) => probe.model_root === rendererCommits[0]?.model_root,
  )).toBe(true);
  expect(latestRendererCommit.render_commit).toBeGreaterThan(rendererCommits[0]!.render_commit);
  expect(finalProbe.webgl_context_losses).toBe(0);
  expect(pageErrors).toEqual([]);
  expect(failedRequests).toEqual([]);
  expect(coldWebGlReadyMs).toBeLessThanOrEqual(budget.budgets.cold_webgl_ready_ms);
  expect(frameIntervals.p95_ms).toBeLessThanOrEqual(budget.budgets.warm_frame_interval_p95_ms);
  expect(frameIntervals.max_ms).toBeLessThanOrEqual(budget.budgets.warm_frame_interval_max_ms);
  expect(selection.p95_ms).toBeLessThanOrEqual(budget.budgets.selection_p95_ms);
  expect(viewSwitch.p95_ms).toBeLessThanOrEqual(budget.budgets.view_switch_p95_ms);
  expect(transparencyToggle.p95_ms).toBeLessThanOrEqual(budget.budgets.transparency_toggle_p95_ms);
  expect(warmLongTasks?.max_ms ?? 0).toBeLessThanOrEqual(budget.budgets.warm_long_task_max_ms);
});

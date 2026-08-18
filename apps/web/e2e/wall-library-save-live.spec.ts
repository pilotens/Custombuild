import { expect, test, type Locator, type Request } from "@playwright/test";
import {
  provisionLiveProject,
  selectProjectBeforeNavigation,
  waitForSuccessfulProjectDraftSave,
} from "./live-helpers";
import { chooseTemplateAndCreate } from "./planning-helpers";

test.skip(
  process.env.PLAYWRIGHT_REAL_API !== "1",
  "Requires the complete Compose API and an authenticated server workspace.",
);
test.skip(
  ({ browserName }) => browserName !== "chromium",
  "The state-mutating real-API regression runs once in Chromium.",
);

const PLAIN_DADO_RULE = {
  ruleId: "CB-JOINT-001",
  ruleVersion: "1.3.0",
  title: "Lokalt upplag i hyllspår och hyllbärare",
  actionName: "Gå till Indelning och konstruktionsstöd",
} as const;

const SERVER_WARNING_PATHS = [
  PLAIN_DADO_RULE,
  {
    ruleId: "CB-TIP-001",
    ruleVersion: "1.3.0",
    title: "Tipprisk och krav på väggförankring",
    actionName: "Extern kontroll: öppna underlaget",
  },
  {
    ruleId: "CB-HARDWARE-001",
    ruleVersion: "1.3.0",
    title: "Beslag och borrbild för underskåp",
    actionName: "Extern kontroll: öppna underlaget",
  },
  {
    ruleId: "DFM-GRAIN-001",
    ruleVersion: "1.0.0",
    title: "Fiberriktning för skivmaterial",
    actionName: null,
  },
] as const;

const WARNING_PATHS = SERVER_WARNING_PATHS;

const STOCKLESS_BLOCK_PATHS = [
  { ruleId: "DFM-MACHINE-001", title: "Maskinens arbetsområde" },
  { ruleId: "DFM-STOCK-001", title: "Delar ryms i råmaterial" },
] as const;

const STOCKLESS_FORBIDDEN_ARTIFACT_KIND = /(?:stock|nest|placement|label_index|measurement_plan|operation|tool|setup|backplot|machine|ngc)/i;

function stocklessArtifactKindIsForbidden(kind: string): boolean {
  return kind !== "stock_selection" && STOCKLESS_FORBIDDEN_ARTIFACT_KIND.test(kind);
}

function requestPath(request: Request): string {
  return new URL(request.url()).pathname;
}

function isVersionCreate(request: Request): boolean {
  return request.method() === "POST"
    && /^\/v1\/projects\/[^/]+\/versions$/.test(requestPath(request));
}

function isVersionValidation(request: Request): boolean {
  return request.method() === "POST"
    && /^\/v1\/projects\/[^/]+\/versions\/\d+\/validate$/.test(requestPath(request));
}

function isVersionGeneration(request: Request): boolean {
  return request.method() === "POST"
    && /^\/v1\/projects\/[^/]+\/versions\/\d+\/generate$/.test(requestPath(request));
}

async function expectStocklessReviewControls(panel: Locator) {
  const blocks = panel.locator(".validation-card.validation-block");
  await expect(blocks).toHaveCount(STOCKLESS_BLOCK_PATHS.length, { timeout: 30_000 });
  for (const blocker of STOCKLESS_BLOCK_PATHS) {
    const card = blocks.filter({ hasText: `Regel ${blocker.ruleId} · version 1.0.0` });
    await expect(card).toHaveCount(1);
    await expect(card.getByText(blocker.title, { exact: true })).toBeVisible();
    await expect(card.getByRole("button", { name: /Åtgärda problem/ })).toBeVisible();
    await expect(card).toContainText("Lagerobundet granskningspaket");
  }
  const warnings = panel.locator(".validation-card.validation-warning");
  await expect(warnings).toHaveCount(WARNING_PATHS.length, { timeout: 30_000 });
  await expect(panel.locator(".validation-summary")).toContainText(`${WARNING_PATHS.length} behöver beslut`);
  await expect(panel.locator(".validation-summary")).toContainText("2 måste lösas");

  for (const warningPath of WARNING_PATHS) {
    const ruleIdentity = `Regel ${warningPath.ruleId} · version ${warningPath.ruleVersion}`;
    const warning = warnings.filter({ hasText: ruleIdentity });
    await expect(warning).toHaveCount(1);
    await expect(warning.getByText(warningPath.title, { exact: true })).toBeVisible();
    await expect(warning).toContainText("Rekommenderad lösning");
    await expect(warning).toContainText("Värde eller underlag:");
    if (warningPath.actionName === null) {
      await expect(warning.getByRole("button")).toHaveCount(0);
    } else {
      await expect(warning.getByRole("button", { name: warningPath.actionName, exact: true })).toBeVisible();
    }
  }
}

test("4200 × 2400-väggbiblioteket ger ett lagerobundet reviewpaket utan storformatsmutation", async ({
  page,
  request,
}, testInfo) => {
  test.setTimeout(8 * 60_000);
  await page.setViewportSize({ width: 1_280, height: 1_100 });
  const project = await provisionLiveProject(request, testInfo, "wall-save");
  await selectProjectBeforeNavigation(page, project);
  const apiUrl = process.env.PLAYWRIGHT_API_URL?.replace(/\/$/, "");
  expect(apiUrl).toBeTruthy();
  const authHeaders = {
    Authorization: `Bearer ${process.env.PLAYWRIGHT_DEMO_TOKEN?.trim() || "demo-nordic-owner"}`,
  };

  const pageErrors: string[] = [];
  const consoleErrors: string[] = [];
  const failedRequests: string[] = [];
  const failedApiResponses: string[] = [];
  const versionCreates: Request[] = [];
  const validations: Request[] = [];
  const generations: Request[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("request", (request) => {
    if (isVersionCreate(request)) versionCreates.push(request);
    if (isVersionValidation(request)) validations.push(request);
    if (isVersionGeneration(request)) generations.push(request);
  });
  page.on("requestfailed", (request) => {
    failedRequests.push(`${request.method()} ${request.url()}: ${request.failure()?.errorText}`);
  });
  page.on("response", (response) => {
    const path = new URL(response.url()).pathname;
    if (path.startsWith("/v1/") && response.status() >= 400) {
      failedApiResponses.push(`${response.request().method()} ${path}: ${response.status()}`);
    }
  });

  // Keep the real validate call in flight long enough to prove that the user gets
  // immediate progress feedback. The request still reaches the real Compose API.
  await page.route("**/v1/projects/*/versions/*/validate", async (route) => {
    if (route.request().method() === "POST") {
      await new Promise((resolve) => setTimeout(resolve, 450));
    }
    await route.continue();
  });

  const response = await page.goto("/", { waitUntil: "domcontentloaded" });
  expect(response?.status()).toBe(200);

  const templateHeading = page.getByRole("heading", { name: "Vad vill du skapa?" });
  const serverSaved = page.getByText("Sparad på servern", { exact: true });
  await expect(templateHeading).toBeVisible({ timeout: 30_000 });

  const initialDraftSave = waitForSuccessfulProjectDraftSave(page, project.project.id, {
    furniture_type: "wall_library",
    width_mm: 4_200,
    height_mm: 2_400,
    depth_mm: 320,
  });
  await chooseTemplateAndCreate(page, "Väggbibliotek", {
    widthMm: 4_200,
    heightMm: 2_400,
    depthMm: 320,
  });
  await initialDraftSave;
  await expect(serverSaved).toBeVisible({ timeout: 30_000 });

  const width = page.getByRole("spinbutton", { name: "Bredd", exact: true });
  const height = page.getByRole("spinbutton", { name: "Höjd", exact: true });
  await expect(width).toHaveValue("4200");
  await expect(height).toHaveValue("2400");

  const modes = page.getByRole("navigation", { name: "Produktlägen" });
  await modes.getByRole("button", { name: /Kontroll/ }).click();
  const panel = page.locator(".validation-panel");
  await expectStocklessReviewControls(panel);

  const stocklessAction = panel.locator(".validation-card.validation-block")
    .filter({ hasText: "Regel DFM-STOCK-001 · version 1.0.0" })
    .getByRole("button", { name: /Åtgärda problem/ });
  await stocklessAction.click();
  let drawer = page.locator("section.production-drawer-embedded");
  await expect(drawer).toBeVisible();
  await expect(drawer.locator("#production-next-action-heading")).not.toHaveText("Hämtar projektet", { timeout: 30_000 });
  await expect(drawer.getByText("Blockerar CAM · 2 krav")).toBeVisible();
  await expect(drawer).not.toContainText("5 000 × 2 500");
  await expect(page.getByRole("spinbutton", { name: "Bredd", exact: true })).toHaveValue("4200");
  await expect(page.getByRole("spinbutton", { name: "Höjd", exact: true })).toHaveValue("2400");

  const buildabilityScreenshot = await drawer.screenshot({
    path: testInfo.outputPath("01-actionable-buildability-warnings.png"),
  });
  await testInfo.attach("01-actionable-buildability-warnings.png", {
    body: buildabilityScreenshot,
    contentType: "image/png",
  });

  let save = drawer.getByRole("button", {
    name: "Spara för lagerobunden granskning",
    exact: true,
  });

  if (await save.count() === 0) {
    // A repeated local run can find this exact design already reviewed. Keep the
    // required 4200 × 2400 outer size and alter only the safe cabinet height.
    await modes.getByRole("button", { name: /Studio/ }).click();
    const shelfOutput = page.getByLabel("Hyllnivåer");
    const currentCount = Number(await shelfOutput.textContent());
    const nextCount = currentCount >= 30 ? currentCount - 1 : currentCount + 1;
    const changedPreview = page.waitForResponse((candidate) => {
      if (
        candidate.request().method() !== "POST"
        || requestPath(candidate.request()) !== "/v1/designs/autofix"
        || !candidate.ok()
      ) return false;
      const payload = candidate.request().postDataJSON() as Record<string, unknown>;
      return payload.width_mm === 4_200
        && payload.height_mm === 2_400
        && payload.shelf_count === nextCount;
    });
    await page.getByRole("button", { name: currentCount >= 30 ? "Minska hyllnivåer" : "Öka hyllnivåer" }).click();
    await changedPreview;
    await modes.getByRole("button", { name: /Kontroll/ }).click();
    await expectStocklessReviewControls(panel);
    await modes.getByRole("button", { name: /Underlag/ }).click();
    drawer = page.locator("section.production-drawer-embedded");
    await expect(drawer.locator("#production-next-action-heading")).not.toHaveText("Hämtar projektet", { timeout: 30_000 });
    save = drawer.getByRole("button", {
      name: "Spara för lagerobunden granskning",
      exact: true,
    });
  }

  await expect(save).toBeVisible({ timeout: 30_000 });
  await expect(save).toBeEnabled();
  // aria-describedby is present while the rendered design is still a stale local
  // hash. Its disappearance proves the autofix/spec hash loop has converged.
  await expect(save).not.toHaveAttribute("aria-describedby", /.+/, { timeout: 30_000 });

  const createResponsePromise = page.waitForResponse((candidate) => (
    isVersionCreate(candidate.request())
  ));
  const validationResponsePromise = page.waitForResponse((candidate) => (
    isVersionValidation(candidate.request())
  ));
  await save.click();

  await expect(save).toHaveAttribute("aria-busy", "true");
  await expect(drawer.locator(".production-action-guidance")).toBeVisible();

  const [createResponse, validationResponse] = await Promise.all([
    createResponsePromise,
    validationResponsePromise,
  ]);
  expect(createResponse.ok(), `Version POST returned HTTP ${createResponse.status()}`).toBe(true);
  expect(validationResponse.ok(), `Validation POST returned HTTP ${validationResponse.status()}`).toBe(true);

  const createPayload = createResponse.request().postDataJSON() as {
    spec: Record<string, unknown>;
    production_context: Record<string, unknown>;
    expected_design_hash: string;
    expected_current_revision: number;
  };
  expect(createPayload.spec).toMatchObject({
    furniture_type: "wall_library",
    width_mm: 4_200,
    height_mm: 2_400,
  });
  expect(createPayload.production_context).toMatchObject({
    stock_width_mm: 2_440,
    stock_height_mm: 1_220,
    stock_count: 16,
    back_stock_width_mm: 2_440,
    back_stock_height_mm: 1_220,
    back_stock_count: 6,
    machine_profile_id: "custombuild-router-1325-linuxcnc",
  });
  expect(createPayload.expected_design_hash).toMatch(/^[a-f0-9]{64}$/);
  expect(Number.isInteger(createPayload.expected_current_revision)).toBe(true);
  expect(createPayload.expected_current_revision).toBeGreaterThanOrEqual(0);

  const created = await createResponse.json() as { revision: number; status: string };
  const validated = await validationResponse.json() as { revision: number; status: string };
  expect(validated.revision).toBe(created.revision);
  expect(requestPath(validationResponse.request())).toBe(
    `${requestPath(createResponse.request())}/${created.revision}/validate`,
  );
  expect(validated.status).toBe("design_validated");
  const nextAction = drawer.locator("#production-next-action-heading");
  await expect(nextAction).toBeVisible();
  await expect(nextAction).toHaveText("Skapa underlag");
  await expect(nextAction).toBeFocused();
  await expect(drawer.getByRole("region", { name: "Status för verifieringen" })).toContainText(
    "Lagerprofil saknas · CAM blockeras",
  );
  await expect(drawer.getByText(/Parametrar eller produktionsval har ändrats/)).toHaveCount(0);
  await expect(drawer.getByRole("button", {
    name: "Spara för lagerobunden granskning",
    exact: true,
  })).toHaveCount(0);

  const warningList = drawer.getByRole("region", { name: "Varningar att kontrollera" });
  const warningItems = warningList.getByRole("listitem");
  await expect(warningList).toBeVisible();
  await expect(warningItems).toHaveCount(WARNING_PATHS.length);
  for (const warningPath of WARNING_PATHS) {
    const warningItem = warningItems.filter({ hasText: warningPath.title });
    await expect(warningItem).toHaveCount(1);
    await expect(warningItem.getByText(warningPath.title, { exact: true })).toBeVisible();
    await expect(warningItem.locator("small")).toHaveText(/\S{12,}/);
  }
  const warningConfirmation = drawer.getByRole("checkbox", {
    name: "Jag har läst och kontrollerat varningarna ovan.",
  });
  await expect(warningConfirmation).toBeVisible();
  await expect(warningConfirmation).not.toBeChecked();
  await expect(drawer.getByText("Kontrollbevis", { exact: true })).toHaveCount(0);
  await expect(drawer.getByLabel("Katalog-ID", { exact: true })).toHaveCount(0);
  await expect(drawer.locator('input[type="file"]')).toHaveCount(0);

  const validatedScreenshot = await drawer.screenshot({
    path: testInfo.outputPath("02-validated-on-step-two.png"),
  });
  await testInfo.attach("02-validated-on-step-two.png", {
    body: validatedScreenshot,
    contentType: "image/png",
  });

  await warningConfirmation.check();
  const generationResponsePromise = page.waitForResponse((candidate) => (
    isVersionGeneration(candidate.request())
  ));
  const createPackage = drawer.getByRole("button", { name: "Skapa underlag", exact: true });
  await expect(createPackage).toBeEnabled();
  await createPackage.click();
  const generationResponse = await generationResponsePromise;
  expect(
    generationResponse.ok(),
    `Generation POST returned HTTP ${generationResponse.status()}`,
  ).toBe(true);
  const generationPayload = generationResponse.request().postDataJSON() as Record<string, unknown>;
  expect(generationPayload).toMatchObject({
    stock_width_mm: 2_440,
    stock_height_mm: 1_220,
    stock_count: 16,
    back_stock_width_mm: 2_440,
    back_stock_height_mm: 1_220,
    back_stock_count: 6,
    machine_profile_id: "custombuild-router-1325-linuxcnc",
    include_step: true,
    include_validation_program: true,
  });
  const queuedJob = await generationResponse.json() as { id?: unknown };
  expect(typeof queuedJob.id).toBe("string");

  await expect(drawer.getByText("Granskningspaketet är klart", { exact: true })).toBeVisible({
    timeout: 5 * 60_000,
  });
  const camStatus = drawer.getByRole("status", { name: "Status för CAM" });
  await expect(camStatus).toContainText("En exakt serverbunden lagerprofil saknas");
  await expect(camStatus).toContainText("Lagerinköp, nesting, operationer");
  await expect(drawer.getByRole("button", {
    name: "Ladda ned granskningspaket (.zip)",
  })).toBeVisible();
  await expect(drawer).not.toContainText("Custombuild Router 5125");

  const completedJobResponse = await request.get(
    `${apiUrl}/v1/jobs/${encodeURIComponent(queuedJob.id as string)}`,
    { headers: authHeaders },
  );
  expect(completedJobResponse.ok()).toBe(true);
  const completedJob = await completedJobResponse.json() as {
    status?: unknown;
    result_json?: Record<string, unknown> | null;
  };
  expect(completedJob.status).toBe("succeeded");
  expect(completedJob.result_json).toMatchObject({
    authoritative_geometry: true,
    dfm_status: "BLOCK",
    nesting_utilization_ppm: null,
    used_sheet_count: 0,
    nesting_layouts: [],
    machine_program_mode: "CAM_BLOCKED",
    production_machine_program: false,
    design_review_package_status: {
      cam_status: "BLOCKED",
      blocker_codes: ["STOCK_PROFILE_MISSING"],
      physical_cutting_authorized: false,
    },
    workshop_readiness: {
      design_review_ready: false,
      physical_cutting_authorized: false,
    },
  });
  const readiness = completedJob.result_json?.workshop_readiness as {
    software_evidence?: Array<{ code?: unknown; status?: unknown }>;
  } | undefined;
  const softwareStatus = new Map(
    (readiness?.software_evidence ?? []).map((item) => [item.code, item.status]),
  );
  expect(softwareStatus.get("AUTHORITATIVE_CAD")).toBe("VERIFIED");
  expect(softwareStatus.get("DFM_SCREEN")).toBe("MISSING");
  expect(softwareStatus.get("SEMANTIC_OPERATIONS")).toBe("MISSING");

  const artifactsResponse = await request.get(
    `${apiUrl}/v1/jobs/${encodeURIComponent(queuedJob.id as string)}/artifacts`,
    { headers: authHeaders },
  );
  expect(artifactsResponse.ok()).toBe(true);
  const artifactPayload = await artifactsResponse.json() as Array<{ kind?: unknown }>;
  const artifactKinds = artifactPayload.map((artifact) => String(artifact.kind));
  expect(artifactKinds).toEqual(expect.arrayContaining([
    "production_bundle",
    "design_review_package_status",
    "dfm_report",
    "stock_selection",
    "generation_plan",
    "workshop_readiness",
  ]));
  expect(artifactKinds.filter(stocklessArtifactKindIsForbidden)).toEqual([]);

  const completedScreenshot = await drawer.screenshot({
    path: testInfo.outputPath("03-stockless-review-package.png"),
  });
  await testInfo.attach("03-stockless-review-package.png", {
    body: completedScreenshot,
    contentType: "image/png",
  });

  // Observe a quiet interval so an effect regression cannot silently create a
  // second revision or re-run validation after the successful transition.
  await page.waitForTimeout(900);
  expect(versionCreates).toHaveLength(1);
  expect(validations).toHaveLength(1);
  expect(generations).toHaveLength(1);
  expect(pageErrors).toEqual([]);
  expect(consoleErrors).toEqual([]);
  expect(failedApiResponses).toEqual([]);
  const unexpectedFailedRequests = failedRequests.filter((failure) => !(
    failure.endsWith(": net::ERR_ABORTED")
    && failure.includes("/v1/designs/autofix")
  ));
  expect(unexpectedFailedRequests).toEqual([]);
});

import { expect, test, type Request } from "@playwright/test";
import {
  provisionLiveProject,
  selectProjectBeforeNavigation,
  waitForLiveWorkspaceReady,
  waitForSuccessfulProjectDraftSave,
} from "./live-helpers";
import { chooseTemplateAndCreate } from "./planning-helpers";

const PLAIN_DADO_RULE = {
  ruleId: "CB-JOINT-001",
  ruleVersion: "1.3.0",
  title: "Lokalt upplag i hyllspår och hyllbärare",
} as const;

const SERVER_WARNING_PATHS = [
  PLAIN_DADO_RULE,
  {
    ruleId: "CB-TIP-001",
    ruleVersion: "1.3.0",
    title: "Tipprisk och krav på väggförankring",
  },
  {
    ruleId: "DFM-GRAIN-001",
    ruleVersion: "1.0.0",
    title: "Fiberriktning för skivmaterial",
  },
] as const;

const REVIEW_WARNING_PATHS = SERVER_WARNING_PATHS;

const SCREENED_STOCK_PASS_PATHS = [
  { ruleId: "DFM-MACHINE-001", ruleVersion: "1.0.0", title: "Maskinens arbetsområde" },
  { ruleId: "DFM-STOCK-001", ruleVersion: "1.0.0", title: "Delar ryms i råmaterial" },
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

test.skip(
  process.env.PLAYWRIGHT_REAL_API !== "1",
  "Requires the complete Compose API, worker, database, queue and object storage.",
);
test.skip(
  ({ browserName }) => browserName !== "chromium",
  "The state-mutating manufacturing acceptance runs once in Chromium; offline UX smoke covers all engines.",
);

test("det verkliga designgranskningsflödet kan skapa och hämta ett granskningspaket", async ({
  page,
  request,
}, testInfo) => {
  test.setTimeout(6 * 60_000);
  await page.setViewportSize({ width: 966, height: 1197 });
  const project = await provisionLiveProject(request, testInfo, "production-flow");
  await selectProjectBeforeNavigation(page, project);
  const apiUrl = process.env.PLAYWRIGHT_API_URL?.replace(/\/$/, "");
  expect(apiUrl).toBeTruthy();
  const authHeaders = {
    Authorization: `Bearer ${process.env.PLAYWRIGHT_DEMO_TOKEN?.trim() || "demo-nordic-owner"}`,
  };
  // Package acceptance exercises the audited server workflow, not GPU
  // throughput. Force the application's supported front-projection fallback so
  // headless SwiftShader cannot starve network and locator progress; the live UI
  // suite validates the interactive WebGL viewer separately.
  await page.addInitScript(() => {
    const nativeGetContext = HTMLCanvasElement.prototype.getContext;
    Object.defineProperty(HTMLCanvasElement.prototype, "getContext", {
      configurable: true,
      value(this: HTMLCanvasElement, contextId: string, ...args: unknown[]) {
        if (contextId === "webgl" || contextId === "webgl2" || contextId === "experimental-webgl") {
          return null;
        }
        return Reflect.apply(nativeGetContext, this, [contextId, ...args]);
      },
    });
  });
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
  page.on("requestfailed", (request) => {
    failedRequests.push(`${request.method()} ${request.url()}: ${request.failure()?.errorText}`);
  });
  page.on("request", (request) => {
    if (isVersionCreate(request)) versionCreates.push(request);
    if (isVersionValidation(request)) validations.push(request);
    if (isVersionGeneration(request)) generations.push(request);
  });
  page.on("response", (response) => {
    const path = new URL(response.url()).pathname;
    if (path.startsWith("/v1/") && response.status() >= 400) {
      failedApiResponses.push(`${response.request().method()} ${path}: ${response.status()}`);
    }
  });

  const response = await page.goto("/", { waitUntil: "domcontentloaded" });
  expect(response?.status()).toBe(200);
  await waitForLiveWorkspaceReady(page, project.project.id);
  await expect(page.getByText("Sparad på servern", { exact: true })).toBeVisible({ timeout: 30_000 });
  const initialPreview = page.waitForResponse((candidate) => {
    if (
      candidate.request().method() !== "POST"
      || new URL(candidate.url()).pathname !== "/v1/designs/autofix"
      || !candidate.ok()
    ) return false;
    const payload = candidate.request().postDataJSON() as Record<string, unknown>;
    return payload.furniture_type === "bookcase"
      && payload.width_mm === 1_800
      && payload.height_mm === 2_100;
  }, { timeout: 30_000 });
  const initialDraftSave = waitForSuccessfulProjectDraftSave(page, project.project.id, {
    furniture_type: "bookcase",
    width_mm: 1_800,
    height_mm: 2_100,
  });
  await chooseTemplateAndCreate(page, "Hyllsystem", {
    widthMm: 1_800,
    heightMm: 2_100,
    depthMm: 320,
  });
  const [initialPreviewResponse] = await Promise.all([initialPreview, initialDraftSave]);
  const initialPreviewPayload = await initialPreviewResponse.json() as {
    rule_evaluations?: Array<{
      rule_id?: unknown;
      rule_version?: unknown;
      status?: unknown;
      title?: unknown;
      inputs?: Array<{ name?: unknown; value?: unknown; unit?: unknown }>;
    }>;
  };
  const jointRule = initialPreviewPayload.rule_evaluations?.find(
    (evaluation) => evaluation.rule_id === PLAIN_DADO_RULE.ruleId,
  );
  expect(jointRule, "Live response must expose plain DADO as the versioned review warning.").toMatchObject({
    rule_id: PLAIN_DADO_RULE.ruleId,
    rule_version: PLAIN_DADO_RULE.ruleVersion,
    status: "WARNING",
    title: PLAIN_DADO_RULE.title,
  });
  const serverWarnings = (initialPreviewPayload.rule_evaluations ?? [])
    .filter((evaluation) => evaluation.status === "WARNING")
    .map((evaluation) => ({
      rule_id: evaluation.rule_id,
      rule_version: evaluation.rule_version,
      title: evaluation.title,
    }));
  expect(serverWarnings).toEqual(SERVER_WARNING_PATHS.map((warning) => ({
    rule_id: warning.ruleId,
    rule_version: warning.ruleVersion,
    title: warning.title,
  })));
  const shelfMount = jointRule?.inputs?.find((input) => input.name === "hylltyp");
  expect(shelfMount, "Nullable RuleDatum units must survive the live JSON contract.").toMatchObject({
    unit: null,
  });
  const bearingArea = jointRule?.inputs?.find((input) => input.name === "bärande_area")?.value;
  expect(typeof bearingArea).toBe("number");
  expect(Number.isSafeInteger(bearingArea as number), "Large rule integers must remain safe integers.").toBe(true);
  expect(bearingArea as number).toBeGreaterThan(1_000_000_000);
  console.log("production-live: initial model and draft ready");
  await expect(page.getByText("Servermodell", { exact: true })).toBeVisible({ timeout: 30_000 });

  // The project identity is unique for every run, so the Explore draft above is
  // already a new revision candidate. Avoid synthetic rapid geometry changes:
  // the acceptance should exercise the same direct path as a user.
  const modes = page.getByRole("navigation", { name: "Produktlägen" });
  await modes.getByRole("button", { name: /Kontroll/ }).click();
  console.log("production-live: construction check open");
  const validationPanel = page.getByRole("region", { name: "Kontrollera konstruktionen" });
  await expect(validationPanel).toBeVisible({
    timeout: 30_000,
  });
  await expect(validationPanel.locator(".validation-card.validation-block")).toHaveCount(0);
  const screenedStockPasses = validationPanel.locator(".validation-card.validation-pass");
  for (const screenedPath of SCREENED_STOCK_PASS_PATHS) {
    const card = screenedStockPasses.filter({
      hasText: `Regel ${screenedPath.ruleId} · version ${screenedPath.ruleVersion}`,
    });
    await expect(card).toHaveCount(1);
    await expect(card.getByText(screenedPath.title, { exact: true })).toBeVisible();
  }
  const reviewWarnings = validationPanel.locator(".validation-card.validation-warning");
  await expect(reviewWarnings).toHaveCount(REVIEW_WARNING_PATHS.length);
  for (const warningPath of REVIEW_WARNING_PATHS) {
    const warning = reviewWarnings.filter({
      hasText: `Regel ${warningPath.ruleId} · version ${warningPath.ruleVersion}`,
    });
    await expect(warning).toHaveCount(1);
    await expect(warning.getByText(warningPath.title, { exact: true })).toBeVisible();
    if (warningPath.ruleId === "DFM-GRAIN-001") {
      await expect(warning.locator(".validation-actions").getByRole("button")).toHaveCount(0);
    }
  }
  await modes.getByRole("button", { name: /Underlag/ }).click();
  const productionDialog = page.locator("section.production-drawer-embedded");
  await expect(productionDialog).toBeVisible();
  await expect(productionDialog.locator("#production-next-action-heading"))
    .not.toHaveText("Hämtar projektet", { timeout: 30_000 });
  await expect(productionDialog.getByText("Blockerar CAM · 2 krav")).toBeVisible();
  await expect(productionDialog).not.toContainText("5 000 × 2 500");
  // Underlag intentionally replaces the editable Studio inspector. Verify the
  // construction summary that remains visible across the mode transition.
  await expect(page.getByTestId("current-design-label").getByText("1800 × 2100 × 320 mm", { exact: true }))
    .toBeVisible();
  console.log("production-live: manufacturing review visible");

  const save = page.getByRole("button", {
    name: "Spara för lagerobunden granskning",
    exact: true,
  });
  await expect(save).toBeVisible({ timeout: 30_000 });
  try {
    await expect(save).toBeEnabled({ timeout: 30_000 });
  } catch (error) {
    // Keep server/model synchronization failures actionable in CI output.
    console.error(await page.locator(".production-workflow").innerText());
    throw error;
  }
  const createResponsePromise = page.waitForResponse((candidate) => (
    isVersionCreate(candidate.request())
  ));
  const validationResponsePromise = page.waitForResponse((candidate) => (
    isVersionValidation(candidate.request())
  ));
  await save.click();
  const [createResponse, validationResponse] = await Promise.all([
    createResponsePromise,
    validationResponsePromise,
  ]);
  expect(createResponse.ok(), `Version POST returned HTTP ${createResponse.status()}`).toBe(true);
  expect(validationResponse.ok(), `Validation POST returned HTTP ${validationResponse.status()}`).toBe(true);
  const createPayload = createResponse.request().postDataJSON() as {
    spec: Record<string, unknown>;
    production_context: Record<string, unknown>;
  };
  expect(createPayload.spec).toMatchObject({
    furniture_type: "bookcase",
    width_mm: 1_800,
    height_mm: 2_100,
  });
  expect(createPayload.production_context).toMatchObject({
    stock_width_mm: 2_440,
    stock_height_mm: 1_220,
    stock_count: 4,
    back_stock_width_mm: 2_440,
    back_stock_height_mm: 1_220,
    back_stock_count: 2,
    machine_profile_id: "custombuild-router-1325-linuxcnc",
  });
  await expect(productionDialog.locator("#production-next-action-heading")).toHaveText("Skapa underlag", { timeout: 30_000 });
  await expect(productionDialog.getByRole("region", { name: "Status för verifieringen" }))
    .toContainText("Lagerprofil saknas · CAM blockeras");

  const versionHistory = productionDialog.getByRole("list", { name: "Serverrevisioner" });
  await expect(versionHistory).toBeVisible({ timeout: 30_000 });
  const currentRevision = versionHistory.locator('li[aria-current="true"]');
  await expect(currentRevision).toContainText("Revision R1");
  await expect(currentRevision).toContainText("Designvaliderad");
  await expect(productionDialog.getByText("Versionshistoriken kunde inte hämtas", { exact: false })).toHaveCount(0);

  const warningList = productionDialog.getByRole("region", { name: "Varningar att kontrollera" });
  await expect(warningList).toBeVisible();
  // Hyllsystemet saknar underskåpsfronter. Dess tre explicita granskningsvägar
  // är torrhållning för DADO, väggförankring och skivmaterialets fiberriktning.
  const warningItems = warningList.getByRole("listitem");
  await expect(warningItems).toHaveCount(REVIEW_WARNING_PATHS.length);
  for (const warningPath of REVIEW_WARNING_PATHS) {
    const warningItem = warningItems.filter({ hasText: warningPath.title });
    await expect(warningItem).toHaveCount(1);
    await expect(warningItem.getByText(warningPath.title, { exact: true })).toBeVisible();
  }
  await expect(productionDialog.getByText("Kontrollbevis", { exact: true })).toHaveCount(0);
  await expect(productionDialog.getByLabel("Katalog-ID", { exact: true })).toHaveCount(0);
  await expect(productionDialog.locator('input[type="file"]')).toHaveCount(0);

  const warningConfirmation = productionDialog.getByRole("checkbox", {
    name: "Jag har läst och kontrollerat varningarna ovan.",
  });
  await expect(warningConfirmation).toBeVisible();
  await expect(warningConfirmation).not.toBeChecked();
  await page.screenshot({ path: testInfo.outputPath("01-simple-review.png"), fullPage: true });
  await warningConfirmation.check();

  const createPackage = productionDialog.getByRole("button", { name: "Skapa underlag", exact: true });
  await expect(createPackage).toBeEnabled();
  const generationResponsePromise = page.waitForResponse((candidate) => (
    isVersionGeneration(candidate.request())
  ));
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
    stock_count: 4,
    back_stock_width_mm: 2_440,
    back_stock_height_mm: 1_220,
    back_stock_count: 2,
    machine_profile_id: "custombuild-router-1325-linuxcnc",
    include_step: true,
    include_validation_program: true,
  });
  const queuedJob = await generationResponse.json() as { id?: unknown };
  expect(typeof queuedJob.id).toBe("string");

  const downloadButton = productionDialog.getByRole("button", {
    name: "Ladda ned granskningspaket (.zip)",
    exact: true,
  });
  await expect(downloadButton).toBeVisible({ timeout: 4 * 60_000 });
  const camStatus = productionDialog.getByRole("status", { name: "Status för CAM" });
  await expect(camStatus).toContainText("strukturerad X/Y-bindning");
  await expect(camStatus).toContainText("Uppladdade dokument och varningsgodkännanden kan inte");
  await expect(camStatus).toContainText("Nesting, operationer, setupblad");

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
    nesting_layouts: [],
    machine_program_mode: "CAM_BLOCKED",
    production_machine_program: false,
    design_review_package_status: {
      cam_status: "BLOCKED",
      blocker_codes: ["DFM-GRAIN-001"],
      physical_cutting_authorized: false,
    },
    workshop_readiness: {
      design_review_ready: false,
      physical_cutting_authorized: false,
    },
  });
  expect(completedJob.result_json?.used_sheet_count).toBe(0);
  expect(completedJob.result_json?.nesting_utilization_ppm).toBeNull();
  const readiness = completedJob.result_json?.workshop_readiness as {
    software_evidence?: Array<{ code?: unknown; status?: unknown }>;
    workshop_evidence?: Array<{ code?: unknown; status?: unknown }>;
  } | undefined;
  const softwareStatus = new Map(
    (readiness?.software_evidence ?? []).map((item) => [item.code, item.status]),
  );
  expect(softwareStatus.get("AUTHORITATIVE_CAD")).toBe("VERIFIED");
  expect(softwareStatus.get("DFM_SCREEN")).toBe("MISSING");
  expect(softwareStatus.get("SEMANTIC_OPERATIONS")).toBe("MISSING");
  const workshopStatus = new Map(
    (readiness?.workshop_evidence ?? []).map((item) => [item.code, item.status]),
  );
  expect(workshopStatus.get("MATERIAL_GRAIN")).toBe("EXTERNAL_EVIDENCE_REQUIRED");

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
  await page.screenshot({ path: testInfo.outputPath("02-underlag-ready.png"), fullPage: true });

  const downloadPromise = page.waitForEvent("download", { timeout: 60_000 });
  await downloadButton.click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(
    /^custombuild-design-review-rev-\d+\.zip$/,
  );
  expect(await download.failure()).toBeNull();
  const stream = await download.createReadStream();
  const firstChunk = await new Promise<Buffer>((resolve, reject) => {
    stream.once("data", (chunk: Buffer) => resolve(chunk));
    stream.once("error", reject);
  });
  expect(firstChunk.subarray(0, 2).toString("ascii")).toBe("PK");

  const previousDraftResponse = await request.get(
    `${apiUrl}/v1/projects/${encodeURIComponent(project.project.id)}/draft`,
    { headers: authHeaders },
  );
  expect(previousDraftResponse.ok()).toBe(true);
  const previousDraft = await previousDraftResponse.json() as Record<string, unknown>;

  const newProductName = `${project.project.name}-new`;
  await page.getByRole("button", { name: "Skapa ny produkt" }).click();
  await page.getByLabel("Nytt projekt").fill(newProductName);
  const newProjectResponsePromise = page.waitForResponse((candidate) => {
    if (
      candidate.request().method() !== "POST"
      || new URL(candidate.url()).pathname !== "/v1/projects"
      || candidate.status() !== 201
    ) return false;
    const payload = candidate.request().postDataJSON() as Record<string, unknown>;
    return payload.name === newProductName;
  }, { timeout: 30_000 });
  const newProjectDraftResponsePromise = page.waitForResponse((candidate) => {
    if (candidate.request().method() !== "GET" || !candidate.ok()) return false;
    const match = /^\/v1\/projects\/([^/]+)\/draft$/.exec(new URL(candidate.url()).pathname);
    return match !== null && decodeURIComponent(match[1]!) !== project.project.id;
  }, { timeout: 30_000 });
  await page.getByRole("button", { name: "Skapa", exact: true }).click();
  const [newProjectResponse, newProjectDraftResponse] = await Promise.all([
    newProjectResponsePromise,
    newProjectDraftResponsePromise,
  ]);
  const newProject = await newProjectResponse.json() as { id?: unknown; name?: unknown };
  expect(newProject).toMatchObject({ name: newProductName });
  expect(typeof newProject.id).toBe("string");
  const newProjectId = newProject.id as string;
  expect(newProjectId).not.toBe(project.project.id);
  expect(newProject.name).not.toBe(project.project.name);

  const draftPathMatch = /^\/v1\/projects\/([^/]+)\/draft$/.exec(
    new URL(newProjectDraftResponse.url()).pathname,
  );
  expect(draftPathMatch).not.toBeNull();
  expect(decodeURIComponent(draftPathMatch![1]!)).toBe(newProjectId);
  const emptyDraft = await newProjectDraftResponse.json() as Record<string, unknown>;
  expect(emptyDraft).toMatchObject({
    project_id: newProjectId,
    draft_revision: 0,
    template_id: null,
    design_hash: null,
    spec_json: null,
    workspace_spec_json: null,
    result_json: null,
  });

  const activeProject = page.getByRole("combobox", { name: "Aktivt projekt" });
  await expect(activeProject).toHaveValue(newProjectId);
  await expect(activeProject.locator("option:checked"))
    .toHaveText(newProductName);
  await expect(page).toHaveURL(/[?&]mode=explore(?:&|$)/);
  expect(new URL(page.url()).searchParams.get("project")).toBe(newProjectId);
  await expect(page.getByRole("heading", { name: "Vad vill du skapa?" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Konstruktionsvy" })).toHaveCount(0);
  for (const mode of [/Studio/, /Kontroll/, /Underlag/]) {
    await expect(modes.getByRole("button", { name: mode })).toBeDisabled();
  }

  const preservedDraftResponse = await request.get(
    `${apiUrl}/v1/projects/${encodeURIComponent(project.project.id)}/draft`,
    { headers: authHeaders },
  );
  expect(preservedDraftResponse.ok()).toBe(true);
  const preservedDraft = await preservedDraftResponse.json() as Record<string, unknown>;
  expect(preservedDraft.design_hash).toEqual(previousDraft.design_hash);
  expect(preservedDraft.spec_json).toEqual(previousDraft.spec_json);
  expect(preservedDraft.workspace_spec_json).toEqual(previousDraft.workspace_spec_json);

  expect(versionCreates).toHaveLength(1);
  expect(validations).toHaveLength(1);
  expect(generations).toHaveLength(1);
  expect(pageErrors).toEqual([]);
  expect(consoleErrors).toEqual([]);
  expect(failedApiResponses).toEqual([]);
  const unexpectedFailedRequests = failedRequests.filter(
    (failure) =>
      !(
        failure.endsWith(": net::ERR_ABORTED")
        && (
          (failure.includes("/custombuild-artifacts/") && failure.includes("/production.zip?"))
          // The editor intentionally cancels a superseded latest-wins preview request.
          || failure.includes("/v1/designs/autofix")
        )
      ),
  );
  expect(unexpectedFailedRequests).toEqual([]);
});

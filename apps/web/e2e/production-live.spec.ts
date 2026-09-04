import {
  expect,
  test,
  type APIRequestContext,
  type Page,
  type Request,
  type TestInfo,
} from "@playwright/test";
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

const WORKSHOP_MACHINE_PROFILE_ID = "custombuild-router-5125-linuxcnc" as const;

const CAM_BLOCKED_FORBIDDEN_ARTIFACT_KIND = /(?:stock|nest|placement|label_index|measurement_plan|operation|tool|setup|backplot|machine|ngc)/i;

function camBlockedArtifactKindIsForbidden(kind: string): boolean {
  return kind !== "stock_selection" && CAM_BLOCKED_FORBIDDEN_ARTIFACT_KIND.test(kind);
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

function isVersionApproval(request: Request): boolean {
  return request.method() === "POST"
    && /^\/v1\/projects\/[^/]+\/versions\/\d+\/approve$/.test(requestPath(request));
}

const WORKSHOP_STOCK_PROFILES = [
  {
    role: "carcass",
    declaration_authority: "CLIENT_DECLARED",
    supplier_profile_id: "playwright-birch-18",
    supplier_profile_version: "batch-2026-09",
    material_id: "birch-plywood",
    material_version: "screening-2026.1",
    sheet_width_um: 2_440_000,
    sheet_height_um: 2_200_000,
    thickness_um: 17_800,
    sheet_count: 4,
    trim_margin_um: 10_000,
    kerf_um: 6_000,
    grain_direction: "X",
    allow_rotation: true,
    defect_zones: [],
    fixture_keep_out_zones: [],
  },
  {
    role: "back",
    declaration_authority: "CLIENT_DECLARED",
    supplier_profile_id: "playwright-birch-6",
    supplier_profile_version: "batch-2026-09",
    material_id: "birch-plywood-6",
    material_version: "screening-2026.1",
    sheet_width_um: 2_440_000,
    sheet_height_um: 2_200_000,
    thickness_um: 6_000,
    sheet_count: 2,
    trim_margin_um: 10_000,
    kerf_um: 6_000,
    grain_direction: "X",
    allow_rotation: true,
    defect_zones: [],
    fixture_keep_out_zones: [],
  },
] as const;

const WORKSHOP_REGISTRATIONS = [
  ...Array.from({ length: 4 }, (_, sheetIndex) => ({
    stock_role: "carcass" as const,
    sheet_index: sheetIndex,
    declaration_authority: "CLIENT_DECLARED" as const,
    flip_axis: "X" as const,
    fixture_method_id: "playwright-pin-fixture",
    fixture_method_version: "v1.0",
    pin_diameter_um: 10_000,
    position_tolerance_um: 1_000,
    pins: [{ x_um: 80_000, y_um: 30_000 }, { x_um: 2_360_000, y_um: 30_000 }],
  })),
  ...Array.from({ length: 2 }, (_, sheetIndex) => ({
    stock_role: "back" as const,
    sheet_index: sheetIndex,
    declaration_authority: "CLIENT_DECLARED" as const,
    flip_axis: "X" as const,
    fixture_method_id: "playwright-pin-fixture",
    fixture_method_version: "v1.0",
    pin_diameter_um: 10_000,
    position_tolerance_um: 1_000,
    pins: [{ x_um: 80_000, y_um: 30_000 }, { x_um: 2_360_000, y_um: 30_000 }],
  })),
] as const;

async function bindStructuredWorkshopContext(page: Page, projectId: string): Promise<void> {
  const editor = page.getByRole("region", { name: "Råmaterial och tvåsidig registrering" });
  await editor.getByRole("radio", { name: /Router 5125/ }).check();
  await editor.getByRole("button", {
    name: "Bind leverantörsdeklarerad verkstadsprofil",
  }).click();

  const fillProfile = async (
    name: "Stomskivor" | "Bakstyckesskivor",
    values: { profileId: string; profileVersion: string; sheetHeightMm: string },
  ) => {
    const profile = editor.getByRole("group", { name });
    await profile.getByLabel("Leverantörens profil-ID (deklarerat)").fill(values.profileId);
    await profile.getByLabel("Profilversion eller batch").fill(values.profileVersion);
    await profile.getByLabel("Skivhöjd (mm)").fill(values.sheetHeightMm);
    await profile.getByLabel("Trimkant (mm)").fill("10");
    await profile.getByLabel("Kerf/verktygsspalt (mm)").fill("6");
    await profile.getByLabel("Fiberriktning i råskivan").selectOption("X");
    await profile.getByLabel("Tillåt 90° rotation vid nesting").selectOption("true");
  };
  await fillProfile("Stomskivor", {
    profileId: WORKSHOP_STOCK_PROFILES[0].supplier_profile_id,
    profileVersion: WORKSHOP_STOCK_PROFILES[0].supplier_profile_version,
    sheetHeightMm: String(WORKSHOP_STOCK_PROFILES[0].sheet_height_um / 1_000),
  });
  await fillProfile("Bakstyckesskivor", {
    profileId: WORKSHOP_STOCK_PROFILES[1].supplier_profile_id,
    profileVersion: WORKSHOP_STOCK_PROFILES[1].supplier_profile_version,
    sheetHeightMm: String(WORKSHOP_STOCK_PROFILES[1].sheet_height_um / 1_000),
  });

  const persistedContext = page.waitForResponse((response) => {
    if (
      response.request().method() !== "PUT"
      || requestPath(response.request()) !== `/v1/projects/${projectId}/draft`
      || !response.ok()
    ) return false;
    const payload = response.request().postDataJSON() as {
      workspace_spec?: { production_context?: { two_sided_registrations?: unknown[] } };
    };
    return payload.workspace_spec?.production_context?.two_sided_registrations?.length
      === WORKSHOP_REGISTRATIONS.length;
  }, { timeout: 30_000 });

  const registrationGroup = editor.getByRole("group", {
    name: "Tvåsidig registrering per fysisk skiva",
  });
  for (const [index, registration] of WORKSHOP_REGISTRATIONS.entries()) {
    await registrationGroup.getByRole("button", { name: "Lägg till tvåsidig skiva" }).click();
    await registrationGroup.getByLabel("Råmaterialroll").nth(index)
      .selectOption(registration.stock_role);
    await registrationGroup.getByLabel("Fysiskt skivnummer").nth(index)
      .fill(String(registration.sheet_index + 1));
    await registrationGroup.getByLabel("Fixtur-/registreringsmetod-ID").nth(index)
      .fill(registration.fixture_method_id);
    await registrationGroup.getByLabel("Fixturmetodens version").nth(index)
      .fill(registration.fixture_method_version);
    await registrationGroup.getByLabel("Registreringspinnens diameter (mm)").nth(index)
      .fill("10");
    await registrationGroup.getByLabel("Positionstolerans (mm)").nth(index)
      .fill("1");
    await registrationGroup.getByLabel("Pinne 1, X (mm)").nth(index).fill("80");
    await registrationGroup.getByLabel("Pinne 1, Y (mm)").nth(index).fill("30");
    await registrationGroup.getByLabel("Pinne 2, X (mm)").nth(index).fill("2360");
    await registrationGroup.getByLabel("Pinne 2, Y (mm)").nth(index).fill("30");
  }

  await expect(editor.getByText("Verkstadsprofilen är komplett och exakt bunden till aktuella designval."))
    .toBeVisible();
  await persistedContext;
}

interface LiveRolePrincipal {
  user_id: string;
  organization_id: string;
  role: "designer" | "reviewer";
}

async function liveRolePrincipal(
  request: APIRequestContext,
  apiUrl: string,
  token: string,
  expectedRole: LiveRolePrincipal["role"],
): Promise<LiveRolePrincipal> {
  const response = await request.get(`${apiUrl}/v1/me`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(response.ok(), `${expectedRole} token was rejected by /v1/me`).toBe(true);
  const principal = await response.json() as LiveRolePrincipal;
  expect(principal.role).toBe(expectedRole);
  expect(principal.user_id).toBeTruthy();
  expect(principal.organization_id).toBeTruthy();
  return principal;
}

async function switchLiveIdentity(
  page: Page,
  token: string,
  principal: LiveRolePrincipal,
  project: { id: string; name: string },
): Promise<void> {
  await page.evaluate(({ accessToken, identity, selectedProject }) => {
    window.sessionStorage.setItem("custombuild:oidc:access-token", JSON.stringify({
      accessToken,
      expiresAt: Date.now() + 60 * 60 * 1_000,
    }));
    window.localStorage.setItem(
      `custombuild:workspace:v2:${identity}:selected-project`,
      JSON.stringify(selectedProject),
    );
  }, {
    accessToken: token,
    identity: `organization:${encodeURIComponent(principal.organization_id)}:user:${encodeURIComponent(principal.user_id)}`,
    selectedProject: project,
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.getByRole("combobox", { name: "Aktivt projekt" }))
    .toHaveValue(project.id, { timeout: 30_000 });
  await expect(page.getByTestId("server-draft-hydration-blocker"))
    .toHaveCount(0, { timeout: 30_000 });
}

async function captureProductionEvidence(
  page: Page,
  testInfo: TestInfo,
  fileName: string,
): Promise<void> {
  // Playwright 1.62's WebKit screenshotter injects an un-nonced `body {}`
  // stylesheet to synchronize animations. The production CSP correctly
  // rejects that test-only style and WebKit reports it as a console error.
  // Chromium/Firefox still provide the diagnostic images; keep WebKit focused
  // on the real cross-browser flow without mutating the page under test.
  if (testInfo.project.name.startsWith("webkit-")) return;
  await page.screenshot({ path: testInfo.outputPath(fileName), fullPage: true });
}

test.skip(
  process.env.PLAYWRIGHT_REAL_API !== "1",
  "Requires the complete Compose API, worker, database, queue and object storage.",
);
test("det verkliga designgranskningsflödet kan skapa och hämta ett granskningspaket", async ({
  page,
  request,
}, testInfo) => {
  test.setTimeout(6 * 60_000);
  await page.setViewportSize({ width: 966, height: 1197 });
  const project = await provisionLiveProject(
    request,
    testInfo,
    `production-flow-${testInfo.project.name}`,
  );
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
  const approvals: Request[] = [];
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
    if (isVersionApproval(request)) approvals.push(request);
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
  // The screened stock and machine checks passed above, so Underlag must not
  // reintroduce the former stockless-review blocker summary.
  await expect(productionDialog.locator(".production-blocking-rules")).toHaveCount(0);
  await expect(productionDialog).not.toContainText("Blockerar CAM");
  await expect(productionDialog).not.toContainText("5 000 × 2 500");
  // Underlag intentionally replaces the editable Studio inspector. Verify the
  // construction summary that remains visible across the mode transition.
  await expect(page.getByTestId("current-design-label").getByText("1800 × 2100 × 320 mm", { exact: true }))
    .toBeVisible();
  console.log("production-live: manufacturing review visible");

  await bindStructuredWorkshopContext(page, project.project.id);
  console.log("production-live: exact stock and two-sided registration persisted");

  const save = page.getByRole("button", {
    name: "Spara och kontrollera",
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
  const createdVersion = await createResponse.json() as { revision?: unknown };
  expect(createdVersion.revision).toBe(1);
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
    stock_height_mm: 2_200,
    stock_count: 4,
    back_stock_width_mm: 2_440,
    back_stock_height_mm: 2_200,
    back_stock_count: 2,
    machine_profile_id: WORKSHOP_MACHINE_PROFILE_ID,
    stock_profiles: WORKSHOP_STOCK_PROFILES,
    two_sided_registrations: WORKSHOP_REGISTRATIONS,
  });
  await expect(productionDialog.locator("#production-next-action-heading")).toHaveText("Skapa underlag", { timeout: 30_000 });
  await expect(productionDialog.getByRole("region", { name: "Status för verifieringen" }))
    .toContainText("Behöver beslut");

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
  const retentionUpload = productionDialog.getByLabel("Certifierarsignerad retention-JSON");
  await expect(retentionUpload).toBeVisible();
  await expect(retentionUpload).toBeEnabled({ timeout: 30_000 });
  await expect(retentionUpload).toHaveAttribute("accept", ".json,application/json");
  await expect(productionDialog).toContainText(
    "Filen måste komma direkt från en extern certifierare",
  );

  const warningConfirmation = productionDialog.getByRole("checkbox", {
    name: "Jag har läst och kontrollerat varningarna ovan.",
  });
  await expect(warningConfirmation).toBeVisible();
  await expect(warningConfirmation).not.toBeChecked();
  await captureProductionEvidence(page, testInfo, "01-simple-review.png");
  await warningConfirmation.check();

  const approvalResponsePromise = page.waitForResponse((candidate) => (
    isVersionApproval(candidate.request())
  ));
  const approveDesign = productionDialog.getByRole("button", {
    name: "Godkänn designkontroll",
    exact: true,
  });
  await expect(approveDesign).toBeEnabled();
  await approveDesign.click();
  const approvalResponse = await approvalResponsePromise;
  expect(
    approvalResponse.ok(),
    `Approval POST returned HTTP ${approvalResponse.status()}`,
  ).toBe(true);

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
    stock_height_mm: 2_200,
    stock_count: 4,
    back_stock_width_mm: 2_440,
    back_stock_height_mm: 2_200,
    back_stock_count: 2,
    machine_profile_id: WORKSHOP_MACHINE_PROFILE_ID,
    stock_profiles: WORKSHOP_STOCK_PROFILES,
    two_sided_registrations: WORKSHOP_REGISTRATIONS,
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
  await expect(camStatus).toContainText("versionsbunden, checksummeadresserad");
  await expect(camStatus).toContainText("torr självlåsning eller mekanisk retention");
  await expect(camStatus).toContainText(
    "Lim, bärande geometri och granskningsgodkännanden ersätter inte retentionsevidens",
  );

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
    dfm_status: "WARNING",
    nesting_layouts: [],
    machine_program_mode: "CAM_BLOCKED",
    production_machine_program: false,
    design_review_package_status: {
      cam_status: "BLOCKED",
      blocker_codes: ["DADO_RETENTION_EVIDENCE_MISSING"],
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
  expect(softwareStatus.get("DFM_SCREEN")).toBe("VERIFIED");
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
  expect(artifactKinds.filter(camBlockedArtifactKindIsForbidden)).toEqual([]);
  await captureProductionEvidence(page, testInfo, "02-underlag-ready.png");

  const downloadPromise = page.waitForEvent("download", { timeout: 60_000 });
  await downloadButton.click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe(
    `custombuild-project-${project.project.id}-design-review-rev-${String(createdVersion.revision)}.zip`,
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
  expect(approvals).toHaveLength(1);
  expect(generations).toHaveLength(1);
  expect(pageErrors).toEqual([]);
  expect(consoleErrors).toEqual([]);
  expect(failedApiResponses).toEqual([]);
  const unexpectedFailedRequests = failedRequests.filter(
    (failure) =>
      !(
        failure.endsWith(": net::ERR_ABORTED")
        // The editor intentionally cancels a superseded latest-wins preview request.
        && failure.includes("/v1/designs/autofix")
      ),
  );
  expect(unexpectedFailedRequests).toEqual([]);
});

test("retention går genom en verkligt rollseparerad granskningskedja", async ({
  page,
  request,
}, testInfo) => {
  const designerToken = process.env.PLAYWRIGHT_DESIGNER_TOKEN?.trim();
  const reviewerToken = process.env.PLAYWRIGHT_REVIEWER_TOKEN?.trim();
  const signedRetentionPath = process.env.PLAYWRIGHT_SIGNED_RETENTION_JSON_PATH?.trim();
  test.skip(
    !designerToken || !reviewerToken || !signedRetentionPath,
    "Requires provisioned designer/reviewer principals and certifier-signed evidence trusted by the live API.",
  );
  test.setTimeout(8 * 60_000);

  const apiUrl = process.env.PLAYWRIGHT_API_URL?.replace(/\/$/, "");
  expect(apiUrl).toBeTruthy();
  const project = await provisionLiveProject(
    request,
    testInfo,
    `retention-role-flow-${testInfo.project.name}`,
  );
  const designer = await liveRolePrincipal(request, apiUrl!, designerToken!, "designer");
  const reviewer = await liveRolePrincipal(request, apiUrl!, reviewerToken!, "reviewer");
  expect(designer.organization_id).toBe(project.principal.organization_id);
  expect(reviewer.organization_id).toBe(project.principal.organization_id);
  expect(reviewer.user_id).not.toBe(designer.user_id);

  await page.goto("/", { waitUntil: "domcontentloaded" });
  await switchLiveIdentity(page, designerToken!, designer, project.project);
  await waitForLiveWorkspaceReady(page, project.project.id);
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
  await initialDraftSave;
  const modes = page.getByRole("navigation", { name: "Produktlägen" });
  await modes.getByRole("button", { name: /Underlag/ }).click();
  await bindStructuredWorkshopContext(page, project.project.id);

  // Reviewer: register the certifier's immutable statement. Uploading alone
  // neither binds retention nor approves the design.
  await switchLiveIdentity(page, reviewerToken!, reviewer, project.project);
  const reviewerWorkflow = page.locator("section.production-drawer-embedded");
  await expect(reviewerWorkflow).toBeVisible({ timeout: 30_000 });
  const uploadResponsePromise = page.waitForResponse((response) => (
    response.request().method() === "POST"
    && requestPath(response.request()) === `/v1/projects/${project.project.id}/evidence`
  ));
  const retentionUpload = reviewerWorkflow.getByLabel("Certifierarsignerad retention-JSON");
  await expect(retentionUpload).toBeEnabled({ timeout: 30_000 });
  await retentionUpload.setInputFiles(signedRetentionPath!);
  const uploadResponse = await uploadResponsePromise;
  const uploadBody = await uploadResponse.text();
  expect(uploadResponse.status(), uploadBody).toBe(201);
  const uploadedEvidence = JSON.parse(uploadBody) as { id?: unknown };
  expect(typeof uploadedEvidence.id).toBe("string");
  await expect(reviewerWorkflow).toContainText("Filen finns nu i serverregistret");

  // Designer: select the server record, require a successful trust-bound
  // preview, and freeze both evidence and workshop context into revision R1.
  await switchLiveIdentity(page, designerToken!, designer, project.project);
  const designerWorkflow = page.locator("section.production-drawer-embedded");
  await expect(designerWorkflow).toBeVisible({ timeout: 30_000 });
  const retentionSelect = designerWorkflow.getByRole("combobox", {
    name: "Signerad retentionsevidens",
  });
  await expect(retentionSelect.locator(`option[value="${String(uploadedEvidence.id)}"]`))
    .toHaveCount(1, { timeout: 30_000 });
  const boundPreviewPromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return response.request().method() === "POST"
      && url.pathname === "/v1/designs/preview"
      && url.searchParams.get("joint_retention_evidence_id") === uploadedEvidence.id;
  });
  await retentionSelect.selectOption(String(uploadedEvidence.id));
  const boundPreview = await boundPreviewPromise;
  expect(boundPreview.ok(), await boundPreview.text()).toBe(true);

  const versionResponsePromise = page.waitForResponse((response) => (
    isVersionCreate(response.request())
  ));
  const validationResponsePromise = page.waitForResponse((response) => (
    isVersionValidation(response.request())
  ));
  const save = designerWorkflow.getByRole("button", {
    name: "Spara och kontrollera",
    exact: true,
  });
  await expect(save).toBeEnabled({ timeout: 30_000 });
  await save.click();
  const [versionResponse, validationResponse] = await Promise.all([
    versionResponsePromise,
    validationResponsePromise,
  ]);
  expect(versionResponse.status(), await versionResponse.text()).toBe(201);
  expect(validationResponse.ok(), await validationResponse.text()).toBe(true);
  const versionRequest = versionResponse.request().postDataJSON() as Record<string, unknown>;
  expect(versionRequest).toMatchObject({
    joint_retention_evidence_id: uploadedEvidence.id,
    production_context: {
      stock_profiles: WORKSHOP_STOCK_PROFILES,
      two_sided_registrations: WORKSHOP_REGISTRATIONS,
    },
  });

  // Reviewer: approve the unchanged server revision. This principal cannot
  // generate, so the browser must stop at the hand-off after approval.
  await switchLiveIdentity(page, reviewerToken!, reviewer, project.project);
  const approvalWorkflow = page.locator("section.production-drawer-embedded");
  await expect(approvalWorkflow).toBeVisible({ timeout: 30_000 });
  const warningConfirmation = approvalWorkflow.getByRole("checkbox", {
    name: "Jag har läst och kontrollerat varningarna ovan.",
  });
  if (await warningConfirmation.count()) await warningConfirmation.check();
  const approvalResponsePromise = page.waitForResponse((response) => (
    isVersionApproval(response.request())
  ));
  const approve = approvalWorkflow.getByRole("button", {
    name: "Godkänn designkontroll",
    exact: true,
  });
  await expect(approve).toBeEnabled({ timeout: 30_000 });
  await approve.click();
  const approvalResponse = await approvalResponsePromise;
  expect(approvalResponse.ok(), await approvalResponse.text()).toBe(true);
  await expect(approvalWorkflow.getByRole("button", { name: "Skapa underlag" }))
    .toBeDisabled();
  await expect(approvalWorkflow).toContainText(
    "En designer, admin eller owner måste nu skapa underlaget",
  );

  // Designer: restore the approved revision and submit the exact frozen
  // generation context. The physical-cutting boundary remains server-owned.
  await switchLiveIdentity(page, designerToken!, designer, project.project);
  const generationWorkflow = page.locator("section.production-drawer-embedded");
  await expect(generationWorkflow).toBeVisible({ timeout: 30_000 });
  const generationResponsePromise = page.waitForResponse((response) => (
    isVersionGeneration(response.request())
  ));
  const generate = generationWorkflow.getByRole("button", {
    name: "Skapa underlag",
    exact: true,
  });
  await expect(generate).toBeEnabled({ timeout: 30_000 });
  await generate.click();
  const generationResponse = await generationResponsePromise;
  const generationBody = await generationResponse.text();
  expect(generationResponse.ok(), generationBody).toBe(true);
  expect(generationResponse.request().postDataJSON()).toMatchObject({
    stock_profiles: WORKSHOP_STOCK_PROFILES,
    two_sided_registrations: WORKSHOP_REGISTRATIONS,
    machine_profile_id: WORKSHOP_MACHINE_PROFILE_ID,
    postprocessor_id: "linuxcnc-validation-1.1.0",
    include_validation_program: true,
  });
  const queuedJob = JSON.parse(generationBody) as { id?: unknown };
  expect(typeof queuedJob.id).toBe("string");

  const packageDownload = generationWorkflow.getByRole("button", {
    name: "Ladda ned granskningspaket (.zip)",
    exact: true,
  });
  await expect(packageDownload).toBeVisible({ timeout: 4 * 60_000 });
  const packageDownloadPromise = page.waitForEvent("download", { timeout: 60_000 });
  await packageDownload.click();
  const packageFile = await packageDownloadPromise;
  expect(packageFile.suggestedFilename()).toBe(
    `custombuild-project-${project.project.id}-design-review-rev-1.zip`,
  );
  expect(await packageFile.failure()).toBeNull();

  const manifestDownload = generationWorkflow.getByRole("button", {
    name: "Hämta fil – Manifest",
    exact: true,
  });
  await expect(manifestDownload).toBeEnabled();
  const manifestDownloadPromise = page.waitForEvent("download", { timeout: 60_000 });
  await manifestDownload.click();
  const manifestFile = await manifestDownloadPromise;
  expect(manifestFile.suggestedFilename()).toBe(
    `custombuild-project-${project.project.id}-design-review-manifest-rev-1.json`,
  );
  expect(await manifestFile.failure()).toBeNull();
});

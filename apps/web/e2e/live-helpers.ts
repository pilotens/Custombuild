import { expect, type APIRequestContext, type Page, type Response, type TestInfo } from "@playwright/test";

interface LivePrincipal {
  user_id: string;
  organization_id: string;
}

export interface LiveProject {
  id: string;
  name: string;
}

export interface ProvisionedLiveProject {
  principal: LivePrincipal;
  project: LiveProject;
}

function requiredEnvironment(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`${name} must be set for real-API acceptance.`);
  return value;
}

function safeSegment(value: string, label: string, maximumLength: number): string {
  const normalized = value
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, maximumLength);
  if (!normalized) throw new Error(`${label} must contain at least one letter or digit.`);
  return normalized;
}

function liveSettings() {
  const apiUrl = requiredEnvironment("PLAYWRIGHT_API_URL").replace(/\/$/, "");
  const parsedApiUrl = new URL(apiUrl);
  if (!(["http:", "https:"] as const).includes(parsedApiUrl.protocol as "http:" | "https:")) {
    throw new Error("PLAYWRIGHT_API_URL must be an HTTP(S) origin.");
  }
  const environment = safeSegment(requiredEnvironment("PLAYWRIGHT_ENVIRONMENT"), "PLAYWRIGHT_ENVIRONMENT", 16);
  if (!new Set(["test", "prod"]).has(environment)) {
    throw new Error("PLAYWRIGHT_ENVIRONMENT must be either test or prod.");
  }
  return {
    apiUrl,
    environment,
    runId: safeSegment(requiredEnvironment("PLAYWRIGHT_RUN_ID"), "PLAYWRIGHT_RUN_ID", 48),
    token: process.env.PLAYWRIGHT_DEMO_TOKEN?.trim() || "demo-nordic-owner",
  };
}

async function responseFailure(response: Awaited<ReturnType<APIRequestContext["get"]>>): Promise<string> {
  const body = await response.text().catch(() => "<unreadable body>");
  return `HTTP ${response.status()} ${response.url()}: ${body.slice(0, 1_000)}`;
}

export async function provisionLiveProject(
  request: APIRequestContext,
  testInfo: TestInfo,
  purpose: string,
): Promise<ProvisionedLiveProject> {
  const settings = liveSettings();
  const headers = { Authorization: `Bearer ${settings.token}` };

  const readiness = await request.get(`${settings.apiUrl}/ready`);
  if (!readiness.ok()) throw new Error(`API is not ready: ${await responseFailure(readiness)}`);
  const readinessBody = await readiness.json() as Record<string, unknown>;
  for (const dependency of ["database", "redis", "object_storage", "rule_engine"]) {
    if (readinessBody[dependency] !== "ok") {
      throw new Error(`API readiness did not report ${dependency}=ok.`);
    }
  }

  const principalResponse = await request.get(`${settings.apiUrl}/v1/me`, { headers });
  if (!principalResponse.ok()) {
    throw new Error(`Could not resolve the acceptance principal: ${await responseFailure(principalResponse)}`);
  }
  const principal = await principalResponse.json() as LivePrincipal;
  if (!principal.user_id || !principal.organization_id) {
    throw new Error("The acceptance principal is missing user_id or organization_id.");
  }

  const projectName = [
    "UI-QA",
    settings.environment,
    settings.runId,
    safeSegment(purpose, "project purpose", 28),
    `r${testInfo.retry}`,
  ].join("-");
  const projectResponse = await request.post(`${settings.apiUrl}/v1/projects`, {
    headers,
    data: {
      name: projectName,
      description: `Playwright current-source acceptance: ${purpose}`,
      furniture_type: "bookcase",
    },
  });
  if (projectResponse.status() !== 201) {
    throw new Error(
      `Could not create deterministic QA project ${projectName}. Use a new PLAYWRIGHT_RUN_ID. ${await responseFailure(projectResponse)}`,
    );
  }
  const project = await projectResponse.json() as LiveProject;
  if (!project.id || project.name !== projectName) {
    throw new Error("The project create response did not preserve the requested QA identity.");
  }
  return { principal, project };
}

export async function selectProjectBeforeNavigation(
  page: Page,
  provisioned: ProvisionedLiveProject,
): Promise<void> {
  const token = liveSettings().token;
  const identity = `organization:${encodeURIComponent(provisioned.principal.organization_id)}:user:${encodeURIComponent(provisioned.principal.user_id)}`;
  const key = `custombuild:workspace:v2:${identity}:selected-project`;
  const bootstrapKey = `custombuild:e2e:live-storage-initialized:${encodeURIComponent(provisioned.project.id)}`;
  await page.addInitScript(({ accessToken, bootstrapMarker, selectionKey, project }) => {
    const storageInitialized = window.sessionStorage.getItem(bootstrapMarker) === "1";
    if (!storageInitialized) {
      window.localStorage.clear();
      window.sessionStorage.clear();
      window.sessionStorage.setItem(bootstrapMarker, "1");
    }
    window.sessionStorage.setItem("custombuild:oidc:access-token", JSON.stringify({
      accessToken,
      expiresAt: Date.now() + 60 * 60 * 1_000,
    }));
    window.localStorage.setItem(selectionKey, JSON.stringify(project));
  }, {
    accessToken: token,
    bootstrapMarker: bootstrapKey,
    selectionKey: key,
    project: provisioned.project,
  });
}

export async function waitForLiveWorkspaceReady(
  page: Page,
  projectId: string,
): Promise<void> {
  const activeProject = page.getByRole("combobox", { name: "Aktivt projekt" });
  await expect(activeProject).toHaveValue(projectId, { timeout: 30_000 });
  await expect(activeProject).toBeEnabled({ timeout: 30_000 });
  await expect(page.getByTestId("server-draft-hydration-blocker"))
    .toHaveCount(0, { timeout: 30_000 });
  await expect(page.getByRole("status").filter({ hasText: "Verifierar projektets serverutkast" }))
    .toHaveCount(0, { timeout: 30_000 });
  await expect(page.getByRole("heading", { name: "Vad vill du skapa?" }))
    .toBeVisible({ timeout: 30_000 });
}

export function waitForSuccessfulProjectDraftSave(
  page: Page,
  projectId: string,
  expectedDraftFields: Record<string, unknown> = {},
): Promise<Response> {
  const expectedPath = `/v1/projects/${encodeURIComponent(projectId)}/draft`;
  return page.waitForResponse((response) => {
    if (
      response.request().method() !== "PUT"
      || new URL(response.url()).pathname !== expectedPath
      || !response.ok()
    ) return false;
    const payload = response.request().postDataJSON() as {
      spec?: unknown;
      workspace_spec?: unknown;
    };
    const spec = payload.spec !== null && typeof payload.spec === "object" && !Array.isArray(payload.spec)
      ? payload.spec as Record<string, unknown>
      : {};
    const workspace = payload.workspace_spec !== null
      && typeof payload.workspace_spec === "object"
      && !Array.isArray(payload.workspace_spec)
      ? payload.workspace_spec as Record<string, unknown>
      : {};
    const production = workspace.production_context !== null
      && typeof workspace.production_context === "object"
      && !Array.isArray(workspace.production_context)
      ? workspace.production_context as Record<string, unknown>
      : {};
    return Object.entries(expectedDraftFields).every(([key, value]) => {
      const source = Object.hasOwn(spec, key)
        ? spec
        : Object.hasOwn(workspace, key)
          ? workspace
          : production;
      return source[key] === value;
    });
  }, { timeout: 30_000 });
}

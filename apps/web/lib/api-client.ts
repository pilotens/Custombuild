import { resolveDesign } from "./design-engine";
import type { components, paths } from "./api-schema";
import type {
  BomLine,
  ChangeDiff,
  DesignSpec,
  ManufacturingFeature,
  ResolvedDesign,
  ResolvedPart,
  RuleEvaluation,
  ValidationStatus,
} from "./design-types";

type JsonRecord = Record<string, unknown>;
type PreviewRequestBody = paths["/v1/designs/preview"]["post"]["requestBody"]["content"]["application/json"];
type PreviewResponseBody = paths["/v1/designs/preview"]["post"]["responses"][200]["content"]["application/json"];
type AutofixRequestBody = paths["/v1/designs/autofix"]["post"]["requestBody"]["content"]["application/json"];
type AutofixResponseBody = paths["/v1/designs/autofix"]["post"]["responses"][200]["content"]["application/json"];
export type ProjectRead = components["schemas"]["ProjectRead"];
export type DesignVersionRead = components["schemas"]["DesignVersionRead"];
export type JobRead = components["schemas"]["JobRead"];
export type GenerationRequest = components["schemas"]["GenerationRequest"];
export type ApprovalCreate = components["schemas"]["ApprovalCreate"];
export type ArtifactRead = components["schemas"]["ArtifactRead"];
export type ReleaseRead = components["schemas"]["ReleaseRead"];

export function toPreviewRequest(spec: DesignSpec): PreviewRequestBody {
  if (spec.joint_system !== "dado") {
    throw new ApiError(
      `Fogsystemet ${String(spec.joint_system)} stöds inte i produktions-MVP:n. Välj not/spår (DADO).`,
    );
  }
  return {
    width_mm: spec.width_mm,
    height_mm: spec.height_mm,
    depth_mm: spec.depth_mm,
    material_id: spec.material_id === "birch-plywood" ? "birch-plywood" : "mdf",
    nominal_thickness_mm: spec.nominal_thickness_mm,
    measured_thickness_mm: spec.measured_thickness_mm,
    shelf_count: spec.shelf_count,
    shelf_mount: spec.fixed_shelves ? "fixed" : "adjustable",
    load_per_shelf_kg: spec.load_per_shelf_kg,
    back_panel: spec.back_panel,
    plinth: spec.plinth,
    divider_count: spec.divider_count,
    edge_band_mm: spec.edge_band_mm,
    joint_system: spec.joint_system,
    reinforcement_mode: spec.reinforcement_mode,
    wall_anchor_required: false,
    wall_anchor_verified: false,
  };
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function asRecord(value: unknown): JsonRecord | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : undefined;
}

function asNumber(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function asString(value: unknown, fallback: string): string {
  return typeof value === "string" && value.length > 0 ? value : fallback;
}

function asStatus(value: unknown, fallback: ValidationStatus): ValidationStatus {
  const normalized = typeof value === "string" ? value.toUpperCase() : "";
  return normalized === "PASS" || normalized === "WARNING" || normalized === "BLOCK"
    ? normalized
    : fallback;
}

function normalizeFeatures(value: unknown, partId: string, fallback: ManufacturingFeature[]): ManufacturingFeature[] {
  if (!Array.isArray(value)) return fallback;
  return value.map((raw, index) => {
    const feature = asRecord(raw) ?? {};
    const rawKind = asString(feature.kind, "outline").toLowerCase();
    const kind: ManufacturingFeature["kind"] = rawKind === "drill" || rawKind === "drill_pattern"
      ? "drill"
      : rawKind === "groove" || rawKind === "rabbet"
        ? "groove"
        : rawKind === "pocket" || rawKind === "tenon"
          ? "pocket"
          : rawKind === "label"
            ? "label"
            : "outline";
    const rawFace = asString(feature.face, "A").toUpperCase();
    const face: ManufacturingFeature["face"] = rawFace === "A" || rawFace === "B" ? rawFace : "EDGE";
    const dimensions = asRecord(feature.dimensions);
    return {
      id: asString(feature.id ?? feature.feature_id, `${partId}:feature-${index + 1}`),
      kind,
      face,
      depth_mm: dimensions && typeof dimensions.depth_um === "number"
        ? dimensions.depth_um / 1_000
        : asNumber(feature.depth_mm, 0),
      description: asString(feature.description, kind),
      ...(typeof feature.tool_diameter_mm === "number" ? { tool_diameter_mm: feature.tool_diameter_mm } : {}),
    };
  });
}

function normalizeParts(value: unknown, fallback: ResolvedPart[], spec: DesignSpec): ResolvedPart[] {
  if (!Array.isArray(value) || value.length === 0) return fallback;
  return value.map((raw, index) => {
    const part = asRecord(raw) ?? {};
    const fallbackPart = fallback[index] ?? fallback[0];
    if (!fallbackPart) throw new ApiError("Servern returnerade en tom eller ogiltig dellista.");
    const rawPosition = asRecord(part.position_mm) ?? {};
    const rawKind = asString(part.kind, fallbackPart.kind);
    const kind: ResolvedPart["kind"] =
      rawKind === "side" ||
      rawKind === "top" ||
      rawKind === "bottom" ||
      rawKind === "shelf" ||
      rawKind === "back" ||
      rawKind === "plinth" ||
      rawKind === "divider"
        ? rawKind
        : fallbackPart.kind;
    const rawOrientation = asString(part.orientation, fallbackPart.orientation);
    const orientation: ResolvedPart["orientation"] =
      rawOrientation === "XZ" || rawOrientation === "YZ" ? rawOrientation : "XY";
    return {
      part_id: asString(part.part_id, fallbackPart.part_id),
      name: asString(part.name, fallbackPart.name),
      kind,
      width_mm: asNumber(part.width_mm, fallbackPart.width_mm),
      depth_mm: asNumber(part.depth_mm, fallbackPart.depth_mm),
      thickness_mm: asNumber(part.thickness_mm, fallbackPart.thickness_mm),
      position_mm: {
        x: asNumber(rawPosition.x, fallbackPart.position_mm.x),
        y: asNumber(rawPosition.y, fallbackPart.position_mm.y),
        z: asNumber(rawPosition.z, fallbackPart.position_mm.z),
      },
      orientation,
      color: asString(part.color, fallbackPart.color),
      material_id: asString(part.material_id, spec.material_id),
      weight_kg: asNumber(part.weight_kg, fallbackPart.weight_kg),
      features: normalizeFeatures(part.features, asString(part.part_id, fallbackPart.part_id), fallbackPart.features),
    };
  });
}

function normalizeRules(value: unknown, fallback: RuleEvaluation[]): RuleEvaluation[] {
  if (!Array.isArray(value) || value.length === 0) return fallback;
  return value.map((raw) => {
    const rule = asRecord(raw) ?? {};
    const ruleId = asString(rule.rule_id, "");
    if (!ruleId) throw new ApiError("Servern returnerade ett regelresultat utan rule_id.");
    const trace = Array.isArray(rule.trace)
      ? rule.trace.map((item) => asRecord(item)).filter((item): item is JsonRecord => Boolean(item))
      : [];
    const calculation = trace
      .map((step) => `${asString(step.expression, "Beräkning")}: ${asString(step.result, "—")}${typeof step.unit === "string" ? ` ${step.unit}` : ""}`)
      .join(" · ");
    const suggested = Array.isArray(rule.suggested_actions)
      ? rule.suggested_actions.map((item) => asRecord(item)).find((item): item is JsonRecord => Boolean(item))
      : undefined;
    const actionType = suggested ? asString(suggested.action_type, "") : "";
    const changes = suggested && Array.isArray(suggested.changes)
      ? suggested.changes.map((item) => asRecord(item)).filter((item): item is JsonRecord => Boolean(item))
      : [];
    const mappedAction = actionType === "add_vertical_divider"
      ? "set_divider_count"
      : actionType === "add_back_panel"
        ? "enable_back"
        : actionType === "verify_wall_anchor"
          ? "verify_wall_anchor"
          : undefined;
    const suggestedValue = mappedAction === "set_divider_count"
      ? asNumber(changes.find((change) => change.path === "parameters.vertical_divider_count")?.after, 1)
      : mappedAction === "enable_back"
        ? true
        : false;
    const calculated = asNumber(rule.calculated_value, 0);
    const allowed = asNumber(rule.allowed_value, 0);
    const unit = asString(rule.unit, "");
    return {
      rule_id: ruleId,
      rule_version: asString(rule.rule_version, "unknown"),
      status: asStatus(rule.status, "BLOCK"),
      title: asString(rule.title, ruleId),
      summary: `${asString(rule.title, ruleId)}: beräknat ${calculated} ${unit}, tillåtet ${allowed} ${unit}.`,
      calculation: calculation || "Servern returnerade inget beräkningsspår.",
      calculated_value: calculated,
      allowed_value: allowed,
      unit,
      ...(typeof rule.safety_margin_permille === "number" ? { margin_percent: rule.safety_margin_permille / 10 } : {}),
      assumptions: Array.isArray(rule.assumptions)
        ? rule.assumptions.filter((item): item is string => typeof item === "string")
        : [],
      affected_part_ids: Array.isArray(rule.applies_to_part_ids)
        ? rule.applies_to_part_ids.filter((item): item is string => typeof item === "string")
        : [],
      ...(suggested && mappedAction
        ? {
            suggestion: {
              action: mappedAction,
              label: asString(suggested.description, "Tillämpa åtgärd"),
              value: suggestedValue,
              explanation: asString(suggested.description, "Servern föreslår en deterministisk korrigering."),
            },
          }
        : {}),
    };
  });
}

function specFromServer(value: unknown, requested: DesignSpec): DesignSpec {
  const root = asRecord(value);
  const parameters = root ? asRecord(root.parameters) : undefined;
  const material = root ? asRecord(root.material) : undefined;
  if (!parameters) return requested;
  const materialId = asString(material?.material_id, requested.material_id);
  const materialDefinition = materialId === "birch-plywood" ? "Björkplywood" : "MDF";
  const joint = asString(parameters.joint_system, requested.joint_system);
  if (joint !== "dado") {
    throw new ApiError(
      `Servern returnerade fogsystemet ${joint}, som inte stöds av produktions-MVP:n.`,
    );
  }
  return {
    ...requested,
    width_mm: asNumber(parameters.width_um, requested.width_mm * 1_000) / 1_000,
    height_mm: asNumber(parameters.height_um, requested.height_mm * 1_000) / 1_000,
    depth_mm: asNumber(parameters.depth_um, requested.depth_mm * 1_000) / 1_000,
    material_id: materialId,
    material_name: asString(material?.name, materialDefinition),
    nominal_thickness_mm: asNumber(parameters.nominal_thickness_um, requested.nominal_thickness_mm * 1_000) / 1_000,
    measured_thickness_mm: asNumber(parameters.actual_thickness_um, requested.measured_thickness_mm * 1_000) / 1_000,
    shelf_count: asNumber(parameters.shelf_count, requested.shelf_count),
    fixed_shelves: asString(parameters.shelf_mount, "fixed") === "fixed",
    load_per_shelf_kg: asNumber(parameters.shelf_load_n, requested.load_per_shelf_kg * 9.80665) / 9.80665,
    back_panel: asString(parameters.back_panel, "none") !== "none",
    plinth: asNumber(parameters.plinth_height_um, requested.plinth ? 80_000 : 0) > 0,
    divider_count: asNumber(parameters.vertical_divider_count, requested.divider_count),
    reinforcement_mode: asString(parameters.reinforcement_mode, requested.reinforcement_mode) === "auto" ? "auto" : "manual",
    joint_system: joint,
    edge_band_mm: asNumber(parameters.edge_band_thickness_um, requested.edge_band_mm * 1_000) / 1_000,
    wall_anchor_verified: false,
  };
}

function normalizeChangeDiff(value: unknown): ChangeDiff[] {
  if (!Array.isArray(value)) return [];
  const fields: Record<string, keyof DesignSpec> = {
    "parameters.vertical_divider_count": "divider_count",
    "parameters.back_panel": "back_panel",
    "parameters.reinforcement_mode": "reinforcement_mode",
  };
  return value.flatMap((raw) => {
    const diff = asRecord(raw);
    if (!diff || !Array.isArray(diff.changes)) return [];
    return diff.changes.flatMap((rawChange) => {
      const change = asRecord(rawChange);
      const field = change ? fields[asString(change.path, "")] : undefined;
      if (!change || !field) return [];
      return [{
        field,
        before: change.before as string | number | boolean,
        after: change.after as string | number | boolean,
        reason: asString(diff.explanation, "Serverns deterministiska autokorrigering."),
      }];
    });
  });
}

function normalizeBom(value: unknown, fallback: BomLine[]): BomLine[] {
  if (!Array.isArray(value) || value.length === 0) return fallback;
  return value.map((raw, index) => {
    const line = asRecord(raw) ?? {};
    const fallbackLine = fallback[index] ?? fallback[0];
    if (!fallbackLine) throw new ApiError("Servern returnerade en ogiltig BOM.");
    return {
      id: asString(line.id ?? line.line_id, fallbackLine.id),
      category: line.category === "hardware" ? "hardware" : "part",
      item: asString(line.item ?? line.name, fallbackLine.item),
      quantity: asNumber(line.quantity, fallbackLine.quantity),
      unit: line.unit === "m" ? "m" : "st",
      part_ids: Array.isArray(line.part_ids)
        ? line.part_ids.filter((item): item is string => typeof item === "string")
        : fallbackLine.part_ids,
      ...(typeof line.dimensions === "string" ? { dimensions: line.dimensions } : {}),
      ...(typeof line.material === "string" ? { material: line.material } : {}),
    };
  });
}

export function normalizePreviewResponse(payload: unknown, requestedSpec: DesignSpec): ResolvedDesign {
  const response = asRecord(payload);
  if (!response) throw new ApiError("Servern returnerade ett svar i okänt format.");
  const serverSpec = specFromServer(response.spec, requestedSpec);
  const local = resolveDesign(serverSpec);
  const rules = normalizeRules(response.rule_evaluations, local.rule_evaluations);
  const status = rules.some((rule) => rule.status === "BLOCK")
    ? "BLOCK"
    : rules.some((rule) => rule.status === "WARNING")
      ? "WARNING"
      : asStatus(response.status, "PASS");
  return {
    ...local,
    design_hash: asString(response.design_hash, local.design_hash),
    parts: normalizeParts(response.parts, local.parts, serverSpec),
    bom: normalizeBom(response.bom, local.bom),
    rule_evaluations: rules,
    status,
    change_diff: normalizeChangeDiff(response.change_diff),
    source: "server-preview",
  };
}

export class CustombuildApiClient {
  readonly baseUrl: string | undefined;

  constructor(
    baseUrl = process.env.NEXT_PUBLIC_API_URL,
    private readonly token = process.env.NEXT_PUBLIC_DEMO_TOKEN,
  ) {
    this.baseUrl = baseUrl?.replace(/\/$/, "") || undefined;
  }

  get configured(): boolean {
    return Boolean(this.baseUrl);
  }

  private async request<ResponseBody>(path: string, options: RequestInit): Promise<ResponseBody> {
    if (!this.baseUrl) throw new ApiError("API-adress saknas. Lokal deterministisk förhandsvisning används.");
    let response: Response;
    try {
      response = await fetch(`${this.baseUrl}${path}`, {
        ...options,
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          ...(this.token ? { Authorization: `Bearer ${this.token}` } : {}),
          ...options.headers,
        },
      });
    } catch {
      throw new ApiError("Kunde inte nå konstruktions-API:t. Lokal förhandsvisning är fortfarande aktiv.");
    }
    if (!response.ok) {
      let detail = response.statusText;
      try {
        const body = (await response.json()) as { detail?: unknown };
        if (typeof body.detail === "string") detail = body.detail;
      } catch {
        // Keep the HTTP status text if the body is not JSON.
      }
      throw new ApiError(`API ${response.status}: ${detail}`, response.status);
    }
    return response.json() as Promise<ResponseBody>;
  }

  async listProjects(): Promise<ProjectRead[]> {
    return this.request<ProjectRead[]>("/v1/projects", { method: "GET" });
  }

  async createProject(name: string): Promise<ProjectRead> {
    return this.request<ProjectRead>("/v1/projects", {
      method: "POST",
      body: JSON.stringify({ name, furniture_type: "bookcase" }),
    });
  }

  async ensureProject(name: string): Promise<ProjectRead> {
    const projects = await this.listProjects();
    const existing = projects.find((project) => project.name === name);
    if (existing) return existing;
    try {
      return await this.createProject(name);
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 409) throw error;
      const refreshed = await this.listProjects();
      const raced = refreshed.find((project) => project.name === name);
      if (!raced) throw error;
      return raced;
    }
  }

  async listVersions(projectId: string): Promise<DesignVersionRead[]> {
    return this.request<DesignVersionRead[]>(
      `/v1/projects/${encodeURIComponent(projectId)}/versions`,
      { method: "GET" },
    );
  }

  async createVersion(projectId: string, spec: DesignSpec): Promise<DesignVersionRead> {
    return this.request<DesignVersionRead>(
      `/v1/projects/${encodeURIComponent(projectId)}/versions`,
      {
        method: "POST",
        body: JSON.stringify({ spec: toPreviewRequest(spec) }),
      },
    );
  }

  async validateVersion(projectId: string, revision: number): Promise<DesignVersionRead> {
    return this.request<DesignVersionRead>(
      `/v1/projects/${encodeURIComponent(projectId)}/versions/${revision}/validate`,
      { method: "POST" },
    );
  }

  async approveVersion(
    projectId: string,
    revision: number,
    approval: ApprovalCreate,
  ): Promise<DesignVersionRead> {
    return this.request<DesignVersionRead>(
      `/v1/projects/${encodeURIComponent(projectId)}/versions/${revision}/approve`,
      { method: "POST", body: JSON.stringify(approval) },
    );
  }

  async generateVersion(
    projectId: string,
    revision: number,
    request: GenerationRequest,
  ): Promise<JobRead> {
    return this.request<JobRead>(
      `/v1/projects/${encodeURIComponent(projectId)}/versions/${revision}/generate`,
      { method: "POST", body: JSON.stringify(request) },
    );
  }

  async getJob(jobId: string): Promise<JobRead> {
    return this.request<JobRead>(`/v1/jobs/${encodeURIComponent(jobId)}`, { method: "GET" });
  }

  async listArtifacts(jobId: string): Promise<ArtifactRead[]> {
    const payload = await this.request<JsonRecord[]>(
      `/v1/jobs/${encodeURIComponent(jobId)}/artifacts`,
      { method: "GET" },
    );
    return payload.map((artifact) => {
      const id = asString(artifact.id, "");
      const downloadUrl = asString(artifact.download_url, "");
      const sha256 = asString(artifact.sha256, "");
      const sizeBytes = asNumber(artifact.size_bytes, -1);
      let isWebUrl = false;
      try {
        const parsed = new URL(downloadUrl);
        isWebUrl = parsed.protocol === "https:" || parsed.protocol === "http:";
      } catch {
        // The URL is validated below together with the artifact identity and digest.
      }
      if (!id || !isWebUrl || !/^[a-f0-9]{64}$/.test(sha256) || sizeBytes < 0) {
        throw new ApiError("Servern returnerade en ogiltig artefaktlänk.");
      }
      return {
        id,
        kind: asString(artifact.kind, "unknown"),
        sha256,
        size_bytes: sizeBytes,
        content_type: asString(artifact.content_type, "application/octet-stream"),
        download_url: downloadUrl,
        download_path: asString(artifact.download_path, ""),
      };
    });
  }

  async releaseVersion(
    projectId: string,
    revision: number,
    releaseNumber: string,
  ): Promise<ReleaseRead> {
    const payload = await this.request<JsonRecord>(
      `/v1/projects/${encodeURIComponent(projectId)}/versions/${revision}/release`,
      {
        method: "POST",
        body: JSON.stringify({ release_number: releaseNumber, confirmation: "RELEASE" }),
      },
    );
    const releaseId = asString(payload.release_id, "");
    const manifestSha = asString(payload.manifest_sha256, "");
    if (!releaseId || manifestSha.length !== 64 || payload.machine_use !== "validation_only") {
      throw new ApiError("Servern returnerade ett ofullständigt frisläppningsbevis.");
    }
    return {
      release_id: releaseId,
      release_number: asString(payload.release_number, releaseNumber),
      status: "released",
      manifest_sha256: manifestSha,
      machine_use: "validation_only",
    };
  }

  async previewDesign(spec: DesignSpec, signal?: AbortSignal): Promise<ResolvedDesign> {
    const requestBody = toPreviewRequest(spec);
    const payload = await this.request<PreviewResponseBody>("/v1/designs/preview", {
      method: "POST",
      body: JSON.stringify(requestBody),
      signal,
    });
    return normalizePreviewResponse(payload, spec);
  }

  async autofixDesign(spec: DesignSpec, signal?: AbortSignal): Promise<ResolvedDesign> {
    const requestBody: AutofixRequestBody = toPreviewRequest(spec);
    const payload = await this.request<AutofixResponseBody>("/v1/designs/autofix", {
      method: "POST",
      body: JSON.stringify(requestBody),
      signal,
    });
    return normalizePreviewResponse(payload, spec);
  }
}

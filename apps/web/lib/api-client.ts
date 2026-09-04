import { resolveDesign } from "./design-engine";
import { getStoredAccessToken } from "./auth-client";
import { referenceImageVerificationIsCurrent } from "./reference-image";
import type { components, paths } from "./api-schema";
import type {
  BackMaterialId,
  BomLine,
  ChangeDiff,
  DesignSpec,
  ManufacturingFeature,
  RetentionCertificationRequest,
  ResolvedDesign,
  ResolvedPart,
  RuleEvaluation,
  ValidationStatus,
} from "./design-types";
import type { FurnitureTemplateId } from "./furniture-templates";
import {
  parseLocalDesignSpec,
  workspaceIntentEnvelopeFromSpec,
} from "./workspace-design-envelope";
import {
  productionContextFromDesignSpec,
  productionContextsEqual,
  type RevisionProductionContextSnapshot as WorkshopRevisionProductionContextSnapshot,
} from "./workshop-production-context";

type JsonRecord = Record<string, unknown>;
type PreviewRequestBody = paths["/v1/designs/preview"]["post"]["requestBody"]["content"]["application/json"];
type PreviewResponseBody = paths["/v1/designs/preview"]["post"]["responses"][200]["content"]["application/json"];
type AutofixRequestBody = paths["/v1/designs/autofix"]["post"]["requestBody"]["content"]["application/json"];
type AutofixResponseBody = paths["/v1/designs/autofix"]["post"]["responses"][200]["content"]["application/json"];
export type ProjectRead = components["schemas"]["ProjectRead"];
export type DesignVersionRead = components["schemas"]["DesignVersionRead"];
export type ImportInspection = components["schemas"]["ImportInspection"];
export type JobRead = components["schemas"]["JobRead"];
export type GenerationRequest = components["schemas"]["GenerationRequest"];
export type ApprovalCreate = components["schemas"]["ApprovalCreate"];
export type ArtifactRead = components["schemas"]["ArtifactRead"];
export type ReleaseRead = components["schemas"]["ReleaseRead"];
export type ApprovalRead = components["schemas"]["ApprovalRead"];
export type ProductionStateRead = components["schemas"]["ProductionStateRead"];
export type ExternalEvidenceRead = components["schemas"]["ExternalEvidenceRead"];

export interface ExternalEvidenceUploadInput {
  document: File;
  evidenceType: ExternalEvidenceRead["evidence_type"];
  ruleId: string;
  catalogId: string;
  catalogVersion: string;
  designHash: string;
  expiresAt?: string;
}
export interface CurrentPrincipal {
  user_id: string;
  organization_id: string;
  role: string;
  name: string;
  email: string;
}

export interface ProjectDraftRead {
  project_id: string;
  draft_revision: number;
  template_id: string | null;
  design_hash: string | null;
  spec_json: Record<string, unknown> | null;
  workspace_spec_json: Record<string, unknown> | null;
  result_json: Record<string, unknown> | null;
  updated_at: string;
}

export type RevisionProductionContextSnapshot = WorkshopRevisionProductionContextSnapshot;

export function productionContextFromSpec(spec: DesignSpec): RevisionProductionContextSnapshot {
  return productionContextFromDesignSpec(spec);
}

export function versionProductionContextMatches(
  version: DesignVersionRead,
  spec: DesignSpec,
): boolean {
  const result = asRecord(version.result_json);
  return productionContextsEqual(result?.production_context, spec);
}

export function toPreviewRequest(spec: DesignSpec): PreviewRequestBody {
  if (spec.joint_system !== "dado") {
    throw new ApiError(
      `Förbandssystemet ${String(spec.joint_system)} stöds inte i produktions-MVP:n. Välj not/spår (DADO).`,
    );
  }
  return {
    furniture_type: spec.furniture_type,
    width_mm: spec.width_mm,
    height_mm: spec.height_mm,
    depth_mm: spec.depth_mm,
    material_id: spec.material_id === "birch-plywood" ? "birch-plywood" : "mdf",
    ...(spec.back_panel ? { back_material_id: spec.back_material_id } : {}),
    nominal_thickness_mm: spec.nominal_thickness_mm,
    measured_thickness_mm: spec.measured_thickness_mm,
    measured_back_thickness_mm: spec.measured_back_thickness_mm,
    shelf_count: spec.shelf_count,
    shelf_mount: spec.fixed_shelves ? "fixed" : "adjustable",
    load_per_shelf_kg: spec.load_per_shelf_kg,
    back_panel: spec.back_panel ? spec.back_panel_type : false,
    plinth: spec.plinth,
    plinth_height_mm: spec.plinth_height_mm,
    divider_count: spec.divider_count,
    bay_width_ratios: spec.bay_width_ratios,
    shelf_height_ratios: spec.shelf_height_ratios,
    base_cabinet_height_mm: spec.base_cabinet_height_mm,
    base_cabinet_depth_mm: spec.base_cabinet_depth_mm,
    base_cabinet_count: spec.base_cabinet_count,
    edge_band_mm: spec.edge_band_mm,
    joint_system: spec.joint_system,
    reinforcement_mode: spec.reinforcement_mode,
    wall_anchor_required: spec.wall_anchor_required,
    wall_anchor_verified: false,
  };
}

export function toSourceProvenance(
  spec: DesignSpec,
  verifiedModelFingerprint = spec.reference_image_import?.verified_model_fingerprint,
): JsonRecord | undefined {
  const provenance = spec.reference_image_import;
  if (!provenance || !referenceImageVerificationIsCurrent(spec)) return undefined;
  return {
    source: provenance.source,
    import_id: provenance.import_id,
    image_sha256: provenance.image_sha256,
    file_name: provenance.file_name,
    image_width_px: provenance.image_width_px,
    image_height_px: provenance.image_height_px,
    confidence: provenance.confidence,
    detected_shelves: provenance.detected_shelves,
    detected_dividers: provenance.detected_dividers,
    detected_base_cabinets: provenance.detected_base_cabinets,
    warnings: provenance.warnings,
    verification_status: provenance.verification_status,
    confirmed_inputs: provenance.confirmed_inputs,
    verified_model_fingerprint: verifiedModelFingerprint,
  };
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status?: number,
    readonly code?: string,
    readonly solution?: string,
    readonly transportFailure = false,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

const MAX_PREVIEW_RESPONSE_CHARS = 4 * 1024 * 1024;
const MAX_PREVIEW_PARTS = 1_024;
const MAX_FEATURES_PER_PART = 256;
const MAX_TOTAL_FEATURES = 8_192;
const MAX_PREVIEW_RULES = 256;
const MAX_PREVIEW_BOM_LINES = 1_024;
const MAX_PREVIEW_CHANGE_DIFFS = 256;
const MAX_RULE_TRACE_STEPS = 128;
const MAX_RULE_INPUTS = 128;
const MAX_RULE_ACTIONS = 16;
const MAX_RULE_ACTION_CHANGES = 32;
const MAX_RESPONSE_STRING = 2_000;
const MAX_ARTIFACT_BYTES = 32 * 1024 * 1024;
const MAX_SIGNED_EVIDENCE_BYTES = 20 * 1024 * 1024;
const CANONICAL_UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
// The API derives a displayed centre from integer-micrometre placement and
// floors half of an odd-sized part. Reconstructing an AABB from that centre can
// therefore appear half a micrometre outside the exact integer envelope.
const SERVER_PART_HALF_UM_TOLERANCE_MM = 0.000_501;

function asRecord(value: unknown): JsonRecord | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : undefined;
}

const RELEASE_READ_KEYS = [
  "release_id",
  "release_number",
  "status",
  "bundle_sha256",
  "manifest_sha256",
  "release_kind",
  "machine_use",
  "physical_cutting_authorized",
] as const;

function strictReleaseRead(value: unknown, expectedReleaseNumber?: string): ReleaseRead {
  const payload = asRecord(value);
  if (
    !payload
    || Object.keys(payload).length !== RELEASE_READ_KEYS.length
    || RELEASE_READ_KEYS.some((key) => !Object.prototype.hasOwnProperty.call(payload, key))
    || typeof payload.release_id !== "string"
    || !CANONICAL_UUID_PATTERN.test(payload.release_id)
    || typeof payload.release_number !== "string"
    || !/^[A-Z0-9][A-Z0-9._-]{0,39}$/.test(payload.release_number)
    || (expectedReleaseNumber !== undefined && payload.release_number !== expectedReleaseNumber)
    || payload.status !== "released"
    || typeof payload.bundle_sha256 !== "string"
    || !/^[a-f0-9]{64}$/.test(payload.bundle_sha256)
    || typeof payload.manifest_sha256 !== "string"
    || !/^[a-f0-9]{64}$/.test(payload.manifest_sha256)
    || payload.machine_use !== "validation_only"
    || payload.release_kind !== "design_review"
    || payload.physical_cutting_authorized !== false
  ) {
    throw new ApiError("Servern returnerade ett ofullständigt frisläppningsbevis.");
  }
  return {
    release_id: payload.release_id,
    release_number: payload.release_number,
    status: payload.status,
    bundle_sha256: payload.bundle_sha256,
    manifest_sha256: payload.manifest_sha256,
    release_kind: payload.release_kind,
    machine_use: payload.machine_use,
    physical_cutting_authorized: false,
  };
}

function asNumber(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function asString(value: unknown, fallback: string): string {
  return typeof value === "string" && value.length > 0 ? value : fallback;
}

function sha256HexToBase64(sha256: string): string {
  const bytes = new Uint8Array(32);
  for (let index = 0; index < bytes.length; index += 1) {
    bytes[index] = Number.parseInt(sha256.slice(index * 2, index * 2 + 2), 16);
  }
  return btoa(String.fromCharCode(...bytes));
}

function boundedServerNumber(
  value: unknown,
  fallback: number,
  path: string,
  minimum: number,
  maximum: number,
  integer = false,
): number {
  const resolved = value === undefined ? fallback : value;
  if (
    typeof resolved !== "number"
    || !Number.isFinite(resolved)
    || resolved < minimum
    || resolved > maximum
    || (integer && !Number.isInteger(resolved))
  ) {
    throw new ApiError(`Servern returnerade ett ogiltigt värde för ${path}.`);
  }
  return resolved;
}

function boundedServerString(value: unknown, path: string, maximum = MAX_RESPONSE_STRING): string {
  if (typeof value !== "string" || value.length === 0 || value.length > maximum) {
    throw new ApiError(`Servern returnerade en ogiltig text för ${path}.`);
  }
  return value;
}

const STABLE_KEY_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]*$/;

function boundedStableKey(value: unknown, path: string, maximum: number): string {
  const resolved = boundedServerString(value, path, maximum);
  if (!STABLE_KEY_PATTERN.test(resolved)) {
    throw new ApiError(`Servern returnerade en ogiltig stabil nyckel för ${path}.`);
  }
  return resolved;
}

function boundedArray(value: unknown, path: string, maximum: number): unknown[] {
  if (!Array.isArray(value) || value.length > maximum) {
    throw new ApiError(`Servern returnerade en ogiltig eller för stor lista för ${path}.`);
  }
  return value;
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
      : rawKind === "rabbet"
        ? "rabbet"
        : rawKind === "groove"
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

function uiPartId(semanticKey: string, fallback: string): string {
  if (semanticKey === "left-side") return "side-left";
  if (semanticKey === "right-side") return "side-right";
  if (semanticKey === "back") return "back-panel";
  const backField = /^back-b(\d+)$/.exec(semanticKey);
  if (backField) return `back-panel-bay-${Number(backField[1]) + 1}`;
  if (semanticKey === "plinth") return "plinth-front";
  const divider = /^divider-(\d+)$/.exec(semanticKey);
  if (divider) return `divider-${Number(divider[1]) + 1}`;
  const shelf = /^shelf-r(\d+)-b(\d+)$/.exec(semanticKey);
  if (shelf) return `shelf-${Number(shelf[1]) + 1}-bay-${Number(shelf[2]) + 1}`;
  const oneBased = /^(base-side|base-bottom|base-top|cabinet-front)-(\d+)$/.exec(semanticKey);
  if (oneBased) return `${oneBased[1]}-${Number(oneBased[2]) + 1}`;
  if (semanticKey === "top" || semanticKey === "bottom") return semanticKey;
  return fallback;
}

function serverPartIdMap(value: unknown): Map<string, string> {
  const mapping = new Map<string, string>();
  if (!Array.isArray(value)) return mapping;
  value.forEach((raw) => {
    const part = asRecord(raw);
    if (!part) return;
    const serverId = asString(part.part_id, "");
    const semanticKey = asString(part.name, "");
    if (serverId) mapping.set(serverId, uiPartId(semanticKey, serverId));
  });
  return mapping;
}

function normalizeParts(value: unknown, fallback: ResolvedPart[], spec: DesignSpec): ResolvedPart[] {
  if (!Array.isArray(value) || value.length === 0) return fallback;
  return value.map((raw, index) => {
    const part = asRecord(raw) ?? {};
    const semanticKey = asString(part.name, "");
    const semanticPartId = uiPartId(semanticKey, "");
    const fallbackPart = fallback.find((candidate) => candidate.part_id === semanticPartId)
      ?? fallback[index]
      ?? fallback[0];
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
      rawKind === "divider" ||
      rawKind === "base_side" ||
      rawKind === "base_bottom" ||
      rawKind === "base_top" ||
      rawKind === "cabinet_front"
        ? rawKind
        : fallbackPart.kind;
    const rawOrientation = asString(part.orientation, fallbackPart.orientation);
    const orientation: ResolvedPart["orientation"] =
      rawOrientation === "XZ" || rawOrientation === "YZ" ? rawOrientation : "XY";
    return {
      part_id: semanticPartId || fallbackPart.part_id,
      name: fallbackPart.name,
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
      material_id: asString(
        part.material_id,
        fallbackPart.kind === "back" ? spec.back_material_id : spec.material_id,
      ),
      weight_kg: asNumber(part.weight_kg, fallbackPart.weight_kg),
      features: normalizeFeatures(part.features, asString(part.part_id, fallbackPart.part_id), fallbackPart.features),
    };
  });
}

function normalizeRules(
  value: unknown,
  fallback: RuleEvaluation[],
  partIds: Map<string, string> = new Map(),
): RuleEvaluation[] {
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
      : actionType === "align_base_cabinets"
        ? "align_base_cabinets"
      : actionType === "add_back_panel"
        ? "enable_back"
        : actionType === "verify_wall_anchor"
          ? "verify_wall_anchor"
          : suggested
            ? "manual_review"
            : undefined;
    const suggestedValue = mappedAction === "set_divider_count"
      ? asNumber(changes.find((change) => change.path === "parameters.vertical_divider_count")?.after, 1)
      : mappedAction === "align_base_cabinets"
        ? asNumber(changes.find((change) => change.path === "parameters.base_cabinet_count")?.after, 1)
      : mappedAction === "enable_back"
        ? true
        : false;
    const calculated = asNumber(rule.calculated_value, 0);
    const allowed = asNumber(rule.allowed_value, 0);
    const unit = asString(rule.unit, "");
    const status = asStatus(rule.status, "BLOCK");
    const suggestionDescription = suggested ? asString(suggested.description, "") : "";
    const diagnostics = Array.isArray(rule.inputs)
      ? rule.inputs
        .map((item) => asRecord(item))
        .filter((item): item is JsonRecord => Boolean(item))
        .map((item) => ({
          label: asString(item.name, "Kontrollvärde").replaceAll("_", " "),
          value: String(item.value ?? "—"),
          ...(typeof item.unit === "string" && item.unit ? { unit: item.unit } : {}),
        }))
      : [];
    return {
      rule_id: ruleId,
      rule_version: asString(rule.rule_version, "unknown"),
      status,
      title: asString(rule.title, ruleId),
      summary: status !== "PASS" && suggestionDescription
        ? suggestionDescription
        : `${asString(rule.title, ruleId)}: beräknat ${calculated} ${unit}, gränsvärde ${allowed} ${unit}.`,
      calculation: calculation || "Servern returnerade inget beräkningsspår.",
      calculated_value: calculated,
      allowed_value: allowed,
      unit,
      ...(typeof rule.safety_margin_permille === "number" ? { margin_percent: rule.safety_margin_permille / 10 } : {}),
      assumptions: Array.isArray(rule.assumptions)
        ? rule.assumptions.filter((item): item is string => typeof item === "string")
        : [],
      affected_part_ids: Array.isArray(rule.applies_to_part_ids)
        ? rule.applies_to_part_ids
          .filter((item): item is string => typeof item === "string")
          .map((item) => partIds.get(item) ?? item)
        : [],
      ...(diagnostics.length > 0 ? { diagnostics } : {}),
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

function serverRatioArray(
  value: unknown,
  fallback: number[],
  path: string,
  maximumLength: number,
): number[] {
  if (value === undefined) return fallback;
  return boundedArray(value, path, maximumLength).map((item, index) => (
    boundedServerNumber(item, 0, `${path}[${index}]`, 0, 1_000_000, true) / 1_000_000
  ));
}

export function designSpecFromServer(value: unknown, requested: DesignSpec): DesignSpec {
  const boundedRequested = parseLocalDesignSpec(requested);
  if (value === undefined) return boundedRequested;
  const root = asRecord(value);
  if (!root) throw new ApiError("Servern returnerade en ogiltig designspecifikation.");
  const parameters = asRecord(root.parameters);
  if (!parameters) throw new ApiError("Serverns designspecifikation saknar parametrar.");
  const material = root.material === undefined ? undefined : asRecord(root.material);
  if (root.material !== undefined && !material) {
    throw new ApiError("Serverns materialdefinition har ogiltigt format.");
  }
  const materialId = material?.material_id === undefined
    ? boundedRequested.material_id
    : boundedServerString(material.material_id, "spec.material.material_id", 64);
  if (materialId !== "mdf" && materialId !== "birch-plywood") {
    throw new ApiError(`Servern returnerade det okända materialet ${materialId}.`);
  }
  const materialDefinition = materialId === "birch-plywood" ? "Björkplywood" : "MDF";
  const joint = parameters.joint_system === undefined
    ? boundedRequested.joint_system
    : boundedServerString(parameters.joint_system, "spec.parameters.joint_system", 32);
  if (joint !== "dado") {
    throw new ApiError(
      `Servern returnerade förbandssystemet ${joint}, som inte stöds av produktions-MVP:n.`,
    );
  }
  const shelfMount = parameters.shelf_mount === undefined
    ? (boundedRequested.fixed_shelves ? "fixed" : "adjustable")
    : boundedServerString(parameters.shelf_mount, "spec.parameters.shelf_mount", 32);
  if (shelfMount !== "fixed" && shelfMount !== "adjustable") {
    throw new ApiError(`Servern returnerade det okända hyllmontaget ${shelfMount}.`);
  }
  if (parameters.back_panel === undefined) {
    throw new ApiError(
      "Serverns designspecifikation saknar en exakt bakstyckestyp.",
    );
  }
  const backPanel = boundedServerString(
    parameters.back_panel,
    "spec.parameters.back_panel",
    32,
  );
  if (backPanel !== "none" && backPanel !== "surface_mounted" && backPanel !== "inset_groove") {
    throw new ApiError(`Servern returnerade den okända bakstyckestypen ${backPanel}.`);
  }
  const rawBackMaterial = root.back_material;
  const backMaterial = rawBackMaterial === undefined || rawBackMaterial === null
    ? undefined
    : asRecord(rawBackMaterial);
  if (rawBackMaterial !== undefined && rawBackMaterial !== null && !backMaterial) {
    throw new ApiError("Serverns bakstyckesmaterial har ogiltigt format.");
  }
  if (backPanel === "none" && backMaterial) {
    throw new ApiError("Servern returnerade ett bakstyckesmaterial utan ett bakstycke.");
  }
  if (backPanel !== "none" && rawBackMaterial === null) {
    throw new ApiError("Serverns aktiva bakstycke saknar en materialdefinition.");
  }
  if (backPanel !== "none" && rawBackMaterial === undefined) {
    throw new ApiError(
      "Serverns aktiva bakstycke saknar en exakt materialdefinition.",
    );
  }
  if (backMaterial && backMaterial.material_id === undefined) {
    throw new ApiError("Serverns bakstyckesmaterial saknar en exakt materialdefinition.");
  }
  const backMaterialId = backMaterial
    ? boundedServerString(backMaterial.material_id, "spec.back_material.material_id", 64)
    : boundedRequested.back_material_id;
  if (backMaterialId !== "mdf-6" && backMaterialId !== "birch-plywood-6") {
    throw new ApiError(`Servern returnerade det okända bakstyckesmaterialet ${backMaterialId}.`);
  }
  const reinforcementMode = parameters.reinforcement_mode === undefined
    ? boundedRequested.reinforcement_mode
    : boundedServerString(parameters.reinforcement_mode, "spec.parameters.reinforcement_mode", 32);
  if (reinforcementMode !== "manual" && reinforcementMode !== "auto") {
    throw new ApiError(`Servern returnerade det okända förstärkningsläget ${reinforcementMode}.`);
  }
  const baseCabinetCount = boundedServerNumber(
    parameters.base_cabinet_count,
    boundedRequested.base_cabinet_count,
    "spec.parameters.base_cabinet_count",
    0,
    17,
    true,
  );
  const plinthHeightMm = boundedServerNumber(
    parameters.plinth_height_um,
    boundedRequested.plinth_height_mm * 1_000,
    "spec.parameters.plinth_height_um",
    0,
    500_000,
    true,
  ) / 1_000;
  const rawWallAnchor = parameters.wall_anchor;
  const wallAnchor = rawWallAnchor === undefined ? undefined : asRecord(rawWallAnchor);
  if (rawWallAnchor !== undefined && !wallAnchor) {
    throw new ApiError("Serverns väggförankring har ogiltigt format.");
  }
  let wallAnchorRequired = boundedRequested.wall_anchor_required;
  if (wallAnchor) {
    if (typeof wallAnchor.required !== "boolean") {
      throw new ApiError("Servern returnerade ett ogiltigt värde för spec.parameters.wall_anchor.required.");
    }
    if (typeof wallAnchor.verified !== "boolean") {
      throw new ApiError("Servern returnerade ett ogiltigt värde för spec.parameters.wall_anchor.verified.");
    }
    if (wallAnchor.verified) {
      throw new ApiError(
        "Serverns preview försökte återställa väggförankringsbevis i den redigerbara designspecifikationen.",
      );
    }
    wallAnchorRequired = wallAnchor.required;
  }
  const candidate: DesignSpec = {
    ...boundedRequested,
    furniture_type: baseCabinetCount > 0 ? "wall_library" : "bookcase",
    width_mm: boundedServerNumber(
      parameters.width_um,
      boundedRequested.width_mm * 1_000,
      "spec.parameters.width_um",
      250_000,
      6_000_000,
      true,
    ) / 1_000,
    height_mm: boundedServerNumber(
      parameters.height_um,
      boundedRequested.height_mm * 1_000,
      "spec.parameters.height_um",
      300_000,
      4_000_000,
      true,
    ) / 1_000,
    depth_mm: boundedServerNumber(
      parameters.depth_um,
      boundedRequested.depth_mm * 1_000,
      "spec.parameters.depth_um",
      100_000,
      1_200_000,
      true,
    ) / 1_000,
    material_id: materialId,
    material_name: material?.name === undefined
      ? materialDefinition
      : boundedServerString(material.name, "spec.material.name", 160),
    back_material_id: backMaterialId as BackMaterialId,
    nominal_thickness_mm: boundedServerNumber(
      parameters.nominal_thickness_um,
      boundedRequested.nominal_thickness_mm * 1_000,
      "spec.parameters.nominal_thickness_um",
      18_000,
      18_000,
      true,
    ) / 1_000,
    measured_thickness_mm: boundedServerNumber(
      parameters.actual_thickness_um,
      boundedRequested.measured_thickness_mm * 1_000,
      "spec.parameters.actual_thickness_um",
      17_000,
      19_000,
      true,
    ) / 1_000,
    measured_back_thickness_mm: boundedServerNumber(
      parameters.back_thickness_um,
      boundedRequested.measured_back_thickness_mm * 1_000,
      "spec.parameters.back_thickness_um",
      5_500,
      6_500,
      true,
    ) / 1_000,
    shelf_count: boundedServerNumber(
      parameters.shelf_count,
      boundedRequested.shelf_count,
      "spec.parameters.shelf_count",
      0,
      40,
      true,
    ),
    fixed_shelves: shelfMount === "fixed",
    load_per_shelf_kg: boundedServerNumber(
      parameters.shelf_load_n,
      boundedRequested.load_per_shelf_kg * 9.80665,
      "spec.parameters.shelf_load_n",
      0,
      500 * 9.80665,
    ) / 9.80665,
    back_panel: backPanel !== "none",
    back_panel_type: backPanel === "surface_mounted" ? "surface_mounted" : "inset_groove",
    plinth: plinthHeightMm > 0,
    plinth_height_mm: plinthHeightMm,
    divider_count: boundedServerNumber(
      parameters.vertical_divider_count,
      boundedRequested.divider_count,
      "spec.parameters.vertical_divider_count",
      0,
      16,
      true,
    ),
    bay_width_ratios: serverRatioArray(
      parameters.bay_width_ratios_ppm,
      boundedRequested.bay_width_ratios,
      "spec.parameters.bay_width_ratios_ppm",
      17,
    ),
    shelf_height_ratios: serverRatioArray(
      parameters.shelf_height_ratios_ppm,
      boundedRequested.shelf_height_ratios,
      "spec.parameters.shelf_height_ratios_ppm",
      40,
    ),
    base_cabinet_height_mm: boundedServerNumber(
      parameters.base_cabinet_height_um,
      boundedRequested.base_cabinet_height_mm * 1_000,
      "spec.parameters.base_cabinet_height_um",
      0,
      2_000_000,
      true,
    ) / 1_000,
    base_cabinet_depth_mm: boundedServerNumber(
      parameters.base_cabinet_depth_um,
      boundedRequested.base_cabinet_depth_mm * 1_000,
      "spec.parameters.base_cabinet_depth_um",
      0,
      1_200_000,
      true,
    ) / 1_000,
    base_cabinet_count: baseCabinetCount,
    reinforcement_mode: reinforcementMode,
    joint_system: joint,
    edge_band_mm: boundedServerNumber(
      parameters.edge_band_thickness_um,
      boundedRequested.edge_band_mm * 1_000,
      "spec.parameters.edge_band_thickness_um",
      0,
      5_000,
      true,
    ) / 1_000,
    wall_anchor_required: wallAnchorRequired,
    wall_anchor_verified: false,
  };
  return parseLocalDesignSpec(candidate);
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

function assertOptionalString(value: unknown, path: string, maximum = MAX_RESPONSE_STRING): void {
  if (value !== undefined && (typeof value !== "string" || value.length > maximum)) {
    throw new ApiError(`Servern returnerade en ogiltig text för ${path}.`);
  }
}

function assertOptionalNullableString(
  value: unknown,
  path: string,
  maximum = MAX_RESPONSE_STRING,
): void {
  if (value !== null) assertOptionalString(value, path, maximum);
}

function assertOptionalFinite(
  value: unknown,
  path: string,
  minimum = -1_000_000_000,
  maximum = 1_000_000_000,
): void {
  if (
    value !== undefined
    && (typeof value !== "number" || !Number.isFinite(value) || value < minimum || value > maximum)
  ) {
    throw new ApiError(`Servern returnerade ett ogiltigt tal för ${path}.`);
  }
}

function assertOptionalRuleInteger(value: unknown, path: string): void {
  if (value !== undefined && (typeof value !== "number" || !Number.isSafeInteger(value))) {
    throw new ApiError(`Servern returnerade ett ogiltigt tal för ${path}.`);
  }
}

function assertOptionalRuleScalar(value: unknown, path: string): void {
  if (value === undefined || value === null || typeof value === "boolean") return;
  if (typeof value === "string") {
    assertOptionalString(value, path);
    return;
  }
  assertOptionalRuleInteger(value, path);
}

function assertRecordList(value: unknown, path: string, maximum: number): JsonRecord[] {
  return boundedArray(value, path, maximum).map((item, index) => {
    const parsed = asRecord(item);
    if (!parsed) throw new ApiError(`Servern returnerade ett ogiltigt objekt i ${path}[${index}].`);
    return parsed;
  });
}

function assertPreviewResponseResources(response: JsonRecord): void {
  let serialized: string;
  try {
    serialized = JSON.stringify(response);
  } catch {
    throw new ApiError("Serverns previewsvar kunde inte avkodas säkert.");
  }
  if (serialized.length > MAX_PREVIEW_RESPONSE_CHARS) {
    throw new ApiError("Serverns previewsvar överskrider klientens resursgräns.");
  }
  assertOptionalString(response.design_hash, "design_hash", 128);
  assertOptionalString(response.status, "status", 32);

  let totalFeatures = 0;
  if (response.parts !== undefined) {
    const parts = assertRecordList(response.parts, "parts", MAX_PREVIEW_PARTS);
    for (const [index, part] of parts.entries()) {
      boundedServerString(part.part_id, `parts[${index}].part_id`, 128);
      boundedServerString(part.name, `parts[${index}].name`, 128);
      assertOptionalString(part.kind, `parts[${index}].kind`, 64);
      assertOptionalString(part.orientation, `parts[${index}].orientation`, 8);
      if (part.orientation !== "XY" && part.orientation !== "XZ" && part.orientation !== "YZ") {
        throw new ApiError(`Servern returnerade en ogiltig orientering för parts[${index}].`);
      }
      assertOptionalString(part.material_id, `parts[${index}].material_id`, 64);
      boundedServerNumber(part.width_mm, 0, `parts[${index}].width_mm`, 0.001, 6_000);
      boundedServerNumber(part.depth_mm, 0, `parts[${index}].depth_mm`, 0.001, 6_000);
      boundedServerNumber(part.thickness_mm, 0, `parts[${index}].thickness_mm`, 0.001, 100);
      assertOptionalFinite(part.weight_kg, `parts[${index}].weight_kg`, 0, 5_000);
      const position = asRecord(part.position_mm);
      if (!position) throw new ApiError(`Servern returnerade en ogiltig position för parts[${index}].`);
      boundedServerNumber(position.x, 0, `parts[${index}].position_mm.x`, 0, 6_000);
      boundedServerNumber(position.y, 0, `parts[${index}].position_mm.y`, 0, 1_200);
      boundedServerNumber(position.z, 0, `parts[${index}].position_mm.z`, 0, 4_000);
      if (part.features !== undefined) {
        const features = assertRecordList(
          part.features,
          `parts[${index}].features`,
          MAX_FEATURES_PER_PART,
        );
        totalFeatures += features.length;
        if (totalFeatures > MAX_TOTAL_FEATURES) {
          throw new ApiError("Serverns previewsvar innehåller för många bearbetningsfunktioner.");
        }
        for (const [featureIndex, feature] of features.entries()) {
          const featurePath = `parts[${index}].features[${featureIndex}]`;
          assertOptionalString(feature.id ?? feature.feature_id, `${featurePath}.id`, 160);
          assertOptionalString(feature.kind, `${featurePath}.kind`, 64);
          assertOptionalString(feature.face, `${featurePath}.face`, 16);
          assertOptionalString(feature.description, `${featurePath}.description`);
          assertOptionalFinite(feature.depth_mm, `${featurePath}.depth_mm`, 0, 100);
          assertOptionalFinite(feature.tool_diameter_mm, `${featurePath}.tool_diameter_mm`, 0, 100);
          if (feature.dimensions !== undefined) {
            const dimensions = asRecord(feature.dimensions);
            if (!dimensions) throw new ApiError(`Servern returnerade ogiltiga mått för ${featurePath}.`);
            assertOptionalFinite(dimensions.depth_um, `${featurePath}.dimensions.depth_um`, 0, 100_000);
          }
        }
      }
    }
  }

  if (response.rule_evaluations !== undefined) {
    const rules = assertRecordList(response.rule_evaluations, "rule_evaluations", MAX_PREVIEW_RULES);
    for (const [index, rule] of rules.entries()) {
      const path = `rule_evaluations[${index}]`;
      boundedServerString(rule.rule_id, `${path}.rule_id`, 160);
      for (const key of ["rule_version", "status", "title", "summary", "unit"] as const) {
        assertOptionalString(rule[key], `${path}.${key}`);
      }
      for (const key of ["calculated_value", "allowed_value", "safety_margin_permille"] as const) {
        assertOptionalRuleInteger(rule[key], `${path}.${key}`);
      }
      for (const [key, maximum] of [
        ["trace", MAX_RULE_TRACE_STEPS],
        ["inputs", MAX_RULE_INPUTS],
        ["suggested_actions", MAX_RULE_ACTIONS],
      ] as const) {
        if (rule[key] === undefined) continue;
        const entries = assertRecordList(rule[key], `${path}.${key}`, maximum);
        for (const [entryIndex, entry] of entries.entries()) {
          const entryPath = `${path}.${key}[${entryIndex}]`;
          for (const stringKey of ["expression", "unit", "name", "action_type", "description"] as const) {
            const fieldPath = `${entryPath}.${stringKey}`;
            if (stringKey === "unit" && (key === "trace" || key === "inputs")) {
              assertOptionalNullableString(entry[stringKey], fieldPath);
            } else {
              assertOptionalString(entry[stringKey], fieldPath);
            }
          }
          assertOptionalRuleScalar(entry.result, `${entryPath}.result`);
          if (key === "inputs") {
            const inputValue = entry.value;
            if (
              inputValue !== undefined
              && inputValue !== null
              && typeof inputValue !== "string"
              && typeof inputValue !== "number"
              && typeof inputValue !== "boolean"
            ) throw new ApiError(`Servern returnerade ett ogiltigt kontrollvärde för ${entryPath}.value.`);
            if (typeof inputValue === "string") assertOptionalString(inputValue, `${entryPath}.value`);
            if (typeof inputValue === "number") {
              assertOptionalRuleInteger(inputValue, `${entryPath}.value`);
            }
          }
          if (key === "suggested_actions" && entry.changes !== undefined) {
            const changes = assertRecordList(
              entry.changes,
              `${entryPath}.changes`,
              MAX_RULE_ACTION_CHANGES,
            );
            for (const [changeIndex, change] of changes.entries()) {
              assertOptionalString(change.path, `${entryPath}.changes[${changeIndex}].path`, 256);
              assertOptionalRuleScalar(change.before, `${entryPath}.changes[${changeIndex}].before`);
              assertOptionalRuleScalar(change.after, `${entryPath}.changes[${changeIndex}].after`);
            }
          }
        }
      }
      for (const [key, maximum] of [
        ["assumptions", 64],
        ["applies_to_part_ids", MAX_PREVIEW_PARTS],
        ["affected_part_ids", MAX_PREVIEW_PARTS],
      ] as const) {
        if (rule[key] === undefined) continue;
        const items = boundedArray(rule[key], `${path}.${key}`, maximum);
        items.forEach((item, itemIndex) => assertOptionalString(item, `${path}.${key}[${itemIndex}]`, 500));
      }
    }
  }

  if (response.bom !== undefined) {
    const lines = assertRecordList(response.bom, "bom", MAX_PREVIEW_BOM_LINES);
    for (const [index, line] of lines.entries()) {
      for (const key of ["id", "line_id", "item", "name", "unit", "dimensions", "material"] as const) {
        assertOptionalString(line[key], `bom[${index}].${key}`);
      }
      assertOptionalFinite(line.quantity, `bom[${index}].quantity`, 0, 100_000);
      if (line.part_ids !== undefined) {
        const ids = boundedArray(line.part_ids, `bom[${index}].part_ids`, MAX_PREVIEW_PARTS);
        ids.forEach((id, idIndex) => assertOptionalString(id, `bom[${index}].part_ids[${idIndex}]`, 128));
      }
    }
  }

  if (response.change_diff !== undefined) {
    const diffs = assertRecordList(response.change_diff, "change_diff", MAX_PREVIEW_CHANGE_DIFFS);
    for (const [index, diff] of diffs.entries()) {
      assertOptionalString(diff.explanation, `change_diff[${index}].explanation`);
      if (diff.changes === undefined) continue;
      const changes = assertRecordList(
        diff.changes,
        `change_diff[${index}].changes`,
        MAX_RULE_ACTION_CHANGES,
      );
      for (const [changeIndex, change] of changes.entries()) {
        assertOptionalString(change.path, `change_diff[${index}].changes[${changeIndex}].path`, 256);
        assertOptionalRuleScalar(change.before, `change_diff[${index}].changes[${changeIndex}].before`);
        assertOptionalRuleScalar(change.after, `change_diff[${index}].changes[${changeIndex}].after`);
      }
    }
  }
}

function assertExactServerPartSet(value: unknown, localParts: ResolvedPart[], spec: DesignSpec): void {
  if (value === undefined) return;
  const parts = value as JsonRecord[];
  const expected = new Set(localParts.map((part) => part.part_id));
  const seen = new Set<string>();
  const seenServerPartIds = new Set<string>();
  for (const [index, part] of parts.entries()) {
    const serverPartId = String(part.part_id);
    if (seenServerPartIds.has(serverPartId)) {
      throw new ApiError(`Serverns dellista innehåller duplicerat part_id vid parts[${index}].`);
    }
    seenServerPartIds.add(serverPartId);
    const semanticId = uiPartId(String(part.name), "");
    if (!semanticId || seen.has(semanticId) || !expected.has(semanticId)) {
      throw new ApiError(`Serverns dellista innehåller ett okänt eller duplicerat semantiskt ID vid parts[${index}].`);
    }
    seen.add(semanticId);
    const position = part.position_mm as JsonRecord;
    const orientation = String(part.orientation);
    const width = Number(part.width_mm);
    const depth = Number(part.depth_mm);
    const thickness = Number(part.thickness_mm);
    const extents = orientation === "YZ"
      ? { x: thickness, y: depth, z: width }
      : orientation === "XZ"
        ? { x: width, y: thickness, z: depth }
        : { x: width, y: depth, z: thickness };
    const limits = { x: spec.width_mm, y: spec.depth_mm, z: spec.height_mm };
    const outsideEnvelope = (["x", "y", "z"] as const).some((axis) => {
      const centre = Number(position[axis]);
      const size = extents[axis];
      return (
        !Number.isFinite(centre)
        || !Number.isFinite(size)
        || size <= 0
        || centre - size / 2 < -SERVER_PART_HALF_UM_TOLERANCE_MM
        || centre + size / 2 > limits[axis] + SERVER_PART_HALF_UM_TOLERANCE_MM
      );
    });
    if (outsideEnvelope) {
      throw new ApiError(`Serverdelen ${semanticId} ligger utanför den verifierade designrymden.`);
    }
  }
  if (seen.size !== expected.size || [...expected].some((partId) => !seen.has(partId))) {
    throw new ApiError("Serverns dellista motsvarar inte exakt den verifierade lokala topologin.");
  }
}

function assertExactObjectKeys(
  value: JsonRecord,
  expected: readonly string[],
  path: string,
): void {
  const actual = Object.keys(value).sort((left, right) => left.localeCompare(right));
  const canonical = [...expected].sort((left, right) => left.localeCompare(right));
  if (
    actual.length !== canonical.length
    || actual.some((key, index) => key !== canonical[index])
  ) {
    throw new ApiError(`Servern returnerade ett ogiltigt fältset för ${path}.`);
  }
}

function normalizeRetentionCertificationRequest(
  value: unknown,
  designHash: string,
  retentionTrustValue: unknown,
): RetentionCertificationRequest | undefined {
  if (value === undefined) return undefined;
  const root = asRecord(value);
  if (!root) {
    throw new ApiError("Serverns retention-certifieringsbegäran har ogiltigt format.");
  }
  assertExactObjectKeys(root, [
    "schema_version",
    "signed_evidence_schema_version",
    "application_class",
    "joint_geometry_fingerprint_schema",
    "source_design_hash",
    "joint_geometry_sha256",
    "engine_version",
    "template_version",
    "eligible_for_current_binding",
    "blocking_issue",
    "excluded_applications",
    "required_materials",
    "required_load_cases",
    "minimum_safety_factor_permille",
  ], "retention_certification_request");
  if (
    root.schema_version !== "custombuild.joint-retention-certification-request.v2"
    || root.signed_evidence_schema_version !== "custombuild.joint-retention-signed-evidence.v2"
    || root.application_class !== "load_bearing_carcass_dado"
    || root.joint_geometry_fingerprint_schema
      !== "custombuild.joint-retention-application-geometry.v1"
  ) {
    throw new ApiError("Serverns retention-certifieringsbegäran har en okänd kontraktsversion.");
  }
  let expectedSourceDesignHash = designHash;
  let trustedJointGeometrySha256: string | undefined;
  if (retentionTrustValue !== undefined) {
    const trust = asRecord(retentionTrustValue);
    if (!trust) {
      throw new ApiError("Serverns retentionbindning har ogiltigt format.");
    }
    assertExactObjectKeys(trust, [
      "schema_version",
      "application_class",
      "storage_evidence_id",
      "storage_evidence_sha256",
      "base_design_hash",
      "joint_geometry_sha256",
      "registry_sha256",
      "issuer_id",
      "key_id",
      "signed_evidence_id",
      "signed_evidence_expires_at",
      "system_id",
      "system_version",
      "contract_sha256",
    ], "retention_trust");
    if (
      trust.schema_version !== "custombuild.joint-retention-binding.v2"
      || trust.application_class !== "load_bearing_carcass_dado"
      || typeof trust.storage_evidence_id !== "string"
      || !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(trust.storage_evidence_id)
      || typeof trust.storage_evidence_sha256 !== "string"
      || !/^[a-f0-9]{64}$/.test(trust.storage_evidence_sha256)
      || typeof trust.base_design_hash !== "string"
      || !/^[a-f0-9]{64}$/.test(trust.base_design_hash)
      || typeof trust.joint_geometry_sha256 !== "string"
      || !/^[a-f0-9]{64}$/.test(trust.joint_geometry_sha256)
      || typeof trust.registry_sha256 !== "string"
      || !/^[a-f0-9]{64}$/.test(trust.registry_sha256)
      || typeof trust.contract_sha256 !== "string"
      || !/^[a-f0-9]{64}$/.test(trust.contract_sha256)
      || !["issuer_id", "key_id", "signed_evidence_id", "system_id", "system_version"]
        .every((field) => typeof trust[field] === "string" && String(trust[field]).length > 0)
      || typeof trust.signed_evidence_expires_at !== "string"
      || !/T.*(?:Z|[+-]\d{2}:\d{2})$/i.test(trust.signed_evidence_expires_at)
      || !Number.isFinite(Date.parse(trust.signed_evidence_expires_at))
    ) {
      throw new ApiError("Serverns retentionbindning har ogiltigt innehåll.");
    }
    expectedSourceDesignHash = trust.base_design_hash;
    trustedJointGeometrySha256 = trust.joint_geometry_sha256;
  }
  if (
    typeof root.source_design_hash !== "string"
    || !/^[a-f0-9]{64}$/.test(root.source_design_hash)
    || root.source_design_hash !== expectedSourceDesignHash
    || typeof root.joint_geometry_sha256 !== "string"
    || !/^[a-f0-9]{64}$/.test(root.joint_geometry_sha256)
    || (trustedJointGeometrySha256 !== undefined
      && root.joint_geometry_sha256 !== trustedJointGeometrySha256)
  ) {
    throw new ApiError("Serverns retention-certifieringsbegäran matchar inte aktuell designgeometri.");
  }
  boundedStableKey(root.engine_version, "retention_certification_request.engine_version", 80);
  boundedStableKey(root.template_version, "retention_certification_request.template_version", 80);
  if (typeof root.eligible_for_current_binding !== "boolean") {
    throw new ApiError("Serverns retention-certifieringsbegäran saknar bindningsstatus.");
  }
  if (
    root.blocking_issue !== null
    && root.blocking_issue !== "back_panel_capture_not_proven"
  ) {
    throw new ApiError("Serverns retention-certifieringsbegäran har ett okänt blockeringsskäl.");
  }
  if (
    (root.eligible_for_current_binding && root.blocking_issue !== null)
    || (!root.eligible_for_current_binding && root.blocking_issue === null)
  ) {
    throw new ApiError("Serverns retention-certifieringsbegäran har motsägande bindningsstatus.");
  }

  const excludedApplications = boundedArray(
    root.excluded_applications,
    "retention_certification_request.excluded_applications",
    8,
  );
  for (const [index, value] of excludedApplications.entries()) {
    const application = asRecord(value);
    const path = `retention_certification_request.excluded_applications[${index}]`;
    if (!application) throw new ApiError(`Servern returnerade ett ogiltigt objekt för ${path}.`);
    assertExactObjectKeys(application, [
      "application_class",
      "joint_count",
      "retention_basis",
      "capture_proven",
    ], path);
    const captive = application.application_class === "captive_inset_back_groove";
    const surface = application.application_class === "surface_mounted_back";
    if (
      (!captive && !surface)
      || (captive && application.retention_basis !== "canonical_four_boundary_geometric_capture")
      || (surface && application.retention_basis !== "independent_authenticated_evidence_required")
      || typeof application.capture_proven !== "boolean"
      || (surface && application.capture_proven)
    ) {
      throw new ApiError(`Servern returnerade en okänd retentionstillämpning för ${path}.`);
    }
    boundedServerNumber(application.joint_count, 0, `${path}.joint_count`, 0, 10_000, true);
  }

  const requiredMaterials = boundedArray(
    root.required_materials,
    "retention_certification_request.required_materials",
    8,
  );
  if (requiredMaterials.length < 1) {
    throw new ApiError("Serverns retention-certifieringsbegäran saknar materialkrav.");
  }
  const materialKeys = new Set<string>();
  for (const [index, value] of requiredMaterials.entries()) {
    const material = asRecord(value);
    const path = `retention_certification_request.required_materials[${index}]`;
    if (!material) throw new ApiError(`Servern returnerade ett ogiltigt objekt för ${path}.`);
    assertExactObjectKeys(material, [
      "material_id",
      "material_version",
      "actual_thickness_um",
    ], path);
    const materialId = boundedStableKey(material.material_id, `${path}.material_id`, 128);
    const materialVersion = boundedStableKey(
      material.material_version,
      `${path}.material_version`,
      80,
    );
    const materialKey = `${materialId}\u0000${materialVersion}`;
    if (materialKeys.has(materialKey)) {
      throw new ApiError("Serverns retention-certifieringsbegäran har duplicerade materialkrav.");
    }
    materialKeys.add(materialKey);
    boundedServerNumber(
      material.actual_thickness_um,
      0,
      `${path}.actual_thickness_um`,
      1,
      1_000_000,
      true,
    );
  }

  const requiredLoadCases = boundedArray(
    root.required_load_cases,
    "retention_certification_request.required_load_cases",
    2,
  );
  if (requiredLoadCases.length !== 2) {
    throw new ApiError("Serverns retention-certifieringsbegäran har ogiltiga lastfall.");
  }
  for (const [index, value] of requiredLoadCases.entries()) {
    const loadCase = asRecord(value);
    const path = `retention_certification_request.required_load_cases[${index}]`;
    if (!loadCase) throw new ApiError(`Servern returnerade ett ogiltigt objekt för ${path}.`);
    assertExactObjectKeys(loadCase, ["mode", "rated_design_load_n"], path);
    if (loadCase.mode !== (["shear", "withdrawal"] as const)[index]) {
      throw new ApiError("Serverns retention-certifieringsbegäran har ogiltiga lastfall.");
    }
    boundedServerNumber(
      loadCase.rated_design_load_n,
      0,
      `${path}.rated_design_load_n`,
      1,
      1_000_000,
      true,
    );
  }
  boundedServerNumber(
    root.minimum_safety_factor_permille,
    0,
    "retention_certification_request.minimum_safety_factor_permille",
    1_000,
    5_000,
    true,
  );
  return root as unknown as RetentionCertificationRequest;
}

export function normalizePreviewResponse(payload: unknown, requestedSpec: DesignSpec): ResolvedDesign {
  const response = asRecord(payload);
  if (!response) throw new ApiError("Servern returnerade ett svar i okänt format.");
  assertPreviewResponseResources(response);
  const serverSpec = designSpecFromServer(response.spec, requestedSpec);
  const local = resolveDesign(serverSpec);
  assertExactServerPartSet(response.parts, local.parts, serverSpec);
  const rules = normalizeRules(
    response.rule_evaluations,
    local.rule_evaluations,
    serverPartIdMap(response.parts),
  );
  const status = rules.some((rule) => rule.status === "BLOCK")
    ? "BLOCK"
    : rules.some((rule) => rule.status === "WARNING")
      ? "WARNING"
      : asStatus(response.status, "PASS");
  const designHash = asString(response.design_hash, local.design_hash);
  const retentionCertificationRequest = normalizeRetentionCertificationRequest(
    response.retention_certification_request,
    designHash,
    response.retention_trust,
  );
  return {
    ...local,
    design_hash: designHash,
    parts: normalizeParts(response.parts, local.parts, serverSpec),
    bom: normalizeBom(response.bom, local.bom),
    rule_evaluations: rules,
    status,
    change_diff: normalizeChangeDiff(response.change_diff),
    source: "server-preview",
    ...(retentionCertificationRequest
      ? { retention_certification_request: retentionCertificationRequest }
      : {}),
  };
}

export class CustombuildApiClient {
  readonly baseUrl: string | undefined;
  private readonly jointRetentionEvidenceByProject = new Map<string, string>();

  constructor(
    baseUrl?: string,
    private readonly explicitToken?: string,
    private readonly developmentToken?: string,
  ) {
    this.baseUrl = baseUrl?.replace(/\/$/, "") || undefined;
  }

  get configured(): boolean {
    return Boolean(this.baseUrl);
  }

  get authenticated(): boolean {
    return Boolean(this.accessToken());
  }

  setJointRetentionEvidence(projectId: string, evidenceId?: string): void {
    const normalizedProjectId = projectId.trim();
    if (!normalizedProjectId) {
      throw new ApiError("Projektidentiteten saknas för retentionsevidensen.");
    }
    if (evidenceId === undefined) {
      this.jointRetentionEvidenceByProject.delete(normalizedProjectId);
      return;
    }
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(evidenceId)) {
      throw new ApiError("Retentionsevidensen har en ogiltig serveridentitet.");
    }
    this.jointRetentionEvidenceByProject.set(normalizedProjectId, evidenceId);
  }

  private jointRetentionEvidence(projectId?: string): string | undefined {
    return projectId ? this.jointRetentionEvidenceByProject.get(projectId) : undefined;
  }

  private accessToken(): string | undefined {
    return this.explicitToken
      ?? getStoredAccessToken()
      ?? this.developmentToken;
  }

  private artifactDownloadUrl(artifact: Pick<ArtifactRead, "id" | "download_path">): URL {
    if (!this.baseUrl) {
      throw new ApiError("API-adress saknas. Artefakten kan inte hämtas verifierat.");
    }
    const id = artifact.id;
    const path = artifact.download_path;
    if (
      typeof id !== "string"
      || id.length === 0
      || typeof path !== "string"
      || !path.startsWith("/")
      || path.startsWith("//")
      || /[\\\u0000-\u0020\u007f]/.test(path)
    ) {
      throw new ApiError("Servern returnerade en ogiltig signerad artefaktsökväg.");
    }

    let apiOrigin: URL;
    let resolved: URL;
    try {
      apiOrigin = new URL(this.baseUrl);
      resolved = new URL(path, apiOrigin);
    } catch {
      throw new ApiError("Servern returnerade en ogiltig signerad artefaktsökväg.");
    }
    const expectedPath = `/v1/artifacts/${encodeURIComponent(id)}/download`;
    const queryKeys = [...resolved.searchParams.keys()];
    const expiresValues = resolved.searchParams.getAll("expires");
    const signatureValues = resolved.searchParams.getAll("signature");
    if (
      resolved.origin !== apiOrigin.origin
      || resolved.username !== ""
      || resolved.password !== ""
      || resolved.hash !== ""
      || resolved.pathname !== expectedPath
      || queryKeys.length !== 2
      || new Set(queryKeys).size !== 2
      || expiresValues.length !== 1
      || signatureValues.length !== 1
      || !/^[1-9][0-9]{0,15}$/.test(expiresValues[0] ?? "")
      || !Number.isSafeInteger(Number(expiresValues[0]))
      || !/^[a-f0-9]{64}$/.test(signatureValues[0] ?? "")
    ) {
      throw new ApiError("Servern returnerade en ogiltig signerad artefaktsökväg.");
    }
    return resolved;
  }

  private async request<ResponseBody>(path: string, options: RequestInit): Promise<ResponseBody> {
    if (!this.baseUrl) throw new ApiError("API-adress saknas. Lokal deterministisk förhandsvisning används.");
    const token = this.accessToken();
    if (!token) throw new ApiError("Logga in för att använda den serverauktoritativa arbetsytan.", 401);
    let response: Response;
    const multipart = typeof FormData !== "undefined" && options.body instanceof FormData;
    try {
      response = await fetch(`${this.baseUrl}${path}`, {
        ...options,
        headers: {
          Accept: "application/json",
          ...(multipart ? {} : { "Content-Type": "application/json" }),
          Authorization: `Bearer ${token}`,
          ...options.headers,
        },
      });
    } catch {
      throw new ApiError(
        "Kunde inte nå konstruktions-API:t. Lokal förhandsvisning är fortfarande aktiv.",
        undefined,
        "API_TRANSPORT_FAILURE",
        undefined,
        true,
      );
    }
    if (!response.ok) {
      let detail = response.statusText;
      let errorCode: string | undefined;
      let solution: string | undefined;
      try {
        const body = (await response.json()) as { detail?: unknown };
        if (typeof body.detail === "string") detail = body.detail;
        else if (Array.isArray(body.detail)) {
          const messages = body.detail
            .map((item) => asRecord(item))
            .filter((item): item is JsonRecord => Boolean(item))
            .map((item) => {
              const location = Array.isArray(item.loc) ? item.loc.map(String).join(" → ") : "request";
              return `${location}: ${asString(item.msg, "ogiltigt värde")}`;
            });
          if (messages.length > 0) detail = messages.join("; ");
        } else {
          const structuredDetail = asRecord(body.detail);
          if (structuredDetail) {
            detail = asString(structuredDetail.message, detail);
            errorCode = typeof structuredDetail.code === "string" ? structuredDetail.code : undefined;
            solution = typeof structuredDetail.solution === "string" ? structuredDetail.solution : undefined;
          }
        }
      } catch {
        // Keep the HTTP status text if the body is not JSON.
      }
      const solutionSuffix = solution ? ` Lösning: ${solution}` : "";
      throw new ApiError(`API ${response.status}: ${detail}${solutionSuffix}`, response.status, errorCode, solution);
    }
    try {
      return await response.json() as ResponseBody;
    } catch {
      throw new ApiError(
        "Servern returnerade ett ogiltigt JSON-svar.",
        response.status,
        "INVALID_SERVER_RESPONSE",
      );
    }
  }

  async listProjects(): Promise<ProjectRead[]> {
    return this.request<ProjectRead[]>("/v1/projects", { method: "GET" });
  }

  async getCurrentPrincipal(): Promise<CurrentPrincipal> {
    return this.request<CurrentPrincipal>("/v1/me", { method: "GET" });
  }

  async createProject(name: string): Promise<ProjectRead> {
    return this.request<ProjectRead>("/v1/projects", {
      method: "POST",
      body: JSON.stringify({ name, furniture_type: "bookcase" }),
    });
  }

  async inspectReferenceImage(projectId: string, file: File): Promise<ImportInspection> {
    const form = new FormData();
    form.append("document", file, file.name);
    return this.request<ImportInspection>(
      `/v1/projects/${encodeURIComponent(projectId)}/imports/inspect`,
      { method: "POST", body: form },
    );
  }

  async listExternalEvidence(projectId: string): Promise<ExternalEvidenceRead[]> {
    return this.request<ExternalEvidenceRead[]>(
      `/v1/projects/${encodeURIComponent(projectId)}/evidence`,
      { method: "GET" },
    );
  }

  async uploadExternalEvidence(
    projectId: string,
    input: ExternalEvidenceUploadInput,
  ): Promise<ExternalEvidenceRead> {
    const form = new FormData();
    form.append("document", input.document, input.document.name);
    form.append("evidence_type", input.evidenceType);
    form.append("rule_id", input.ruleId);
    form.append("catalog_id", input.catalogId.trim());
    form.append("catalog_version", input.catalogVersion.trim());
    form.append("design_hash", input.designHash);
    if (input.expiresAt) form.append("expires_at", input.expiresAt);
    return this.request<ExternalEvidenceRead>(
      `/v1/projects/${encodeURIComponent(projectId)}/evidence`,
      { method: "POST", body: form },
    );
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

  async getProjectDraft(projectId: string): Promise<ProjectDraftRead> {
    const payload = await this.request<unknown>(
      `/v1/projects/${encodeURIComponent(projectId)}/draft`,
      { method: "GET" },
    );
    const draft = asRecord(payload);
    if (!draft || draft.project_id !== projectId) {
      throw new ApiError(
        "Serverutkastet tillhör inte det uttryckligen begärda projektet.",
        200,
        "INVALID_SERVER_DRAFT",
      );
    }
    const draftRevision = draft.draft_revision;
    const templateId = draft.template_id;
    const designHash = draft.design_hash;
    const specJson = draft.spec_json === null ? null : asRecord(draft.spec_json);
    const workspaceSpecJson =
      draft.workspace_spec_json === null ? null : asRecord(draft.workspace_spec_json);
    const resultJson = draft.result_json === null ? null : asRecord(draft.result_json);
    const updatedAt = draft.updated_at;
    if (
      typeof draftRevision !== "number" ||
      !Number.isInteger(draftRevision) ||
      draftRevision < 0 ||
      (templateId !== null && typeof templateId !== "string") ||
      (designHash !== null && typeof designHash !== "string") ||
      specJson === undefined ||
      workspaceSpecJson === undefined ||
      resultJson === undefined ||
      typeof updatedAt !== "string"
    ) {
      throw new ApiError(
        "Serverutkastet har ett ogiltigt format.",
        200,
        "INVALID_SERVER_DRAFT",
      );
    }

    return {
      project_id: projectId,
      draft_revision: draftRevision,
      template_id: templateId,
      design_hash: designHash,
      spec_json: specJson,
      workspace_spec_json: workspaceSpecJson,
      result_json: resultJson,
      updated_at: updatedAt,
    };
  }

  async updateProjectDraft(
    projectId: string,
    templateId: string,
    spec: DesignSpec,
    expectedDraftRevision: number,
  ): Promise<ProjectDraftRead> {
    return this.request<ProjectDraftRead>(
      `/v1/projects/${encodeURIComponent(projectId)}/draft`,
      {
        method: "PUT",
        body: JSON.stringify({
          expected_draft_revision: expectedDraftRevision,
          template_id: templateId,
          spec: toPreviewRequest(spec),
          workspace_spec: workspaceIntentEnvelopeFromSpec(spec),
        }),
      },
    );
  }

  async listVersions(projectId: string): Promise<DesignVersionRead[]> {
    return this.request<DesignVersionRead[]>(
      `/v1/projects/${encodeURIComponent(projectId)}/versions`,
      { method: "GET" },
    );
  }

  async getProductionState(projectId: string): Promise<ProductionStateRead> {
    const payload = await this.request<unknown>(
      `/v1/projects/${encodeURIComponent(projectId)}/production-state`,
      { method: "GET" },
    );
    const state = asRecord(payload);
    if (!state || !Object.prototype.hasOwnProperty.call(state, "release")) {
      throw new ApiError("Serverns produktionsstatus saknar ett entydigt frisläppningsfält.");
    }
    if (state.release !== null) {
      return {
        ...(payload as ProductionStateRead),
        release: strictReleaseRead(state.release),
      };
    }
    return payload as ProductionStateRead;
  }

  async createVersion(
    projectId: string,
    spec: DesignSpec,
    expectedDesignHash: string,
    expectedCurrentRevision: number,
    templateId: FurnitureTemplateId,
    jointRetentionEvidenceId = this.jointRetentionEvidence(projectId),
  ): Promise<DesignVersionRead> {
    const sourceProvenance = toSourceProvenance(spec, expectedDesignHash);
    return this.request<DesignVersionRead>(
      `/v1/projects/${encodeURIComponent(projectId)}/versions`,
      {
        method: "POST",
        body: JSON.stringify({
          spec: toPreviewRequest(spec),
          production_context: productionContextFromSpec(spec),
          expected_design_hash: expectedDesignHash,
          expected_current_revision: expectedCurrentRevision,
          template_id: templateId,
          ...(jointRetentionEvidenceId
            ? { joint_retention_evidence_id: jointRetentionEvidenceId }
            : {}),
          ...(sourceProvenance ? { source_provenance: sourceProvenance } : {}),
        }),
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

  async getJob(jobId: string, signal?: AbortSignal): Promise<JobRead> {
    return this.request<JobRead>(`/v1/jobs/${encodeURIComponent(jobId)}`, {
      method: "GET",
      signal,
    });
  }

  async listArtifacts(jobId: string, signal?: AbortSignal): Promise<ArtifactRead[]> {
    const payload = await this.request<JsonRecord[]>(
      `/v1/jobs/${encodeURIComponent(jobId)}/artifacts`,
      { method: "GET", signal },
    );
    return payload.map((artifact) => {
      const id = asString(artifact.id, "");
      const sha256 = asString(artifact.sha256, "");
      const sizeBytes = asNumber(artifact.size_bytes, -1);
      const contentType = asString(artifact.content_type, "");
      const downloadPath = asString(artifact.download_path, "");
      if (
        !id
        || !/^[a-f0-9]{64}$/.test(sha256)
        || !Number.isSafeInteger(sizeBytes)
        || sizeBytes <= 0
        || sizeBytes > MAX_ARTIFACT_BYTES
        || !/^[a-z0-9][a-z0-9!#$&^_.+-]*\/[a-z0-9][a-z0-9!#$&^_.+-]*$/.test(contentType)
      ) {
        throw new ApiError("Servern returnerade en ogiltig artefaktlänk.");
      }
      this.artifactDownloadUrl({ id, download_path: downloadPath });
      return {
        id,
        kind: asString(artifact.kind, "unknown"),
        sha256,
        size_bytes: sizeBytes,
        content_type: contentType,
        // Never expose the untrusted external download_url returned for non-web
        // clients. Web downloads always use the authenticated API path with an
        // expiring access signature.
        download_url: downloadPath,
        download_path: downloadPath,
      };
    });
  }

  async downloadArtifact(artifact: ArtifactRead, signal?: AbortSignal): Promise<Blob> {
    const token = this.accessToken();
    if (!token) {
      throw new ApiError("Logga in för att hämta det verifierade granskningspaketet.", 401);
    }
    if (
      !/^[a-f0-9]{64}$/.test(artifact.sha256)
      || !Number.isSafeInteger(artifact.size_bytes)
      || artifact.size_bytes <= 0
      || artifact.size_bytes > MAX_ARTIFACT_BYTES
      || !/^[a-z0-9][a-z0-9!#$&^_.+-]*\/[a-z0-9][a-z0-9!#$&^_.+-]*$/.test(
        artifact.content_type,
      )
    ) {
      throw new ApiError("Artefaktens verifieringsmetadata är ogiltig.");
    }
    const downloadUrl = this.artifactDownloadUrl(artifact);
    let response: Response;
    try {
      response = await fetch(downloadUrl.href, {
        method: "GET",
        headers: {
          Accept: artifact.content_type,
          Authorization: `Bearer ${token}`,
        },
        cache: "no-store",
        redirect: "error",
        signal,
      });
    } catch {
      throw new ApiError(
        "Kunde inte hämta granskningspaketet via den verifierade API-kanalen.",
        undefined,
        "ARTIFACT_DOWNLOAD_TRANSPORT_FAILURE",
        undefined,
        true,
      );
    }

    let responseUrl: URL;
    try {
      responseUrl = new URL(response.url);
    } catch {
      throw new ApiError("Servern returnerade ett ogiltigt nedladdningssvar.");
    }
    if (
      response.status !== 200
      || response.redirected
      || responseUrl.origin !== downloadUrl.origin
      || responseUrl.href !== downloadUrl.href
    ) {
      throw new ApiError(
        response.status === 200
          ? "Servern försökte flytta artefakthämtningen utanför den verifierade API-kanalen."
          : `Artefakthämtningen misslyckades (HTTP ${response.status}).`,
        response.status,
      );
    }

    const contentLength = response.headers.get("Content-Length");
    const contentType = response.headers.get("Content-Type");
    const digest = response.headers.get("Digest");
    const etag = response.headers.get("ETag");
    const expectedDigest = `sha-256=${sha256HexToBase64(artifact.sha256)}`;
    if (
      contentLength === null
      || !/^[1-9][0-9]*$/.test(contentLength)
      || !Number.isSafeInteger(Number(contentLength))
      || Number(contentLength) !== artifact.size_bytes
      || contentType !== artifact.content_type
      || digest !== expectedDigest
      || etag !== `"${artifact.sha256}"`
    ) {
      throw new ApiError(
        "Artefaktens svarshuvuden matchar inte den serverauktoritativa inventeringen.",
      );
    }

    let body: ArrayBuffer;
    try {
      body = await response.arrayBuffer();
    } catch {
      throw new ApiError("Artefaktens svarskropp kunde inte läsas fullständigt.");
    }
    if (body.byteLength !== artifact.size_bytes) {
      throw new ApiError(
        "Artefaktens faktiska storlek matchar inte den serverauktoritativa inventeringen.",
      );
    }
    let actualSha256: string;
    try {
      const digestBytes = await crypto.subtle.digest("SHA-256", body);
      actualSha256 = [...new Uint8Array(digestBytes)]
        .map((value) => value.toString(16).padStart(2, "0"))
        .join("");
    } catch {
      throw new ApiError("Webbläsaren kunde inte verifiera artefaktens SHA-256.");
    }
    if (actualSha256 !== artifact.sha256) {
      throw new ApiError("Artefaktens innehåll matchar inte den förväntade SHA-256-identiteten.");
    }
    return new Blob([body], { type: artifact.content_type });
  }

  async downloadJointRetentionEvidence(
    projectId: string,
    evidence: ExternalEvidenceRead,
    signal?: AbortSignal,
  ): Promise<Blob> {
    const token = this.accessToken();
    if (!token) {
      throw new ApiError("Logga in för att hämta den signerade retentionsevidensen.", 401);
    }
    if (!this.baseUrl) {
      throw new ApiError("API-adress saknas. Retentionsevidensen kan inte hämtas verifierat.");
    }
    if (
      !CANONICAL_UUID_PATTERN.test(projectId)
      || !CANONICAL_UUID_PATTERN.test(evidence.id)
      || evidence.project_id !== projectId
      || evidence.evidence_type !== "joint_retention"
      || evidence.rule_id !== "CB-JOINT-001"
      || evidence.content_type !== "application/json"
      || !/^[a-f0-9]{64}$/.test(evidence.sha256)
      || !Number.isSafeInteger(evidence.size_bytes)
      || evidence.size_bytes <= 0
      || evidence.size_bytes > MAX_SIGNED_EVIDENCE_BYTES
    ) {
      throw new ApiError("Retentionsevidensens servermetadata är ogiltig.");
    }

    const downloadUrl = new URL(
      `/v1/projects/${encodeURIComponent(projectId)}/evidence/${encodeURIComponent(evidence.id)}/download`,
      this.baseUrl,
    );
    let response: Response;
    try {
      response = await fetch(downloadUrl.href, {
        method: "GET",
        headers: {
          Accept: "application/json",
          Authorization: `Bearer ${token}`,
        },
        cache: "no-store",
        redirect: "error",
        signal,
      });
    } catch {
      throw new ApiError(
        "Kunde inte hämta retentionsevidensen via den verifierade API-kanalen.",
        undefined,
        "RETENTION_EVIDENCE_DOWNLOAD_TRANSPORT_FAILURE",
        undefined,
        true,
      );
    }

    let responseUrl: URL;
    try {
      responseUrl = new URL(response.url);
    } catch {
      throw new ApiError("Servern returnerade ett ogiltigt evidenssvar.");
    }
    if (
      response.status !== 200
      || response.redirected
      || responseUrl.origin !== downloadUrl.origin
      || responseUrl.href !== downloadUrl.href
    ) {
      throw new ApiError(
        response.status === 200
          ? "Servern försökte flytta evidenshämtningen utanför den verifierade API-kanalen."
          : `Evidenshämtningen misslyckades (HTTP ${response.status}).`,
        response.status,
      );
    }

    const expectedFilename = `custombuild-joint-retention-${evidence.id}.json`;
    const expectedDigest = `sha-256=${sha256HexToBase64(evidence.sha256)}`;
    const contentLength = response.headers.get("Content-Length");
    if (
      contentLength === null
      || !/^[1-9][0-9]*$/.test(contentLength)
      || !Number.isSafeInteger(Number(contentLength))
      || Number(contentLength) !== evidence.size_bytes
      || response.headers.get("Content-Type") !== evidence.content_type
      || response.headers.get("Content-Disposition") !== `attachment; filename="${expectedFilename}"`
      || response.headers.get("Digest") !== expectedDigest
      || response.headers.get("ETag") !== `"${evidence.sha256}"`
      || response.headers.get("Cache-Control") !== "private, no-store, no-transform, max-age=0"
      || response.headers.get("Pragma") !== "no-cache"
      || response.headers.get("X-Content-Type-Options") !== "nosniff"
    ) {
      throw new ApiError(
        "Retentionsevidensens svarshuvuden matchar inte serverregistret.",
      );
    }

    let body: ArrayBuffer;
    try {
      body = await response.arrayBuffer();
    } catch {
      throw new ApiError("Retentionsevidensens svarskropp kunde inte läsas fullständigt.");
    }
    if (body.byteLength !== evidence.size_bytes) {
      throw new ApiError(
        "Retentionsevidensens faktiska storlek matchar inte serverregistret.",
      );
    }
    let actualSha256: string;
    try {
      const digestBytes = await crypto.subtle.digest("SHA-256", body);
      actualSha256 = [...new Uint8Array(digestBytes)]
        .map((value) => value.toString(16).padStart(2, "0"))
        .join("");
    } catch {
      throw new ApiError("Webbläsaren kunde inte verifiera retentionsevidensens SHA-256.");
    }
    if (actualSha256 !== evidence.sha256) {
      throw new ApiError(
        "Retentionsevidensens innehåll matchar inte den registrerade SHA-256-identiteten.",
      );
    }
    return new Blob([body], { type: evidence.content_type });
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
    return strictReleaseRead(payload, releaseNumber);
  }

  async previewDesign(
    spec: DesignSpec,
    signal?: AbortSignal,
    projectId?: string,
  ): Promise<ResolvedDesign> {
    const requestBody = toPreviewRequest(spec);
    const query = new URLSearchParams();
    if (projectId) query.set("project_id", projectId);
    const jointRetentionEvidenceId = this.jointRetentionEvidence(projectId);
    if (jointRetentionEvidenceId) {
      query.set("joint_retention_evidence_id", jointRetentionEvidenceId);
    }
    const path = `/v1/designs/preview${query.size > 0 ? `?${query.toString()}` : ""}`;
    const payload = await this.request<PreviewResponseBody>(path, {
      method: "POST",
      body: JSON.stringify(requestBody),
      signal,
    });
    return normalizePreviewResponse(payload, spec);
  }

  async autofixDesign(
    spec: DesignSpec,
    signal?: AbortSignal,
    projectId?: string,
  ): Promise<ResolvedDesign> {
    const requestBody: AutofixRequestBody = toPreviewRequest(spec);
    const path = projectId
      ? `/v1/designs/autofix?project_id=${encodeURIComponent(projectId)}`
      : "/v1/designs/autofix";
    const payload = await this.request<AutofixResponseBody>(path, {
      method: "POST",
      body: JSON.stringify(requestBody),
      signal,
    });
    const autofixed = normalizePreviewResponse(payload, spec);
    // Retention has a dedicated, server-verifiable preview boundary. The
    // autofix endpoint intentionally accepts no retention identity, so replay
    // the normalized result through preview when a reviewer selected one.
    if (this.jointRetentionEvidence(projectId)) {
      return this.previewDesign(autofixed.spec, signal, projectId);
    }
    return autofixed;
  }
}

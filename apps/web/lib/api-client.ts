import { resolveDesign } from "./design-engine";
import { getStoredAccessToken } from "./auth-client";
import { referenceImageVerificationIsCurrent } from "./reference-image";
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
import type { FurnitureTemplateId } from "./furniture-templates";
import {
  parseLocalDesignSpec,
  workspaceIntentEnvelopeFromSpec,
} from "./workspace-design-envelope";

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

export interface RevisionProductionContextSnapshot {
  stock_width_mm: number;
  stock_height_mm: number;
  stock_count: number;
  back_stock_width_mm: number;
  back_stock_height_mm: number;
  back_stock_count: number;
  machine_profile_id: string;
}

export function productionContextFromSpec(spec: DesignSpec): RevisionProductionContextSnapshot {
  return {
    stock_width_mm: spec.stock_width_mm,
    stock_height_mm: spec.stock_height_mm,
    stock_count: spec.stock_count,
    back_stock_width_mm: spec.back_stock_width_mm,
    back_stock_height_mm: spec.back_stock_height_mm,
    back_stock_count: spec.back_stock_count,
    machine_profile_id: spec.machine_profile_id,
  };
}

export function versionProductionContextMatches(
  version: DesignVersionRead,
  spec: DesignSpec,
): boolean {
  const result = asRecord(version.result_json);
  const frozen = asRecord(result?.production_context);
  if (!frozen) return false;
  const current = productionContextFromSpec(spec);
  const expectedKeys = Object.keys(current).sort();
  const frozenKeys = Object.keys(frozen).sort();
  if (
    frozenKeys.length !== expectedKeys.length
    || frozenKeys.some((key, index) => key !== expectedKeys[index])
  ) return false;
  return Object.entries(current).every(([key, value]) => (
    typeof frozen[key] === typeof value && frozen[key] === value
  ));
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
    nominal_thickness_mm: spec.nominal_thickness_mm,
    measured_thickness_mm: spec.measured_thickness_mm,
    shelf_count: spec.shelf_count,
    shelf_mount: spec.fixed_shelves ? "fixed" : "adjustable",
    load_per_shelf_kg: spec.load_per_shelf_kg,
    back_panel: spec.back_panel,
    plinth: spec.plinth,
    divider_count: spec.divider_count,
    bay_width_ratios: spec.bay_width_ratios,
    shelf_height_ratios: spec.shelf_height_ratios,
    base_cabinet_height_mm: spec.base_cabinet_height_mm,
    base_cabinet_depth_mm: spec.base_cabinet_depth_mm,
    base_cabinet_count: spec.base_cabinet_count,
    edge_band_mm: spec.edge_band_mm,
    joint_system: spec.joint_system,
    reinforcement_mode: spec.reinforcement_mode,
    wall_anchor_required: false,
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
// The API derives a displayed centre from integer-micrometre placement and
// floors half of an odd-sized part. Reconstructing an AABB from that centre can
// therefore appear half a micrometre outside the exact integer envelope.
const SERVER_PART_HALF_UM_TOLERANCE_MM = 0.000_501;

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

function uiPartId(semanticKey: string, fallback: string): string {
  if (semanticKey === "left-side") return "side-left";
  if (semanticKey === "right-side") return "side-right";
  if (semanticKey === "back") return "back-panel";
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
      material_id: asString(part.material_id, spec.material_id),
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
  const backPanel = parameters.back_panel === undefined
    ? (boundedRequested.back_panel ? "inset_groove" : "none")
    : boundedServerString(parameters.back_panel, "spec.parameters.back_panel", 32);
  if (backPanel !== "none" && backPanel !== "surface_mounted" && backPanel !== "inset_groove") {
    throw new ApiError(`Servern returnerade den okända bakstyckestypen ${backPanel}.`);
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
    plinth: boundedServerNumber(
      parameters.plinth_height_um,
      boundedRequested.plinth ? 80_000 : 0,
      "spec.parameters.plinth_height_um",
      0,
      500_000,
      true,
    ) > 0,
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
    private readonly explicitToken?: string,
  ) {
    this.baseUrl = baseUrl?.replace(/\/$/, "") || undefined;
  }

  get configured(): boolean {
    return Boolean(this.baseUrl);
  }

  get authenticated(): boolean {
    return Boolean(this.accessToken());
  }

  private accessToken(): string | undefined {
    return this.explicitToken
      ?? getStoredAccessToken()
      ?? process.env.NEXT_PUBLIC_DEMO_TOKEN;
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
    return this.request<ProductionStateRead>(
      `/v1/projects/${encodeURIComponent(projectId)}/production-state`,
      { method: "GET" },
    );
  }

  async createVersion(
    projectId: string,
    spec: DesignSpec,
    expectedDesignHash: string,
    expectedCurrentRevision: number,
    templateId: FurnitureTemplateId,
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
    if (
      !releaseId
      || manifestSha.length !== 64
      || payload.machine_use !== "validation_only"
      || payload.release_kind !== "design_review"
    ) {
      throw new ApiError("Servern returnerade ett ofullständigt frisläppningsbevis.");
    }
    return {
      release_id: releaseId,
      release_number: asString(payload.release_number, releaseNumber),
      status: "released",
      manifest_sha256: manifestSha,
      release_kind: "design_review",
      machine_use: "validation_only",
    };
  }

  async previewDesign(
    spec: DesignSpec,
    signal?: AbortSignal,
    projectId?: string,
  ): Promise<ResolvedDesign> {
    const requestBody = toPreviewRequest(spec);
    const path = projectId
      ? `/v1/designs/preview?project_id=${encodeURIComponent(projectId)}`
      : "/v1/designs/preview";
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
    return normalizePreviewResponse(payload, spec);
  }
}

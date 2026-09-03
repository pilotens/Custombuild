import {
  DEFAULT_DESIGN_SPEC,
  MACHINES,
  MATERIALS,
  type BaySizingMode,
  type BackMaterialId,
  type BackPanelType,
  type DesignSpec,
  type PartOverride,
  type ReferenceImageConfirmedInputs,
  type ReferenceImageImport,
  type TopologyBaseline,
} from "./design-types";
import {
  EMPTY_REFERENCE_CONFIRMATIONS,
  referenceImageVerificationIsCurrent,
} from "./reference-image";

export const WORKSPACE_INTENT_SCHEMA_V1 = "custombuild.workspace-intent.v1" as const;
export const MAX_WORKSPACE_INTENT_BYTES = 128 * 1024;
export const MAX_LOCAL_DRAFT_BYTES = 512 * 1024;
export const MAX_CUSTOM_PART_IDS = 1_024;
const RATIO_COMPARISON_TOLERANCE = 1e-9;

export type DesignHydrationErrorCode =
  | "INVALID_LOCAL_DESIGN"
  | "INVALID_SERVER_DRAFT"
  | "INVALID_WORKSPACE_INTENT";

export class DesignHydrationError extends Error {
  constructor(
    readonly code: DesignHydrationErrorCode,
    readonly issues: readonly string[],
  ) {
    super(issues[0] ?? "Designunderlaget kunde inte valideras.");
    this.name = "DesignHydrationError";
  }
}

export interface WorkspaceProductionContext {
  stock_width_mm: number;
  stock_height_mm: number;
  stock_count: number;
  back_stock_width_mm: number;
  back_stock_height_mm: number;
  back_stock_count: number;
  machine_profile_id: string;
}

export interface WorkspaceIntentEnvelopeV1 {
  schema_version: typeof WORKSPACE_INTENT_SCHEMA_V1;
  bay_sizing_mode: BaySizingMode;
  target_bay_width_mm: number;
  symmetry_locked: boolean;
  production_context: WorkspaceProductionContext;
  part_overrides: Record<string, PartOverride>;
  removed_part_ids: string[];
  reference_image_import?: ReferenceImageImport;
  topology_baseline?: TopologyBaseline;
}

export interface ServerProjectDraftEnvelope {
  projectId: string;
  draftRevision: number;
  specJson: unknown;
  workspaceSpecJson: unknown;
}

export type ParsedServerProjectDraft =
  | { kind: "empty" }
  | { kind: "ready"; spec: DesignSpec; intent: WorkspaceIntentEnvelopeV1 };

type JsonRecord = Record<string, unknown>;

const CANONICAL_SERVER_KEYS = [
  "width_mm",
  "height_mm",
  "depth_mm",
  "furniture_type",
  "material_id",
  "nominal_thickness_mm",
  "measured_thickness_mm",
  "shelf_count",
  "shelf_mount",
  "load_per_shelf_kg",
  "back_panel",
  "plinth",
  "divider_count",
  "bay_width_ratios",
  "shelf_height_ratios",
  "base_cabinet_height_mm",
  "base_cabinet_depth_mm",
  "base_cabinet_count",
  "edge_band_mm",
  "joint_system",
  "reinforcement_mode",
  "wall_anchor_required",
  "wall_anchor_verified",
] as const;

const V1_REQUIRED_KEYS = [
  "schema_version",
  "bay_sizing_mode",
  "target_bay_width_mm",
  "symmetry_locked",
  "production_context",
  "part_overrides",
  "removed_part_ids",
] as const;

const V1_OPTIONAL_KEYS = ["reference_image_import", "topology_baseline"] as const;

const PRODUCTION_CONTEXT_KEYS = [
  "stock_width_mm",
  "stock_height_mm",
  "stock_count",
  "back_stock_width_mm",
  "back_stock_height_mm",
  "back_stock_count",
  "machine_profile_id",
] as const;

const PART_OVERRIDE_KEYS = [
  "width_mm",
  "depth_mm",
  "thickness_mm",
  "position_x_mm",
  "position_y_mm",
  "position_z_mm",
] as const;

const REFERENCE_KEYS = [
  "source",
  "import_id",
  "image_sha256",
  "file_name",
  "image_width_px",
  "image_height_px",
  "confidence",
  "detected_shelves",
  "detected_dividers",
  "detected_base_cabinets",
  "warnings",
  "verification_status",
  "confirmed_inputs",
  "verified_model_fingerprint",
] as const;

const CONFIRMATION_KEYS = [
  "dimensions_measured",
  "layout_confirmed",
  "material_confirmed",
  "construction_assumptions_confirmed",
] as const;

const TOPOLOGY_KEYS = [
  "divider_count",
  "shelf_count",
  "base_cabinet_count",
  "bay_width_ratios",
  "shelf_height_ratios",
  "reinforcement_mode",
] as const;

const LOCAL_DESIGN_SPEC_KEYS = [
  "schema_version",
  "design_id",
  "revision",
  "furniture_type",
  "width_mm",
  "height_mm",
  "depth_mm",
  "material_id",
  "material_name",
  "back_material_id",
  "nominal_thickness_mm",
  "measured_thickness_mm",
  "shelf_count",
  "fixed_shelves",
  "load_per_shelf_kg",
  "back_panel",
  "back_panel_type",
  "plinth",
  "plinth_height_mm",
  "divider_count",
  "bay_sizing_mode",
  "target_bay_width_mm",
  "bay_width_ratios",
  "shelf_height_ratios",
  "symmetry_locked",
  "reference_image_import",
  "part_overrides",
  "removed_part_ids",
  "topology_baseline",
  "base_cabinet_height_mm",
  "base_cabinet_depth_mm",
  "base_cabinet_count",
  "reinforcement_mode",
  "joint_system",
  "edge_band_mm",
  "wall_anchor_required",
  "wall_anchor_verified",
  ...PRODUCTION_CONTEXT_KEYS,
] as const;

function fail(code: DesignHydrationErrorCode, issue: string): never {
  throw new DesignHydrationError(code, [issue]);
}

function isRecord(value: unknown): value is JsonRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function record(value: unknown, path: string, code: DesignHydrationErrorCode): JsonRecord {
  if (!isRecord(value)) fail(code, `${path} måste vara ett objekt.`);
  return value;
}

function exactKeys(
  value: JsonRecord,
  required: readonly string[],
  optional: readonly string[],
  path: string,
  code: DesignHydrationErrorCode,
): void {
  const allowed = new Set([...required, ...optional]);
  const unknown = Object.keys(value).filter((key) => !allowed.has(key));
  if (unknown.length > 0) fail(code, `${path} innehåller okända fält.`);
  const missing = required.filter((key) => !Object.hasOwn(value, key));
  if (missing.length > 0) fail(code, `${path} saknar obligatoriska fält.`);
}

function finiteNumber(
  value: unknown,
  path: string,
  minimum: number,
  maximum: number,
  code: DesignHydrationErrorCode,
): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < minimum || value > maximum) {
    fail(code, `${path} ligger utanför tillåtet intervall.`);
  }
  return value;
}

function integer(
  value: unknown,
  path: string,
  minimum: number,
  maximum: number,
  code: DesignHydrationErrorCode,
): number {
  const parsed = finiteNumber(value, path, minimum, maximum, code);
  if (!Number.isInteger(parsed)) fail(code, `${path} måste vara ett heltal.`);
  return parsed;
}

function boolean(value: unknown, path: string, code: DesignHydrationErrorCode): boolean {
  if (typeof value !== "boolean") fail(code, `${path} måste vara sant eller falskt.`);
  return value;
}

function boundedString(
  value: unknown,
  path: string,
  minimum: number,
  maximum: number,
  code: DesignHydrationErrorCode,
): string {
  if (typeof value !== "string" || value.length < minimum || value.length > maximum) {
    fail(code, `${path} har ogiltig längd.`);
  }
  return value;
}

function oneOf<T extends string>(
  value: unknown,
  allowed: readonly T[],
  path: string,
  code: DesignHydrationErrorCode,
): T {
  if (typeof value !== "string" || !allowed.includes(value as T)) {
    fail(code, `${path} har ett värde som inte stöds.`);
  }
  return value as T;
}

function utf8Bytes(value: string): number {
  let bytes = 0;
  for (const character of value) {
    const codePoint = character.codePointAt(0) ?? 0;
    bytes += codePoint <= 0x7f ? 1 : codePoint <= 0x7ff ? 2 : codePoint <= 0xffff ? 3 : 4;
  }
  return bytes;
}

export function localDraftPayloadSizeIsAllowed(raw: string): boolean {
  return utf8Bytes(raw) <= MAX_LOCAL_DRAFT_BYTES;
}

function encodedSize(value: unknown, code: DesignHydrationErrorCode): number {
  try {
    return utf8Bytes(JSON.stringify(value));
  } catch {
    fail(code, "Designunderlaget kan inte serialiseras säkert.");
  }
}

function parseBayRatios(
  value: unknown,
  dividerCount: number,
  path: string,
  code: DesignHydrationErrorCode,
): number[] {
  if (!Array.isArray(value) || value.length > 17) fail(code, `${path} har ogiltig storlek.`);
  if (value.length === 0) return [];
  if (value.length !== dividerCount + 1) fail(code, `${path} matchar inte antalet fack.`);
  const ratios = value.map((item, index) => finiteNumber(item, `${path}[${index}]`, 0, Number.MAX_VALUE, code));
  const total = ratios.reduce((sum, ratio) => sum + ratio, 0);
  if (!Number.isFinite(total) || total <= 0 || ratios.some((ratio) => ratio / total < 0.08)) {
    fail(code, `${path} ger ett fack under åtta procent.`);
  }
  return ratios;
}

function parseShelfRatios(
  value: unknown,
  shelfCount: number,
  path: string,
  code: DesignHydrationErrorCode,
): number[] {
  if (!Array.isArray(value) || value.length > 40) fail(code, `${path} har ogiltig storlek.`);
  if (value.length === 0) return [];
  if (value.length !== shelfCount) fail(code, `${path} matchar inte antalet hyllor.`);
  const ratios = value.map((item, index) => finiteNumber(item, `${path}[${index}]`, 0.05, 0.95, code));
  if (ratios.some((ratio, index) => (
    index > 0
    && ratio - ratios[index - 1]! < 0.05 - RATIO_COMPARISON_TOLERANCE
  ))) {
    fail(code, `${path} har överlappande eller oordnade nivåer.`);
  }
  return ratios;
}

function parseProductionContext(
  value: unknown,
  path: string,
  code: DesignHydrationErrorCode,
  exact: boolean,
): WorkspaceProductionContext {
  const context = record(value, path, code);
  if (exact) exactKeys(context, PRODUCTION_CONTEXT_KEYS, [], path, code);
  const machineProfileId = boundedString(context.machine_profile_id, `${path}.machine_profile_id`, 1, 160, code);
  if (!MACHINES.some((machine) => machine.id === machineProfileId)) {
    fail(code, `${path}.machine_profile_id saknar en känd katalogpost.`);
  }
  return {
    stock_width_mm: finiteNumber(context.stock_width_mm, `${path}.stock_width_mm`, Number.MIN_VALUE, 10_000, code),
    stock_height_mm: finiteNumber(context.stock_height_mm, `${path}.stock_height_mm`, Number.MIN_VALUE, 5_000, code),
    stock_count: integer(context.stock_count, `${path}.stock_count`, 1, 100, code),
    back_stock_width_mm: finiteNumber(context.back_stock_width_mm, `${path}.back_stock_width_mm`, Number.MIN_VALUE, 10_000, code),
    back_stock_height_mm: finiteNumber(context.back_stock_height_mm, `${path}.back_stock_height_mm`, Number.MIN_VALUE, 5_000, code),
    back_stock_count: integer(context.back_stock_count, `${path}.back_stock_count`, 1, 100, code),
    machine_profile_id: machineProfileId,
  };
}

function parseConfirmedInputs(
  value: unknown,
  code: DesignHydrationErrorCode,
): ReferenceImageConfirmedInputs {
  const inputs = record(value, "reference_image_import.confirmed_inputs", code);
  exactKeys(inputs, CONFIRMATION_KEYS, [], "reference_image_import.confirmed_inputs", code);
  return {
    dimensions_measured: boolean(inputs.dimensions_measured, "reference_image_import.confirmed_inputs.dimensions_measured", code),
    layout_confirmed: boolean(inputs.layout_confirmed, "reference_image_import.confirmed_inputs.layout_confirmed", code),
    material_confirmed: boolean(inputs.material_confirmed, "reference_image_import.confirmed_inputs.material_confirmed", code),
    construction_assumptions_confirmed: boolean(
      inputs.construction_assumptions_confirmed,
      "reference_image_import.confirmed_inputs.construction_assumptions_confirmed",
      code,
    ),
  };
}

function parseReferenceImageImport(
  value: unknown,
  code: DesignHydrationErrorCode,
): ReferenceImageImport {
  const provenance = record(value, "reference_image_import", code);
  const required = REFERENCE_KEYS.filter((key) => ![
    "verification_status",
    "confirmed_inputs",
    "verified_model_fingerprint",
  ].includes(key));
  exactKeys(
    provenance,
    required,
    ["verification_status", "confirmed_inputs", "verified_model_fingerprint"],
    "reference_image_import",
    code,
  );
  const importId = boundedString(provenance.import_id, "reference_image_import.import_id", 36, 36, code);
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(importId)) {
    fail(code, "reference_image_import.import_id har ogiltigt format.");
  }
  const imageSha256 = boundedString(provenance.image_sha256, "reference_image_import.image_sha256", 64, 64, code);
  if (!/^[a-f0-9]{64}$/.test(imageSha256)) fail(code, "reference_image_import.image_sha256 har ogiltigt format.");
  const warningsRaw = provenance.warnings;
  if (!Array.isArray(warningsRaw) || warningsRaw.length > 20) {
    fail(code, "reference_image_import.warnings har ogiltig storlek.");
  }
  const warnings = warningsRaw.map((warning, index) => boundedString(
    warning,
    `reference_image_import.warnings[${index}]`,
    0,
    500,
    code,
  ));
  const verificationStatus = provenance.verification_status === undefined
    ? "concept" as const
    : oneOf(provenance.verification_status, ["concept", "parametric_confirmed"] as const, "reference_image_import.verification_status", code);
  const confirmedInputs = provenance.confirmed_inputs === undefined
    ? { ...EMPTY_REFERENCE_CONFIRMATIONS }
    : parseConfirmedInputs(provenance.confirmed_inputs, code);
  const verifiedFingerprint = provenance.verified_model_fingerprint === undefined
    ? undefined
    : boundedString(provenance.verified_model_fingerprint, "reference_image_import.verified_model_fingerprint", 64, 64, code);
  if (verifiedFingerprint && !/^[a-f0-9]{64}$/.test(verifiedFingerprint)) {
    fail(code, "reference_image_import.verified_model_fingerprint har ogiltigt format.");
  }
  return {
    source: oneOf(provenance.source, ["reference_image"] as const, "reference_image_import.source", code),
    import_id: importId,
    image_sha256: imageSha256,
    file_name: boundedString(provenance.file_name, "reference_image_import.file_name", 1, 120, code),
    image_width_px: integer(provenance.image_width_px, "reference_image_import.image_width_px", 160, 20_000, code),
    image_height_px: integer(provenance.image_height_px, "reference_image_import.image_height_px", 160, 20_000, code),
    confidence: finiteNumber(provenance.confidence, "reference_image_import.confidence", 0, 1, code),
    detected_shelves: integer(provenance.detected_shelves, "reference_image_import.detected_shelves", 0, 40, code),
    detected_dividers: integer(provenance.detected_dividers, "reference_image_import.detected_dividers", 0, 16, code),
    detected_base_cabinets: boolean(
      provenance.detected_base_cabinets,
      "reference_image_import.detected_base_cabinets",
      code,
    ),
    warnings,
    verification_status: verificationStatus,
    confirmed_inputs: confirmedInputs,
    ...(verifiedFingerprint ? { verified_model_fingerprint: verifiedFingerprint } : {}),
  };
}

function parseTopologyBaseline(
  value: unknown,
  code: DesignHydrationErrorCode,
): TopologyBaseline {
  const baseline = record(value, "topology_baseline", code);
  exactKeys(baseline, TOPOLOGY_KEYS, [], "topology_baseline", code);
  const dividerCount = integer(baseline.divider_count, "topology_baseline.divider_count", 0, 16, code);
  const shelfCount = integer(baseline.shelf_count, "topology_baseline.shelf_count", 0, 40, code);
  return {
    divider_count: dividerCount,
    shelf_count: shelfCount,
    base_cabinet_count: integer(baseline.base_cabinet_count, "topology_baseline.base_cabinet_count", 0, 17, code),
    bay_width_ratios: parseBayRatios(
      baseline.bay_width_ratios,
      dividerCount,
      "topology_baseline.bay_width_ratios",
      code,
    ),
    shelf_height_ratios: parseShelfRatios(
      baseline.shelf_height_ratios,
      shelfCount,
      "topology_baseline.shelf_height_ratios",
      code,
    ),
    reinforcement_mode: oneOf(
      baseline.reinforcement_mode,
      ["manual", "auto"] as const,
      "topology_baseline.reinforcement_mode",
      code,
    ),
  };
}

function parsePartCustomizations(
  overridesValue: unknown,
  removedValue: unknown,
  code: DesignHydrationErrorCode,
): Pick<WorkspaceIntentEnvelopeV1, "part_overrides" | "removed_part_ids"> {
  const overrides = record(overridesValue, "part_overrides", code);
  if (!Array.isArray(removedValue)) fail(code, "removed_part_ids måste vara en lista.");
  const removedPartIds = removedValue.map((partId, index) => boundedString(
    partId,
    `removed_part_ids[${index}]`,
    1,
    128,
    code,
  ));
  if (new Set(removedPartIds).size !== removedPartIds.length) {
    fail(code, "removed_part_ids innehåller dubletter.");
  }
  const overrideEntries = Object.entries(overrides);
  if (overrideEntries.length + removedPartIds.length > MAX_CUSTOM_PART_IDS) {
    fail(code, "Antalet individuella deländringar överskrider resursgränsen.");
  }
  const partOverrides = Object.fromEntries(overrideEntries.map(([partId, rawOverride]) => {
    boundedString(partId, "part_overrides-id", 1, 128, code);
    const override = record(rawOverride, `part_overrides.${partId}`, code);
    exactKeys(override, [], PART_OVERRIDE_KEYS, `part_overrides.${partId}`, code);
    if (Object.keys(override).length === 0) fail(code, `part_overrides.${partId} är tom.`);
    const parsed: PartOverride = {};
    if (Object.hasOwn(override, "width_mm")) {
      parsed.width_mm = finiteNumber(override.width_mm, `part_overrides.${partId}.width_mm`, 1, 6_000, code);
    }
    if (Object.hasOwn(override, "depth_mm")) {
      parsed.depth_mm = finiteNumber(override.depth_mm, `part_overrides.${partId}.depth_mm`, 1, 6_000, code);
    }
    if (Object.hasOwn(override, "thickness_mm")) {
      parsed.thickness_mm = finiteNumber(override.thickness_mm, `part_overrides.${partId}.thickness_mm`, 1, 100, code);
    }
    if (Object.hasOwn(override, "position_x_mm")) {
      parsed.position_x_mm = finiteNumber(override.position_x_mm, `part_overrides.${partId}.position_x_mm`, 0, 6_000, code);
    }
    if (Object.hasOwn(override, "position_y_mm")) {
      parsed.position_y_mm = finiteNumber(override.position_y_mm, `part_overrides.${partId}.position_y_mm`, 0, 1_200, code);
    }
    if (Object.hasOwn(override, "position_z_mm")) {
      parsed.position_z_mm = finiteNumber(override.position_z_mm, `part_overrides.${partId}.position_z_mm`, 0, 4_000, code);
    }
    return [partId, parsed];
  }));
  return { part_overrides: partOverrides, removed_part_ids: removedPartIds };
}

function generatedPartIds(spec: Pick<
  DesignSpec,
  "divider_count" | "shelf_count" | "base_cabinet_count" | "furniture_type" | "back_panel" | "back_panel_type" | "plinth"
>): Set<string> {
  const ids = new Set(["side-left", "side-right", "bottom", "top"]);
  for (let divider = 1; divider <= spec.divider_count; divider += 1) ids.add(`divider-${divider}`);
  for (let shelf = 1; shelf <= spec.shelf_count; shelf += 1) {
    for (let bay = 1; bay <= spec.divider_count + 1; bay += 1) ids.add(`shelf-${shelf}-bay-${bay}`);
  }
  if (spec.furniture_type === "wall_library") {
    for (let boundary = 2; boundary <= spec.base_cabinet_count; boundary += 1) ids.add(`base-side-${boundary}`);
    for (let cabinet = 1; cabinet <= spec.base_cabinet_count; cabinet += 1) {
      ids.add(`base-bottom-${cabinet}`);
      ids.add(`cabinet-front-${cabinet}`);
    }
  }
  if (spec.back_panel) {
    if (spec.back_panel_type === "inset_groove" && spec.divider_count > 0) {
      for (let bay = 1; bay <= spec.divider_count + 1; bay += 1) {
        ids.add(`back-panel-bay-${bay}`);
      }
    } else {
      ids.add("back-panel");
    }
  }
  if (spec.plinth) ids.add("plinth-front");
  return ids;
}

function assertCustomizationIds(
  spec: Pick<
    DesignSpec,
    "divider_count" | "shelf_count" | "base_cabinet_count" | "furniture_type" | "back_panel" | "back_panel_type" | "plinth"
  >,
  customizations: Pick<WorkspaceIntentEnvelopeV1, "part_overrides" | "removed_part_ids">,
  code: DesignHydrationErrorCode,
): void {
  const allowed = generatedPartIds(spec);
  const requested = [...Object.keys(customizations.part_overrides), ...customizations.removed_part_ids];
  if (requested.some((partId) => !allowed.has(partId))) {
    fail(code, "En individuell deländring pekar på ett okänt eller borttaget del-ID.");
  }
}

function parseIntent(
  value: unknown,
  code: DesignHydrationErrorCode,
): WorkspaceIntentEnvelopeV1 {
  const root = record(value, "workspace_spec_json", code);
  if (encodedSize(root, code) > MAX_WORKSPACE_INTENT_BYTES) {
    fail(code, "workspace_spec_json överskrider 128 KiB.");
  }
  const isV1 = root.schema_version === WORKSPACE_INTENT_SCHEMA_V1;
  const isLegacyFullSpec = root.schema_version === "1.0";
  if (!isV1 && !isLegacyFullSpec) {
    fail(code, "workspace_spec_json har en okänd schemaversion.");
  }
  if (isV1) exactKeys(root, V1_REQUIRED_KEYS, V1_OPTIONAL_KEYS, "workspace_spec_json", code);
  else exactKeys(
    root,
    ["schema_version"],
    LOCAL_DESIGN_SPEC_KEYS.filter((key) => key !== "schema_version"),
    "workspace_spec_json",
    code,
  );

  const productionContext = isV1
    ? parseProductionContext(root.production_context, "workspace_spec_json.production_context", code, true)
    : parseProductionContext(root.production_context ?? root, "workspace_spec_json.production_context", code, Boolean(root.production_context));
  const customizations = parsePartCustomizations(
    root.part_overrides ?? {},
    root.removed_part_ids ?? [],
    code,
  );
  const baySizingMode = root.bay_sizing_mode === undefined && !isV1
    ? DEFAULT_DESIGN_SPEC.bay_sizing_mode
    : oneOf(root.bay_sizing_mode, ["count", "target_width"] as const, "workspace_spec_json.bay_sizing_mode", code);
  const targetBayWidth = root.target_bay_width_mm === undefined && !isV1
    ? DEFAULT_DESIGN_SPEC.target_bay_width_mm
    : finiteNumber(root.target_bay_width_mm, "workspace_spec_json.target_bay_width_mm", 50, 2_000, code);
  const symmetryLocked = root.symmetry_locked === undefined && !isV1
    ? DEFAULT_DESIGN_SPEC.symmetry_locked
    : boolean(root.symmetry_locked, "workspace_spec_json.symmetry_locked", code);

  return {
    schema_version: WORKSPACE_INTENT_SCHEMA_V1,
    bay_sizing_mode: baySizingMode,
    target_bay_width_mm: targetBayWidth,
    symmetry_locked: symmetryLocked,
    production_context: productionContext,
    ...customizations,
    ...(root.reference_image_import === undefined
      ? {}
      : { reference_image_import: parseReferenceImageImport(root.reference_image_import, code) }),
    ...(root.topology_baseline === undefined
      ? {}
      : { topology_baseline: parseTopologyBaseline(root.topology_baseline, code) }),
  };
}

function validateFurnitureInvariants(
  spec: Pick<
    DesignSpec,
    | "furniture_type"
    | "height_mm"
    | "depth_mm"
    | "measured_thickness_mm"
    | "base_cabinet_height_mm"
    | "base_cabinet_depth_mm"
    | "base_cabinet_count"
  >,
  code: DesignHydrationErrorCode,
): void {
  if (spec.furniture_type === "bookcase") {
    if (
      spec.base_cabinet_count !== 0
      || spec.base_cabinet_height_mm !== 0
      || spec.base_cabinet_depth_mm !== 0
    ) fail(code, "En bokhylla får inte innehålla aktiva underskåpsmått.");
    return;
  }
  if (spec.base_cabinet_count < 1 || spec.base_cabinet_count > 17) {
    fail(code, "Ett väggbibliotek måste ha mellan ett och sjutton underskåp.");
  }
  if (spec.base_cabinet_depth_mm !== spec.depth_mm) {
    fail(code, "Underskåpens djup måste vara exakt samma som möbelns djup.");
  }
  if (spec.base_cabinet_height_mm < 300 || spec.base_cabinet_height_mm > 2_000) {
    fail(code, "Underskåpens höjd ligger utanför tillåtet intervall.");
  }
  if (spec.base_cabinet_height_mm >= spec.height_mm - spec.measured_thickness_mm - 200) {
    fail(code, "Underskåpen lämnar inte en säker övre konstruktionszon.");
  }
}

function validateTopologyBaselineInvariants(
  spec: DesignSpec,
  code: DesignHydrationErrorCode,
): void {
  const baseline = spec.topology_baseline;
  if (!baseline) return;
  if (
    baseline.divider_count < spec.divider_count
    || baseline.shelf_count < spec.shelf_count
    || baseline.base_cabinet_count < spec.base_cabinet_count
  ) {
    fail(code, "topology_baseline får inte innehålla färre bärande delar än den aktuella modellen.");
  }
  const restored: DesignSpec = {
    ...spec,
    ...baseline,
    bay_width_ratios: [...baseline.bay_width_ratios],
    shelf_height_ratios: [...baseline.shelf_height_ratios],
    part_overrides: {},
    removed_part_ids: [],
    topology_baseline: undefined,
  };
  validateFurnitureInvariants(restored, code);
}

function currentReferenceOrConcept(spec: DesignSpec): DesignSpec {
  if (!spec.reference_image_import || referenceImageVerificationIsCurrent(spec)) return spec;
  return {
    ...spec,
    reference_image_import: {
      ...spec.reference_image_import,
      verification_status: "concept",
      confirmed_inputs: { ...EMPTY_REFERENCE_CONFIRMATIONS },
      verified_model_fingerprint: undefined,
    },
  };
}

function parseCanonicalServerSpec(
  value: unknown,
  projectId: string,
  draftRevision: number,
  intent: WorkspaceIntentEnvelopeV1,
): DesignSpec {
  const code = "INVALID_SERVER_DRAFT" as const;
  const root = record(value, "spec_json", code);
  exactKeys(
    root,
    CANONICAL_SERVER_KEYS,
    ["plinth_height_mm", "back_material_id"],
    "spec_json",
    code,
  );
  const furnitureType = oneOf(root.furniture_type, ["bookcase", "wall_library"] as const, "spec_json.furniture_type", code);
  const materialId = oneOf(root.material_id, ["mdf", "birch-plywood"] as const, "spec_json.material_id", code);
  const material = MATERIALS.find((candidate) => candidate.id === materialId);
  if (!material) fail(code, "spec_json.material_id saknar en känd katalogpost.");
  const dividerCount = integer(root.divider_count, "spec_json.divider_count", 0, 16, code);
  const shelfCount = integer(root.shelf_count, "spec_json.shelf_count", 0, 40, code);
  const width = finiteNumber(root.width_mm, "spec_json.width_mm", 250, 6_000, code);
  const height = finiteNumber(root.height_mm, "spec_json.height_mm", 300, 4_000, code);
  const depth = finiteNumber(root.depth_mm, "spec_json.depth_mm", 100, 1_200, code);
  const measuredThickness = finiteNumber(
    root.measured_thickness_mm,
    "spec_json.measured_thickness_mm",
    17,
    19,
    code,
  );
  const nominalThickness = finiteNumber(
    root.nominal_thickness_mm,
    "spec_json.nominal_thickness_mm",
    18,
    18,
    code,
  );
  const canonicalBackPanel = typeof root.back_panel === "boolean"
    ? root.back_panel ? "inset_groove" : "none"
    : oneOf(
        root.back_panel,
        ["none", "surface_mounted", "inset_groove"] as const,
        "spec_json.back_panel",
        code,
      );
  const backPanel = canonicalBackPanel !== "none";
  const backPanelType: BackPanelType = canonicalBackPanel === "surface_mounted"
    ? "surface_mounted"
    : "inset_groove";
  const legacyBackMaterialId: BackMaterialId = materialId === "birch-plywood"
    ? "birch-plywood-6"
    : "mdf-6";
  const backMaterialId = root.back_material_id === undefined
    ? legacyBackMaterialId
    : oneOf(
        root.back_material_id,
        ["mdf-6", "birch-plywood-6"] as const,
        "spec_json.back_material_id",
        code,
      );
  if (!backPanel && root.back_material_id !== undefined) {
    fail(code, "spec_json.back_material_id kräver ett aktivt bakstycke.");
  }
  const baseCabinetHeight = finiteNumber(
    root.base_cabinet_height_mm,
    "spec_json.base_cabinet_height_mm",
    0,
    2_000,
    code,
  );
  const baseCabinetDepth = finiteNumber(
    root.base_cabinet_depth_mm,
    "spec_json.base_cabinet_depth_mm",
    0,
    1_200,
    code,
  );
  const baseCabinetCount = integer(root.base_cabinet_count, "spec_json.base_cabinet_count", 0, 17, code);
  const plinth = boolean(root.plinth, "spec_json.plinth", code);
  const plinthHeight = root.plinth_height_mm === undefined
    ? plinth ? DEFAULT_DESIGN_SPEC.plinth_height_mm : 0
    : finiteNumber(root.plinth_height_mm, "spec_json.plinth_height_mm", 0, 500, code);
  if (plinth !== (plinthHeight > 0)) {
    fail(code, "spec_json.plinth måste motsvara om spec_json.plinth_height_mm är större än noll.");
  }
  const wallAnchorRequired = boolean(root.wall_anchor_required, "spec_json.wall_anchor_required", code);
  const wallAnchorVerified = boolean(root.wall_anchor_verified, "spec_json.wall_anchor_verified", code);
  if (wallAnchorVerified) {
    fail(code, "spec_json.wall_anchor_verified får inte återställa ett produktionsbevis i arbetsytan.");
  }

  const customizations = {
    part_overrides: intent.part_overrides,
    removed_part_ids: intent.removed_part_ids,
  };
  const spec: DesignSpec = {
    schema_version: "1.0",
    design_id: projectId,
    revision: draftRevision,
    furniture_type: furnitureType,
    width_mm: width,
    height_mm: height,
    depth_mm: depth,
    material_id: materialId,
    material_name: material.name,
    back_material_id: backMaterialId,
    nominal_thickness_mm: nominalThickness,
    measured_thickness_mm: measuredThickness,
    shelf_count: shelfCount,
    fixed_shelves: oneOf(root.shelf_mount, ["fixed", "adjustable"] as const, "spec_json.shelf_mount", code) === "fixed",
    load_per_shelf_kg: finiteNumber(root.load_per_shelf_kg, "spec_json.load_per_shelf_kg", 0, 500, code),
    back_panel: backPanel,
    back_panel_type: backPanelType,
    plinth,
    plinth_height_mm: plinthHeight,
    divider_count: dividerCount,
    bay_sizing_mode: intent.bay_sizing_mode,
    target_bay_width_mm: intent.target_bay_width_mm,
    bay_width_ratios: parseBayRatios(root.bay_width_ratios, dividerCount, "spec_json.bay_width_ratios", code),
    shelf_height_ratios: parseShelfRatios(root.shelf_height_ratios, shelfCount, "spec_json.shelf_height_ratios", code),
    symmetry_locked: intent.symmetry_locked,
    ...customizations,
    ...(intent.reference_image_import ? { reference_image_import: intent.reference_image_import } : {}),
    ...(intent.topology_baseline ? { topology_baseline: intent.topology_baseline } : {}),
    base_cabinet_height_mm: baseCabinetHeight,
    base_cabinet_depth_mm: baseCabinetDepth,
    base_cabinet_count: baseCabinetCount,
    reinforcement_mode: oneOf(
      root.reinforcement_mode,
      ["manual", "auto"] as const,
      "spec_json.reinforcement_mode",
      code,
    ),
    joint_system: oneOf(root.joint_system, ["dado"] as const, "spec_json.joint_system", code),
    edge_band_mm: finiteNumber(root.edge_band_mm, "spec_json.edge_band_mm", 0, 5, code),
    wall_anchor_required: wallAnchorRequired,
    wall_anchor_verified: false,
    ...intent.production_context,
  };
  validateFurnitureInvariants(spec, code);
  validateTopologyBaselineInvariants(spec, code);
  assertCustomizationIds(spec, customizations, code);
  return currentReferenceOrConcept(spec);
}

export function workspaceIntentEnvelopeFromSpec(spec: DesignSpec): WorkspaceIntentEnvelopeV1 {
  const provenanceSafeSpec = currentReferenceOrConcept(spec);
  validateTopologyBaselineInvariants(provenanceSafeSpec, "INVALID_WORKSPACE_INTENT");
  const envelope: WorkspaceIntentEnvelopeV1 = {
    schema_version: WORKSPACE_INTENT_SCHEMA_V1,
    bay_sizing_mode: provenanceSafeSpec.bay_sizing_mode,
    target_bay_width_mm: provenanceSafeSpec.target_bay_width_mm,
    symmetry_locked: provenanceSafeSpec.symmetry_locked,
    production_context: {
      stock_width_mm: provenanceSafeSpec.stock_width_mm,
      stock_height_mm: provenanceSafeSpec.stock_height_mm,
      stock_count: provenanceSafeSpec.stock_count,
      back_stock_width_mm: provenanceSafeSpec.back_stock_width_mm,
      back_stock_height_mm: provenanceSafeSpec.back_stock_height_mm,
      back_stock_count: provenanceSafeSpec.back_stock_count,
      machine_profile_id: provenanceSafeSpec.machine_profile_id,
    },
    part_overrides: provenanceSafeSpec.part_overrides,
    removed_part_ids: provenanceSafeSpec.removed_part_ids,
    ...(provenanceSafeSpec.reference_image_import
      ? { reference_image_import: provenanceSafeSpec.reference_image_import }
      : {}),
    ...(provenanceSafeSpec.topology_baseline ? { topology_baseline: provenanceSafeSpec.topology_baseline } : {}),
  };
  const parsed = parseIntent(envelope, "INVALID_WORKSPACE_INTENT");
  assertCustomizationIds(provenanceSafeSpec, parsed, "INVALID_WORKSPACE_INTENT");
  return parsed;
}

export function parseServerProjectDraft(
  draft: ServerProjectDraftEnvelope,
): ParsedServerProjectDraft {
  const code = "INVALID_SERVER_DRAFT" as const;
  const projectId = boundedString(draft.projectId, "project_id", 1, 128, code);
  const draftRevision = integer(draft.draftRevision, "draft_revision", 0, 1_000_000_000, code);
  if (draft.specJson === null && draft.workspaceSpecJson === null && draftRevision === 0) return { kind: "empty" };
  if (draft.specJson === null || draft.workspaceSpecJson === null) {
    fail(code, "Serverutkastet är ofullständigt och kan inte öppnas säkert.");
  }
  const intent = parseIntent(draft.workspaceSpecJson, code);
  return {
    kind: "ready",
    spec: parseCanonicalServerSpec(draft.specJson, projectId, draftRevision, intent),
    intent,
  };
}

export function parseLocalDesignSpec(value: unknown): DesignSpec {
  const code = "INVALID_LOCAL_DESIGN" as const;
  const root = record(value, "lokalt DesignSpec", code);
  if (encodedSize(root, code) > MAX_LOCAL_DRAFT_BYTES) fail(code, "Det lokala utkastet överskrider 512 KiB.");
  exactKeys(root, [], LOCAL_DESIGN_SPEC_KEYS, "lokalt DesignSpec", code);
  const merged: JsonRecord = { ...DEFAULT_DESIGN_SPEC, ...root };
  const furnitureType = oneOf(merged.furniture_type, ["bookcase", "wall_library"] as const, "furniture_type", code);
  const materialId = oneOf(merged.material_id, ["mdf", "birch-plywood"] as const, "material_id", code);
  const material = MATERIALS.find((candidate) => candidate.id === materialId);
  if (!material) fail(code, "material_id saknar en känd katalogpost.");
  const backMaterialId: BackMaterialId = root.back_material_id === undefined
    ? materialId === "birch-plywood" ? "birch-plywood-6" : "mdf-6"
    : oneOf(
        merged.back_material_id,
        ["mdf-6", "birch-plywood-6"] as const,
        "back_material_id",
        code,
      );
  const backPanel = boolean(merged.back_panel, "back_panel", code);
  const backPanelType: BackPanelType = root.back_panel_type === undefined
    ? "inset_groove"
    : oneOf(
        merged.back_panel_type,
        ["inset_groove", "surface_mounted"] as const,
        "back_panel_type",
        code,
      );
  const plinth = boolean(merged.plinth, "plinth", code);
  const plinthHeight = root.plinth_height_mm === undefined
    ? plinth ? DEFAULT_DESIGN_SPEC.plinth_height_mm : 0
    : finiteNumber(merged.plinth_height_mm, "plinth_height_mm", 0, 500, code);
  if (plinth !== (plinthHeight > 0)) {
    fail(code, "plinth måste motsvara om plinth_height_mm är större än noll.");
  }
  const dividerCount = integer(merged.divider_count, "divider_count", 0, 16, code);
  const shelfCount = integer(merged.shelf_count, "shelf_count", 0, 40, code);
  const customizations = parsePartCustomizations(merged.part_overrides, merged.removed_part_ids, code);
  const productionContext = parseProductionContext(merged, "production_context", code, false);
  const wallAnchorVerified = boolean(merged.wall_anchor_verified, "wall_anchor_verified", code);
  if (wallAnchorVerified) {
    fail(code, "wall_anchor_verified får inte återställas som bevis från lokal lagring.");
  }
  const referenceImage = merged.reference_image_import === undefined
    ? undefined
    : parseReferenceImageImport(merged.reference_image_import, code);
  const spec: DesignSpec = {
    schema_version: oneOf(merged.schema_version, ["1.0"] as const, "schema_version", code),
    design_id: boundedString(merged.design_id, "design_id", 1, 128, code),
    revision: integer(merged.revision, "revision", 0, 1_000_000_000, code),
    furniture_type: furnitureType,
    width_mm: finiteNumber(merged.width_mm, "width_mm", 250, 6_000, code),
    height_mm: finiteNumber(merged.height_mm, "height_mm", 300, 4_000, code),
    depth_mm: finiteNumber(merged.depth_mm, "depth_mm", 100, 1_200, code),
    material_id: materialId,
    material_name: material.name,
    back_material_id: backMaterialId,
    nominal_thickness_mm: finiteNumber(merged.nominal_thickness_mm, "nominal_thickness_mm", 18, 18, code),
    measured_thickness_mm: finiteNumber(merged.measured_thickness_mm, "measured_thickness_mm", 17, 19, code),
    shelf_count: shelfCount,
    fixed_shelves: boolean(merged.fixed_shelves, "fixed_shelves", code),
    load_per_shelf_kg: finiteNumber(merged.load_per_shelf_kg, "load_per_shelf_kg", 0, 500, code),
    back_panel: backPanel,
    back_panel_type: backPanelType,
    plinth,
    plinth_height_mm: plinthHeight,
    divider_count: dividerCount,
    bay_sizing_mode: oneOf(merged.bay_sizing_mode, ["count", "target_width"] as const, "bay_sizing_mode", code),
    target_bay_width_mm: finiteNumber(merged.target_bay_width_mm, "target_bay_width_mm", 50, 2_000, code),
    bay_width_ratios: parseBayRatios(merged.bay_width_ratios, dividerCount, "bay_width_ratios", code),
    shelf_height_ratios: parseShelfRatios(merged.shelf_height_ratios, shelfCount, "shelf_height_ratios", code),
    symmetry_locked: boolean(merged.symmetry_locked, "symmetry_locked", code),
    ...customizations,
    ...(referenceImage ? { reference_image_import: referenceImage } : {}),
    ...(merged.topology_baseline === undefined
      ? {}
      : { topology_baseline: parseTopologyBaseline(merged.topology_baseline, code) }),
    base_cabinet_height_mm: finiteNumber(merged.base_cabinet_height_mm, "base_cabinet_height_mm", 0, 2_000, code),
    base_cabinet_depth_mm: finiteNumber(merged.base_cabinet_depth_mm, "base_cabinet_depth_mm", 0, 1_200, code),
    base_cabinet_count: integer(merged.base_cabinet_count, "base_cabinet_count", 0, 17, code),
    reinforcement_mode: oneOf(merged.reinforcement_mode, ["manual", "auto"] as const, "reinforcement_mode", code),
    joint_system: oneOf(merged.joint_system, ["dado"] as const, "joint_system", code),
    edge_band_mm: finiteNumber(merged.edge_band_mm, "edge_band_mm", 0, 5, code),
    wall_anchor_required: boolean(merged.wall_anchor_required, "wall_anchor_required", code),
    wall_anchor_verified: false,
    ...productionContext,
  };
  validateFurnitureInvariants(spec, code);
  validateTopologyBaselineInvariants(spec, code);
  assertCustomizationIds(spec, customizations, code);
  return currentReferenceOrConcept(spec);
}

/**
 * Merges one workspace edit at the strict local boundary. A wall-library depth
 * edit is one atomic family transaction, so its active base depth follows the
 * same requested value before any invariant is parsed.
 */
export function parseLocalDesignPatch(
  spec: DesignSpec,
  patch: Partial<DesignSpec>,
): DesignSpec {
  const candidate: DesignSpec = { ...spec, ...patch };
  if (Object.hasOwn(patch, "plinth") && !Object.hasOwn(patch, "plinth_height_mm")) {
    candidate.plinth_height_mm = candidate.plinth
      ? (spec.plinth_height_mm > 0 ? spec.plinth_height_mm : DEFAULT_DESIGN_SPEC.plinth_height_mm)
      : 0;
  }
  if (
    Object.hasOwn(patch, "depth_mm")
    && !Object.hasOwn(patch, "base_cabinet_depth_mm")
    && candidate.furniture_type === "wall_library"
    && candidate.base_cabinet_count > 0
  ) {
    candidate.base_cabinet_depth_mm = candidate.depth_mm;
  }
  return parseLocalDesignSpec(candidate);
}

export type ValidationStatus = "PASS" | "WARNING" | "BLOCK";
export type DesignStatus =
  | "concept"
  | "draft"
  | "design_validated"
  | "cam_validated"
  | "approved"
  | "released";

export type JointSystem = "dado";
export type ReinforcementMode = "manual" | "auto";
export type FurnitureType = "bookcase" | "wall_library";
export type BaySizingMode = "count" | "target_width";
export type BackMaterialId = "mdf-6" | "birch-plywood-6";
export type BackPanelType = "inset_groove" | "surface_mounted";

export interface ReferenceImageConfirmedInputs {
  dimensions_measured: boolean;
  layout_confirmed: boolean;
  material_confirmed: boolean;
  construction_assumptions_confirmed: boolean;
}

export interface ReferenceImageImport {
  source: "reference_image";
  /** Server-issued identity of the immutable image bytes in this project. */
  import_id: string;
  /** Server-computed SHA-256 for offline provenance verification. */
  image_sha256: string;
  file_name: string;
  image_width_px: number;
  image_height_px: number;
  confidence: number;
  detected_shelves: number;
  detected_dividers: number;
  detected_base_cabinets: boolean;
  warnings: string[];
  /** Concept provenance never grants production authority by itself. */
  verification_status?: "concept" | "parametric_confirmed";
  confirmed_inputs?: ReferenceImageConfirmedInputs;
  /** Fingerprint of the exact parametric inputs the confirmations apply to. */
  verified_model_fingerprint?: string;
}

export interface PartOverride {
  width_mm?: number;
  depth_mm?: number;
  thickness_mm?: number;
  position_x_mm?: number;
  position_y_mm?: number;
  position_z_mm?: number;
}

export interface TopologyBaseline {
  divider_count: number;
  shelf_count: number;
  base_cabinet_count: number;
  bay_width_ratios: number[];
  shelf_height_ratios: number[];
  reinforcement_mode: ReinforcementMode;
}

/**
 * The flat API contract used by POST /v1/designs/preview and /v1/designs/autofix.
 * Values are millimetres at the API boundary. The server remains authoritative
 * for production geometry and converts dimensions to integer micrometres.
 */
export interface DesignSpec {
  schema_version: "1.0";
  design_id: string;
  revision: number;
  furniture_type: FurnitureType;
  width_mm: number;
  height_mm: number;
  depth_mm: number;
  material_id: string;
  material_name: string;
  back_material_id: BackMaterialId;
  nominal_thickness_mm: number;
  measured_thickness_mm: number;
  shelf_count: number;
  fixed_shelves: boolean;
  load_per_shelf_kg: number;
  back_panel: boolean;
  back_panel_type: BackPanelType;
  plinth: boolean;
  /** Exact plinth height sent to the canonical API contract. Zero disables the plinth. */
  plinth_height_mm: number;
  divider_count: number;
  /** Workspace intent. Production APIs receive only the deterministically resolved divider count. */
  bay_sizing_mode: BaySizingMode;
  /** Desired minimum clear width for equal bays when bay_sizing_mode is target_width. */
  target_bay_width_mm: number;
  /** Frontend concept layout. Empty arrays mean the production-screened equal grid. */
  bay_width_ratios: number[];
  shelf_height_ratios: number[];
  /** Keeps the visible grid mirrored around the furniture centre lines. Frontend-only. */
  symmetry_locked: boolean;
  /** Immutable source provenance; inferred geometry remains a reviewed concept until confirmed. */
  reference_image_import?: ReferenceImageImport;
  /** Frontend concept edits for individual generated boards. Never sent to production APIs. */
  part_overrides: Record<string, PartOverride>;
  removed_part_ids: string[];
  /** Snapshot used to restore topology-aware removals of dividers, shelf rows and cabinet modules. */
  topology_baseline?: TopologyBaseline;
  base_cabinet_height_mm: number;
  base_cabinet_depth_mm: number;
  base_cabinet_count: number;
  reinforcement_mode: ReinforcementMode;
  joint_system: JointSystem;
  edge_band_mm: number;
  /** Design intent only; verification is always server/evidence bound. */
  wall_anchor_required: boolean;
  wall_anchor_verified: boolean;
  stock_width_mm: number;
  stock_height_mm: number;
  stock_count: number;
  back_stock_width_mm: number;
  back_stock_height_mm: number;
  back_stock_count: number;
  machine_profile_id: string;
}

export interface MaterialDefinition {
  id: string;
  name: string;
  nominalThicknessMm: number;
  measuredThicknessMm: number;
  densityKgM3: number;
  elasticModulusMpa: number;
  allowableBendingStressMpa: number;
  source: string;
  version: string;
}

export interface MachineDefinition {
  id: string;
  name: string;
  workAreaMm: { x: number; y: number; z: number };
  supportedFeatures: ManufacturingFeature["kind"][];
}

export interface ManufacturingFeature {
  id: string;
  kind: "outline" | "drill" | "groove" | "rabbet" | "pocket" | "label";
  face: "A" | "B" | "EDGE";
  depth_mm: number;
  tool_diameter_mm?: number;
  description: string;
}

export interface ResolvedPart {
  part_id: string;
  name: string;
  kind:
    | "side"
    | "top"
    | "bottom"
    | "shelf"
    | "back"
    | "plinth"
    | "divider"
    | "base_side"
    | "base_bottom"
    | "base_top"
    | "cabinet_front";
  width_mm: number;
  depth_mm: number;
  thickness_mm: number;
  position_mm: { x: number; y: number; z: number };
  orientation: "XY" | "XZ" | "YZ";
  color: string;
  material_id: string;
  weight_kg: number;
  features: ManufacturingFeature[];
}

export interface BomLine {
  id: string;
  category: "part" | "hardware";
  item: string;
  quantity: number;
  unit: "st" | "m";
  part_ids: string[];
  dimensions?: string;
  material?: string;
}

export interface CamOperation {
  id: string;
  part_id: string;
  sequence: number;
  operation: string;
  side: "A" | "B" | "EDGE";
  tool: string;
  depth_mm: number;
  status: ValidationStatus;
}

export interface NestingPlacement {
  part_id: string;
  material_id: string;
  stock_role: "carcass" | "back";
  sheet: number;
  x_mm: number;
  y_mm: number;
  width_mm: number;
  height_mm: number;
  rotated: boolean;
}

export interface NestingResult {
  placements: NestingPlacement[];
  sheet_count: number;
  utilization_percent: number;
  overflow_part_ids: string[];
}

export interface RuleSuggestion {
  action:
    | "set_divider_count"
    | "align_base_cabinets"
    | "enable_back"
    | "create_stockless_review_package"
    | "verify_wall_anchor"
    | "manual_review";
  label: string;
  value: number | boolean;
  explanation: string;
}

export interface RuleEvaluation {
  rule_id: string;
  rule_version: string;
  status: ValidationStatus;
  title: string;
  summary: string;
  calculation: string;
  calculated_value?: number;
  allowed_value?: number;
  unit?: string;
  margin_percent?: number;
  assumptions: string[];
  affected_part_ids: string[];
  diagnostics?: Array<{ label: string; value: string; unit?: string }>;
  suggestion?: RuleSuggestion;
}

export interface ChangeDiff {
  field: keyof DesignSpec;
  before: string | number | boolean;
  after: string | number | boolean;
  reason: string;
}

export interface ResolvedDesign {
  design_hash: string;
  spec: DesignSpec;
  parts: ResolvedPart[];
  bom: BomLine[];
  operations: CamOperation[];
  nesting: NestingResult;
  rule_evaluations: RuleEvaluation[];
  status: ValidationStatus;
  change_diff: ChangeDiff[];
  source: "local" | "server-preview";
}

export const MATERIALS: readonly MaterialDefinition[] = [
  {
    id: "birch-plywood",
    name: "Björkplywood",
    nominalThicknessMm: 18,
    measuredThicknessMm: 17.8,
    densityKgM3: 680,
    elasticModulusMpa: 6_500,
    allowableBendingStressMpa: 30,
    source: "Screeningvärde – verifiera leverantörens batchdata",
    version: "screening-2026.1",
  },
  {
    id: "mdf",
    name: "MDF",
    nominalThicknessMm: 18,
    measuredThicknessMm: 18,
    densityKgM3: 750,
    elasticModulusMpa: 3_000,
    allowableBendingStressMpa: 18,
    source: "Screeningvärde – verifiera leverantörens batchdata",
    version: "screening-2026.1",
  },
] as const;

export const BACK_MATERIALS: readonly MaterialDefinition[] = [
  {
    id: "birch-plywood-6",
    name: "Björkplywood, bakstycke",
    nominalThicknessMm: 6,
    measuredThicknessMm: 6,
    densityKgM3: 680,
    elasticModulusMpa: 5_000,
    allowableBendingStressMpa: 24,
    source: "Screeningvärde – verifiera leverantörens batchdata",
    version: "screening-2026.1",
  },
  {
    id: "mdf-6",
    name: "MDF, bakstycke",
    nominalThicknessMm: 6,
    measuredThicknessMm: 6,
    densityKgM3: 780,
    elasticModulusMpa: 2_500,
    allowableBendingStressMpa: 15,
    source: "Screeningvärde – verifiera leverantörens batchdata",
    version: "screening-2026.1",
  },
] as const;

export const MACHINES: readonly MachineDefinition[] = [
  {
    id: "custombuild-router-1325-linuxcnc",
    name: "Custombuild Router 1325 / LinuxCNC (valideringsprofil)",
    workAreaMm: { x: 2_500, y: 1_300, z: 150 },
    supportedFeatures: ["outline", "drill", "groove", "rabbet", "pocket", "label"],
  },
  {
    id: "custombuild-router-5125-linuxcnc",
    name: "Custombuild Router 5125 / LinuxCNC (storformatsvalidering)",
    workAreaMm: { x: 5_100, y: 2_600, z: 150 },
    supportedFeatures: ["outline", "drill", "groove", "rabbet", "pocket", "label"],
  },
] as const;

export const DEFAULT_DESIGN_SPEC: DesignSpec = {
  schema_version: "1.0",
  design_id: "demo-bokhylla-001",
  revision: 1,
  furniture_type: "bookcase",
  width_mm: 1_200,
  height_mm: 2_100,
  depth_mm: 320,
  material_id: MATERIALS[0]!.id,
  material_name: MATERIALS[0]!.name,
  back_material_id: BACK_MATERIALS[0]!.id as BackMaterialId,
  nominal_thickness_mm: MATERIALS[0]!.nominalThicknessMm,
  measured_thickness_mm: MATERIALS[0]!.measuredThicknessMm,
  shelf_count: 5,
  fixed_shelves: true,
  load_per_shelf_kg: 32,
  back_panel: true,
  back_panel_type: "inset_groove",
  plinth: true,
  plinth_height_mm: 80,
  divider_count: 0,
  bay_sizing_mode: "count",
  target_bay_width_mm: 300,
  bay_width_ratios: [],
  shelf_height_ratios: [],
  symmetry_locked: true,
  part_overrides: {},
  removed_part_ids: [],
  base_cabinet_height_mm: 0,
  base_cabinet_depth_mm: 0,
  base_cabinet_count: 0,
  reinforcement_mode: "auto",
  joint_system: "dado",
  edge_band_mm: 1,
  wall_anchor_required: false,
  wall_anchor_verified: false,
  stock_width_mm: 2_440,
  stock_height_mm: 1_220,
  stock_count: 4,
  back_stock_width_mm: 2_440,
  back_stock_height_mm: 1_220,
  back_stock_count: 2,
  machine_profile_id: MACHINES[0]!.id,
};

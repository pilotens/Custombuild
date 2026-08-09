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

/**
 * The flat API contract used by POST /v1/designs/preview and /v1/designs/autofix.
 * Values are millimetres at the API boundary. The server remains authoritative
 * for production geometry and converts dimensions to integer micrometres.
 */
export interface DesignSpec {
  schema_version: "1.0";
  design_id: string;
  revision: number;
  width_mm: number;
  height_mm: number;
  depth_mm: number;
  material_id: string;
  material_name: string;
  nominal_thickness_mm: number;
  measured_thickness_mm: number;
  shelf_count: number;
  fixed_shelves: boolean;
  load_per_shelf_kg: number;
  back_panel: boolean;
  plinth: boolean;
  divider_count: number;
  reinforcement_mode: ReinforcementMode;
  joint_system: JointSystem;
  edge_band_mm: number;
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
  kind: "outline" | "drill" | "groove" | "pocket" | "label";
  face: "A" | "B" | "EDGE";
  depth_mm: number;
  x_mm?: number;
  y_mm?: number;
  diameter_mm?: number;
  width_mm?: number;
  length_mm?: number;
  pattern_count?: number;
  pitch_mm?: number;
  through?: boolean;
  tolerance_mm?: number;
  fit_clearance_mm?: number;
  tool_diameter_mm?: number;
  description: string;
}

export interface ResolvedPart {
  part_id: string;
  name: string;
  kind: "side" | "top" | "bottom" | "shelf" | "back" | "plinth" | "divider";
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
  action: "set_divider_count" | "enable_back" | "verify_wall_anchor";
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

export const MACHINES: readonly MachineDefinition[] = [
  {
    id: "custombuild-router-1325-linuxcnc",
    name: "Custombuild Router 1325 / LinuxCNC (valideringsprofil)",
    workAreaMm: { x: 2_500, y: 1_300, z: 150 },
    supportedFeatures: ["outline", "drill", "groove", "pocket", "label"],
  },
] as const;

export const DEFAULT_DESIGN_SPEC: DesignSpec = {
  schema_version: "1.0",
  design_id: "demo-bokhylla-001",
  revision: 1,
  width_mm: 1_200,
  height_mm: 2_100,
  depth_mm: 320,
  material_id: MATERIALS[0]!.id,
  material_name: MATERIALS[0]!.name,
  nominal_thickness_mm: MATERIALS[0]!.nominalThicknessMm,
  measured_thickness_mm: MATERIALS[0]!.measuredThicknessMm,
  shelf_count: 5,
  fixed_shelves: true,
  load_per_shelf_kg: 32,
  back_panel: true,
  plinth: true,
  divider_count: 0,
  reinforcement_mode: "manual",
  joint_system: "dado",
  edge_band_mm: 1,
  wall_anchor_verified: false,
  stock_width_mm: 2_440,
  stock_height_mm: 1_220,
  stock_count: 4,
  back_stock_width_mm: 2_440,
  back_stock_height_mm: 1_220,
  back_stock_count: 2,
  machine_profile_id: MACHINES[0]!.id,
};

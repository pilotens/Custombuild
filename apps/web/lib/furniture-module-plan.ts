import { newFurnitureWorkspace, type FurnitureInstallation, type FurniturePreview, type FurnitureWorkspace } from "./furniture-workspace";

/** Rows count from the floor upwards; columns count from left to right. */
export interface FurnitureModuleGrid {
  columns: number;
  rows: number;
  /** Explicit user choice, including zero. */
  gap_um: number;
  shelf_count_per_row: number[];
  divider_count_per_module: number;
}

export interface FurnitureModuleRequirement {
  code: string;
  status: "required" | "blocked";
  message: string;
}

export interface FurnitureModuleDimensions {
  width_um: number;
  height_um: number;
  depth_um: number;
}

export interface FurnitureModuleDraft {
  module_id: string;
  row: number;
  column: number;
  placement: { x_um: number; y_um: number; z_um: number };
  dimensions_um: FurnitureModuleDimensions;
  weight_g: number;
  workspace: FurnitureWorkspace;
  workspace_hash: string;
  design_hash: string;
  source_workspace_hash: string;
  source_design_hash: string;
  plan_hash: string;
  shelf_row_payload_n?: number;
  total_shelf_payload_n?: number;
  preview: FurniturePreview;
  requirements: FurnitureModuleRequirement[];
  review_status: "requires_review" | "blocked";
  production_qualified: false;
  physical_cutting_authorized: false;
}

function orderedJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(orderedJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.entries(value).filter(([, entry]) => entry !== undefined)
      .sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)
      .map(([name, entry]) => `${JSON.stringify(name)}:${orderedJson(entry)}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

/** Compare meaning without rejecting the API's documented default omission or
 * material-stock ordering. In particular, unmeasured installation gaps stay null. */
function workspaceIdentity(workspace: FurnitureWorkspace): string {
  const defaults = newFurnitureWorkspace(workspace.design.intent.family);
  const installation = workspace.design.installation;
  const stock = workspace.manufacturing;
  const installationDefaults = { width_includes_trim: false, left_allowance_um: null, right_allowance_um: null,
    top_allowance_um: null, bottom_allowance_um: null, front_allowance_um: null, rear_allowance_um: null };
  const trimDefaults = { height_um: null, width_um: null, use: "unassigned", walls: [] };
  const stockDefaults = { stock_width_um: 2_440_000, stock_height_um: 1_220_000, stock_grain_axis: null, edge_margin_um: 0 };
  const manufacturingDefaults = { ...stockDefaults, machine_profile_version: "1.0.0-validation" };
  return orderedJson({
    ...defaults, ...workspace,
    design: {
      ...defaults.design, ...workspace.design,
      material: { ...defaults.design.material, ...workspace.design.material },
      back_material: { ...defaults.design.back_material, ...workspace.design.back_material },
      intent: {
        ...defaults.design.intent,
        ...(workspace.design.intent.family === "shelving" ? {
          shelf_load_basis: "per_row", shelf_load_per_metre_n: 0,
          bay_width_ratios_ppm: [], back_panel: "inset_groove", shelf_mount: "fixed", plinth_height_um: 0,
        } : {}),
        ...workspace.design.intent,
      },
      installation: installation ? {
        ...installationDefaults, ...installation,
        trim_profile: installation.trim_profile ? {
          ...trimDefaults, ...installation.trim_profile,
        } : null,
      } : null,
    },
    manufacturing: stock ? {
      ...manufacturingDefaults, ...stock,
      material_stocks: (stock.material_stocks ?? []).map(material => ({
        ...stockDefaults, ...material,
      })).sort((a, b) => {
        for (const field of ["material_id", "material_version"] as const) {
          if (a[field] !== b[field]) return a[field] < b[field] ? -1 : 1;
        }
        return a.measured_thickness_um - b.measured_thickness_um;
      }),
    } : null,
  });
}

/** Bind returned drafts to this complete workspace, not just its geometry hash.
 * Installation or stock blocks do not prohibit downloading editable drafts. */
export function assertMatchingFurnitureModulePlan(
  plan: FurnitureModulePlan, workspace: FurnitureWorkspace, designHash: string, grid: FurnitureModuleGrid,
): void {
  if (plan.source_design_hash !== designHash || plan.source?.design_hash !== designHash) {
    throw new Error("Designen har ändrats. Beräkna modulplanen igen.");
  }
  if (workspaceIdentity(plan.source.workspace) !== workspaceIdentity(workspace)
    || workspaceIdentity({ ...plan.source.workspace, design: { ...plan.source.workspace.design,
      installation: plan.source.installation } }) !== workspaceIdentity(workspace)) {
    throw new Error("Modulplanen gäller en annan arbetsfil. Kontrollera material, råformat och installation och beräkna igen.");
  }
  if (orderedJson(plan.grid) !== orderedJson(grid)) {
    throw new Error("Modulplanen motsvarar inte dina val. Beräkna den igen.");
  }
  const invalid = () => { throw new Error("Modulplanen är ofullständig eller innehåller motstridiga modulidentiteter. Beräkna den igen."); };
  const hash = (value: string) => /^[a-f0-9]{64}$/.test(value);
  const validRequirements = (value: FurnitureModuleRequirement[]) => Array.isArray(value) && value.every(item =>
    item && typeof item.code === "string" && typeof item.message === "string" && ["required", "blocked"].includes(item.status));
  if (plan.schema_version !== "custombuild.furniture-module-plan.v1"
    || !hash(plan.plan_hash) || !hash(plan.source.workspace_hash) || !hash(designHash)
    || !Array.isArray(plan.modules) || !validRequirements(plan.requirements) || typeof plan.message !== "string"
    || plan.can_apply !== false || plan.production_qualified !== false || plan.physical_cutting_authorized !== false) invalid();
  if (plan.state === "unavailable") {
    if (plan.modules.length || plan.can_export_drafts !== false || plan.layout !== null) invalid();
    return;
  }
  const layout = plan.layout;
  if (plan.state !== "available" || !layout || plan.modules.length !== grid.columns * grid.rows
    || plan.modules.length > 16 || layout.column_widths_um.length !== grid.columns
    || layout.row_heights_um.length !== grid.rows) return invalid();
  const widths = layout.column_widths_um, heights = layout.row_heights_um;
  const equalAllocation = (values: number[], total: number) => {
    const usable = total - grid.gap_um * (values.length - 1);
    return values.every((value, index) => value === Math.floor(usable / values.length) + (index < usable % values.length ? 1 : 0));
  };
  if (![...widths, ...heights].every(value => Number.isSafeInteger(value) && value > 0)
    || (layout.gap_um !== undefined && layout.gap_um !== grid.gap_um)
    || (layout.row_order !== undefined && layout.row_order !== "bottom_to_top")
    || (layout.column_order !== undefined && layout.column_order !== "left_to_right")
    || !equalAllocation(widths, workspace.design.intent.width_um) || !equalAllocation(heights, workspace.design.intent.height_um)
    || widths.reduce((sum, value) => sum + value, 0) + grid.gap_um * (grid.columns - 1) !== workspace.design.intent.width_um
    || heights.reduce((sum, value) => sum + value, 0) + grid.gap_um * (grid.rows - 1) !== workspace.design.intent.height_um
    || orderedJson(layout.envelope) !== orderedJson({ width_um: workspace.design.intent.width_um,
      height_um: workspace.design.intent.height_um, depth_um: workspace.design.intent.depth_um })) invalid();
  const positions = new Set<string>(), ids = new Set<string>();
  for (const draft of plan.modules) {
    const position = `${draft.row}:${draft.column}`;
    if (!Number.isInteger(draft.row) || !Number.isInteger(draft.column)
      || draft.row < 0 || draft.row >= grid.rows || draft.column < 0 || draft.column >= grid.columns
      || positions.has(position) || ids.has(draft.module_id)
      || draft.module_id !== `module-${plan.plan_hash.slice(0, 24)}-r${draft.row + 1}-c${draft.column + 1}`
      || !hash(draft.workspace_hash) || !hash(draft.design_hash)
      || draft.source_workspace_hash !== plan.source.workspace_hash || draft.source_design_hash !== designHash
      || draft.plan_hash !== plan.plan_hash || draft.module_id !== draft.workspace.design.design_id
      || draft.workspace.design.revision !== 1 || draft.design_hash !== draft.preview.design.design_hash
      || workspaceIdentity(draft.workspace) !== workspaceIdentity(draft.preview.workspace)
      || draft.production_qualified !== false || draft.physical_cutting_authorized !== false
      || draft.preview.production_qualified !== false || draft.preview.physical_cutting_authorized !== false
      || !["PASS", "WARNING", "BLOCK"].includes(draft.preview.rules.overall_status)
      || ![true, false, null].includes(draft.preview.manufacturing.geometry_compatible)
      || !validRequirements(draft.requirements) || !Number.isSafeInteger(draft.weight_g) || draft.weight_g < 0
      || draft.weight_g !== draft.preview.design.total_weight_g) invalid();
    positions.add(position); ids.add(draft.module_id);
    const dimensions = { width_um: widths[draft.column], height_um: heights[draft.row], depth_um: workspace.design.intent.depth_um };
    if (orderedJson(draft.dimensions_um) !== orderedJson(dimensions)
      || draft.workspace.design.intent.width_um !== dimensions.width_um
      || draft.workspace.design.intent.height_um !== dimensions.height_um
      || draft.workspace.design.intent.depth_um !== dimensions.depth_um
      || draft.workspace.design.intent.shelf_count !== grid.shelf_count_per_row[draft.row]
      || draft.workspace.design.intent.divider_count !== grid.divider_count_per_module
      || (workspace.design.intent.shelf_load_basis !== "per_metre" && draft.workspace.design.intent.shelf_load_n
        !== Math.ceil((workspace.design.intent.shelf_load_n ?? 200) * dimensions.width_um! / widths.reduce((sum, value) => sum + value, 0)))
      || orderedJson(draft.placement) !== orderedJson({
        x_um: widths.slice(0, draft.column).reduce((sum, value) => sum + value, 0) + grid.gap_um * draft.column,
        y_um: 0, z_um: heights.slice(0, draft.row).reduce((sum, value) => sum + value, 0) + grid.gap_um * draft.row,
      })) invalid();
    // Only these fields may change in a new draft. Preserve every other source
    // choice, including installation, material batch and material-specific stock.
    const restored: FurnitureWorkspace = { ...draft.workspace, design: { ...draft.workspace.design,
      design_id: workspace.design.design_id ?? "furniture", revision: workspace.design.revision ?? 1,
      intent: { ...draft.workspace.design.intent, width_um: workspace.design.intent.width_um,
        height_um: workspace.design.intent.height_um, shelf_count: workspace.design.intent.shelf_count ?? 4,
        divider_count: workspace.design.intent.divider_count ?? 1,
        ...(workspace.design.intent.shelf_load_basis !== "per_metre"
          ? { shelf_load_n: workspace.design.intent.shelf_load_n ?? 200 } : {}),
      },
    } };
    if (workspaceIdentity(restored) !== workspaceIdentity(workspace)) invalid();
  }
}

export interface FurnitureModulePlan {
  schema_version: "custombuild.furniture-module-plan.v1";
  version?: string;
  state: "available" | "unavailable";
  code: string;
  message: string;
  source_design_hash: string;
  source: {
    workspace: FurnitureWorkspace;
    workspace_hash: string;
    design_hash: string;
    installation: FurnitureInstallation | null;
    dimension_review?: NonNullable<FurniturePreview["workshop_handoff"]>["dimensions"];
  };
  grid: FurnitureModuleGrid;
  plan_hash: string;
  modules: FurnitureModuleDraft[];
  layout: {
    column_widths_um: number[];
    row_heights_um: number[];
    envelope: FurnitureModuleDimensions;
    gap_um?: number;
    coordinate_system?: "source_carcass_bottom_left_front";
    row_order?: "bottom_to_top";
    column_order?: "left_to_right";
    joint_or_spacer_geometry_created?: false;
  } | null;
  load_distribution?: {
    basis: "per_row" | "per_metre";
    source_shelf_row_payload_n: number;
    module_shelf_row_payload_n_by_column: number[];
    assembled_shelf_row_payload_n: number;
    source_total_shelf_payload_n: number;
    planned_total_shelf_payload_n: number;
    shelf_load_per_metre_n: number | null;
    rounding: "ceiling_per_module";
    stacking_loads_included_in_shelf_screening: false;
  } | null;
  screening?: {
    all_stock_fit: boolean | null;
    all_shelf_numeric_pass: boolean;
    rule_block_count: number;
    stock_issue_count: number;
    module_count: number;
    part_count: number;
    total_weight_g: number;
    assembly_qualified: false;
    intermodule_connections: "unknown";
    wall_anchorage: "unknown";
    stacking_load_path: "unknown" | "not_applicable";
  } | null;
  requirements: FurnitureModuleRequirement[];
  review_status: "requires_review" | "blocked";
  can_export_drafts: boolean;
  can_apply: false;
  production_qualified: false;
  physical_cutting_authorized: false;
  limits?: { max_modules: number; max_generated_parts: number };
  scope?: string;
}

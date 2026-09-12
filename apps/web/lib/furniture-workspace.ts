import type { ResolvedPart } from "./design-types";

export type FurnitureFamily = "shelving" | "table" | "chest_of_drawers";
export interface FurnitureIntent {
  family: FurnitureFamily;
  width_um: number;
  height_um: number;
  depth_um: number;
  shelf_count?: number;
  divider_count?: number;
  shelf_load_n?: number;
  shelf_load_basis?: "per_row" | "per_metre";
  shelf_load_per_metre_n?: number;
  shelf_height_ratios_ppm?: number[];
  bay_width_ratios_ppm?: number[];
  back_panel?: "none" | "surface_mounted" | "inset_groove";
  shelf_mount?: "fixed" | "adjustable";
  plinth_height_um?: number;
  end_inset_um?: number;
  stretcher_height_um?: number;
  top_load_n?: number;
  drawer_count?: number;
  drawer_load_n?: number;
  front_gap_um?: number;
}
export interface FurnitureMaterialSelection {
  material_id: string;
  version: string;
  measured_thickness_um: number;
  batch_id: string | null;
}
export interface FurnitureStockFormat {
  stock_width_um: number;
  stock_height_um: number;
  stock_grain_axis: "x" | "y" | null;
  edge_margin_um?: number;
}
export interface FurnitureMaterialStock extends FurnitureStockFormat {
  material_id: string;
  material_version: string;
  measured_thickness_um: number;
}
export interface FurnitureManufacturingSelection extends FurnitureStockFormat {
  machine_profile_id: string;
  machine_profile_version: string;
  material_stocks?: FurnitureMaterialStock[];
}
export interface FurnitureStockGroup {
  material_id: string;
  material_version: string;
  measured_thickness_um: number;
  selection_source: "default" | "material";
  stock: FurnitureStockFormat;
  geometry_compatible: boolean;
  required_formats: { stock_width_um: number; stock_height_um: number; fits_machine: boolean }[];
  parts: { part_id: string; semantic_key: string; fits_stock: boolean | null;
    orientations: { rotation_deg: number; required_stock_width_um: number; required_stock_height_um: number;
      width_shortfall_um: number; height_shortfall_um: number; fits_stock: boolean; fits_machine: boolean }[] }[];
}
export interface FurnitureTrimProfile {
  height_um: number | null;
  width_um: number | null;
  use: "unassigned" | "existing_room_trim" | "furniture_trim";
  walls: ("left" | "right" | "rear")[];
}
export interface FurnitureInstallation {
  width_um: number;
  height_um: number;
  depth_um: number;
  width_includes_trim: boolean;
  trim_profile?: FurnitureTrimProfile | null;
  left_allowance_um: number | null;
  right_allowance_um: number | null;
  top_allowance_um: number | null;
  bottom_allowance_um: number | null;
  front_allowance_um: number | null;
  rear_allowance_um: number | null;
}
export interface FurnitureWorkspace {
  schema_version: "custombuild.furniture-workspace.v1";
  design: {
    schema_version: "custombuild.furniture-design.v1";
    design_id: string;
    revision: number;
    intent: FurnitureIntent;
    material: FurnitureMaterialSelection;
    back_material: FurnitureMaterialSelection;
    hardware: { catalog_id: string; version: string } | null;
    installation?: FurnitureInstallation | null;
  };
  manufacturing: FurnitureManufacturingSelection | null;
}
export interface FurniturePart {
  part_id: string;
  semantic_key: string;
  role: string;
  finished_size: { width_um: number; depth_um: number; height_um: number };
  placement: { x_um: number; y_um: number; z_um: number };
  actual_thickness_um: number;
  material_id: string;
  weight_g: number;
}
export interface FurniturePreview {
  schema_version: "custombuild.furniture-preview.v1";
  workspace: FurnitureWorkspace;
  design: {
    design_hash: string;
    intent_hash: string;
    parts: FurniturePart[];
    moving_groups: { group_id: string; part_ids: string[]; travel_um: number }[];
    total_weight_g: number;
  };
  rules: {
    overall_status: "PASS" | "WARNING" | "BLOCK";
    evaluations: { rule_id: string; title: string; status: string; detail: string;
      values: Record<string, string | number> }[];
  };
  manufacturing: {
    state: "not_selected" | "requires_change" | "not_qualified";
    geometry_compatible: boolean | null;
    issues: { code: string; message: string; part_id?: string }[];
    detail?: string;
    stock_groups?: FurnitureStockGroup[];
    format_scope?: string;
  };
  physical_cutting_authorized: false;
  production_qualified: false;
  trial_readiness?: FurnitureTrialReadiness;
  workshop_handoff?: {
    stock_plan?: FurniturePreview["manufacturing"];
    shelf_load?: { basis: "per_row" | "per_metre"; total_row_load_n: number;
      load_per_metre_n: number | null; width_um: number } | null;
    dimensions: {
      state: string;
      carcass_dimensions_um: { width_um: number; height_um: number; depth_um: number };
      required_carcass_dimensions_um: { width_um: number; height_um: number; depth_um: number } | null;
      installation: FurnitureInstallation | null;
      issues: { code: string; message: string }[];
    };
    stock_requirements: {
      material_id: string; material_version: string; measured_thickness_um: number;
      part_count: number; raw_area_um2: number;
      minimum_long_edge_um: number; minimum_short_edge_um: number;
      minimum_along_grain_um: number; minimum_across_grain_um: number;
      parts: { part_id: string; semantic_key: string; raw_width_um: number; raw_height_um: number;
        grain_direction: string; along_grain_um: number | null; across_grain_um: number | null }[];
    }[];
  };
}
export interface FurnitureTrialReadiness {
  schema_version: "custombuild.furniture-trial-readiness.v1";
  scope: "design_review_preparation";
  design_hash: string;
  workspace_sha256: string;
  report_sha256: string;
  state: "requires_design_change" | "requires_workshop_evidence";
  blocker_count: number;
  evidence_required_count: number;
  next_action: string;
  checks: { code: string; title: string; state: "checked" | "blocked" | "requires_evidence";
    detail: string; action: string }[];
  production_qualified: false;
  physical_cutting_authorized: false;
}
export interface FurnitureProfileComparison {
  state: string;
  message: string;
  proposed: FurniturePreview | null;
  can_apply: boolean;
  intent_preserved?: boolean;
  changed_part_ids?: string[];
  changed_dependencies: string[];
  invalidated_reviews: string[];
}
export interface FurnitureShelfSuggestion {
  schema_version: "custombuild.furniture-shelf-suggestion.v1";
  state: "available" | "already_pass" | "unavailable";
  code: string;
  message: string;
  scope: string;
  current: FurniturePreview;
  proposed: FurniturePreview | null;
  changed_fields: { field: string; before: number; after: number }[];
  current_bays: { divider_count: number; bay_count: number; clear_widths_um: number[] } | null;
  proposed_bays: { divider_count: number; bay_count: number; clear_widths_um: number[] } | null;
  screening_checks: {
    current: { rule_id: string; status: string; numeric_status: string; calculated: number | null;
      allowed: number | null; unit: string | null }[];
    proposed: FurnitureShelfSuggestion["screening_checks"]["current"] | null;
  };
  search: { max_divider_count: number; attempted_candidate_count: number; evaluated_candidate_count: number };
  remaining_issues: { rules: FurniturePreview["rules"]["evaluations"]; manufacturing: FurniturePreview["manufacturing"]["issues"] };
  can_apply: boolean;
  production_qualified: false;
  physical_cutting_authorized: false;
}
export interface FurnitureDraft {
  project_id: string;
  revision: number;
  workspace: FurnitureWorkspace | null;
  preview: FurniturePreview | null;
}
export interface FurnitureHistory {
  items: { id: string; revision: number; created_at: string; workspace: FurnitureWorkspace;
    design_hash: string; engine_version: string }[];
  next_offset: number | null;
}
export interface FurnitureCatalog {
  families: { id: FurnitureFamily; name: string }[];
  materials: { material_id: string; name: string; version: string; nominal_thickness_um: number;
    min_supported_thickness_um: number; max_supported_thickness_um: number }[];
  hardware: { catalog_id: string; name: string; version: string; family: FurnitureFamily;
    production_qualified: false }[];
  machines: { profile_id: string; version: string; name: string; production_qualified: false }[];
}
export interface FurnitureExportResult {
  state: "queued" | "running" | "failed" | "expired" | "succeeded";
  message?: string;
  file_name?: string;
  content_base64?: string;
  sha256?: string;
  design_hash?: string;
}

export interface FurnitureProductionSource {
  schema_version: "custombuild.furniture-production-source.v1";
  bridge_version: "furniture-production-1.0.0";
  workspace: FurnitureWorkspace;
  workspace_sha256: string;
  furniture_design_hash: string;
}

export interface FurnitureProductionPreview {
  source_furniture: FurnitureProductionSource;
  preview: Record<string, unknown>;
  physical_cutting_authorized: false;
}

export const FURNITURE_FAMILY_LABELS: Record<FurnitureFamily, string> = {
  shelving: "Hyllsystem", table: "Bord med gavlar", chest_of_drawers: "Byrå",
};

export function newFurnitureWorkspace(family: FurnitureFamily): FurnitureWorkspace {
  const intent: FurnitureIntent = family === "table"
    ? { family, width_um: 1_000_000, height_um: 740_000, depth_um: 600_000,
        end_inset_um: 60_000, stretcher_height_um: 100_000, top_load_n: 300 }
    : family === "chest_of_drawers"
      ? { family, width_um: 800_000, height_um: 900_000, depth_um: 500_000,
          drawer_count: 3, drawer_load_n: 100, front_gap_um: 3_000 }
      : { family, width_um: 900_000, height_um: 1_800_000, depth_um: 320_000,
          shelf_count: 4, divider_count: 1, shelf_load_n: 200, shelf_height_ratios_ppm: [] };
  return {
    schema_version: "custombuild.furniture-workspace.v1",
    design: {
      schema_version: "custombuild.furniture-design.v1", design_id: "furniture", revision: 1,
      intent,
      material: { material_id: "birch-plywood", version: "screening-2026.1",
        measured_thickness_um: 18_000, batch_id: null },
      back_material: { material_id: "birch-plywood-6", version: "screening-2026.1",
        measured_thickness_um: 6_000, batch_id: null },
      hardware: family === "shelving" ? null : {
        catalog_id: family === "table" ? "panel-table-connectors-layout" : "drawer-side-mount-450-layout",
        version: "layout-1.0.0",
      },
    },
    manufacturing: null,
  };
}

/** Rendering is a projection of server geometry, never a second furniture compiler. */
export function furnitureViewerParts(preview: FurniturePreview, drawersOpen = false): ResolvedPart[] {
  const movements = new Map<string, number>();
  if (drawersOpen) for (const group of preview.design.moving_groups) {
    for (const id of group.part_ids) movements.set(id, group.travel_um);
  }
  const yz = new Set(["left_side", "right_side", "divider", "table_end", "drawer_side"]);
  const xz = new Set(["back", "plinth", "table_stretcher", "drawer_front", "drawer_back"]);
  return preview.design.parts.map((part): ResolvedPart => {
    const size = part.finished_size;
    const orientation = yz.has(part.role) ? "YZ" : xz.has(part.role) ? "XZ" : "XY";
    // The viewer's YZ board width runs vertically (world Z); its depth runs along world Y.
    const width = orientation === "YZ" ? size.height_um : size.width_um;
    const depth = orientation === "XZ" ? size.height_um : size.depth_um;
    const thickness = orientation === "YZ" ? size.width_um : orientation === "XZ" ? size.depth_um : size.height_um;
    return {
      part_id: part.part_id, name: part.semantic_key,
      kind: orientation === "YZ" ? "side" : orientation === "XZ" ? "cabinet_front" : "shelf",
      orientation, width_mm: width/1_000, depth_mm: depth/1_000, thickness_mm: thickness/1_000,
      position_mm: {
        x: (part.placement.x_um+size.width_um/2)/1_000,
        y: (part.placement.y_um+size.depth_um/2-(movements.get(part.part_id) ?? 0))/1_000,
        z: (part.placement.z_um+size.height_um/2)/1_000,
      },
      material_id: part.material_id, weight_kg: part.weight_g/1_000,
      color: part.material_id.startsWith("mdf") ? "#b7a587" : "#d8c4a0", features: [],
    };
  });
}

export function assertFurniturePreview(value: FurniturePreview): FurniturePreview {
  if (value?.schema_version !== "custombuild.furniture-preview.v1"
      || !/^[a-f0-9]{64}$/.test(value.design?.design_hash ?? "")
      || !Array.isArray(value.design?.parts) || !value.design.parts.length
      || !Array.isArray(value.design?.moving_groups)
      || !Array.isArray(value.rules?.evaluations)
      || value.physical_cutting_authorized !== false || value.production_qualified !== false) {
    throw new Error("Servern returnerade ett ogiltigt möbelunderlag.");
  }
  const ids = new Set<string>();
  for (const part of value.design.parts) {
    const dims = part.finished_size;
    const position = part.placement;
    if (!part.part_id || ids.has(part.part_id)
      || !dims || !position
      || ![dims.width_um, dims.depth_um, dims.height_um].every(n => Number.isSafeInteger(n) && n > 0)
      || ![position.x_um, position.y_um, position.z_um].every(n => Number.isSafeInteger(n) && n >= 0)) {
      throw new Error("Möbelunderlaget innehåller ogiltiga delar.");
    }
    ids.add(part.part_id);
  }
  for (const group of value.design.moving_groups) {
    if (!Number.isSafeInteger(group.travel_um) || group.travel_um < 0
      || !group.part_ids.every(id => ids.has(id))) throw new Error("Lådrörelsen saknar giltiga delar.");
  }
  return value;
}

export async function verifiedFurnitureDownload(result: FurnitureExportResult): Promise<Uint8Array<ArrayBuffer>> {
  if (result.state !== "succeeded" || !result.content_base64 || !/^[a-f0-9]{64}$/.test(result.sha256 ?? "")) {
    throw new Error("Exporten är inte klar för nedladdning.");
  }
  const bytes = Uint8Array.from(atob(result.content_base64), char => char.charCodeAt(0));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const hash = [...new Uint8Array(digest)].map(n => n.toString(16).padStart(2, "0")).join("");
  if (hash !== result.sha256) throw new Error("Exportens kontrollsumma stämmer inte.");
  return bytes;
}

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
  shelf_height_ratios_ppm?: number[];
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
export interface FurnitureManufacturingSelection {
  machine_profile_id: string;
  machine_profile_version: string;
  stock_width_um: number;
  stock_height_um: number;
  stock_grain_axis: "x" | "y" | null;
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
  };
  physical_cutting_authorized: false;
  production_qualified: false;
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
    const width = orientation === "YZ" ? size.depth_um : size.width_um;
    const depth = orientation === "XY" ? size.depth_um : size.height_um;
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

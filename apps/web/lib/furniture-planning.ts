import {
  DESIGN_CONSTRAINTS,
  maximumBaseCabinetHeightMm,
} from "./design-constraints";
import { DEFAULT_DESIGN_SPEC } from "./design-types";
import type { FurnitureTemplate, FurnitureTemplateId } from "./furniture-templates";

export type PlanningSpace = "wall" | "alcove" | "freestanding" | "unsure";
export type PlanningUse = "books" | "display" | "mixed" | "concealed" | "unsure";
export type PlanningPriority = "balanced" | "capacity" | "flexibility" | "budget" | "unsure";
export type PlanningStyle = "light" | "natural" | "calm" | "contrast" | "unsure";
export type PlanningStartMode = "recommended" | "template" | "reference" | "scratch";

export interface FurniturePlanningBrief {
  version: 1;
  width_mm: number;
  height_mm: number;
  depth_mm: number;
  space: PlanningSpace;
  primaryUse: PlanningUse;
  priority: PlanningPriority;
  style: PlanningStyle;
  startMode: PlanningStartMode;
  selectedTemplateId?: FurnitureTemplateId;
}

export const DEFAULT_PLANNING_BRIEF: FurniturePlanningBrief = {
  version: 1,
  width_mm: 2_400,
  height_mm: 2_400,
  depth_mm: 340,
  space: "unsure",
  primaryUse: "unsure",
  priority: "unsure",
  style: "unsure",
  startMode: "recommended",
};

export const PLANNING_DIMENSION_LIMITS = Object.freeze({
  width_mm: DESIGN_CONSTRAINTS.widthMm,
  height_mm: DESIGN_CONSTRAINTS.heightMm,
  depth_mm: DESIGN_CONSTRAINTS.depthMm,
});

const TEMPLATE_IDS: readonly FurnitureTemplateId[] = [
  "shelving",
  "wall-library",
  "sideboard",
  "room-divider",
  "hanging-shelf",
  "cupboard",
];

export function isFurniturePlanningBrief(value: unknown): value is FurniturePlanningBrief {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const candidate = value as Partial<FurniturePlanningBrief>;
  return candidate.version === 1
    && typeof candidate.width_mm === "number"
    && candidate.width_mm >= PLANNING_DIMENSION_LIMITS.width_mm.minimum
    && candidate.width_mm <= PLANNING_DIMENSION_LIMITS.width_mm.maximum
    && typeof candidate.height_mm === "number"
    && candidate.height_mm >= PLANNING_DIMENSION_LIMITS.height_mm.minimum
    && candidate.height_mm <= PLANNING_DIMENSION_LIMITS.height_mm.maximum
    && typeof candidate.depth_mm === "number"
    && candidate.depth_mm >= PLANNING_DIMENSION_LIMITS.depth_mm.minimum
    && candidate.depth_mm <= PLANNING_DIMENSION_LIMITS.depth_mm.maximum
    && ["wall", "alcove", "freestanding", "unsure"].includes(candidate.space ?? "")
    && ["books", "display", "mixed", "concealed", "unsure"].includes(candidate.primaryUse ?? "")
    && ["balanced", "capacity", "flexibility", "budget", "unsure"].includes(candidate.priority ?? "")
    && ["light", "natural", "calm", "contrast", "unsure"].includes(candidate.style ?? "")
    && ["recommended", "template", "reference", "scratch"].includes(candidate.startMode ?? "")
    && (candidate.selectedTemplateId === undefined || TEMPLATE_IDS.includes(candidate.selectedTemplateId));
}

export function recommendedTemplateId(brief: FurniturePlanningBrief): FurnitureTemplateId {
  if (brief.space === "freestanding") return "room-divider";
  if (brief.primaryUse === "concealed" || brief.primaryUse === "mixed") return "wall-library";
  return "shelving";
}

export function resolvedPlanningTemplateId(brief: FurniturePlanningBrief): FurnitureTemplateId {
  if (brief.startMode === "scratch") return "shelving";
  if (brief.startMode === "template" && brief.selectedTemplateId) return brief.selectedTemplateId;
  return recommendedTemplateId(brief);
}

function clamp(value: number, min: number, max: number): number {
  return Math.round(Math.min(max, Math.max(min, value)));
}

/** Applies only user-confirmed planning inputs; structural counts remain template-owned. */
export function templateWithPlanningBrief(
  template: FurnitureTemplate,
  brief: FurniturePlanningBrief,
): FurnitureTemplate {
  const scratch = brief.startMode === "scratch";
  const furnitureType = scratch ? "bookcase" : template.patch.furniture_type;
  const nextWidth = clamp(
    brief.width_mm,
    PLANNING_DIMENSION_LIMITS.width_mm.minimum,
    PLANNING_DIMENSION_LIMITS.width_mm.maximum,
  );
  const nextHeight = clamp(
    brief.height_mm,
    PLANNING_DIMENSION_LIMITS.height_mm.minimum,
    PLANNING_DIMENSION_LIMITS.height_mm.maximum,
  );
  const nextDepth = clamp(
    brief.depth_mm,
    PLANNING_DIMENSION_LIMITS.depth_mm.minimum,
    PLANNING_DIMENSION_LIMITS.depth_mm.maximum,
  );
  const shelfCount = clamp(
    template.patch.shelf_count ?? DEFAULT_DESIGN_SPEC.shelf_count,
    DESIGN_CONSTRAINTS.shelfCount.minimum,
    DESIGN_CONSTRAINTS.shelfCount.maximum,
  );
  const dividerCount = clamp(
    template.patch.divider_count ?? DEFAULT_DESIGN_SPEC.divider_count,
    DESIGN_CONSTRAINTS.dividerCount.minimum,
    DESIGN_CONSTRAINTS.dividerCount.maximum,
  );
  const measuredThicknessMm = template.patch.measured_thickness_mm
    ?? DEFAULT_DESIGN_SPEC.measured_thickness_mm;
  const maximumBaseHeightMm = maximumBaseCabinetHeightMm(nextHeight, measuredThicknessMm);
  if (
    furnitureType === "wall_library"
    && maximumBaseHeightMm < DESIGN_CONSTRAINTS.wallLibraryBaseHeightMinimumMm
  ) {
    throw new RangeError(
      `Väggbibliotek kräver mer totalhöjd för ett underskåp på minst ${DESIGN_CONSTRAINTS.wallLibraryBaseHeightMinimumMm} mm.`,
    );
  }
  const baseHeight = furnitureType === "wall_library"
    ? clamp(
        template.patch.base_cabinet_height_mm ?? DESIGN_CONSTRAINTS.wallLibraryBaseHeightMinimumMm,
        DESIGN_CONSTRAINTS.wallLibraryBaseHeightMinimumMm,
        maximumBaseHeightMm,
      )
    : 0;
  const baseCount = furnitureType === "wall_library"
    ? clamp(
        template.patch.base_cabinet_count ?? Math.max(1, dividerCount + 1),
        1,
        DESIGN_CONSTRAINTS.baseCabinetModuleCount.maximum,
      )
    : 0;

  return {
    ...template,
    name: scratch ? "Egen stomme" : template.name,
    description: scratch
      ? "En enkel tom stomme som du formar direkt i Studio"
      : template.description,
    patch: {
      ...template.patch,
      furniture_type: furnitureType,
      width_mm: nextWidth,
      height_mm: nextHeight,
      depth_mm: nextDepth,
      ...(scratch ? {
        shelf_count: 0,
        divider_count: 0,
        bay_sizing_mode: "count" as const,
        bay_width_ratios: [],
        shelf_height_ratios: [],
        base_cabinet_height_mm: 0,
        base_cabinet_depth_mm: 0,
        base_cabinet_count: 0,
      } : {
        shelf_count: shelfCount,
        divider_count: dividerCount,
        base_cabinet_height_mm: baseHeight,
        base_cabinet_depth_mm: furnitureType === "wall_library" ? nextDepth : 0,
        base_cabinet_count: baseCount,
      }),
      ...(brief.priority === "capacity" ? { load_per_shelf_kg: 40 } : {}),
      reinforcement_mode: "auto",
      symmetry_locked: true,
    },
  };
}

export const PLANNING_LABELS = {
  space: {
    wall: "Mot en vägg",
    alcove: "I en nisch",
    freestanding: "Fristående",
    unsure: "Vet inte ännu",
  },
  primaryUse: {
    books: "Mest böcker",
    display: "Visa utvalda saker",
    mixed: "Öppet och dolt",
    concealed: "Mest dold förvaring",
    unsure: "Ingen preferens ännu",
  },
  priority: {
    balanced: "Balans",
    capacity: "Maximal förvaring",
    flexibility: "Flexibel indelning",
    budget: "Enkel konstruktion",
    unsure: "Ingen särskild prioritet",
  },
  style: {
    light: "Ljust och luftigt",
    natural: "Naturligt trä",
    calm: "Lugnt och enhetligt",
    contrast: "Tydlig kontrast",
    unsure: "Ingen stilpreferens ännu",
  },
  startMode: {
    recommended: "Rekommenderad start",
    template: "Välj grundmodell",
    reference: "Utgå från bild",
    scratch: "Bygg från grunden",
  },
} as const;

import type { DesignSpec } from "./design-types";
import { referenceImageVerificationIsCurrent } from "./reference-image";

export type FurnitureTemplateId =
  | "shelving"
  | "wall-library"
  | "sideboard"
  | "room-divider"
  | "hanging-shelf"
  | "cupboard";

export interface FurnitureTemplate {
  id: FurnitureTemplateId;
  name: string;
  description: string;
  feature: string;
  archetypeLabel: string;
  productionLevel: "screened" | "concept";
  limitation?: string;
  preview: "shelf" | "library" | "low" | "divider" | "hanging" | "cupboard";
  patch: Partial<DesignSpec>;
}

export interface TemplatePreviewGeometry {
  x: number;
  top: number;
  width: number;
  openHeight: number;
  baseHeight: number;
  openColumns: number;
  shelfLines: number;
  baseColumns: number;
  hasBase: boolean;
  showBackEdge: boolean;
}

export const FURNITURE_TEMPLATES: readonly FurnitureTemplate[] = [
  {
    id: "shelving",
    name: "Hyllsystem",
    description: "Öppen förvaring från golv till tak",
    feature: "Automatiskt bärande fack",
    archetypeLabel: "Verifierad bokhyllestomme",
    productionLevel: "screened",
    preview: "shelf",
    patch: {
      furniture_type: "bookcase",
      width_mm: 1_800,
      height_mm: 2_100,
      depth_mm: 320,
      shelf_count: 5,
      divider_count: 2,
      base_cabinet_height_mm: 0,
      base_cabinet_depth_mm: 0,
      base_cabinet_count: 0,
      back_panel: true,
      plinth: true,
      reinforcement_mode: "auto",
    },
  },
  {
    id: "wall-library",
    name: "Väggbibliotek",
    description: "Överhyllor och underskåp i samma yttermått",
    feature: "Öppen och stängd förvaring",
    archetypeLabel: "Väggbiblioteksstomme · koncept",
    productionLevel: "concept",
    limitation: "Underskåpens gångjärn, beslag, borrbilder, frontspel och limfria mekaniska retention är ännu inte versionsbundna och verifierade.",
    preview: "library",
    patch: {
      furniture_type: "wall_library",
      width_mm: 4_200,
      height_mm: 2_400,
      depth_mm: 320,
      shelf_count: 5,
      divider_count: 4,
      base_cabinet_height_mm: 680,
      base_cabinet_depth_mm: 320,
      base_cabinet_count: 4,
      back_panel: true,
      plinth: true,
      reinforcement_mode: "auto",
      stock_count: 16,
      back_stock_count: 6,
    },
  },
  {
    id: "sideboard",
    name: "Skänk",
    description: "Låg förvaring med öppen överdel",
    feature: "Valfri modulbredd",
    archetypeLabel: "Väggbiblioteksstomme · koncept",
    productionLevel: "concept",
    limitation: "Skänken använder väggbibliotekets stomlogik och saknar verifierat beslagssystem.",
    preview: "low",
    patch: {
      furniture_type: "wall_library",
      width_mm: 2_000,
      height_mm: 1_100,
      depth_mm: 360,
      shelf_count: 1,
      divider_count: 2,
      base_cabinet_height_mm: 620,
      base_cabinet_depth_mm: 360,
      base_cabinet_count: 3,
      back_panel: true,
      plinth: true,
      reinforcement_mode: "auto",
      stock_count: 8,
    },
  },
  {
    id: "room-divider",
    name: "Rumsavdelare",
    description: "Dubbelsidigt öppet hyllsystem",
    feature: "Utan bakstycke",
    archetypeLabel: "Bokhyllestomme · koncept",
    productionLevel: "concept",
    limitation: "Fristående stabilitet och förankring måste verifieras för rumsavdelaren.",
    preview: "divider",
    patch: {
      furniture_type: "bookcase",
      width_mm: 2_400,
      height_mm: 2_200,
      depth_mm: 420,
      shelf_count: 4,
      divider_count: 3,
      base_cabinet_height_mm: 0,
      base_cabinet_depth_mm: 0,
      base_cabinet_count: 0,
      back_panel: false,
      plinth: true,
      load_per_shelf_kg: 20,
      reinforcement_mode: "manual",
      stock_count: 10,
    },
  },
  {
    id: "hanging-shelf",
    name: "Hänghylla",
    description: "Kompakt vägghängd förvaring",
    feature: "Liten och flexibel",
    archetypeLabel: "Bokhyllestomme · koncept",
    productionLevel: "concept",
    limitation: "Väggbeslag, infästningspunkter och väggens bärförmåga är ännu inte modellerade.",
    preview: "hanging",
    patch: {
      furniture_type: "bookcase",
      width_mm: 900,
      height_mm: 1_200,
      depth_mm: 280,
      shelf_count: 3,
      divider_count: 0,
      base_cabinet_height_mm: 0,
      base_cabinet_depth_mm: 0,
      base_cabinet_count: 0,
      back_panel: true,
      plinth: false,
      load_per_shelf_kg: 15,
      reinforcement_mode: "auto",
    },
  },
  {
    id: "cupboard",
    name: "Förvaringsvägg",
    description: "Hög kombination av skåp och hyllor",
    feature: "Generös dold förvaring",
    archetypeLabel: "Väggbiblioteksstomme · koncept",
    productionLevel: "concept",
    limitation: "Höga dörrar, gångjärn och frontspel saknar verifierad konstruktionsmodell.",
    preview: "cupboard",
    patch: {
      furniture_type: "wall_library",
      width_mm: 1_800,
      height_mm: 2_200,
      depth_mm: 400,
      shelf_count: 3,
      divider_count: 2,
      base_cabinet_height_mm: 1_000,
      base_cabinet_depth_mm: 400,
      base_cabinet_count: 3,
      back_panel: true,
      plinth: true,
      reinforcement_mode: "auto",
      stock_count: 10,
    },
  },
] as const;

export function furnitureTemplate(id: FurnitureTemplateId): FurnitureTemplate {
  return FURNITURE_TEMPLATES.find((template) => template.id === id) ?? FURNITURE_TEMPLATES[0]!;
}

export function hasCustomInteriorLayout(spec: DesignSpec): boolean {
  return spec.bay_width_ratios.length > 0 || spec.shelf_height_ratios.length > 0;
}

export function isReferenceImageDesign(spec: DesignSpec): boolean {
  return spec.reference_image_import?.source === "reference_image"
    && !referenceImageVerificationIsCurrent(spec);
}

export function hasPartCustomization(spec: DesignSpec): boolean {
  return spec.removed_part_ids.length > 0 || Object.keys(spec.part_overrides).length > 0;
}

export function templatePreviewGeometry(template: FurnitureTemplate): TemplatePreviewGeometry {
  const compact = template.preview === "hanging";
  const low = template.preview === "low";
  const hasBase = template.patch.furniture_type === "wall_library"
    && (template.patch.base_cabinet_height_mm ?? 0) > 0;
  const totalHeight = low ? 99 : 120;
  const baseRatio = hasBase
    ? Math.min(0.58, Math.max(0.24, (template.patch.base_cabinet_height_mm ?? 0) / (template.patch.height_mm ?? 1)))
    : 0;
  const baseHeight = Math.round(totalHeight * baseRatio);

  return {
    x: compact ? 95 : 36,
    top: low ? 42 : 20,
    width: compact ? 70 : low ? 188 : 190,
    openHeight: totalHeight - baseHeight,
    baseHeight,
    openColumns: Math.max(1, (template.patch.divider_count ?? 0) + 1),
    shelfLines: Math.max(0, template.patch.shelf_count ?? 0),
    baseColumns: hasBase ? Math.max(1, template.patch.base_cabinet_count ?? 1) : 0,
    hasBase,
    showBackEdge: template.preview !== "divider",
  };
}

export function validateTemplatePreview(template: FurnitureTemplate): string[] {
  const geometry = templatePreviewGeometry(template);
  const errors: string[] = [];
  const numericValues = [
    geometry.x,
    geometry.top,
    geometry.width,
    geometry.openHeight,
    geometry.baseHeight,
    geometry.openColumns,
    geometry.shelfLines,
    geometry.baseColumns,
  ];

  if (numericValues.some((value) => !Number.isFinite(value))) errors.push("Förhandsvisningen innehåller ogiltiga tal.");
  if (geometry.x < 0 || geometry.top < 0 || geometry.x + geometry.width > 260 || geometry.top + geometry.openHeight + geometry.baseHeight > 160) {
    errors.push("Förhandsvisningen ligger utanför bildytan.");
  }
  if (geometry.width <= 0 || geometry.openHeight <= 0) errors.push("Förhandsvisningen saknar synlig storlek.");
  if (geometry.openColumns !== (template.patch.divider_count ?? 0) + 1) errors.push("Antalet öppna fack stämmer inte med modellen.");
  if (geometry.shelfLines !== (template.patch.shelf_count ?? 0)) errors.push("Antalet hyllinjer stämmer inte med modellen.");

  const modelHasBase = template.patch.furniture_type === "wall_library" && (template.patch.base_cabinet_height_mm ?? 0) > 0;
  if (geometry.hasBase !== modelHasBase) errors.push("Underskåpet stämmer inte med modellen.");
  if (geometry.hasBase && geometry.baseColumns !== template.patch.base_cabinet_count) errors.push("Antalet underskåpsmoduler stämmer inte med modellen.");
  if (geometry.hasBase && template.patch.base_cabinet_depth_mm !== template.patch.depth_mm) errors.push("Underskåpets djup ligger utanför modellens yttermått.");
  if (!geometry.hasBase && geometry.baseHeight !== 0) errors.push("En öppen modell visar ett felaktigt underskåp.");
  if (template.preview === "divider" && geometry.showBackEdge) errors.push("Rumsavdelaren visas felaktigt med bakstycke.");

  return errors;
}

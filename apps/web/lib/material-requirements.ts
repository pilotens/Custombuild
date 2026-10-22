import type { DesignSpec, NestingPlacement, ResolvedPart } from "./design-types";
import { partFitsLocalStock, resolveLocalNesting, validLocalNestingDimension } from "./local-nesting";

export interface MaterialRequirementRow {
  role: NestingPlacement["stock_role"];
  materialId: string;
  thicknessMm: number;
  widthMm: number;
  heightMm: number;
  declaredSheets: number;
  estimatedSheets: number;
  additionalSheets: number;
}

export interface MaterialRequirements {
  source: "local-estimate";
  rows: MaterialRequirementRow[];
  oversizedPartIds: string[];
  /** Complete only for this approximate layout, never manufacturing approval. */
  complete: boolean;
}

/**
 * Read-only concept material estimate using the same local layout as screening.
 * Does not reserve/purchase stock, modify workshop profiles, or clear any rule.
 * Grain, kerf, defects and fixtures remain the server/workshop planner's concern.
 */
export function estimateMaterialRequirements(
  spec: DesignSpec,
  parts: readonly ResolvedPart[],
): MaterialRequirements {
  const groups = [
    {
      role: "carcass" as const,
      materialId: spec.material_id,
      thicknessMm: spec.measured_thickness_mm,
      widthMm: spec.stock_width_mm,
      heightMm: spec.stock_height_mm,
      declaredSheets: spec.stock_count,
      parts: parts.filter((part) => part.kind !== "back"),
    },
    {
      role: "back" as const,
      materialId: spec.back_material_id,
      thicknessMm: spec.measured_back_thickness_mm,
      widthMm: spec.back_stock_width_mm,
      heightMm: spec.back_stock_height_mm,
      declaredSheets: spec.back_stock_count,
      parts: parts.filter((part) => part.kind === "back"),
    },
  ];
  // A fitting part always fits on its own sheet; this is a finite upper bound,
  // not an assertion that any of these virtual sheets are available stock.
  const estimate = resolveLocalNesting(spec, parts, {
    carcass: groups[0]!.parts.length,
    back: groups[1]!.parts.length,
  });
  let valid = parts.length > 0;
  const oversizedPartIds: string[] = [];
  const rows = groups.filter((group) => group.parts.length > 0).map((group) => {
    const validFormat = [group.widthMm, group.heightMm].every(validLocalNestingDimension);
    const validCount = Number.isSafeInteger(group.declaredSheets) && group.declaredSheets >= 0;
    valid &&= validFormat && validCount && validLocalNestingDimension(group.thicknessMm);
    for (const part of group.parts) {
      const validPart = [part.width_mm, part.depth_mm, part.thickness_mm].every(validLocalNestingDimension)
        && part.material_id === group.materialId && part.thickness_mm === group.thicknessMm;
      valid &&= validPart;
      if (validFormat && validPart && !partFitsLocalStock(part, group.widthMm, group.heightMm)) {
        oversizedPartIds.push(part.part_id);
      }
    }
    const estimatedSheets = new Set(estimate.placements
      .filter((placement) => placement.stock_role === group.role)
      .map((placement) => placement.sheet)).size;
    const declaredSheets = validCount ? group.declaredSheets : 0;
    return {
      role: group.role,
      materialId: group.materialId,
      thicknessMm: group.thicknessMm,
      widthMm: group.widthMm,
      heightMm: group.heightMm,
      declaredSheets,
      estimatedSheets,
      additionalSheets: Math.max(0, estimatedSheets - declaredSheets),
    };
  });
  return {
    source: "local-estimate",
    rows,
    oversizedPartIds,
    complete: valid && estimate.overflow_part_ids.length === 0,
  };
}

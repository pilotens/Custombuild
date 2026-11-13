import type { DesignSpec, NestingPlacement, NestingResult, ResolvedPart } from "./design-types";

export const LOCAL_NESTING_GAP_MM = 8;

export interface LocalNestingCounts {
  carcass: number;
  back: number;
}

interface NestedPartGroup {
  placements: NestingPlacement[];
  overflow_part_ids: string[];
  used_sheet_count: number;
  used_area_mm2: number;
  available_area_mm2: number;
}

function round(value: number, decimals = 3): number {
  const factor = 10 ** decimals;
  return Math.round(value * factor) / factor;
}

export function validLocalNestingDimension(value: number): boolean {
  // Bound arithmetic as well as rejecting NaN, infinities and non-positive sizes.
  return Number.isFinite(value) && value > 0 && value <= 10_000_000;
}

export function partFitsLocalStock(
  part: ResolvedPart,
  sheetWidth: number,
  sheetHeight: number,
): boolean {
  if (![sheetWidth, sheetHeight, part.width_mm, part.depth_mm].every(validLocalNestingDimension)) {
    return false;
  }
  const margin = 2 * LOCAL_NESTING_GAP_MM;
  return (part.width_mm + margin <= sheetWidth && part.depth_mm + margin <= sheetHeight)
    || (part.depth_mm + margin <= sheetWidth && part.width_mm + margin <= sheetHeight);
}

function nestPartGroup(
  parts: readonly ResolvedPart[],
  sheetWidth: number,
  sheetHeight: number,
  stockCount: number,
  sheetOffset: number,
  stockRole: NestingPlacement["stock_role"],
): NestedPartGroup {
  const ordered = [...parts].sort((left, right) => {
    const areaDifference = right.width_mm * right.depth_mm - left.width_mm * left.depth_mm;
    return Number.isFinite(areaDifference) && areaDifference !== 0
      ? areaDifference
      : left.part_id.localeCompare(right.part_id);
  });
  // One sheet per part bounds the work even if a caller provides a huge count.
  const capacity = Number.isSafeInteger(stockCount) && stockCount >= 0
    ? Math.min(stockCount, parts.length)
    : 0;
  const placements: NestingPlacement[] = [];
  const overflow: string[] = [];
  let sheet = 1;
  let cursorX = LOCAL_NESTING_GAP_MM;
  let cursorY = LOCAL_NESTING_GAP_MM;
  let rowHeight = 0;

  for (const part of ordered) {
    if (capacity === 0 || !partFitsLocalStock(part, sheetWidth, sheetHeight)) {
      overflow.push(part.part_id);
      continue;
    }
    const rawWidth = part.width_mm;
    const rawHeight = part.depth_mm;
    const normalFitsSheet = rawWidth + 2 * LOCAL_NESTING_GAP_MM <= sheetWidth
      && rawHeight + 2 * LOCAL_NESTING_GAP_MM <= sheetHeight;
    const rotatedFitsSheet = rawHeight + 2 * LOCAL_NESTING_GAP_MM <= sheetWidth
      && rawWidth + 2 * LOCAL_NESTING_GAP_MM <= sheetHeight;
    let rotated = !normalFitsSheet && rotatedFitsSheet;
    let placementWidth = rotated ? rawHeight : rawWidth;
    let placementHeight = rotated ? rawWidth : rawHeight;
    if (cursorX + placementWidth + LOCAL_NESTING_GAP_MM > sheetWidth
      && rotatedFitsSheet && cursorX + rawHeight + LOCAL_NESTING_GAP_MM <= sheetWidth) {
      rotated = true;
      placementWidth = rawHeight;
      placementHeight = rawWidth;
    }
    if (cursorX + placementWidth + LOCAL_NESTING_GAP_MM > sheetWidth) {
      cursorX = LOCAL_NESTING_GAP_MM;
      cursorY += rowHeight + LOCAL_NESTING_GAP_MM;
      rowHeight = 0;
      rotated = !normalFitsSheet && rotatedFitsSheet;
      placementWidth = rotated ? rawHeight : rawWidth;
      placementHeight = rotated ? rawWidth : rawHeight;
      if (cursorX + placementWidth + LOCAL_NESTING_GAP_MM > sheetWidth && rotatedFitsSheet) {
        rotated = true;
        placementWidth = rawHeight;
        placementHeight = rawWidth;
      }
    }
    if (cursorY + placementHeight + LOCAL_NESTING_GAP_MM > sheetHeight) {
      // Do not advance onto nonexistent stock. A later, smaller part may still
      // fit the remaining space on this last real sheet.
      if (sheet >= capacity) {
        overflow.push(part.part_id);
        continue;
      }
      sheet += 1;
      cursorX = LOCAL_NESTING_GAP_MM;
      cursorY = LOCAL_NESTING_GAP_MM;
      rowHeight = 0;
      rotated = !normalFitsSheet && rotatedFitsSheet;
      placementWidth = rotated ? rawHeight : rawWidth;
      placementHeight = rotated ? rawWidth : rawHeight;
      if (cursorX + placementWidth + LOCAL_NESTING_GAP_MM > sheetWidth && rotatedFitsSheet) {
        rotated = true;
        placementWidth = rawHeight;
        placementHeight = rawWidth;
      }
    }
    placements.push({
      part_id: part.part_id,
      material_id: part.material_id,
      stock_role: stockRole,
      sheet: sheetOffset + sheet,
      x_mm: round(cursorX),
      y_mm: round(cursorY),
      width_mm: round(placementWidth),
      height_mm: round(placementHeight),
      rotated,
    });
    cursorX += placementWidth + LOCAL_NESTING_GAP_MM;
    rowHeight = Math.max(rowHeight, placementHeight);
  }
  const sheetCount = placements.length === 0
    ? 0
    : Math.max(...placements.map((placement) => placement.sheet)) - sheetOffset;
  return {
    placements,
    overflow_part_ids: overflow,
    used_sheet_count: sheetCount,
    used_area_mm2: placements.reduce((sum, part) => sum + part.width_mm * part.height_mm, 0),
    available_area_mm2: sheetCount > 0 ? sheetCount * sheetWidth * sheetHeight : 0,
  };
}

/** Deterministic local screening only; not workshop nesting or stock verification. */
export function resolveLocalNesting(
  spec: DesignSpec,
  parts: readonly ResolvedPart[],
  counts: LocalNestingCounts = { carcass: spec.stock_count, back: spec.back_stock_count },
): NestingResult {
  const carcass = nestPartGroup(
    parts.filter((part) => part.kind !== "back"),
    spec.stock_width_mm,
    spec.stock_height_mm,
    counts.carcass,
    0,
    "carcass",
  );
  const back = nestPartGroup(
    parts.filter((part) => part.kind === "back"),
    spec.back_stock_width_mm,
    spec.back_stock_height_mm,
    counts.back,
    carcass.used_sheet_count,
    "back",
  );
  const usedArea = carcass.used_area_mm2 + back.used_area_mm2;
  const availableArea = Math.max(carcass.available_area_mm2 + back.available_area_mm2, 1);
  return {
    placements: [...carcass.placements, ...back.placements],
    sheet_count: carcass.used_sheet_count + back.used_sheet_count,
    utilization_percent: round((usedArea / availableArea) * 100, 1),
    overflow_part_ids: [...carcass.overflow_part_ids, ...back.overflow_part_ids],
  };
}

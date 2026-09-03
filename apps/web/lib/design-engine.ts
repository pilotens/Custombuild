import {
  BACK_MATERIALS,
  MACHINES,
  MATERIALS,
  type BomLine,
  type CamOperation,
  type ChangeDiff,
  type DesignSpec,
  type ManufacturingFeature,
  type NestingPlacement,
  type NestingResult,
  type PartOverride,
  type ResolvedDesign,
  type ResolvedPart,
  type RuleEvaluation,
  type ValidationStatus,
} from "./design-types";
import { DESIGN_CONSTRAINTS, maximumBaseCabinetHeightMm } from "./design-constraints";

const GRAVITY = 9.80665;
const BACK_THICKNESS_MM = 6;
const BACK_INSET_MM = 12;
const DOGBONE_BACK_CLEARANCE_MM = 4;
const OPEN_BACK_EDGE_CLEARANCE_MM = 2;
const NESTING_GAP_MM = 8;
const SHELF_SIDE_CLEARANCE_MM = 1;
const RATIO_COMPARISON_TOLERANCE = 1e-9;
export const MAX_BAY_COUNT = DESIGN_CONSTRAINTS.bayCount.maximum;
export const MIN_TARGET_BAY_WIDTH_MM = 50;
export const MAX_TARGET_BAY_WIDTH_MM = 2_000;
const MIN_BASE_CABINET_OPENING_MM = DESIGN_CONSTRAINTS.minimumBaseCabinetOpeningMm;

function customBayRatiosAreFeasible(count: number): boolean {
  return Number.isInteger(count) && count > 0 && count * 0.08 <= 1;
}

function customShelfRatiosAreFeasible(count: number): boolean {
  if (!Number.isInteger(count) || count < 0) return false;
  if (count === 0) return true;
  return 0.05 + (count - 1) * 0.05 <= 0.95 + RATIO_COMPARISON_TOLERANCE;
}

function interiorPanelDepth(
  depthMm: number,
  hasBackPanel: boolean,
  backPanelType: DesignSpec["back_panel_type"] = "inset_groove",
): number {
  const clearance = !hasBackPanel
    ? OPEN_BACK_EDGE_CLEARANCE_MM
    : backPanelType === "surface_mounted"
      ? BACK_THICKNESS_MM + OPEN_BACK_EDGE_CLEARANCE_MM
      : BACK_INSET_MM + BACK_THICKNESS_MM + DOGBONE_BACK_CLEARANCE_MM;
  return Math.max(depthMm - clearance, 1);
}

function round(value: number, decimals = 3): number {
  const factor = 10 ** decimals;
  return Math.round(value * factor) / factor;
}

export interface TargetBayLayout {
  bayCount: number;
  dividerCount: number;
  actualClearWidthMm: number;
  targetMet: boolean;
  limitedByMaximum: boolean;
}

type TargetBayLayoutInput = Pick<DesignSpec,
  "width_mm" | "measured_thickness_mm" | "target_bay_width_mm"
> & Partial<Pick<DesignSpec, "furniture_type" | "base_cabinet_count">>;

function millimetresToMicrometres(value: number, fallback: number): number {
  const finite = Number.isFinite(value) ? value : fallback;
  return Math.round(finite * 1_000);
}

/**
 * Resolve the largest equal-bay grid whose clear openings stay at or above
 * the requested width. Side panels and every internal divider consume their
 * actual measured thickness, matching the production domain geometry.
 */
export function targetBayLayout(spec: TargetBayLayoutInput): TargetBayLayout {
  const widthUm = Math.max(1, millimetresToMicrometres(spec.width_mm, 1));
  const thicknessUm = Math.max(1, millimetresToMicrometres(spec.measured_thickness_mm, 0.001));
  const requestedTargetUm = Math.min(
    millimetresToMicrometres(MAX_TARGET_BAY_WIDTH_MM, MAX_TARGET_BAY_WIDTH_MM),
    Math.max(
      millimetresToMicrometres(MIN_TARGET_BAY_WIDTH_MM, MIN_TARGET_BAY_WIDTH_MM),
      millimetresToMicrometres(spec.target_bay_width_mm, 300),
    ),
  );
  const hasBaseCabinets = spec.furniture_type === "wall_library"
    && (spec.base_cabinet_count ?? 0) > 0;
  const effectiveTargetUm = Math.max(
    requestedTargetUm,
    hasBaseCabinets ? millimetresToMicrometres(MIN_BASE_CABINET_OPENING_MM, 200) : 0,
  );
  const unconstrainedCount = Math.max(
    1,
    Math.floor((widthUm - thicknessUm) / (effectiveTargetUm + thicknessUm)),
  );
  const bayCount = Math.min(MAX_BAY_COUNT, unconstrainedCount);
  const dividerCount = bayCount - 1;
  const clearWidthUm = Math.max(
    1,
    Math.floor((widthUm - (bayCount + 1) * thicknessUm) / bayCount),
  );
  return {
    bayCount,
    dividerCount,
    actualClearWidthMm: round(clearWidthUm / 1_000, 1),
    targetMet: clearWidthUm >= effectiveTargetUm,
    limitedByMaximum: unconstrainedCount > MAX_BAY_COUNT,
  };
}

/** Clear width of the current equal grid; custom ratios are intentionally excluded. */
export function currentEqualBayWidthMm(spec: Pick<DesignSpec,
  "width_mm" | "measured_thickness_mm" | "divider_count"
>): number {
  const bayCount = Math.max(1, Math.trunc(spec.divider_count) + 1);
  const widthUm = Math.max(1, millimetresToMicrometres(spec.width_mm, 1));
  const thicknessUm = Math.max(1, millimetresToMicrometres(spec.measured_thickness_mm, 0.001));
  const clearWidthUm = Math.max(
    1,
    Math.floor((widthUm - (bayCount + 1) * thicknessUm) / bayCount),
  );
  return round(
    clearWidthUm / 1_000,
    1,
  );
}

function normalizedRatios(ratios: number[], count: number): number[] {
  if (count <= 0) return [];
  if (ratios.length !== count || ratios.some((value) => !Number.isFinite(value) || value <= 0)) {
    return Array.from({ length: count }, () => 1 / count);
  }
  const total = ratios.reduce((sum, value) => sum + value, 0);
  if (total <= 0) return Array.from({ length: count }, () => 1 / count);
  return ratios.map((value) => value / total);
}

/** Mirror bay widths left-to-right while preserving their combined proportions. */
export function symmetrizeBayRatios(ratios: number[], count: number): number[] {
  const normalized = normalizedRatios(ratios, count);
  const next = [...normalized];
  for (let index = 0; index < Math.floor(count / 2); index += 1) {
    const mirror = count - 1 - index;
    const average = (normalized[index]! + normalized[mirror]!) / 2;
    next[index] = average;
    next[mirror] = average;
  }
  return normalizedRatios(next, count).map((value) => round(value, 6));
}

function validShelfRatios(ratios: number[], count: number): number[] {
  if (
    ratios.length !== count
    || ratios.some((value, index, values) => (
      !Number.isFinite(value)
      || value < 0.05 - RATIO_COMPARISON_TOLERANCE
      || value > 0.95 + RATIO_COMPARISON_TOLERANCE
      || (index > 0 && value - values[index - 1]! < 0.05 - RATIO_COMPARISON_TOLERANCE)
    ))
  ) return Array.from({ length: count }, (_, index) => (index + 1) / (count + 1));
  return ratios;
}

/** Mirror shelf centres bottom-to-top around the usable shelf zone. */
export function symmetrizeShelfRatios(ratios: number[], count: number): number[] {
  const source = validShelfRatios(ratios, count);
  const next = [...source];
  for (let index = 0; index < Math.floor(count / 2); index += 1) {
    const mirror = count - 1 - index;
    const lower = (source[index]! + (1 - source[mirror]!)) / 2;
    next[index] = lower;
    next[mirror] = 1 - lower;
  }
  if (count % 2 === 1) next[Math.floor(count / 2)] = 0.5;
  return next.map((value) => round(value, 6));
}

/**
 * A wall-library depth is its outer depth. Lower modules are derived from that
 * dimension so persisted or imported legacy values cannot escape the carcass.
 */
export function normalizeBaseCabinetDepth(spec: DesignSpec): DesignSpec {
  if (
    spec.furniture_type !== "wall_library"
    || spec.base_cabinet_count < 1
    || Object.is(spec.base_cabinet_depth_mm, spec.depth_mm)
  ) return spec;
  return { ...spec, base_cabinet_depth_mm: spec.depth_mm };
}

/** Balance valid geometry when symmetry is enabled; validation owns invalid legacy state. */
export function balanceDesignSymmetry(spec: DesignSpec): DesignSpec {
  if (!spec.symmetry_locked) return spec;
  const bayCount = Math.max(1, Math.trunc(spec.divider_count) + 1);
  const shelfCount = Math.max(0, Math.trunc(spec.shelf_count));
  const bayRatiosCanBeBalanced = spec.bay_width_ratios.length === bayCount
    && spec.bay_width_ratios.every((value) => Number.isFinite(value) && value > 0);
  const shelfRatiosCanBeBalanced = spec.shelf_height_ratios.length === shelfCount
    && spec.shelf_height_ratios.every((value, index, values) => (
      Number.isFinite(value)
      && value >= 0.05 - RATIO_COMPARISON_TOLERANCE
      && value <= 0.95 + RATIO_COMPARISON_TOLERANCE
      && (index === 0 || value - values[index - 1]! >= 0.05 - RATIO_COMPARISON_TOLERANCE)
  ));
  return {
    ...spec,
    bay_width_ratios: spec.bay_width_ratios.length === 0 || !bayRatiosCanBeBalanced
      ? spec.bay_width_ratios
      : symmetrizeBayRatios(spec.bay_width_ratios, bayCount),
    shelf_height_ratios: spec.shelf_height_ratios.length === 0 || !shelfRatiosCanBeBalanced
      ? spec.shelf_height_ratios
      : symmetrizeShelfRatios(spec.shelf_height_ratios, shelfCount),
  };
}

function distributeRatioRemainder(
  source: number[],
  fixedIndexes: Set<number>,
  remainingTotal: number,
  minimum: number | readonly number[],
): number[] {
  const next = [...source];
  const freeIndexes = source.map((_, index) => index).filter((index) => !fixedIndexes.has(index));
  if (freeIndexes.length === 0) return next;
  const minimumAt = (index: number) => typeof minimum === "number" ? minimum : minimum[index]!;
  const minimumTotal = freeIndexes.reduce((sum, index) => sum + minimumAt(index), 0);
  const excess = Math.max(0, remainingTotal - minimumTotal);
  const weights = freeIndexes.map((index) => Math.max(0, source[index]! - minimumAt(index)));
  const weightTotal = weights.reduce((sum, value) => sum + value, 0);
  freeIndexes.forEach((index, freeIndex) => {
    const share = weightTotal > 0 ? weights[freeIndex]! / weightTotal : 1 / freeIndexes.length;
    next[index] = minimumAt(index) + excess * share;
  });
  return next;
}

/** Change one bay width; with symmetry locked its opposite bay follows automatically. */
export function setBayWidthRatio(spec: DesignSpec, bayIndex: number, requested: number): DesignSpec {
  const count = Math.max(1, Math.trunc(spec.divider_count) + 1);
  if (count < 2 || !customBayRatiosAreFeasible(count) || !Number.isFinite(requested)) return spec;
  const index = Math.min(count - 1, Math.max(0, Math.trunc(bayIndex)));
  const source = spec.symmetry_locked
    ? symmetrizeBayRatios(spec.bay_width_ratios, count)
    : normalizedRatios(spec.bay_width_ratios, count);
  const minimum = 0.08;

  if (!spec.symmetry_locked) {
    const neighbor = index === count - 1 ? index - 1 : index + 1;
    const pairTotal = source[index]! + source[neighbor]!;
    const value = Math.min(pairTotal - minimum, Math.max(minimum, requested));
    const next = [...source];
    next[index] = value;
    next[neighbor] = pairTotal - value;
    return {
      ...spec,
      bay_sizing_mode: "count",
      reinforcement_mode: "manual",
      bay_width_ratios: next.map((ratio) => round(ratio, 6)),
    };
  }

  const mirror = count - 1 - index;
  const fixedIndexes = new Set([index, mirror]);
  const slots = fixedIndexes.size;
  const maximum = (1 - minimum * (count - slots)) / slots;
  const value = Math.min(maximum, Math.max(minimum, requested));
  let next = distributeRatioRemainder(source, fixedIndexes, 1 - value * slots, minimum);
  next[index] = value;
  next[mirror] = value;
  next = symmetrizeBayRatios(next, count);
  return {
    ...spec,
    bay_sizing_mode: "count",
    reinforcement_mode: "manual",
    bay_width_ratios: next,
  };
}

/** Change one shelf centre; with symmetry locked its vertically opposite row follows. */
export function setShelfHeightRatio(spec: DesignSpec, shelfIndex: number, requested: number): DesignSpec {
  const count = Math.max(0, Math.trunc(spec.shelf_count));
  if (count === 0 || !customShelfRatiosAreFeasible(count) || !Number.isFinite(requested)) return spec;
  const index = Math.min(count - 1, Math.max(0, Math.trunc(shelfIndex)));
  const source = spec.symmetry_locked
    ? symmetrizeShelfRatios(spec.shelf_height_ratios, count)
    : validShelfRatios(spec.shelf_height_ratios, count);
  const gap = 0.05;

  if (!spec.symmetry_locked) {
    const lower = index === 0 ? gap : source[index - 1]! + gap;
    const upper = index === count - 1 ? 1 - gap : source[index + 1]! - gap;
    const next = [...source];
    next[index] = Math.min(upper, Math.max(lower, requested));
    return { ...spec, shelf_height_ratios: next.map((ratio) => round(ratio, 6)) };
  }

  const mirror = count - 1 - index;
  if (mirror === index) {
    const next = [...source];
    next[index] = 0.5;
    return { ...spec, shelf_height_ratios: next };
  }
  const lowerIndex = Math.min(index, mirror);
  const desiredLower = index === lowerIndex ? requested : 1 - requested;
  const lower = lowerIndex === 0 ? gap : source[lowerIndex - 1]! + gap;
  const upper = lowerIndex + 1 === mirror ? (1 - gap) / 2 : source[lowerIndex + 1]! - gap;
  const value = Math.min(upper, Math.max(lower, desiredLower));
  const next = [...source];
  next[lowerIndex] = value;
  next[mirror] = 1 - value;
  return { ...spec, shelf_height_ratios: symmetrizeShelfRatios(next, count) };
}

function bayWidths(spec: DesignSpec, innerWidth: number): number[] {
  return normalizedRatios(spec.bay_width_ratios, Math.max(1, Math.trunc(spec.divider_count) + 1))
    .map((ratio) => innerWidth * ratio);
}

function shelfHeightRatios(spec: DesignSpec): number[] {
  const count = Math.max(0, Math.trunc(spec.shelf_count));
  return validShelfRatios(spec.shelf_height_ratios, count);
}

function shelfVerticalBounds(spec: DesignSpec): { bottomMm: number; topMm: number; thicknessMm: number } {
  const thicknessMm = Math.max(spec.measured_thickness_mm, 0.1);
  const plinthHeight = spec.plinth ? spec.plinth_height_mm : 0;
  const bottomMm = spec.furniture_type === "wall_library" && spec.base_cabinet_count > 0
    ? plinthHeight + spec.base_cabinet_height_mm
    : plinthHeight + thicknessMm;
  return { bottomMm, topMm: spec.height_mm - thicknessMm, thicknessMm };
}

function rawShelfOpeningHeights(spec: DesignSpec): number[] {
  const { bottomMm, topMm, thicknessMm } = shelfVerticalBounds(spec);
  const ratios = shelfHeightRatios(spec);
  const innerHeight = Math.max(topMm - bottomMm, 1);
  if (ratios.length === 0) return [innerHeight];
  const centers = ratios.map((ratio) => bottomMm + innerHeight * ratio);
  const openings: number[] = [];
  let previousTop = bottomMm;
  for (const center of centers) {
    openings.push(Math.max(0, center - thicknessMm / 2 - previousTop));
    previousTop = center + thicknessMm / 2;
  }
  openings.push(Math.max(0, topMm - previousTop));
  return openings;
}

/** Clear edge-to-edge openings below, between and above the shelf rows. */
export function shelfOpeningHeights(spec: DesignSpec): number[] {
  return rawShelfOpeningHeights(spec).map((heightMm) => round(heightMm, 1));
}

/** Set one exact clear opening while retaining total height and, when enabled, symmetry. */
export function setShelfOpeningHeight(spec: DesignSpec, openingIndex: number, requestedMm: number): DesignSpec {
  const count = Math.max(0, Math.trunc(spec.shelf_count));
  if (count === 0 || !customShelfRatiosAreFeasible(count) || !Number.isFinite(requestedMm)) return spec;
  const openings = rawShelfOpeningHeights(spec);
  const index = Math.min(openings.length - 1, Math.max(0, Math.trunc(openingIndex)));
  const { bottomMm, topMm, thicknessMm } = shelfVerticalBounds(spec);
  const innerHeight = Math.max(topMm - bottomMm, 1);
  const minimumInternalOpeningMm = Math.max(
    DESIGN_CONSTRAINTS.minimumShelfOpeningMm,
    Math.ceil(innerHeight * 0.05 - thicknessMm),
  );
  const minimumEdgeOpeningMm = Math.max(
    DESIGN_CONSTRAINTS.minimumShelfOpeningMm,
    Math.ceil(innerHeight * 0.05 - thicknessMm / 2),
  );
  const minimumOpenings = openings.map((_, openingIndex) => (
    openingIndex === 0 || openingIndex === openings.length - 1
      ? minimumEdgeOpeningMm
      : minimumInternalOpeningMm
  ));
  let nextOpenings: number[];

  if (spec.symmetry_locked) {
    const symmetricOpenings = [...openings];
    for (let current = 0; current < Math.floor(openings.length / 2); current += 1) {
      const mirror = openings.length - 1 - current;
      const average = (openings[current]! + openings[mirror]!) / 2;
      symmetricOpenings[current] = average;
      symmetricOpenings[mirror] = average;
    }
    const mirror = openings.length - 1 - index;
    const fixedIndexes = new Set([index, mirror]);
    const slots = fixedIndexes.size;
    const total = symmetricOpenings.reduce((sum, value) => sum + value, 0);
    const minimumFixedOpeningMm = Math.max(minimumOpenings[index]!, minimumOpenings[mirror]!);
    const freeMinimumTotal = minimumOpenings.reduce((sum, value, openingIndex) => (
      fixedIndexes.has(openingIndex) ? sum : sum + value
    ), 0);
    const maximum = Math.max(
      minimumFixedOpeningMm,
      (total - freeMinimumTotal) / slots,
    );
    const value = Math.min(maximum, Math.max(minimumFixedOpeningMm, Math.round(requestedMm)));
    nextOpenings = distributeRatioRemainder(
      symmetricOpenings,
      fixedIndexes,
      total - value * slots,
      minimumOpenings,
    );
    nextOpenings[index] = value;
    nextOpenings[mirror] = value;
  } else {
    const neighbor = index === openings.length - 1 ? index - 1 : index + 1;
    if (neighbor < 0) return spec;
    const pairTotal = openings[index]! + openings[neighbor]!;
    const minimumOpeningMm = minimumOpenings[index]!;
    const maximum = Math.max(minimumOpeningMm, pairTotal - minimumOpenings[neighbor]!);
    const value = Math.min(maximum, Math.max(minimumOpeningMm, Math.round(requestedMm)));
    nextOpenings = [...openings];
    nextOpenings[index] = value;
    nextOpenings[neighbor] = pairTotal - value;
  }

  const ratios: number[] = [];
  let previousTop = bottomMm;
  for (let shelfIndex = 0; shelfIndex < count; shelfIndex += 1) {
    const center = previousTop + nextOpenings[shelfIndex]! + thicknessMm / 2;
    ratios.push(round((center - bottomMm) / innerHeight, 6));
    previousTop = center + thicknessMm / 2;
  }
  return { ...spec, shelf_height_ratios: ratios };
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, child]) => `${JSON.stringify(key)}:${canonicalJson(child)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

/** Stable local preview identifier. Production hashes are returned by the API. */
export function localDesignHash(spec: DesignSpec): string {
  const hashSpec: Partial<DesignSpec> = { ...spec };
  delete hashSpec.bay_sizing_mode;
  delete hashSpec.target_bay_width_mm;
  const text = canonicalJson(hashSpec);
  let left = 0x811c9dc5;
  let right = 0x9e3779b9;
  for (let index = 0; index < text.length; index += 1) {
    const code = text.charCodeAt(index);
    left = Math.imul(left ^ code, 0x01000193) >>> 0;
    right = Math.imul(right ^ code, 0x85ebca6b) >>> 0;
  }
  const token = `${left.toString(16).padStart(8, "0")}${right.toString(16).padStart(8, "0")}`;
  return token.repeat(4);
}

function partWeightKg(widthMm: number, depthMm: number, thicknessMm: number, density: number): number {
  return round((widthMm * depthMm * thicknessMm * density) / 1_000_000_000, 2);
}

function makeFeature(
  partId: string,
  suffix: string,
  kind: ManufacturingFeature["kind"],
  face: ManufacturingFeature["face"],
  depthMm: number,
  description: string,
  toolDiameterMm?: number,
): ManufacturingFeature {
  return {
    id: `${partId}:${suffix}`,
    kind,
    face,
    depth_mm: round(depthMm),
    description,
    ...(toolDiameterMm === undefined ? {} : { tool_diameter_mm: toolDiameterMm }),
  };
}

function commonFeatures(partId: string, thickness: number): ManufacturingFeature[] {
  return [
    makeFeature(partId, "outline", "outline", "A", thickness, "Utvändig kontur", 6),
    makeFeature(partId, "label", "label", "A", 0.4, "Part-ID och orienteringsmärke", 3),
  ];
}

/** Mirror the domain engine's versioned dado depth in millimetres. */
function dadoDepthMm(thicknessMm: number): number {
  const oneThirdToMicron = Math.floor((thicknessMm * 1_000) / 3) / 1_000;
  return Math.max(1, Math.min(12, oneThirdToMicron));
}

function generateParts(spec: DesignSpec): ResolvedPart[] {
  const width = Math.max(spec.width_mm, 1);
  const height = Math.max(spec.height_mm, 1);
  const depth = Math.max(spec.depth_mm, 1);
  const thickness = Math.max(spec.measured_thickness_mm, 0.1);
  const dadoDepth = dadoDepthMm(thickness);
  const dividerCount = Math.max(0, Math.trunc(spec.divider_count));
  const shelfCount = Math.max(0, Math.trunc(spec.shelf_count));
  const hasBaseCabinets = spec.furniture_type === "wall_library" && spec.base_cabinet_count > 0;
  const baseCabinetCount = hasBaseCabinets ? Math.max(1, Math.trunc(spec.base_cabinet_count)) : 0;
  const plinthHeight = spec.plinth ? spec.plinth_height_mm : 0;
  const shelfZoneBottom = hasBaseCabinets
    ? plinthHeight + spec.base_cabinet_height_mm
    : plinthHeight + thickness;
  const materialCatalog = [...MATERIALS, ...BACK_MATERIALS];
  const materialForId = (materialId: string) => {
    const match = materialCatalog.find((candidate) => candidate.id === materialId);
    if (!match) throw new Error(`Unknown material identity: ${materialId}`);
    return match;
  };
  materialForId(spec.material_id);
  const innerWidth = Math.max(width - 2 * thickness - dividerCount * thickness, 1);
  const bayCount = dividerCount + 1;
  const resolvedBayWidths = bayWidths(spec, innerWidth);
  const resolvedBayStarts = resolvedBayWidths.map((_, bayIndex) => (
    thickness
    + resolvedBayWidths.slice(0, bayIndex).reduce((sum, value) => sum + value, 0)
    + bayIndex * thickness
  ));
  const innerHeight = Math.max(height - shelfZoneBottom - thickness, 1);
  const shelfDepth = interiorPanelDepth(depth, spec.back_panel, spec.back_panel_type);
  const dividerDepth = spec.back_panel && spec.back_panel_type === "inset_groove"
    ? Math.max(depth - BACK_INSET_MM, 1)
    : shelfDepth;
  const caseDepth = spec.back_panel && spec.back_panel_type === "surface_mounted"
    ? Math.max(depth - BACK_THICKNESS_MM, 1)
    : depth;
  const parts: ResolvedPart[] = [];

  const addPart = (
    part: Omit<ResolvedPart, "material_id" | "weight_kg">,
    materialId = spec.material_id,
  ) => {
    const partMaterial = materialForId(materialId);
    parts.push({
      ...part,
      material_id: partMaterial.id,
      weight_kg: partWeightKg(
        part.width_mm,
        part.depth_mm,
        part.thickness_mm,
        partMaterial.densityKgM3,
      ),
    });
  };

  for (const side of ["left", "right"] as const) {
    const partId = `side-${side}`;
    const features = commonFeatures(partId, thickness);
    if (spec.back_panel) {
      const surfaceMounted = spec.back_panel_type === "surface_mounted";
      features.push(
        makeFeature(
          partId,
          surfaceMounted ? "back-rabbet" : "back-groove",
          surfaceMounted ? "rabbet" : "groove",
          side === "left" ? "B" : "A",
          dadoDepth,
          surfaceMounted ? "Fals för utanpåliggande bakstycke" : "Not för bakstycke",
          6,
        ),
      );
    }
    if (hasBaseCabinets) {
      features.push(
        makeFeature(
          partId,
          "base-bottom-support",
          "groove",
          "A",
          dadoDepth,
          "Spår för yttersta underskåpsbotten",
          6,
        ),
      );
    }
    for (let shelfIndex = 0; shelfIndex < shelfCount; shelfIndex += 1) {
      features.push(
        makeFeature(
          partId,
          `shelf-joint-${shelfIndex + 1}`,
          spec.fixed_shelves ? "groove" : "drill",
          "A",
          spec.fixed_shelves ? Math.min(thickness / 3, 6) : 12,
          spec.fixed_shelves
            ? `Not för fast hylla ${shelfIndex + 1}`
            : `Hyllbärarhål för hylla ${shelfIndex + 1}`,
          spec.fixed_shelves ? 6 : 5,
        ),
      );
    }
    addPart({
      part_id: partId,
      name: side === "left" ? "Vänster gavel" : "Höger gavel",
      kind: "side",
      width_mm: height,
      depth_mm: caseDepth,
      thickness_mm: thickness,
      position_mm: {
        x: side === "left" ? thickness / 2 : width - thickness / 2,
        y: caseDepth / 2,
        z: height / 2,
      },
      orientation: "YZ",
      color: "#c7a97c",
      features,
    });
  }

  for (const horizontal of ["bottom", "top"] as const) {
    const partId = horizontal;
    const features = commonFeatures(partId, thickness);
    if (spec.back_panel) {
      const surfaceMounted = spec.back_panel_type === "surface_mounted";
      features.push(
        makeFeature(
          partId,
          surfaceMounted ? "back-rabbet" : "back-groove",
          surfaceMounted ? "rabbet" : "groove",
          horizontal === "top" ? "A" : "B",
          dadoDepth,
          surfaceMounted ? "Fals för utanpåliggande bakstycke" : "Not för bakstycke",
          6,
        ),
      );
    }
    if (horizontal === "bottom" && hasBaseCabinets) {
      for (let supportIndex = 1; supportIndex < baseCabinetCount; supportIndex += 1) {
        features.push(
          makeFeature(
            partId,
            `base-side-support-${supportIndex}`,
            "groove",
            "B",
            dadoDepth,
            `Undersidespår för intern underskåpssida ${supportIndex}`,
            6,
          ),
        );
      }
    }
    addPart({
      part_id: partId,
      name: horizontal === "bottom" ? "Botten" : "Topp",
      kind: horizontal,
      width_mm: Math.max(width - 2 * thickness + 2 * dadoDepth, 1),
      depth_mm: caseDepth,
      thickness_mm: thickness,
      position_mm: {
        x: width / 2,
        y: caseDepth / 2,
        z: horizontal === "bottom" ? shelfZoneBottom - thickness / 2 : height - thickness / 2,
      },
      orientation: "XY",
      color: "#d3b78d",
      features,
    });
  }

  for (let dividerIndex = 0; dividerIndex < dividerCount; dividerIndex += 1) {
    const partId = `divider-${dividerIndex + 1}`;
    const precedingWidth = resolvedBayWidths.slice(0, dividerIndex + 1).reduce((sum, value) => sum + value, 0);
    const x = thickness + precedingWidth + thickness * (dividerIndex + 0.5);
    addPart({
      part_id: partId,
      name: `Vertikal avdelare ${dividerIndex + 1}`,
      kind: "divider",
      width_mm: innerHeight + 2 * dadoDepth,
      depth_mm: dividerDepth,
      thickness_mm: thickness,
      position_mm: { x, y: dividerDepth / 2, z: shelfZoneBottom + innerHeight / 2 },
      orientation: "YZ",
      color: "#b8925d",
      features: [
        ...commonFeatures(partId, thickness),
        makeFeature(partId, "top-joint", "groove", "EDGE", Math.min(thickness / 3, 6), "Spåranslutning mot topp", 6),
        makeFeature(partId, "bottom-joint", "groove", "EDGE", Math.min(thickness / 3, 6), "Spåranslutning mot botten", 6),
      ],
    });
  }

  const resolvedShelfRatios = shelfHeightRatios(spec);
  for (let shelfIndex = 0; shelfIndex < shelfCount; shelfIndex += 1) {
    const z = shelfZoneBottom + innerHeight * resolvedShelfRatios[shelfIndex]!;
    for (let bayIndex = 0; bayIndex < bayCount; bayIndex += 1) {
      const partId = `shelf-${shelfIndex + 1}-bay-${bayIndex + 1}`;
      const precedingWidth = resolvedBayWidths.slice(0, bayIndex).reduce((sum, value) => sum + value, 0);
      const currentBayWidth = resolvedBayWidths[bayIndex]!;
      const x = thickness + precedingWidth + bayIndex * thickness + currentBayWidth / 2;
      const features = commonFeatures(partId, thickness);
      const shelfWidth = spec.fixed_shelves
        ? currentBayWidth + 2 * dadoDepth
        : Math.max(currentBayWidth - 2 * SHELF_SIDE_CLEARANCE_MM, 1);
      addPart({
        part_id: partId,
        name: bayCount === 1 ? `Hylla ${shelfIndex + 1}` : `Hylla ${shelfIndex + 1}.${bayIndex + 1}`,
        kind: "shelf",
        width_mm: shelfWidth,
        depth_mm: shelfDepth,
        thickness_mm: thickness,
        position_mm: { x, y: shelfDepth / 2, z },
        orientation: "XY",
        color: "#d7bc91",
        features,
      });
    }
  }

  if (hasBaseCabinets) {
    const baseDepth = depth;
    const availableWidth = Math.max(width - (baseCabinetCount + 1) * thickness, 1);
    const baseOpeningWidths = baseCabinetCount === resolvedBayWidths.length
      ? resolvedBayWidths
      : Array.from({ length: baseCabinetCount }, () => availableWidth / baseCabinetCount);
    const baseSideLeftEdges = [0];
    let baseCursor = 0;
    for (const openingWidth of baseOpeningWidths) {
      baseCursor += thickness + openingWidth;
      baseSideLeftEdges.push(baseCursor);
    }
    const frontGap = 2;

    for (let boundaryIndex = 1; boundaryIndex < baseCabinetCount; boundaryIndex += 1) {
      const sideIndex = boundaryIndex;
      // Full-height carcass sides are the two outer supports. UI IDs stay
      // aligned with server normalisation: semantic base-side-1 becomes
      // base-side-2, so internal supports always use the range 2..N.
      const partId = `base-side-${boundaryIndex + 1}`;
      const x = baseSideLeftEdges[boundaryIndex]! + thickness / 2;
      const panelHeight = Math.max(
        spec.base_cabinet_height_mm - thickness + dadoDepth,
        1,
      );
      addPart({
        part_id: partId,
        name: `Underskåpsida ${sideIndex + 1}`,
        kind: "base_side",
        width_mm: panelHeight,
        depth_mm: baseDepth,
        thickness_mm: thickness,
        position_mm: {
          x,
          y: baseDepth / 2,
          z: plinthHeight + panelHeight / 2,
        },
        orientation: "YZ",
        color: "#8f542e",
        features: [
          ...commonFeatures(partId, thickness),
          makeFeature(
            partId,
            "left-bottom-support",
            "groove",
            "A",
            dadoDepth,
            "Spår för vänster underskåpsbotten",
            6,
          ),
          makeFeature(
            partId,
            "right-bottom-support",
            "groove",
            "B",
            dadoDepth,
            "Spår för höger underskåpsbotten",
            6,
          ),
        ],
      });
    }

    for (let cabinetIndex = 0; cabinetIndex < baseCabinetCount; cabinetIndex += 1) {
      const openingWidth = baseOpeningWidths[cabinetIndex]!;
      const openingX = baseSideLeftEdges[cabinetIndex]! + thickness;
      const bottomId = `base-bottom-${cabinetIndex + 1}`;
      const baseBottomDepth = Math.max(baseDepth - thickness, 1);
      addPart({
        part_id: bottomId,
        name: `Underskåpsbotten ${cabinetIndex + 1}`,
        kind: "base_bottom",
        width_mm: openingWidth + 2 * dadoDepth,
        depth_mm: baseBottomDepth,
        thickness_mm: thickness,
        position_mm: {
          x: openingX + openingWidth / 2,
          y: thickness + baseBottomDepth / 2,
          z: plinthHeight + thickness / 2,
        },
        orientation: "XY",
        color: "#9b5d32",
        features: commonFeatures(bottomId, thickness),
      });

      const frontId = `cabinet-front-${cabinetIndex + 1}`;
      const frontBottom = plinthHeight + thickness + frontGap;
      const frontTop = shelfZoneBottom - thickness - frontGap;
      const frontHeight = Math.max(frontTop - frontBottom, 1);
      addPart({
        part_id: frontId,
        name: `Skåpsfront ${cabinetIndex + 1}`,
        kind: "cabinet_front",
        width_mm: Math.max(openingWidth - 2 * frontGap, 1),
        depth_mm: frontHeight,
        thickness_mm: thickness,
        position_mm: {
          x: openingX + openingWidth / 2,
          y: thickness / 2,
          z: frontBottom + frontHeight / 2,
        },
        orientation: "XZ",
        color: "#7d4223",
        features: commonFeatures(frontId, thickness),
      });
    }
  }

  if (spec.back_panel) {
    if (spec.back_panel_type === "surface_mounted") {
      const partId = "back-panel";
      addPart({
        part_id: partId,
        name: "Bakstycke",
        kind: "back",
        width_mm: width,
        depth_mm: height,
        thickness_mm: BACK_THICKNESS_MM,
        position_mm: {
          x: width / 2,
          y: depth - BACK_THICKNESS_MM / 2,
          z: height / 2,
        },
        orientation: "XZ",
        color: "#b99869",
        features: commonFeatures(partId, BACK_THICKNESS_MM),
      }, spec.back_material_id);
    } else {
      for (let bayIndex = 0; bayIndex < bayCount; bayIndex += 1) {
        const partId = bayCount === 1
          ? "back-panel"
          : `back-panel-bay-${bayIndex + 1}`;
        const bayWidth = resolvedBayWidths[bayIndex]!;
        addPart({
          part_id: partId,
          name: bayCount === 1 ? "Bakstycke" : `Bakstycke fack ${bayIndex + 1}`,
          kind: "back",
          width_mm: bayWidth + 2 * dadoDepth,
          depth_mm: innerHeight + 2 * dadoDepth,
          thickness_mm: BACK_THICKNESS_MM,
          position_mm: {
            x: resolvedBayStarts[bayIndex]! + bayWidth / 2,
            y: depth - BACK_INSET_MM - BACK_THICKNESS_MM / 2,
            z: shelfZoneBottom + innerHeight / 2,
          },
          orientation: "XZ",
          color: "#b99869",
          features: commonFeatures(partId, BACK_THICKNESS_MM),
        }, spec.back_material_id);
      }
    }
  }

  if (spec.plinth) {
    const partId = "plinth-front";
    const plinthPanelHeight = hasBaseCabinets ? plinthHeight : plinthHeight + dadoDepth;
    addPart({
      part_id: partId,
      name: "Främre sockel",
      kind: "plinth",
      width_mm: Math.max(width - 2 * thickness, 1),
      depth_mm: plinthPanelHeight,
      thickness_mm: thickness,
      position_mm: { x: width / 2, y: thickness / 2, z: plinthPanelHeight / 2 },
      orientation: "XZ",
      color: "#a98455",
      features: commonFeatures(partId, thickness),
    });
  }

  const removed = new Set(spec.removed_part_ids);
  return parts
    .filter((part) => !removed.has(part.part_id))
    .map((part) => {
      const override = spec.part_overrides[part.part_id];
      if (!override) return part;
      const widthMm = Number.isFinite(override.width_mm) ? Math.max(10, override.width_mm!) : part.width_mm;
      const depthMm = Number.isFinite(override.depth_mm) ? Math.max(10, override.depth_mm!) : part.depth_mm;
      const thicknessMm = Number.isFinite(override.thickness_mm) ? Math.max(1, override.thickness_mm!) : part.thickness_mm;
      const position = {
        x: Number.isFinite(override.position_x_mm) ? override.position_x_mm! : part.position_mm.x,
        y: Number.isFinite(override.position_y_mm) ? override.position_y_mm! : part.position_mm.y,
        z: Number.isFinite(override.position_z_mm) ? override.position_z_mm! : part.position_mm.z,
      };
      return {
        ...part,
        width_mm: widthMm,
        depth_mm: depthMm,
        thickness_mm: thicknessMm,
        position_mm: position,
        weight_kg: partWeightKg(
          widthMm,
          depthMm,
          thicknessMm,
          materialForId(part.material_id).densityKgM3,
        ),
        features: part.features.map((feature) => feature.kind === "outline" ? { ...feature, depth_mm: thicknessMm } : feature),
      };
    });
}

function individualPartRule(spec: DesignSpec): RuleEvaluation | null {
  const changedIds = Object.keys(spec.part_overrides);
  const affectedIds = [...new Set([...spec.removed_part_ids, ...changedIds])];
  const topologyChanged = Boolean(spec.topology_baseline);
  if (affectedIds.length === 0 && !topologyChanged) return null;
  const removedCount = spec.removed_part_ids.length;
  const modifiedCount = changedIds.length;
  const topologyOnly = topologyChanged && removedCount === 0 && modifiedCount === 0;
  return {
    rule_id: "PART-CUSTOM-001",
    rule_version: "1.0.0",
    status: topologyOnly ? "WARNING" : "BLOCK",
    title: topologyOnly ? "Ombyggd bärande indelning" : "Individuellt ändrade delar",
    summary: topologyChanged
      ? `Möbelns bärande indelning har byggts om${removedCount + modifiedCount > 0 ? ` tillsammans med ${removedCount} borttagna och ${modifiedCount} modifierade delar` : ""}. Konstruktionen måste granskas.`
      : `${removedCount} borttagna och ${modifiedCount} modifierade delar måste konstruktionsgranskas.`,
    calculation: "Ändrad del eller topologi ≠ verifierad parametrisk förbandskedja",
    assumptions: topologyOnly
      ? [
          "Alla anslutande hyllsegment, fack och stomdelar har byggts om från samma parametriska DesignSpec.",
          "Den nya indelningen måste fortfarande passera serverns förbands-, bärighets- och CAM-kontroller innan produktion.",
        ]
      : [
          "Fria delmått eller borttagna delar kan inte garantera att angränsande förband följer med.",
          "Bärighet, kollisioner och nya bearbetningsoperationer måste verifieras innan produktion.",
        ],
    affected_part_ids: affectedIds,
  };
}

function topologyIntegrityRule(spec: DesignSpec, parts: ResolvedPart[]): RuleEvaluation {
  const actualIds = new Set(parts.map((part) => part.part_id));
  const expectedIds = ["side-left", "side-right", "bottom", "top"];
  for (let dividerIndex = 1; dividerIndex <= spec.divider_count; dividerIndex += 1) {
    expectedIds.push(`divider-${dividerIndex}`);
  }
  for (let shelfIndex = 1; shelfIndex <= spec.shelf_count; shelfIndex += 1) {
    for (let bayIndex = 1; bayIndex <= spec.divider_count + 1; bayIndex += 1) {
      expectedIds.push(`shelf-${shelfIndex}-bay-${bayIndex}`);
    }
  }
  if (spec.furniture_type === "wall_library" && spec.base_cabinet_count > 0) {
    for (let boundaryIndex = 1; boundaryIndex < spec.base_cabinet_count; boundaryIndex += 1) {
      expectedIds.push(`base-side-${boundaryIndex + 1}`);
    }
    for (let cabinetIndex = 1; cabinetIndex <= spec.base_cabinet_count; cabinetIndex += 1) expectedIds.push(`base-bottom-${cabinetIndex}`);
  }
  const missing = expectedIds.filter((partId) => !actualIds.has(partId));
  return {
    rule_id: "STR-TOPO-001",
    rule_version: "1.0.0",
    status: missing.length > 0 ? "BLOCK" : "PASS",
    title: "Sammanhängande bärande geometri",
    summary: missing.length > 0
      ? `${missing.length} bärande ${missing.length === 1 ? "del saknas" : "delar saknas"}. Det lämnar öppna förband eller hyllplan utan korrekt upplag.`
      : "Ram, avdelare och hyllrader bildar en sammanhängande parametrisk geometri.",
    calculation: `Förväntade bärande delar ${expectedIds.length}; saknade ${missing.length}`,
    assumptions: [
      "Varje hyllrad måste innehålla ett sammanhängande hyllplan i varje aktuellt fack.",
      "Ytterramens sidor, topp och botten behandlas som obligatoriska bärande delar.",
    ],
    affected_part_ids: missing,
  };
}

export interface PartRemovalResult {
  spec: DesignSpec;
  diff: ChangeDiff[];
  topologyRebuilt: boolean;
  notice: string;
}

function topologyBaseline(spec: DesignSpec): NonNullable<DesignSpec["topology_baseline"]> {
  return spec.topology_baseline ?? {
    divider_count: spec.divider_count,
    shelf_count: spec.shelf_count,
    base_cabinet_count: spec.base_cabinet_count,
    bay_width_ratios: [...spec.bay_width_ratios],
    shelf_height_ratios: [...spec.shelf_height_ratios],
    reinforcement_mode: spec.reinforcement_mode,
  };
}

function withoutPartFamilies(spec: DesignSpec, prefixes: string[]): Pick<DesignSpec, "part_overrides" | "removed_part_ids"> {
  return {
    part_overrides: Object.fromEntries(
      Object.entries(spec.part_overrides).filter(([partId]) => !prefixes.some((prefix) => partId.startsWith(prefix))),
    ),
    removed_part_ids: spec.removed_part_ids.filter((partId) => !prefixes.some((prefix) => partId.startsWith(prefix))),
  };
}

/**
 * Remove a generated part while preserving a coherent furniture topology.
 * Grid dividers merge adjacent bays, shelf segments remove their complete row,
 * and inner base dividers merge cabinet modules. Unknown/non-grid parts remain
 * explicit removals and are handled by the structural integrity rule.
 */
export function removePartFromDesign(spec: DesignSpec, partId: string): PartRemovalResult {
  const dividerMatch = /^divider-(\d+)$/.exec(partId);
  if (dividerMatch && spec.divider_count > 0) {
    const dividerIndex = Math.min(spec.divider_count - 1, Math.max(0, Number(dividerMatch[1]) - 1));
    const thickness = Math.max(spec.measured_thickness_mm, 0.1);
    const oldInnerWidth = Math.max(spec.width_mm - 2 * thickness - spec.divider_count * thickness, 1);
    const oldBayWidths = normalizedRatios(spec.bay_width_ratios, spec.divider_count + 1)
      .map((ratio) => ratio * oldInnerWidth);
    const mergedBayWidths = [...oldBayWidths];
    mergedBayWidths.splice(
      dividerIndex,
      2,
      oldBayWidths[dividerIndex]! + thickness + oldBayWidths[dividerIndex + 1]!,
    );
    const newDividerCount = spec.divider_count - 1;
    const newInnerWidth = mergedBayWidths.reduce((sum, width) => sum + width, 0);
    const bayWidthRatios = newDividerCount === 0
      ? []
      : mergedBayWidths.map((width) => round(width / newInnerWidth, 6));
    const cleaned = withoutPartFamilies(spec, ["divider-", "shelf-", "back-panel"]);
    return {
      spec: balanceDesignSymmetry({
        ...spec,
        ...cleaned,
        divider_count: newDividerCount,
        bay_sizing_mode: "count",
        bay_width_ratios: bayWidthRatios,
        reinforcement_mode: "manual",
        topology_baseline: topologyBaseline(spec),
      }),
      diff: [{
        field: "divider_count",
        before: spec.divider_count,
        after: newDividerCount,
        reason: `Avdelare ${dividerIndex + 1} togs bort. Angränsande fack och samtliga hyllplan byggdes om.`,
      }],
      topologyRebuilt: true,
      notice: "Avdelaren togs bort och de två facken slogs ihop. Hyllplanen har räknats om över den nya spännvidden.",
    };
  }

  const shelfMatch = /^shelf-(\d+)-bay-\d+$/.exec(partId);
  if (shelfMatch && spec.shelf_count > 0) {
    const shelfIndex = Math.min(spec.shelf_count - 1, Math.max(0, Number(shelfMatch[1]) - 1));
    const oldLevels = shelfHeightRatios(spec);
    const remainingShelfRatios = oldLevels.filter((_, index) => index !== shelfIndex);
    const newShelfCount = spec.shelf_count - 1;
    const cleaned = withoutPartFamilies(spec, ["shelf-"]);
    return {
      spec: balanceDesignSymmetry({
        ...spec,
        ...cleaned,
        shelf_count: newShelfCount,
        shelf_height_ratios: newShelfCount === 0 ? [] : remainingShelfRatios,
        topology_baseline: topologyBaseline(spec),
      }),
      diff: [{
        field: "shelf_count",
        before: spec.shelf_count,
        after: newShelfCount,
        reason: `Hyllrad ${shelfIndex + 1} togs bort i hela möbeln så inga osammanhängande hål lämnas.`,
      }],
      topologyRebuilt: true,
      notice: "Hela hyllraden togs bort och de återstående hyllorna byggdes om som sammanhängande plan.",
    };
  }

  const baseSideMatch = /^base-side-(\d+)$/.exec(partId);
  const baseSideIndex = baseSideMatch ? Number(baseSideMatch[1]) : 0;
  if (baseSideMatch && baseSideIndex > 1 && baseSideIndex <= spec.base_cabinet_count && spec.base_cabinet_count > 1) {
    const newCabinetCount = spec.base_cabinet_count - 1;
    const cleaned = withoutPartFamilies(spec, ["base-side-", "base-bottom-", "base-top-", "cabinet-front-"]);
    return {
      spec: {
        ...spec,
        ...cleaned,
        base_cabinet_count: newCabinetCount,
        topology_baseline: topologyBaseline(spec),
      },
      diff: [{
        field: "base_cabinet_count",
        before: spec.base_cabinet_count,
        after: newCabinetCount,
        reason: "En inre underskåpsavdelare togs bort och skåpsmodulerna byggdes om.",
      }],
      topologyRebuilt: true,
      notice: "De två underskåpsmodulerna slogs ihop och stomdelarna genererades om.",
    };
  }

  const partOverrides = { ...spec.part_overrides };
  delete partOverrides[partId];
  return {
    spec: {
      ...spec,
      part_overrides: partOverrides,
      removed_part_ids: [...new Set([...spec.removed_part_ids, partId])],
    },
    diff: [],
    topologyRebuilt: false,
    notice: "Delen togs bort. Konstruktionens integritet måste kontrolleras innan fler delar tas bort.",
  };
}

/** Repairs persisted removals created before grid-aware deletion was introduced. */
export function migrateLegacyStructuralRemovals(spec: DesignSpec): { spec: DesignSpec; diff: ChangeDiff[] } {
  const legacyIds = [...spec.removed_part_ids];
  const dividerIds = legacyIds
    .filter((partId) => /^divider-\d+$/.test(partId))
    .sort((left, right) => Number(right.split("-").at(-1)) - Number(left.split("-").at(-1)));
  const shelfIds = [...new Set(legacyIds.filter((partId) => /^shelf-\d+-bay-\d+$/.test(partId)).map((partId) => `shelf-${partId.split("-")[1]}-bay-1`))]
    .sort((left, right) => Number(right.split("-")[1]) - Number(left.split("-")[1]));
  let current = spec;
  const diff: ChangeDiff[] = [];
  for (const partId of [...dividerIds, ...shelfIds]) {
    const removal = removePartFromDesign(current, partId);
    current = removal.spec;
    diff.push(...removal.diff);
  }
  return { spec: current, diff };
}

export function restorePartCustomizations(spec: DesignSpec): DesignSpec {
  const baseline = spec.topology_baseline;
  return {
    ...spec,
    ...(baseline ?? {}),
    ...(baseline ? {
      bay_width_ratios: [...baseline.bay_width_ratios],
      shelf_height_ratios: [...baseline.shelf_height_ratios],
    } : {}),
    part_overrides: {},
    removed_part_ids: [],
    topology_baseline: undefined,
  };
}

export interface PartVerticalMoveResult {
  spec: DesignSpec;
  topologyRebuilt: boolean;
  notice: string;
}

function minimumFurnitureHeightMm(spec: DesignSpec): number {
  const plinthHeight = spec.plinth ? spec.plinth_height_mm : 0;
  if (spec.furniture_type !== "wall_library" || spec.base_cabinet_count < 1) {
    return Math.max(
      DESIGN_CONSTRAINTS.heightMm.minimum,
      Math.floor(plinthHeight + 2 * spec.measured_thickness_mm) + 1,
    );
  }
  return Math.max(
    DESIGN_CONSTRAINTS.heightMm.minimum,
    Math.floor(
      plinthHeight
      + spec.base_cabinet_height_mm
      + spec.measured_thickness_mm
      + DESIGN_CONSTRAINTS.baseCabinetUpperClearanceMm,
    ) + 1,
  );
}

/** Moves a complete shelf row as one parametric unit; other boards use a local Z override. */
export function movePartVertically(spec: DesignSpec, partId: string, targetZMm: number): PartVerticalMoveResult {
  const shelfMatch = /^shelf-(\d+)-bay-\d+$/.exec(partId);
  if (shelfMatch && spec.shelf_count > 0) {
    const shelfCount = Math.max(0, Math.trunc(spec.shelf_count));
    if (!customShelfRatiosAreFeasible(shelfCount)) {
      return {
        spec,
        topologyRebuilt: false,
        notice: "Den jämna hyllindelningen har för många nivåer för individuella femprocentspositioner och lämnades oförändrad.",
      };
    }
    const shelfIndex = Math.min(spec.shelf_count - 1, Math.max(0, Number(shelfMatch[1]) - 1));
    const thickness = Math.max(spec.measured_thickness_mm, 0.1);
    const plinthHeight = spec.plinth ? spec.plinth_height_mm : 0;
    const shelfZoneBottom = spec.furniture_type === "wall_library" && spec.base_cabinet_count > 0
      ? plinthHeight + spec.base_cabinet_height_mm
      : plinthHeight + thickness;
    const innerHeight = Math.max(spec.height_mm - shelfZoneBottom - thickness, 1);
    const ratios = shelfHeightRatios(spec);
    const requestedRatio = (targetZMm - shelfZoneBottom) / innerHeight;
    const lower = shelfIndex === 0 ? 0.05 : ratios[shelfIndex - 1]! + 0.05;
    const upper = shelfIndex === spec.shelf_count - 1 ? 0.95 : ratios[shelfIndex + 1]! - 0.05;
    const nextRatios = setShelfHeightRatio(
      { ...spec, shelf_height_ratios: ratios },
      shelfIndex,
      Math.min(upper, Math.max(lower, requestedRatio)),
    ).shelf_height_ratios;
    const cleaned = withoutPartFamilies(spec, [`shelf-${shelfIndex + 1}-`]);
    return {
      spec: {
        ...spec,
        ...cleaned,
        shelf_height_ratios: nextRatios,
      },
      topologyRebuilt: true,
      notice: `Hyllrad ${shelfIndex + 1} flyttades som ett sammanhängande plan i alla fack.`,
    };
  }

  if (partId === "top") {
    const part = generateParts(spec).find((candidate) => candidate.part_id === partId);
    const thickness = part?.thickness_mm ?? spec.measured_thickness_mm;
    const heightMm = Math.min(
      DESIGN_CONSTRAINTS.heightMm.maximum,
      Math.max(minimumFurnitureHeightMm(spec), Math.round((targetZMm + thickness / 2) / 5) * 5),
    );
    return {
      spec: { ...spec, height_mm: heightMm },
      topologyRebuilt: true,
      notice: "Toppskivan flyttades och hela möbelns höjd samt anslutande delar räknades om.",
    };
  }

  if (partId === "bottom" && spec.furniture_type === "wall_library" && spec.base_cabinet_count > 0) {
    const thickness = Math.max(spec.measured_thickness_mm, 0.1);
    const plinthHeight = spec.plinth ? spec.plinth_height_mm : 0;
    const baseHeight = Math.min(
      maximumBaseCabinetHeightMm(spec.height_mm, thickness),
      Math.max(
        DESIGN_CONSTRAINTS.wallLibraryBaseHeightMinimumMm,
        Math.round((targetZMm + thickness / 2 - plinthHeight) / 5) * 5,
      ),
    );
    return {
      spec: { ...spec, base_cabinet_height_mm: baseHeight },
      topologyRebuilt: true,
      notice: "Gränsen mot underskåpet flyttades och hela överbyggnaden räknades om.",
    };
  }

  return {
    spec,
    topologyRebuilt: false,
    notice: "Den här delen är låst till anslutande förband. Flytta en hyllrad, toppskivan eller underskåpsgränsen i stället.",
  };
}

export interface ParametricPartEditResult {
  spec: DesignSpec;
  supported: boolean;
  notice: string;
}

function editResult(spec: DesignSpec, supported: boolean, notice: string): ParametricPartEditResult {
  return { spec, supported, notice };
}

/**
 * Applies inspector edits as product-level operations. Connected boards are
 * never detached from their joints: the carcass, complete shelf row or bay
 * layout is regenerated from the resulting DesignSpec.
 */
export function editPartParametrically(
  spec: DesignSpec,
  partId: string,
  patch: PartOverride,
): ParametricPartEditResult {
  if (Number.isFinite(patch.position_z_mm)) {
    const moved = movePartVertically(spec, partId, patch.position_z_mm!);
    return editResult(moved.spec, localDesignHash(moved.spec) !== localDesignHash(spec), moved.notice);
  }

  const divider = /^divider-(\d+)$/.exec(partId);
  if (divider && Number.isFinite(patch.position_x_mm)) {
    const dividerIndex = Math.min(spec.divider_count - 1, Math.max(0, Number(divider[1]) - 1));
    const thickness = Math.max(spec.measured_thickness_mm, 0.1);
    const innerWidth = Math.max(spec.width_mm - 2 * thickness - spec.divider_count * thickness, 1);
    const widths = bayWidths(spec, innerWidth);
    const preceding = widths.slice(0, dividerIndex).reduce((sum, value) => sum + value, 0);
    const desiredBayWidth = patch.position_x_mm!
      - thickness
      - preceding
      - thickness * (dividerIndex + 0.5);
    const next = setBayWidthRatio(spec, dividerIndex, desiredBayWidth / innerWidth);
    const changed = next !== spec && localDesignHash(next) !== localDesignHash(spec);
    return editResult(
      changed ? next : spec,
      changed,
      changed
        ? spec.symmetry_locked
          ? `Avdelare ${dividerIndex + 1} och dess speglade motsvarighet flyttades. Alla hyllsegment räknades om.`
          : `Avdelare ${dividerIndex + 1} flyttades och angränsande hyllsegment räknades om.`
        : "Den jämna fackindelningen har för många fack för individuella åttaprocentsbredder och lämnades oförändrad.",
    );
  }

  if (Number.isFinite(patch.thickness_mm) && !partId.startsWith("back-panel")) {
    const measured = Math.min(19, Math.max(17, round(patch.thickness_mm!, 1)));
    return editResult(
      { ...spec, measured_thickness_mm: measured },
      true,
      `Materialtjockleken ändrades till ${measured} mm för hela konstruktionen och samtliga förband räknades om.`,
    );
  }

  const shelf = /^shelf-(\d+)-bay-(\d+)$/.exec(partId);
  if (shelf && Number.isFinite(patch.width_mm)) {
    const bayIndex = Math.min(spec.divider_count, Math.max(0, Number(shelf[2]) - 1));
    const thickness = Math.max(spec.measured_thickness_mm, 0.1);
    const innerWidth = Math.max(spec.width_mm - 2 * thickness - spec.divider_count * thickness, 1);
    const next = setBayWidthRatio(spec, bayIndex, patch.width_mm! / innerWidth);
    const changed = next !== spec && localDesignHash(next) !== localDesignHash(spec);
    return editResult(
      changed ? next : spec,
      changed,
      changed
        ? spec.symmetry_locked
          ? "Fackbredden och dess speglade motsvarighet ändrades; hela hyllsystemet byggdes om."
          : "Fackbredden ändrades; avdelare och alla hyllrader byggdes om."
        : "Den jämna fackindelningen har för många fack för individuella åttaprocentsbredder och lämnades oförändrad.",
    );
  }

  if ((partId === "top" || partId === "bottom" || partId === "plinth-front") && Number.isFinite(patch.width_mm)) {
    const width = Math.min(
      DESIGN_CONSTRAINTS.widthMm.maximum,
      Math.max(DESIGN_CONSTRAINTS.widthMm.minimum, round(patch.width_mm! + 2 * spec.measured_thickness_mm, 1)),
    );
    return editResult({ ...spec, width_mm: width }, true, "Möbelns bredd och alla anslutande delar räknades om.");
  }

  if ((partId === "side-left" || partId === "side-right") && Number.isFinite(patch.width_mm)) {
    const height = Math.min(
      DESIGN_CONSTRAINTS.heightMm.maximum,
      Math.max(minimumFurnitureHeightMm(spec), round(patch.width_mm!, 1)),
    );
    return editResult({ ...spec, height_mm: height }, true, "Möbelns höjd och alla anslutande delar räknades om.");
  }

  if ((partId === "side-left" || partId === "side-right" || partId === "top" || partId === "bottom") && Number.isFinite(patch.depth_mm)) {
    const depth = Math.min(
      DESIGN_CONSTRAINTS.depthMm.maximum,
      Math.max(DESIGN_CONSTRAINTS.depthMm.minimum, round(patch.depth_mm!, 1)),
    );
    return editResult(
      {
        ...spec,
        depth_mm: depth,
        ...(spec.furniture_type === "wall_library" && spec.base_cabinet_count > 0
          ? { base_cabinet_depth_mm: depth }
          : {}),
      },
      true,
      "Möbelns djup och alla anslutande delar räknades om.",
    );
  }

  const baseSide = /^base-side-\d+$/.test(partId);
  if (baseSide && Number.isFinite(patch.width_mm)) {
    const thickness = Math.max(spec.measured_thickness_mm, 0.1);
    const height = Math.min(
      maximumBaseCabinetHeightMm(spec.height_mm, spec.measured_thickness_mm),
      Math.max(
        DESIGN_CONSTRAINTS.wallLibraryBaseHeightMinimumMm,
        round(patch.width_mm! + thickness - dadoDepthMm(thickness), 1),
      ),
    );
    return editResult({ ...spec, base_cabinet_height_mm: height }, true, "Alla underskåpsdelar räknades om till den nya höjden.");
  }
  if (baseSide && Number.isFinite(patch.depth_mm)) {
    const depth = Math.min(
      DESIGN_CONSTRAINTS.depthMm.maximum,
      Math.max(DESIGN_CONSTRAINTS.depthMm.minimum, round(patch.depth_mm!, 1)),
    );
    return editResult(
      { ...spec, depth_mm: depth, base_cabinet_depth_mm: depth },
      true,
      "Underskåpets djup följer möbelns yttermått; alla anslutande delar räknades om.",
    );
  }

  return editResult(
    spec,
    false,
    "Måttet är låst av möbelns förband. Ändra yttermått, fackbredd eller hyllnivå så räknas konstruktionen om korrekt.",
  );
}

function calculateShelfDeflection(spec: DesignSpec): {
  deflectionMm: number;
  allowableMm: number;
  stressMpa: number;
  spanMm: number;
} {
  const material = MATERIALS.find((candidate) => candidate.id === spec.material_id) ?? MATERIALS[0]!;
  const thickness = Math.max(spec.measured_thickness_mm, 0.1);
  const innerWidth = Math.max(spec.width_mm - 2 * thickness - Math.max(0, spec.divider_count) * thickness, 1);
  const span = Math.max(...bayWidths(spec, innerWidth), 1);
  const depth = interiorPanelDepth(spec.depth_mm, spec.back_panel, spec.back_panel_type);
  const loadN = Math.max(spec.load_per_shelf_kg, 0) * GRAVITY;
  const lineLoad = loadN / span;
  const inertia = (depth * thickness ** 3) / 12;
  const creepFactor = 1.6;
  const deflection = (5 * lineLoad * span ** 4 * creepFactor) / (384 * material.elasticModulusMpa * inertia);
  const bendingMoment = (lineLoad * span ** 2) / 8;
  const stress = (bendingMoment * (thickness / 2)) / inertia;
  return {
    deflectionMm: round(deflection),
    allowableMm: round(Math.min(span / 200, 5)),
    stressMpa: round(stress),
    spanMm: round(span),
  };
}

export function suggestedDividerCount(spec: DesignSpec): number {
  const current = Math.max(0, Math.trunc(spec.divider_count));
  if (
    spec.shelf_count === 0
    || !Number.isFinite(spec.load_per_shelf_kg)
    || spec.load_per_shelf_kg <= 0
  ) return current;
  for (let candidate = current; candidate <= DESIGN_CONSTRAINTS.dividerCount.maximum; candidate += 1) {
    const result = calculateShelfDeflection({ ...spec, divider_count: candidate });
    if (result.deflectionMm <= result.allowableMm) return candidate;
  }
  return Math.max(current, DESIGN_CONSTRAINTS.dividerCount.maximum);
}

/**
 * Resolve the minimum divider layout required by the deterministic shelf
 * screening calculation. Auto mode owns the divider count so dimensional,
 * material and load changes can both add and remove no-longer-required support.
 * Manual mode deliberately leaves the requested layout untouched for what-if
 * analysis; blocking rules still prevent that layout from reaching production.
 */
export function adaptStructuralSupports(spec: DesignSpec): { spec: DesignSpec; diff: ChangeDiff[] } {
  const targetWidthMode = spec.bay_sizing_mode === "target_width";
  if (spec.reinforcement_mode !== "auto" && !targetWidthMode) return { spec, diff: [] };
  const requestedCount = targetWidthMode ? targetBayLayout(spec).dividerCount : 0;
  const structuralCount = spec.reinforcement_mode === "auto"
    ? suggestedDividerCount({ ...spec, divider_count: 0, bay_width_ratios: [] })
    : 0;
  const requiredCount = spec.reinforcement_mode === "auto"
    ? Math.max(structuralCount, requestedCount)
    : requestedCount;
  const targetBaseCabinetCount = spec.furniture_type === "wall_library" && spec.base_cabinet_count > 0
    ? requiredCount + 1
    : spec.base_cabinet_count;
  const dividerChanged = requiredCount !== spec.divider_count;
  const baseChanged = targetBaseCabinetCount !== spec.base_cabinet_count;
  const ratiosChanged = targetWidthMode && spec.bay_width_ratios.length > 0;
  if (!dividerChanged && !baseChanged && !ratiosChanged) return { spec, diff: [] };
  const diff: ChangeDiff[] = [];
  if (dividerChanged) {
    diff.push({
      field: "divider_count",
      before: spec.divider_count,
      after: requiredCount,
      reason:
        requiredCount > spec.divider_count
          ? "Automatiskt konstruktionsstöd lades till för att hålla hyllnedböjningen inom tillåten gräns."
          : "Automatiskt konstruktionsstöd räknades om efter ändrade mått, material eller last.",
    });
  }
  if (baseChanged) {
    diff.push({
      field: "base_cabinet_count",
      before: spec.base_cabinet_count,
      after: targetBaseCabinetCount,
      reason: "Underskåpen riktades automatiskt mot överdelens bärande centrumlinjer.",
    });
  }
  return {
    spec: {
      ...spec,
      ...(dividerChanged
        ? withoutPartFamilies(spec, ["divider-", "shelf-", "back-panel"])
        : {}),
      divider_count: requiredCount,
      base_cabinet_count: targetBaseCabinetCount,
      reinforcement_mode: spec.reinforcement_mode,
      bay_width_ratios: targetWidthMode || dividerChanged ? [] : spec.bay_width_ratios,
    },
    diff,
  };
}

function outsideConstraintRange(
  value: number,
  range: { readonly minimum: number; readonly maximum: number },
): boolean {
  return !Number.isFinite(value) || value < range.minimum || value > range.maximum;
}

function invalidGeometryRule(spec: DesignSpec): RuleEvaluation {
  const failures: string[] = [];
  const thicknessMm = spec.measured_thickness_mm;
  if (outsideConstraintRange(spec.width_mm, DESIGN_CONSTRAINTS.widthMm)) failures.push("bredden måste vara mellan 250 och 6000 mm");
  if (outsideConstraintRange(spec.height_mm, DESIGN_CONSTRAINTS.heightMm)) failures.push("höjden måste vara mellan 300 och 4000 mm");
  if (outsideConstraintRange(spec.depth_mm, DESIGN_CONSTRAINTS.depthMm)) failures.push("djupet måste vara mellan 100 och 1200 mm");
  if (spec.width_mm <= 2 * spec.measured_thickness_mm) failures.push("bredden måste överstiga två materialtjocklekar");
  if (!Number.isFinite(spec.plinth_height_mm) || spec.plinth_height_mm < 0 || spec.plinth_height_mm > 500) failures.push("sockelhöjden måste vara mellan 0 och 500 mm");
  if (spec.plinth !== (spec.plinth_height_mm > 0)) failures.push("sockelval och sockelhöjd måste stämma överens");
  if (spec.height_mm <= 2 * spec.measured_thickness_mm + (spec.plinth ? spec.plinth_height_mm : 0)) failures.push("höjden lämnar inget användbart innerutrymme");
  if (spec.depth_mm <= spec.measured_thickness_mm) failures.push("djupet måste överstiga materialtjockleken");
  if (!Number.isFinite(spec.measured_thickness_mm) || spec.measured_thickness_mm <= 0) failures.push("uppmätt tjocklek måste vara ett ändligt värde större än noll");
  if (!Number.isInteger(spec.shelf_count) || outsideConstraintRange(spec.shelf_count, DESIGN_CONSTRAINTS.shelfCount)) failures.push("antal hyllor måste vara ett heltal mellan 0 och 40");
  if (!Number.isInteger(spec.divider_count) || outsideConstraintRange(spec.divider_count, DESIGN_CONSTRAINTS.dividerCount)) failures.push("antal avdelare måste vara ett heltal mellan 0 och 16");
  if (outsideConstraintRange(spec.load_per_shelf_kg, DESIGN_CONSTRAINTS.shelfLoadKg)) failures.push("total last per hyllrad måste vara mellan 0 och 500 kg");
  if (!Number.isInteger(spec.base_cabinet_count) || outsideConstraintRange(spec.base_cabinet_count, DESIGN_CONSTRAINTS.baseCabinetModuleCount)) failures.push("antal underskåp måste vara ett heltal mellan 0 och 17");
  if (outsideConstraintRange(spec.base_cabinet_height_mm, DESIGN_CONSTRAINTS.baseCabinetHeightMm)) failures.push("underskåpshöjden måste vara mellan 0 och 2000 mm");
  if (outsideConstraintRange(spec.base_cabinet_depth_mm, DESIGN_CONSTRAINTS.baseCabinetDepthMm)) failures.push("underskåpsdjupet måste vara mellan 0 och 1200 mm");
  if (spec.base_cabinet_count === 0 && (spec.base_cabinet_height_mm !== 0 || spec.base_cabinet_depth_mm !== 0)) failures.push("underskåpsmåtten måste vara 0 när underskåp saknas");
  if (spec.base_cabinet_count > 0 && spec.base_cabinet_depth_mm !== spec.depth_mm) failures.push("underskåpets djup måste följa möbelns yttermått");
  const bayCount = spec.divider_count + 1;
  const innerWidthMm = spec.width_mm - (spec.divider_count + 2) * thicknessMm;
  if (innerWidthMm <= 0) failures.push("avdelarna förbrukar hela den fria innerbredden");
  let validBayRatios = true;
  if (spec.bay_width_ratios.length > 0) {
    const ratioTotal = spec.bay_width_ratios.reduce((sum, value) => sum + value, 0);
    validBayRatios = spec.bay_width_ratios.length === bayCount
      && Number.isFinite(ratioTotal)
      && ratioTotal > 0
      && spec.bay_width_ratios.every((value) => (
        Number.isFinite(value)
        && value > 0
        && value * 100 >= ratioTotal * 8
      ));
    if (spec.bay_width_ratios.length !== bayCount) failures.push("antal anpassade fackbredder måste motsvara antalet fack");
    if (!validBayRatios) failures.push("varje anpassat fack måste vara minst 8 % av innerbredden");
  }
  if (spec.shelf_height_ratios.length > 0) {
    if (spec.shelf_height_ratios.length !== spec.shelf_count) failures.push("antal anpassade hyllnivåer måste motsvara antalet hyllor");
    if (spec.shelf_height_ratios.some((value, index, values) => !Number.isFinite(value) || value < 0.05 - RATIO_COMPARISON_TOLERANCE || value > 0.95 + RATIO_COMPARISON_TOLERANCE || (index > 0 && value - values[index - 1]! < 0.05 - RATIO_COMPARISON_TOLERANCE))) failures.push("anpassade hyllnivåer måste vara ordnade och ha minst 5 % avstånd");
  }
  if (innerWidthMm > 0 && Number.isInteger(bayCount) && bayCount >= 1 && bayCount <= DESIGN_CONSTRAINTS.bayCount.maximum && validBayRatios) {
    const minimumClearShelfWidthMm = DESIGN_CONSTRAINTS.minimumShelfWidthMm + 2 * SHELF_SIDE_CLEARANCE_MM;
    if (bayWidths(spec, innerWidthMm).some((widthMm) => widthMm < minimumClearShelfWidthMm)) {
      failures.push("fackindelningen lämnar inte tillräcklig fri bredd för hyllplanen");
    }
  }
  if (rawShelfOpeningHeights(spec).some((heightMm) => heightMm < DESIGN_CONSTRAINTS.minimumShelfOpeningMm)) {
    failures.push("hyllindelningen lämnar en öppning som är lägre än 40 mm");
  }
  if (
    Number.isInteger(spec.base_cabinet_count)
    && spec.base_cabinet_count > 0
    && spec.base_cabinet_count <= DESIGN_CONSTRAINTS.baseCabinetModuleCount.maximum
  ) {
    const baseOpeningWidths = spec.base_cabinet_count === bayCount
      && innerWidthMm > 0
      && validBayRatios
      ? bayWidths(spec, innerWidthMm)
      : Array.from(
        { length: spec.base_cabinet_count },
        () => (
          spec.width_mm - (spec.base_cabinet_count + 1) * thicknessMm
        ) / spec.base_cabinet_count,
      );
    if (baseOpeningWidths.some((widthMm) => widthMm < DESIGN_CONSTRAINTS.minimumBaseCabinetOpeningMm)) {
      failures.push("underskåpsindelningen lämnar en öppning som är smalare än 200 mm");
    }
  }
  if (spec.furniture_type === "bookcase") {
    if (spec.base_cabinet_count !== 0 || spec.base_cabinet_height_mm !== 0 || spec.base_cabinet_depth_mm !== 0) {
      failures.push("bokhyllor får inte innehålla underskåpsgeometri");
    }
  }
  if (spec.furniture_type === "wall_library") {
    if (spec.base_cabinet_count < 1) failures.push("väggbibliotek måste ha minst ett underskåp");
    if (spec.base_cabinet_height_mm < DESIGN_CONSTRAINTS.wallLibraryBaseHeightMinimumMm) failures.push("underskåpet måste vara minst 300 mm högt");
    if (
      spec.base_cabinet_height_mm
      >= spec.height_mm - spec.measured_thickness_mm - DESIGN_CONSTRAINTS.baseCabinetUpperClearanceMm
    ) failures.push("underskåpet lämnar inte föreskriven fri höjd för överbyggnaden");
  }
  return {
    rule_id: "GEO-001",
    rule_version: "1.0.0",
    status: failures.length > 0 ? "BLOCK" : "PASS",
    title: "Giltig parametrisk geometri",
    summary: failures.length > 0 ? failures.join("; ") : "Alla grundmått ger ett giltigt innerutrymme.",
    calculation: "Envelope + B > 2t, H > 2t + sockel, D > t och baseH < H - t - 200",
    assumptions: [],
    affected_part_ids: [],
  };
}

function cabinetHardwareRule(spec: DesignSpec, parts: ResolvedPart[]): RuleEvaluation | null {
  if (spec.furniture_type !== "wall_library" || spec.base_cabinet_count < 1) return null;
  const affected = parts
    .filter((part) => part.kind === "base_side" || part.kind === "base_bottom" || part.kind === "cabinet_front")
    .map((part) => part.part_id);
  return {
    rule_id: "CB-HARDWARE-001",
    rule_version: "1.1.0",
    status: "WARNING",
    title: "Beslag och borrbild för underskåp",
    summary: "Stom- och frontdelar är genererade, men fronterna är inte monteringsbara förrän beslagssystem, frontspel och borrbild har verifierats.",
    calculation: "Verifierat gångjärn + montageplatta + borrbild = saknas",
    calculated_value: 1,
    allowed_value: 0,
    unit: "obekräftade system",
    margin_percent: -100,
    assumptions: [
      "Frontlayouten är geometrisk tills ett versionslåst beslagssystem har valts.",
      "Ingen borrbild för gångjärn skickas till CAM i detta läge.",
    ],
    affected_part_ids: affected,
    suggestion: {
      action: "manual_review",
      label: "Specificera underskåpsbeslag",
      value: false,
      explanation: "Välj ett versionslåst gångjärn och montageplatta, ange öppningsvinkel och frontspel och verifiera därefter beslagets borrbild mot den uppmätta materialtjockleken.",
    },
  };
}

function baseSupportAlignmentRule(spec: DesignSpec, parts: ResolvedPart[]): RuleEvaluation | null {
  if (spec.furniture_type !== "wall_library" || spec.base_cabinet_count < 1) return null;
  const dividers = parts
    .filter((part) => part.kind === "divider")
    .sort((left, right) => left.position_mm.x - right.position_mm.x);
  const baseSides = parts
    .filter((part) => part.kind === "base_side")
    .sort((left, right) => left.position_mm.x - right.position_mm.x);
  // BASE_SIDE now contains internal module boundaries only. The full-height
  // carcass sides carry the two outer base-bottom joints.
  const internalBaseSides = baseSides;
  const toleranceMm = Math.max(1, spec.measured_thickness_mm * 0.25);
  const unsupported = dividers.filter(
    (divider) => !internalBaseSides.some(
      (support) => Math.abs(support.position_mm.x - divider.position_mm.x) <= toleranceMm,
    ),
  );
  const positions = unsupported.map((part) => Math.round(part.position_mm.x));
  const targetModules = spec.divider_count + 1;
  const positionText = positions.map((position) => `${position.toLocaleString("sv-SE")} mm`).join(", ");
  const aligned = unsupported.length === 0;
  return {
    rule_id: "BASE-SUPPORT-001",
    rule_version: "1.0.0",
    status: aligned ? "PASS" : "BLOCK",
    title: "Lodrät lastväg genom underskåpen",
    summary: aligned
      ? "Varje övre avdelare står över en underskåpssida, så lasten förs lodrätt ner till sockeln."
      : `${unsupported.length} ${unsupported.length === 1 ? "övre avdelare saknar" : "övre avdelare saknar"} direkt understöd vid ${positionText} från vänster ytterkant. Mellanbottnen får annars en overifierad punktlast.`,
    calculation: `Övre avdelare utan underskåpssida inom ±${round(toleranceMm, 1)} mm: ${unsupported.length}`,
    calculated_value: unsupported.length,
    allowed_value: 0,
    unit: "ostödda avdelare",
    margin_percent: aligned ? 100 : -100,
    assumptions: [
      "En bärande övre avdelare ska ha en genomgående underskåpssida på samma centrumlinje.",
      "Mellanbottnen är inte dimensionerad som en fritt spännande balk för koncentrerade laster i denna screening.",
    ],
    affected_part_ids: unsupported.length > 0
      ? ["bottom", ...unsupported.map((part) => part.part_id)]
      : dividers.map((part) => part.part_id),
    diagnostics: [
      { label: "Övre avdelare", value: String(dividers.length), unit: "st" },
      { label: "Inre underskåpssidor", value: String(internalBaseSides.length), unit: "st" },
      { label: "Ostödda centrumlinjer", value: positionText || "Inga" },
    ],
    ...(aligned
      ? {}
      : {
          suggestion: {
            action: "align_base_cabinets" as const,
            label: `Rikta in ${targetModules} underskåpsmoduler`,
            value: targetModules,
            explanation: `Skapa ${targetModules} lika breda underskåpsmoduler och återställ lika breda övre fack. Då hamnar en fullhöjd underskåpssida direkt under varje övre avdelare (${positionText}).`,
          },
        }),
  };
}

function shelfRule(spec: DesignSpec, parts: ResolvedPart[]): RuleEvaluation {
  if (spec.shelf_count === 0) {
    return {
      rule_id: "STR-DEF-001",
      rule_version: "1.1.0",
      status: "PASS",
      title: "Hyllnedböjning",
      summary: "Ingen hylla att kontrollera.",
      calculation: "Ej tillämplig",
      assumptions: [],
      affected_part_ids: [],
    };
  }
  const result = calculateShelfDeflection(spec);
  const ratio = result.deflectionMm / Math.max(result.allowableMm, 0.001);
  const status: ValidationStatus = ratio > 1.5 ? "BLOCK" : ratio > 1 ? "WARNING" : "PASS";
  const proposedCount = suggestedDividerCount(spec);
  const proposedInnerWidth = Math.max(
    spec.width_mm - (proposedCount + 2) * spec.measured_thickness_mm,
    1,
  );
  const proposedBayWidth = proposedInnerWidth / (proposedCount + 1);
  const proposedSupportPositions = Array.from({ length: proposedCount }, (_, index) => Math.round(
    spec.measured_thickness_mm
      + (index + 1) * proposedBayWidth
      + spec.measured_thickness_mm * (index + 0.5),
  ));
  const proposedPositionText = proposedSupportPositions
    .map((position) => `${position.toLocaleString("sv-SE")} mm`)
    .join(", ");
  const material = MATERIALS.find((candidate) => candidate.id === spec.material_id) ?? MATERIALS[0]!;
  const affected = parts.filter((part) => part.kind === "shelf").map((part) => part.part_id);
  return {
    rule_id: "STR-DEF-001",
    rule_version: "1.1.0",
    status,
    title: "Hyllnedböjning",
    summary:
      status === "PASS"
        ? `Beräknad långtidsnedböjning ${result.deflectionMm} mm ligger inom gränsen.`
        : `Beräknad långtidsnedböjning ${result.deflectionMm} mm överskrider gränsen ${result.allowableMm} mm.`,
    calculation: "δ = 5wL⁴ / (384EI) × 1,6",
    calculated_value: result.deflectionMm,
    allowed_value: result.allowableMm,
    unit: "mm",
    margin_percent: round(((result.allowableMm - result.deflectionMm) / Math.max(result.allowableMm, 0.001)) * 100, 1),
    assumptions: [
      `Total jämnt fördelad radlast ${spec.load_per_shelf_kg} kg`,
      `Fri spännvidd ${result.spanMm} mm`,
      `E-modul ${material.elasticModulusMpa} MPa (${material.version})`,
      "Långtidsfaktor 1,6; screening, inte certifiering",
    ],
    affected_part_ids: affected,
    ...(status === "PASS" || proposedCount <= spec.divider_count
      ? {}
      : {
          suggestion: {
            action: "set_divider_count" as const,
            label: `Inför ${proposedCount} vertikala avdelare totalt`,
            value: proposedCount,
            explanation: `Montera ${proposedCount} fullhöga, bärande avdelarskivor på centrumlinjerna ${proposedPositionText} från vänster ytterkant. Det ger ${proposedCount + 1} lika fack med cirka ${Math.round(proposedBayWidth)} mm fri spännvidd; hyllplan, förband och underskåpsstöd räknas därefter om.`,
          },
        }),
  };
}

function dryJoiningRule(spec: DesignSpec, parts: ResolvedPart[]): RuleEvaluation {
  const adjustable = !spec.fixed_shelves;
  const affected = parts
    .filter((part) => part.kind !== "cabinet_front" && part.kind !== "plinth")
    .map((part) => part.part_id);
  return {
    rule_id: "CB-JOINT-001",
    rule_version: "1.3.0",
    status: adjustable ? "BLOCK" : "WARNING",
    title: "Lokalt upplag i hyllspår och hyllbärare",
    summary: adjustable
      ? "Justerbara hyllor saknar versionsbunden hyllbärarkapacitet, materialkompatibilitet och borrbild. Konstruktionen kan inte granskas färdigt förrän ett mekaniskt hyllbärarsystem har verifierats."
      : "Ett vanligt DADO-spår visar geometri och lokalt upplag men verifierar inte permanent hållning. Ett versionslåst självlåsande torrförband eller en demonterbar mekanisk säkring måste granskas.",
    calculation: adjustable
      ? "Tillåten last för versionsbundet hyllbärarsystem = saknas"
      : "DADO = geometriskt upplag; permanent hållning = ej verifierad",
    assumptions: [
      ...(adjustable
        ? ["Ingen hyllbärar-SKU, katalogversion, kapacitet eller borrbild är verifierad."]
        : ["Ett vanligt DADO-spår är inte självlåsande."]),
      "Lim, fogmassa, epoxy och annan adhesiv låsning är förbjuden.",
      "Den lokala kontrollen är endast designgranskning och ger ingen fysisk frisläppning.",
    ],
    affected_part_ids: affected,
    diagnostics: [
      { label: "hylltyp", value: spec.fixed_shelves ? "fixed" : "adjustable" },
      { label: "permanent hållning verifierad", value: "false" },
    ],
  };
}

function stabilityRule(spec: DesignSpec): RuleEvaluation {
  const slenderness = spec.height_mm / Math.max(spec.depth_mm, 1);
  const needsAnchor = slenderness > 4 || spec.height_mm >= 1_800;
  const status: ValidationStatus = needsAnchor && !spec.wall_anchor_verified ? "WARNING" : "PASS";
  return {
    rule_id: "STAB-TIP-002",
    rule_version: "1.0.0",
    status,
    title: "Tipprisk och väggförankring",
    summary:
      status === "PASS"
        ? "Geometrin kräver ingen ytterligare varning i denna screening."
        : "Proportionerna kräver att väggunderlag och ett godkänt förankringssystem verifieras före release.",
    calculation: `H/D = ${round(slenderness, 2)}; varningsgräns 4,0`,
    calculated_value: round(slenderness, 2),
    allowed_value: 4,
    unit: "H/D",
    margin_percent: round(((4 - slenderness) / 4) * 100, 1),
    assumptions: ["Ingen dörr- eller lådlast i MVP", "Förankring får inte väljas utan känt väggunderlag och evidens"],
    affected_part_ids: ["side-left", "side-right"],
  };
}

function backRule(spec: DesignSpec, parts: ResolvedPart[]): RuleEvaluation {
  const backPartIds = parts.filter((part) => part.kind === "back").map((part) => part.part_id);
  const hasBack = spec.back_panel && backPartIds.length > 0;
  return {
    rule_id: "STAB-RACK-001",
    rule_version: "1.0.0",
    status: hasBack ? "PASS" : "WARNING",
    title: "Sidostabilitet",
    summary: hasBack
      ? "Bakstycke finns och bidrar till att motverka skevning."
      : "Bakstycke saknas. En separat, verifierad stagning behöver dimensioneras.",
    calculation: hasBack ? "Bakstycke: aktivt" : "Bakstycke: saknas",
    assumptions: ["Bakstyckets infästning behöver verifieras fysiskt"],
    affected_part_ids: hasBack ? backPartIds : ["side-left", "side-right"],
    ...(hasBack
      ? {}
      : {
          suggestion: {
            action: "enable_back" as const,
            label: "Lägg till bakstycke",
            value: true,
            explanation: "Tillför ett bakstycke och genererar matchande noter.",
          },
        }),
  };
}

function machineRule(spec: DesignSpec, parts: ResolvedPart[]): RuleEvaluation {
  const machine = MACHINES.find((candidate) => candidate.id === spec.machine_profile_id);
  if (!machine) {
    return {
      rule_id: "DFM-MACHINE-001",
      rule_version: "1.0.0",
      status: "BLOCK",
      title: "Maskinens arbetsområde",
      summary: "Vald maskinprofil saknas eller är inte versionslåst.",
      calculation: "Profil måste finnas i maskinbiblioteket",
      assumptions: [],
      affected_part_ids: parts.map((part) => part.part_id),
    };
  }
  const outside = parts.filter((part) => {
    const long = Math.max(part.width_mm, part.depth_mm);
    const short = Math.min(part.width_mm, part.depth_mm);
    const fitsNormal = long <= machine.workAreaMm.x && short <= machine.workAreaMm.y;
    const fitsRotated = short <= machine.workAreaMm.x && long <= machine.workAreaMm.y;
    return !fitsNormal && !fitsRotated;
  });
  return {
    rule_id: "DFM-MACHINE-001",
    rule_version: "1.0.0",
    status: outside.length > 0 ? "BLOCK" : "PASS",
    title: "Maskinens arbetsområde",
    summary:
      outside.length > 0
        ? `${outside.length} ${outside.length === 1 ? "del ryms" : "delar ryms"} inte inom ${machine.workAreaMm.x} × ${machine.workAreaMm.y} mm.`
        : `Alla delar ryms i ${machine.name}.`,
    calculation: `Delrektangel ≤ ${machine.workAreaMm.x} × ${machine.workAreaMm.y} mm, 90° rotation tillåten`,
    assumptions: ["Fixtur- och vakuumzoner verifieras i produktionssteget"],
    affected_part_ids: outside.map((part) => part.part_id),
    ...(outside.length > 0
      ? {
          suggestion: {
            action: "create_stockless_review_package" as const,
            label: "Skapa lagerobundet granskningsunderlag",
            value: true,
            explanation:
              "Behåll de verkliga måtten och den valda maskinprofilen. Skapa endast ett designgranskningspaket; lagerläggning, nesting och CAM förblir blockerade tills en exakt lager- och maskinprofil har verifierats.",
          },
        }
      : {}),
  };
}

interface NestedPartGroup {
  placements: NestingPlacement[];
  overflow_part_ids: string[];
  used_sheet_count: number;
  used_area_mm2: number;
  available_area_mm2: number;
}

function nestPartGroup(
  parts: ResolvedPart[],
  sheetWidth: number,
  sheetHeight: number,
  stockCount: number,
  sheetOffset: number,
  stockRole: NestingPlacement["stock_role"],
): NestedPartGroup {
  const ordered = [...parts].sort((left, right) => {
    const areaDifference = right.width_mm * right.depth_mm - left.width_mm * left.depth_mm;
    return areaDifference === 0 ? left.part_id.localeCompare(right.part_id) : areaDifference;
  });
  const placements: NestingPlacement[] = [];
  const overflow: string[] = [];
  let sheet = 1;
  let cursorX = NESTING_GAP_MM;
  let cursorY = NESTING_GAP_MM;
  let rowHeight = 0;

  for (const part of ordered) {
    const rawWidth = part.width_mm;
    const rawHeight = part.depth_mm;
    const normalFitsSheet = rawWidth + 2 * NESTING_GAP_MM <= sheetWidth && rawHeight + 2 * NESTING_GAP_MM <= sheetHeight;
    const rotatedFitsSheet = rawHeight + 2 * NESTING_GAP_MM <= sheetWidth && rawWidth + 2 * NESTING_GAP_MM <= sheetHeight;
    if (!normalFitsSheet && !rotatedFitsSheet) {
      overflow.push(part.part_id);
      continue;
    }

    let rotated = !normalFitsSheet && rotatedFitsSheet;
    let placementWidth = rotated ? rawHeight : rawWidth;
    let placementHeight = rotated ? rawWidth : rawHeight;
    if (cursorX + placementWidth + NESTING_GAP_MM > sheetWidth && rotatedFitsSheet && cursorX + rawHeight + NESTING_GAP_MM <= sheetWidth) {
      rotated = true;
      placementWidth = rawHeight;
      placementHeight = rawWidth;
    }
    if (cursorX + placementWidth + NESTING_GAP_MM > sheetWidth) {
      cursorX = NESTING_GAP_MM;
      cursorY += rowHeight + NESTING_GAP_MM;
      rowHeight = 0;
      rotated = !normalFitsSheet && rotatedFitsSheet;
      placementWidth = rotated ? rawHeight : rawWidth;
      placementHeight = rotated ? rawWidth : rawHeight;
      if (cursorX + placementWidth + NESTING_GAP_MM > sheetWidth && rotatedFitsSheet) {
        rotated = true;
        placementWidth = rawHeight;
        placementHeight = rawWidth;
      }
    }
    if (cursorY + placementHeight + NESTING_GAP_MM > sheetHeight) {
      sheet += 1;
      if (sheet > stockCount) {
        overflow.push(part.part_id);
        continue;
      }
      cursorX = NESTING_GAP_MM;
      cursorY = NESTING_GAP_MM;
      rowHeight = 0;
      rotated = !normalFitsSheet && rotatedFitsSheet;
      placementWidth = rotated ? rawHeight : rawWidth;
      placementHeight = rotated ? rawWidth : rawHeight;
      if (cursorX + placementWidth + NESTING_GAP_MM > sheetWidth && rotatedFitsSheet) {
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
    cursorX += placementWidth + NESTING_GAP_MM;
    rowHeight = Math.max(rowHeight, placementHeight);
  }
  const sheetCount = placements.length === 0
    ? 0
    : Math.max(...placements.map((placement) => placement.sheet)) - sheetOffset;
  const usedArea = placements.reduce((sum, placement) => sum + placement.width_mm * placement.height_mm, 0);
  return {
    placements,
    overflow_part_ids: overflow,
    used_sheet_count: sheetCount,
    used_area_mm2: usedArea,
    available_area_mm2: sheetCount * sheetWidth * sheetHeight,
  };
}

function nestParts(spec: DesignSpec, parts: ResolvedPart[]): NestingResult {
  const carcass = nestPartGroup(
    parts.filter((part) => part.kind !== "back"),
    Math.max(spec.stock_width_mm, 1),
    Math.max(spec.stock_height_mm, 1),
    spec.stock_count,
    0,
    "carcass",
  );
  const back = nestPartGroup(
    parts.filter((part) => part.kind === "back"),
    Math.max(spec.back_stock_width_mm, 1),
    Math.max(spec.back_stock_height_mm, 1),
    spec.back_stock_count,
    carcass.used_sheet_count,
    "back",
  );
  const usedArea = carcass.used_area_mm2 + back.used_area_mm2;
  const availableArea = Math.max(
    carcass.available_area_mm2 + back.available_area_mm2,
    1,
  );
  return {
    placements: [...carcass.placements, ...back.placements],
    sheet_count: carcass.used_sheet_count + back.used_sheet_count,
    utilization_percent: round((usedArea / availableArea) * 100, 1),
    overflow_part_ids: [...carcass.overflow_part_ids, ...back.overflow_part_ids],
  };
}

function nestingRule(
  spec: DesignSpec,
  parts: ResolvedPart[],
  nesting: NestingResult,
): RuleEvaluation {
  const partsById = new Map(parts.map((part) => [part.part_id, part]));
  const oversizedOverflowPartIds = nesting.overflow_part_ids.filter((partId) => {
    const part = partsById.get(partId);
    if (!part) return false;
    const stockWidth = part.kind === "back" ? spec.back_stock_width_mm : spec.stock_width_mm;
    const stockHeight = part.kind === "back" ? spec.back_stock_height_mm : spec.stock_height_mm;
    const normalFits = part.width_mm + 2 * NESTING_GAP_MM <= stockWidth
      && part.depth_mm + 2 * NESTING_GAP_MM <= stockHeight;
    const rotatedFits = part.depth_mm + 2 * NESTING_GAP_MM <= stockWidth
      && part.width_mm + 2 * NESTING_GAP_MM <= stockHeight;
    return !normalFits && !rotatedFits;
  });
  const stockCapacity = "valda materialseparerade skivor";
  return {
    rule_id: "DFM-STOCK-001",
    rule_version: "1.0.0",
    status: nesting.overflow_part_ids.length > 0 ? "BLOCK" : "PASS",
    title: "Delar ryms i råmaterial",
    summary:
      nesting.overflow_part_ids.length > 0
        ? oversizedOverflowPartIds.length > 0
          ? `${nesting.overflow_part_ids.length} delar ryms inte i valt skivformat.`
          : `${nesting.overflow_part_ids.length} delar kan inte placeras inom ${stockCapacity}.`
        : `${nesting.sheet_count} skivor används med ${nesting.utilization_percent} % materialutnyttjande.`,
    calculation: "Materialseparerad deterministisk radplacering med 8 mm bearbetningsmellanrum",
    calculated_value: nesting.utilization_percent,
    unit: "%",
    assumptions: [
      "18 mm stomdelar och 6 mm ryggfält placeras aldrig på samma råskiva.",
      "Fiberriktning och visuell fanérmatchning behöver slutkontrolleras",
    ],
    affected_part_ids: nesting.overflow_part_ids,
    ...(oversizedOverflowPartIds.length > 0
      ? {
          suggestion: {
            action: "create_stockless_review_package" as const,
            label: "Skapa lagerobundet granskningsunderlag",
            value: true,
            explanation:
              "Behåll det verkliga råformatet och möbelns mått. Skapa endast ett designgranskningspaket; lagerinköp, nesting och CAM förblir blockerade tills en exakt lagerprofil har verifierats.",
          },
        }
      : {}),
  };
}

function generateBom(parts: ResolvedPart[]): BomLine[] {
  const grouped = new Map<string, ResolvedPart[]>();
  for (const part of parts) {
    const key = `${part.kind}:${part.material_id}:${round(part.width_mm, 1)}:${round(part.depth_mm, 1)}:${round(part.thickness_mm, 1)}`;
    grouped.set(key, [...(grouped.get(key) ?? []), part]);
  }
  const lines: BomLine[] = [...grouped.values()].map((group, index) => {
    const representative = group[0];
    if (!representative) throw new Error("BOM group may not be empty");
    return {
      id: `BOM-P-${String(index + 1).padStart(3, "0")}`,
      category: "part",
      item: representative.name.replace(/ \d+(?:\.\d+)?$/, ""),
      quantity: group.length,
      unit: "st",
      part_ids: group.map((part) => part.part_id),
      dimensions: `${round(representative.width_mm, 1)} × ${round(representative.depth_mm, 1)} × ${round(representative.thickness_mm, 1)} mm`,
      material: [...MATERIALS, ...BACK_MATERIALS].find(
        (material) => material.id === representative.material_id,
      )?.name ?? representative.material_id,
    };
  });
  return lines;
}

function generateOperations(parts: ResolvedPart[]): CamOperation[] {
  let sequence = 1;
  return parts.flatMap((part) =>
    part.features.map((feature) => ({
      id: `OP-${String(sequence).padStart(4, "0")}`,
      part_id: part.part_id,
      sequence: sequence++,
      operation:
        feature.kind === "outline"
          ? "Konturfräsning"
          : feature.kind === "drill"
            ? "Borrning"
            : feature.kind === "groove"
              ? "Spårfräsning"
              : feature.kind === "rabbet"
                ? "Falsfräsning"
                : feature.kind === "pocket"
                  ? "Fickfräsning"
                  : "Märkning",
      side: feature.face,
      tool: feature.tool_diameter_mm ? `Ø${feature.tool_diameter_mm} mm` : "T1",
      depth_mm: feature.depth_mm,
      status: "PASS" as const,
    })),
  );
}

function overallStatus(rules: RuleEvaluation[]): ValidationStatus {
  if (rules.some((rule) => rule.status === "BLOCK")) return "BLOCK";
  if (rules.some((rule) => rule.status === "WARNING")) return "WARNING";
  return "PASS";
}

export function mergeServerDesignWithLocalDfm(
  server: ResolvedDesign,
  local: ResolvedDesign,
): ResolvedDesign {
  const localDfmRules = local.rule_evaluations.filter((rule) => rule.rule_id.startsWith("DFM-"));
  const localDfmIds = new Set(localDfmRules.map((rule) => rule.rule_id));
  const rules = [
    ...server.rule_evaluations.filter((rule) => !localDfmIds.has(rule.rule_id)),
    ...localDfmRules,
  ];
  return {
    ...server,
    rule_evaluations: rules,
    status: overallStatus(rules),
  };
}

export function resolveDesign(spec: DesignSpec, changeDiff: ChangeDiff[] = []): ResolvedDesign {
  const resolvedSpec = balanceDesignSymmetry(spec);
  const parts = generateParts(resolvedSpec);
  const nesting = nestParts(resolvedSpec, parts);
  const rules = [
    invalidGeometryRule(resolvedSpec),
    topologyIntegrityRule(resolvedSpec, parts),
    shelfRule(resolvedSpec, parts),
    dryJoiningRule(resolvedSpec, parts),
    stabilityRule(resolvedSpec),
    backRule(resolvedSpec, parts),
    machineRule(resolvedSpec, parts),
    nestingRule(resolvedSpec, parts, nesting),
  ];
  const hardwareRule = cabinetHardwareRule(resolvedSpec, parts);
  const baseAlignmentRule = baseSupportAlignmentRule(resolvedSpec, parts);
  if (baseAlignmentRule) rules.push(baseAlignmentRule);
  if (hardwareRule) rules.push(hardwareRule);
  const partRule = individualPartRule(resolvedSpec);
  if (partRule) rules.push(partRule);
  return {
    design_hash: localDesignHash(resolvedSpec),
    spec: resolvedSpec,
    parts,
    bom: generateBom(parts),
    operations: generateOperations(parts),
    nesting,
    rule_evaluations: rules,
    status: overallStatus(rules),
    change_diff: changeDiff,
    source: "local",
  };
}

export function applySuggestion(spec: DesignSpec, evaluation: RuleEvaluation): { spec: DesignSpec; diff: ChangeDiff[] } {
  const suggestion = evaluation.suggestion;
  if (!suggestion) return { spec, diff: [] };
  if (suggestion.action === "set_divider_count" && typeof suggestion.value === "number") {
    const cleaned = withoutPartFamilies(spec, ["divider-", "shelf-", "back-panel"]);
    return {
      spec: {
        ...spec,
        ...cleaned,
        divider_count: suggestion.value,
        bay_width_ratios: [],
        reinforcement_mode: "auto",
      },
      diff: [
        {
          field: "divider_count",
          before: spec.divider_count,
          after: suggestion.value,
          reason: evaluation.summary,
        },
        {
          field: "reinforcement_mode",
          before: spec.reinforcement_mode,
          after: "auto",
          reason: "Förstärkningen godkändes manuellt i valideringspanelen.",
        },
      ],
    };
  }
  if (suggestion.action === "align_base_cabinets" && typeof suggestion.value === "number") {
    return {
      spec: {
        ...spec,
        base_cabinet_count: suggestion.value,
        bay_width_ratios: [],
        symmetry_locked: true,
      },
      diff: [
        {
          field: "base_cabinet_count",
          before: spec.base_cabinet_count,
          after: suggestion.value,
          reason: suggestion.explanation,
        },
      ],
    };
  }
  if (suggestion.action === "enable_back" && suggestion.value === true) {
    return {
      spec: { ...spec, back_panel: true },
      diff: [
        {
          field: "back_panel",
          before: spec.back_panel,
          after: true,
          reason: evaluation.summary,
        },
      ],
    };
  }
  return { spec, diff: [] };
}

export function partsDoNotOverlap(nesting: NestingResult): boolean {
  return nesting.placements.every((left, leftIndex) =>
    nesting.placements.every((right, rightIndex) => {
      if (leftIndex >= rightIndex || left.sheet !== right.sheet) return true;
      return (
        left.x_mm + left.width_mm <= right.x_mm ||
        right.x_mm + right.width_mm <= left.x_mm ||
        left.y_mm + left.height_mm <= right.y_mm ||
        right.y_mm + right.height_mm <= left.y_mm
      );
    }),
  );
}

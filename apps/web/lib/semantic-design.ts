import {
  DESIGN_CONSTRAINTS,
  maximumBaseCabinetHeightMm,
} from "./design-constraints";
import { resolveDesign } from "./design-engine";
import type { ChangeDiff, DesignSpec } from "./design-types";

export const SEMANTIC_COMPONENT_MIME = "application/x-custombuild-component";

export type SemanticComponentKind =
  | "shelf_row"
  | "divider"
  | "back_panel"
  | "plinth"
  | "base_cabinet";

export type SemanticSnapRelation =
  | "shelf_in_carcass"
  | "divider_in_carcass"
  | "back_behind_carcass"
  | "plinth_under_carcass"
  | "cabinet_under_shelves";

export interface SemanticComponentDefinition {
  kind: SemanticComponentKind;
  label: string;
  description: string;
  placement: string;
}

export interface SemanticDropRequest {
  kind: SemanticComponentKind;
  normalizedX: number;
  normalizedY: number;
}

export interface SemanticSnapPreview {
  kind: SemanticComponentKind;
  relation: SemanticSnapRelation;
  label: string;
  detail: string;
  targetId: string;
  /** Furniture-relative positions, measured from left or bottom. */
  normalizedPositions: number[];
}

export interface SemanticDropOutcome {
  spec: DesignSpec;
  diff: ChangeDiff[];
  message: string;
  detail: string;
  preview: SemanticSnapPreview;
}

export const SEMANTIC_COMPONENTS: readonly SemanticComponentDefinition[] = [
  {
    kind: "shelf_row",
    label: "Hyllplan",
    description: "Placeras på den valda höjden genom alla fack.",
    placement: "Höjd snappar i 5 mm",
  },
  {
    kind: "divider",
    label: "Avdelare",
    description: "Delar ett fack och bygger om alla hyllsegment.",
    placement: "Speglas automatiskt",
  },
  {
    kind: "base_cabinet",
    label: "Underskåp",
    description: "Skapar en sammanhängande skåpsrad under hyllorna.",
    placement: "Följer fackindelningen",
  },
  {
    kind: "back_panel",
    label: "Bakstycke",
    description: "Monteras bakom stommen med matchande noter.",
    placement: "6 mm produktionsregel",
  },
  {
    kind: "plinth",
    label: "Sockel",
    description: "Läggs under möbeln och räknas in i höjden.",
    placement: "80 mm indragen",
  },
] as const;

const COMPONENT_KINDS = new Set<SemanticComponentKind>(SEMANTIC_COMPONENTS.map(({ kind }) => kind));

function clamp(value: number, minimum = 0, maximum = 1): number {
  if (!Number.isFinite(value)) return (minimum + maximum) / 2;
  return Math.min(maximum, Math.max(minimum, value));
}

function rounded(value: number): number {
  return Math.round(value * 1_000_000) / 1_000_000;
}

function validBayRatios(spec: DesignSpec): number[] {
  const count = Math.max(1, Math.trunc(spec.divider_count) + 1);
  if (spec.bay_width_ratios.length !== count || spec.bay_width_ratios.some((value) => value <= 0)) {
    return Array.from({ length: count }, () => 1 / count);
  }
  const total = spec.bay_width_ratios.reduce((sum, value) => sum + value, 0);
  return spec.bay_width_ratios.map((value) => value / total);
}

function validShelfRatios(spec: DesignSpec): number[] {
  const count = Math.max(0, Math.trunc(spec.shelf_count));
  if (
    spec.shelf_height_ratios.length !== count
    || spec.shelf_height_ratios.some((value, index, values) => (
      value < 0.05 || value > 0.95 || (index > 0 && value - values[index - 1]! < 0.05)
    ))
  ) return Array.from({ length: count }, (_, index) => (index + 1) / (count + 1));
  return [...spec.shelf_height_ratios];
}

function boundariesFromRatios(ratios: number[]): number[] {
  const boundaries = [0];
  for (const ratio of ratios) boundaries.push(rounded(boundaries.at(-1)! + ratio));
  boundaries[boundaries.length - 1] = 1;
  return boundaries;
}

function ratiosFromBoundaries(boundaries: number[]): number[] {
  return boundaries.slice(1).map((boundary, index) => rounded(boundary - boundaries[index]!));
}

function closestValidPosition(boundaries: number[], requested: number, minimumGap: number): number {
  const value = clamp(requested, minimumGap, 1 - minimumGap);
  let bayIndex = boundaries.findIndex((boundary, index) => index > 0 && value < boundary);
  if (bayIndex < 1) bayIndex = boundaries.length - 1;
  const start = boundaries[bayIndex - 1]!;
  const end = boundaries[bayIndex]!;
  if (end - start < minimumGap * 2) {
    const candidates = boundaries.slice(1).map((right, index) => ({
      left: boundaries[index]!,
      right,
      width: right - boundaries[index]!,
    })).filter(({ width }) => width >= minimumGap * 2);
    const fallback = candidates.sort((left, right) => right.width - left.width)[0];
    if (!fallback) throw new Error("Det finns inte plats för ytterligare en bärande indelning.");
    return rounded((fallback.left + fallback.right) / 2);
  }
  return rounded(clamp(value, start + minimumGap, end - minimumGap));
}

function insertSymmetricPositions(
  existing: number[],
  requested: number,
  minimumGap: number,
  symmetryLocked: boolean,
  maximumCount: number,
): number[] {
  const boundaries = [0, ...existing, 1];
  const primary = closestValidPosition(boundaries, requested, minimumGap);
  const candidates = symmetryLocked && Math.abs(primary - 0.5) >= minimumGap / 2
    ? [primary, rounded(1 - primary)]
    : [symmetryLocked ? 0.5 : primary];
  const unique = [...new Set(candidates.map(rounded))]
    .filter((candidate) => existing.every((value) => Math.abs(value - candidate) >= minimumGap));
  if (existing.length + unique.length > maximumCount) throw new Error("Mallen har nått maximalt antal delar.");
  if (unique.length === 0) throw new Error("En del finns redan vid den valda positionen.");
  const result = [...existing, ...unique].sort((left, right) => left - right);
  if (result.some((value, index) => index > 0 && value - result[index - 1]! < minimumGap)) {
    throw new Error("Placeringen ligger för nära en befintlig del.");
  }
  return result;
}

function dividerPositions(spec: DesignSpec): number[] {
  return boundariesFromRatios(validBayRatios(spec)).slice(1, -1);
}

function mmLabel(value: number): string {
  return `${Math.round(value).toLocaleString("sv-SE")} mm`;
}

function previewFor(spec: DesignSpec, request: SemanticDropRequest): SemanticSnapPreview {
  if (request.kind === "shelf_row") {
    const desired = 1 - clamp(request.normalizedY);
    const positions = insertSymmetricPositions(
      validShelfRatios(spec), desired, 0.05, spec.symmetry_locked, 40,
    ).filter((value) => validShelfRatios(spec).every((current) => Math.abs(current - value) >= 0.05));
    const actual = positions.length > 0 ? positions : [desired];
    const heights = actual.map((ratio) => mmLabel(ratio * spec.height_mm));
    return {
      kind: request.kind,
      relation: "shelf_in_carcass",
      label: actual.length > 1 ? "Speglade hyllplan" : "Hyllplan",
      detail: heights.join(" och "),
      targetId: "furniture:shelves",
      normalizedPositions: actual,
    };
  }
  if (request.kind === "divider") {
    const existing = dividerPositions(spec);
    const all = insertSymmetricPositions(existing, clamp(request.normalizedX), 0.08, spec.symmetry_locked, 16);
    const positions = all.filter((value) => existing.every((current) => Math.abs(current - value) >= 0.08));
    return {
      kind: request.kind,
      relation: "divider_in_carcass",
      label: positions.length > 1 ? "Speglade avdelare" : "Avdelare",
      detail: positions.map((ratio) => mmLabel(ratio * spec.width_mm)).join(" och "),
      targetId: "furniture:carcass",
      normalizedPositions: positions,
    };
  }
  if (request.kind === "base_cabinet") return {
    kind: request.kind,
    relation: "cabinet_under_shelves",
    label: "Underskåpsrad",
    detail: `${spec.divider_count + 1} symmetriska moduler`,
    targetId: "furniture:lower-zone",
    normalizedPositions: [0.5],
  };
  if (request.kind === "back_panel") return {
    kind: request.kind,
    relation: "back_behind_carcass",
    label: "Bakstycke",
    detail: "Fästs bakom hela stommen",
    targetId: "furniture:rear",
    normalizedPositions: [0.5],
  };
  return {
    kind: request.kind,
    relation: "plinth_under_carcass",
    label: "Sockel",
    detail: "Fästs under hela stommen",
    targetId: "furniture:base",
    normalizedPositions: [0.5],
  };
}

export function writeSemanticDragPayload(dataTransfer: DataTransfer, kind: SemanticComponentKind): void {
  dataTransfer.effectAllowed = "copy";
  dataTransfer.setData(SEMANTIC_COMPONENT_MIME, kind);
  dataTransfer.setData("text/plain", kind);
}

export function readSemanticDragPayload(dataTransfer: DataTransfer): SemanticComponentKind | undefined {
  const raw = dataTransfer.getData(SEMANTIC_COMPONENT_MIME) || dataTransfer.getData("text/plain");
  return COMPONENT_KINDS.has(raw as SemanticComponentKind) ? raw as SemanticComponentKind : undefined;
}

export function defaultSemanticDropRequest(spec: DesignSpec, kind: SemanticComponentKind): SemanticDropRequest {
  if (kind === "divider") {
    const boundaries = boundariesFromRatios(validBayRatios(spec));
    const widest = boundaries.slice(1).map((right, index) => ({
      center: (right + boundaries[index]!) / 2,
      width: right - boundaries[index]!,
    })).sort((left, right) => right.width - left.width)[0];
    return { kind, normalizedX: widest?.center ?? 0.5, normalizedY: 0.5 };
  }
  if (kind === "shelf_row") {
    const levels = [0, ...validShelfRatios(spec), 1];
    const widest = levels.slice(1).map((top, index) => ({
      center: (top + levels[index]!) / 2,
      height: top - levels[index]!,
    })).sort((left, right) => right.height - left.height)[0];
    return { kind, normalizedX: 0.5, normalizedY: 1 - (widest?.center ?? 0.5) };
  }
  return { kind, normalizedX: 0.5, normalizedY: 0.5 };
}

export function createSemanticSnapPreview(spec: DesignSpec, request: SemanticDropRequest): SemanticSnapPreview {
  return previewFor(spec, request);
}

export function resolveSemanticDrop(spec: DesignSpec, request: SemanticDropRequest): SemanticDropOutcome {
  const preview = previewFor(spec, request);

  if (request.kind === "shelf_row") {
    const levels = insertSymmetricPositions(
      validShelfRatios(spec), 1 - clamp(request.normalizedY), 0.05, spec.symmetry_locked, 40,
    );
    const added = levels.length - spec.shelf_count;
    const next = { ...spec, shelf_count: levels.length, shelf_height_ratios: levels, fixed_shelves: true };
    return {
      spec: next,
      diff: [{ field: "shelf_count", before: spec.shelf_count, after: levels.length, reason: "Hyllplan placerades semantiskt i stommen." }],
      message: added > 1 ? "Två speglade hyllplan lades till." : "Hyllplanet lades till.",
      detail: `${preview.detail}. Alla fack, förband och stöd räknades om.`,
      preview,
    };
  }

  if (request.kind === "divider") {
    const existing = dividerPositions(spec);
    const positions = insertSymmetricPositions(existing, clamp(request.normalizedX), 0.08, spec.symmetry_locked, 16);
    const boundaries = [0, ...positions, 1];
    const added = positions.length - existing.length;
    const next = {
      ...spec,
      divider_count: positions.length,
      bay_sizing_mode: "count" as const,
      bay_width_ratios: ratiosFromBoundaries(boundaries),
      reinforcement_mode: "manual" as const,
    };
    return {
      spec: next,
      diff: [{ field: "divider_count", before: spec.divider_count, after: positions.length, reason: "Avdelare placerades semantiskt och hyllorna segmenterades om." }],
      message: added > 1 ? "Två speglade avdelare lades till." : "Avdelaren lades till.",
      detail: `${preview.detail}. Alla hyllsegment följer nu den nya fackindelningen.`,
      preview,
    };
  }

  if (request.kind === "base_cabinet") {
    const count = spec.divider_count + 1;
    const maximumBaseHeightMm = maximumBaseCabinetHeightMm(
      spec.height_mm,
      spec.measured_thickness_mm,
    );
    if (maximumBaseHeightMm < DESIGN_CONSTRAINTS.wallLibraryBaseHeightMinimumMm) {
      throw new Error(
        `Möbeln är för kort för ett underskåp på minst ${DESIGN_CONSTRAINTS.wallLibraryBaseHeightMinimumMm} mm med föreskriven fri höjd ovanför.`,
      );
    }
    const requestedBaseHeightMm = spec.base_cabinet_height_mm > 0
      ? spec.base_cabinet_height_mm
      : 680;
    const baseHeightMm = Math.min(
      maximumBaseHeightMm,
      Math.max(DESIGN_CONSTRAINTS.wallLibraryBaseHeightMinimumMm, requestedBaseHeightMm),
    );
    const next = {
      ...spec,
      furniture_type: "wall_library" as const,
      base_cabinet_count: count,
      base_cabinet_height_mm: baseHeightMm,
      base_cabinet_depth_mm: spec.depth_mm,
    };
    const geometryRule = resolveDesign(next).rule_evaluations
      .find(({ rule_id }) => rule_id === "GEO-001");
    if (geometryRule?.status !== "PASS") {
      throw new Error(
        `Underskåpsraden kan inte läggas till utan ogiltig geometri. ${geometryRule?.summary ?? "Kontrollera möbelns mått och indelning."}`,
      );
    }
    return {
      spec: next,
      diff: [{ field: "base_cabinet_count", before: spec.base_cabinet_count, after: count, reason: "Underskåpsraden kopplades till den bärande fackindelningen." }],
      message: "Underskåpsraden lades till.",
      detail: `${count} moduler följer överdelens symmetri och bredd.`,
      preview,
    };
  }

  if (request.kind === "back_panel") {
    if (spec.back_panel) throw new Error("Möbeln har redan ett bakstycke.");
    return {
      spec: { ...spec, back_panel: true },
      diff: [{ field: "back_panel", before: false, after: true, reason: "Bakstycket snappades till stommens baksida." }],
      message: "Bakstycket lades till.",
      detail: "Noter och stabilitetskontroll regenererades.",
      preview,
    };
  }

  if (spec.plinth) throw new Error("Möbeln har redan en sockel.");
  return {
    spec: { ...spec, plinth: true },
    diff: [{ field: "plinth", before: false, after: true, reason: "Sockeln snappades till stommens undersida." }],
    message: "Sockeln lades till.",
    detail: "Totalhöjd och fria öppningar räknades om.",
    preview,
  };
}

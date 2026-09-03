import {
  adaptStructuralSupports,
  balanceDesignSymmetry,
  resolveDesign,
} from "./design-engine";
import type { ChangeDiff, DesignSpec, ResolvedDesign } from "./design-types";
import { parseLocalDesignSpec } from "./workspace-design-envelope";

const DESIGN_SPEC_FIELD_ORDER = [
  "schema_version",
  "design_id",
  "revision",
  "furniture_type",
  "width_mm",
  "height_mm",
  "depth_mm",
  "material_id",
  "material_name",
  "back_material_id",
  "nominal_thickness_mm",
  "measured_thickness_mm",
  "shelf_count",
  "fixed_shelves",
  "load_per_shelf_kg",
  "back_panel",
  "back_panel_type",
  "plinth",
  "plinth_height_mm",
  "divider_count",
  "bay_sizing_mode",
  "target_bay_width_mm",
  "bay_width_ratios",
  "shelf_height_ratios",
  "symmetry_locked",
  "reference_image_import",
  "part_overrides",
  "removed_part_ids",
  "topology_baseline",
  "base_cabinet_height_mm",
  "base_cabinet_depth_mm",
  "base_cabinet_count",
  "reinforcement_mode",
  "joint_system",
  "edge_band_mm",
  "wall_anchor_required",
  "wall_anchor_verified",
  "stock_width_mm",
  "stock_height_mm",
  "stock_count",
  "back_stock_width_mm",
  "back_stock_height_mm",
  "back_stock_count",
  "machine_profile_id",
] as const satisfies readonly (keyof DesignSpec)[];

type MissingDesignSpecField = Exclude<keyof DesignSpec, (typeof DESIGN_SPEC_FIELD_ORDER)[number]>;
const DESIGN_SPEC_FIELD_ORDER_IS_EXHAUSTIVE: Record<MissingDesignSpecField, never> = {};
void DESIGN_SPEC_FIELD_ORDER_IS_EXHAUSTIVE;

export interface WorkspaceDesignFieldChange {
  field: keyof DesignSpec;
  before: unknown;
  after: unknown;
}

export interface NormalizedWorkspaceDesignTransaction {
  normalizedSpec: DesignSpec;
  requestedDiff: ChangeDiff[];
  structuralDiff: ChangeDiff[];
  changeDiff: ChangeDiff[];
  changedFields: WorkspaceDesignFieldChange[];
}

export interface PreviewWorkspaceDesignTransaction extends NormalizedWorkspaceDesignTransaction {
  resolvedDesign: ResolvedDesign;
}

function canonicalJson(value: unknown): string {
  if (value === undefined) return "undefined";
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0)
      .map(([key, child]) => `${JSON.stringify(key)}:${canonicalJson(child)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

function canonicalSnapshot(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalSnapshot);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0)
        .map(([key, child]) => [key, canonicalSnapshot(child)]),
    );
  }
  return value;
}

function cloneDiff(diff: readonly ChangeDiff[]): ChangeDiff[] {
  return diff.map((entry) => ({ ...entry }));
}

const DIVIDER_TOPOLOGY_PART_PREFIXES = ["divider-", "shelf-", "back-panel"] as const;
const SHELF_TOPOLOGY_PART_PREFIXES = ["shelf-"] as const;
const BACK_TOPOLOGY_PART_PREFIXES = ["back-panel"] as const;
const BASE_TOPOLOGY_PART_PREFIXES = [
  "base-side-",
  "base-bottom-",
  "base-top-",
  "cabinet-front-",
] as const;

function cleanupChangedTopologyCustomizations(
  source: DesignSpec,
  candidate: DesignSpec,
): DesignSpec {
  const prefixes = new Set<string>();
  if (source.divider_count !== candidate.divider_count) {
    DIVIDER_TOPOLOGY_PART_PREFIXES.forEach((prefix) => prefixes.add(prefix));
  }
  if (source.shelf_count !== candidate.shelf_count) {
    SHELF_TOPOLOGY_PART_PREFIXES.forEach((prefix) => prefixes.add(prefix));
  }
  if (
    source.back_panel !== candidate.back_panel
    || source.back_panel_type !== candidate.back_panel_type
  ) {
    BACK_TOPOLOGY_PART_PREFIXES.forEach((prefix) => prefixes.add(prefix));
  }
  if (
    source.base_cabinet_count !== candidate.base_cabinet_count
    || source.furniture_type !== candidate.furniture_type
  ) {
    BASE_TOPOLOGY_PART_PREFIXES.forEach((prefix) => prefixes.add(prefix));
  }
  if (prefixes.size === 0) return candidate;

  const matchesChangedFamily = (partId: string) => (
    [...prefixes].some((prefix) => partId.startsWith(prefix))
  );
  return {
    ...candidate,
    part_overrides: Object.fromEntries(
      Object.entries(candidate.part_overrides)
        .filter(([partId]) => !matchesChangedFamily(partId)),
    ),
    removed_part_ids: candidate.removed_part_ids
      .filter((partId) => !matchesChangedFamily(partId)),
  };
}

function changedDesignFields(
  source: DesignSpec,
  normalizedSpec: DesignSpec,
): WorkspaceDesignFieldChange[] {
  return DESIGN_SPEC_FIELD_ORDER.flatMap((field) => {
    const before = source[field];
    const after = normalizedSpec[field];
    if (canonicalJson(before) === canonicalJson(after)) return [];
    return [{
      field,
      before: canonicalSnapshot(before),
      after: canonicalSnapshot(after),
    }];
  });
}

/**
 * Applies the workspace's canonical local transaction boundary without
 * resolving geometry. Keeping this path pure lets commits and previews share
 * normalization without adding a second resolve to ordinary workspace edits.
 */
export function normalizeWorkspaceDesignTransaction(
  source: DesignSpec,
  candidate: DesignSpec,
  requestedDiff: readonly ChangeDiff[] = [],
): NormalizedWorkspaceDesignTransaction {
  const topologySafeCandidate = cleanupChangedTopologyCustomizations(source, candidate);
  const boundedCandidate = parseLocalDesignSpec(topologySafeCandidate);
  const balancedCandidate = balanceDesignSymmetry(boundedCandidate);
  const adapted = adaptStructuralSupports(balancedCandidate);
  const normalizedSpec = parseLocalDesignSpec(balanceDesignSymmetry(adapted.spec));
  const requested = cloneDiff(requestedDiff);
  const structural = cloneDiff(adapted.diff);
  return {
    normalizedSpec,
    requestedDiff: requested,
    structuralDiff: structural,
    changeDiff: [...requested, ...structural],
    changedFields: changedDesignFields(source, normalizedSpec),
  };
}

/** Resolves the exact normalized transaction that a later commit can reuse. */
export function previewWorkspaceDesignTransaction(
  source: DesignSpec,
  candidate: DesignSpec,
  requestedDiff: readonly ChangeDiff[] = [],
): PreviewWorkspaceDesignTransaction {
  const transaction = normalizeWorkspaceDesignTransaction(source, candidate, requestedDiff);
  return {
    ...transaction,
    resolvedDesign: resolveDesign(transaction.normalizedSpec, transaction.changeDiff),
  };
}

import { describe, expect, it } from "vitest";
import { DEFAULT_DESIGN_SPEC, type ChangeDiff, type DesignSpec } from "./design-types";
import {
  normalizeWorkspaceDesignTransaction,
  previewWorkspaceDesignTransaction,
} from "./workspace-design-transaction";

function manualSpec(patch: Partial<DesignSpec> = {}): DesignSpec {
  return {
    ...DEFAULT_DESIGN_SPEC,
    reinforcement_mode: "manual",
    ...patch,
  };
}

describe("workspace design transaction", () => {
  it("normalizes a wall-library divider fix and its base-count follow-up once", () => {
    const source = manualSpec({
      furniture_type: "wall_library",
      width_mm: 1_200,
      height_mm: 2_600,
      depth_mm: 320,
      base_cabinet_height_mm: 720,
      base_cabinet_depth_mm: 320,
      base_cabinet_count: 1,
      divider_count: 0,
    });
    const candidate: DesignSpec = {
      ...source,
      width_mm: 4_200,
      base_cabinet_count: 4,
      reinforcement_mode: "auto",
    };
    const requestedDiff: ChangeDiff[] = [{
      field: "width_mm",
      before: source.width_mm,
      after: candidate.width_mm,
      reason: "Requested width",
    }];

    const transaction = previewWorkspaceDesignTransaction(source, candidate, requestedDiff);

    expect(transaction.normalizedSpec.divider_count).toBe(4);
    expect(transaction.normalizedSpec.base_cabinet_count).toBe(5);
    expect(transaction.requestedDiff).toEqual(requestedDiff);
    expect(transaction.structuralDiff).toEqual([
      expect.objectContaining({ field: "divider_count", before: 0, after: 4 }),
      expect.objectContaining({ field: "base_cabinet_count", before: 4, after: 5 }),
    ]);
    expect(transaction.changeDiff).toEqual([
      requestedDiff[0],
      ...transaction.structuralDiff,
    ]);
    expect(transaction.changedFields.map(({ field }) => field)).toEqual([
      "width_mm",
      "divider_count",
      "base_cabinet_count",
      "reinforcement_mode",
    ]);
    expect(transaction.resolvedDesign.spec).toEqual(transaction.normalizedSpec);
    expect(transaction.resolvedDesign.change_diff).toEqual(transaction.changeDiff);
  });

  it("reports a normalized no-op without field or rule diffs", () => {
    const source = manualSpec();

    const transaction = normalizeWorkspaceDesignTransaction(source, source);

    expect(transaction.normalizedSpec).toEqual(source);
    expect(transaction.normalizedSpec).not.toBe(source);
    expect(transaction.requestedDiff).toEqual([]);
    expect(transaction.structuralDiff).toEqual([]);
    expect(transaction.changeDiff).toEqual([]);
    expect(transaction.changedFields).toEqual([]);
  });

  it("includes identity fields in deterministic DesignSpec order", () => {
    const source = manualSpec();
    const candidate = {
      ...source,
      design_id: "project-identity-002",
      revision: 8,
    };

    const transaction = normalizeWorkspaceDesignTransaction(source, candidate);

    expect(transaction.changedFields).toEqual([
      { field: "design_id", before: source.design_id, after: "project-identity-002" },
      { field: "revision", before: source.revision, after: 8 },
    ]);
  });

  it("compares arrays by order and record maps independently of key insertion order", () => {
    const source = manualSpec({
      symmetry_locked: false,
      shelf_height_ratios: [0.15, 0.35, 0.5, 0.7, 0.85],
      part_overrides: {
        "shelf-1-bay-1": { depth_mm: 280, position_z_mm: 410 },
        "shelf-2-bay-1": { depth_mm: 275 },
      },
    });
    const reorderedMap = {
      "shelf-2-bay-1": { depth_mm: 275 },
      "shelf-1-bay-1": { position_z_mm: 410, depth_mm: 280 },
    };
    const mapOnly = normalizeWorkspaceDesignTransaction(source, {
      ...source,
      part_overrides: reorderedMap,
    });
    const arrayChange = normalizeWorkspaceDesignTransaction(source, {
      ...source,
      shelf_height_ratios: [0.15, 0.35, 0.55, 0.7, 0.85],
      part_overrides: reorderedMap,
    });
    const mapChange = normalizeWorkspaceDesignTransaction(source, {
      ...source,
      part_overrides: {
        "shelf-2-bay-1": { depth_mm: 295 },
        "shelf-1-bay-1": { position_z_mm: 410, depth_mm: 280 },
      },
    });

    expect(mapOnly.changedFields).toEqual([]);
    expect(arrayChange.changedFields).toEqual([{
      field: "shelf_height_ratios",
      before: [0.15, 0.35, 0.5, 0.7, 0.85],
      after: [0.15, 0.35, 0.55, 0.7, 0.85],
    }]);
    expect(mapChange.changedFields).toEqual([{
      field: "part_overrides",
      before: {
        "shelf-1-bay-1": { depth_mm: 280, position_z_mm: 410 },
        "shelf-2-bay-1": { depth_mm: 275 },
      },
      after: {
        "shelf-1-bay-1": { depth_mm: 280, position_z_mm: 410 },
        "shelf-2-bay-1": { depth_mm: 295 },
      },
    }]);
  });

  it("cleans only customization families invalidated by a manual topology change", () => {
    const source = manualSpec({
      furniture_type: "wall_library",
      divider_count: 2,
      shelf_count: 2,
      base_cabinet_count: 2,
      base_cabinet_height_mm: 720,
      base_cabinet_depth_mm: 320,
      part_overrides: {
        "divider-2": { position_x_mm: 810 },
        "shelf-2-bay-3": { depth_mm: 260 },
        "back-panel-bay-3": { width_mm: 390 },
        "base-side-2": { position_x_mm: 610 },
        "side-left": { depth_mm: 300 },
      },
      removed_part_ids: [
        "divider-1",
        "shelf-1-bay-3",
        "back-panel-bay-2",
        "base-bottom-2",
        "side-right",
      ],
    });

    const transaction = normalizeWorkspaceDesignTransaction(source, {
      ...source,
      divider_count: 1,
    });

    expect(transaction.normalizedSpec.part_overrides).toEqual({
      "base-side-2": { position_x_mm: 610 },
      "side-left": { depth_mm: 300 },
    });
    expect(transaction.normalizedSpec.removed_part_ids).toEqual([
      "base-bottom-2",
      "side-right",
    ]);
    expect(transaction.changedFields).toEqual(expect.arrayContaining([
      expect.objectContaining({ field: "divider_count", before: 2, after: 1 }),
      expect.objectContaining({ field: "part_overrides" }),
      expect.objectContaining({ field: "removed_part_ids" }),
    ]));
  });

  it.each([
    {
      name: "shelf count",
      patch: { shelf_count: 1 },
      removedOverride: "shelf-2-bay-1",
      preservedOverride: "back-panel",
    },
    {
      name: "back mounting type",
      patch: { back_panel_type: "surface_mounted" as const },
      removedOverride: "back-panel",
      preservedOverride: "shelf-2-bay-1",
    },
    {
      name: "back presence",
      patch: { back_panel: false },
      removedOverride: "back-panel",
      preservedOverride: "shelf-2-bay-1",
    },
    {
      name: "base count",
      patch: { base_cabinet_count: 1 },
      removedOverride: "base-side-2",
      preservedOverride: "shelf-2-bay-1",
    },
  ])("cleans the $name family before local parsing", ({ patch, removedOverride, preservedOverride }) => {
    const source = manualSpec({
      furniture_type: "wall_library",
      base_cabinet_count: 2,
      base_cabinet_height_mm: 720,
      base_cabinet_depth_mm: 320,
      part_overrides: {
        "shelf-2-bay-1": { depth_mm: 260 },
        "back-panel": { width_mm: 1_150 },
        "base-side-2": { position_x_mm: 610 },
        "side-left": { depth_mm: 300 },
      },
      removed_part_ids: [],
    });

    const transaction = normalizeWorkspaceDesignTransaction(source, {
      ...source,
      ...patch,
    });

    expect(transaction.normalizedSpec.part_overrides).not.toHaveProperty(removedOverride);
    expect(transaction.normalizedSpec.part_overrides).toHaveProperty(preservedOverride);
    expect(transaction.normalizedSpec.part_overrides).toHaveProperty("side-left");
  });

  it("does not mutate or expose mutable aliases to its inputs", () => {
    const source = manualSpec({
      shelf_height_ratios: [0.15, 0.35, 0.5, 0.7, 0.85],
      part_overrides: { "shelf-1-bay-1": { depth_mm: 280 } },
    });
    const candidate: DesignSpec = {
      ...source,
      shelf_height_ratios: [...source.shelf_height_ratios],
      part_overrides: {
        "shelf-1-bay-1": { depth_mm: 290 },
      },
    };
    const requestedDiff: ChangeDiff[] = [{
      field: "depth_mm",
      before: 320,
      after: 321,
      reason: "Requested depth",
    }];
    const sourceBefore = structuredClone(source);
    const candidateBefore = structuredClone(candidate);
    const requestedBefore = structuredClone(requestedDiff);

    const transaction = normalizeWorkspaceDesignTransaction(source, candidate, requestedDiff);
    (transaction.changedFields.find(({ field }) => field === "part_overrides")?.after as Record<string, { depth_mm: number }>)["shelf-1-bay-1"]!.depth_mm = 999;
    transaction.requestedDiff[0]!.reason = "Changed result";

    expect(source).toEqual(sourceBefore);
    expect(candidate).toEqual(candidateBefore);
    expect(requestedDiff).toEqual(requestedBefore);
    expect(transaction.normalizedSpec.part_overrides["shelf-1-bay-1"]?.depth_mm).toBe(290);
  });
});

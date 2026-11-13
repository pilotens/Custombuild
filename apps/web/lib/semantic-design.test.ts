import { describe, expect, it } from "vitest";
import { DEFAULT_DESIGN_SPEC, type DesignSpec } from "./design-types";
import { removePartFromDesign, resolveDesign, restorePartCustomizations } from "./design-engine";
import { normalizeWorkspaceDesignTransaction } from "./workspace-design-transaction";
import { parseLocalDesignSpec } from "./workspace-design-envelope";
import {
  createSemanticSnapPreview,
  defaultSemanticDropRequest,
  resolveSemanticDrop,
  resolveSemanticRemoval,
  type SemanticComponentKind,
} from "./semantic-design";

describe("semantic furniture studio", () => {
  it("adds a mirrored divider pair and regenerates symmetric bay ratios", () => {
    const outcome = resolveSemanticDrop(
      {
        ...DEFAULT_DESIGN_SPEC,
        bay_sizing_mode: "target_width",
        target_bay_width_mm: 300,
      },
      {
        kind: "divider",
        normalizedX: 0.3,
        normalizedY: 0.5,
      },
    );

    expect(outcome.spec.divider_count).toBe(2);
    expect(outcome.spec.bay_sizing_mode).toBe("count");
    expect(outcome.spec.reinforcement_mode).toBe("manual");
    expect(outcome.spec.bay_width_ratios).toEqual([0.3, 0.4, 0.3]);
    expect(outcome.message).toContain("speglade");
    expect(outcome.spec.part_overrides).toEqual({});
  });

  it("places shelf rows at the intended height while retaining vertical symmetry", () => {
    const outcome = resolveSemanticDrop(DEFAULT_DESIGN_SPEC, {
      kind: "shelf_row",
      normalizedX: 0.5,
      normalizedY: 0.2,
    });

    expect(outcome.spec.shelf_count).toBe(7);
    expect(outcome.spec.shelf_height_ratios).toHaveLength(7);
    outcome.spec.shelf_height_ratios.forEach((ratio, index, ratios) => {
      expect(ratio + ratios[ratios.length - 1 - index]!).toBeCloseTo(1, 5);
    });
    expect(outcome.detail).toContain("Alla fack, förband och stöd räknades om");
  });

  it("allows a single unsymmetrical placement only when symmetry is unlocked", () => {
    const outcome = resolveSemanticDrop(
      { ...DEFAULT_DESIGN_SPEC, symmetry_locked: false },
      { kind: "divider", normalizedX: 0.28, normalizedY: 0.5 },
    );

    expect(outcome.spec.divider_count).toBe(1);
    expect(outcome.spec.bay_width_ratios).toEqual([0.28, 0.72]);
  });

  it("creates a lower cabinet row that follows the structural bays", () => {
    const outcome = resolveSemanticDrop(
      { ...DEFAULT_DESIGN_SPEC, divider_count: 2, bay_width_ratios: [0.25, 0.5, 0.25] },
      { kind: "base_cabinet", normalizedX: 0.5, normalizedY: 0.8 },
    );

    expect(outcome.spec.furniture_type).toBe("wall_library");
    expect(outcome.spec.base_cabinet_count).toBe(3);
    expect(outcome.spec.base_cabinet_height_mm).toBe(680);
    expect(outcome.spec.base_cabinet_depth_mm).toBe(DEFAULT_DESIGN_SPEC.depth_mm);
  });

  it("derives the strict maximum base height instead of creating an invalid short model", () => {
    const outcome = resolveSemanticDrop(
      {
        ...DEFAULT_DESIGN_SPEC,
        height_mm: 1_000,
        shelf_count: 0,
        shelf_height_ratios: [],
        base_cabinet_height_mm: 1_400,
      },
      { kind: "base_cabinet", normalizedX: 0.5, normalizedY: 0.8 },
    );

    expect(outcome.spec.base_cabinet_height_mm).toBe(782);
    expect(outcome.spec.base_cabinet_height_mm).toBeLessThan(
      outcome.spec.height_mm - outcome.spec.measured_thickness_mm - 200,
    );
  });

  it("leaves a too-short or too-narrow model unchanged with a clear error", () => {
    const short = {
      ...DEFAULT_DESIGN_SPEC,
      height_mm: 517,
      shelf_count: 0,
      shelf_height_ratios: [],
    };
    expect(() => resolveSemanticDrop(
      short,
      { kind: "base_cabinet", normalizedX: 0.5, normalizedY: 0.8 },
    )).toThrow(/för kort/);
    expect(short.furniture_type).toBe("bookcase");
    expect(short.base_cabinet_count).toBe(0);

    const narrowBays = {
      ...DEFAULT_DESIGN_SPEC,
      width_mm: 600,
      divider_count: 2,
      bay_width_ratios: [],
    };
    expect(() => resolveSemanticDrop(
      narrowBays,
      { kind: "base_cabinet", normalizedX: 0.5, normalizedY: 0.8 },
    )).toThrow(/ogiltig geometri/);
    expect(narrowBays.furniture_type).toBe("bookcase");
    expect(narrowBays.base_cabinet_count).toBe(0);
  });

  it("previews the exact semantic result and chooses the largest free gap for click insertion", () => {
    const request = defaultSemanticDropRequest(DEFAULT_DESIGN_SPEC, "divider");
    const preview = createSemanticSnapPreview(DEFAULT_DESIGN_SPEC, request);

    expect(request.normalizedX).toBe(0.5);
    expect(preview.relation).toBe("divider_in_carcass");
    expect(preview.normalizedPositions).toEqual([0.5]);
    expect(preview.detail).toContain("600 mm");
  });
});

describe("semantic component removal", () => {
  const manualSpec: DesignSpec = {
    ...DEFAULT_DESIGN_SPEC,
    reinforcement_mode: "manual",
    symmetry_locked: false,
  };

  function remove(spec: DesignSpec, kind: SemanticComponentKind, selectedId?: string): DesignSpec {
    const outcome = resolveSemanticRemoval(spec, kind, selectedId);
    return normalizeWorkspaceDesignTransaction(spec, outcome.spec, outcome.diff).normalizedSpec;
  }

  function insert(spec: DesignSpec, kind: SemanticComponentKind): DesignSpec {
    const outcome = resolveSemanticDrop(spec, defaultSemanticDropRequest(spec, kind));
    return normalizeWorkspaceDesignTransaction(spec, outcome.spec, outcome.diff).normalizedSpec;
  }

  it("removes the selected divider and preserves the existing physical bay-merge semantics", () => {
    const spec = {
      ...manualSpec,
      divider_count: 3,
      bay_sizing_mode: "target_width" as const,
      reinforcement_mode: "auto" as const,
      bay_width_ratios: [0.2, 0.25, 0.3, 0.25],
      part_overrides: { "shelf-1-bay-1": { width_mm: 180 }, "side-left": { depth_mm: 350 } },
      removed_part_ids: ["back-panel-bay-2"],
    };
    const outcome = remove(spec, "divider", "divider-2");
    const expected = removePartFromDesign(spec, "divider-2").spec;

    expect(outcome.divider_count).toBe(2);
    expect(outcome.bay_width_ratios).toEqual(expected.bay_width_ratios);
    expect(outcome.bay_sizing_mode).toBe("count");
    expect(outcome.reinforcement_mode).toBe("manual");
    expect(outcome.part_overrides).toEqual({ "side-left": { depth_mm: 350 } });
    expect(outcome.removed_part_ids).toEqual([]);
    expect(resolveDesign(outcome).parts.filter(({ kind }) => kind === "divider")).toHaveLength(2);
  });

  it("uses the last divider when selection is absent, unrelated or out of range", () => {
    const spec = { ...manualSpec, divider_count: 3, bay_width_ratios: [0.2, 0.2, 0.25, 0.35] };
    const expected = removePartFromDesign(spec, "divider-3").spec;
    for (const selectedId of [undefined, "top", "divider-99", "divider-0", "shelf-2-bay-1"]) {
      expect(remove(spec, "divider", selectedId).bay_width_ratios).toEqual(expected.bay_width_ratios);
    }
  });

  it("removes a complete selected shelf row and otherwise the top row", () => {
    const spec = { ...manualSpec, divider_count: 2, shelf_count: 3, shelf_height_ratios: [0.2, 0.5, 0.8] };
    const selected = remove(spec, "shelf_row", "shelf-2-bay-3");
    expect(selected.shelf_count).toBe(2);
    expect(selected.shelf_height_ratios).toEqual([0.2, 0.8]);
    expect(resolveDesign(selected).parts.filter(({ kind }) => kind === "shelf")).toHaveLength(6);
    expect(remove(spec, "shelf_row").shelf_height_ratios).toEqual([0.2, 0.5]);
  });

  it("keeps the existing symmetry lock active when removing one parametric unit", () => {
    const spec = { ...manualSpec, symmetry_locked: true, divider_count: 3, bay_width_ratios: [0.2, 0.3, 0.3, 0.2] };
    const next = remove(spec, "divider", "divider-1");
    expect(next.divider_count).toBe(2);
    expect(next.symmetry_locked).toBe(true);
    expect(next.bay_width_ratios[0]).toBeCloseTo(next.bay_width_ratios.at(-1)!, 6);
  });

  it.each(["divider", "shelf_row"] as const)("supports removal down to zero %s components", (kind) => {
    const spec = { ...manualSpec, divider_count: 1, shelf_count: 1 };
    const next = remove(spec, kind);
    expect(kind === "divider" ? next.divider_count : next.shelf_count).toBe(0);
    expect(kind === "divider" ? next.bay_width_ratios : next.shelf_height_ratios).toEqual([]);
    expect(() => resolveSemanticRemoval(next, kind)).toThrow(/inga/);
  });

  it.each(["divider", "shelf_row"] as const)("keeps maximum-count %s removal inside strict ratio constraints", (kind) => {
    const spec = { ...manualSpec, width_mm: 6_000, height_mm: 4_000, divider_count: 16, shelf_count: 40 };
    const outcome = resolveSemanticRemoval(spec, kind);
    const next = remove(spec, kind);
    expect(kind === "divider" ? next.divider_count : next.shelf_count).toBe(kind === "divider" ? 15 : 39);
    expect(kind === "divider" ? next.bay_width_ratios : next.shelf_height_ratios).toEqual([]);
    expect(outcome.detail).toContain("fördelas jämnt");
  });

  it("removes all back panels, clears only their edits and permits re-addition", () => {
    const spec = {
      ...manualSpec,
      divider_count: 1,
      part_overrides: { "back-panel-bay-1": { width_mm: 520 }, "side-left": { depth_mm: 330 } },
      removed_part_ids: ["back-panel-bay-2"],
    };
    const next = remove(spec, "back_panel");
    expect(next.back_panel).toBe(false);
    expect(next.part_overrides).toEqual({ "side-left": { depth_mm: 330 } });
    expect(next.removed_part_ids).toEqual([]);
    expect(resolveDesign(next).parts.some(({ kind }) => kind === "back")).toBe(false);
    expect(insert(next, "back_panel").back_panel).toBe(true);
    expect(() => resolveSemanticRemoval(next, "back_panel")).toThrow(/inget bakstycke/);
  });

  it("removes and re-adds a plinth with valid height and without stale part edits", () => {
    const spec = {
      ...manualSpec,
      part_overrides: { "plinth-front": { width_mm: 1_000 }, top: { depth_mm: 330 } },
    };
    let next = spec as DesignSpec;
    for (let iteration = 0; iteration < 2; iteration += 1) {
      next = remove(next, "plinth");
      expect(next.plinth).toBe(false);
      expect(next.plinth_height_mm).toBe(0);
      expect(next.part_overrides).toEqual({ top: { depth_mm: 330 } });
      expect(resolveDesign(next).parts.some(({ kind }) => kind === "plinth")).toBe(false);
      expect(() => resolveSemanticRemoval(next, "plinth")).toThrow(/ingen sockel/);
      next = insert(next, "plinth");
      expect(next.plinth_height_mm).toBe(DEFAULT_DESIGN_SPEC.plinth_height_mm);
      expect(resolveDesign(next).rule_evaluations.find(({ rule_id }) => rule_id === "GEO-001")?.status).toBe("PASS");
    }
  });

  it("removes the whole base row and preserves a compatible pre-existing topology baseline", () => {
    const withBase = insert({ ...manualSpec, divider_count: 2 }, "base_cabinet");
    const customized = remove(withBase, "shelf_row", "shelf-3-bay-1");
    const originalBaseline = customized.topology_baseline!;
    let next = {
      ...customized,
      part_overrides: { "base-bottom-1": { depth_mm: 300 }, "side-left": { depth_mm: 330 } },
      removed_part_ids: ["cabinet-front-2"],
    };
    for (let iteration = 0; iteration < 2; iteration += 1) {
      next = remove(next, "base_cabinet") as typeof next;
      expect(next).toMatchObject({ furniture_type: "bookcase", base_cabinet_count: 0, base_cabinet_height_mm: 0, base_cabinet_depth_mm: 0 });
      expect(next.topology_baseline).toEqual({ ...originalBaseline, base_cabinet_count: 0 });
      expect(() => parseLocalDesignSpec(restorePartCustomizations(next))).not.toThrow();
      expect(next.part_overrides).toEqual({ "side-left": { depth_mm: 330 } });
      expect(next.removed_part_ids).toEqual([]);
      expect(resolveDesign(next).parts.some(({ kind }) => kind === "cabinet_front" || kind === "base_bottom")).toBe(false);
      expect(() => resolveSemanticRemoval(next, "base_cabinet")).toThrow(/ingen underskåpsrad/);
      next = insert(next, "base_cabinet") as typeof next;
      expect(next.base_cabinet_count).toBe(3);
      expect(next.topology_baseline?.shelf_count).toBe(originalBaseline.shelf_count);
      expect(() => parseLocalDesignSpec(restorePartCustomizations(next))).not.toThrow();
    }
  });

  it.each(["divider", "shelf_row"] as const)("can re-add %s beyond a retained removal baseline without locking further editing", (kind) => {
    const spec = { ...manualSpec, divider_count: 1, shelf_count: 1 };
    const removed = remove(spec, kind);
    const once = insert(removed, kind);
    const twice = insert(once, kind);
    expect(kind === "divider" ? twice.divider_count : twice.shelf_count).toBe(2);
    expect(kind === "divider" ? twice.topology_baseline?.divider_count : twice.topology_baseline?.shelf_count).toBe(2);
    expect(() => parseLocalDesignSpec(restorePartCustomizations(twice))).not.toThrow();
    const again = insert(remove(twice, kind), kind);
    expect(kind === "divider" ? again.divider_count : again.shelf_count).toBe(2);
  });
});

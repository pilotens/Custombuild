import { describe, expect, it } from "vitest";
import { DEFAULT_DESIGN_SPEC, type DesignSpec } from "./design-types";
import { resolveDesign } from "./design-engine";
import { resolveSemanticRemoval } from "./semantic-design";
import { DesignChangeRejected, planWorkspaceDesignTransaction } from "./workspace-design-planner";
import { parseLocalDesignSpec } from "./workspace-design-envelope";
import { FURNITURE_TEMPLATES } from "./furniture-templates";

const bookcase = (patch: Partial<DesignSpec> = {}): DesignSpec => ({
  ...DEFAULT_DESIGN_SPEC, reinforcement_mode: "manual", divider_count: 2, ...patch,
});
const library = bookcase({
  furniture_type: "wall_library", width_mm: 2400, height_mm: 2400, depth_mm: 340,
  divider_count: 3, base_cabinet_count: 4, base_cabinet_height_mm: 680, base_cabinet_depth_mm: 340,
});

function expectFeasible(spec: DesignSpec) {
  const design = resolveDesign(spec);
  expect(design.rule_evaluations.filter((rule) => rule.status === "BLOCK")).toEqual([]);
  expect(design.rule_evaluations.find((rule) => rule.rule_id === "STR-DEF-001")?.status).toBe("PASS");
  expect(design.rule_evaluations.find((rule) => rule.rule_id === "STAB-RACK-001")?.status).toBe("PASS");
  expect(design.nesting.overflow_part_ids).toEqual([]);
  expect(spec.wall_anchor_verified).toBe(false);
}

describe("admission of workspace design changes", () => {
  it("plans enough sheets for the reported library instead of saving a material BLOCK", () => {
    const before = structuredClone(library);
    const result = planWorkspaceDesignTransaction(library, library);
    expect(result.normalizedSpec).toMatchObject({ stock_count: 6, back_stock_count: 2, width_mm: 2400 });
    expectFeasible(result.normalizedSpec);
    expect(result.changeDiff).toContainEqual(expect.objectContaining({ field: "stock_count", before: 4, after: 6 }));
    expect(result.resolvedDesign.source).toBe("local");
    expect(result.normalizedSpec.workshop_context).toBeUndefined();
    expect(result.resolvedDesign.rule_evaluations.find((rule) => rule.rule_id === "CB-HARDWARE-001")?.status).toBe("WARNING");
    expect(library).toEqual(before);
  });

  it("adds supports before committing a wider manually arranged bookcase, without lowering its load", () => {
    const source = bookcase({ width_mm: 600, divider_count: 0 });
    const result = planWorkspaceDesignTransaction(source, { ...source, width_mm: 2200, load_per_shelf_kg: 80 });
    expect(result.normalizedSpec.divider_count).toBeGreaterThan(0);
    expect(result.normalizedSpec).toMatchObject({ width_mm: 2200, load_per_shelf_kg: 80 });
    expectFeasible(result.normalizedSpec);
  });

  it("rejects removing the last necessary support and leaves the source untouched", () => {
    const source = planWorkspaceDesignTransaction(DEFAULT_DESIGN_SPEC, DEFAULT_DESIGN_SPEC).normalizedSpec;
    const before = structuredClone(source);
    expect(source.divider_count).toBeGreaterThan(0);
    const removal = resolveSemanticRemoval(source, "divider");
    expect(() => planWorkspaceDesignTransaction(source, removal.spec)).toThrow(/stödet behålls/);
    expect(source).toEqual(before);
  });

  it("still permits optional dividers and shelf rows to be removed", () => {
    const source = bookcase({ width_mm: 1200, divider_count: 3, load_per_shelf_kg: 10 });
    for (const kind of ["divider", "shelf_row"] as const) {
      const candidate = resolveSemanticRemoval(source, kind).spec;
      const result = planWorkspaceDesignTransaction(source, candidate);
      expect(result.normalizedSpec[kind === "divider" ? "divider_count" : "shelf_count"])
        .toBe(source[kind === "divider" ? "divider_count" : "shelf_count"] - 1);
      expectFeasible(result.normalizedSpec);
    }
  });

  it("prevents removing necessary bracing or switching to unsupported adjustable shelves", () => {
    const source = bookcase();
    expect(() => planWorkspaceDesignTransaction(source, { ...source, back_panel: false })).toThrow(/Bakstycket behövs/);
    expect(() => planWorkspaceDesignTransaction(source, { ...source, fixed_shelves: false })).toThrow(/hyllbärarsystem/);
  });

  it("rejects 100 metres, oversize boards and too many shelves without changing the last valid design", () => {
    const source = bookcase();
    const before = structuredClone(source);
    for (const patch of [{ width_mm: 100000 }, { width_mm: 6000 }, { shelf_count: 40, height_mm: 500 }]) {
      expect(() => planWorkspaceDesignTransaction(source, { ...source, ...patch })).toThrow();
      expect(source).toEqual(before);
    }
  });

  it("rejects unsupported free part changes instead of deleting them or approving broken topology", () => {
    const source = bookcase();
    expect(() => planWorkspaceDesignTransaction(source, { ...source, removed_part_ids: ["side-left"] })).toThrow(DesignChangeRejected);
    expect(() => planWorkspaceDesignTransaction(source, { ...source, part_overrides: { "side-left": { depth_mm: 250 } } })).toThrow(DesignChangeRejected);
  });

  it("does not replace custom bay placement with a different layout when a divider is dragged too far", () => {
    const source = bookcase({ symmetry_locked: false, divider_count: 1 });
    expect(() => planWorkspaceDesignTransaction(source, { ...source, bay_width_ratios: [0.1, 0.9] })).toThrow(DesignChangeRejected);
  });

  it("respects explicitly selected quantities instead of inventing available stock", () => {
    expect(() => planWorkspaceDesignTransaction(library, { ...library, stock_count: 1 })).toThrow(DesignChangeRejected);
  });

  it("keeps exact workshop stock profiles immutable and refuses insufficient quantities", () => {
    const source: DesignSpec = {
      ...library,
      workshop_context: { stock_profiles: [{
        role: "carcass", declaration_authority: "CLIENT_DECLARED", supplier_profile_id: "sheet",
        supplier_profile_version: "v1", material_id: "birch-plywood", material_version: "screening-2026.1",
        sheet_width_um: 2440000, sheet_height_um: 1220000, thickness_um: 17800,
        sheet_count: 4, trim_margin_um: 10000, kerf_um: 6000, grain_direction: "X",
        allow_rotation: false, defect_zones: [], fixture_keep_out_zones: [],
      }, {
        role: "back", declaration_authority: "CLIENT_DECLARED", supplier_profile_id: "sheet-back",
        supplier_profile_version: "v1", material_id: "birch-plywood-6", material_version: "screening-2026.1",
        sheet_width_um: 2440000, sheet_height_um: 1220000, thickness_um: 6000,
        sheet_count: 2, trim_margin_um: 10000, kerf_um: 6000, grain_direction: "X",
        allow_rotation: false, defect_zones: [], fixture_keep_out_zones: [],
      }] },
    };
    const before = structuredClone(source);
    expect(parseLocalDesignSpec(source).workshop_context).toEqual(source.workshop_context);
    expect(() => planWorkspaceDesignTransaction(source, source)).toThrow(DesignChangeRejected);
    expect(source).toEqual(before);
  });

  it.each(FURNITURE_TEMPLATES.filter((template) => !template.designUnavailableReason))(
    "opens the offered $name start model without blocking design errors",
    (template) => {
      const candidate = { ...DEFAULT_DESIGN_SPEC, ...template.patch };
      const result = planWorkspaceDesignTransaction(DEFAULT_DESIGN_SPEC, candidate);
      expectFeasible(result.normalizedSpec);
      expect(result.normalizedSpec.width_mm).toBe(template.patch.width_mm);
    },
  );

  it("is deterministic and idempotent after automatic planning", () => {
    const first = planWorkspaceDesignTransaction(library, library);
    expect(planWorkspaceDesignTransaction(library, library)).toEqual(first);
    const again = planWorkspaceDesignTransaction(first.normalizedSpec, first.normalizedSpec);
    expect(again.changedFields).toEqual([]);
    expect(again.normalizedSpec).toEqual(first.normalizedSpec);
  });

  it.each([600, 1200, 2200, 2400])("admits only locally feasible designs at width %i across loads and shelf counts", (width_mm) => {
    for (const load_per_shelf_kg of [0, 32, 100, 500]) {
      for (const shelf_count of [0, 3, 8]) {
        const source = bookcase({ width_mm: 600, divider_count: 0 });
        try {
          const result = planWorkspaceDesignTransaction(source, { ...source, width_mm, load_per_shelf_kg, shelf_count });
          expectFeasible(result.normalizedSpec);
          expect(result.normalizedSpec).toMatchObject({ width_mm, load_per_shelf_kg, shelf_count });
        } catch (error) {
          if (!(error instanceof DesignChangeRejected)) throw error;
        }
      }
    }
  });
});

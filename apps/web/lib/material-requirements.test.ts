import { describe, expect, it } from "vitest";
import { resolveDesign } from "./design-engine";
import { DEFAULT_DESIGN_SPEC, type DesignSpec } from "./design-types";
import { estimateMaterialRequirements } from "./material-requirements";

const library: DesignSpec = {
  ...DEFAULT_DESIGN_SPEC,
  furniture_type: "wall_library",
  width_mm: 2_400,
  height_mm: 2_400,
  depth_mm: 340,
  divider_count: 3,
  base_cabinet_count: 4,
  base_cabinet_height_mm: 680,
  base_cabinet_depth_mm: 340,
};

describe("read-only material requirements", () => {
  it("automatically estimates the screenshot furniture without making stock available", () => {
    const design = resolveDesign(library);
    const before = JSON.stringify(design);
    expect(estimateMaterialRequirements(library, design.parts)).toEqual({
      source: "local-estimate",
      complete: true,
      oversizedPartIds: [],
      rows: [
        { role: "carcass", materialId: library.material_id, thicknessMm: 17.8, widthMm: 2440, heightMm: 1220, declaredSheets: 4, estimatedSheets: 6, additionalSheets: 2 },
        { role: "back", materialId: library.back_material_id, thicknessMm: 6, widthMm: 2440, heightMm: 1220, declaredSheets: 2, estimatedSheets: 2, additionalSheets: 0 },
      ],
    });
    expect(JSON.stringify(design)).toBe(before);
    expect(design.rule_evaluations.find((rule) => rule.rule_id === "DFM-STOCK-001")?.status).toBe("BLOCK");
  });

  it("keeps carcass and back requirements independent even if carcass stock is abundant", () => {
    const spec = { ...library, stock_count: 100, back_stock_count: 1 };
    const plan = estimateMaterialRequirements(spec, resolveDesign(spec).parts);
    expect(plan.rows.map((row) => [row.role, row.estimatedSheets, row.additionalSheets]))
      .toEqual([["carcass", 6, 0], ["back", 2, 1]]);
  });

  it("does not estimate back sheets for an open-backed design", () => {
    const spec = { ...library, back_panel: false };
    const plan = estimateMaterialRequirements(spec, resolveDesign(spec).parts);
    expect(plan.rows.map((row) => row.role)).toEqual(["carcass"]);
    expect(plan.complete).toBe(true);
  });

  it("does not claim a complete quantity when a part exceeds the selected sheet format", () => {
    const spec = { ...library, width_mm: 4_200 };
    const plan = estimateMaterialRequirements(spec, resolveDesign(spec).parts);
    expect(plan.complete).toBe(false);
    expect(plan.oversizedPartIds).toContain("top");
    expect(plan.oversizedPartIds).toContain("plinth-front");
  });

  it.each([0, -1, Number.NaN, Number.POSITIVE_INFINITY])("never marks invalid sheet dimensions %s complete", (width) => {
    const parts = resolveDesign(library).parts;
    const plan = estimateMaterialRequirements({ ...library, stock_width_mm: width }, parts);
    expect(plan.complete).toBe(false);
    expect(plan.rows.find((row) => row.role === "carcass")?.estimatedSheets).toBe(0);
  });

  it("rejects invalid part dimensions, material binding, counts and empty geometry", () => {
    const parts = resolveDesign(library).parts;
    expect(estimateMaterialRequirements(library, [{ ...parts[0]!, width_mm: Number.NaN }]).complete).toBe(false);
    expect(estimateMaterialRequirements(library, [{ ...parts[0]!, material_id: "other-material" }]).complete).toBe(false);
    expect(estimateMaterialRequirements({ ...library, stock_count: Number.POSITIVE_INFINITY }, parts).complete).toBe(false);
    expect(estimateMaterialRequirements(library, []).complete).toBe(false);
  });

  it("leaves exact workshop quantities, grain and fixture evidence untouched", () => {
    const spec: DesignSpec = {
      ...library,
      back_panel: false,
      workshop_context: {
        stock_profiles: [{
          role: "carcass", declaration_authority: "CLIENT_DECLARED",
          supplier_profile_id: "supplier-birch-18", supplier_profile_version: "batch-2026-09",
          material_id: "birch-plywood", material_version: "screening-2026.1",
          sheet_width_um: 2_440_000, sheet_height_um: 1_220_000, thickness_um: 17_800,
          sheet_count: 4, trim_margin_um: 10_000, kerf_um: 6_000, grain_direction: "X",
          allow_rotation: false, defect_zones: [],
          fixture_keep_out_zones: [{ x_um: 0, y_um: 0, width_um: 40_000, height_um: 1_220_000 }],
        }],
      },
    };
    const before = JSON.stringify(spec);
    const design = resolveDesign(spec);
    const plan = estimateMaterialRequirements(spec, design.parts);
    expect(plan.source).toBe("local-estimate");
    expect(plan.rows[0]?.estimatedSheets).toBeGreaterThan(spec.stock_count);
    expect(JSON.stringify(spec)).toBe(before);
    expect(spec.workshop_context?.stock_profiles[0]?.sheet_count).toBe(4);
    expect(design.status).toBe("BLOCK");
  });
});

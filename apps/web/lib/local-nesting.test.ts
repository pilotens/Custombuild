import { describe, expect, it } from "vitest";
import { partsDoNotOverlap, resolveDesign } from "./design-engine";
import { DEFAULT_DESIGN_SPEC, type DesignSpec, type ResolvedPart } from "./design-types";
import { resolveLocalNesting } from "./local-nesting";

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

function part(id: string, width: number, depth: number): ResolvedPart {
  return {
    part_id: id, name: id, kind: "shelf", width_mm: width, depth_mm: depth,
    thickness_mm: 17.8, position_mm: { x: 0, y: 0, z: 0 }, orientation: "XY",
    color: "#ffffff", material_id: DEFAULT_DESIGN_SPEC.material_id, weight_kg: 1, features: [],
  };
}

describe("bounded local nesting", () => {
  it("never places the screenshot's plinth on phantom stock after capacity is exhausted", () => {
    const design = resolveDesign(library);
    const carcass = design.nesting.placements.filter((placement) => placement.stock_role === "carcass");
    const back = design.nesting.placements.filter((placement) => placement.stock_role === "back");
    expect(carcass.every((placement) => placement.sheet <= library.stock_count)).toBe(true);
    expect(new Set(carcass.map((placement) => placement.sheet))).toEqual(new Set([1, 2, 3, 4]));
    expect(back.every((placement) => placement.sheet > 4 && placement.sheet <= 6)).toBe(true);
    expect(design.nesting.sheet_count).toBe(6);
    expect(design.nesting.overflow_part_ids).toHaveLength(23);
    expect(design.nesting.placements.find((placement) => placement.part_id === "plinth-front")?.sheet).toBe(4);
    expect(partsDoNotOverlap(design.nesting)).toBe(true);
    const allIds = [
      ...design.nesting.placements.map((placement) => placement.part_id),
      ...design.nesting.overflow_part_ids,
    ];
    expect(allIds).toHaveLength(design.parts.length);
    expect(new Set(allIds)).toEqual(new Set(design.parts.map((item) => item.part_id)));
    expect(design.rule_evaluations.find((rule) => rule.rule_id === "DFM-STOCK-001")?.status).toBe("BLOCK");
  });

  it("still fits a smaller later part on the last real sheet after a larger part overflows", () => {
    const result = resolveLocalNesting(
      { ...DEFAULT_DESIGN_SPEC, stock_width_mm: 120, stock_height_mm: 120, stock_count: 1 },
      [part("large", 100, 70), part("overflow", 100, 40), part("small", 100, 20)],
    );
    expect(result.overflow_part_ids).toEqual(["overflow"]);
    expect(result.placements.map((placement) => [placement.part_id, placement.sheet]))
      .toEqual([["large", 1], ["small", 1]]);
    expect(partsDoNotOverlap(result)).toBe(true);
  });

  it("uses bounded virtual capacity without changing the input or declared capacity", () => {
    const design = resolveDesign(library);
    const before = JSON.stringify({ spec: library, parts: design.parts });
    const result = resolveLocalNesting(library, design.parts, { carcass: design.parts.length, back: design.parts.length });
    expect(result.overflow_part_ids).toEqual([]);
    expect(result.sheet_count).toBe(8);
    expect(new Set(result.placements.filter((placement) => placement.stock_role === "carcass").map((placement) => placement.sheet)).size).toBe(6);
    expect(JSON.stringify({ spec: library, parts: design.parts })).toBe(before);
  });

  it.each([0, 1, 2, 3, 4, 5, 6, 7, 8])("partitions every part within capacity %s without overlaps or phantom sheets", (count) => {
    const parts = resolveDesign(library).parts;
    const result = resolveLocalNesting(library, parts, { carcass: count, back: 1 });
    const carcass = result.placements.filter((placement) => placement.stock_role === "carcass");
    const carcassSheetCount = new Set(carcass.map((placement) => placement.sheet)).size;
    expect(carcass.every((placement) => placement.sheet >= 1 && placement.sheet <= count)).toBe(true);
    expect(result.placements.filter((placement) => placement.stock_role === "back")
      .every((placement) => placement.sheet === carcassSheetCount + 1)).toBe(true);
    const ids = [...result.placements.map((placement) => placement.part_id), ...result.overflow_part_ids];
    expect(ids).toHaveLength(parts.length);
    expect(new Set(ids).size).toBe(parts.length);
    expect(new Set(ids)).toEqual(new Set(parts.map((item) => item.part_id)));
    expect(partsDoNotOverlap(result)).toBe(true);
  });

  it.each([0, -1, 1.5, Number.NaN, Number.POSITIVE_INFINITY])("fails closed for an unusable sheet count %s", (count) => {
    const result = resolveLocalNesting(DEFAULT_DESIGN_SPEC, [part("one", 100, 100)], { carcass: count, back: 0 });
    expect(result).toEqual({ placements: [], overflow_part_ids: ["one"], sheet_count: 0, utilization_percent: 0 });
  });

  it.each([0, -1, Number.NaN, Number.POSITIVE_INFINITY, 1e30])("rejects invalid sheet dimensions %s", (width) => {
    const result = resolveLocalNesting({ ...DEFAULT_DESIGN_SPEC, stock_width_mm: width }, [part("one", 100, 100)]);
    expect(result.placements).toEqual([]);
    expect(result.overflow_part_ids).toEqual(["one"]);
    expect(result.utilization_percent).toBe(0);
  });

  it("rejects non-finite parts without losing valid parts or corrupting metrics", () => {
    const result = resolveLocalNesting(DEFAULT_DESIGN_SPEC, [part("invalid", Number.NaN, 100), part("valid", 100, 100)]);
    expect(result.overflow_part_ids).toEqual(["invalid"]);
    expect(result.placements.map((placement) => placement.part_id)).toEqual(["valid"]);
    expect(Number.isFinite(result.utilization_percent)).toBe(true);
  });

  it("keeps deterministic sorting and three-decimal placement dimensions", () => {
    const input = [part("b", 100.1234, 40), part("a", 100.1234, 40)];
    const result = resolveLocalNesting(DEFAULT_DESIGN_SPEC, input);
    expect(result).toEqual(resolveLocalNesting(DEFAULT_DESIGN_SPEC, [...input].reverse()));
    expect(result.placements[0]).toMatchObject({ part_id: "a", x_mm: 8, y_mm: 8, width_mm: 100.123 });
  });
});

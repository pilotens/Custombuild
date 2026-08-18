import { describe, expect, it } from "vitest";
import { DEFAULT_DESIGN_SPEC } from "./design-types";
import {
  createSemanticSnapPreview,
  defaultSemanticDropRequest,
  resolveSemanticDrop,
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

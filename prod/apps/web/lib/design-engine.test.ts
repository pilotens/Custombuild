import { describe, expect, it } from "vitest";
import { applySuggestion, partsDoNotOverlap, resolveDesign } from "./design-engine";
import { DEFAULT_DESIGN_SPEC } from "./design-types";

describe("deterministic bookcase preview", () => {
  it("returns identical canonical output for identical input", () => {
    const first = resolveDesign({ ...DEFAULT_DESIGN_SPEC });
    const second = resolveDesign({ ...DEFAULT_DESIGN_SPEC });

    expect(second.design_hash).toBe(first.design_hash);
    expect(second.parts).toEqual(first.parts);
    expect(second.bom).toEqual(first.bom);
    expect(second.operations).toEqual(first.operations);
    expect(partsDoNotOverlap(first.nesting)).toBe(true);
  });

  it("adds a proposed divider to model, BOM, nesting and operations", () => {
    const initial = resolveDesign(DEFAULT_DESIGN_SPEC);
    const shelfRule = initial.rule_evaluations.find((rule) => rule.rule_id === "STR-DEF-001");

    expect(shelfRule?.status).not.toBe("PASS");
    expect(shelfRule?.suggestion?.action).toBe("set_divider_count");
    if (!shelfRule) throw new Error("Shelf rule missing");

    const applied = applySuggestion(DEFAULT_DESIGN_SPEC, shelfRule);
    const reinforced = resolveDesign(applied.spec, applied.diff);

    expect(applied.spec.divider_count).toBeGreaterThan(0);
    expect(applied.spec.reinforcement_mode).toBe("auto");
    expect(reinforced.parts.some((part) => part.kind === "divider")).toBe(true);
    expect(reinforced.bom.some((line) => line.part_ids.some((partId) => partId.startsWith("divider-")))).toBe(true);
    expect(reinforced.nesting.placements.some((placement) => placement.part_id.startsWith("divider-"))).toBe(true);
    expect(reinforced.operations.some((operation) => operation.part_id.startsWith("divider-"))).toBe(true);
    expect(reinforced.rule_evaluations.find((rule) => rule.rule_id === "STR-DEF-001")?.status).toBe("PASS");
  });

  it("blocks a machine profile whose work area cannot contain the side panels", () => {
    const result = resolveDesign({ ...DEFAULT_DESIGN_SPEC, machine_profile_id: "shapeoko-hdmi-v1" });
    const machineRule = result.rule_evaluations.find((rule) => rule.rule_id === "DFM-MACHINE-001");

    expect(machineRule?.status).toBe("BLOCK");
    expect(machineRule?.affected_part_ids).toContain("side-left");
    expect(result.status).toBe("BLOCK");
  });

  it("changes the preview hash when any production-driving input changes", () => {
    const initial = resolveDesign(DEFAULT_DESIGN_SPEC);
    const changed = resolveDesign({ ...DEFAULT_DESIGN_SPEC, measured_thickness_mm: 17.7 });

    expect(changed.design_hash).not.toBe(initial.design_hash);
    expect(changed.parts.find((part) => part.part_id === "side-left")?.thickness_mm).toBe(17.7);
  });

  it("reports invalid dimensions as blocking instead of inventing valid output", () => {
    const result = resolveDesign({ ...DEFAULT_DESIGN_SPEC, width_mm: 20 });
    const geometryRule = result.rule_evaluations.find((rule) => rule.rule_id === "GEO-001");

    expect(geometryRule?.status).toBe("BLOCK");
    expect(geometryRule?.summary).toContain("bredden");
  });
});

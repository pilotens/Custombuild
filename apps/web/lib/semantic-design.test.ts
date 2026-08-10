import { describe, expect, it } from "vitest";
import { DEFAULT_DESIGN_SPEC } from "./design-types";
import { createSemanticSnapPreview, resolveSemanticDrop } from "./semantic-design";

describe("semantic furniture editing", () => {
  it("snaps a shelf drop to a bay but compiles only a furniture intent", () => {
    const spec = { ...DEFAULT_DESIGN_SPEC, divider_count: 1 };
    const preview = createSemanticSnapPreview(spec, {
      kind: "shelf_row",
      normalizedX: 0.8,
      normalizedY: 0.3,
    });

    expect(preview.targetId).toBe("bookcase:shelf:bay:2");
    const outcome = resolveSemanticDrop(spec, {
      kind: "shelf_row",
      normalizedX: 0.8,
      normalizedY: 0.3,
    });
    expect(outcome.spec.shelf_count).toBe(spec.shelf_count + 1);
    expect(outcome.warning).toContain("inte CNC-koordinat");
  });

  it("adds a divider and returns the design to manual review", () => {
    const spec = { ...DEFAULT_DESIGN_SPEC, reinforcement_mode: "auto" as const };
    const outcome = resolveSemanticDrop(spec, {
      kind: "divider",
      normalizedX: 0.4,
      normalizedY: 0.5,
    });

    expect(outcome.spec.divider_count).toBe(1);
    expect(outcome.spec.reinforcement_mode).toBe("manual");
    expect(outcome.diff.map((item) => item.field)).toContain("reinforcement_mode");
  });

  it("fails instead of silently duplicating unique components", () => {
    expect(() => resolveSemanticDrop(DEFAULT_DESIGN_SPEC, {
      kind: "back_panel",
      normalizedX: 0.5,
      normalizedY: 0.5,
    })).toThrow("redan ett bakstycke");
  });
});

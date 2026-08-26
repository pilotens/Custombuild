import { describe, expect, it } from "vitest";
import { furnitureTemplate } from "./furniture-templates";
import {
  DEFAULT_PLANNING_BRIEF,
  isFurniturePlanningBrief,
  PLANNING_DIMENSION_LIMITS,
  recommendedTemplateId,
  templateWithPlanningBrief,
} from "./furniture-planning";

describe("furniture planning", () => {
  it("recommends from actual use and placement", () => {
    expect(recommendedTemplateId({ ...DEFAULT_PLANNING_BRIEF, primaryUse: "concealed" })).toBe("wall-library");
    expect(recommendedTemplateId({ ...DEFAULT_PLANNING_BRIEF, primaryUse: "books" })).toBe("shelving");
    expect(recommendedTemplateId({ ...DEFAULT_PLANNING_BRIEF, space: "freestanding" })).toBe("room-divider");
  });

  it("applies confirmed dimensions but keeps the template capability", () => {
    const result = templateWithPlanningBrief(furnitureTemplate("wall-library"), {
      ...DEFAULT_PLANNING_BRIEF,
      width_mm: 3_650,
      height_mm: 2_650,
      depth_mm: 390,
      priority: "capacity",
    });
    expect(result.productionLevel).toBe("concept");
    expect(result.patch).toMatchObject({ width_mm: 3650, height_mm: 2650, depth_mm: 390, load_per_shelf_kg: 40 });
    expect(result.patch.base_cabinet_depth_mm).toBe(390);
  });

  it("does not overwrite inherited construction values with undefined", () => {
    const result = templateWithPlanningBrief(furnitureTemplate("shelving"), DEFAULT_PLANNING_BRIEF);

    expect(result.patch).not.toHaveProperty("load_per_shelf_kg");
    expect(result.patch).not.toHaveProperty("bay_sizing_mode");
    expect(result.patch).not.toHaveProperty("bay_width_ratios");
    expect(result.patch).not.toHaveProperty("shelf_height_ratios");
  });

  it("opens scratch as a genuinely empty frame instead of auto-filling narrow bays", () => {
    const result = templateWithPlanningBrief(furnitureTemplate("shelving"), {
      ...DEFAULT_PLANNING_BRIEF,
      startMode: "scratch",
      selectedTemplateId: "shelving",
    });

    expect(result.name).toBe("Egen stomme");
    expect(result.patch).toMatchObject({
      shelf_count: 0,
      divider_count: 0,
      bay_sizing_mode: "count",
      bay_width_ratios: [],
      shelf_height_ratios: [],
    });
  });

  it("validates persisted briefs defensively", () => {
    expect(isFurniturePlanningBrief(DEFAULT_PLANNING_BRIEF)).toBe(true);
    expect(isFurniturePlanningBrief({
      ...DEFAULT_PLANNING_BRIEF,
      dimensionsConfirmed: true,
    })).toBe(true);
    expect(recommendedTemplateId(DEFAULT_PLANNING_BRIEF)).toBe("shelving");
    expect(isFurniturePlanningBrief({ ...DEFAULT_PLANNING_BRIEF, width_mm: 30 })).toBe(false);
    expect(isFurniturePlanningBrief({ ...DEFAULT_PLANNING_BRIEF, startMode: "magic" })).toBe(false);
    expect(isFurniturePlanningBrief({
      ...DEFAULT_PLANNING_BRIEF,
      dimensionsConfirmed: "yes",
    })).toBe(false);
  });

  it("uses the complete canonical B1 dimension envelope", () => {
    expect(PLANNING_DIMENSION_LIMITS).toMatchObject({
      width_mm: { minimum: 250, maximum: 6_000 },
      height_mm: { minimum: 300, maximum: 4_000 },
      depth_mm: { minimum: 100, maximum: 1_200 },
    });
    expect(isFurniturePlanningBrief({
      ...DEFAULT_PLANNING_BRIEF,
      width_mm: 250,
      height_mm: 300,
      depth_mm: 100,
    })).toBe(true);
    expect(isFurniturePlanningBrief({
      ...DEFAULT_PLANNING_BRIEF,
      width_mm: 6_000,
      height_mm: 4_000,
      depth_mm: 1_200,
    })).toBe(true);
  });

  it("keeps family-specific base geometry canonical after planning", () => {
    const wallLibrary = templateWithPlanningBrief(furnitureTemplate("cupboard"), {
      ...DEFAULT_PLANNING_BRIEF,
      height_mm: 1_000,
      depth_mm: 1_200,
    });
    expect(wallLibrary.patch).toMatchObject({
      furniture_type: "wall_library",
      height_mm: 1_000,
      depth_mm: 1_200,
      base_cabinet_height_mm: 782,
      base_cabinet_depth_mm: 1_200,
      base_cabinet_count: 3,
    });

    const bookcase = templateWithPlanningBrief(furnitureTemplate("shelving"), {
      ...DEFAULT_PLANNING_BRIEF,
      depth_mm: 1_200,
    });
    expect(bookcase.patch).toMatchObject({
      furniture_type: "bookcase",
      base_cabinet_height_mm: 0,
      base_cabinet_depth_mm: 0,
      base_cabinet_count: 0,
    });
  });

  it("refuses a wall-library height that cannot contain a valid base cabinet", () => {
    expect(() => templateWithPlanningBrief(furnitureTemplate("wall-library"), {
      ...DEFAULT_PLANNING_BRIEF,
      height_mm: 300,
    })).toThrow(/kräver mer totalhöjd/);
  });
});

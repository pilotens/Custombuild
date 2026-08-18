import { describe, expect, it } from "vitest";
import { resolveDesign } from "./design-engine";
import { DEFAULT_DESIGN_SPEC } from "./design-types";
import {
  analyzeReferencePixels,
  draftFromReferenceAnalysis,
  REFERENCE_IMAGE_INFERENCE_LIMITS,
  referenceResult,
  type PixelImage,
  type ReferenceAssetInspection,
} from "./reference-image";

const REFERENCE_ASSET: ReferenceAssetInspection = {
  import_id: "11111111-1111-4111-8111-111111111111",
  project_id: "22222222-2222-4222-8222-222222222222",
  image_sha256: "a".repeat(64),
  media_type: "image/jpeg",
  size_bytes: 1_024,
};

function syntheticLibrary(): PixelImage {
  const width = 360;
  const height = 260;
  const data = new Uint8ClampedArray(width * height * 4);
  const paint = (left: number, top: number, right: number, bottom: number, color: [number, number, number]) => {
    for (let y = top; y < bottom; y += 1) {
      for (let x = left; x < right; x += 1) {
        const offset = (y * width + x) * 4;
        data[offset] = color[0];
        data[offset + 1] = color[1];
        data[offset + 2] = color[2];
        data[offset + 3] = 255;
      }
    }
  };

  paint(0, 0, width, height, [246, 246, 242]);
  paint(30, 20, 330, 235, [194, 162, 111]);
  paint(30, 174, 330, 235, [111, 54, 28]);
  for (const x of [30, 90, 150, 210, 270, 330]) paint(x - 2, 20, x + 2, 235, [70, 45, 30]);
  for (const y of [20, 50, 80, 110, 140, 174, 235]) paint(30, y - 2, 330, y + 2, [74, 48, 30]);
  return { width, height, data };
}

describe("reference image analysis", () => {
  it("detects a cabinet base and repeated furniture grid from real pixels", () => {
    const analysis = analyzeReferencePixels(syntheticLibrary());

    expect(analysis.hasBaseCabinets).toBe(true);
    expect(analysis.boundaryDetected).toBe(true);
    expect(analysis.baseSplitRatio).toBeGreaterThan(0.6);
    expect(analysis.verticalGuides.length).toBeGreaterThanOrEqual(4);
    expect(analysis.horizontalGuides.length).toBeGreaterThanOrEqual(4);
    expect(analysis.confidence).toBeGreaterThan(0.6);
  });

  it("maps image proportions to an editable, production-blocked concept", () => {
    const analysis = analyzeReferencePixels(syntheticLibrary());
    const draft = draftFromReferenceAnalysis(analysis);
    const result = referenceResult(analysis, draft, "min/referens.jpg", REFERENCE_ASSET);

    expect(result.patch.furniture_type).toBe("wall_library");
    expect(result.patch.bay_width_ratios).toHaveLength((result.patch.divider_count as number) + 1);
    expect(result.patch.shelf_height_ratios).toHaveLength(result.patch.shelf_count as number);
    expect(result.patch.reinforcement_mode).toBe("manual");
    expect(result.metadata.source).toBe("reference_image");
    expect(result.metadata.file_name).toBe("min_referens.jpg");
    expect(result.metadata.image_sha256).toBe("a".repeat(64));
    expect(result.metadata.verification_status).toBe("concept");
    expect(result.metadata.confirmed_inputs).toEqual({
      dimensions_measured: false,
      layout_confirmed: false,
      material_confirmed: false,
      construction_assumptions_confirmed: false,
    });
  });

  it("keeps conservative inference caps separate from the complete manual envelope", () => {
    expect(REFERENCE_IMAGE_INFERENCE_LIMITS).toEqual({ shelfCount: 20, dividerCount: 12 });
    const analysis = {
      ...analyzeReferencePixels(syntheticLibrary()),
      horizontalGuides: Array.from({ length: 30 }, (_, index) => (index + 1) / 31),
      verticalGuides: Array.from({ length: 20 }, (_, index) => (index + 1) / 21),
    };
    const inferred = draftFromReferenceAnalysis(analysis);
    expect(inferred.shelfCount).toBe(20);
    expect(inferred.dividerCount).toBe(12);

    const fullBookcase = referenceResult(analysis, {
      ...inferred,
      furnitureType: "bookcase",
      widthMm: 6_000,
      heightMm: 4_000,
      depthMm: 1_200,
      shelfCount: 40,
      dividerCount: 16,
      baseCabinetHeightMm: 900,
      baseCabinetDepthMm: 800,
      baseCabinetCount: 17,
      bayWidthRatios: Array.from({ length: 17 }, () => 1 / 17),
      shelfHeightRatios: Array.from({ length: 40 }, (_, index) => (index + 1) / 41),
    }, "full-bookcase.jpg", REFERENCE_ASSET);

    expect(fullBookcase.patch).toMatchObject({
      furniture_type: "bookcase",
      width_mm: 6_000,
      height_mm: 4_000,
      depth_mm: 1_200,
      shelf_count: 40,
      divider_count: 16,
      base_cabinet_height_mm: 0,
      base_cabinet_depth_mm: 0,
      base_cabinet_count: 0,
    });
    expect(fullBookcase.patch.bay_width_ratios).toEqual([]);
    expect(fullBookcase.patch.shelf_height_ratios).toEqual([]);
    expect(resolveDesign({ ...DEFAULT_DESIGN_SPEC, ...fullBookcase.patch }).rule_evaluations
      .find(({ rule_id }) => rule_id === "GEO-001")?.status).toBe("PASS");

    const fullWallLibrary = referenceResult(analysis, {
      ...inferred,
      furnitureType: "wall_library",
      widthMm: 6_000,
      heightMm: 4_000,
      depthMm: 1_200,
      shelfCount: 20,
      dividerCount: 16,
      baseCabinetHeightMm: 2_000,
      baseCabinetDepthMm: 300,
      baseCabinetCount: 17,
      bayWidthRatios: [],
      shelfHeightRatios: [],
    }, "full-wall-library.jpg", REFERENCE_ASSET);
    expect(fullWallLibrary.patch).toMatchObject({
      furniture_type: "wall_library",
      base_cabinet_height_mm: 2_000,
      base_cabinet_depth_mm: 1_200,
      base_cabinet_count: 17,
    });
    expect(resolveDesign({ ...DEFAULT_DESIGN_SPEC, ...fullWallLibrary.patch }).rule_evaluations
      .find(({ rule_id }) => rule_id === "GEO-001")?.status).toBe("PASS");
  });

  it("repairs invalid custom ratios to a valid equal layout fallback", () => {
    const analysis = analyzeReferencePixels(syntheticLibrary());
    const draft = {
      ...draftFromReferenceAnalysis(analysis),
      shelfCount: 2,
      dividerCount: 2,
      bayWidthRatios: [0.01, 0.49, 0.5],
      shelfHeightRatios: [0.8, 0.2],
    };
    const result = referenceResult(analysis, draft, "ratios.jpg", REFERENCE_ASSET);
    expect(result.patch.bay_width_ratios).toEqual([1 / 3, 1 / 3, 1 / 3]);
    expect(result.patch.shelf_height_ratios).toEqual([1 / 3, 2 / 3]);
  });

  it("refuses a wall library that is too short for the strict base clearance", () => {
    const analysis = analyzeReferencePixels(syntheticLibrary());
    const draft = {
      ...draftFromReferenceAnalysis(analysis),
      furnitureType: "wall_library" as const,
      heightMm: 300,
    };
    expect(() => referenceResult(analysis, draft, "short.jpg", REFERENCE_ASSET))
      .toThrow(/kräver mer totalhöjd/);
  });

  it("rejects tiny or incomplete images before generating geometry", () => {
    expect(() => analyzeReferencePixels({ width: 100, height: 100, data: new Uint8ClampedArray(40_000) })).toThrow(/minst 160/);
    expect(() => analyzeReferencePixels({ width: 200, height: 200, data: new Uint8ClampedArray(100) })).toThrow(/ofullständig/);
  });

  it("marks an empty image as unusable instead of inventing a product", () => {
    const data = new Uint8ClampedArray(240 * 200 * 4);
    for (let index = 0; index < data.length; index += 4) {
      data[index] = 245;
      data[index + 1] = 245;
      data[index + 2] = 245;
      data[index + 3] = 255;
    }
    const analysis = analyzeReferencePixels({ width: 240, height: 200, data });
    expect(analysis.boundaryDetected).toBe(false);
    expect(analysis.warnings.join(" ")).toMatch(/Ingen tydlig möbelytterkant/);
  });
});

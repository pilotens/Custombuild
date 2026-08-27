import { fireEvent, render, screen, within } from "@testing-library/react";
import { createElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { resolveDesign } from "@/lib/design-engine";
import { DEFAULT_DESIGN_SPEC, type ResolvedPart } from "@/lib/design-types";
import type { SemanticSnapPreview } from "@/lib/semantic-design";
import {
  BOX_EDGE_SEGMENTS_PER_PART,
  browserSupportsWebGL,
  buildComparisonGhostRenderData,
  buildInstancedPartRenderData,
  buildSortableTransparentMaterialCatalog,
  buildSortableTransparentPartRenderData,
  cameraPositionForView,
  cameraProjectionForView,
  centeredFrontProjectionRect,
  classifyComparisonParts,
  comparisonGhostBufferResourceKey,
  COMPARISON_GHOST_LINE_STYLES,
  commitPendingViewerRender,
  ComparisonLegend,
  DimensionDragOverlay,
  dimensionAfterDrag,
  dimensionAfterNudge,
  exposeViewerModelRoot,
  FrontProjectionFallback,
  initialCameraForView,
  initializeViewerRenderContract,
  horizontalPositionAfterDrag,
  hoveredPartAfterInstanceOut,
  LARGE_SCENE_PART_THRESHOLD,
  PartMoveFeedbackOverlay,
  partForInstancedBatch,
  partDragThresholdReached,
  partMoveFeedback,
  partSupportsHorizontalDrag,
  partSupportsVerticalDrag,
  shouldFitViewCamera,
  shouldUseInstancedPartRendering,
  SnapPreview,
  verticalPositionAfterDrag,
  verticalDragBoundsForPart,
  VIEWER_FRAMELOOP,
  VIEWER_MODEL_ROOT_ATTRIBUTE,
  VIEWER_RENDER_COMMIT_ATTRIBUTE,
  ViewerDemandInvalidator,
  viewerOrbitControlsPerformanceProps,
  viewerPartAppearance,
  viewerPartRenderMode,
  viewerPartTransform,
  viewerMaterialVisual,
} from "./furniture-viewer";

describe("demand-render contract", () => {
  it("keeps a monotonic post-render revision without resetting an existing canvas", () => {
    const canvas = document.createElement("canvas");
    const pending = { current: false };

    expect(VIEWER_FRAMELOOP).toBe("demand");
    initializeViewerRenderContract(canvas);
    expect(canvas).toHaveAttribute(VIEWER_RENDER_COMMIT_ATTRIBUTE, "0");
    expect(commitPendingViewerRender(canvas, pending)).toBe(false);

    pending.current = true;
    expect(commitPendingViewerRender(canvas, pending)).toBe(true);
    expect(canvas).toHaveAttribute(VIEWER_RENDER_COMMIT_ATTRIBUTE, "1");

    initializeViewerRenderContract(canvas);
    pending.current = true;
    expect(commitPendingViewerRender(canvas, pending)).toBe(true);
    expect(canvas).toHaveAttribute(VIEWER_RENDER_COMMIT_ATTRIBUTE, "2");
  });

  it("keeps the active model-root identity stable against stale cleanup", () => {
    const canvas = document.createElement("canvas");
    const releaseFirstRoot = exposeViewerModelRoot(canvas, "model-root-a");
    expect(canvas).toHaveAttribute(VIEWER_MODEL_ROOT_ATTRIBUTE, "model-root-a");

    const releaseReplacementRoot = exposeViewerModelRoot(canvas, "model-root-b");
    releaseFirstRoot();
    expect(canvas).toHaveAttribute(VIEWER_MODEL_ROOT_ATTRIBUTE, "model-root-b");

    releaseReplacementRoot();
    expect(canvas).not.toHaveAttribute(VIEWER_MODEL_ROOT_ATTRIBUTE);
  });

  it("invalidates selection, view, transparency and resolved drag geometry, but not equal size objects", () => {
    const invalidate = vi.fn();
    const parts = resolveDesign(DEFAULT_DESIGN_SPEC).parts;
    const designSize = {
      widthMm: DEFAULT_DESIGN_SPEC.width_mm,
      heightMm: DEFAULT_DESIGN_SPEC.height_mm,
      depthMm: DEFAULT_DESIGN_SPEC.depth_mm,
    };
    const baseProps = {
      designSize,
      exploded: false,
      invalidate,
      isolateSelection: false,
      parts,
      transparent: false,
      viewMode: "perspective" as const,
    };
    const { rerender } = render(createElement(ViewerDemandInvalidator, baseProps));
    expect(invalidate).toHaveBeenCalledTimes(1);

    rerender(createElement(ViewerDemandInvalidator, {
      ...baseProps,
      designSize: { ...designSize },
    }));
    expect(invalidate).toHaveBeenCalledTimes(1);

    rerender(createElement(ViewerDemandInvalidator, {
      ...baseProps,
      selectedPartId: "side-left",
    }));
    expect(invalidate).toHaveBeenCalledTimes(2);

    rerender(createElement(ViewerDemandInvalidator, {
      ...baseProps,
      selectedPartId: "side-left",
      viewMode: "front",
    }));
    expect(invalidate).toHaveBeenCalledTimes(3);

    rerender(createElement(ViewerDemandInvalidator, {
      ...baseProps,
      selectedPartId: "side-left",
      transparent: true,
      viewMode: "front",
    }));
    expect(invalidate).toHaveBeenCalledTimes(4);

    rerender(createElement(ViewerDemandInvalidator, {
      ...baseProps,
      parts: parts.map((part) => part.part_id === "side-left"
        ? { ...part, position_mm: { ...part.position_mm, z: part.position_mm.z + 5 } }
        : part),
      selectedPartId: "side-left",
      transparent: true,
      viewMode: "front",
    }));
    expect(invalidate).toHaveBeenCalledTimes(5);

    rerender(createElement(ViewerDemandInvalidator, {
      ...baseProps,
      comparisonPreview: {
        proposedParts: parts,
        designSize,
        rule: { ruleId: "STR-DEF-001", ruleVersion: "1.1.0", title: "Hyllnedböjning" },
      },
    }));
    expect(invalidate).toHaveBeenCalledTimes(6);
  });
});

describe("large-scene rendering contract", () => {
  const fullCeilingSpec = {
    ...DEFAULT_DESIGN_SPEC,
    design_id: "unit-full-ceiling-wall-library",
    furniture_type: "wall_library" as const,
    width_mm: 6_000,
    height_mm: 4_000,
    depth_mm: 1_200,
    shelf_count: 40,
    divider_count: 16,
    bay_sizing_mode: "count" as const,
    bay_width_ratios: [],
    shelf_height_ratios: [],
    symmetry_locked: true,
    part_overrides: {},
    removed_part_ids: [],
    base_cabinet_height_mm: 680,
    base_cabinet_depth_mm: 1_200,
    base_cabinet_count: 17,
    reinforcement_mode: "manual" as const,
    back_panel: true,
    plinth: true,
  };
  const parts = resolveDesign(fullCeilingSpec).parts;
  const designSize = { widthMm: 6_000, heightMm: 4_000, depthMm: 1_200 };

  it("keeps normal scenes on PartMesh through 200 parts and batches only above the threshold", () => {
    expect(LARGE_SCENE_PART_THRESHOLD).toBe(200);
    expect(shouldUseInstancedPartRendering(199)).toBe(false);
    expect(shouldUseInstancedPartRendering(200)).toBe(false);
    expect(shouldUseInstancedPartRendering(201)).toBe(true);
    expect(shouldUseInstancedPartRendering(768)).toBe(true);
    expect(viewerPartRenderMode(200, false)).toBe("standard");
    expect(viewerPartRenderMode(200, true)).toBe("standard");
    expect(viewerPartRenderMode(201, false)).toBe("instanced");
    expect(viewerPartRenderMode(201, true)).toBe("sortable-transparent");
    expect(viewerPartRenderMode(768, false)).toBe("instanced");
    expect(viewerPartRenderMode(768, true)).toBe("sortable-transparent");
  });

  it("keeps Drei damping for normal scenes and disables its frame cascade only above 200 parts", () => {
    expect(viewerOrbitControlsPerformanceProps(199)).toEqual({ enableDamping: true });
    expect(viewerOrbitControlsPerformanceProps(200)).toEqual({ enableDamping: true });
    expect(viewerOrbitControlsPerformanceProps(201)).toEqual({ enableDamping: false });
    expect(viewerOrbitControlsPerformanceProps(768)).toEqual({ enableDamping: false });
  });

  it("covers all 768 real parts exactly once with stable instanceId mappings and real material groups", () => {
    expect(parts).toHaveLength(768);
    const renderData = buildInstancedPartRenderData({
      parts,
      designSize,
      exploded: false,
      transparent: false,
      isolateSelection: false,
    });
    const instances = renderData.batches.flatMap((batch) => batch.instances);
    const sourceIds = parts.map((part) => part.part_id).sort();
    const instanceIds = instances.map((instance) => instance.partId).sort();

    expect(renderData.partCount).toBe(768);
    expect(instances).toHaveLength(768);
    expect(renderData.batches.length).toBeLessThan(768);
    expect(renderData.batches.every((batch) => !batch.transparent && batch.opacity === 1)).toBe(true);
    expect(new Set(instanceIds).size).toBe(768);
    expect(instanceIds).toEqual(sourceIds);
    expect(new Set(renderData.batches.map((batch) => batch.key)).size).toBe(renderData.batches.length);

    for (const batch of renderData.batches) {
      expect(batch.materialVisual).toEqual(viewerMaterialVisual(batch.materialId));
      expect(batch.receiveShadow).toBe(true);
      expect(batch.instances.every((instance) => instance.part.material_id === batch.materialId)).toBe(true);
      batch.instances.forEach((instance, instanceId) => {
        expect(partForInstancedBatch(batch, instanceId)?.part_id).toBe(instance.partId);
      });
      expect(partForInstancedBatch(batch, batch.instances.length)).toBeUndefined();
    }
  });

  it("renders every transparent ceiling part as its own sortable object with one exact id mapping", () => {
    const selectedPart = parts.find((part) => part.part_id === "top")!;
    const hoveredPart = parts.find((part) => part.part_id !== selectedPart.part_id)!;
    const renderData = buildSortableTransparentPartRenderData({
      parts,
      designSize,
      selectedPartId: selectedPart.part_id,
      hoveredPartId: hoveredPart.part_id,
      exploded: true,
      isolateSelection: false,
    });
    const sourceIds = parts.map((part) => part.part_id);
    const objectIds = renderData.objects.map((object) => object.partId);
    const materialKeys = new Set(renderData.materialBuckets.map((bucket) => bucket.key));
    const floatsPerPart = BOX_EDGE_SEGMENTS_PER_PART * 2 * 3;

    expect(renderData.partCount).toBe(768);
    expect(renderData.objects).toHaveLength(768);
    expect(new Set(objectIds).size).toBe(768);
    expect(objectIds).toEqual(sourceIds);
    expect(renderData.objects.every((object, index) => (
      object.part === parts[index]
      && object.partId === parts[index]!.part_id
      && object.receiveShadow
      && !object.castShadow
      && materialKeys.has(object.materialKey)
      && JSON.stringify(object.transform) === JSON.stringify(viewerPartTransform(parts[index]!, designSize, true))
    ))).toBe(true);
    expect(renderData.edgeSegmentCount).toBe(768 * BOX_EDGE_SEGMENTS_PER_PART);
    expect(renderData.edgePositions).toHaveLength(768 * floatsPerPart);
    expect(renderData.edgeColors).toHaveLength(renderData.edgePositions.length);
  });

  it("plans a deterministic shared transparent resource catalog independent of part transforms", () => {
    const materialIds = parts.map((part) => part.material_id);
    const uniqueMaterialCount = new Set(materialIds).size;
    const catalog = buildSortableTransparentMaterialCatalog(materialIds);
    const reorderedCatalog = buildSortableTransparentMaterialCatalog([...materialIds].reverse());
    const transformedParts = parts.map((part) => ({
      ...part,
      position_mm: { ...part.position_mm, x: part.position_mm.x + 5 },
    }));
    const transformedRenderData = buildSortableTransparentPartRenderData({
      parts: transformedParts,
      designSize,
      exploded: false,
      isolateSelection: false,
    });

    expect(catalog).toEqual(reorderedCatalog);
    expect(catalog.length).toBeLessThanOrEqual(uniqueMaterialCount * 5);
    expect(catalog.length).toBeLessThan(768);
    expect(new Set(catalog.map((bucket) => bucket.key)).size).toBe(catalog.length);
    expect(transformedRenderData.materialBuckets).toEqual(catalog);
    expect(transformedRenderData.objects).toHaveLength(768);
  });

  it("preserves every box transform and emits exactly twelve merged edge segments per part", () => {
    const renderData = buildInstancedPartRenderData({
      parts,
      designSize,
      exploded: false,
      transparent: false,
      isolateSelection: false,
    });
    const floatsPerPart = BOX_EDGE_SEGMENTS_PER_PART * 2 * 3;
    const allTransformsMatch = renderData.batches.every((batch) => batch.instances.every((instance) => (
      JSON.stringify(instance.transform) === JSON.stringify(viewerPartTransform(instance.part, designSize, false))
    )));
    const everyEdgeBoxMatches = parts.every((part, partIndex) => {
      const transform = viewerPartTransform(part, designSize, false);
      const chunk = renderData.edgePositions.subarray(
        partIndex * floatsPerPart,
        (partIndex + 1) * floatsPerPart,
      );
      const axisValues = [0, 1, 2].map((axis) => (
        Array.from({ length: chunk.length / 3 }, (_, vertexIndex) => chunk[vertexIndex * 3 + axis]!)
      ));
      return axisValues.every((values, axis) => {
        const span = Math.max(...values) - Math.min(...values);
        return Math.abs(span - transform.scale[axis]!) < 0.000_01;
      });
    });

    expect(allTransformsMatch).toBe(true);
    expect(renderData.edgeSegmentCount).toBe(768 * BOX_EDGE_SEGMENTS_PER_PART);
    expect(renderData.edgePositions).toHaveLength(768 * floatsPerPart);
    expect(renderData.edgeColors).toHaveLength(renderData.edgePositions.length);
    expect(renderData.edgePositions.every(Number.isFinite)).toBe(true);
    expect(renderData.edgeColors.every(Number.isFinite)).toBe(true);
    expect(everyEdgeBoxMatches).toBe(true);

    const shelf = parts.find((part) => part.part_id === "shelf-1-bay-1")!;
    const compact = viewerPartTransform(shelf, designSize, false);
    const exploded = viewerPartTransform(shelf, designSize, true);
    expect(exploded.scale).toEqual(compact.scale);
    expect(exploded.position[0]).toBe(compact.position[0]);
    expect(exploded.position[1]).toBe(compact.position[1]);
    expect(exploded.position[2]).toBeCloseTo(compact.position[2] - 0.04);
  });

  it("retains selected, hover, transparency, dimming and shadow appearance in sortable objects", () => {
    const selectedPart = parts.find((part) => part.part_id === "top")!;
    const ordinaryPart = parts.find((part) => part.part_id !== selectedPart.part_id)!;
    expect(viewerPartAppearance(selectedPart, true, true, true, false)).toMatchObject({
      color: "#d5b77f",
      edgeColor: "#145c42",
      opacity: 0.76,
      transparent: true,
      depthWrite: true,
      castShadow: false,
    });
    expect(viewerPartAppearance(ordinaryPart, false, true, false, false).color).toBe("#ded3c0");
    expect(viewerPartAppearance(ordinaryPart, false, false, false, true)).toMatchObject({
      edgeColor: "#94a39c",
      opacity: 0.07,
      transparent: true,
      depthWrite: false,
      castShadow: false,
    });

    const renderData = buildSortableTransparentPartRenderData({
      parts,
      designSize,
      selectedPartId: selectedPart.part_id,
      hoveredPartId: ordinaryPart.part_id,
      exploded: false,
      isolateSelection: true,
    });
    const materials = new Map(renderData.materialBuckets.map((bucket) => [bucket.key, bucket] as const));
    const selectedObject = renderData.objects.find((object) => object.partId === selectedPart.part_id)!;
    const dimmedObjects = renderData.objects.filter((object) => object !== selectedObject);
    expect(materials.get(selectedObject.materialKey)).toMatchObject({
      color: "#d5b77f",
      opacity: 0.76,
      depthWrite: true,
      transparent: true,
    });
    expect(selectedObject.castShadow).toBe(false);
    const hoveredObject = renderData.objects.find((object) => object.partId === ordinaryPart.part_id)!;
    expect(materials.get(hoveredObject.materialKey)).toMatchObject({
      color: "#ded3c0",
      opacity: 0.07,
      depthWrite: false,
    });
    expect(dimmedObjects.length).toBeGreaterThan(0);
    expect(dimmedObjects.every((object) => {
      const material = materials.get(object.materialKey);
      return material?.opacity === 0.07 && !material.depthWrite && !object.castShadow;
    })).toBe(true);
  });

  it("rejects duplicate part ids instead of creating an ambiguous event mapping", () => {
    const duplicate = { ...parts[0]! };
    expect(() => buildInstancedPartRenderData({
      parts: [parts[0]!, duplicate],
      designSize,
      exploded: false,
      transparent: false,
      isolateSelection: false,
    })).toThrow(`Duplicate viewer part id: ${duplicate.part_id}`);
    expect(() => buildSortableTransparentPartRenderData({
      parts: [parts[0]!, duplicate],
      designSize,
      exploded: false,
      isolateSelection: false,
    })).toThrow(`Duplicate viewer part id: ${duplicate.part_id}`);
  });

  it("does not clear B hover when a delayed pointerout for A arrives", () => {
    expect(hoveredPartAfterInstanceOut("part-b", "part-a")).toBe("part-b");
    expect(hoveredPartAfterInstanceOut("part-b", "part-b")).toBeUndefined();
    expect(hoveredPartAfterInstanceOut(undefined, "part-a")).toBeUndefined();
  });
});

describe("dimensionAfterDrag", () => {
  it("changes each dimension in the intuitive screen direction", () => {
    expect(dimensionAfterDrag("width", 1_800, 100, 0)).toBeGreaterThan(1_800);
    expect(dimensionAfterDrag("height", 2_100, 0, -100)).toBeGreaterThan(2_100);
    expect(dimensionAfterDrag("depth", 320, 100, 0)).toBeGreaterThan(320);
  });

  it("snaps to ten millimetres and respects manufacturing limits", () => {
    expect(dimensionAfterDrag("width", 1_800, 13, 0) % 10).toBe(0);
    expect(dimensionAfterDrag("width", 5_900, 10_000, 0)).toBe(6_000);
    expect(dimensionAfterDrag("width", 300, -10_000, 0)).toBe(250);
    expect(dimensionAfterDrag("height", 500, 0, 10_000)).toBe(300);
    expect(dimensionAfterDrag("height", 3_900, 0, -10_000)).toBe(4_000);
    expect(dimensionAfterDrag("depth", 200, -10_000, 0)).toBe(100);
    expect(dimensionAfterDrag("depth", 1_100, 10_000, 0)).toBe(1_200);
  });

  it("uses an exact ten millimetre keyboard nudge regardless of current size", () => {
    expect(dimensionAfterNudge("width", 4_200, 1)).toBe(4_210);
    expect(dimensionAfterNudge("height", 2_400, -1)).toBe(2_390);
    expect(dimensionAfterNudge("width", 250, -1)).toBe(250);
    expect(dimensionAfterNudge("height", 4_000, 1)).toBe(4_000);
    expect(dimensionAfterNudge("depth", 100, -1)).toBe(100);
    expect(dimensionAfterNudge("depth", 1_200, 1)).toBe(1_200);
  });
});

describe("DimensionDragOverlay", () => {
  it("keeps the width grip in a screen overlay and emits live width changes", () => {
    const onResizeStart = vi.fn();
    const onResize = vi.fn();
    const onResizeEnd = vi.fn();
    render(createElement(DimensionDragOverlay, {
      designSize: { widthMm: 1_800, heightMm: 2_100, depthMm: 320 },
      onResizeStart,
      onResize,
      onResizeEnd,
    }));

    const widthGrip = screen.getByRole("button", { name: "Dra för att ändra bredd" });
    fireEvent.pointerDown(widthGrip, { pointerId: 1, clientX: 500, clientY: 500 });
    fireEvent.pointerMove(window, { pointerId: 1, clientX: 620, clientY: 500 });
    fireEvent.pointerUp(window, { pointerId: 1, clientX: 620, clientY: 500 });

    expect(onResizeStart).toHaveBeenCalledOnce();
    expect(onResize).toHaveBeenLastCalledWith({ width_mm: dimensionAfterDrag("width", 1_800, 120, 0) });
    expect(onResizeEnd).toHaveBeenCalledOnce();
  });

  it("supports exact keyboard nudging on the width grip", () => {
    const onResize = vi.fn();
    render(createElement(DimensionDragOverlay, {
      designSize: { widthMm: 4_200, heightMm: 2_400, depthMm: 520 },
      onResize,
    }));

    fireEvent.keyDown(screen.getByRole("button", { name: "Dra för att ändra bredd" }), { key: "ArrowRight" });
    expect(onResize).toHaveBeenCalledWith({ width_mm: 4_210 });
  });
});

describe("verticalPositionAfterDrag", () => {
  it("moves upward when the pointer moves upward and snaps to five millimetres", () => {
    const moved = verticalPositionAfterDrag(1_000, -80, 2_100, 700, 10, 2_090);
    expect(moved).toBeGreaterThan(1_000);
    expect(moved % 5).toBe(0);
  });

  it("keeps a dragged board inside its supplied vertical limits", () => {
    expect(verticalPositionAfterDrag(1_000, -10_000, 2_100, 700, 10, 1_900)).toBe(1_900);
    expect(verticalPositionAfterDrag(1_000, 10_000, 2_100, 700, 100, 1_900)).toBe(100);
  });

  it("maps the canonical 300–4000 mm furniture envelope onto the top-board centre", () => {
    const top = resolveDesign(DEFAULT_DESIGN_SPEC).parts.find((part) => part.part_id === "top")!;
    const bounds = verticalDragBoundsForPart(top, DEFAULT_DESIGN_SPEC.height_mm);

    expect(bounds.minZMm + top.thickness_mm / 2).toBe(300);
    expect(bounds.maxZMm + top.thickness_mm / 2).toBe(4_000);
    expect(verticalPositionAfterDrag(
      top.position_mm.z,
      10_000,
      DEFAULT_DESIGN_SPEC.height_mm,
      700,
      bounds.minZMm,
      bounds.maxZMm,
    )).toBe(bounds.minZMm);
    expect(verticalPositionAfterDrag(
      top.position_mm.z,
      -10_000,
      DEFAULT_DESIGN_SPEC.height_mm,
      700,
      bounds.minZMm,
      bounds.maxZMm,
    )).toBe(bounds.maxZMm);
  });
});

describe("horizontalPositionAfterDrag", () => {
  it("moves right when the pointer moves right and snaps to five millimetres", () => {
    const moved = horizontalPositionAfterDrag(900, 80, 2_400, 800, 20, 2_380);
    expect(moved).toBeGreaterThan(900);
    expect(moved % 5).toBe(0);
  });

  it("keeps a divider within its supplied horizontal limits", () => {
    expect(horizontalPositionAfterDrag(900, -10_000, 2_400, 800, 120, 2_280)).toBe(120);
    expect(horizontalPositionAfterDrag(900, 10_000, 2_400, 800, 120, 2_280)).toBe(2_280);
  });
});

describe("part move feedback", () => {
  const spec = {
    ...DEFAULT_DESIGN_SPEC,
    divider_count: 2,
    symmetry_locked: false,
  };
  const parts = resolveDesign(spec).parts;
  const designSize = {
    widthMm: spec.width_mm,
    heightMm: spec.height_mm,
    depthMm: spec.depth_mm,
  };

  it("derives divider openings from the nearest physical YZ faces", () => {
    const divider = parts.find((part) => part.part_id === "divider-1")!;
    const leftSide = parts.find((part) => part.part_id === "side-left")!;
    const rightDivider = parts.find((part) => part.part_id === "divider-2")!;
    const feedback = partMoveFeedback(divider, parts, designSize)!;

    expect(feedback).toMatchObject({
      kind: "divider",
      positionAxis: "X",
      positionMm: divider.position_mm.x,
    });
    expect(feedback.leadingClearanceMm).toBeCloseTo(
      divider.position_mm.x - divider.thickness_mm / 2
        - (leftSide.position_mm.x + leftSide.thickness_mm / 2),
    );
    expect(feedback.trailingClearanceMm).toBeCloseTo(
      rightDivider.position_mm.x - rightDivider.thickness_mm / 2
        - (divider.position_mm.x + divider.thickness_mm / 2),
    );
  });

  it("derives shelf clear heights from resolved boards in the same bay", () => {
    const shelf = parts.find((part) => part.part_id === "shelf-3-bay-1")!;
    const lowerShelf = parts.find((part) => part.part_id === "shelf-2-bay-1")!;
    const upperShelf = parts.find((part) => part.part_id === "shelf-4-bay-1")!;
    const feedback = partMoveFeedback(shelf, parts, designSize)!;

    expect(feedback).toMatchObject({
      kind: "shelf",
      positionAxis: "Z",
      positionMm: shelf.position_mm.z,
    });
    expect(feedback.leadingClearanceMm).toBeCloseTo(
      shelf.position_mm.z - shelf.thickness_mm / 2
        - (lowerShelf.position_mm.z + lowerShelf.thickness_mm / 2),
    );
    expect(feedback.trailingClearanceMm).toBeCloseTo(
      upperShelf.position_mm.z - upperShelf.thickness_mm / 2
        - (shelf.position_mm.z + shelf.thickness_mm / 2),
    );
  });

  it("never substitutes the design envelope for a missing physical face", () => {
    const divider = parts.find((part) => part.part_id === "divider-1")!;
    const withoutLeftFace = parts.filter((part) => part.part_id !== "side-left");
    const feedback = partMoveFeedback(divider, withoutLeftFace, designSize)!;

    expect(feedback.leadingClearanceMm).toBeUndefined();
    expect(feedback.trailingClearanceMm).toBeGreaterThan(0);
  });

  it("announces the snapped live geometry atomically and names an unknown side neutrally", () => {
    const divider = parts.find((part) => part.part_id === "divider-1")!;
    const feedback = partMoveFeedback(
      divider,
      parts.filter((part) => part.part_id !== "side-left"),
      designSize,
    )!;
    render(createElement(PartMoveFeedbackOverlay, { feedback }));

    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status).toHaveAttribute("aria-atomic", "true");
    expect(status).toHaveTextContent(`X ${divider.position_mm.x.toLocaleString("sv-SE", { maximumFractionDigits: 1 })} mm`);
    expect(status).toHaveTextContent("Vänster öppningEj beräkningsbart");
    expect(status).toHaveTextContent("Live · snäpper i 5 mm");
  });

  it("keeps the four-pixel activation threshold exact", () => {
    expect(partDragThresholdReached(3.99, 0)).toBe(false);
    expect(partDragThresholdReached(0, 4)).toBe(true);
    expect(partDragThresholdReached(3, Math.sqrt(7))).toBe(true);
  });
});

describe("validation comparison ghost", () => {
  const sourceParts = resolveDesign(DEFAULT_DESIGN_SPEC).parts;
  const designSize = {
    widthMm: DEFAULT_DESIGN_SPEC.width_mm,
    heightMm: DEFAULT_DESIGN_SPEC.height_mm,
    depthMm: DEFAULT_DESIGN_SPEC.depth_mm,
  };

  it("classifies topology and world-space geometry by id, orientation, scale and position", () => {
    const unchanged = sourceParts[0]!;
    const changed = sourceParts[1]!;
    const removed = sourceParts[2]!;
    const added: ResolvedPart = {
      ...removed,
      part_id: "comparison-added",
      name: "Tillagd jämförelsedel",
    };
    const proposedParts = [
      { ...unchanged, color: "#ffffff", weight_kg: unchanged.weight_kg + 1 },
      { ...changed, position_mm: { ...changed.position_mm, x: changed.position_mm.x + 5 } },
      added,
    ];
    const classification = classifyComparisonParts(
      [unchanged, changed, removed],
      designSize,
      proposedParts,
      designSize,
    );

    expect(classification.unchanged.map((entry) => entry.partId)).toEqual([unchanged.part_id]);
    expect(classification.changed.map((entry) => entry.partId)).toEqual([changed.part_id]);
    expect(classification.removed.map((entry) => entry.partId)).toEqual([removed.part_id]);
    expect(classification.added.map((entry) => entry.partId)).toEqual([added.part_id]);
  });

  it("marks a raw-position-identical left side changed when its viewer envelope moves", () => {
    const leftSide = sourceParts.find((part) => part.part_id === "side-left")!;
    const classification = classifyComparisonParts(
      [leftSide],
      designSize,
      [{ ...leftSide }],
      { ...designSize, widthMm: 4_200 },
    );

    expect(classification.changed.map((entry) => entry.partId)).toEqual([leftSide.part_id]);
    expect(classification.unchanged).toHaveLength(0);
  });

  it("keeps a 768-part comparison linear and bounded to two merged draw buffers", () => {
    const fullCeilingParts = resolveDesign({
      ...DEFAULT_DESIGN_SPEC,
      furniture_type: "wall_library",
      width_mm: 6_000,
      height_mm: 4_000,
      depth_mm: 1_200,
      shelf_count: 40,
      divider_count: 16,
      bay_sizing_mode: "count",
      bay_width_ratios: [],
      shelf_height_ratios: [],
      symmetry_locked: true,
      base_cabinet_height_mm: 680,
      base_cabinet_depth_mm: 1_200,
      base_cabinet_count: 17,
      reinforcement_mode: "manual",
      back_panel: true,
      plinth: true,
    }).parts;
    const proposedParts = fullCeilingParts.map((part) => ({
      ...part,
      position_mm: { ...part.position_mm, y: part.position_mm.y + 1 },
    }));
    const renderData = buildComparisonGhostRenderData({
      sourceParts: fullCeilingParts,
      proposedParts,
      sourceDesignSize: { widthMm: 6_000, heightMm: 4_000, depthMm: 1_200 },
      proposedDesignSize: { widthMm: 6_000, heightMm: 4_000, depthMm: 1_200 },
      exploded: false,
    });
    const floatsPerPart = BOX_EDGE_SEGMENTS_PER_PART * 2 * 3;

    expect(fullCeilingParts).toHaveLength(768);
    expect(renderData.classification.changed).toHaveLength(768);
    expect(renderData.drawBufferCount).toBe(2);
    expect(renderData.sourcePartCount).toBe(768);
    expect(renderData.proposedPartCount).toBe(768);
    expect(renderData.sourceEdgePositions).toHaveLength(768 * floatsPerPart);
    expect(renderData.proposedEdgePositions).toHaveLength(768 * floatsPerPart);
    expect(renderData.sourceEdgePositions.every(Number.isFinite)).toBe(true);
    expect(renderData.proposedEdgePositions.every(Number.isFinite)).toBe(true);
  });

  it("states a no-geometry comparison without relying on color", () => {
    const classification = classifyComparisonParts(sourceParts, designSize, sourceParts, designSize);
    render(createElement(ComparisonLegend, {
      preview: {
        proposedParts: sourceParts,
        designSize,
        rule: { ruleId: "MAT-001", ruleVersion: "2.0.0", title: "Materialkontroll" },
      },
      classification,
    }));

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Inte serververifierad eller tillämpad");
    expect(status).toHaveTextContent("Förslaget ändrar ingen geometri");
    expect(status).toHaveTextContent("Regel MAT-001 · version 2.0.0");
  });

  it("uses distinct declarative dash contracts for original and proposed WebGL outlines", () => {
    const sourceBuffer = new Float32Array([0, 0, 0, 1, 1, 1]);
    const proposedBuffer = new Float32Array([0, 0, 0, 2, 2, 2]);
    expect(comparisonGhostBufferResourceKey(sourceBuffer)).toBe(comparisonGhostBufferResourceKey(sourceBuffer));
    expect(comparisonGhostBufferResourceKey(sourceBuffer)).not.toBe(comparisonGhostBufferResourceKey(proposedBuffer));
    expect(COMPARISON_GHOST_LINE_STYLES.source.semanticPattern).toBe("dashed");
    expect(COMPARISON_GHOST_LINE_STYLES.proposed.semanticPattern).toBe("dotted");
    expect([
      COMPARISON_GHOST_LINE_STYLES.source.dashSize,
      COMPARISON_GHOST_LINE_STYLES.source.gapSize,
    ]).not.toEqual([
      COMPARISON_GHOST_LINE_STYLES.proposed.dashSize,
      COMPARISON_GHOST_LINE_STYLES.proposed.gapSize,
    ]);
    expect(COMPARISON_GHOST_LINE_STYLES.source.color).not.toBe(COMPARISON_GHOST_LINE_STYLES.proposed.color);

    const changedPart = sourceParts[0]!;
    render(createElement(ComparisonLegend, {
      preview: {
        proposedParts: [{
          ...changedPart,
          position_mm: { ...changedPart.position_mm, x: changedPart.position_mm.x + 10 },
        }],
        designSize,
        rule: { ruleId: "VIS-001", ruleVersion: "1.0.0", title: "Visuell jämförelse" },
      },
      classification: {
        added: [],
        removed: [],
        unchanged: [],
        changed: [{
          partId: changedPart.part_id,
          kind: "changed",
          sourcePart: changedPart,
          proposedPart: {
            ...changedPart,
            position_mm: { ...changedPart.position_mm, x: changedPart.position_mm.x + 10 },
          },
        }],
      },
    }));
    expect(screen.getByRole("status")).toHaveTextContent("streckad ockra kontur");
    expect(screen.getByRole("status")).toHaveTextContent("prickad turkos kontur");
  });
});

describe("cameraPositionForView", () => {
  it("opens both 3D and front view facing the furniture front", () => {
    expect(cameraPositionForView("perspective")).toEqual([0, 0, 6]);
    expect(cameraPositionForView("front")).toEqual([0, 0, 6]);
  });

  it("keeps dedicated side and top orientations", () => {
    expect(cameraPositionForView("side")).toEqual([6, 0, 0]);
    expect(cameraPositionForView("top")).toEqual([0, 6, 0.001]);
  });

  it("gives Canvas the front orientation before the first frame", () => {
    expect(initialCameraForView("perspective", false)).toMatchObject({
      position: [0, 0, 6],
      up: [0, 1, 0],
      fov: 38,
    });
    expect(initialCameraForView("top", true)).toMatchObject({
      position: [0, 6, 0.001],
      up: [0, 0, -1],
      zoom: 150,
    });
  });

  it("keeps one initialized camera per view instead of fitting it again on return", () => {
    const initialized = new Set(["perspective", "front"] as const);

    expect(cameraProjectionForView("perspective")).toBe("perspective");
    expect(cameraProjectionForView("front")).toBe("orthographic");
    expect(shouldFitViewCamera(initialized, "front", false)).toBe(false);
    expect(shouldFitViewCamera(initialized, "side", false)).toBe(true);
    expect(shouldFitViewCamera(initialized, "front", true)).toBe(true);
  });
});

describe("viewerMaterialVisual", () => {
  it("maps the existing global material id to distinct neutral PBR values", () => {
    expect(viewerMaterialVisual("birch-plywood")).toMatchObject({
      color: "#c8b18a",
      roughness: 0.68,
      metalness: 0.01,
    });
    expect(viewerMaterialVisual("mdf")).toMatchObject({
      color: "#aaa49b",
      roughness: 0.9,
      metalness: 0,
    });
    expect(viewerMaterialVisual("birch-plywood")).not.toEqual(viewerMaterialVisual("mdf"));
    expect(viewerMaterialVisual("unknown-material")).toEqual(viewerMaterialVisual("unknown-material"));
  });
});

describe("partSupportsVerticalDrag", () => {
  it("offers handles only for moves implemented by the parametric engine", () => {
    const bookcaseParts = resolveDesign(DEFAULT_DESIGN_SPEC).parts;
    const shelf = bookcaseParts.find((part) => part.kind === "shelf")!;
    const top = bookcaseParts.find((part) => part.part_id === "top")!;
    const bottom = bookcaseParts.find((part) => part.part_id === "bottom")!;

    expect(partSupportsVerticalDrag(shelf, bookcaseParts)).toBe(true);
    expect(partSupportsVerticalDrag(top, bookcaseParts)).toBe(true);
    expect(partSupportsVerticalDrag(bottom, bookcaseParts)).toBe(false);

    const wallLibraryParts = resolveDesign({
      ...DEFAULT_DESIGN_SPEC,
      furniture_type: "wall_library",
      base_cabinet_height_mm: 680,
      base_cabinet_depth_mm: 520,
      base_cabinet_count: 2,
    }).parts;
    const wallLibraryBottom = wallLibraryParts.find((part) => part.part_id === "bottom")!;
    const unsupportedBaseBoard = wallLibraryParts.find((part) => (
      part.kind === "base_top" || part.kind === "base_bottom"
    ))!;

    expect(partSupportsVerticalDrag(wallLibraryBottom, wallLibraryParts)).toBe(true);
    expect(partSupportsVerticalDrag(unsupportedBaseBoard, wallLibraryParts)).toBe(false);
  });
});

describe("partSupportsHorizontalDrag", () => {
  it("offers direct horizontal movement only for generated dividers", () => {
    const parts = resolveDesign({ ...DEFAULT_DESIGN_SPEC, divider_count: 2 }).parts;
    const divider = parts.find((part) => part.kind === "divider")!;
    const shelf = parts.find((part) => part.kind === "shelf")!;

    expect(partSupportsHorizontalDrag(divider)).toBe(true);
    expect(partSupportsHorizontalDrag(shelf)).toBe(false);
    expect(partSupportsHorizontalDrag({ ...divider, part_id: "custom-divider" })).toBe(false);
  });
});

describe("browserSupportsWebGL", () => {
  it("fails closed when the browser cannot create a graphics context", () => {
    const createElement = vi.spyOn(document, "createElement").mockReturnValue({
      getContext: vi.fn(() => null),
    } as unknown as HTMLCanvasElement);

    expect(browserSupportsWebGL()).toBe(false);
    createElement.mockRestore();
  });
});

describe("FrontProjectionFallback", () => {
  it("preserves click, Enter and Space selection parity", () => {
    const parts = resolveDesign(DEFAULT_DESIGN_SPEC).parts;
    const target = parts.find((part) => part.part_id === "side-left")!;
    const onSelectPart = vi.fn();
    render(createElement(FrontProjectionFallback, {
      parts,
      designSize: {
        widthMm: DEFAULT_DESIGN_SPEC.width_mm,
        heightMm: DEFAULT_DESIGN_SPEC.height_mm,
        depthMm: DEFAULT_DESIGN_SPEC.depth_mm,
      },
      viewMode: "front",
      exploded: false,
      transparent: false,
      isolateSelection: false,
      onSelectPart,
    }));

    const selectablePart = screen.getByLabelText(target.name);
    fireEvent.click(selectablePart);
    fireEvent.keyDown(selectablePart, { key: "Enter" });
    fireEvent.keyDown(selectablePart, { key: " " });

    expect(onSelectPart).toHaveBeenNthCalledWith(1, target.part_id);
    expect(onSelectPart).toHaveBeenNthCalledWith(2, target.part_id);
    expect(onSelectPart).toHaveBeenNthCalledWith(3, target.part_id);
  });

  it("draws semantic comparison outlines without making ghost geometry interactive", () => {
    const parts = resolveDesign(DEFAULT_DESIGN_SPEC).parts;
    const target = parts.find((part) => part.part_id === "side-left")!;
    const proposedParts = parts.map((part) => part.part_id === target.part_id
      ? { ...part, position_mm: { ...part.position_mm, x: part.position_mm.x + 10 } }
      : part);
    const onSelectPart = vi.fn();
    const { container } = render(createElement(FrontProjectionFallback, {
      parts,
      designSize: {
        widthMm: DEFAULT_DESIGN_SPEC.width_mm,
        heightMm: DEFAULT_DESIGN_SPEC.height_mm,
        depthMm: DEFAULT_DESIGN_SPEC.depth_mm,
      },
      viewMode: "front",
      exploded: false,
      transparent: false,
      isolateSelection: false,
      onSelectPart,
      comparisonPreview: {
        proposedParts,
        designSize: {
          widthMm: DEFAULT_DESIGN_SPEC.width_mm,
          heightMm: DEFAULT_DESIGN_SPEC.height_mm,
          depthMm: DEFAULT_DESIGN_SPEC.depth_mm,
        },
        rule: { ruleId: "STR-DEF-001", ruleVersion: "1.1.0", title: "Hyllnedböjning" },
      },
    }));

    const ghost = screen.getByTestId("comparison-ghost-outlines");
    expect(ghost).toHaveAttribute("aria-hidden", "true");
    expect(ghost).toHaveAttribute("pointer-events", "none");
    expect(within(ghost).queryAllByRole("button")).toHaveLength(0);
    expect(ghost.querySelectorAll("[tabindex]")).toHaveLength(0);
    expect(container.querySelectorAll("svg > rect[tabindex='0']")).toHaveLength(parts.length);
    fireEvent.click(ghost.querySelector("rect")!);
    expect(onSelectPart).not.toHaveBeenCalled();
  });

  it("centers both models in the union viewBox so larger and smaller proposals remain visible", () => {
    const compactSize = { widthMm: 1_200, heightMm: 2_100, depthMm: 320 };
    const expandedSize = { widthMm: 4_200, heightMm: 3_200, depthMm: 320 };
    const compactParts = resolveDesign({
      ...DEFAULT_DESIGN_SPEC,
      width_mm: compactSize.widthMm,
      height_mm: compactSize.heightMm,
    }).parts;
    const expandedParts = resolveDesign({
      ...DEFAULT_DESIGN_SPEC,
      width_mm: expandedSize.widthMm,
      height_mm: expandedSize.heightMm,
    }).parts;
    const onSelectPart = vi.fn();
    const commonProps = {
      viewMode: "front" as const,
      exploded: false,
      transparent: false,
      isolateSelection: false,
      onSelectPart,
    };
    const result = render(createElement(FrontProjectionFallback, {
      ...commonProps,
      parts: compactParts,
      designSize: compactSize,
      comparisonPreview: {
        proposedParts: expandedParts,
        designSize: expandedSize,
        rule: { ruleId: "ENV-001", ruleVersion: "1.0.0", title: "Större förslag" },
      },
    }));

    const assertUnionBounds = () => {
      const svg = result.container.querySelector("svg")!;
      expect(svg).toHaveAttribute("viewBox", "0 0 4200 3200");
      for (const rect of svg.querySelectorAll("rect")) {
        const x = Number(rect.getAttribute("x"));
        const y = Number(rect.getAttribute("y"));
        const width = Number(rect.getAttribute("width"));
        const height = Number(rect.getAttribute("height"));
        expect(x).toBeGreaterThanOrEqual(-1e-6);
        expect(y).toBeGreaterThanOrEqual(-1e-6);
        expect(x + width).toBeLessThanOrEqual(4_200 + 1e-6);
        expect(y + height).toBeLessThanOrEqual(3_200 + 1e-6);
      }
    };

    assertUnionBounds();
    const compactLeft = compactParts.find((part) => part.part_id === "side-left")!;
    const compactLeftRect = screen.getByLabelText(compactLeft.name);
    const expectedCompactRect = centeredFrontProjectionRect(
      compactLeft,
      expandedSize,
      compactSize,
    );
    expect(compactLeftRect).toHaveAttribute("x", String(expectedCompactRect.x));
    expect(compactLeftRect).toHaveAttribute("y", String(expectedCompactRect.y));

    result.rerender(createElement(FrontProjectionFallback, {
      ...commonProps,
      parts: expandedParts,
      designSize: expandedSize,
      comparisonPreview: {
        proposedParts: compactParts,
        designSize: compactSize,
        rule: { ruleId: "ENV-002", ruleVersion: "1.0.0", title: "Mindre förslag" },
      },
    }));
    assertUnionBounds();
  });
});

describe("SnapPreview", () => {
  it("positions arbitrary snap lines with SVG attributes instead of inline CSS", () => {
    const preview: SemanticSnapPreview = {
      kind: "divider",
      relation: "divider_in_carcass",
      label: "Avdelare",
      detail: "1 050 mm",
      targetId: "furniture:carcass",
      normalizedPositions: [0.25, 0.75],
    };

    const { container } = render(createElement(SnapPreview, { preview }));
    const lines = container.querySelectorAll("line");
    expect(lines).toHaveLength(4);
    expect(lines[0]).toHaveAttribute("x1", "25");
    expect(lines[2]).toHaveAttribute("x1", "75");
    expect(container.querySelector("[style]")).toBeNull();
  });
});

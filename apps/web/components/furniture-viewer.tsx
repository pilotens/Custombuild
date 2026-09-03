"use client";

import {
  Suspense,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type DragEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { Bounds, ContactShadows, Edges, GizmoHelper, GizmoViewport, OrbitControls, useBounds } from "@react-three/drei";
import { addAfterEffect, Canvas, type ThreeEvent, useFrame, useThree } from "@react-three/fiber";
import {
  BoxGeometry,
  Color,
  DynamicDrawUsage,
  Matrix4,
  MeshStandardMaterial,
  OrthographicCamera as ThreeOrthographicCamera,
  PerspectiveCamera as ThreePerspectiveCamera,
  Vector3,
} from "three";
import type {
  Group as ThreeGroup,
  InstancedMesh as ThreeInstancedMesh,
  LineSegments as ThreeLineSegments,
} from "three";
import { DESIGN_CONSTRAINTS } from "@/lib/design-constraints";
import type { DesignSpec, ManufacturingFeature, ResolvedPart } from "@/lib/design-types";
import {
  createSemanticSnapPreview,
  readSemanticDragPayload,
  type SemanticComponentKind,
  type SemanticDropRequest,
  type SemanticSnapPreview,
} from "@/lib/semantic-design";
import styles from "./semantic-editor.module.css";

export type ViewMode = "perspective" | "front" | "side" | "top";

export const VIEWER_FRAMELOOP = "demand" as const;
export const VIEWER_RENDER_COMMIT_ATTRIBUTE = "data-custombuild-render-commit";
export const VIEWER_MODEL_ROOT_ATTRIBUTE = "data-custombuild-model-root";
export const LARGE_SCENE_PART_THRESHOLD = 200;
export const BOX_EDGE_SEGMENTS_PER_PART = 12;

interface ViewerRenderContractTarget {
  getAttribute(name: string): string | null;
  removeAttribute(name: string): void;
  setAttribute(name: string, value: string): void;
}

interface PendingViewerRender {
  current: boolean;
}

export function initializeViewerRenderContract(target: ViewerRenderContractTarget): void {
  if (target.getAttribute(VIEWER_RENDER_COMMIT_ATTRIBUTE) === null) {
    target.setAttribute(VIEWER_RENDER_COMMIT_ATTRIBUTE, "0");
  }
}

export function markViewerRenderCommitted(target: ViewerRenderContractTarget): number {
  const rawRevision = target.getAttribute(VIEWER_RENDER_COMMIT_ATTRIBUTE);
  const parsedRevision = rawRevision !== null && /^(0|[1-9]\d*)$/.test(rawRevision)
    ? Number(rawRevision)
    : 0;
  const currentRevision = Number.isSafeInteger(parsedRevision) && parsedRevision >= 0
    ? parsedRevision
    : 0;
  const nextRevision = currentRevision + 1;
  target.setAttribute(VIEWER_RENDER_COMMIT_ATTRIBUTE, String(nextRevision));
  return nextRevision;
}

export function commitPendingViewerRender(
  target: ViewerRenderContractTarget,
  pending: PendingViewerRender,
): boolean {
  if (!pending.current) return false;
  pending.current = false;
  markViewerRenderCommitted(target);
  return true;
}

export function exposeViewerModelRoot(
  target: ViewerRenderContractTarget,
  modelRootId: string,
): () => void {
  target.setAttribute(VIEWER_MODEL_ROOT_ATTRIBUTE, modelRootId);
  return () => {
    if (target.getAttribute(VIEWER_MODEL_ROOT_ATTRIBUTE) === modelRootId) {
      target.removeAttribute(VIEWER_MODEL_ROOT_ATTRIBUTE);
    }
  };
}

export interface ViewerMaterialVisual {
  color: string;
  roughness: number;
  metalness: number;
}

export interface FurnitureComparisonPreview {
  proposedParts: readonly ResolvedPart[];
  designSize: { widthMm: number; heightMm: number; depthMm: number };
  rule: { ruleId: string; ruleVersion: string; title: string };
}

interface FurnitureViewerProps {
  parts: ResolvedPart[];
  designSize: { widthMm: number; heightMm: number; depthMm: number };
  selectedPartId?: string;
  viewMode: ViewMode;
  exploded: boolean;
  transparent: boolean;
  isolateSelection: boolean;
  onSelectPart: (partId?: string) => void;
  resizeEnabled?: boolean;
  onResizeStart?: () => void;
  onResize?: (patch: Partial<Pick<DesignSpec, "width_mm" | "height_mm" | "depth_mm">>) => void;
  onResizeEnd?: () => void;
  onPartMoveStart?: (partId: string) => void;
  onPartMove?: (partId: string, positionZMm: number) => void;
  onPartMoveEnd?: () => void;
  onPartHorizontalMoveStart?: (partId: string) => void;
  onPartHorizontalMove?: (partId: string, positionXMm: number) => void;
  onPartHorizontalMoveEnd?: () => void;
  presentation?: "studio" | "validation" | "production";
  cameraResetNonce?: number;
  semanticDropEnabled?: boolean;
  semanticSpec?: DesignSpec;
  semanticDragKind?: SemanticComponentKind;
  onSemanticDrop?: (request: SemanticDropRequest) => void;
  comparisonPreview?: FurnitureComparisonPreview;
}

export function ViewerDemandInvalidator({
  cameraResetNonce = 0,
  comparisonPreview,
  designSize,
  exploded,
  invalidate,
  isolateSelection,
  parts,
  presentation = "studio",
  selectedPartId,
  transparent,
  viewMode,
}: Pick<
  FurnitureViewerProps,
  | "cameraResetNonce"
  | "comparisonPreview"
  | "designSize"
  | "exploded"
  | "isolateSelection"
  | "parts"
  | "presentation"
  | "selectedPartId"
  | "transparent"
  | "viewMode"
> & { invalidate: () => void }) {
  const { depthMm, heightMm, widthMm } = designSize;

  useLayoutEffect(() => {
    invalidate();
  }, [
    cameraResetNonce,
    comparisonPreview,
    depthMm,
    exploded,
    heightMm,
    invalidate,
    isolateSelection,
    parts,
    presentation,
    selectedPartId,
    transparent,
    viewMode,
    widthMm,
  ]);

  return null;
}

function ViewerRenderCommitProbe() {
  const gl = useThree((state) => state.gl);
  const pending = useRef(false);

  useFrame(() => {
    pending.current = true;
  });

  useLayoutEffect(() => {
    const canvas = gl.domElement;
    initializeViewerRenderContract(canvas);
    const removeAfterEffect = addAfterEffect(() => {
      commitPendingViewerRender(canvas, pending);
    });
    return () => {
      pending.current = false;
      removeAfterEffect();
    };
  }, [gl]);

  return null;
}

type ResizeAxis = "width" | "height" | "depth";

const RESIZE_AXIS_CONSTRAINTS = {
  width: DESIGN_CONSTRAINTS.widthMm,
  height: DESIGN_CONSTRAINTS.heightMm,
  depth: DESIGN_CONSTRAINTS.depthMm,
} as const;

const MATERIAL_VISUALS: Readonly<Record<string, ViewerMaterialVisual>> = {
  "birch-plywood": { color: "#c8b18a", roughness: 0.68, metalness: 0.01 },
  "birch-plywood-6": { color: "#c8b18a", roughness: 0.68, metalness: 0.01 },
  mdf: { color: "#aaa49b", roughness: 0.9, metalness: 0 },
  "mdf-6": { color: "#aaa49b", roughness: 0.9, metalness: 0 },
};

const DEFAULT_MATERIAL_VISUAL: ViewerMaterialVisual = {
  color: "#b8ad9b",
  roughness: 0.78,
  metalness: 0,
};

/** A restrained PBR preview for the material already carried by every resolved part. */
export function viewerMaterialVisual(materialId: string): ViewerMaterialVisual {
  return MATERIAL_VISUALS[materialId] ?? DEFAULT_MATERIAL_VISUAL;
}

const VIEWER_FEATURE_KIND_ORDER = [
  "drill",
  "groove",
  "rabbet",
  "pocket",
  "outline",
  "label",
] as const satisfies readonly ManufacturingFeature["kind"][];

const JOINERY_FEATURE_KINDS = new Set<ManufacturingFeature["kind"]>([
  "drill",
  "groove",
  "rabbet",
  "pocket",
]);

const VIEWER_FEATURE_LABELS: Readonly<Record<ManufacturingFeature["kind"], string>> = {
  outline: "Kontur",
  drill: "Borrning",
  groove: "Spår",
  rabbet: "Fals",
  pocket: "Ficka",
  label: "Märkning",
};

const VIEWER_FEATURE_COLORS: Readonly<Record<ManufacturingFeature["kind"], string>> = {
  outline: "#6c685f",
  drill: "#246b94",
  groove: "#a96824",
  rabbet: "#8a4e78",
  pocket: "#8a3d35",
  label: "#39705a",
};

export const VIEWER_FEATURE_GEOMETRY_LIMITATION =
  "Den lokala 3D-modellen innehåller featuretyp, sida, djup och verktygsdiameter men inte "
  + "featurekoordinater eller utbredning. Exakt geometri ska därför verifieras i DXF/SVG och cam/operations.json.";

export interface ViewerMachiningKindSummary {
  kind: ManufacturingFeature["kind"];
  label: string;
  count: number;
}

export interface ViewerMachiningSummary {
  featureCount: number;
  joineryFeatureCount: number;
  partsWithFeatures: number;
  partsWithJoineryFeatures: number;
  kinds: ViewerMachiningKindSummary[];
}

/** Deterministic feature inventory; no geometry is inferred beyond the resolved viewer contract. */
export function buildViewerMachiningSummary(parts: readonly ResolvedPart[]): ViewerMachiningSummary {
  const counts = new Map<ManufacturingFeature["kind"], number>();
  let featureCount = 0;
  let joineryFeatureCount = 0;
  let partsWithFeatures = 0;
  let partsWithJoineryFeatures = 0;

  for (const part of parts) {
    if (part.features.length > 0) partsWithFeatures += 1;
    let partHasJoineryFeature = false;
    for (const feature of part.features) {
      featureCount += 1;
      counts.set(feature.kind, (counts.get(feature.kind) ?? 0) + 1);
      if (JOINERY_FEATURE_KINDS.has(feature.kind)) {
        joineryFeatureCount += 1;
        partHasJoineryFeature = true;
      }
    }
    if (partHasJoineryFeature) partsWithJoineryFeatures += 1;
  }

  return {
    featureCount,
    joineryFeatureCount,
    partsWithFeatures,
    partsWithJoineryFeatures,
    kinds: VIEWER_FEATURE_KIND_ORDER.flatMap((kind) => {
      const count = counts.get(kind) ?? 0;
      return count > 0 ? [{ kind, label: VIEWER_FEATURE_LABELS[kind], count }] : [];
    }),
  };
}

function formatViewerMillimetres(value: number): string {
  if (!Number.isFinite(value)) return "ogiltigt värde";
  return new Intl.NumberFormat("sv-SE", { maximumFractionDigits: 3 }).format(value);
}

export function viewerFeatureAccessibilityDescription(feature: ManufacturingFeature): string {
  const tool = feature.tool_diameter_mm === undefined
    ? "verktygsdiameter saknas i förhandsvisningen"
    : `verktyg diameter ${formatViewerMillimetres(feature.tool_diameter_mm)} millimeter`;
  const face = feature.face === "EDGE" ? "kant" : `sida ${feature.face}`;
  return `${feature.description}. ${VIEWER_FEATURE_LABELS[feature.kind]}, ${face}, `
    + `djup ${formatViewerMillimetres(feature.depth_mm)} millimeter, ${tool}.`;
}

const MACHINING_OVERLAY_STYLE: CSSProperties = {
  position: "absolute",
  zIndex: 8,
  bottom: 48,
  left: 12,
  width: "min(430px, calc(100% - 24px))",
  maxHeight: "min(58%, 390px)",
  overflow: "auto",
  color: "#f5faf7",
  background: "rgba(22, 39, 31, 0.96)",
  border: "1px solid rgba(145, 212, 178, 0.55)",
  borderRadius: 10,
  boxShadow: "0 12px 30px rgba(24, 34, 29, 0.2)",
  fontSize: 11,
};

const MACHINING_SUMMARY_STYLE: CSSProperties = {
  display: "flex",
  alignItems: "baseline",
  justifyContent: "space-between",
  gap: 12,
  padding: "9px 11px",
  cursor: "pointer",
  fontWeight: 720,
};

const MACHINING_LIST_STYLE: CSSProperties = {
  display: "grid",
  maxHeight: 190,
  gap: 7,
  margin: "8px 0 0",
  padding: 0,
  overflowY: "auto",
  listStyle: "none",
};

/**
 * Exact feature metadata for the selected part. It deliberately refuses to draw
 * approximate cuts because the lightweight viewer contract carries no feature coordinates.
 */
export function ManufacturingFeatureOverlay({
  parts,
  selectedPart,
}: {
  parts: readonly ResolvedPart[];
  selectedPart?: ResolvedPart;
}) {
  const selectedPartId = selectedPart?.part_id;
  const [expansion, setExpansion] = useState({
    partId: selectedPartId,
    open: Boolean(selectedPartId),
  });
  const modelSummary = useMemo(() => buildViewerMachiningSummary(parts), [parts]);
  const selectedSummary = useMemo(
    () => buildViewerMachiningSummary(selectedPart ? [selectedPart] : []),
    [selectedPart],
  );
  const activeSummary = selectedPart ? selectedSummary : modelSummary;
  const summaryText = selectedPart
    ? `${selectedSummary.featureCount} poster · ${selectedSummary.joineryFeatureCount} fog/hål`
    : `${modelSummary.joineryFeatureCount} fog/hål · ${modelSummary.partsWithJoineryFeatures} delar`;
  const expanded = expansion.partId === selectedPartId
    ? expansion.open
    : Boolean(selectedPartId);

  return (
    <aside
      aria-label="Bearbetningsöversikt"
      aria-live="polite"
      data-geometry-source="metadata-only"
      data-testid="manufacturing-feature-overlay"
      role="region"
      style={MACHINING_OVERLAY_STYLE}
    >
      <details
        open={expanded}
        onToggle={(event) => setExpansion({
          partId: selectedPartId,
          open: event.currentTarget.open,
        })}
      >
        <summary style={MACHINING_SUMMARY_STYLE}>
          <span>Bearbetningsöversikt</span>
          <small style={{ color: "#a9d8c0", fontVariantNumeric: "tabular-nums" }}>
            {summaryText}
          </small>
        </summary>
        <div style={{ padding: "0 11px 11px" }}>
          <p style={{ margin: 0, color: "#d9e9e0", lineHeight: 1.45 }}>
            {selectedPart
              ? <><strong>{selectedPart.name}</strong> · <code>{selectedPart.part_id}</code></>
              : <>Välj en del för exakt feature-ID, sida, djup och verktygsdiameter.</>}
          </p>
          {activeSummary.kinds.length > 0 ? (
            <ul
              aria-label={selectedPart ? "Featuretyper på vald del" : "Featuretyper i modellen"}
              style={{ display: "flex", flexWrap: "wrap", gap: 5, margin: "8px 0", padding: 0, listStyle: "none" }}
            >
              {activeSummary.kinds.map((entry) => (
                <li
                  key={entry.kind}
                  style={{
                    padding: "3px 6px",
                    background: VIEWER_FEATURE_COLORS[entry.kind],
                    borderRadius: 999,
                    fontSize: 9,
                    fontWeight: 720,
                  }}
                >
                  {entry.label} {entry.count}
                </li>
              ))}
            </ul>
          ) : null}
          {selectedPart ? (
            selectedPart.features.length > 0 ? (
              <ul aria-label={`Bearbetningsposter för ${selectedPart.name}`} style={MACHINING_LIST_STYLE}>
                {selectedPart.features.map((feature, index) => (
                  <li
                    key={`${feature.id}-${index}`}
                    aria-label={viewerFeatureAccessibilityDescription(feature)}
                    style={{
                      display: "grid",
                      gridTemplateColumns: "5px minmax(0, 1fr)",
                      gap: 8,
                      padding: "6px 7px",
                      color: "#eef6f1",
                      background: "rgba(255, 255, 255, 0.07)",
                      borderRadius: 7,
                      lineHeight: 1.35,
                    }}
                  >
                    <i
                      aria-hidden="true"
                      style={{ background: VIEWER_FEATURE_COLORS[feature.kind], borderRadius: 999 }}
                    />
                    <span aria-hidden="true">
                      <strong>{feature.description}</strong>
                      <small style={{ display: "block", marginTop: 2, color: "#bfd1c7" }}>
                        {VIEWER_FEATURE_LABELS[feature.kind]} · {feature.face === "EDGE" ? "kant" : `sida ${feature.face}`} · djup {formatViewerMillimetres(feature.depth_mm)} mm
                        {feature.tool_diameter_mm === undefined
                          ? " · verktyg ej angivet"
                          : ` · verktyg Ø${formatViewerMillimetres(feature.tool_diameter_mm)} mm`}
                      </small>
                      <code style={{ display: "block", marginTop: 2, color: "#9fc3b1", fontSize: 8, overflowWrap: "anywhere" }}>
                        {feature.id}
                      </code>
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p style={{ margin: "8px 0 0" }}>Den valda delen saknar bearbetningsposter.</p>
            )
          ) : null}
          <p
            data-testid="viewer-feature-geometry-limitation"
            style={{ margin: "9px 0 0", paddingTop: 8, color: "#bad0c4", borderTop: "1px solid rgba(186, 208, 196, 0.22)", fontSize: 9, lineHeight: 1.45 }}
          >
            <strong>Ingen uppskattad skärgeometri:</strong> {VIEWER_FEATURE_GEOMETRY_LIMITATION}
          </p>
        </div>
      </details>
    </aside>
  );
}

/** Mirrors the vertical parametric moves that the design engine can actually perform. */
export function partSupportsVerticalDrag(part: ResolvedPart, parts: readonly ResolvedPart[]): boolean {
  if (/^shelf-\d+-bay-\d+$/.test(part.part_id)) return true;
  if (part.part_id === "top") return true;
  return part.part_id === "bottom" && parts.some((candidate) => candidate.kind === "base_side");
}

/** Only generated full-height dividers have a canonical horizontal ratio edit. */
export function partSupportsHorizontalDrag(part: ResolvedPart): boolean {
  return part.kind === "divider" && /^divider-\d+$/.test(part.part_id);
}

export function cameraProjectionForView(viewMode: ViewMode): "perspective" | "orthographic" {
  return viewMode === "perspective" ? "perspective" : "orthographic";
}

export function shouldFitViewCamera(
  initializedViews: ReadonlySet<ViewMode>,
  viewMode: ViewMode,
  resetRequested: boolean,
): boolean {
  return resetRequested || !initializedViews.has(viewMode);
}

export function browserSupportsWebGL(): boolean {
  if (typeof document === "undefined") return false;
  try {
    const canvas = document.createElement("canvas");
    return Boolean(canvas.getContext("webgl2") ?? canvas.getContext("webgl"));
  } catch {
    return false;
  }
}

export function dimensionAfterDrag(
  axis: ResizeAxis,
  startValue: number,
  deltaX: number,
  deltaY: number,
): number {
  const limits = RESIZE_AXIS_CONSTRAINTS[axis];
  const pixelScale = axis === "width"
    ? Math.max(3, startValue / 620)
    : axis === "height"
      ? Math.max(3, startValue / 520)
      : Math.max(1.2, startValue / 360);
  const pixelDelta = axis === "width"
    ? deltaX
    : axis === "height"
      ? -deltaY
      : (deltaX - deltaY) * 0.55;
  const next = Math.round((startValue + pixelDelta * pixelScale) / 10) * 10;
  return Math.min(limits.maximum, Math.max(limits.minimum, next));
}

export function dimensionAfterNudge(axis: ResizeAxis, startValue: number, direction: -1 | 1): number {
  const limits = RESIZE_AXIS_CONSTRAINTS[axis];
  const next = Math.round((startValue + direction * 10) / 10) * 10;
  return Math.min(limits.maximum, Math.max(limits.minimum, next));
}

export function verticalDragBoundsForPart(
  part: ResolvedPart,
  designHeightMm: number,
): { minZMm: number; maxZMm: number } {
  if (part.kind === "top") {
    const halfThickness = part.thickness_mm / 2;
    return {
      minZMm: DESIGN_CONSTRAINTS.heightMm.minimum - halfThickness,
      maxZMm: DESIGN_CONSTRAINTS.heightMm.maximum - halfThickness,
    };
  }

  const verticalSizeMm = part.orientation === "YZ"
    ? part.width_mm
    : part.orientation === "XZ"
      ? part.depth_mm
      : part.thickness_mm;
  const horizontalBoard = part.orientation === "XY";
  return {
    minZMm: horizontalBoard ? verticalSizeMm / 2 : 0,
    maxZMm: horizontalBoard ? designHeightMm - verticalSizeMm / 2 : designHeightMm,
  };
}

export function verticalPositionAfterDrag(
  startZMm: number,
  deltaY: number,
  designHeightMm: number,
  viewportHeightPx: number,
  minZMm: number,
  maxZMm: number,
): number {
  const millimetresPerPixel = Math.max(1, designHeightMm / Math.max(viewportHeightPx, 320) * 1.08);
  const next = Math.round((startZMm - deltaY * millimetresPerPixel) / 5) * 5;
  return Math.min(maxZMm, Math.max(minZMm, next));
}

export function horizontalPositionAfterDrag(
  startXMm: number,
  deltaX: number,
  designWidthMm: number,
  viewportWidthPx: number,
  minXMm: number,
  maxXMm: number,
): number {
  const millimetresPerPixel = Math.max(1, designWidthMm / Math.max(viewportWidthPx, 320) * 1.08);
  const next = Math.round((startXMm + deltaX * millimetresPerPixel) / 5) * 5;
  return Math.min(maxXMm, Math.max(minXMm, next));
}

export function partDragThresholdReached(deltaX: number, deltaY: number): boolean {
  return Math.hypot(deltaX, deltaY) >= 4;
}

export interface PartMoveFeedback {
  kind: "divider" | "shelf";
  partId: string;
  partName: string;
  positionAxis: "X" | "Z";
  positionMm: number;
  leadingLabel: "Vänster öppning" | "Fri höjd under";
  trailingLabel: "Höger öppning" | "Fri höjd över";
  leadingClearanceMm?: number;
  trailingClearanceMm?: number;
}

type PhysicalAxis = "x" | "z";

function physicalSpan(part: ResolvedPart, axis: PhysicalAxis): readonly [number, number] {
  const centre = part.position_mm[axis];
  const size = axis === "x"
    ? part.orientation === "YZ" ? part.thickness_mm : part.width_mm
    : part.orientation === "YZ"
      ? part.width_mm
      : part.orientation === "XZ"
        ? part.depth_mm
        : part.thickness_mm;
  return [centre - size / 2, centre + size / 2];
}

function withinDesignEnvelope(
  span: readonly [number, number],
  sizeMm: number,
): boolean {
  const toleranceMm = 0.01;
  return span[0] >= -toleranceMm && span[1] <= sizeMm + toleranceMm;
}

/**
 * Measures the genuinely open space around a movable board. Every clearance
 * ends at a physical face in the resolved model; the design envelope is only
 * used to reject unrelated/out-of-envelope geometry and is never substituted
 * for a missing board.
 */
export function partMoveFeedback(
  part: ResolvedPart,
  parts: readonly ResolvedPart[],
  designSize: FurnitureViewerProps["designSize"],
): PartMoveFeedback | undefined {
  if (part.kind === "divider" && partSupportsHorizontalDrag(part)) {
    const [targetLeft, targetRight] = physicalSpan(part, "x");
    const [targetBottom, targetTop] = physicalSpan(part, "z");
    const targetHeight = targetTop - targetBottom;
    const boundaries = parts
      .filter((candidate) => {
        if (candidate.part_id === part.part_id || candidate.orientation !== "YZ") return false;
        const candidateX = physicalSpan(candidate, "x");
        if (!withinDesignEnvelope(candidateX, designSize.widthMm)) return false;
        const [candidateBottom, candidateTop] = physicalSpan(candidate, "z");
        const verticalOverlap = Math.min(targetTop, candidateTop) - Math.max(targetBottom, candidateBottom);
        return verticalOverlap > Math.min(targetHeight, candidateTop - candidateBottom) * 0.5;
      })
      .map((candidate) => physicalSpan(candidate, "x"));
    const leftFace = boundaries
      .filter(([, right]) => right <= targetLeft + 0.01)
      .reduce<number | undefined>((nearest, [, right]) => nearest === undefined || right > nearest ? right : nearest, undefined);
    const rightFace = boundaries
      .filter(([left]) => left >= targetRight - 0.01)
      .reduce<number | undefined>((nearest, [left]) => nearest === undefined || left < nearest ? left : nearest, undefined);
    return {
      kind: "divider",
      partId: part.part_id,
      partName: part.name,
      positionAxis: "X",
      positionMm: part.position_mm.x,
      leadingLabel: "Vänster öppning",
      trailingLabel: "Höger öppning",
      ...(leftFace === undefined ? {} : { leadingClearanceMm: Math.max(0, targetLeft - leftFace) }),
      ...(rightFace === undefined ? {} : { trailingClearanceMm: Math.max(0, rightFace - targetRight) }),
    };
  }

  if (part.kind === "shelf" && /^shelf-\d+-bay-\d+$/.test(part.part_id)) {
    const [targetLeft, targetRight] = physicalSpan(part, "x");
    const [targetBottom, targetTop] = physicalSpan(part, "z");
    const targetCentreX = (targetLeft + targetRight) / 2;
    const boundaries = parts
      .filter((candidate) => {
        if (candidate.part_id === part.part_id || candidate.orientation !== "XY") return false;
        const candidateZ = physicalSpan(candidate, "z");
        if (!withinDesignEnvelope(candidateZ, designSize.heightMm)) return false;
        const [candidateLeft, candidateRight] = physicalSpan(candidate, "x");
        return targetCentreX > candidateLeft + 0.01 && targetCentreX < candidateRight - 0.01;
      })
      .map((candidate) => physicalSpan(candidate, "z"));
    const lowerFace = boundaries
      .filter(([, top]) => top <= targetBottom + 0.01)
      .reduce<number | undefined>((nearest, [, top]) => nearest === undefined || top > nearest ? top : nearest, undefined);
    const upperFace = boundaries
      .filter(([bottom]) => bottom >= targetTop - 0.01)
      .reduce<number | undefined>((nearest, [bottom]) => nearest === undefined || bottom < nearest ? bottom : nearest, undefined);
    return {
      kind: "shelf",
      partId: part.part_id,
      partName: part.name,
      positionAxis: "Z",
      positionMm: part.position_mm.z,
      leadingLabel: "Fri höjd under",
      trailingLabel: "Fri höjd över",
      ...(lowerFace === undefined ? {} : { leadingClearanceMm: Math.max(0, targetBottom - lowerFace) }),
      ...(upperFace === undefined ? {} : { trailingClearanceMm: Math.max(0, upperFace - targetTop) }),
    };
  }

  return undefined;
}

function formatMillimetres(value: number | undefined): string {
  if (value === undefined) return "Ej beräkningsbart";
  return `${value.toLocaleString("sv-SE", { maximumFractionDigits: 1 })} mm`;
}

export function PartMoveFeedbackOverlay({ feedback }: { feedback: PartMoveFeedback }) {
  return (
    <output
      className={styles.partMoveFeedback}
      data-testid="part-move-feedback"
      role="status"
      aria-live="polite"
      aria-atomic="true"
    >
      <strong>{feedback.kind === "divider" ? "Flyttar avdelare" : "Flyttar hylla"}</strong>
      <span>{feedback.partName} · {feedback.positionAxis} {formatMillimetres(feedback.positionMm)}</span>
      <dl>
        <div><dt>{feedback.leadingLabel}</dt><dd>{formatMillimetres(feedback.leadingClearanceMm)}</dd></div>
        <div><dt>{feedback.trailingLabel}</dt><dd>{formatMillimetres(feedback.trailingClearanceMm)}</dd></div>
      </dl>
      <small>Live · snäpper i 5 mm</small>
    </output>
  );
}

export function cameraPositionForView(viewMode: ViewMode): [number, number, number] {
  if (viewMode === "side") return [6, 0, 0];
  if (viewMode === "top") return [0, 6, 0.001];
  // Furniture depth starts at the physical front and grows toward the back.
  // Original Y is mapped to negative Three.js Z below, so the physical front
  // is on positive Z and the back panel remains behind shelves and fronts.
  return [0, 0, 6];
}

export function initialCameraForView(viewMode: ViewMode, orthographic: boolean) {
  const orientation = {
    position: cameraPositionForView(viewMode),
    up: viewMode === "top" ? [0, 0, -1] as [number, number, number] : [0, 1, 0] as [number, number, number],
  };
  return orthographic
    ? { ...orientation, near: 0.01, far: 100, zoom: 150 }
    : { ...orientation, near: 0.01, far: 100, fov: 38 };
}

export function DimensionDragOverlay(props: Pick<
  FurnitureViewerProps,
  "designSize" | "onResizeStart" | "onResize" | "onResizeEnd"
>) {
  const [activeAxis, setActiveAxis] = useState<ResizeAxis>();
  const drag = useRef<{ axis: ResizeAxis; x: number; y: number; value: number } | undefined>(undefined);
  const removeDragListeners = useRef<(() => void) | undefined>(undefined);
  const { designSize, onResize, onResizeEnd, onResizeStart } = props;

  useEffect(() => {
    return () => removeDragListeners.current?.();
  }, []);

  const start = (axis: ResizeAxis, event: ReactPointerEvent<HTMLButtonElement>) => {
    event.preventDefault();
    event.stopPropagation();
    removeDragListeners.current?.();
    event.currentTarget.setPointerCapture?.(event.pointerId);
    const value = axis === "width"
      ? designSize.widthMm
      : axis === "height"
        ? designSize.heightMm
        : designSize.depthMm;
    drag.current = { axis, x: event.clientX, y: event.clientY, value };

    // Register synchronously during pointerdown. Waiting for a React effect can
    // lose the first (or only) pointermove on a fast physical drag.
    const move = (event: PointerEvent) => {
      const current = drag.current;
      if (!current) return;
      const value = dimensionAfterDrag(
        current.axis,
        current.value,
        event.clientX - current.x,
        event.clientY - current.y,
      );
      const field = current.axis === "width" ? "width_mm" : current.axis === "height" ? "height_mm" : "depth_mm";
      onResize?.({ [field]: value });
    };
    const finish = () => {
      removeDragListeners.current?.();
      drag.current = undefined;
      setActiveAxis(undefined);
      onResizeEnd?.();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", finish, { once: true });
    window.addEventListener("pointercancel", finish, { once: true });
    const cleanup = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", finish);
      window.removeEventListener("pointercancel", finish);
    };
    removeDragListeners.current = cleanup;
    setActiveAxis(axis);
    onResizeStart?.();
  };

  const nudge = (axis: ResizeAxis, direction: -1 | 1) => {
    const value = axis === "width"
      ? designSize.widthMm
      : axis === "height"
        ? designSize.heightMm
        : designSize.depthMm;
    const field = axis === "width" ? "width_mm" : axis === "height" ? "height_mm" : "depth_mm";
    onResizeStart?.();
    onResize?.({ [field]: dimensionAfterNudge(axis, value, direction) });
    onResizeEnd?.();
  };

  const valueFor = (axis: ResizeAxis) => axis === "width"
    ? designSize.widthMm
    : axis === "height"
      ? designSize.heightMm
      : designSize.depthMm;

  return (
    <div
      className={`dimension-drag-layer ${activeAxis ? "dragging" : ""}`}
      aria-label="Ändra möbelns yttermått direkt i modellen"
    >
      {(["width", "height", "depth"] as const).map((axis) => (
        <div
          key={axis}
          className={`dimension-guide dimension-guide-${axis} ${activeAxis === axis ? "active" : ""}`}
        >
          <span className="dimension-guide-line" aria-hidden="true" />
          <button
            type="button"
            data-resize-axis={axis}
            aria-label={`Dra för att ändra ${axis === "width" ? "bredd" : axis === "height" ? "höjd" : "djup"}`}
            onPointerDown={(event) => start(axis, event)}
            onKeyDown={(event) => {
              if (event.key === "ArrowRight" || event.key === "ArrowUp") nudge(axis, 1);
              if (event.key === "ArrowLeft" || event.key === "ArrowDown") nudge(axis, -1);
            }}
          >
            <span className="dimension-handle-dot" aria-hidden="true" />
          </button>
          <output>{valueFor(axis).toLocaleString("sv-SE")} mm</output>
        </div>
      ))}
      {activeAxis ? (
        <span className="drag-live-message" role="status">
          {activeAxis === "width" ? "Bredd" : activeAxis === "height" ? "Höjd" : "Djup"}: {valueFor(activeAxis).toLocaleString("sv-SE")} mm
        </span>
      ) : null}
    </div>
  );
}

function partSize(part: ResolvedPart): [number, number, number] {
  if (part.orientation === "YZ") {
    return [part.thickness_mm / 1_000, part.width_mm / 1_000, part.depth_mm / 1_000];
  }
  if (part.orientation === "XZ") {
    return [part.width_mm / 1_000, part.depth_mm / 1_000, part.thickness_mm / 1_000];
  }
  return [part.width_mm / 1_000, part.thickness_mm / 1_000, part.depth_mm / 1_000];
}

function explodedOffset(part: ResolvedPart, designSize: FurnitureViewerProps["designSize"]): [number, number, number] {
  const relativeX = part.position_mm.x - designSize.widthMm / 2;
  if (part.kind === "side") return [Math.sign(relativeX || 1) * 0.18, 0, 0];
  if (part.kind === "top") return [0, 0.2, 0];
  if (part.kind === "bottom") return [0, -0.12, 0];
  if (part.kind === "back") return [0, 0, 0.22];
  if (part.kind === "plinth") return [0, -0.06, -0.16];
  if (part.kind === "divider") return [Math.sign(relativeX || 1) * 0.08, 0, -0.08];
  if (part.kind === "base_side") return [Math.sign(relativeX || 1) * 0.1, -0.04, -0.08];
  if (part.kind === "base_bottom" || part.kind === "base_top") return [0, -0.1, -0.08];
  if (part.kind === "cabinet_front") return [0, 0, -0.24];
  const shelfNumber = Number(part.part_id.match(/shelf-(\d+)/)?.[1] ?? 0);
  return [0, 0, -0.04 * (shelfNumber % 3)];
}

export interface ViewerPartTransform {
  position: [number, number, number];
  scale: [number, number, number];
}

export interface ViewerPartAppearance {
  color: string;
  edgeColor: string;
  opacity: number;
  transparent: boolean;
  depthWrite: boolean;
  castShadow: boolean;
  materialVisual: ViewerMaterialVisual;
}

export interface InstancedViewerPart {
  partId: string;
  part: ResolvedPart;
  transform: ViewerPartTransform;
  color: string;
}

export interface InstancedViewerBatch {
  key: string;
  materialId: string;
  materialVisual: ViewerMaterialVisual;
  opacity: number;
  transparent: boolean;
  depthWrite: boolean;
  castShadow: boolean;
  receiveShadow: true;
  instances: InstancedViewerPart[];
}

export interface InstancedViewerRenderData {
  batches: InstancedViewerBatch[];
  edgePositions: Float32Array;
  edgeColors: Float32Array;
  edgeSegmentCount: number;
  partCount: number;
}

export type ViewerPartRenderMode = "standard" | "instanced" | "sortable-transparent";

export interface SortableTransparentMaterialBucket {
  key: string;
  materialId: string;
  color: string;
  roughness: number;
  metalness: number;
  opacity: number;
  transparent: true;
  depthWrite: boolean;
}

export interface SortableTransparentPartObject {
  partId: string;
  part: ResolvedPart;
  transform: ViewerPartTransform;
  materialKey: string;
  castShadow: boolean;
  receiveShadow: true;
}

export interface SortableTransparentPartRenderData {
  objects: SortableTransparentPartObject[];
  materialBuckets: SortableTransparentMaterialBucket[];
  edgePositions: Float32Array;
  edgeColors: Float32Array;
  edgeSegmentCount: number;
  partCount: number;
}

export type ComparisonPartKind = "added" | "removed" | "changed" | "unchanged";

export interface ComparisonPartEntry {
  partId: string;
  kind: ComparisonPartKind;
  sourcePart?: ResolvedPart;
  proposedPart?: ResolvedPart;
}

export interface FurnitureComparisonClassification {
  added: ComparisonPartEntry[];
  removed: ComparisonPartEntry[];
  changed: ComparisonPartEntry[];
  unchanged: ComparisonPartEntry[];
}

export interface FurnitureComparisonGhostRenderData {
  classification: FurnitureComparisonClassification;
  sourceEdgePositions: Float32Array;
  proposedEdgePositions: Float32Array;
  sourcePartCount: number;
  proposedPartCount: number;
  drawBufferCount: number;
}

export const COMPARISON_SOURCE_COLOR = "#a96824";
export const COMPARISON_PROPOSED_COLOR = "#087f8c";
export const COMPARISON_GHOST_LINE_STYLES = {
  source: {
    color: COMPARISON_SOURCE_COLOR,
    dashSize: 0.014,
    gapSize: 0.008,
    semanticPattern: "dashed",
  },
  proposed: {
    color: COMPARISON_PROPOSED_COLOR,
    dashSize: 0.004,
    gapSize: 0.005,
    semanticPattern: "dotted",
  },
} as const;

const comparisonGhostBufferKeys = new WeakMap<Float32Array, number>();
let comparisonGhostBufferSequence = 0;

/** Stable per-buffer key so R3F disposes replaced dashed-line resources on unmount. */
export function comparisonGhostBufferResourceKey(positions: Float32Array): number {
  const existing = comparisonGhostBufferKeys.get(positions);
  if (existing !== undefined) return existing;
  comparisonGhostBufferSequence += 1;
  comparisonGhostBufferKeys.set(positions, comparisonGhostBufferSequence);
  return comparisonGhostBufferSequence;
}

interface ViewerPartRenderRecord {
  part: ResolvedPart;
  transform: ViewerPartTransform;
  appearance: ViewerPartAppearance;
}

interface ViewerPartGeometryData {
  records: ViewerPartRenderRecord[];
  edgePositions: Float32Array;
  edgeColors: Float32Array;
  edgeSegmentCount: number;
  partCount: number;
}

const BOX_EDGE_CORNER_PAIRS = [
  [0, 1], [1, 2], [2, 3], [3, 0],
  [4, 5], [5, 6], [6, 7], [7, 4],
  [0, 4], [1, 5], [2, 6], [3, 7],
] as const;

function assertUniqueComparisonPartIds(parts: readonly ResolvedPart[], side: "source" | "proposed"): void {
  const ids = new Set<string>();
  for (const part of parts) {
    if (ids.has(part.part_id)) throw new Error(`Duplicate ${side} comparison part id: ${part.part_id}`);
    ids.add(part.part_id);
  }
}

function sameTransformTuple(left: readonly number[], right: readonly number[]): boolean {
  return left.length === right.length && left.every((value, index) => Object.is(value, right[index]));
}

function hasSameComparisonGeometry(
  source: ResolvedPart,
  sourceDesignSize: FurnitureViewerProps["designSize"],
  proposed: ResolvedPart,
  proposedDesignSize: FurnitureViewerProps["designSize"],
): boolean {
  const sourceTransform = viewerPartTransform(source, sourceDesignSize, false);
  const proposedTransform = viewerPartTransform(proposed, proposedDesignSize, false);
  return source.part_id === proposed.part_id
    && source.orientation === proposed.orientation
    && sameTransformTuple(sourceTransform.position, proposedTransform.position)
    && sameTransformTuple(sourceTransform.scale, proposedTransform.scale);
}

/** Classifies a comparison in two linear passes without changing current-part identity. */
export function classifyComparisonParts(
  sourceParts: readonly ResolvedPart[],
  sourceDesignSize: FurnitureViewerProps["designSize"],
  proposedParts: readonly ResolvedPart[],
  proposedDesignSize: FurnitureViewerProps["designSize"],
): FurnitureComparisonClassification {
  assertUniqueComparisonPartIds(sourceParts, "source");
  assertUniqueComparisonPartIds(proposedParts, "proposed");
  const proposedById = new Map(proposedParts.map((part) => [part.part_id, part] as const));
  const sourceIds = new Set(sourceParts.map((part) => part.part_id));
  const classification: FurnitureComparisonClassification = {
    added: [],
    removed: [],
    changed: [],
    unchanged: [],
  };

  for (const sourcePart of sourceParts) {
    const proposedPart = proposedById.get(sourcePart.part_id);
    if (!proposedPart) {
      classification.removed.push({ partId: sourcePart.part_id, kind: "removed", sourcePart });
    } else if (hasSameComparisonGeometry(sourcePart, sourceDesignSize, proposedPart, proposedDesignSize)) {
      classification.unchanged.push({
        partId: sourcePart.part_id,
        kind: "unchanged",
        sourcePart,
        proposedPart,
      });
    } else {
      classification.changed.push({
        partId: sourcePart.part_id,
        kind: "changed",
        sourcePart,
        proposedPart,
      });
    }
  }
  for (const proposedPart of proposedParts) {
    if (!sourceIds.has(proposedPart.part_id)) {
      classification.added.push({ partId: proposedPart.part_id, kind: "added", proposedPart });
    }
  }
  return classification;
}

export function shouldUseInstancedPartRendering(partCount: number): boolean {
  return partCount > LARGE_SCENE_PART_THRESHOLD;
}

export function viewerPartRenderMode(partCount: number, transparent: boolean): ViewerPartRenderMode {
  if (!shouldUseInstancedPartRendering(partCount)) return "standard";
  return transparent ? "sortable-transparent" : "instanced";
}

export function viewerOrbitControlsPerformanceProps(partCount: number): { enableDamping: boolean } {
  return { enableDamping: !shouldUseInstancedPartRendering(partCount) };
}

export function viewerPartTransform(
  part: ResolvedPart,
  designSize: FurnitureViewerProps["designSize"],
  exploded: boolean,
): ViewerPartTransform {
  const basePosition: [number, number, number] = [
    (part.position_mm.x - designSize.widthMm / 2) / 1_000,
    (part.position_mm.z - designSize.heightMm / 2) / 1_000,
    -(part.position_mm.y - designSize.depthMm / 2) / 1_000,
  ];
  const offset = exploded ? explodedOffset(part, designSize) : [0, 0, 0] as [number, number, number];
  return {
    position: [
      basePosition[0] + offset[0],
      basePosition[1] + offset[1],
      basePosition[2] + offset[2],
    ],
    scale: partSize(part),
  };
}

function viewerPartAppearanceForMaterial(
  materialId: string,
  selected: boolean,
  hovered: boolean,
  transparent: boolean,
  dimmed: boolean,
): ViewerPartAppearance {
  const opacity = dimmed ? 0.07 : transparent ? (selected ? 0.76 : 0.34) : 1;
  const materialVisual = viewerMaterialVisual(materialId);
  return {
    color: selected ? "#d5b77f" : hovered ? "#ded3c0" : materialVisual.color,
    edgeColor: selected ? "#145c42" : dimmed ? "#94a39c" : "#574b3b",
    opacity,
    transparent: opacity < 1,
    depthWrite: opacity > 0.25,
    castShadow: !transparent && !dimmed,
    materialVisual,
  };
}

export function viewerPartAppearance(
  part: ResolvedPart,
  selected: boolean,
  hovered: boolean,
  transparent: boolean,
  dimmed: boolean,
): ViewerPartAppearance {
  return viewerPartAppearanceForMaterial(part.material_id, selected, hovered, transparent, dimmed);
}

function writeBoxEdges(
  positions: Float32Array,
  colors: Float32Array | undefined,
  offset: number,
  transform: ViewerPartTransform,
  edgeColor?: Color,
): number {
  const [positionX, positionY, positionZ] = transform.position;
  const [scaleX, scaleY, scaleZ] = transform.scale;
  const minX = positionX - scaleX / 2;
  const maxX = positionX + scaleX / 2;
  const minY = positionY - scaleY / 2;
  const maxY = positionY + scaleY / 2;
  const minZ = positionZ - scaleZ / 2;
  const maxZ = positionZ + scaleZ / 2;
  const corners: readonly [number, number, number][] = [
    [minX, minY, minZ],
    [maxX, minY, minZ],
    [maxX, maxY, minZ],
    [minX, maxY, minZ],
    [minX, minY, maxZ],
    [maxX, minY, maxZ],
    [maxX, maxY, maxZ],
    [minX, maxY, maxZ],
  ];
  let nextOffset = offset;
  for (const [firstCornerIndex, secondCornerIndex] of BOX_EDGE_CORNER_PAIRS) {
    for (const cornerIndex of [firstCornerIndex, secondCornerIndex]) {
      const corner = corners[cornerIndex]!;
      positions[nextOffset] = corner[0];
      if (colors && edgeColor) colors[nextOffset] = edgeColor.r;
      positions[nextOffset + 1] = corner[1];
      if (colors && edgeColor) colors[nextOffset + 1] = edgeColor.g;
      positions[nextOffset + 2] = corner[2];
      if (colors && edgeColor) colors[nextOffset + 2] = edgeColor.b;
      nextOffset += 3;
    }
  }
  return nextOffset;
}

function comparisonEdgeBuffer(
  parts: readonly ResolvedPart[],
  designSize: FurnitureViewerProps["designSize"],
  exploded: boolean,
): Float32Array {
  const floatsPerPart = BOX_EDGE_SEGMENTS_PER_PART * 2 * 3;
  const positions = new Float32Array(parts.length * floatsPerPart);
  let offset = 0;
  for (const part of parts) {
    offset = writeBoxEdges(positions, undefined, offset, viewerPartTransform(part, designSize, exploded));
  }
  return positions;
}

/** Builds at most two non-interactive merged line buffers for a comparison. */
export function buildComparisonGhostRenderData({
  sourceParts,
  proposedParts,
  sourceDesignSize,
  proposedDesignSize,
  exploded,
}: {
  sourceParts: readonly ResolvedPart[];
  proposedParts: readonly ResolvedPart[];
  sourceDesignSize: FurnitureViewerProps["designSize"];
  proposedDesignSize: FurnitureViewerProps["designSize"];
  exploded: boolean;
}): FurnitureComparisonGhostRenderData {
  const classification = classifyComparisonParts(
    sourceParts,
    sourceDesignSize,
    proposedParts,
    proposedDesignSize,
  );
  const sourceGhostParts = [
    ...classification.removed.map((entry) => entry.sourcePart!),
    ...classification.changed.map((entry) => entry.sourcePart!),
  ];
  const proposedGhostParts = [
    ...classification.changed.map((entry) => entry.proposedPart!),
    ...classification.added.map((entry) => entry.proposedPart!),
  ];
  const sourceEdgePositions = comparisonEdgeBuffer(sourceGhostParts, sourceDesignSize, exploded);
  const proposedEdgePositions = comparisonEdgeBuffer(proposedGhostParts, proposedDesignSize, exploded);
  return {
    classification,
    sourceEdgePositions,
    proposedEdgePositions,
    sourcePartCount: sourceGhostParts.length,
    proposedPartCount: proposedGhostParts.length,
    drawBufferCount: Number(sourceGhostParts.length > 0) + Number(proposedGhostParts.length > 0),
  };
}

function buildViewerPartGeometryData({
  parts,
  designSize,
  selectedPartId,
  hoveredPartId,
  exploded,
  transparent,
  isolateSelection,
}: {
  parts: readonly ResolvedPart[];
  designSize: FurnitureViewerProps["designSize"];
  selectedPartId?: string;
  hoveredPartId?: string;
  exploded: boolean;
  transparent: boolean;
  isolateSelection: boolean;
}): ViewerPartGeometryData {
  const floatsPerPart = BOX_EDGE_SEGMENTS_PER_PART * 2 * 3;
  const edgePositions = new Float32Array(parts.length * floatsPerPart);
  const edgeColors = new Float32Array(parts.length * floatsPerPart);
  const partIds = new Set<string>();
  const records: ViewerPartRenderRecord[] = [];
  const edgeColor = new Color();
  let edgeOffset = 0;

  for (const part of parts) {
    if (partIds.has(part.part_id)) {
      throw new Error(`Duplicate viewer part id: ${part.part_id}`);
    }
    partIds.add(part.part_id);
    const selected = part.part_id === selectedPartId;
    const dimmed = isolateSelection && Boolean(selectedPartId) && !selected;
    const appearance = viewerPartAppearance(
      part,
      selected,
      part.part_id === hoveredPartId,
      transparent,
      dimmed,
    );
    const transform = viewerPartTransform(part, designSize, exploded);
    records.push({ part, transform, appearance });
    edgeColor.set(appearance.edgeColor);
    edgeOffset = writeBoxEdges(edgePositions, edgeColors, edgeOffset, transform, edgeColor);
  }

  return {
    records,
    edgePositions,
    edgeColors,
    edgeSegmentCount: parts.length * BOX_EDGE_SEGMENTS_PER_PART,
    partCount: parts.length,
  };
}

export function buildInstancedPartRenderData(options: {
  parts: readonly ResolvedPart[];
  designSize: FurnitureViewerProps["designSize"];
  selectedPartId?: string;
  hoveredPartId?: string;
  exploded: boolean;
  transparent: boolean;
  isolateSelection: boolean;
}): InstancedViewerRenderData {
  const geometryData = buildViewerPartGeometryData(options);
  const batchesByKey = new Map<string, InstancedViewerBatch>();

  for (const { part, transform, appearance } of geometryData.records) {
    const key = JSON.stringify([part.material_id, appearance.opacity, appearance.castShadow]);
    let batch = batchesByKey.get(key);
    if (!batch) {
      batch = {
        key,
        materialId: part.material_id,
        materialVisual: appearance.materialVisual,
        opacity: appearance.opacity,
        transparent: appearance.transparent,
        depthWrite: appearance.depthWrite,
        castShadow: appearance.castShadow,
        receiveShadow: true,
        instances: [],
      };
      batchesByKey.set(key, batch);
    }
    batch.instances.push({
      partId: part.part_id,
      part,
      transform,
      color: appearance.color,
    });
  }

  return {
    batches: [...batchesByKey.values()],
    edgePositions: geometryData.edgePositions,
    edgeColors: geometryData.edgeColors,
    edgeSegmentCount: geometryData.edgeSegmentCount,
    partCount: geometryData.partCount,
  };
}

function sortableTransparentMaterialKey(
  materialId: string,
  appearance: ViewerPartAppearance,
): string {
  return JSON.stringify([
    materialId,
    appearance.color,
    appearance.opacity,
    appearance.transparent,
    appearance.depthWrite,
  ]);
}

function uniqueViewerMaterialIds(parts: readonly ResolvedPart[]): string[] {
  return [...new Set(parts.map((part) => part.material_id))].sort();
}

export function buildSortableTransparentMaterialCatalog(
  materialIds: readonly string[],
): SortableTransparentMaterialBucket[] {
  const buckets = new Map<string, SortableTransparentMaterialBucket>();
  for (const materialId of [...new Set(materialIds)].sort()) {
    const appearances = [
      viewerPartAppearanceForMaterial(materialId, false, false, true, false),
      viewerPartAppearanceForMaterial(materialId, false, true, true, false),
      viewerPartAppearanceForMaterial(materialId, true, false, true, false),
      viewerPartAppearanceForMaterial(materialId, false, false, true, true),
      viewerPartAppearanceForMaterial(materialId, false, true, true, true),
    ];
    for (const appearance of appearances) {
      const key = sortableTransparentMaterialKey(materialId, appearance);
      if (buckets.has(key)) continue;
      buckets.set(key, {
        key,
        materialId,
        color: appearance.color,
        roughness: appearance.materialVisual.roughness,
        metalness: appearance.materialVisual.metalness,
        opacity: appearance.opacity,
        transparent: true,
        depthWrite: appearance.depthWrite,
      });
    }
  }
  return [...buckets.values()];
}

export function buildSortableTransparentPartRenderData(options: {
  parts: readonly ResolvedPart[];
  designSize: FurnitureViewerProps["designSize"];
  selectedPartId?: string;
  hoveredPartId?: string;
  exploded: boolean;
  isolateSelection: boolean;
}): SortableTransparentPartRenderData {
  const geometryData = buildViewerPartGeometryData({ ...options, transparent: true });
  const materialBuckets = buildSortableTransparentMaterialCatalog(
    uniqueViewerMaterialIds(options.parts),
  );
  const materialKeys = new Set(materialBuckets.map((bucket) => bucket.key));
  const objects = geometryData.records.map(({ part, transform, appearance }) => {
    const materialKey = sortableTransparentMaterialKey(part.material_id, appearance);
    if (!materialKeys.has(materialKey)) {
      throw new Error(`Missing transparent material bucket for viewer part: ${part.part_id}`);
    }
    return {
      partId: part.part_id,
      part,
      transform,
      materialKey,
      castShadow: appearance.castShadow,
      receiveShadow: true as const,
    };
  });

  return {
    objects,
    materialBuckets,
    edgePositions: geometryData.edgePositions,
    edgeColors: geometryData.edgeColors,
    edgeSegmentCount: geometryData.edgeSegmentCount,
    partCount: geometryData.partCount,
  };
}

export function partForInstancedBatch(
  batch: InstancedViewerBatch,
  instanceId: number | undefined,
): ResolvedPart | undefined {
  if (instanceId === undefined || !Number.isInteger(instanceId) || instanceId < 0) return undefined;
  return batch.instances[instanceId]?.part;
}

export function hoveredPartAfterInstanceOut(
  currentPartId: string | undefined,
  leavingPartId: string,
): string | undefined {
  return currentPartId === leavingPartId ? undefined : currentPartId;
}

function CameraRig({ viewMode, resetToken }: { viewMode: ViewMode; resetToken: number }) {
  const perspectiveRef = useRef<ThreePerspectiveCamera>(null);
  const frontRef = useRef<ThreeOrthographicCamera>(null);
  const sideRef = useRef<ThreeOrthographicCamera>(null);
  const topRef = useRef<ThreeOrthographicCamera>(null);
  const initializedViewsRef = useRef<Set<ViewMode>>(new Set());
  const previousViewRef = useRef<ViewMode | undefined>(undefined);
  const previousResetTokenRef = useRef(resetToken);
  const targetsRef = useRef<Map<ViewMode, Vector3>>(new Map());
  const { get, set, size } = useThree();
  const bounds = useBounds();

  useEffect(() => {
    const aspect = size.width / Math.max(size.height, 1);
    if (perspectiveRef.current) {
      perspectiveRef.current.aspect = aspect;
      perspectiveRef.current.updateProjectionMatrix();
    }
    for (const camera of [frontRef.current, sideRef.current, topRef.current]) {
      if (!camera) continue;
      camera.left = -aspect;
      camera.right = aspect;
      camera.top = 1;
      camera.bottom = -1;
      camera.updateProjectionMatrix();
    }
  }, [size.height, size.width]);

  useLayoutEffect(() => {
    const cameras = {
      perspective: perspectiveRef.current,
      front: frontRef.current,
      side: sideRef.current,
      top: topRef.current,
    } satisfies Record<ViewMode, ThreePerspectiveCamera | ThreeOrthographicCamera | null>;
    const camera = cameras[viewMode];
    if (!camera) return;

    const controlsBeforeSwitch = get().controls as { target?: Vector3 } | null;
    if (previousViewRef.current && controlsBeforeSwitch?.target) {
      targetsRef.current.set(previousViewRef.current, controlsBeforeSwitch.target.clone());
    }

    const resetRequested = previousResetTokenRef.current !== resetToken;
    const fitCamera = shouldFitViewCamera(initializedViewsRef.current, viewMode, resetRequested);
    previousResetTokenRef.current = resetToken;
    previousViewRef.current = viewMode;
    set({ camera });

    if (fitCamera) {
      camera.up.set(0, 1, 0);
      const [cameraX, cameraY, cameraZ] = cameraPositionForView(viewMode);
      camera.position.set(cameraX, cameraY, cameraZ);
      if (camera instanceof ThreeOrthographicCamera) camera.zoom = 150;
      if (viewMode === "top") camera.up.set(0, 0, -1);
      camera.lookAt(0, 0, 0);
      camera.updateProjectionMatrix();
      initializedViewsRef.current.add(viewMode);
    }

    const frame = requestAnimationFrame(() => {
      const controls = get().controls as { target?: Vector3; update?: () => void } | null;
      const target = targetsRef.current.get(viewMode);
      if (!fitCamera && target && controls?.target) controls.target.copy(target);
      controls?.update?.();
      if (fitCamera) bounds.refresh().clip().fit();
    });
    return () => cancelAnimationFrame(frame);
  }, [bounds, get, resetToken, set, viewMode]);

  return (
    <>
      <perspectiveCamera ref={perspectiveRef} near={0.01} far={100} fov={38} />
      <orthographicCamera ref={frontRef} near={0.01} far={100} zoom={150} />
      <orthographicCamera ref={sideRef} near={0.01} far={100} zoom={150} />
      <orthographicCamera ref={topRef} near={0.01} far={100} zoom={150} />
    </>
  );
}

function RoomStage({ heightMm, presentation = "studio" }: {
  heightMm: number;
  presentation?: FurnitureViewerProps["presentation"];
}) {
  const floorY = -heightMm / 2_000 - 0.025;
  const quiet = presentation === "production";
  return (
    <group>
      <mesh position={[0, floorY - 0.015, -1.35]} rotation={[-Math.PI / 2, 0, 0]} receiveShadow>
        <planeGeometry args={[14, 12]} />
        <meshStandardMaterial color={quiet ? "#eeeae2" : "#e8e2d7"} roughness={0.98} />
      </mesh>
      <mesh position={[0, floorY + 3.6, -2.25]} receiveShadow>
        <planeGeometry args={[14, 8]} />
        <meshStandardMaterial color={quiet ? "#f7f5f0" : "#eeeae2"} roughness={1} />
      </mesh>
      <mesh position={[-4.6, floorY + 3.6, 0]} rotation={[0, Math.PI / 2, 0]} receiveShadow>
        <planeGeometry args={[8, 8]} />
        <meshStandardMaterial color="#e9e5dc" roughness={1} />
      </mesh>
    </group>
  );
}

function PartMesh({
  part,
  designSize,
  selected,
  exploded,
  transparent,
  dimmed,
  onSelect,
  onPrepareMove,
}: {
  part: ResolvedPart;
  designSize: FurnitureViewerProps["designSize"];
  selected: boolean;
  exploded: boolean;
  transparent: boolean;
  dimmed: boolean;
  onSelect: (partId: string) => void;
  onPrepareMove?: (part: ResolvedPart, clientX: number, clientY: number) => void;
}) {
  const [hovered, setHovered] = useState(false);
  const transform = viewerPartTransform(part, designSize, exploded);
  const appearance = viewerPartAppearance(part, selected, hovered, transparent, dimmed);

  const handleSelect = (event: ThreeEvent<MouseEvent>) => {
    event.stopPropagation();
    onSelect(part.part_id);
  };

  return (
    <mesh
      position={transform.position}
      castShadow={appearance.castShadow}
      receiveShadow
      onClick={handleSelect}
      onPointerDown={(event) => {
        if (!onPrepareMove) return;
        event.stopPropagation();
        onSelect(part.part_id);
        onPrepareMove(part, event.clientX, event.clientY);
      }}
      onPointerOver={(event) => {
        event.stopPropagation();
        setHovered(true);
      }}
      onPointerOut={() => setHovered(false)}
    >
      <boxGeometry args={partSize(part)} />
      <meshStandardMaterial
        color={appearance.color}
        roughness={appearance.materialVisual.roughness}
        metalness={appearance.materialVisual.metalness}
        transparent={appearance.transparent}
        opacity={appearance.opacity}
        depthWrite={appearance.depthWrite}
      />
      <Edges color={appearance.edgeColor} threshold={18} />
    </mesh>
  );
}

function InstancedPartBatchMesh({
  batch,
  onSelect,
  canPrepareMove,
  onPrepareMove,
  onHoverPart,
  onLeavePart,
}: {
  batch: InstancedViewerBatch;
  onSelect: (partId: string) => void;
  canPrepareMove: (part: ResolvedPart) => boolean;
  onPrepareMove: (part: ResolvedPart, clientX: number, clientY: number) => void;
  onHoverPart: (partId?: string) => void;
  onLeavePart: (partId: string) => void;
}) {
  const meshRef = useRef<ThreeInstancedMesh>(null);
  const invalidate = useThree((state) => state.invalidate);

  useLayoutEffect(() => {
    const mesh = meshRef.current;
    if (!mesh) return;
    const matrix = new Matrix4();
    const color = new Color();
    mesh.count = batch.instances.length;
    mesh.instanceMatrix.setUsage(DynamicDrawUsage);
    batch.instances.forEach((instance, instanceId) => {
      matrix.makeScale(...instance.transform.scale);
      matrix.setPosition(...instance.transform.position);
      mesh.setMatrixAt(instanceId, matrix);
      mesh.setColorAt(instanceId, color.set(instance.color));
    });
    mesh.instanceMatrix.needsUpdate = true;
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    mesh.computeBoundingBox();
    mesh.computeBoundingSphere();
    invalidate();
  }, [batch.instances, invalidate]);

  return (
    <instancedMesh
      ref={meshRef}
      args={[undefined, undefined, batch.instances.length]}
      castShadow={batch.castShadow}
      receiveShadow={batch.receiveShadow}
      onClick={(event) => {
        const part = partForInstancedBatch(batch, event.instanceId);
        if (!part) return;
        event.stopPropagation();
        onSelect(part.part_id);
      }}
      onPointerDown={(event) => {
        const part = partForInstancedBatch(batch, event.instanceId);
        if (!part || !canPrepareMove(part)) return;
        event.stopPropagation();
        onSelect(part.part_id);
        onPrepareMove(part, event.clientX, event.clientY);
      }}
      onPointerOver={(event) => {
        const part = partForInstancedBatch(batch, event.instanceId);
        if (!part) return;
        event.stopPropagation();
        onHoverPart(part.part_id);
      }}
      onPointerMove={(event) => {
        const part = partForInstancedBatch(batch, event.instanceId);
        if (!part) return;
        event.stopPropagation();
        onHoverPart(part.part_id);
      }}
      onPointerOut={(event) => {
        const part = partForInstancedBatch(batch, event.instanceId);
        if (part) onLeavePart(part.part_id);
      }}
    >
      <boxGeometry args={[1, 1, 1]} />
      <meshStandardMaterial
        color="#ffffff"
        roughness={batch.materialVisual.roughness}
        metalness={batch.materialVisual.metalness}
        transparent={batch.transparent}
        opacity={batch.opacity}
        depthWrite={batch.depthWrite}
      />
    </instancedMesh>
  );
}

function MergedPartEdges({
  edgePositions,
  edgeColors,
}: Pick<InstancedViewerRenderData, "edgePositions" | "edgeColors">) {
  return (
    <lineSegments frustumCulled={false} raycast={() => undefined}>
      <bufferGeometry>
        <bufferAttribute attach="attributes-position" args={[edgePositions, 3]} />
        <bufferAttribute attach="attributes-color" args={[edgeColors, 3]} />
      </bufferGeometry>
      <lineBasicMaterial vertexColors />
    </lineSegments>
  );
}

function ComparisonGhostLine({
  positions,
  style,
}: {
  positions: Float32Array;
  style: (typeof COMPARISON_GHOST_LINE_STYLES)[keyof typeof COMPARISON_GHOST_LINE_STYLES];
}) {
  const lineRef = useRef<ThreeLineSegments>(null);
  const invalidate = useThree((state) => state.invalidate);

  useLayoutEffect(() => {
    const line = lineRef.current;
    if (!line) return;
    line.computeLineDistances();
    invalidate();
  }, [invalidate]);

  return (
    <lineSegments ref={lineRef} frustumCulled={false} raycast={() => undefined}>
      <bufferGeometry>
        <bufferAttribute attach="attributes-position" args={[positions, 3]} />
      </bufferGeometry>
      <lineDashedMaterial
        color={style.color}
        dashSize={style.dashSize}
        gapSize={style.gapSize}
        depthTest={false}
        transparent
        opacity={0.96}
      />
    </lineSegments>
  );
}

function ComparisonGhost({
  currentParts,
  currentDesignSize,
  preview,
  exploded,
}: {
  currentParts: readonly ResolvedPart[];
  currentDesignSize: FurnitureViewerProps["designSize"];
  preview: FurnitureComparisonPreview;
  exploded: boolean;
}) {
  const renderData = useMemo(() => buildComparisonGhostRenderData({
    sourceParts: currentParts,
    proposedParts: preview.proposedParts,
    sourceDesignSize: currentDesignSize,
    proposedDesignSize: preview.designSize,
    exploded,
  }), [currentDesignSize, currentParts, exploded, preview.designSize, preview.proposedParts]);

  return (
    <group name="validation-comparison-ghost">
      {renderData.sourceEdgePositions.length > 0 ? (
        <ComparisonGhostLine
          key={`source-${comparisonGhostBufferResourceKey(renderData.sourceEdgePositions)}`}
          positions={renderData.sourceEdgePositions}
          style={COMPARISON_GHOST_LINE_STYLES.source}
        />
      ) : null}
      {renderData.proposedEdgePositions.length > 0 ? (
        <ComparisonGhostLine
          key={`proposed-${comparisonGhostBufferResourceKey(renderData.proposedEdgePositions)}`}
          positions={renderData.proposedEdgePositions}
          style={COMPARISON_GHOST_LINE_STYLES.proposed}
        />
      ) : null}
    </group>
  );
}

function InstancedParts({
  parts,
  designSize,
  selectedPartId,
  exploded,
  transparent,
  isolateSelection,
  onSelect,
  canPrepareMove,
  onPrepareMove,
}: Pick<
  FurnitureViewerProps,
  "parts" | "designSize" | "selectedPartId" | "exploded" | "transparent" | "isolateSelection"
> & {
  onSelect: (partId: string) => void;
  canPrepareMove: (part: ResolvedPart) => boolean;
  onPrepareMove: (part: ResolvedPart, clientX: number, clientY: number) => void;
}) {
  const [hoveredPartId, setHoveredPartId] = useState<string>();
  const { depthMm, heightMm, widthMm } = designSize;
  const renderData = useMemo(() => buildInstancedPartRenderData({
    parts,
    designSize: { depthMm, heightMm, widthMm },
    selectedPartId,
    hoveredPartId,
    exploded,
    transparent,
    isolateSelection,
  }), [
    depthMm,
    exploded,
    heightMm,
    hoveredPartId,
    isolateSelection,
    parts,
    selectedPartId,
    transparent,
    widthMm,
  ]);

  return (
    <>
      {renderData.batches.map((batch) => (
        <InstancedPartBatchMesh
          key={batch.key}
          batch={batch}
          onSelect={onSelect}
          canPrepareMove={canPrepareMove}
          onPrepareMove={onPrepareMove}
          onHoverPart={setHoveredPartId}
          onLeavePart={(partId) => {
            setHoveredPartId((currentPartId) => hoveredPartAfterInstanceOut(currentPartId, partId));
          }}
        />
      ))}
      <MergedPartEdges
        edgePositions={renderData.edgePositions}
        edgeColors={renderData.edgeColors}
      />
    </>
  );
}

function SortableTransparentPartMesh({
  object,
  geometry,
  material,
  onSelect,
  canPrepareMove,
  onPrepareMove,
  onHoverPart,
  onLeavePart,
}: {
  object: SortableTransparentPartObject;
  geometry: BoxGeometry;
  material: MeshStandardMaterial;
  onSelect: (partId: string) => void;
  canPrepareMove: (part: ResolvedPart) => boolean;
  onPrepareMove: (part: ResolvedPart, clientX: number, clientY: number) => void;
  onHoverPart: (partId: string) => void;
  onLeavePart: (partId: string) => void;
}) {
  const { part, partId, transform } = object;
  return (
    <mesh
      geometry={geometry}
      material={material}
      position={transform.position}
      scale={transform.scale}
      castShadow={object.castShadow}
      receiveShadow={object.receiveShadow}
      dispose={null}
      onClick={(event) => {
        event.stopPropagation();
        onSelect(partId);
      }}
      onPointerDown={(event) => {
        if (!canPrepareMove(part)) return;
        event.stopPropagation();
        onSelect(partId);
        onPrepareMove(part, event.clientX, event.clientY);
      }}
      onPointerOver={(event) => {
        event.stopPropagation();
        onHoverPart(partId);
      }}
      onPointerMove={(event) => {
        event.stopPropagation();
        onHoverPart(partId);
      }}
      onPointerOut={() => onLeavePart(partId)}
    />
  );
}

function SortableTransparentParts({
  parts,
  designSize,
  selectedPartId,
  exploded,
  isolateSelection,
  onSelect,
  canPrepareMove,
  onPrepareMove,
}: Pick<
  FurnitureViewerProps,
  "parts" | "designSize" | "selectedPartId" | "exploded" | "isolateSelection"
> & {
  onSelect: (partId: string) => void;
  canPrepareMove: (part: ResolvedPart) => boolean;
  onPrepareMove: (part: ResolvedPart, clientX: number, clientY: number) => void;
}) {
  const [hoveredPartId, setHoveredPartId] = useState<string>();
  const { depthMm, heightMm, widthMm } = designSize;
  const renderData = useMemo(() => buildSortableTransparentPartRenderData({
    parts,
    designSize: { depthMm, heightMm, widthMm },
    selectedPartId,
    hoveredPartId,
    exploded,
    isolateSelection,
  }), [
    depthMm,
    exploded,
    heightMm,
    hoveredPartId,
    isolateSelection,
    parts,
    selectedPartId,
    widthMm,
  ]);
  const materialSignature = JSON.stringify(uniqueViewerMaterialIds(parts));
  const geometry = useMemo(() => new BoxGeometry(1, 1, 1), []);
  const materials = useMemo(() => {
    const materialIds = JSON.parse(materialSignature) as string[];
    return new Map(buildSortableTransparentMaterialCatalog(materialIds).map((bucket) => [
      bucket.key,
      new MeshStandardMaterial({
        color: bucket.color,
        roughness: bucket.roughness,
        metalness: bucket.metalness,
        transparent: bucket.transparent,
        opacity: bucket.opacity,
        depthWrite: bucket.depthWrite,
      }),
    ] as const));
  }, [materialSignature]);

  useEffect(() => () => geometry.dispose(), [geometry]);
  useEffect(() => () => {
    for (const material of materials.values()) material.dispose();
  }, [materials]);

  return (
    <>
      {renderData.objects.map((object) => {
        const material = materials.get(object.materialKey);
        if (!material) throw new Error(`Missing transparent material resource: ${object.materialKey}`);
        return (
          <SortableTransparentPartMesh
            key={object.partId}
            object={object}
            geometry={geometry}
            material={material}
            onSelect={onSelect}
            canPrepareMove={canPrepareMove}
            onPrepareMove={onPrepareMove}
            onHoverPart={setHoveredPartId}
            onLeavePart={(partId) => {
              setHoveredPartId((currentPartId) => hoveredPartAfterInstanceOut(currentPartId, partId));
            }}
          />
        );
      })}
      <MergedPartEdges
        edgePositions={renderData.edgePositions}
        edgeColors={renderData.edgeColors}
      />
    </>
  );
}

interface ActivePartMove {
  partId: string;
  axis: "horizontal" | "vertical";
}

function Scene(props: FurnitureViewerProps & {
  onPartMoveActivity?: (activity?: ActivePartMove) => void;
}) {
  const [movingPartId, setMovingPartId] = useState<string>();
  const modelRootRef = useRef<ThreeGroup>(null);
  const partDrag = useRef<{
    part: ResolvedPart;
    axis: "horizontal" | "vertical";
    startX: number;
    startY: number;
    startPositionMm: number;
    lastEmittedPositionMm: number;
    started: boolean;
  } | undefined>(undefined);
  const { gl, invalidate, size } = useThree();
  const designHeightMm = props.designSize.heightMm;
  const designWidthMm = props.designSize.widthMm;
  const onPartMove = props.onPartMove;
  const onPartMoveStart = props.onPartMoveStart;
  const onPartMoveEnd = props.onPartMoveEnd;
  const onPartHorizontalMove = props.onPartHorizontalMove;
  const onPartHorizontalMoveStart = props.onPartHorizontalMoveStart;
  const onPartHorizontalMoveEnd = props.onPartHorizontalMoveEnd;
  const onPartMoveActivity = props.onPartMoveActivity;

  useLayoutEffect(() => {
    const modelRoot = modelRootRef.current;
    if (!modelRoot) return;
    return exposeViewerModelRoot(gl.domElement, modelRoot.uuid);
  }, [gl]);

  useEffect(() => {
    if (!movingPartId) return;
    const move = (event: PointerEvent) => {
      const current = partDrag.current;
      if (!current) return;
      const deltaX = event.clientX - current.startX;
      const deltaY = event.clientY - current.startY;
      if (!current.started) {
        if (!partDragThresholdReached(deltaX, deltaY)) return;
        current.started = true;
        if (current.axis === "horizontal") onPartHorizontalMoveStart?.(current.part.part_id);
        else onPartMoveStart?.(current.part.part_id);
        if (current.part.kind === "divider" || current.part.kind === "shelf") {
          onPartMoveActivity?.({ partId: current.part.part_id, axis: current.axis });
        }
      }

      if (current.axis === "horizontal") {
        const halfThickness = current.part.thickness_mm / 2;
        const nextPositionMm = horizontalPositionAfterDrag(
          current.startPositionMm,
          deltaX,
          designWidthMm,
          size.width,
          halfThickness,
          designWidthMm - halfThickness,
        );
        if (nextPositionMm === current.lastEmittedPositionMm) return;
        current.lastEmittedPositionMm = nextPositionMm;
        onPartHorizontalMove?.(current.part.part_id, nextPositionMm);
        return;
      }
      const { minZMm, maxZMm } = verticalDragBoundsForPart(current.part, designHeightMm);
      const nextPositionMm = verticalPositionAfterDrag(
        current.startPositionMm,
        deltaY,
        designHeightMm,
        size.height,
        minZMm,
        maxZMm,
      );
      if (nextPositionMm === current.lastEmittedPositionMm) return;
      current.lastEmittedPositionMm = nextPositionMm;
      onPartMove?.(current.part.part_id, nextPositionMm);
    };
    const finish = () => {
      const current = partDrag.current;
      partDrag.current = undefined;
      setMovingPartId(undefined);
      onPartMoveActivity?.(undefined);
      if (!current?.started) return;
      if (current.axis === "horizontal") onPartHorizontalMoveEnd?.();
      else onPartMoveEnd?.();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", finish, { once: true });
    window.addEventListener("pointercancel", finish, { once: true });
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", finish);
      window.removeEventListener("pointercancel", finish);
    };
  }, [
    designHeightMm,
    designWidthMm,
    movingPartId,
    onPartHorizontalMove,
    onPartHorizontalMoveEnd,
    onPartHorizontalMoveStart,
    onPartMove,
    onPartMoveActivity,
    onPartMoveEnd,
    onPartMoveStart,
    size.height,
    size.width,
  ]);

  const preparePartMove = (part: ResolvedPart, clientX: number, clientY: number) => {
    const axis = partSupportsHorizontalDrag(part) ? "horizontal" : "vertical";
    partDrag.current = {
      part,
      axis,
      startX: clientX,
      startY: clientY,
      startPositionMm: axis === "horizontal" ? part.position_mm.x : part.position_mm.z,
      lastEmittedPositionMm: axis === "horizontal" ? part.position_mm.x : part.position_mm.z,
      started: false,
    };
    setMovingPartId(part.part_id);
  };

  const canPreparePartMove = (part: ResolvedPart) => {
    if (props.exploded) return false;
    return partSupportsHorizontalDrag(part)
      ? Boolean(props.onPartHorizontalMove)
      : partSupportsVerticalDrag(part, props.parts) && Boolean(props.onPartMove);
  };
  const orbitPerformanceProps = viewerOrbitControlsPerformanceProps(props.parts.length);
  const partRenderMode = viewerPartRenderMode(props.parts.length, props.transparent);

  return (
    <>
      <ViewerRenderCommitProbe />
      <ViewerDemandInvalidator
        cameraResetNonce={props.cameraResetNonce}
        comparisonPreview={props.comparisonPreview}
        designSize={props.designSize}
        exploded={props.exploded}
        invalidate={invalidate}
        isolateSelection={props.isolateSelection}
        parts={props.parts}
        presentation={props.presentation}
        selectedPartId={props.selectedPartId}
        transparent={props.transparent}
        viewMode={props.viewMode}
      />
      <color attach="background" args={["#eeeae2"]} />
      <hemisphereLight args={["#fffaf0", "#9f978a", 1.35]} />
      <directionalLight
        position={[3.8, 5.8, 4.5]}
        intensity={2.4}
        castShadow
        shadow-mapSize={[2_048, 2_048]}
        shadow-bias={-0.00015}
      />
      <directionalLight position={[-4.5, 2.8, 1.2]} intensity={0.72} color="#d9e2e2" />
      <pointLight position={[0, 1.8, -1.5]} intensity={0.42} color="#f4dfbd" distance={7} />
      <RoomStage heightMm={props.designSize.heightMm} presentation={props.presentation} />
      <Bounds fit clip margin={1.35}>
        <group ref={modelRootRef}>
          {partRenderMode === "sortable-transparent" ? (
            <SortableTransparentParts
              parts={props.parts}
              designSize={props.designSize}
              selectedPartId={props.selectedPartId}
              exploded={props.exploded}
              isolateSelection={props.isolateSelection}
              onSelect={props.onSelectPart}
              canPrepareMove={canPreparePartMove}
              onPrepareMove={preparePartMove}
            />
          ) : partRenderMode === "instanced" ? (
            <InstancedParts
              parts={props.parts}
              designSize={props.designSize}
              selectedPartId={props.selectedPartId}
              exploded={props.exploded}
              transparent={props.transparent}
              isolateSelection={props.isolateSelection}
              onSelect={props.onSelectPart}
              canPrepareMove={canPreparePartMove}
              onPrepareMove={preparePartMove}
            />
          ) : props.parts.map((part) => {
            const selected = part.part_id === props.selectedPartId;
            return (
              <PartMesh
                key={part.part_id}
                part={part}
                designSize={props.designSize}
                selected={selected}
                exploded={props.exploded}
                transparent={props.transparent}
                dimmed={props.isolateSelection && Boolean(props.selectedPartId) && !selected}
                onSelect={(partId) => props.onSelectPart(partId)}
                onPrepareMove={canPreparePartMove(part) ? preparePartMove : undefined}
              />
            );
          })}
        </group>
        <CameraRig viewMode={props.viewMode} resetToken={props.cameraResetNonce ?? 0} />
      </Bounds>
      {props.comparisonPreview ? (
        <ComparisonGhost
          currentParts={props.parts}
          currentDesignSize={props.designSize}
          preview={props.comparisonPreview}
          exploded={props.exploded}
        />
      ) : null}
      <ContactShadows
        position={[0, -props.designSize.heightMm / 2_000 - 0.02, 0]}
        opacity={0.24}
        scale={8}
        blur={2.4}
        far={4}
      />
      <OrbitControls
        makeDefault
        {...orbitPerformanceProps}
        enabled={!movingPartId}
        enableRotate={props.viewMode === "perspective"}
        minDistance={0.4}
        maxDistance={10}
        zoomSpeed={0.8}
        panSpeed={0.65}
      />
      <GizmoHelper alignment="bottom-right" margin={[62, 54]}>
        <GizmoViewport axisColors={["#c85757", "#39845f", "#4774ad"]} labelColor="#eef4f0" />
      </GizmoHelper>
    </>
  );
}

function semanticPointerRequest(
  event: DragEvent<HTMLDivElement>,
  kind: SemanticComponentKind,
): SemanticDropRequest {
  const rect = event.currentTarget.getBoundingClientRect();
  return {
    kind,
    normalizedX: rect.width > 0 ? (event.clientX - rect.left) / rect.width : 0.5,
    normalizedY: rect.height > 0 ? (event.clientY - rect.top) / rect.height : 0.5,
  };
}

export function SnapPreview({ preview }: { preview: SemanticSnapPreview }) {
  const lineClass = preview.kind === "shelf_row"
    ? styles.snapLineHorizontal
    : preview.kind === "divider"
      ? styles.snapLineVertical
      : preview.kind === "back_panel"
        ? styles.snapBack
        : preview.kind === "base_cabinet"
          ? styles.snapCabinet
          : styles.snapPlinth;
  return (
    <div className={styles.snapPreview} aria-hidden="true">
      <span className={styles.snapLabel}><strong>{preview.label}</strong><small>{preview.detail}</small></span>
      {preview.kind === "shelf_row" || preview.kind === "divider"
        ? (
            <svg
              className={styles.snapLines}
              viewBox="0 0 100 100"
              preserveAspectRatio="none"
              focusable="false"
            >
              {preview.normalizedPositions.map((position) => {
                const horizontal = preview.kind === "shelf_row";
                const coordinate = (horizontal ? 1 - position : position) * 100;
                const line = horizontal
                  ? { x1: 10, x2: 90, y1: coordinate, y2: coordinate }
                  : { x1: coordinate, x2: coordinate, y1: 8, y2: 92 };
                return (
                  <g key={position} className={lineClass}>
                    <line {...line} className={styles.snapLineSurface} vectorEffect="non-scaling-stroke" />
                    <line {...line} className={styles.snapLineEdge} vectorEffect="non-scaling-stroke" />
                  </g>
                );
              })}
            </svg>
          )
        : <span className={lineClass} />}
    </div>
  );
}

export function centeredFrontProjectionRect(
  part: ResolvedPart,
  canvasSize: Pick<FurnitureViewerProps["designSize"], "widthMm" | "heightMm">,
  partDesignSize: Pick<FurnitureViewerProps["designSize"], "widthMm" | "heightMm">,
) {
  const [partWidthM, partHeightM] = partSize(part);
  const partWidth = partWidthM * 1_000;
  const partHeight = partHeightM * 1_000;
  return {
    x: canvasSize.widthMm / 2 + part.position_mm.x - partDesignSize.widthMm / 2 - partWidth / 2,
    y: canvasSize.heightMm / 2 - (part.position_mm.z - partDesignSize.heightMm / 2) - partHeight / 2,
    width: Math.max(partWidth, 1),
    height: Math.max(partHeight, 1),
  };
}

export function FrontProjectionFallback(props: FurnitureViewerProps) {
  const canvasSize = {
    widthMm: Math.max(props.designSize.widthMm, props.comparisonPreview?.designSize.widthMm ?? 0, 1),
    heightMm: Math.max(props.designSize.heightMm, props.comparisonPreview?.designSize.heightMm ?? 0, 1),
  };
  const orderedParts = [...props.parts].sort((left, right) => {
    if (left.kind === "back") return -1;
    if (right.kind === "back") return 1;
    return left.position_mm.y - right.position_mm.y;
  });
  const comparison = props.comparisonPreview
    ? classifyComparisonParts(
        props.parts,
        props.designSize,
        props.comparisonPreview.proposedParts,
        props.comparisonPreview.designSize,
      )
    : undefined;
  const sourceGhostParts = comparison
    ? [...comparison.removed, ...comparison.changed].map((entry) => entry.sourcePart!)
    : [];
  const proposedGhostParts = comparison
    ? [...comparison.changed, ...comparison.added].map((entry) => entry.proposedPart!)
    : [];
  return (
    <div className="webgl-fallback" data-testid="front-projection-fallback">
      <svg
        viewBox={`0 0 ${canvasSize.widthMm} ${canvasSize.heightMm}`}
        role="img"
        aria-label="Förenklad interaktiv frontvy av möbeln"
        preserveAspectRatio="xMidYMid meet"
      >
        {orderedParts.map((part) => {
          const selected = part.part_id === props.selectedPartId;
          return (
            <rect
              key={part.part_id}
              {...centeredFrontProjectionRect(part, canvasSize, props.designSize)}
              fill={selected ? "#d5b77f" : viewerMaterialVisual(part.material_id).color}
              fillOpacity={props.transparent ? (selected ? 0.76 : 0.4) : 0.92}
              stroke={selected ? "#145c42" : "#574b3b"}
              strokeWidth={selected ? 7 : 3}
              vectorEffect="non-scaling-stroke"
              tabIndex={0}
              aria-label={part.name}
              onClick={() => props.onSelectPart(part.part_id)}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") props.onSelectPart(part.part_id);
              }}
            />
          );
        })}
        {props.comparisonPreview ? (
          <g aria-hidden="true" data-testid="comparison-ghost-outlines" pointerEvents="none">
            {sourceGhostParts.map((part) => (
              <rect
                key={`source-${part.part_id}`}
                {...centeredFrontProjectionRect(part, canvasSize, props.designSize)}
                fill="none"
                stroke={COMPARISON_SOURCE_COLOR}
                strokeWidth={6}
                strokeDasharray="14 8"
                vectorEffect="non-scaling-stroke"
              />
            ))}
            {proposedGhostParts.map((part) => (
              <rect
                key={`proposed-${part.part_id}`}
                {...centeredFrontProjectionRect(part, canvasSize, props.comparisonPreview!.designSize)}
                fill="none"
                stroke={COMPARISON_PROPOSED_COLOR}
                strokeWidth={5}
                strokeDasharray="4 5"
                vectorEffect="non-scaling-stroke"
              />
            ))}
          </g>
        ) : null}
      </svg>
      <p role="status">3D-acceleration saknas. En interaktiv frontvy visas.</p>
      <ManufacturingFeatureOverlay
        parts={props.parts}
        selectedPart={props.parts.find((part) => part.part_id === props.selectedPartId)}
      />
    </div>
  );
}

export function ComparisonLegend({
  preview,
  classification,
}: {
  preview: FurnitureComparisonPreview;
  classification: FurnitureComparisonClassification;
}) {
  const sourceCount = classification.removed.length + classification.changed.length;
  const proposedCount = classification.added.length + classification.changed.length;
  const noGeometryChange = sourceCount === 0 && proposedCount === 0;
  return (
    <aside className={styles.comparisonLegend} role="status" aria-live="polite" aria-atomic="true">
      <strong>Lokalt beräknad förhandsvisning</strong>
      <small>
        {preview.rule.title}. Regel {preview.rule.ruleId} · version {preview.rule.ruleVersion}. Inte serververifierad eller tillämpad.
      </small>
      {noGeometryChange ? (
        <p>Förslaget ändrar ingen geometri. Endast specifikations- eller regelunderlag ändras.</p>
      ) : (
        <dl>
          <div>
            <dt><i className={styles.comparisonSourceMark} aria-hidden="true" />Nuvarande</dt>
            <dd>{sourceCount} {sourceCount === 1 ? "del som ändras eller tas bort" : "delar som ändras eller tas bort"} · streckad ockra kontur</dd>
          </div>
          <div>
            <dt><i className={styles.comparisonProposedMark} aria-hidden="true" />Föreslagen</dt>
            <dd>{proposedCount} {proposedCount === 1 ? "del som ändras eller läggs till" : "delar som ändras eller läggs till"} · prickad turkos kontur</dd>
          </div>
        </dl>
      )}
    </aside>
  );
}

export default function FurnitureViewer(props: FurnitureViewerProps) {
  const [snapPreview, setSnapPreview] = useState<SemanticSnapPreview>();
  const [activePartMove, setActivePartMove] = useState<ActivePartMove>();
  const [webGLAvailable] = useState(browserSupportsWebGL);
  const comparisonClassification = useMemo(() => (
    props.comparisonPreview
      ? classifyComparisonParts(
          props.parts,
          props.designSize,
          props.comparisonPreview.proposedParts,
          props.comparisonPreview.designSize,
        )
      : undefined
  ), [props.comparisonPreview, props.designSize, props.parts]);
  const selectedPart = props.parts.find((part) => part.part_id === props.selectedPartId);
  const movingPart = activePartMove
    ? props.parts.find((part) => part.part_id === activePartMove.partId)
    : undefined;
  const moveFeedback = movingPart
    ? partMoveFeedback(movingPart, props.parts, props.designSize)
    : undefined;
  const selectedPartCanMoveVertically = Boolean(
    selectedPart
      && !props.exploded
      && props.onPartMove
      && partSupportsVerticalDrag(selectedPart, props.parts),
  );
  const selectedPartCanMoveHorizontally = Boolean(
    selectedPart
      && !props.exploded
      && props.onPartHorizontalMove
      && partSupportsHorizontalDrag(selectedPart),
  );

  const handleDragOver = (event: DragEvent<HTMLDivElement>) => {
    if (!props.semanticDropEnabled || !props.semanticDragKind || !props.semanticSpec) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    try {
      setSnapPreview(createSemanticSnapPreview(
        props.semanticSpec,
        semanticPointerRequest(event, props.semanticDragKind),
      ));
    } catch {
      setSnapPreview(undefined);
    }
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    if (!props.semanticDropEnabled || !props.onSemanticDrop) return;
    const kind = readSemanticDragPayload(event.dataTransfer) ?? props.semanticDragKind;
    if (!kind) return;
    event.preventDefault();
    props.onSemanticDrop(semanticPointerRequest(event, kind));
    setSnapPreview(undefined);
  };

  return (
    <div
      className={`${styles.dropSurface} ${snapPreview ? styles.dropSurfaceActive : ""}`}
      onDragOver={handleDragOver}
      onDragLeave={(event) => {
        if (!(event.relatedTarget instanceof Node) || !event.currentTarget.contains(event.relatedTarget)) {
          setSnapPreview(undefined);
        }
      }}
      onDrop={handleDrop}
    >
      <div
        className={`canvas-shell ${props.selectedPartId ? "part-selected" : ""}`}
        aria-label="Interaktiv 3D-modell av möbeln"
        data-testid="furniture-viewer"
        data-renderer={webGLAvailable ? "webgl" : "front-projection"}
      >
        {webGLAvailable ? (
          <Canvas
            camera={initialCameraForView("perspective", false)}
            dpr={[1, 1.75]}
            frameloop={VIEWER_FRAMELOOP}
            shadows
            gl={{ antialias: true, alpha: false, powerPreference: "high-performance" }}
            onPointerMissed={() => props.onSelectPart(undefined)}
          >
            <Suspense fallback={null}>
              <Scene {...props} onPartMoveActivity={setActivePartMove} />
            </Suspense>
          </Canvas>
        ) : (
          <FrontProjectionFallback {...props} />
        )}
        {webGLAvailable && selectedPart ? (
          <div className="canvas-part-label canvas-part-label-overlay">
            {selectedPart.name}
            <small>{Math.round(selectedPart.width_mm)} × {Math.round(selectedPart.depth_mm)} mm</small>
            <em>{selectedPartCanMoveHorizontally
              ? "↔ Dra åt vänster eller höger"
              : selectedPartCanMoveVertically
                ? "↕ Dra uppåt eller nedåt"
                : "Mått och åtgärder finns i sidopanelen"}</em>
          </div>
        ) : null}
        {webGLAvailable ? (
          <ManufacturingFeatureOverlay parts={props.parts} selectedPart={selectedPart} />
        ) : null}
        {moveFeedback ? <PartMoveFeedbackOverlay feedback={moveFeedback} /> : null}
        {props.comparisonPreview && comparisonClassification ? (
          <ComparisonLegend preview={props.comparisonPreview} classification={comparisonClassification} />
        ) : null}
        <div className="canvas-dimensions" aria-label="Aktuella yttermått">
          <span><small>X</small>{props.designSize.widthMm} mm</span>
          <span><small>Y</small>{props.designSize.depthMm} mm</span>
          <span><small>Z</small>{props.designSize.heightMm} mm</span>
        </div>
      </div>
      {props.resizeEnabled ? (
        <DimensionDragOverlay
          designSize={props.designSize}
          onResizeStart={props.onResizeStart}
          onResize={props.onResize}
          onResizeEnd={props.onResizeEnd}
        />
      ) : null}
      {snapPreview ? <SnapPreview preview={snapPreview} /> : null}
    </div>
  );
}

import type { components } from "./api-schema";
import type { ManufacturingFeature, ResolvedPart } from "./design-types";

export type PartInspection = components["schemas"]["PartInspection"];
export type PartView = "whole" | "alone" | "without";
export interface InspectionView {
  key: string;
  mode: PartView;
}

export function visibleAssembly(
  parts: ResolvedPart[],
  selected: string | undefined,
  mode: PartView,
) {
  if (!selected || mode === "whole") return parts;
  return parts.filter(
    (part) => (part.part_id === selected) === (mode === "alone"),
  );
}

export function domainFeatureSummary(
  f: components["schemas"]["ManufacturingFeature"],
  a?: string,
  b?: string,
): ManufacturingFeature {
  const kinds = {
    drill: "drill",
    drill_pattern: "drill",
    countersink: "pocket",
    pocket: "pocket",
    tenon: "pocket",
    groove: "groove",
    rabbet: "rabbet",
    edge_relief: "pocket",
    outer_contour: "outline",
    mark: "label",
  } as const;
  return {
    id: f.feature_id,
    kind: kinds[f.kind],
    face:
      f.face === a || f.face === "a"
        ? "A"
        : f.face === b || f.face === "b"
          ? "B"
          : "EDGE",
    depth_mm: (f.dimensions.depth_um ?? 0) / 1000,
    description: `${f.kind} · ${f.face}${f.pattern_count > 1 ? ` · ${f.pattern_count} lägen` : ""}`,
  };
}

export function checkedInspection(
  value: PartInspection,
  id: string,
  hash: string,
) {
  if (
    value?.physical_cutting_authorized !== false ||
    value.design_hash !== hash ||
    value.part?.part_id !== id ||
    !value.drawings?.length ||
    !Array.isArray(value.mates)
  ) {
    throw new Error(
      "Delbladets identitet stämmer inte med den valda modellen.",
    );
  }
  return value;
}

export interface DrillMarker {
  key: string;
  center: [number, number, number];
  rotation: [number, number, number];
  radius: number;
}

/** Convert measured drawing U/V to the viewer's centred X/Z/-Y frame, in metres. */
export function inspectionDrillMarkers(sheet: PartInspection): DrillMarker[] {
  const s = sheet.part.finished_size;
  const lengths = [s.width_um, s.depth_um, s.height_um];
  const planes: Record<string, [number, number, DrillMarker["rotation"]]> = {
    left: [0, -1, [0, -Math.PI / 2, 0]],
    right: [0, 1, [0, Math.PI / 2, 0]],
    front: [1, -1, [0, 0, 0]],
    back: [1, 1, [0, Math.PI, 0]],
    bottom: [2, -1, [Math.PI / 2, 0, 0]],
    top: [2, 1, [-Math.PI / 2, 0, 0]],
  };
  return sheet.drawings.flatMap((view) => {
    const plane = planes[view.physical_face];
    if (!plane) return [];
    const [normal, sign, rotation] = plane;
    const u = "xyz".indexOf(view.u_axis),
      v = "xyz".indexOf(view.v_axis);
    if (u < 0 || v < 0 || u === v || u === normal || v === normal) return [];
    return view.holes.map((h, index): DrillMarker => {
      const pt = lengths.map((length) => -length / 2);
      pt[u] = pt[u]! + h.u_um;
      pt[v] = pt[v]! + h.v_um;
      // 50 micrometres of display offset avoids z-fighting on the receiving face.
      pt[normal] = sign * (lengths[normal]! / 2 + 50);
      return {
        key: view.view + "-" + h.feature_id + "-" + index,
        rotation,
        radius: h.diameter_um / 2_000_000,
        center: [pt[0]! / 1_000_000, pt[2]! / 1_000_000, -pt[1]! / 1_000_000],
      };
    });
  });
}

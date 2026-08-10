import type { ChangeDiff, DesignSpec } from "./design-types";

export const SEMANTIC_COMPONENT_MIME = "application/x-custombuild-component";

export type SemanticComponentKind = "shelf_row" | "divider" | "back_panel" | "plinth";
export type SemanticSnapRelation =
  | "shelf_in_bay"
  | "divider_in_carcass"
  | "back_behind_carcass"
  | "plinth_under_carcass";

export interface SemanticComponentDefinition {
  kind: SemanticComponentKind;
  label: string;
  description: string;
  limit: string;
}

export interface SemanticDropRequest {
  kind: SemanticComponentKind;
  normalizedX: number;
  normalizedY: number;
}

export interface SemanticSnapPreview {
  kind: SemanticComponentKind;
  relation: SemanticSnapRelation;
  label: string;
  targetId: string;
  normalizedX: number;
  normalizedY: number;
  bayIndex?: number;
}

export interface SemanticDropOutcome {
  spec: DesignSpec;
  diff: ChangeDiff[];
  message: string;
  warning?: string;
  preview: SemanticSnapPreview;
}

export const SEMANTIC_COMPONENTS: readonly SemanticComponentDefinition[] = [
  {
    kind: "shelf_row",
    label: "Hyllrad",
    description: "Snappar till närmaste fack och bygger om hyllgeometrin.",
    limit: "Max 30 rader",
  },
  {
    kind: "divider",
    label: "Vertikal avdelare",
    description: "Snappar i stommen och delar bredden i lika stora fack.",
    limit: "Max 8 avdelare",
  },
  {
    kind: "back_panel",
    label: "Bakstycke",
    description: "Snappar bakom stommen och skapar matchande noter.",
    limit: "6 mm i MVP",
  },
  {
    kind: "plinth",
    label: "Sockel",
    description: "Snappar under stommen och återställer 80 mm sockel.",
    limit: "En främre sockel",
  },
] as const;

const COMPONENT_KINDS = new Set<SemanticComponentKind>(
  SEMANTIC_COMPONENTS.map((component) => component.kind),
);

function clamp(value: number): number {
  if (!Number.isFinite(value)) return 0.5;
  return Math.min(1, Math.max(0, value));
}

export function writeSemanticDragPayload(
  dataTransfer: DataTransfer,
  kind: SemanticComponentKind,
): void {
  dataTransfer.effectAllowed = "copy";
  dataTransfer.setData(SEMANTIC_COMPONENT_MIME, kind);
  dataTransfer.setData("text/plain", kind);
}

export function readSemanticDragPayload(dataTransfer: DataTransfer): SemanticComponentKind | undefined {
  const raw = dataTransfer.getData(SEMANTIC_COMPONENT_MIME) || dataTransfer.getData("text/plain");
  return COMPONENT_KINDS.has(raw as SemanticComponentKind)
    ? (raw as SemanticComponentKind)
    : undefined;
}

export function createSemanticSnapPreview(
  spec: DesignSpec,
  request: SemanticDropRequest,
): SemanticSnapPreview {
  const normalizedX = clamp(request.normalizedX);
  const normalizedY = clamp(request.normalizedY);
  const bayCount = Math.max(1, Math.trunc(spec.divider_count) + 1);
  const bayIndex = Math.min(bayCount, Math.floor(normalizedX * bayCount) + 1);

  if (request.kind === "shelf_row") {
    return {
      kind: request.kind,
      relation: "shelf_in_bay",
      label: `Hyllfack ${bayIndex}`,
      targetId: `bookcase:shelf:bay:${bayIndex}`,
      normalizedX,
      normalizedY,
      bayIndex,
    };
  }
  if (request.kind === "divider") {
    return {
      kind: request.kind,
      relation: "divider_in_carcass",
      label: "Bokhyllans stomme",
      targetId: "bookcase:divider:carcass",
      normalizedX,
      normalizedY,
    };
  }
  if (request.kind === "back_panel") {
    return {
      kind: request.kind,
      relation: "back_behind_carcass",
      label: "Stommens baksida",
      targetId: "bookcase:back:rear",
      normalizedX: 0.5,
      normalizedY: 0.5,
    };
  }
  return {
    kind: request.kind,
    relation: "plinth_under_carcass",
    label: "Stommens undersida",
    targetId: "bookcase:plinth:base",
    normalizedX: 0.5,
    normalizedY: 1,
  };
}

export function resolveSemanticDrop(
  spec: DesignSpec,
  request: SemanticDropRequest,
): SemanticDropOutcome {
  const preview = createSemanticSnapPreview(spec, request);

  if (request.kind === "shelf_row") {
    if (spec.shelf_count >= 30) throw new Error("Mallen stödjer högst 30 hyllrader.");
    const after = spec.shelf_count + 1;
    return {
      spec: { ...spec, shelf_count: after, fixed_shelves: true },
      diff: [
        {
          field: "shelf_count",
          before: spec.shelf_count,
          after,
          reason: `Hyllrad släpptes i ${preview.label.toLowerCase()}.`,
        },
        ...(spec.fixed_shelves
          ? []
          : [
              {
                field: "fixed_shelves" as const,
                before: spec.fixed_shelves,
                after: true,
                reason: "Dragkomponenten använder den produktionsstödda fasta hyllfogen.",
              },
            ]),
      ],
      message: `En hyllrad lades till via ${preview.label}.`,
      warning:
        "DesignSpec 1.0 fördelar hyllraderna jämnt över samtliga fack. Släpphöjden är avsikt, inte CNC-koordinat.",
      preview,
    };
  }

  if (request.kind === "divider") {
    if (spec.divider_count >= 8) throw new Error("Mallen stödjer högst 8 avdelare.");
    const after = spec.divider_count + 1;
    return {
      spec: { ...spec, divider_count: after, reinforcement_mode: "manual" },
      diff: [
        {
          field: "divider_count",
          before: spec.divider_count,
          after,
          reason: "Vertikal avdelare lades till via semantisk snapping.",
        },
        ...(spec.reinforcement_mode === "manual"
          ? []
          : [
              {
                field: "reinforcement_mode" as const,
                before: spec.reinforcement_mode,
                after: "manual" as const,
                reason: "En användarstyrd strukturändring kräver manuell konstruktionsgranskning.",
              },
            ]),
      ],
      message: "En vertikal avdelare lades till och facken räknades om.",
      warning:
        "DesignSpec 1.0 delar stommen i lika breda fack. Fri förflyttning "
        + "aktiveras först med ett versionsstött positionsschema.",
      preview,
    };
  }

  if (request.kind === "back_panel") {
    if (spec.back_panel) throw new Error("Bokhyllan har redan ett bakstycke.");
    return {
      spec: { ...spec, back_panel: true },
      diff: [
        {
          field: "back_panel",
          before: false,
          after: true,
          reason: "Bakstycke snappades till stommens baksida.",
        },
      ],
      message: "Bakstycket lades till och matchande noter regenereras.",
      preview,
    };
  }

  if (spec.plinth) throw new Error("Bokhyllan har redan en sockel.");
  return {
    spec: { ...spec, plinth: true },
    diff: [
      {
        field: "plinth",
        before: false,
        after: true,
        reason: "Sockeln snappades till stommens undersida.",
      },
    ],
    message: "En 80 mm främre sockel lades till.",
    preview,
  };
}

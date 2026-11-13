import type { DesignSpec, ResolvedDesign, RuleEvaluation } from "@/lib/design-types";

export type UnsupportedDesignSystem = "joint_retention" | "cabinet_hardware";

export interface DesignPlanningSummary {
  readonly wallAnchoringRequired: boolean;
  readonly unsupportedSystems: readonly UnsupportedDesignSystem[];
}

/**
 * Planning facts only: this neither selects physical fixings nor clears rules.
 * Known local implementation gaps must not be inferred from server evaluations;
 * a server preview can carry an authenticated system that the local model lacks.
 */
export function designPlanning(
  spec: Readonly<DesignSpec>,
  evaluations: readonly RuleEvaluation[],
  source: ResolvedDesign["source"],
): DesignPlanningSummary {
  const wallAnchoringRequired = spec.wall_anchor_required
    || spec.height_mm / spec.depth_mm >= 4
    || spec.height_mm >= 1_800;
  const unsupportedSystems: UnsupportedDesignSystem[] = [];

  if (source === "local") {
    const unresolved = evaluations.filter((evaluation) => evaluation.status !== "PASS");
    const fixedShelfRetentionMissing = spec.fixed_shelves && unresolved.some((evaluation) => (
      evaluation.rule_id === "CB-JOINT-001"
      && !evaluation.diagnostics?.some((diagnostic) => (
        diagnostic.label.toLowerCase().includes("hylltyp")
        && diagnostic.value.toLowerCase().includes("adjustable")
      ))
    ));
    if (fixedShelfRetentionMissing) unsupportedSystems.push("joint_retention");
    if (
      spec.furniture_type === "wall_library"
      && spec.base_cabinet_count > 0
      && unresolved.some((evaluation) => evaluation.rule_id === "CB-HARDWARE-001")
    ) {
      unsupportedSystems.push("cabinet_hardware");
    }
  }

  return { wallAnchoringRequired, unsupportedSystems };
}

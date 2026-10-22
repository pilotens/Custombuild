import { DESIGN_CONSTRAINTS } from "./design-constraints";
import { resolveDesign } from "./design-engine";
import type { ChangeDiff, DesignSpec, RuleEvaluation } from "./design-types";
import { estimateMaterialRequirements } from "./material-requirements";
import { DesignHydrationError } from "./workspace-design-envelope";
import {
  normalizeWorkspaceDesignTransaction,
  type PreviewWorkspaceDesignTransaction,
} from "./workspace-design-transaction";

/** An edit is rejected before it can enter history, persistence or server preview. */
export class DesignChangeRejected extends DesignHydrationError {
  constructor(message: string, readonly ruleIds: readonly string[] = []) {
    super("INVALID_LOCAL_DESIGN", [message]);
    this.name = "DesignChangeRejected";
  }
}

function needsDesignChange(rule: RuleEvaluation): boolean {
  return rule.status === "BLOCK" || (
    rule.status !== "PASS" && ["STR-DEF-001", "STAB-RACK-001"].includes(rule.rule_id)
  );
}

function rejection(rule: RuleEvaluation): DesignChangeRejected {
  const messages: Record<string, string> = {
    "DFM-STOCK-001": "Ändringen ryms inte i det valda skivformatet eller det bundna materialet. Den tidigare modellen behålls.",
    "DFM-MACHINE-001": "Delarna skulle bli större än maskinens arbetsområde. Den tidigare storleken behålls.",
    "STR-DEF-001": "Den här indelningen ger för långa fria hyllspann. Det nödvändiga stödet behålls.",
    "STAB-RACK-001": "Bakstycket behövs för sidostabiliteten och kan inte tas bort utan en verifierad ersättande stagning.",
    "CB-JOINT-001": "Justerbara hyllor kan inte väljas innan ett passande hyllbärarsystem stöds. Fasta hyllor behålls.",
    "PART-CUSTOM-001": "Den ändringen skulle bryta den parametriska konstruktionen. Använd möbelns mått och indelning i stället.",
    "GEO-001": "Delarna får inte tillräckligt utrymme med de måtten. Den tidigare indelningen behålls.",
  };
  return new DesignChangeRejected(messages[rule.rule_id]
    ?? `Ändringen kan inte genomföras: ${rule.summary}`, [rule.rule_id]);
}

/**
 * Design feasibility, not manufacturing approval. Uses the existing rules without
 * changing thresholds or evidence. Unbound quantities are planned demand, never
 * verified inventory. Exact workshop profiles and explicit quantity edits are
 * constraints and are never increased by this planner.
 */
export function planWorkspaceDesignTransaction(
  source: DesignSpec,
  candidate: DesignSpec,
  requestedDiff: readonly ChangeDiff[] = [],
): PreviewWorkspaceDesignTransaction {
  let transaction = normalizeWorkspaceDesignTransaction(source, candidate, requestedDiff);
  const autoDiff: ChangeDiff[] = [...transaction.structuralDiff];
  const quantityExplicitlyChanged = candidate.stock_count !== source.stock_count
    || candidate.back_stock_count !== source.back_stock_count;
  const sameEnvelope = candidate.furniture_type === source.furniture_type
    && candidate.width_mm === source.width_mm && candidate.height_mm === source.height_mm
    && candidate.depth_mm === source.depth_mm;
  const removingSupport = sameEnvelope && candidate.divider_count < source.divider_count;
  const movingCustomBays = candidate.divider_count === source.divider_count
    && sameEnvelope
    && JSON.stringify(candidate.bay_width_ratios) !== JSON.stringify(source.bay_width_ratios);

  // Bounded search: at most all permitted divider counts plus the independent
  // base/back/material repairs. Never reduce the requested load or outer size.
  for (let attempt = 0; attempt < DESIGN_CONSTRAINTS.dividerCount.maximum + 5; attempt += 1) {
    const spec = transaction.normalizedSpec;
    const design = resolveDesign(spec);
    const unresolved = design.rule_evaluations.filter(needsDesignChange);
    if (unresolved.length === 0) {
      return { ...transaction, resolvedDesign: { ...design, change_diff: transaction.changeDiff } };
    }
    // Unsupported free edits and unqualified adjustable hardware must not be
    // turned into another construction by quietly dropping the user's choice.
    const unsupported = unresolved.find((rule) => (
      ["PART-CUSTOM-001", "STR-TOPO-001", "GEO-001", "CB-JOINT-001"].includes(rule.rule_id)
    ));
    if (unsupported) throw rejection(unsupported);

    const patch: Partial<DesignSpec> = {};
    const span = unresolved.find((rule) => rule.rule_id === "STR-DEF-001");
    if (span) {
      if (removingSupport || movingCustomBays || spec.divider_count >= DESIGN_CONSTRAINTS.dividerCount.maximum) {
        throw rejection(span);
      }
      patch.divider_count = spec.divider_count + 1;
      patch.bay_width_ratios = [];
      patch.reinforcement_mode = "manual";
      patch.topology_baseline = undefined;
      if (spec.furniture_type === "wall_library") patch.base_cabinet_count = patch.divider_count + 1;
    } else if (unresolved.some((rule) => rule.rule_id === "BASE-SUPPORT-001")) {
      patch.base_cabinet_count = spec.divider_count + 1;
      patch.topology_baseline = undefined;
    } else if (unresolved.some((rule) => rule.rule_id === "STAB-RACK-001")) {
      if (source.back_panel && !candidate.back_panel) {
        throw rejection(unresolved.find((rule) => rule.rule_id === "STAB-RACK-001")!);
      }
      patch.back_panel = true;
    } else {
      const stock = unresolved.find((rule) => rule.rule_id === "DFM-STOCK-001");
      const machine = unresolved.find((rule) => rule.rule_id === "DFM-MACHINE-001");
      if (!stock && !machine) throw rejection(unresolved[0]!);
      const material = estimateMaterialRequirements(spec, design.parts);
      const oversize = new Set([...material.oversizedPartIds, ...(machine?.affected_part_ids ?? [])]);
      const canPartition = oversize.size > 0 && [...oversize].every((id) => {
        const part = design.parts.find((item) => item.part_id === id);
        return part?.kind === "shelf" || (part?.kind === "back" && spec.back_panel_type === "inset_groove");
      });
      if (canPartition && !removingSupport && !movingCustomBays && spec.divider_count < DESIGN_CONSTRAINTS.dividerCount.maximum) {
        patch.divider_count = spec.divider_count + 1;
        patch.bay_width_ratios = [];
        patch.reinforcement_mode = "manual";
        patch.topology_baseline = undefined;
        if (spec.furniture_type === "wall_library") patch.base_cabinet_count = patch.divider_count + 1;
      } else {
        if (machine) throw rejection(machine);
        if (!stock) throw rejection(unresolved[0]!);
        if (source.workshop_context || spec.workshop_context || quantityExplicitlyChanged) throw rejection(stock);
        if (!material.complete || material.rows.some((row) => row.estimatedSheets > 100)) throw rejection(stock);
        for (const row of material.rows) {
          const field = row.role === "carcass" ? "stock_count" : "back_stock_count";
          if (row.estimatedSheets > spec[field]) patch[field] = row.estimatedSheets;
        }
        if (Object.keys(patch).length === 0) throw rejection(stock);
      }
    }
    for (const field of Object.keys(patch) as (keyof DesignSpec)[]) {
      const before = spec[field];
      const after = patch[field];
      if (before === after || !["number", "string", "boolean"].includes(typeof after)
        || !["number", "string", "boolean"].includes(typeof before)) continue;
      autoDiff.push({
        field,
        before: before as number | string | boolean,
        after: after as number | string | boolean,
        reason: field === "stock_count" || field === "back_stock_count"
          ? "Planerat skivbehov räknades upp automatiskt. Detta är inte verifierat lager."
          : "Konstruktionen anpassades automatiskt innan ändringen tillämpades.",
      });
    }
    transaction = normalizeWorkspaceDesignTransaction(source, { ...spec, ...patch }, [...requestedDiff, ...autoDiff]);
  }
  throw new DesignChangeRejected("Ingen fungerande indelning hittades inom modellens gränser. Den tidigare modellen behålls.");
}

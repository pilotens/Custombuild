"use client";

import { ChevronDown, Crosshair, Sparkles } from "lucide-react";
import type { RuleEvaluation, ValidationStatus } from "@/lib/design-types";
import { StatusBadge } from "./status-badge";

interface ValidationPanelProps {
  evaluations: RuleEvaluation[];
  status: ValidationStatus;
  applyingRuleId?: string;
  onApplySuggestion: (evaluation: RuleEvaluation) => void;
  onSelectPart: (partId: string) => void;
}

const priority: Record<ValidationStatus, number> = { BLOCK: 0, WARNING: 1, PASS: 2 };

export function ValidationPanel({
  evaluations,
  status,
  applyingRuleId,
  onApplySuggestion,
  onSelectPart,
}: ValidationPanelProps) {
  const ordered = [...evaluations].sort((left, right) => priority[left.status] - priority[right.status]);
  const warnings = evaluations.filter((evaluation) => evaluation.status === "WARNING").length;
  const blocks = evaluations.filter((evaluation) => evaluation.status === "BLOCK").length;

  return (
    <section className="panel validation-panel" aria-labelledby="validation-heading">
      <div className="panel-heading validation-heading-row">
        <div>
          <p className="eyebrow">Regelmotor 1.1.0</p>
          <h2 id="validation-heading">Validering</h2>
        </div>
        <StatusBadge status={status} compact />
      </div>
      <div className="validation-summary" aria-label={`${blocks} blockerande fel och ${warnings} varningar`}>
        <span><strong>{blocks}</strong> blockerar</span>
        <span><strong>{warnings}</strong> varningar</span>
        <span><strong>{evaluations.length - blocks - warnings}</strong> godkända</span>
      </div>
      <div className="validation-list">
        {ordered.map((evaluation) => (
          <details
            className={`validation-card validation-${evaluation.status.toLowerCase()}`}
            key={evaluation.rule_id}
            open={evaluation.status !== "PASS"}
          >
            <summary>
              <span className="validation-title-wrap">
                <StatusBadge status={evaluation.status} compact />
                <span>
                  <strong>{evaluation.title}</strong>
                  <small>{evaluation.rule_id} · v{evaluation.rule_version}</small>
                </span>
              </span>
              <ChevronDown aria-hidden="true" size={15} className="summary-chevron" />
            </summary>
            <div className="validation-card-body">
              <p>{evaluation.summary}</p>
              <div className="calculation-trace">
                <span>Beräkningsspår</span>
                <code>{evaluation.calculation}</code>
                {evaluation.calculated_value !== undefined ? (
                  <div className="calculation-values">
                    <span>Beräknat <strong>{evaluation.calculated_value} {evaluation.unit}</strong></span>
                    {evaluation.allowed_value !== undefined ? <span>Tillåtet <strong>{evaluation.allowed_value} {evaluation.unit}</strong></span> : null}
                    {evaluation.margin_percent !== undefined ? <span>Marginal <strong className={evaluation.margin_percent < 0 ? "negative" : ""}>{evaluation.margin_percent} %</strong></span> : null}
                  </div>
                ) : null}
              </div>
              {evaluation.assumptions.length > 0 ? (
                <ul className="assumption-list">
                  {evaluation.assumptions.map((assumption) => <li key={assumption}>{assumption}</li>)}
                </ul>
              ) : null}
              <div className="validation-actions">
                {evaluation.affected_part_ids[0] ? (
                  <button type="button" className="text-button" onClick={() => onSelectPart(evaluation.affected_part_ids[0] ?? "")}>
                    <Crosshair aria-hidden="true" size={14} /> Visa i modell
                  </button>
                ) : null}
                {evaluation.suggestion && evaluation.suggestion.action !== "verify_wall_anchor" ? (
                  <button
                    type="button"
                    className="suggestion-button"
                    disabled={applyingRuleId === evaluation.rule_id}
                    onClick={() => onApplySuggestion(evaluation)}
                  >
                    <Sparkles aria-hidden="true" size={14} />
                    {applyingRuleId === evaluation.rule_id ? "Räknar om…" : evaluation.suggestion.label}
                  </button>
                ) : null}
              </div>
            </div>
          </details>
        ))}
      </div>
      <p className="screening-disclaimer">Resultaten är konstruktionsscreening och beslutsstöd – inte produktcertifiering.</p>
    </section>
  );
}

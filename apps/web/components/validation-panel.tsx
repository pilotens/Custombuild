"use client";

import { useRef } from "react";
import { AlertTriangle, CheckCircle2, ChevronDown, CircleX, Crosshair, Sparkles } from "lucide-react";
import type { DesignSpec, RuleEvaluation, ValidationStatus } from "@/lib/design-types";
import {
  automaticValidationFixPreview,
  permitsStocklessDesignReview,
  validationGuidance,
} from "@/lib/validation-guidance";

export interface ActiveValidationFixPreview {
  ruleId: string;
  ruleVersion: string;
  label: string;
  reason: string;
  changes: ReadonlyArray<{ field: keyof DesignSpec; before: unknown; after: unknown }>;
  noGeometryChange: boolean;
}

interface ValidationPanelProps {
  evaluations: RuleEvaluation[];
  status: ValidationStatus;
  spec?: DesignSpec;
  activePreview?: ActiveValidationFixPreview;
  onRequestPreview: (evaluation: RuleEvaluation) => void;
  onCancelPreview: () => void;
  onConfirmPreview: () => void;
  onSelectPart: (partId: string) => void;
  onNavigateToStep?: (step: 0 | 1 | 2 | 3) => void;
  onOpenProduction?: () => void;
}

const priority: Record<ValidationStatus, number> = { BLOCK: 0, WARNING: 1, PASS: 2 };
const statusLabel: Record<ValidationStatus, string> = {
  PASS: "Godkänt",
  WARNING: "Behöver beslut",
  BLOCK: "Måste lösas",
};

function ValidationState({ status }: { status: ValidationStatus }) {
  const Icon = status === "PASS" ? CheckCircle2 : status === "WARNING" ? AlertTriangle : CircleX;
  return (
    <span className={`status-badge status-${status.toLowerCase()}`} data-testid="validation-state">
      <Icon aria-hidden="true" size={14} strokeWidth={2.4} />
      {statusLabel[status]}
    </span>
  );
}

function formatMeasuredValue(value: number, unit?: string): string {
  return `${value}${unit ? ` ${unit}` : ""}`;
}

function formatPreviewValue(value: unknown): string {
  if (value === undefined) return "undefined";
  if (typeof value === "number" || typeof value === "boolean" || typeof value === "string") {
    return String(value);
  }
  return JSON.stringify(value);
}

function numericDiagnostics(evaluation: RuleEvaluation) {
  return evaluation.diagnostics?.filter((item) => /[-+]?(?:\d+(?:[.,]\d*)?|[.,]\d+)/.test(item.value)) ?? [];
}

function thresholdLabel(evaluation: RuleEvaluation): string {
  const ruleId = evaluation.rule_id.toUpperCase();
  if (ruleId === "CB-TIP-001") return "Minimikrav";
  if (ruleId === "STAB-TIP-002") return "Förankring krävs från";
  if (ruleId === "CB-JOINT-001") return "Verifierad lokal kapacitet";
  if (
    ruleId.includes("DEFLECTION")
    || ruleId.includes("BENDING")
    || ruleId.includes("STABILITY")
    || ruleId === "STR-DEF-001"
  ) return "Högsta tillåtna";
  if (
    ruleId.includes("HARDWARE")
    || ruleId.includes("SUPPORT")
    || ruleId.startsWith("DFM-")
    || ruleId === "GEO-001"
  ) return "Kravvärde";
  return "Gränsvärde";
}

export function ValidationPanel({
  evaluations,
  status,
  spec,
  activePreview,
  onRequestPreview,
  onCancelPreview,
  onConfirmPreview,
  onSelectPart,
  onNavigateToStep,
  onOpenProduction,
}: ValidationPanelProps) {
  const previewTriggerRef = useRef<HTMLButtonElement | null>(null);
  const ordered = [...evaluations].sort((left, right) => priority[left.status] - priority[right.status]);
  const warnings = evaluations.filter((evaluation) => evaluation.status === "WARNING").length;
  const blocks = evaluations.filter((evaluation) => evaluation.status === "BLOCK").length;
  const stocklessReviewAllowed = permitsStocklessDesignReview(evaluations);

  return (
    <section className="panel validation-panel" aria-labelledby="validation-heading">
      <div className="panel-heading validation-heading-row">
        <div>
          <p className="eyebrow">Kontroll · Aktuell konstruktion</p>
          <h2 id="validation-heading">Kontrollera konstruktionen</h2>
        </div>
        <ValidationState status={status} />
      </div>
      <div className="validation-summary" aria-label={`${blocks} måste lösas och ${warnings} behöver beslut`}>
        <span><strong>{blocks}</strong> måste lösas</span>
        <span><strong>{warnings}</strong> behöver beslut</span>
        <span><strong>{evaluations.length - blocks - warnings}</strong> godkända</span>
      </div>
      <div className="validation-list">
        {ordered.map((evaluation, evaluationIndex) => {
          const guidance = validationGuidance(evaluation, spec);
          const automaticPreview = evaluation.status !== "PASS" && spec
            ? automaticValidationFixPreview(evaluation, spec)
            : undefined;
          const isPreviewOpen = activePreview?.ruleId === evaluation.rule_id
            && activePreview.ruleVersion === evaluation.rule_version
            && automaticPreview !== undefined;
          const targetStep = guidance.target.kind === "step" ? guidance.target.step : undefined;
          const affectedPartIds = [...new Set(evaluation.affected_part_ids.filter(Boolean))];
          const actualValue = evaluation.calculated_value !== undefined && Number.isFinite(evaluation.calculated_value)
            ? formatMeasuredValue(evaluation.calculated_value, evaluation.unit)
            : undefined;
          const allowedValue = evaluation.allowed_value !== undefined && Number.isFinite(evaluation.allowed_value)
            ? formatMeasuredValue(evaluation.allowed_value, evaluation.unit)
            : undefined;
          const diagnostics = numericDiagnostics(evaluation);
          const cardId = `${evaluation.rule_id.replace(/[^a-zA-Z0-9_-]/g, "-")}-${evaluationIndex}`;
          const headingId = `validation-title-${cardId}`;
          const fixPreviewId = `validation-fix-preview-${cardId}`;
          return (
          <article
            className={`validation-card validation-${evaluation.status.toLowerCase()}`}
            key={evaluation.rule_id}
            aria-labelledby={headingId}
          >
            <div className="validation-card-header">
              <span className="validation-title-wrap">
                <ValidationState status={evaluation.status} />
                <span>
                  <strong id={headingId}>{evaluation.title}</strong>
                  <small>{evaluation.summary}</small>
                </span>
              </span>
            </div>
            <div className="validation-card-body">
              <div className="validation-guidance">
                <section>
                  <strong>{evaluation.status === "PASS" ? "Verifierat resultat" : "Orsak"}</strong>
                  <p>{guidance.problem}</p>
                </section>
                <section>
                  <strong>Varför spelar det roll?</strong>
                  <p>{guidance.impact}</p>
                </section>
                <section className="solution">
                  <strong>Rekommenderad lösning</strong>
                  <p>{guidance.solution}</p>
                  <small><b>Värde eller underlag:</b> {guidance.requiredInput}</small>
                </section>
              </div>
              {actualValue || allowedValue || diagnostics.length > 0 || evaluation.assumptions.length > 0 ? (
                <dl className="validation-evidence" aria-label={`Verifierat beslutsunderlag för ${evaluation.title}`}>
                  {actualValue ? (
                    <div>
                      <dt>Aktuellt verifierat värde</dt>
                      <dd>{actualValue}</dd>
                    </div>
                  ) : null}
                  {allowedValue ? (
                    <div>
                      <dt>{thresholdLabel(evaluation)}</dt>
                      <dd>{allowedValue}</dd>
                    </div>
                  ) : null}
                  {diagnostics.map((item) => (
                    <div key={`${item.label}-${item.value}-${item.unit ?? ""}`}>
                      <dt>Kontrollvärde · {item.label}</dt>
                      <dd>{item.value}{item.unit ? ` ${item.unit}` : ""}</dd>
                    </div>
                  ))}
                  {evaluation.assumptions.map((assumption, assumptionIndex) => (
                    <div key={`${assumption}-${assumptionIndex}`}>
                      <dt>Antagande</dt>
                      <dd>{assumption}</dd>
                    </div>
                  ))}
                </dl>
              ) : null}
              {affectedPartIds.length > 0 ? (
                <div className="validation-part-actions" role="group" aria-label={`Berörda delar för ${evaluation.title}`}>
                  <span>Berörda delar</span>
                  {affectedPartIds.map((partId) => (
                    <button
                      type="button"
                      className="text-button"
                      key={partId}
                      aria-label={`Fokusera delen ${partId} i modellen`}
                      onClick={() => onSelectPart(partId)}
                    >
                      <Crosshair aria-hidden="true" size={14} />
                      {partId}
                    </button>
                  ))}
                </div>
              ) : null}
              {isPreviewOpen && automaticPreview && activePreview ? (
                <section
                  className="validation-fix-preview"
                  id={fixPreviewId}
                  aria-labelledby={`${fixPreviewId}-heading`}
                  aria-live="polite"
                >
                  <strong id={`${fixPreviewId}-heading`}>Kontrollera ändringen före tillämpning</strong>
                  <p>Den här förhandsvisningen är lokalt beräknad. Den är inte serververifierad eller tillämpad.</p>
                  <dl>
                    {activePreview.changes.map((change) => (
                      <div key={change.field}>
                        <dt><code>{change.field}</code></dt>
                        <dd>
                          <span>Före <code>{formatPreviewValue(change.before)}</code></span>
                          <span aria-hidden="true">→</span>
                          <span>Efter <code>{formatPreviewValue(change.after)}</code></span>
                        </dd>
                      </div>
                    ))}
                  </dl>
                  {activePreview.noGeometryChange ? (
                    <p><strong>Ingen geometriförändring:</strong> endast specifikations- eller regelunderlag ändras.</p>
                  ) : (
                    <p><strong>3D-jämförelse:</strong> nuvarande ändrade eller borttagna delar visas i ockra och föreslagen geometri i turkos.</p>
                  )}
                  <p><strong>Orsak:</strong> {activePreview.reason}</p>
                  <div className="validation-fix-preview-actions">
                    <button
                      type="button"
                      className="text-button"
                      onClick={() => {
                        onCancelPreview();
                        previewTriggerRef.current?.focus();
                      }}
                    >
                      Avbryt
                    </button>
                    <button
                      type="button"
                      className="suggestion-button"
                      onClick={onConfirmPreview}
                    >
                      <Sparkles aria-hidden="true" size={14} />
                      Bekräfta och tillämpa
                    </button>
                  </div>
                </section>
              ) : null}
              <div className="validation-actions">
                {automaticPreview ? (
                  <button
                    type="button"
                    className="suggestion-button"
                    aria-label={evaluation.status === "BLOCK"
                      ? `Åtgärda problem för ${evaluation.title}: ${automaticPreview.label}`
                      : `Förhandsgranska: ${automaticPreview.label}`}
                    aria-expanded={isPreviewOpen}
                    aria-controls={isPreviewOpen ? fixPreviewId : undefined}
                    onClick={(event) => {
                      previewTriggerRef.current = event.currentTarget;
                      onRequestPreview(evaluation);
                    }}
                  >
                    <Sparkles aria-hidden="true" size={14} />
                    {evaluation.status === "BLOCK"
                      ? "Åtgärda problem"
                      : `Förhandsgranska: ${automaticPreview.label}`}
                  </button>
                ) : evaluation.status !== "PASS" && targetStep !== undefined && onNavigateToStep ? (
                  <button
                    type="button"
                    className="suggestion-button"
                    aria-label={evaluation.status === "BLOCK"
                      ? `Åtgärda problem för ${evaluation.title} i ${guidance.target.control}`
                      : `Gå till ${guidance.target.control}`}
                    onClick={() => onNavigateToStep(targetStep)}
                  >
                    {evaluation.status === "BLOCK" ? "Åtgärda problem" : `Gå till ${guidance.target.control}`}
                  </button>
                ) : evaluation.status !== "PASS"
                  && guidance.target.kind === "production"
                  && (guidance.target.gate !== "stockless_review" || stocklessReviewAllowed)
                  && onOpenProduction ? (
                  <button
                    type="button"
                    className="suggestion-button"
                    aria-label={evaluation.status === "BLOCK"
                      ? `Åtgärda problem för ${evaluation.title}: öppna ${guidance.target.control}`
                      : "Extern kontroll: öppna underlaget"}
                    onClick={onOpenProduction}
                  >
                    {evaluation.status === "BLOCK" ? "Åtgärda problem" : "Extern kontroll: öppna underlaget"}
                  </button>
                ) : null}
              </div>
              <details className="validation-technical">
                <summary>
                  <span>Tekniska detaljer</span>
                  <ChevronDown aria-hidden="true" size={15} className="summary-chevron" />
                </summary>
                <div className="validation-technical-body">
                  <p><small>Regel {evaluation.rule_id} · version {evaluation.rule_version}</small></p>
                  {evaluation.diagnostics?.length ? (
                    <dl className="validation-diagnostics" aria-label="Kontrollens exakta diagnostik">
                      {evaluation.diagnostics.map((item) => (
                        <div key={`${item.label}-${item.value}-${item.unit ?? ""}`}>
                          <dt>{item.label}</dt>
                          <dd>{item.value}{item.unit ? ` ${item.unit}` : ""}</dd>
                        </div>
                      ))}
                    </dl>
                  ) : null}
                  <div className="calculation-trace">
                    <span>Beräkningsspår</span>
                    <code>{evaluation.calculation}</code>
                    {actualValue || allowedValue || evaluation.margin_percent !== undefined ? (
                      <div className="calculation-values">
                        {actualValue ? <span>Beräknat <strong>{actualValue}</strong></span> : null}
                        {allowedValue ? <span>{thresholdLabel(evaluation)} <strong>{allowedValue}</strong></span> : null}
                        {evaluation.margin_percent !== undefined ? (
                          <span>Marginal <strong className={evaluation.margin_percent < 0 ? "negative" : ""}>{evaluation.margin_percent} %</strong></span>
                        ) : null}
                      </div>
                    ) : null}
                  </div>
                </div>
              </details>
            </div>
          </article>
          );
        })}
      </div>
      <p className="screening-disclaimer">Kontrollen använder den aktuella konstruktionen. Tekniska detaljer finns kvar för den som vill granska beräkningen.</p>
    </section>
  );
}

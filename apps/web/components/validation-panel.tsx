"use client";

import { useMemo, useRef } from "react";
import { AlertTriangle, CheckCircle2, ChevronDown, CircleX, Crosshair, Sparkles } from "lucide-react";
import type { DesignSpec, ResolvedDesign, ResolvedPart, RuleEvaluation, ValidationStatus } from "@/lib/design-types";
import { designPlanning } from "@/lib/design-planning";
import { estimateMaterialRequirements } from "@/lib/material-requirements";
import {
  validationPresentation,
  validationPreviewFieldLabel,
  validationPreviewValue,
} from "@/lib/validation-presentation";
import styles from "./validation-panel.module.css";
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
  parts?: readonly ResolvedPart[];
  source?: ResolvedDesign["source"];
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
  parts,
  source,
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
  const materialRequirements = useMemo(() => (
    spec && parts ? estimateMaterialRequirements(spec, parts) : undefined
  ), [spec, parts]);
  const planning = spec && source ? designPlanning(spec, evaluations, source) : undefined;
  const unsupportedRuleIds = new Set<string>(planning?.unsupportedSystems.map((system) => (
    system === "joint_retention" ? "CB-JOINT-001" : "CB-HARDWARE-001"
  )));

  const pending = ordered.filter((evaluation) => evaluation.status !== "PASS");
  const passed = ordered.filter((evaluation) => evaluation.status === "PASS");
  const isDesignAction = (evaluation: RuleEvaluation) => (
    validationPresentation(evaluation).category === "design"
    || (spec !== undefined && automaticValidationFixPreview(evaluation, spec) !== undefined)
  );
  const designChecks = pending.filter(isDesignAction);
  const workshopChecks = pending.filter((evaluation) => !isDesignAction(evaluation) && !unsupportedRuleIds.has(evaluation.rule_id));
  const capabilityChecks = pending.filter((evaluation) => unsupportedRuleIds.has(evaluation.rule_id));

  const renderCards = (items: RuleEvaluation[]) => items.map((evaluation, evaluationIndex) => {
    const guidance = validationGuidance(evaluation, spec);
    let presentation = validationPresentation(evaluation);
    const materialPlan = evaluation.rule_id === "DFM-STOCK-001" ? materialRequirements : undefined;
    if (materialPlan?.complete && materialPlan.rows.length > 0 && source === "local") {
      const total = materialPlan.rows.reduce((sum, row) => sum + row.estimatedSheets, 0);
      const additional = materialPlan.rows.reduce((sum, row) => sum + row.additionalSheets, 0);
      const quantities = materialPlan.rows.map((row) => (
        `${row.estimatedSheets} ${row.role === "carcass" ? "till stommen" : "till bakstycket"}`
      )).join(" + ");
      presentation = {
        ...presentation,
        title: `Beräknat behov: ${total} skivor`,
        summary: `${quantities}. ${additional > 0 ? `${additional} fler än angivet antal behöver ordnas.` : "Beräknat för de valda skivformaten."} Tillgången är inte verifierad.`,
      };
    }
    if (evaluation.rule_id === "STAB-TIP-002" && evaluation.status !== "PASS" && planning?.wallAnchoringRequired) {
      presentation = {
        ...presentation,
        title: "Väggförankring behövs",
        summary: "Appen har räknat med behovet av väggfästen. Infästningen måste passa den verkliga väggen.",
      };
    }
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
        className={`validation-card validation-${evaluation.status.toLowerCase()} ${styles.card}`}
        key={evaluation.rule_id}
        aria-label={evaluation.title}
      >
        <div className={styles.cardHeader}>
          <span className={styles.titleWrap}>
            <span>
              <strong id={headingId}>{presentation.title}</strong>
              <small>{presentation.summary}</small>
            </span>
          </span>
          <ValidationState status={evaluation.status} />
        </div>
        <div className={styles.cardBody}>
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
                    <dt>{validationPreviewFieldLabel(change.field)}</dt>
                    <dd>
                      <span>{validationPreviewValue(change.field, change.before)}</span>
                      <span aria-hidden="true">→</span>
                      <span>{validationPreviewValue(change.field, change.after)}</span>
                    </dd>
                  </div>
                ))}
              </dl>
              <details className={styles.exactChanges}>
                <summary>Exakta ändringsvärden</summary>
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
              </details>
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
          <div className={`validation-actions ${styles.actions}`}>
            {automaticPreview ? (
              <button
                type="button"
                className="suggestion-button"
                aria-label={`Visa förslag: ${automaticPreview.label}`}
                aria-expanded={isPreviewOpen}
                aria-controls={isPreviewOpen ? fixPreviewId : undefined}
                onClick={(event) => {
                  previewTriggerRef.current = event.currentTarget;
                  onRequestPreview(evaluation);
                }}
              >
                <Sparkles aria-hidden="true" size={14} />
                Visa förslag
              </button>
            ) : evaluation.status !== "PASS" && targetStep !== undefined && onNavigateToStep ? (
              <button
                type="button"
                className="suggestion-button"
                aria-label={`Ändra i Studio: ${evaluation.title}`}
                onClick={() => onNavigateToStep(targetStep)}
              >
                Ändra i Studio
              </button>
            ) : evaluation.status !== "PASS"
              && spec?.furniture_type !== "wall_library"
              && guidance.target.kind === "production"
              && (guidance.target.gate !== "stockless_review" || stocklessReviewAllowed)
              && onOpenProduction ? (
              <button
                type="button"
                className="suggestion-button"
                aria-label={`Öppna underlag för ${evaluation.title}`}
                onClick={onOpenProduction}
              >
                Öppna underlag
              </button>
            ) : null}
          </div>
          <details className={`validation-technical ${styles.details}`}>
            <summary>
              <span>Visa detaljer</span>
              <ChevronDown aria-hidden="true" size={15} className="summary-chevron" />
            </summary>
          <div className="validation-technical-body">
            <strong>{evaluation.title}</strong>
            {materialPlan ? (
              <section className={styles.materialPlan} aria-label="Automatiskt beräknat materialbehov">
                <strong>{materialPlan.complete ? "Preliminär materialplan" : "Ofullständig materialplan"}</strong>
                <dl>
                  {materialPlan.rows.map((row) => (
                    <div key={row.role}>
                      <dt>{row.role === "carcass" ? "Stomme" : "Bakstycke"} · {row.thicknessMm} mm</dt>
                      <dd>{row.estimatedSheets} skivor à {row.widthMm} × {row.heightMm} mm. Angivet antal: {row.declaredSheets}.</dd>
                    </div>
                  ))}
                </dl>
                {!materialPlan.complete ? (
                  <p>{materialPlan.oversizedPartIds.length > 0
                    ? "Alla delar ryms inte i beräkningen. Fler skivor löser inte ett för litet skivformat."
                    : "Materialplanen kunde inte beräknas för alla delar. Kontrollera skivformat, material och mått i underlaget."}</p>
                ) : null}
                <p>Geometrisk uppskattning, inte verifierat lager eller färdig kapplan. Verkstadens fiberriktning, marginaler och fixturer kan öka behovet. Inga lageruppgifter ändras.</p>
              </section>
            ) : null}
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
  });

  return (
    <section className={`panel validation-panel ${styles.panel}`} aria-labelledby="validation-heading">
      <div className={`panel-heading validation-heading-row ${styles.heading}`}>
        <div>
          <p className="eyebrow">Kontroll · Aktuell konstruktion</p>
          <h2 id="validation-heading">Kontrollera konstruktionen</h2>
        </div>
      </div>
      <div className={`validation-summary ${styles.summary}`} aria-label={blocks > 0
        ? `${blocks} måste lösas och ${warnings} behöver beslut`
        : `Inga blockerande designfel. ${warnings} tillverkningskrav återstår.`}>
        {blocks > 0 ? <span><strong>{blocks}</strong> måste lösas</span> : <span>Inga blockerande designfel</span>}
        <span><strong>{warnings}</strong> behöver beslut</span>
        <span><strong>{passed.length}</strong> godkända</span>
      </div>
      <div className={styles.content}>
        {designChecks.length > 0 ? (
          <section aria-labelledby="validation-design-heading" className={styles.group}>
            <h3 id="validation-design-heading">Justera möbeln</h3>
            <p>Se förslaget i modellen innan du bestämmer dig.</p>
            <div className="validation-list">{renderCards(designChecks)}</div>
          </section>
        ) : null}
        {workshopChecks.length > 0 ? (
          <section aria-labelledby="validation-workshop-heading" className={styles.group}>
            <h3 id="validation-workshop-heading">Inför tillverkning</h3>
            <p>Verkstaden behöver bekräfta detta innan möbeln kan tillverkas.</p>
            <div className="validation-list">{renderCards(workshopChecks)}</div>
          </section>
        ) : null}
        {capabilityChecks.length > 0 ? (
          <section className={styles.capability} aria-labelledby="validation-capability-heading">
            <h3 id="validation-capability-heading">Tillverkningsstödet är inte färdigt</h3>
            <p>Appen saknar ännu en verifierad lösning för {planning?.unsupportedSystems.map((system) => (
              system === "joint_retention" ? "foglåsning" : "gångjärn och borrhål"
            )).join(" samt ")}. Det kräver utveckling av appen, inte fler inställningar från dig.</p>
            <strong>Modellen är inte klar för tillverkning.</strong>
            <details>
              <summary>Visa {capabilityChecks.length} kvarstående krav</summary>
              <div className="validation-list">{renderCards(capabilityChecks)}</div>
            </details>
          </section>
        ) : null}
        {pending.length === 0 ? (
          <p className={styles.empty}>
            <CheckCircle2 aria-hidden="true" size={18} />
            {passed.length > 0 ? "Inga ändringar föreslås av kontrollerna." : "Inga kontroller att visa ännu."}
          </p>
        ) : null}
        {passed.length > 0 ? (
          <details className={styles.passed}>
            <summary>
              <CheckCircle2 aria-hidden="true" size={17} />
              <span>Godkända kontroller ({passed.length})</span>
              <ChevronDown aria-hidden="true" size={15} />
            </summary>
            <div className="validation-list">{renderCards(passed)}</div>
          </details>
        ) : null}
        <p className={styles.disclaimer}>
          {status === "BLOCK" ? "Tillverkning är spärrad tills kraven är lösta." : "Kontrollen är inte ett tillverkningstillstånd."}
        </p>
      </div>
    </section>
  );
}

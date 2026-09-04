"use client";

import { Check, Download, FileDown, LoaderCircle, RefreshCw, ShieldAlert } from "lucide-react";
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  ApiError,
  CustombuildApiClient,
  type ArtifactRead,
  type DesignVersionRead,
  type ExternalEvidenceRead,
  type GenerationRequest,
  type JobRead,
  type ReleaseRead,
  productionContextFromSpec,
  versionProductionContextMatches,
} from "@/lib/api-client";
import type {
  DesignSpec,
  ResolvedDesign,
  RetentionCertificationRequest,
  RuleEvaluation,
} from "@/lib/design-types";
import {
  clearLegacyProductionStorage,
  productionSessionKey,
  readProductionSession,
  writeProductionSession,
} from "@/lib/production-session-storage";
import type { WorkspaceIdentity } from "@/lib/workspace-draft-storage";
import {
  hasPartCustomization,
  type FurnitureTemplateId,
} from "@/lib/furniture-templates";
import {
  permitsStocklessDesignReview,
  validationGuidance,
} from "@/lib/validation-guidance";
import {
  parseRevisionProductionContext,
  type RevisionProductionContextSnapshot,
  type WorkshopProductionContext,
} from "@/lib/workshop-production-context";
import { RevisionHistory } from "./revision-history";
import {
  WorkshopContextEditor,
  createWorkshopContextDraftState,
  type WorkshopContextDraftState,
} from "./workshop-context-editor";

type BusyAction =
  | "save"
  | "validate"
  | "design-approval"
  | "cam-approval"
  | "generation"
  | "release"
  | "evidence-upload"
  | "evidence-download"
  | "download";

type ActionFeedbackTone = "busy" | "success" | "error";
type HandoffGuidanceMode = "self_build" | "workshop";

interface ActionFeedback {
  tone: ActionFeedbackTone;
  message: string;
}

interface JobPollingIssue {
  jobId: string;
  message: string;
  retryDelayMs: number;
}

export type ProductionApi = Pick<
  CustombuildApiClient,
  | "configured"
  | "listProjects"
  | "getProductionState"
  | "ensureProject"
  | "createVersion"
  | "validateVersion"
  | "approveVersion"
  | "generateVersion"
  | "releaseVersion"
  | "getJob"
  | "listArtifacts"
  | "downloadArtifact"
> & Partial<Pick<
  CustombuildApiClient,
  | "listVersions"
  | "listExternalEvidence"
  | "uploadExternalEvidence"
  | "downloadJointRetentionEvidence"
  | "setJointRetentionEvidence"
>>;

export interface ProductionSummary {
  revision?: number;
  status: string;
  stale: boolean;
  designReviewReady?: boolean;
  physicalCuttingAuthorized?: boolean;
}

export function productionSuggestionPatch(evaluation: RuleEvaluation): Partial<DesignSpec> | undefined {
  const suggestion = evaluation.suggestion;
  if (!suggestion) return undefined;
  if (suggestion.action === "align_base_cabinets" && typeof suggestion.value === "number") {
    return { base_cabinet_count: suggestion.value };
  }
  if (suggestion.action === "set_divider_count" && typeof suggestion.value === "number") {
    return {
      divider_count: suggestion.value,
      bay_sizing_mode: "count",
      reinforcement_mode: "manual",
      bay_width_ratios: [],
    };
  }
  if (suggestion.action === "enable_back" && typeof suggestion.value === "boolean") {
    return { back_panel: suggestion.value };
  }
  return undefined;
}

export { permitsStocklessDesignReview };

export function serverApprovalWarningRuleIds(evaluations: RuleEvaluation[]): string[] {
  return [...new Set(evaluations
    .filter((evaluation) => (
      evaluation.status === "WARNING"
      && (
        /^CB-[A-Z]+-[0-9]{3}$/.test(evaluation.rule_id)
        || evaluation.rule_id === "DFM-GRAIN-001"
      )
    ))
    .map((evaluation) => evaluation.rule_id))]
    .sort((left, right) => left.localeCompare(right));
}

interface ProductionWorkflowProps {
  spec: DesignSpec;
  design: ResolvedDesign;
  onSummaryChange: (summary: ProductionSummary) => void;
  apiClient?: ProductionApi;
  pollIntervalMs?: number;
  projectId?: string;
  projectName?: string;
  templateId?: FurnitureTemplateId;
  onApplyDesignChange?: (patch: Partial<DesignSpec>, reason: string) => void;
  onRequestServerPreviewRetry?: () => void;
  active?: boolean;
  principal?: WorkspaceIdentity & { role?: string };
  showRevisionHistory?: boolean;
  workshopContextDraftState?: WorkshopContextDraftState;
  onWorkshopContextDraftStateChange?: (state: WorkshopContextDraftState) => void;
}

const PROJECT_NAME = "Arkitektväggen";
const ARTIFACT_INTEGRITY_API_MESSAGE = (
  "API 409: Production evidence failed integrity verification; regenerate the package"
);
const STOCK_PROFILE_MISSING_CODE = "STOCK_PROFILE_MISSING";
const DFM_GRAIN_MISSING_CODE = "DFM-GRAIN-001";
const TWO_SIDED_REGISTRATION_MISSING_CODE = "TWO_SIDED_REGISTRATION_MISSING";
const DADO_RETENTION_EVIDENCE_MISSING_CODE = "DADO_RETENTION_EVIDENCE_MISSING";
const BACK_PANEL_RETENTION_EVIDENCE_MISSING_CODE = "BACK_PANEL_RETENTION_EVIDENCE_MISSING";
const RETENTION_RULE_ID = "CB-JOINT-001";
const RETENTION_EVIDENCE_MAX_BYTES = 20 * 1024 * 1024;
const GENERAL_EVIDENCE_TYPES = ["wall_anchor", "hardware"] as const;
type GeneralEvidenceType = typeof GENERAL_EVIDENCE_TYPES[number];
type GeneralEvidenceSelection = Partial<Record<GeneralEvidenceType, string>>;
const GENERATED_REVIEW_REQUIRED_ACTION = (
  "None for design review; physical workshop evidence remains required."
);
const BLOCKED_CAM_REQUIRED_ACTIONS: Record<string, string> = {
  [STOCK_PROFILE_MISSING_CODE]: (
    "Select and server-bind an exact stock profile for every part material, version, "
    + "thickness, blank size and quantity; do not infer sheet size, stock identity or "
    + "machine capacity."
  ),
  [DFM_GRAIN_MISSING_CODE]: (
    "Bind an exact, structured X or Y stock-grain axis for every directional material "
    + "stock profile; opaque evidence or acknowledgement cannot resolve this blocker."
  ),
  [TWO_SIDED_REGISTRATION_MISSING_CODE]: (
    "Bind an externally specified two-sided registration and fixture plan; "
    + "do not infer WCS, pins, fixtures or registration coordinates."
  ),
  [DADO_RETENTION_EVIDENCE_MISSING_CODE]: (
    "Bind current certifier-signed, checksum-addressed mechanical retention evidence "
    + "to every load-bearing carcass DADO application, including exact geometry, compiler, "
    + "hardware quantity, material/thickness and shear/withdrawal capacity; a review "
    + "acknowledgement, adhesive or geometric bearing check cannot replace that evidence."
  ),
  [BACK_PANEL_RETENTION_EVIDENCE_MISSING_CODE]: (
    "Use only the canonical inset back whose four boundary grooves and multi-direction "
    + "closing sequence prove mechanical capture, or bind independently authenticated "
    + "back-panel retention evidence when that application class is implemented."
  ),
};

const GENERAL_EVIDENCE_LABELS: Record<GeneralEvidenceType, string> = {
  wall_anchor: "Väggförankring",
  hardware: "Beslag",
};

type WorkflowCapability = "design" | "review" | "generate";

function hasWorkflowCapability(
  principal: ProductionWorkflowProps["principal"],
  capability: WorkflowCapability,
): boolean {
  if (!principal || !("role" in principal)) return false;
  const role = (principal as { role?: unknown }).role;
  if (role === "admin" || role === "owner") return true;
  if (capability === "review") return role === "reviewer";
  return role === "designer";
}

function canSelectExternalEvidence(principal: ProductionWorkflowProps["principal"]): boolean {
  return hasWorkflowCapability(principal, "review");
}

function canDownloadJointRetentionEvidence(
  principal: ProductionWorkflowProps["principal"],
): boolean {
  if (!principal || !("role" in principal)) return false;
  return ["reviewer", "operator", "production", "admin", "owner"].includes(
    String((principal as { role?: unknown }).role),
  );
}

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

interface RetentionEvidenceUploadMetadata {
  catalogId: string;
  catalogVersion: string;
  expiresAt: string;
}

function requiredRetentionMetadataString(
  value: unknown,
  label: string,
  maxLength: number,
): string {
  if (
    typeof value !== "string"
    || value.length < 1
    || value.length > maxLength
    || value !== value.trim()
  ) {
    throw new Error(`${label} i den signerade JSON-filen är ogiltigt.`);
  }
  return value;
}

/**
 * Read only the signed statement fields needed to mirror multipart metadata.
 * The server remains authoritative for the schema, signature, issuer, hash and revocation checks.
 */
export async function retentionEvidenceUploadMetadata(
  file: File,
  now = Date.now(),
): Promise<RetentionEvidenceUploadMetadata> {
  const mediaType = file.type.split(";", 1)[0]?.trim().toLowerCase();
  if (!file.name.toLowerCase().endsWith(".json") || mediaType !== "application/json") {
    throw new Error("Retentionsevidensen måste vara en .json-fil med innehållstypen application/json.");
  }
  if (!Number.isSafeInteger(file.size) || file.size < 1 || file.size > RETENTION_EVIDENCE_MAX_BYTES) {
    throw new Error("Retentionsevidensen måste innehålla data och får vara högst 20 MiB.");
  }

  let payload: unknown;
  try {
    payload = JSON.parse(await file.text());
  } catch {
    throw new Error("Retentionsevidensen innehåller inte giltig JSON.");
  }
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("Retentionsevidensen måste innehålla ett signerat JSON-objekt.");
  }
  const root = payload as Record<string, unknown>;
  const entry = root.catalogue_entry;
  if (entry === null || typeof entry !== "object" || Array.isArray(entry)) {
    throw new Error("catalogue_entry saknas i den signerade JSON-filen.");
  }
  const catalogueEntry = entry as Record<string, unknown>;
  const catalogId = requiredRetentionMetadataString(
    catalogueEntry.system_id,
    "catalogue_entry.system_id",
    160,
  );
  const catalogVersion = requiredRetentionMetadataString(
    catalogueEntry.system_version,
    "catalogue_entry.system_version",
    80,
  );
  const expiresAt = requiredRetentionMetadataString(root.expires_at, "expires_at", 80);
  if (
    !/T.*(?:Z|[+-]\d{2}:\d{2})$/i.test(expiresAt)
    || !Number.isFinite(Date.parse(expiresAt))
    || Date.parse(expiresAt) <= now
  ) {
    throw new Error("expires_at i den signerade JSON-filen måste vara en framtida tidszonssatt tidpunkt.");
  }
  return { catalogId, catalogVersion, expiresAt };
}

function canonicalJson(value: unknown): string {
  if (
    value === null
    || typeof value === "string"
    || typeof value === "boolean"
    || (typeof value === "number" && Number.isFinite(value))
  ) return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0))
      .map(([key, child]) => `${JSON.stringify(key)}:${canonicalJson(child)}`)
      .join(",")}}`;
  }
  throw new Error("Certifieringsbegäran innehåller ett värde som inte kan serialiseras säkert.");
}

export function canonicalRetentionCertificationRequestJson(
  request: RetentionCertificationRequest,
): string {
  return canonicalJson(request);
}

const WARNING_RULE_ID_PATTERN = /^(CB|DFM)-[A-Z]+-[0-9]{3}$/;
const APPROVAL_EVIDENCE_TYPES = new Set([
  "wall_anchor",
  "hardware",
  "material_grain",
  "joint_retention",
]);

function nonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function timezonedTimestamp(value: unknown): value is string {
  return typeof value === "string"
    && /T.*(?:Z|[+-]\d{2}:\d{2})$/i.test(value)
    && Number.isFinite(Date.parse(value));
}

function canonicalApprovalEvidenceIds(row: Record<string, unknown>): string[] | undefined {
  if (
    !nonEmptyString(row.rule_id)
    || !WARNING_RULE_ID_PATTERN.test(row.rule_id)
    || !nonEmptyString(row.rule_version)
    || !nonEmptyString(row.reason)
    || !nonEmptyString(row.approved_by)
    || !timezonedTimestamp(row.approved_at)
    || !Array.isArray(row.external_evidence)
    || !["verified", "missing", "acknowledged_unresolved"].includes(String(row.evidence_status))
  ) return undefined;

  const ids: string[] = [];
  for (const entry of row.external_evidence) {
    if (entry === null || typeof entry !== "object" || Array.isArray(entry)) return undefined;
    const snapshot = entry as Record<string, unknown>;
    if (
      typeof snapshot.evidence_id !== "string"
      || !UUID_PATTERN.test(snapshot.evidence_id)
      || !nonEmptyString(snapshot.evidence_type)
      || !APPROVAL_EVIDENCE_TYPES.has(snapshot.evidence_type)
      || snapshot.rule_id !== row.rule_id
      || !nonEmptyString(snapshot.catalog_id)
      || !nonEmptyString(snapshot.catalog_version)
      || typeof snapshot.design_hash !== "string"
      || !/^[a-f0-9]{64}$/.test(snapshot.design_hash)
      || typeof snapshot.sha256 !== "string"
      || !/^[a-f0-9]{64}$/.test(snapshot.sha256)
      || !Number.isSafeInteger(snapshot.size_bytes)
      || Number(snapshot.size_bytes) < 1
      || !nonEmptyString(snapshot.content_type)
      || !nonEmptyString(snapshot.created_by)
      || !timezonedTimestamp(snapshot.created_at)
      || !(snapshot.expires_at === null || timezonedTimestamp(snapshot.expires_at))
    ) return undefined;
    ids.push(snapshot.evidence_id);
  }

  if (
    (row.evidence_status === "verified") !== (ids.length > 0)
    || (row.evidence_status === "acknowledged_unresolved" && row.rule_id !== DFM_GRAIN_MISSING_CODE)
  ) return undefined;
  return ids;
}

function legacyApprovalEvidenceIds(row: Record<string, unknown>): string[] | undefined {
  if (
    !nonEmptyString(row.rule_id)
    || !WARNING_RULE_ID_PATTERN.test(row.rule_id)
    || !nonEmptyString(row.reason)
    || !Array.isArray(row.evidence_ids)
    || row.evidence_ids.some((id) => typeof id !== "string" || !UUID_PATTERN.test(id))
  ) return undefined;
  return row.evidence_ids as string[];
}

/**
 * Recover the canonical response snapshots persisted by approve_version.  The request-shaped
 * evidence_ids form is retained only as an all-or-nothing legacy read path for older sessions.
 */
export function approvalExternalEvidenceIds(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const ids: string[] = [];
  let shape: "canonical" | "legacy" | undefined;
  for (const entry of value) {
    if (entry === null || typeof entry !== "object" || Array.isArray(entry)) return undefined;
    const row = entry as Record<string, unknown>;
    const hasCanonicalShape = Object.prototype.hasOwnProperty.call(row, "external_evidence");
    const hasLegacyShape = Object.prototype.hasOwnProperty.call(row, "evidence_ids");
    if (hasCanonicalShape === hasLegacyShape) return undefined;
    const rowShape = hasCanonicalShape ? "canonical" : "legacy";
    if (shape !== undefined && shape !== rowShape) return undefined;
    shape = rowShape;
    const rowIds = rowShape === "canonical"
      ? canonicalApprovalEvidenceIds(row)
      : legacyApprovalEvidenceIds(row);
    if (!rowIds) return undefined;
    ids.push(...rowIds);
  }
  if (new Set(ids).size !== ids.length) return undefined;
  return ids.sort((left, right) => left.localeCompare(right));
}

function retentionBindingFromVersion(version?: DesignVersionRead): {
  baseDesignHash: string;
  evidenceId: string;
} | undefined {
  const result = version?.result_json;
  if (!result || typeof result !== "object" || Array.isArray(result)) return undefined;
  const trust = result.retention_trust;
  if (!trust || typeof trust !== "object" || Array.isArray(trust)) return undefined;
  const trustRecord = trust as Record<string, unknown>;
  const baseDesignHash = trustRecord.base_design_hash;
  const evidenceId = trustRecord.storage_evidence_id;
  if (
    typeof baseDesignHash !== "string"
    || !/^[a-f0-9]{64}$/.test(baseDesignHash)
    || typeof evidenceId !== "string"
    || !/^[0-9a-f-]{36}$/.test(evidenceId)
  ) return undefined;
  return { baseDesignHash, evidenceId };
}

function evidenceMetadataIsCurrent(
  evidence: ExternalEvidenceRead,
  projectId: string,
  designHash: string,
  now = Date.now(),
): boolean {
  const expiry = evidence.expires_at ? Date.parse(evidence.expires_at) : undefined;
  return (
    evidence.project_id === projectId
    && evidence.design_hash === designHash
    && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(evidence.id)
    && /^[a-f0-9]{64}$/.test(evidence.sha256)
    && Number.isSafeInteger(evidence.size_bytes)
    && evidence.size_bytes > 0
    && (expiry === undefined || (Number.isFinite(expiry) && expiry > now))
  );
}

function evidenceOptionLabel(evidence: ExternalEvidenceRead): string {
  const validity = evidence.expires_at
    ? `giltig till ${new Intl.DateTimeFormat("sv-SE", {
        year: "numeric",
        month: "short",
        day: "numeric",
      }).format(new Date(evidence.expires_at))}`
    : "utan registrerat slutdatum";
  return `${evidence.catalog_id} · ${evidence.catalog_version} · ${validity}`;
}

function productionErrorHasCode(message: string | null | undefined, code: string): boolean {
  return message?.split(/[^A-Za-z0-9_]+/).includes(code) ?? false;
}

function generationMachineProfileId(value: string): GenerationRequest["machine_profile_id"] {
  if (
    value === "custombuild-router-1325-linuxcnc"
    || value === "custombuild-router-5125-linuxcnc"
  ) return value;
  throw new ApiError(
    "Den valda maskinprofilen ingår inte i serverns versionslåsta genereringskontrakt.",
  );
}

function productionFailureMessage(message: string): string {
  if (message === ARTIFACT_INTEGRITY_API_MESSAGE) {
    return "Underlaget gick inte att kontrollera. Skapa om det och försök igen.";
  }
  if (productionErrorHasCode(message, STOCK_PROFILE_MISSING_CODE)) {
    return "En exakt lagerprofil saknas. Behåll modellens och lagrets verkliga mått och försök skapa ett lagerobundet designgranskningspaket igen; nesting och CAM ska förbli blockerade.";
  }
  if (productionErrorHasCode(message, DFM_GRAIN_MISSING_CODE)) {
    return "Råskivans fiberriktningsaxel är inte strukturerat serverbunden. Designgranskningspaketet kan skapas, men nesting och CAM ska förbli blockerade; ett dokument eller ett godkännande ersätter inte X/Y-bindningen.";
  }
  if (productionErrorHasCode(message, DADO_RETENTION_EVIDENCE_MISSING_CODE)) {
    return "Not/spår-förbanden saknar versionsbunden torr självlåsning eller mekanisk retention. Designgranskningspaketet kan skapas, men CAM och fysisk frisläppning ska förbli blockerade; lim, bärande geometri eller ett granskningsgodkännande är inte retentionsevidens.";
  }
  if (message.includes("FROZEN_PRODUCTION_CONTEXT_")) {
    return "Skivformat, antal skivor eller maskinval har ändrats. Spara och kontrollera modellen igen.";
  }
  if (message.includes("server deadline")) {
    return "Det tog för lång tid att skapa underlaget. Försök igen.";
  }
  if (message.includes("include_freecad_project") || message.includes("FreeCAD")) {
    return "FreeCAD-projektet kunde inte skapas. Den valfria workern saknar en fungerande headless FreeCAD-installation eller kunde inte importera den auktoritativa STEP-filen. Avmarkera FreeCAD-export eller åtgärda worker-miljön och kör ett nytt granskningsjobb.";
  }
  return message;
}

function errorMessage(error: unknown): string {
  const message = error instanceof Error
    ? error.message
    : "Ett okänt fel inträffade när granskningsunderlaget hanterades.";
  return productionFailureMessage(message);
}

function actionFailureMessage(action: BusyAction, error: unknown): string {
  const message = errorMessage(error);
  const nextAction: Record<BusyAction, string> = {
    save: "Kontrollera serveranslutningen och välj Spara och kontrollera igen.",
    validate: "Modellen är redan sparad. Välj Kontrollera igen.",
    "design-approval": "Bekräfta varningarna igen och välj Skapa underlag.",
    "cam-approval": "Kontrollera paketet och låt en annan reviewer än designgranskaren försöka igen.",
    generation: "Kontrollera orsaken ovan och välj Försök skapa underlag igen.",
    release: "Kontrollera den aktuella CAM-granskningen och bekräfta revisionslåset igen.",
    "evidence-upload": "Kontrollera att filen kommer direkt från certifieraren och försök ladda upp den igen.",
    "evidence-download": "Hämta om serverregistret och försök verifiera originalfilen igen.",
    download: "Försök hämta filen igen.",
  };
  return `${message} ${nextAction[action]}`;
}

function generationElapsedLabel(job: JobRead, now: number): string {
  const startedAt = Date.parse(job.started_at ?? job.created_at);
  if (!Number.isFinite(startedAt)) return "tid ej tillgänglig";
  const elapsedMinutes = Math.max(0, Math.floor((now - startedAt) / 60_000));
  return elapsedMinutes < 1 ? "mindre än 1 min" : `${elapsedMinutes} min`;
}

function generationDeadlineLabel(job: JobRead): string {
  if (!job.deadline_at) return "Servern bevakar jobbet tills det blir klart eller felmarkeras.";
  const deadline = Date.parse(job.deadline_at);
  if (!Number.isFinite(deadline)) return "Servern bevakar jobbet tills det blir klart eller felmarkeras.";
  const formatted = new Intl.DateTimeFormat("sv-SE", {
    hour: "2-digit",
    minute: "2-digit",
  }).format(deadline);
  return `Serverns tidsgräns är ${formatted}.`;
}

export function generationProgressMessage(job: JobRead, now = Date.now()): string {
  const elapsed = generationElapsedLabel(job, now);
  const deadline = generationDeadlineLabel(job);
  if (job.status === "queued") {
    return `Väntar på en ledig worker · ${elapsed}. ${deadline}`;
  }
  const leaseExpiresAt = job.lease_expires_at ? Date.parse(job.lease_expires_at) : Number.NaN;
  if (Number.isFinite(leaseExpiresAt) && leaseExpiresAt <= now) {
    return `Workerns körsignal har löpt ut · ${elapsed}. Servern återställer jobbet automatiskt och sidan fortsätter följa status. ${deadline}`;
  }
  return `Det verifierbara granskningsunderlaget skapas · ${elapsed}. Workern svarar och förnyar sin körsignal automatiskt. ${deadline}`;
}

type ReadinessEvidenceStatus = "VERIFIED" | "MISSING" | "EXTERNAL_EVIDENCE_REQUIRED";

interface ReadinessRequirement {
  code: string;
  title: string;
  status: ReadinessEvidenceStatus;
  evidence: string;
  required_action: string;
}

interface WorkshopReadiness {
  schema_version: "custombuild.workshop-readiness.v2";
  release_scope: "design_review";
  machine_use: "validation_only";
  edge_band_selection_required: boolean;
  design_review_ready: boolean;
  physical_cutting_authorized: false;
  missing_evidence_count: number;
  software_evidence: ReadinessRequirement[];
  workshop_evidence: ReadinessRequirement[];
}

export interface DesignReviewPackageStatus {
  schema_version: "custombuild.design-review-package-status.v1";
  package_status: "READY_FOR_DESIGN_REVIEW";
  cam_status: "VALIDATION_GENERATED" | "BLOCKED";
  blocker_codes: string[];
  operations_included: boolean;
  setup_sheets_included: boolean;
  nesting_included: boolean;
  validation_backplot_included: boolean;
  validation_program_included: boolean;
  physical_cutting_authorized: false;
  required_action: string;
}

const DESIGN_REVIEW_PACKAGE_STATUS_KEYS = [
  "schema_version",
  "package_status",
  "cam_status",
  "blocker_codes",
  "operations_included",
  "setup_sheets_included",
  "nesting_included",
  "validation_backplot_included",
  "validation_program_included",
  "physical_cutting_authorized",
  "required_action",
] as const;

const READINESS_V1_KEYS = [
  "schema_version",
  "design_review_ready",
  "physical_cutting_authorized",
  "missing_evidence_count",
  "software_evidence",
  "workshop_evidence",
] as const;

const READINESS_V2_KEYS = [
  ...READINESS_V1_KEYS,
  "release_scope",
  "machine_use",
  "edge_band_selection_required",
] as const;

const READINESS_REQUIREMENT_KEYS = [
  "code",
  "title",
  "status",
  "evidence",
  "required_action",
] as const;

const SOFTWARE_READINESS_REQUIREMENTS = [
  ["AUTHORITATIVE_CAD", "Authoritative CAD geometry"],
  ["DFM_SCREEN", "Manufacturing feasibility screen"],
  ["SEMANTIC_OPERATIONS", "Semantic machining operations"],
  ["SETUP_SHEETS", "Setup sheets"],
  ["VALIDATION_BACKPLOT", "Independent review backplot"],
  ["NON_CUTTING_PROGRAM", "Non-cutting controller validation"],
] as const;

const WORKSHOP_READINESS_REQUIREMENTS = [
  ["WALL_ANCHOR", "Wall substrate and anchor system"],
  ["CABINET_HARDWARE", "Base-cabinet hardware and drill pattern"],
  ["MATERIAL_GRAIN", "Structured sheet-material grain-axis binding"],
  ["MACHINE_CALIBRATION", "Calibrated physical machine"],
  ["WCS_CONVENTION", "Verified WCS and origin convention"],
  ["MEASURED_TOOLING", "Measured tool, holder and runout"],
  ["MATERIAL_BATCH", "Verified material batch"],
  ["JOINT_COUPONS", "Joint coupon and tolerance test"],
  ["MATERIAL_REMOVAL_COMPARISON", "Independent material-removal comparison"],
  ["SUPERVISED_AIR_CUT", "Supervised air cut"],
  ["REFERENCE_PART", "Measured reference part"],
  ["PROTOTYPE_BUILD", "Complete prototype furniture build"],
  ["CNC_OPERATOR_APPROVAL", "Named CNC operator approval"],
  ["FURNITURE_CONSTRUCTOR_APPROVAL", "Named furniture constructor approval"],
] as const;

const EDGE_BAND_READINESS_REQUIREMENT = [
  "EDGE_BAND_SYSTEM",
  "Adhesive-free mechanical edge protection and cut-size compensation",
] as const;

type WorkshopRequirementCode =
  | (typeof WORKSHOP_READINESS_REQUIREMENTS)[number][0]
  | typeof EDGE_BAND_READINESS_REQUIREMENT[0];

type WorkshopRequirementGroup =
  | "furniture"
  | "materials"
  | "workshop"
  | "verification";

interface WorkshopRequirementPresentation {
  title: string;
  group: WorkshopRequirementGroup;
  owner: string;
  nextAction: string;
}

const WORKSHOP_REQUIREMENT_GROUPS: readonly {
  id: WorkshopRequirementGroup;
  title: string;
  description: string;
}[] = [
  {
    id: "furniture",
    title: "Möbelbeslut och infästning",
    description: "Konstruktionens beslag, förankring och ansvarig möbelgranskning.",
  },
  {
    id: "materials",
    title: "Material och limfria förband",
    description: "Verkligt material, fiberriktning, toleranser och mekanisk hållning.",
  },
  {
    id: "workshop",
    title: "Verkstad och maskin",
    description: "Maskin-, verktygs-, nollpunkts- och körbevis som bara den verkliga verkstaden kan fastställa.",
  },
  {
    id: "verification",
    title: "Provning och godkännande",
    description: "Fysiska prov som återstår efter programvarans designgranskning.",
  },
] as const;

const WORKSHOP_REQUIREMENT_PRESENTATION: Readonly<Record<
  WorkshopRequirementCode,
  WorkshopRequirementPresentation
>> = Object.freeze({
  WALL_ANCHOR: {
    title: "Väggtyp och förankringssystem",
    group: "furniture",
    owner: "Du och möbelkonstruktören",
    nextAction: "Fastställ väggunderlag, exakt infästning, antal och tillverkarens lastdata.",
  },
  CABINET_HARDWARE: {
    title: "Beslag och borrbild för underskåp",
    group: "furniture",
    owner: "Möbelkonstruktören",
    nextAction: "Välj versionslåsta beslag och kontrollera borrbild, öppningsvinkel och frontspel mot uppmätt material.",
  },
  MATERIAL_GRAIN: {
    title: "Fiberriktning i råmaterial",
    group: "materials",
    owner: "Materialleverantören och verkstaden",
    nextAction: "Bind den verkliga råskivans riktning till en strukturerad X- eller Y-axel.",
  },
  MATERIAL_BATCH: {
    title: "Verifierad materialsats",
    group: "materials",
    owner: "Materialleverantören och verkstaden",
    nextAction: "Registrera exakt produkt, batch, verklig tjocklek och versionsbunden materialdata.",
  },
  JOINT_COUPONS: {
    title: "Provbit för förband och tolerans",
    group: "materials",
    owner: "Möbelkonstruktören och verkstaden",
    nextAction: "Tillverka och prova en representativ fog med samma material, verktyg och toleranser som möbeln.",
  },
  EDGE_BAND_SYSTEM: {
    title: "Limfri mekanisk kantskyddslösning",
    group: "materials",
    owner: "Möbelkonstruktören och verkstaden",
    nextAction: "Välj mekaniskt fasthållet kantskydd och bind dess kompensation till delarnas kapmått.",
  },
  MACHINE_CALIBRATION: {
    title: "Kalibrerad fysisk maskin",
    group: "workshop",
    owner: "Verkstaden",
    nextAction: "Dokumentera maskinidentitet, kalibrering och verifierat arbetsområde för den verkliga körningen.",
  },
  WCS_CONVENTION: {
    title: "Nollpunkt och koordinatsystem",
    group: "workshop",
    owner: "CNC-operatören",
    nextAction: "Fastställ och verifiera WCS, origo, uppspänningsriktning och registreringsmetod.",
  },
  MEASURED_TOOLING: {
    title: "Uppmätta verktyg och hållare",
    group: "workshop",
    owner: "CNC-operatören",
    nextAction: "Mät verktygsdiameter, hållare, utstick och kast för den faktiska uppsättningen.",
  },
  MATERIAL_REMOVAL_COMPARISON: {
    title: "Oberoende jämförelse av materialavverkning",
    group: "workshop",
    owner: "Verkstaden",
    nextAction: "Jämför operationerna mot den auktoritativa geometrin innan någon skärande körning.",
  },
  SUPERVISED_AIR_CUT: {
    title: "Övervakad provkörning utan ingrepp",
    group: "workshop",
    owner: "CNC-operatören",
    nextAction: "Kör hela programförloppet utan materialkontakt under operatörens övervakning.",
  },
  REFERENCE_PART: {
    title: "Uppmätt referensdel",
    group: "verification",
    owner: "Verkstaden och möbelkonstruktören",
    nextAction: "Tillverka en representativ del och kontrollmät mått, hål, spår och kanter före serien.",
  },
  PROTOTYPE_BUILD: {
    title: "Komplett fysisk prototyp",
    group: "verification",
    owner: "Du och möbelkonstruktören",
    nextAction: "Bygg hela möbeln och verifiera montering, stabilitet, last, toleranser och förankring.",
  },
  CNC_OPERATOR_APPROVAL: {
    title: "Namngivet godkännande från CNC-operatör",
    group: "verification",
    owner: "CNC-operatören",
    nextAction: "Låt ansvarig operatör granska och godkänna exakt maskinjobb och uppspänning.",
  },
  FURNITURE_CONSTRUCTOR_APPROVAL: {
    title: "Namngivet godkännande från möbelkonstruktör",
    group: "furniture",
    owner: "Möbelkonstruktören",
    nextAction: "Låt ansvarig konstruktör godkänna den exakta revisionen och den fysiskt provade lösningen.",
  },
});

export function workshopRequirementPresentation(
  code: string,
): WorkshopRequirementPresentation {
  const presentation = WORKSHOP_REQUIREMENT_PRESENTATION[code as WorkshopRequirementCode];
  return presentation ?? {
    title: `Okänt externt krav (${code})`,
    group: "verification",
    owner: "Ansvarig granskare",
    nextAction: "Stoppa fysisk tillverkning och utred det okända kravet innan arbetet fortsätter.",
  };
}

function hasExactKeys(
  value: Record<string, unknown>,
  expectedKeys: readonly string[],
): boolean {
  const actualKeys = Object.keys(value);
  return actualKeys.length === expectedKeys.length
    && expectedKeys.every((key) => Object.prototype.hasOwnProperty.call(value, key));
}

const GENERATED_CAM_REVIEW_ARTIFACT_KINDS = [
  "production_bundle",
  "manifest",
  "manufacturing_intent",
  "supplier_handoff",
  "dfm_report",
  "stock_selection",
  "generation_plan",
  "operations",
  "validation_backplot",
  "design_glb",
  "setup_sheet_001",
  "workshop_readiness",
] as const;

const BLOCKED_CAM_REVIEW_ARTIFACT_KINDS = [
  "production_bundle",
  "manifest",
  "manufacturing_intent",
  "supplier_handoff",
  "dfm_report",
  "stock_selection",
  "generation_plan",
  "design_glb",
  "workshop_readiness",
  "design_review_package_status",
] as const;

const BLOCKED_CAM_ALLOWED_ARTIFACT_KINDS = new Set([
  "production_bundle",
  "manifest",
  "manufacturing_intent",
  "supplier_handoff",
  "dfm_report",
  "stock_selection",
  "generation_plan",
  "design_review_package_status",
  "design_glb",
  "workshop_readiness",
  "design_fcstd",
  "cad_interchange_status",
  "source_provenance",
  "assembly_readiness",
]);

const GENERATED_CAM_ALLOWED_ARTIFACT_KINDS = new Set([
  ...BLOCKED_CAM_ALLOWED_ARTIFACT_KINDS,
  "operations",
  "validation_backplot",
]);
const SETUP_SHEET_KIND_PATTERN = /^setup_sheet_(?:00[1-9]|0[1-9]\d|[1-4]\d\d|5(?:0\d|1[0-2]))$/;
const MAX_REVIEW_EVIDENCE_ARTIFACTS = 512;
const MAX_REVIEW_EVIDENCE_BYTES = 96 * 1024 * 1024;
const MAX_REVIEW_ARTIFACT_BYTES = 32 * 1024 * 1024;
const MAX_REVIEW_CORE_DOCUMENT_BYTES = 3 * 1024 * 1024;
const MAX_REVIEW_READINESS_BYTES = 64 * 1024;
const REVIEW_EVIDENCE_RESULT_KEYS = [
  "kind",
  "object_key",
  "sha256",
  "size_bytes",
  "content_type",
] as const;
const REVIEW_ARTIFACT_CONTENT_TYPES: Readonly<Record<string, string>> = {
  manufacturing_intent: "application/json",
  supplier_handoff: "application/json",
  dfm_report: "application/json",
  stock_selection: "application/json",
  generation_plan: "application/json",
  operations: "application/json",
  validation_backplot: "image/svg+xml",
  design_glb: "model/gltf-binary",
  design_fcstd: "application/vnd.freecad",
  cad_interchange_status: "application/json",
  source_provenance: "application/json",
  workshop_readiness: "application/json",
  assembly_readiness: "application/json",
  design_review_package_status: "application/json",
};
const REVIEW_READINESS_ARTIFACT_KINDS = new Set([
  "workshop_readiness",
  "design_review_package_status",
  "assembly_readiness",
  "cad_interchange_status",
]);
const REVIEW_CORE_DOCUMENT_ARTIFACT_KINDS = new Set([
  "manifest",
  "dfm_report",
  "stock_selection",
  "generation_plan",
  "manufacturing_intent",
  "operations",
  "supplier_handoff",
  "source_provenance",
]);

function reviewArtifactKindIsAllowed(kind: string): boolean {
  return GENERATED_CAM_ALLOWED_ARTIFACT_KINDS.has(kind)
    || SETUP_SHEET_KIND_PATTERN.test(kind);
}

function expectedReviewArtifactContentType(kind: string): string | undefined {
  return SETUP_SHEET_KIND_PATTERN.test(kind)
    ? "image/svg+xml"
    : REVIEW_ARTIFACT_CONTENT_TYPES[kind];
}

function reviewArtifactSizeLimit(kind: string): number {
  if (REVIEW_READINESS_ARTIFACT_KINDS.has(kind)) return MAX_REVIEW_READINESS_BYTES;
  if (REVIEW_CORE_DOCUMENT_ARTIFACT_KINDS.has(kind) || SETUP_SHEET_KIND_PATTERN.test(kind)) {
    return MAX_REVIEW_CORE_DOCUMENT_BYTES;
  }
  return MAX_REVIEW_ARTIFACT_BYTES;
}

export function reviewArtifactKindsFromJob(job?: JobRead): string[] | undefined {
  const result = job?.result_json;
  if (
    job?.status !== "succeeded"
    || !result
    || typeof result !== "object"
    || Array.isArray(result)
    || typeof result.bundle_object_key !== "string"
    || result.bundle_object_key.length < 1
    || typeof result.bundle_sha256 !== "string"
    || !/^[a-f0-9]{64}$/.test(result.bundle_sha256)
    || !Number.isSafeInteger(result.bundle_size_bytes)
    || Number(result.bundle_size_bytes) < 1
    || Number(result.bundle_size_bytes) > MAX_REVIEW_ARTIFACT_BYTES
    || typeof result.manifest_object_key !== "string"
    || result.manifest_object_key.length < 1
    || typeof result.manifest_sha256 !== "string"
    || !/^[a-f0-9]{64}$/.test(result.manifest_sha256)
    || !Number.isSafeInteger(result.manifest_size_bytes)
    || Number(result.manifest_size_bytes) < 1
    || Number(result.manifest_size_bytes) > MAX_REVIEW_CORE_DOCUMENT_BYTES
    || !Array.isArray(result.evidence_artifacts)
    || result.evidence_artifacts.length > MAX_REVIEW_EVIDENCE_ARTIFACTS
  ) return undefined;

  const kinds = ["production_bundle", "manifest"];
  const objectKeys = new Set([result.bundle_object_key, result.manifest_object_key]);
  let totalBytes = 0;
  for (const value of result.evidence_artifacts) {
    if (value === null || typeof value !== "object" || Array.isArray(value)) return undefined;
    const artifact = value as Record<string, unknown>;
    const kind = artifact.kind;
    const sizeBytes = artifact.size_bytes;
    const objectKey = artifact.object_key;
    if (
      !hasExactKeys(artifact, REVIEW_EVIDENCE_RESULT_KEYS)
      || typeof kind !== "string"
      || !reviewArtifactKindIsAllowed(kind)
      || typeof objectKey !== "string"
      || objectKey.length < 1
      || objectKeys.has(objectKey)
      || typeof artifact.sha256 !== "string"
      || !/^[a-f0-9]{64}$/.test(artifact.sha256)
      || !Number.isSafeInteger(sizeBytes)
      || Number(sizeBytes) < 1
      || Number(sizeBytes) > reviewArtifactSizeLimit(kind)
      || artifact.content_type !== expectedReviewArtifactContentType(kind)
    ) return undefined;
    totalBytes += Number(sizeBytes);
    if (totalBytes > MAX_REVIEW_EVIDENCE_BYTES) return undefined;
    kinds.push(kind);
    objectKeys.add(objectKey);
  }
  if (new Set(kinds.map((kind) => kind.toLowerCase())).size !== kinds.length) {
    return undefined;
  }
  return kinds;
}

export function reviewBundleSha256FromJob(job?: JobRead): string | undefined {
  const result = job?.result_json;
  if (
    job?.status !== "succeeded"
    || !result
    || typeof result !== "object"
    || Array.isArray(result)
    || typeof result.bundle_sha256 !== "string"
    || !/^[a-f0-9]{64}$/.test(result.bundle_sha256)
  ) return undefined;
  return result.bundle_sha256;
}

export function reviewBundleArtifactMatchesJob(
  artifacts: readonly Pick<ArtifactRead, "kind" | "sha256" | "content_type">[],
  bundleSha256: string | undefined,
): boolean {
  if (!bundleSha256) return false;
  const bundles = artifacts.filter((artifact) => artifact.kind === "production_bundle");
  const bundle = bundles[0];
  return bundles.length === 1
    && bundle?.content_type === "application/zip"
    && bundle.sha256 === bundleSha256;
}

export function blockedCamEvidenceKindIsForbidden(kind: string): boolean {
  return !BLOCKED_CAM_ALLOWED_ARTIFACT_KINDS.has(kind);
}

export function reviewPackageArtifactInventoryIsTruthful(
  artifacts: readonly Pick<ArtifactRead, "kind">[],
  status: Pick<
    DesignReviewPackageStatus,
    "cam_status" | "validation_program_included"
  > | undefined,
  statusClaimed = status !== undefined,
  expectedKinds?: readonly string[],
): boolean {
  if (statusClaimed !== (status !== undefined)) return false;
  if (!status || !statusClaimed || !expectedKinds) return false;
  const kinds = artifacts.map((artifact) => artifact.kind);
  if (
    kinds.some((kind) => !kind)
    || new Set(kinds.map((kind) => kind.toLowerCase())).size !== kinds.length
    || new Set(expectedKinds.map((kind) => kind.toLowerCase())).size !== expectedKinds.length
    || expectedKinds.some((kind) => !reviewArtifactKindIsAllowed(kind))
    || kinds.length !== expectedKinds.length
    || expectedKinds.some((kind) => !kinds.includes(kind))
  ) return false;

  const requiredKinds = status.cam_status === "BLOCKED"
    ? BLOCKED_CAM_REVIEW_ARTIFACT_KINDS
    : [...GENERATED_CAM_REVIEW_ARTIFACT_KINDS, "design_review_package_status"];
  if (!requiredKinds.every((kind) => kinds.includes(kind))) return false;

  if (status.cam_status === "BLOCKED") {
    return !kinds.some(blockedCamEvidenceKindIsForbidden);
  }
  return status.validation_program_included === true
    && kinds.every((kind) => GENERATED_CAM_ALLOWED_ARTIFACT_KINDS.has(kind)
      || SETUP_SHEET_KIND_PATTERN.test(kind));
}

export function designReviewPackageStatusFromJob(
  job?: JobRead,
): DesignReviewPackageStatus | undefined {
  const result = job?.result_json;
  if (!result || typeof result !== "object" || Array.isArray(result)) return undefined;
  const value = result.design_review_package_status;
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const raw = value as Record<string, unknown>;
  if (
    !hasExactKeys(raw, DESIGN_REVIEW_PACKAGE_STATUS_KEYS)
    || raw.schema_version !== "custombuild.design-review-package-status.v1"
    || raw.package_status !== "READY_FOR_DESIGN_REVIEW"
    || (raw.cam_status !== "VALIDATION_GENERATED" && raw.cam_status !== "BLOCKED")
    || raw.physical_cutting_authorized !== false
    || typeof raw.required_action !== "string"
    || !raw.required_action.trim()
    || !Array.isArray(raw.blocker_codes)
    || raw.blocker_codes.some((code) => typeof code !== "string" || !code)
  ) return undefined;
  const blockerCodes = raw.blocker_codes as string[];
  const canonicalCodes = [...new Set(blockerCodes)].sort((left, right) => (
    left.localeCompare(right)
  ));
  if (
    canonicalCodes.length !== blockerCodes.length
    || canonicalCodes.some((code, index) => code !== blockerCodes[index])
  ) return undefined;
  const flagNames = [
    "operations_included",
    "setup_sheets_included",
    "nesting_included",
    "validation_backplot_included",
    "validation_program_included",
  ] as const;
  if (flagNames.some((name) => typeof raw[name] !== "boolean")) return undefined;
  if (raw.cam_status === "BLOCKED") {
    const blockerCode = blockerCodes.length === 1 ? blockerCodes[0] : undefined;
    const expectedAction = blockerCode
      ? BLOCKED_CAM_REQUIRED_ACTIONS[blockerCode]
      : undefined;
    if (
      !expectedAction
      || raw.required_action !== expectedAction
      || flagNames.some((name) => raw[name] !== false)
    ) {
      return undefined;
    }
  } else if (
    blockerCodes.length > 0
    || raw.required_action !== GENERATED_REVIEW_REQUIRED_ACTION
    || raw.operations_included !== true
    || raw.setup_sheets_included !== true
    || raw.nesting_included !== true
    || raw.validation_backplot_included !== true
  ) return undefined;
  return {
    schema_version: "custombuild.design-review-package-status.v1",
    package_status: "READY_FOR_DESIGN_REVIEW",
    cam_status: raw.cam_status,
    blocker_codes: [...blockerCodes],
    operations_included: raw.operations_included as boolean,
    setup_sheets_included: raw.setup_sheets_included as boolean,
    nesting_included: raw.nesting_included as boolean,
    validation_backplot_included: raw.validation_backplot_included as boolean,
    validation_program_included: raw.validation_program_included as boolean,
    physical_cutting_authorized: false,
    required_action: raw.required_action,
  };
}

function readinessRequirement(
  value: unknown,
  expected: readonly [string, string],
  allowedStatuses: readonly ReadinessEvidenceStatus[],
): ReadinessRequirement | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const raw = value as Record<string, unknown>;
  const status = raw.status;
  if (
    !hasExactKeys(raw, READINESS_REQUIREMENT_KEYS)
    || READINESS_REQUIREMENT_KEYS.some((key) => (
      typeof raw[key] !== "string" || !(raw[key] as string).trim()
    ))
    || raw.code !== expected[0]
    || raw.title !== expected[1]
    || !allowedStatuses.includes(status as ReadinessEvidenceStatus)
  ) return undefined;
  return {
    code: raw.code as string,
    title: raw.title as string,
    status: status as ReadinessEvidenceStatus,
    evidence: raw.evidence as string,
    required_action: raw.required_action as string,
  };
}

function readinessRequirementList(
  value: unknown,
  expected: readonly (readonly [string, string])[],
  allowedStatuses: readonly ReadinessEvidenceStatus[],
): ReadinessRequirement[] | undefined {
  if (!Array.isArray(value) || value.length !== expected.length) return undefined;
  const requirements = expected.map((identity, index) => (
    readinessRequirement(value[index], identity, allowedStatuses)
  ));
  return requirements.some((item) => !item)
    ? undefined
    : requirements as ReadinessRequirement[];
}

export function workshopReadinessFromJob(job?: JobRead): WorkshopReadiness | undefined {
  const result = job?.result_json;
  if (!result || typeof result !== "object" || Array.isArray(result)) return undefined;
  const value = result.workshop_readiness;
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const raw = value as Record<string, unknown>;
  const schemaVersion = raw.schema_version;
  const isV2 = schemaVersion === "custombuild.workshop-readiness.v2";
  const isLegacyV1 = schemaVersion === "custombuild.workshop-readiness.v1";
  if (
    (!isV2 && !isLegacyV1)
    || !hasExactKeys(raw, isV2 ? READINESS_V2_KEYS : READINESS_V1_KEYS)
  ) return undefined;

  const hasMachineProgramMode = Object.prototype.hasOwnProperty.call(
    result,
    "machine_program_mode",
  );
  const hasProductionMachineProgram = Object.prototype.hasOwnProperty.call(
    result,
    "production_machine_program",
  );
  const hasSafeMachineProgramPair = result.machine_program_mode === "VALIDATION_DRY_RUN"
    && result.production_machine_program === false;
  const packageStatus = designReviewPackageStatusFromJob(job);
  const hasBlockedCamProgramPair = packageStatus?.cam_status === "BLOCKED"
    && result.machine_program_mode === "CAM_BLOCKED"
    && result.production_machine_program === false;
  const hasExpectedMachineProgramPair = packageStatus?.cam_status === "BLOCKED"
    ? hasBlockedCamProgramPair
    : hasSafeMachineProgramPair;
  if (
    (
      isV2
      && (
        !hasMachineProgramMode
        || !hasProductionMachineProgram
        || !hasExpectedMachineProgramPair
      )
    )
    || (
      isLegacyV1
      && (
        hasMachineProgramMode !== hasProductionMachineProgram
        || (hasMachineProgramMode && !hasSafeMachineProgramPair)
      )
    )
  ) return undefined;

  let edgeBandSelectionRequired: boolean;
  if (isV2) {
    if (
      raw.release_scope !== "design_review"
      || raw.machine_use !== "validation_only"
      || typeof raw.edge_band_selection_required !== "boolean"
    ) return undefined;
    edgeBandSelectionRequired = raw.edge_band_selection_required;
  } else {
    if (!Array.isArray(raw.workshop_evidence)) return undefined;
    edgeBandSelectionRequired = (
      raw.workshop_evidence.length === WORKSHOP_READINESS_REQUIREMENTS.length + 1
    );
  }

  if (
    typeof raw.design_review_ready !== "boolean"
    || raw.physical_cutting_authorized !== false
    || typeof raw.missing_evidence_count !== "number"
    || !Number.isInteger(raw.missing_evidence_count)
    || raw.missing_evidence_count < 0
  ) return undefined;

  const softwareEvidence = readinessRequirementList(
    raw.software_evidence,
    SOFTWARE_READINESS_REQUIREMENTS,
    ["VERIFIED", "MISSING"],
  );
  const workshopExpected = edgeBandSelectionRequired
    ? [...WORKSHOP_READINESS_REQUIREMENTS, EDGE_BAND_READINESS_REQUIREMENT]
    : WORKSHOP_READINESS_REQUIREMENTS;
  const workshopEvidence = readinessRequirementList(
    raw.workshop_evidence,
    workshopExpected,
    ["VERIFIED", "EXTERNAL_EVIDENCE_REQUIRED"],
  );
  if (!softwareEvidence || !workshopEvidence) return undefined;
  const evidence = [...softwareEvidence, ...workshopEvidence];
  if (
    evidence.filter((item) => item.status !== "VERIFIED").length !== raw.missing_evidence_count
    || softwareEvidence.every((item) => item.status === "VERIFIED") !== raw.design_review_ready
  ) return undefined;
  return {
    schema_version: "custombuild.workshop-readiness.v2",
    release_scope: "design_review",
    machine_use: "validation_only",
    edge_band_selection_required: edgeBandSelectionRequired,
    design_review_ready: raw.design_review_ready,
    physical_cutting_authorized: false,
    missing_evidence_count: raw.missing_evidence_count,
    software_evidence: softwareEvidence,
    workshop_evidence: workshopEvidence,
  };
}

function formatArtifactSize(sizeBytes: number): string {
  if (sizeBytes < 1_000) return `${sizeBytes} B`;
  if (sizeBytes < 1_000_000) return `${(sizeBytes / 1_000).toFixed(1)} kB`;
  return `${(sizeBytes / 1_000_000).toFixed(1)} MB`;
}

export function artifactRoleLabel(kind: string): string {
  if (kind === "production_bundle") return "Designgranskningspaket (ZIP)";
  if (kind === "manifest") return "Manifest";
  if (kind === "dfm_report") return "Tillverkningsbarhetskontroll";
  if (kind === "stock_selection") return "Lagerurval";
  if (kind === "generation_plan") return "Genereringsplan";
  if (kind === "operations") return "Semantiska operationer";
  if (kind === "validation_backplot") return "Valideringsbackplot";
  if (kind === "design_glb") return "3D-modell för granskning";
  if (kind === "design_fcstd") return "FreeCAD-projekt";
  if (kind === "cad_interchange_status") return "CAD-interchangekontroll";
  if (kind === "source_provenance") return "Källproveniens";
  if (kind === "workshop_readiness") return "Readinessbevis";
  if (kind === "design_review_package_status") return "Status för designgranskningspaket";
  if (kind === "assembly_readiness") return "Monteringskontroll";
  if (kind === "supplier_handoff") return "Leverantörsöverlämning";
  if (kind === "manufacturing_intent") return "Maskinneutralt bearbetningsunderlag";
  if (kind.startsWith("setup_sheet_")) return "Setupblad";
  return kind.replaceAll("_", " ");
}

function artifactFormatLabel(artifact: Pick<ArtifactRead, "kind" | "content_type">): string {
  if (artifact.kind === "design_fcstd") return "FreeCAD";
  if (artifact.kind === "design_glb" || artifact.content_type === "model/gltf-binary") return "GLB";
  if (artifact.content_type === "application/zip") return "ZIP";
  if (artifact.content_type === "application/json") return "JSON";
  if (artifact.content_type === "image/svg+xml") return "SVG";
  if (artifact.content_type === "application/pdf") return "PDF";
  if (artifact.content_type === "text/csv") return "CSV";
  return artifact.content_type;
}

export function artifactReviewUseLabel(kind: string): string {
  if (kind === "production_bundle") return "Samlat underlag för designgranskning";
  if (kind === "design_glb") return "Visuell 3D-granskning";
  if (kind === "design_fcstd") return "Valfri CAD-granskning";
  if (kind === "validation_backplot") return "Icke-skärande validering";
  if (kind.startsWith("setup_sheet_")) return "Setupgranskning – inte arbetsorder";
  if (kind === "operations") return "Semantisk kontroll – inte körbar CNC-kod";
  if (kind === "supplier_handoff") {
    return "Överlämning till CNC-verkstad för granskning – inte arbetsorder eller körbar CNC-kod";
  }
  if (kind === "manufacturing_intent") {
    return "Maskinneutralt bearbetningsunderlag för CNC-verkstadens granskning – inte körbar CNC-kod";
  }
  return "Verifieringsbevis för designgranskning";
}

export function artifactFileExtension(
  artifact: Pick<ArtifactRead, "kind" | "content_type">,
): string {
  if (artifact.kind === "supplier_handoff" || artifact.kind === "manufacturing_intent") {
    return "json";
  }
  if (artifact.kind === "design_fcstd") return "FCStd";
  if (artifact.kind === "design_glb" || artifact.content_type === "model/gltf-binary") return "glb";
  if (artifact.content_type === "application/zip") return "zip";
  if (artifact.content_type === "application/json") return "json";
  if (artifact.content_type === "image/svg+xml") return "svg";
  if (artifact.content_type === "application/pdf") return "pdf";
  if (artifact.content_type === "text/csv") return "csv";
  return "bin";
}

export function artifactDownloadFileName(
  artifact: Pick<ArtifactRead, "kind" | "content_type">,
  projectId: string,
  revision: number,
): string {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(projectId)) {
    throw new ApiError("Artefaktens projektidentitet är ogiltig.");
  }
  if (!Number.isSafeInteger(revision) || revision < 1) {
    throw new ApiError("Artefaktens revision är ogiltig.");
  }
  const canonicalIdentities: Record<string, readonly [string, string, string]> = {
    production_bundle: ["design-review", "application/zip", "zip"],
    manifest: ["design-review-manifest", "application/json", "json"],
    manufacturing_intent: ["manufacturing-intent", "application/json", "json"],
    supplier_handoff: ["cnc-shop-handoff", "application/json", "json"],
    dfm_report: ["dfm-report", "application/json", "json"],
    design_review_package_status: ["design-review-package-status", "application/json", "json"],
    stock_selection: ["stock-selection", "application/json", "json"],
    generation_plan: ["generation-plan", "application/json", "json"],
    operations: ["machine-neutral-operations", "application/json", "json"],
    validation_backplot: ["validation-backplot", "image/svg+xml", "svg"],
    design_glb: ["design", "model/gltf-binary", "glb"],
    design_fcstd: ["design", "application/vnd.freecad", "FCStd"],
    cad_interchange_status: ["cad-interchange-status", "application/json", "json"],
    source_provenance: ["source-provenance", "application/json", "json"],
    workshop_readiness: ["workshop-readiness", "application/json", "json"],
    assembly_readiness: ["assembly-readiness", "application/json", "json"],
  };
  let identity = canonicalIdentities[artifact.kind];
  if (!identity) {
    const setupMatch = /^setup_sheet_([0-9]{3})$/.exec(artifact.kind);
    if (setupMatch) identity = [`setup-sheet-${setupMatch[1]}`, "image/svg+xml", "svg"];
  }
  if (!identity || artifact.content_type !== identity[1]) {
    throw new ApiError("Artefaktens typ och medieformat matchar inte serverns filnamnskontrakt.");
  }
  return `custombuild-project-${projectId}-${identity[0]}-rev-${revision}.${identity[2]}`;
}

function sameArtifactIdentity(left: ArtifactRead, right: ArtifactRead): boolean {
  return left.id === right.id
    && left.kind === right.kind
    && left.sha256 === right.sha256
    && left.size_bytes === right.size_bytes
    && left.content_type === right.content_type;
}

const CUSTOMER_REVIEW_DOCUMENTS = [
  ["Monteringsmanual", "PDF", "Granskningskopia – inte frisläppt monteringsinstruktion"],
  ["Material- och komponentlista (BOM)", "PDF", "Kvantitets- och måttgranskning"],
  ["Beslagslista", "CSV", "Inköps- och konstruktionsgranskning"],
  ["Deletiketter", "PDF", "Identifiering mot designhash och del-ID"],
  ["Mätprotokoll", "PDF", "Underlag för fysisk verifiering"],
] as const;

export function ProductionWorkflow({
  spec,
  design,
  onSummaryChange,
  apiClient,
  pollIntervalMs = 2_000,
  projectId,
  projectName = PROJECT_NAME,
  templateId = "shelving",
  onApplyDesignChange,
  onRequestServerPreviewRetry,
  active = true,
  principal,
  showRevisionHistory = false,
  workshopContextDraftState,
  onWorkshopContextDraftStateChange,
}: ProductionWorkflowProps) {
  const defaultApi = useMemo(() => new CustombuildApiClient(), []);
  const api = apiClient ?? defaultApi;
  const [version, setVersion] = useState<DesignVersionRead>();
  const [job, setJob] = useState<JobRead>();
  const [artifacts, setArtifacts] = useState<ArtifactRead[]>([]);
  const [designApproved, setDesignApproved] = useState(false);
  const [designApproverId, setDesignApproverId] = useState<string>();
  const [camApprovedJobId, setCamApprovedJobId] = useState<string>();
  const [camApproverId, setCamApproverId] = useState<string>();
  const [release, setRelease] = useState<ReleaseRead>();
  const [warningsAcknowledged, setWarningsAcknowledged] = useState(false);
  const [camApprovalConfirmed, setCamApprovalConfirmed] = useState(false);
  const [releaseConfirmed, setReleaseConfirmed] = useState(false);
  const [busy, setBusy] = useState<BusyAction>();
  const [error, setError] = useState<string>();
  const [actionFeedback, setActionFeedback] = useState<ActionFeedback>();
  const [jobPollingIssue, setJobPollingIssue] = useState<JobPollingIssue>();
  const [jobPollRetryRequest, setJobPollRetryRequest] = useState(0);
  const [downloadingArtifactId, setDownloadingArtifactId] = useState<string>();
  const [handoffGuidanceMode, setHandoffGuidanceMode] = useState<HandoffGuidanceMode>("self_build");
  const [loadedStorageKey, setLoadedStorageKey] = useState<string>();
  const [serverSynchronized, setServerSynchronized] = useState(false);
  const [synchronizing, setSynchronizing] = useState(false);
  const [serverProjectId, setServerProjectId] = useState<string>();
  const [externalEvidence, setExternalEvidence] = useState<ExternalEvidenceRead[]>([]);
  const [selectedRetentionEvidenceId, setSelectedRetentionEvidenceId] = useState<string>();
  const [selectedGeneralEvidence, setSelectedGeneralEvidence] = useState<GeneralEvidenceSelection>({});
  const [approvedGeneralEvidenceIds, setApprovedGeneralEvidenceIds] = useState<string[]>([]);
  const [approvalEvidenceValid, setApprovalEvidenceValid] = useState(true);
  const [localWorkshopContextDraftState, setLocalWorkshopContextDraftState] =
    useState<WorkshopContextDraftState>(
      () => createWorkshopContextDraftState(spec, spec.workshop_context),
    );
  const [evidenceLoading, setEvidenceLoading] = useState(false);
  const [evidenceLoadError, setEvidenceLoadError] = useState<string>();
  const saveBlockReasonId = useId();
  const warningAcknowledgementHelpId = useId();
  const handoffGuidanceName = useId();
  const retentionEvidenceSelectId = useId();
  const retentionEvidenceUploadId = useId();
  const retentionEvidenceUploadHelpId = useId();
  const nextActionHeadingRef = useRef<HTMLHeadingElement>(null);
  const previousActiveStepRef = useRef<number | undefined>(undefined);
  const stepFocusInitializedRef = useRef(false);
  const pendingDownloadUrlsRef = useRef(new Map<string, number | undefined>());
  const retentionSpecSignatureRef = useRef<string | undefined>(undefined);
  const storageKey = productionSessionKey(principal, projectId, spec.design_id);
  const retentionSpecSignature = JSON.stringify(spec);
  const activeWorkshopContextDraftState = workshopContextDraftState
    ?? localWorkshopContextDraftState;
  const workshopContextBlocked = activeWorkshopContextDraftState.dirty
    || !activeWorkshopContextDraftState.valid;
  const workshopContextBlockReason = workshopContextBlocked
    ? "Verkstadsprofilen har osparade eller ofullständiga uppgifter. Slutför alla fält eller välj Återgå till lagerobundet paket innan du sparar, godkänner eller skapar underlag."
    : undefined;

  const updateWorkshopContextDraftState = useCallback((state: WorkshopContextDraftState) => {
    setLocalWorkshopContextDraftState(state);
    onWorkshopContextDraftStateChange?.(state);
  }, [onWorkshopContextDraftStateChange]);

  useEffect(() => {
    onWorkshopContextDraftStateChange?.(activeWorkshopContextDraftState);
  }, [activeWorkshopContextDraftState, onWorkshopContextDraftStateChange]);

  useEffect(() => () => {
    for (const [objectUrl, timer] of pendingDownloadUrlsRef.current) {
      if (timer !== undefined) window.clearTimeout(timer);
      URL.revokeObjectURL(objectUrl);
    }
    pendingDownloadUrlsRef.current.clear();
  }, []);

  useEffect(() => {
    clearLegacyProductionStorage(window.localStorage);
    let cancelled = false;
    const stored = readProductionSession(window.sessionStorage, storageKey);
    queueMicrotask(() => {
      if (cancelled) return;
      setVersion(stored?.version);
      setJob(stored?.job);
      setArtifacts([]);
      setDesignApproved(Boolean(stored?.designApproved));
      setDesignApproverId(undefined);
      setCamApprovedJobId(undefined);
      setCamApproverId(undefined);
      setRelease(stored?.release);
      setWarningsAcknowledged(false);
      setCamApprovalConfirmed(false);
      setReleaseConfirmed(false);
      setActionFeedback(undefined);
      setError(undefined);
      setExternalEvidence([]);
      setSelectedGeneralEvidence({});
      setApprovedGeneralEvidenceIds([]);
      setApprovalEvidenceValid(true);
      setEvidenceLoadError(undefined);
      setLoadedStorageKey(storageKey);
    });
    return () => { cancelled = true; };
  }, [storageKey]);

  useEffect(() => {
    if (!active || loadedStorageKey !== storageKey || !api.configured) return;
    let cancelled = false;

    void (async () => {
      await Promise.resolve();
      if (cancelled) return;
      setSynchronizing(true);
      setServerSynchronized(false);
      try {
        const projects = projectId ? [] : await api.listProjects();
        const resolvedProjectId = projectId
          ?? projects.find((candidate) => candidate.name === projectName)?.id;
        if (!resolvedProjectId) {
          if (cancelled) return;
          setVersion(undefined);
          setJob(undefined);
          setArtifacts([]);
          setDesignApproved(false);
          setDesignApproverId(undefined);
          setCamApprovedJobId(undefined);
          setCamApproverId(undefined);
          setRelease(undefined);
          setWarningsAcknowledged(false);
          setCamApprovalConfirmed(false);
          setReleaseConfirmed(false);
          setServerProjectId(undefined);
          setSelectedRetentionEvidenceId(undefined);
          setExternalEvidence([]);
          setApprovedGeneralEvidenceIds([]);
          setApprovalEvidenceValid(true);
          setServerSynchronized(true);
          setError(undefined);
          return;
        }

        const state = await api.getProductionState(resolvedProjectId);
        if (cancelled) return;
        const restoredVersion = state.version ?? undefined;
        const restoredRetention = retentionBindingFromVersion(restoredVersion);
        api.setJointRetentionEvidence?.(resolvedProjectId, restoredRetention?.evidenceId);
        let restoredJob = state.latest_job ?? undefined;
        const designApproval = state.approvals.find((approval) => approval.approval_type === "design");
        const camApproval = state.approvals.find((approval) => approval.approval_type === "cam");
        const restoredApprovalEvidence = designApproval
          ? approvalExternalEvidenceIds(designApproval.overrides_json)
          : [];
        let restoredArtifacts: ArtifactRead[] = [];
        if (restoredJob?.status === "succeeded") {
          try {
            restoredArtifacts = await api.listArtifacts(restoredJob.id);
            if (cancelled) return;
          } catch (artifactError) {
            if (!(artifactError instanceof ApiError) || artifactError.status !== 409) {
              throw artifactError;
            }
            // A deployment may intentionally invalidate an old generation context.
            // Restore the design revision without advertising stale evidence as current.
            restoredJob = undefined;
          }
        }

        setVersion(restoredVersion);
        setServerProjectId(resolvedProjectId);
        setSelectedRetentionEvidenceId(restoredRetention?.evidenceId);
        setSelectedGeneralEvidence({});
        setApprovedGeneralEvidenceIds(restoredApprovalEvidence ?? []);
        setApprovalEvidenceValid(restoredApprovalEvidence !== undefined);
        setJob(restoredJob);
        setArtifacts(restoredArtifacts);
        setDesignApproved(Boolean(designApproval));
        setDesignApproverId(designApproval?.approved_by);
        setCamApprovedJobId(
          restoredJob?.status === "succeeded"
          && camApproval?.generation_job_id === restoredJob.id
          && camApproval.manifest_sha256 === restoredJob.result_json?.manifest_sha256
          && camApproval.production_context_hash === restoredJob.production_context_hash
            ? restoredJob.id
            : undefined,
        );
        setCamApproverId(camApproval?.approved_by);
        setRelease(state.release ?? undefined);
        setWarningsAcknowledged(false);
        setCamApprovalConfirmed(false);
        setReleaseConfirmed(false);
        setError(undefined);
        setServerSynchronized(true);
        if (restoredRetention) onRequestServerPreviewRetry?.();
      } catch (caught) {
        if (cancelled) return;
        const message = `Serverstatus kunde inte återställas. ${errorMessage(caught)}`;
        setError(message);
        setActionFeedback({
          tone: "error",
          message: `${message} Stäng och öppna Underlag igen för att försöka hämta status på nytt.`,
        });
      } finally {
        if (!cancelled) setSynchronizing(false);
      }
    })();

    return () => { cancelled = true; };
  }, [
    active,
    api,
    loadedStorageKey,
    onRequestServerPreviewRetry,
    projectId,
    projectName,
    storageKey,
  ]);

  useEffect(() => {
    const previousSignature = retentionSpecSignatureRef.current;
    retentionSpecSignatureRef.current = retentionSpecSignature;
    if (
      previousSignature === undefined
      || previousSignature === retentionSpecSignature
      || !serverProjectId
      || !selectedRetentionEvidenceId
    ) return;
    api.setJointRetentionEvidence?.(serverProjectId, undefined);
    let cancelled = false;
    queueMicrotask(() => {
      if (cancelled) return;
      setSelectedRetentionEvidenceId(undefined);
      setSelectedGeneralEvidence({});
      setJob(undefined);
      setArtifacts([]);
      setDesignApproved(false);
      setDesignApproverId(undefined);
      setCamApprovedJobId(undefined);
      setCamApproverId(undefined);
      setRelease(undefined);
      setApprovedGeneralEvidenceIds([]);
      setApprovalEvidenceValid(true);
      setWarningsAcknowledged(false);
      setCamApprovalConfirmed(false);
      setReleaseConfirmed(false);
      setActionFeedback({
        tone: "busy",
        message: "Designen ändrades. Tidigare evidens har kopplats bort och måste väljas igen för den nya geometrin.",
      });
    });
    return () => { cancelled = true; };
  }, [api, retentionSpecSignature, selectedRetentionEvidenceId, serverProjectId]);

  useEffect(() => {
    if (!active || !serverSynchronized || !serverProjectId || !api.listExternalEvidence) return;
    let cancelled = false;
    const loadEvidence = api.listExternalEvidence.bind(api);
    void Promise.resolve()
      .then(() => {
        if (cancelled) return [] as ExternalEvidenceRead[];
        setEvidenceLoading(true);
        setEvidenceLoadError(undefined);
        return loadEvidence(serverProjectId);
      })
      .then((evidence) => {
        if (cancelled) return;
        setExternalEvidence(evidence);
      })
      .catch((caught: unknown) => {
        if (cancelled) return;
        setExternalEvidence([]);
        setEvidenceLoadError(
          `Serverevidensen kunde inte hämtas. ${errorMessage(caught)}`,
        );
      })
      .finally(() => {
        if (!cancelled) setEvidenceLoading(false);
      });
    return () => { cancelled = true; };
  }, [active, api, serverProjectId, serverSynchronized]);

  useEffect(() => {
    if (loadedStorageKey !== storageKey) return;
    writeProductionSession(window.sessionStorage, storageKey, {
      version,
      job,
      release,
      designApproved,
      releaseNumber: release?.release_number ?? `R${version?.revision ?? 1}`,
    });
  }, [designApproved, job, loadedStorageKey, release, storageKey, version]);

  useEffect(() => {
    if (
      !active
      || !serverSynchronized
      || busy === "generation"
      || !job
      || (job.status !== "queued" && job.status !== "running")
    ) return;
    let cancelled = false;
    let retryTimer: number | undefined;
    let consecutiveFailures = 0;
    const activeJobId = job.id;
    const controller = new AbortController();

    const clearCurrentPollingIssue = () => {
      setJobPollingIssue((current) => current?.jobId === activeJobId ? undefined : current);
    };
    const schedule = (delayMs: number) => {
      if (cancelled) return;
      retryTimer = window.setTimeout(() => { void poll(); }, delayMs);
    };
    const poll = async () => {
      try {
        const currentJob = await api.getJob(activeJobId, controller.signal);
        if (cancelled) return;
        if (currentJob.status === "succeeded") {
          const generatedArtifacts = await api.listArtifacts(currentJob.id, controller.signal);
          if (cancelled) return;
          if (!generatedArtifacts.some((artifact) => artifact.kind === "production_bundle")) {
            const message = "Jobbet lyckades men saknar verifierat granskningspaket.";
            setArtifacts(generatedArtifacts);
            setJob(currentJob);
            clearCurrentPollingIssue();
            setError(message);
            setActionFeedback({
              tone: "error",
              message: `${message} Välj Skapa om underlag och försök igen.`,
            });
            return;
          }
          setArtifacts(generatedArtifacts);
          // Publish the terminal job state only after its evidence is present.
          // Otherwise this effect is cleaned up on the job-state render and the
          // in-flight artifact response is discarded.
          setJob(currentJob);
          clearCurrentPollingIssue();
          setError(undefined);
          return;
        }
        setJob(currentJob);
        if (currentJob.status === "failed" || currentJob.status === "cancelled") {
          const message = errorMessage(new ApiError(
            currentJob.error ?? `Granskningsjobbet ${currentJob.status}.`,
          ));
          clearCurrentPollingIssue();
          setError(message);
          setActionFeedback({
            tone: "error",
            message: `${message} Kontrollera orsaken och välj Försök skapa underlag igen.`,
          });
          return;
        }
        consecutiveFailures = 0;
        clearCurrentPollingIssue();
        schedule(pollIntervalMs);
      } catch (caught) {
        if (cancelled) return;
        consecutiveFailures += 1;
        const retryBaseMs = Math.max(250, pollIntervalMs);
        const retryDelayMs = Math.min(
          30_000,
          retryBaseMs * (2 ** Math.min(consecutiveFailures - 1, 5)),
        );
        const message = errorMessage(caught);
        setJobPollingIssue({
          jobId: activeJobId,
          retryDelayMs,
          message: `Serverstatus kunde inte hämtas: ${message} Senast kända jobbstatus behålls och sidan försöker igen automatiskt om ${Math.ceil(retryDelayMs / 1_000)} sekunder.`,
        });
        schedule(retryDelayMs);
      }
    };

    schedule(jobPollRetryRequest > 0 ? 0 : pollIntervalMs);

    return () => {
      cancelled = true;
      controller.abort();
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
    };
  }, [active, api, busy, job, jobPollRetryRequest, pollIntervalMs, serverSynchronized]);

  const stale = Boolean(version && (
    version.design_hash !== design.design_hash
    || !versionProductionContextMatches(version, spec)
  ));
  const mayDesign = hasWorkflowCapability(principal, "design");
  const mayReview = hasWorkflowCapability(principal, "review");
  const mayGenerate = hasWorkflowCapability(principal, "generate");
  const frozenProductionContext = useMemo<RevisionProductionContextSnapshot | undefined>(() => {
    const result = version?.result_json;
    if (!result || typeof result !== "object" || Array.isArray(result)) return undefined;
    try {
      return parseRevisionProductionContext(result.production_context);
    } catch {
      return undefined;
    }
  }, [version]);
  const reviewerMaySelectEvidence = canSelectExternalEvidence(principal);
  const mayDownloadRetentionEvidence = canDownloadJointRetentionEvidence(principal);
  const restoredRetentionBinding = retentionBindingFromVersion(version);
  const boundRetentionEvidence = restoredRetentionBinding
    ? externalEvidence.find((evidence) => (
        evidence.id === restoredRetentionBinding.evidenceId
        && evidence.evidence_type === "joint_retention"
        && evidence.rule_id === RETENTION_RULE_ID
        && evidence.content_type === "application/json"
        && evidenceMetadataIsCurrent(
          evidence,
          version?.project_id ?? "",
          restoredRetentionBinding.baseDesignHash,
        )
      ))
    : undefined;
  const retentionDownloadBlockReason = !mayDownloadRetentionEvidence
    ? "Endast reviewer, operator, production, admin eller owner får hämta den signerade originalfilen."
    : !api.downloadJointRetentionEvidence
      ? "Den autentiserade evidenshämtningen är inte tillgänglig i den här klientanslutningen."
      : !restoredRetentionBinding
        ? "Den sparade revisionen har ingen serverbunden retentionsevidens att hämta."
        : !boundRetentionEvidence
          ? "Den bundna evidensposten är inte aktuell i serverregistret. Hämta om status innan filen används."
          : undefined;
  const selectedRetentionRow = externalEvidence.find(
    (evidence) => evidence.id === selectedRetentionEvidenceId,
  );
  const retentionBaseDesignHash = selectedRetentionRow?.design_hash
    ?? restoredRetentionBinding?.baseDesignHash
    ?? design.design_hash;
  const retentionEvidenceOptions = externalEvidence
    .filter((evidence) => (
      evidence.evidence_type === "joint_retention"
      && evidence.rule_id === RETENTION_RULE_ID
      && evidence.content_type === "application/json"
      && evidence.expires_at !== null
      && evidenceMetadataIsCurrent(evidence, serverProjectId ?? "", retentionBaseDesignHash)
    ))
    .sort((left, right) => right.created_at.localeCompare(left.created_at));
  const generalEvidenceOptions = version
    ? externalEvidence.filter((evidence) => (
        evidence.evidence_type !== "joint_retention"
        && GENERAL_EVIDENCE_TYPES.includes(evidence.evidence_type as GeneralEvidenceType)
        && evidenceMetadataIsCurrent(evidence, version.project_id, version.design_hash)
      ))
    : [];
  const selectedGeneralEvidenceIds = GENERAL_EVIDENCE_TYPES
    .map((evidenceType) => selectedGeneralEvidence[evidenceType])
    .filter((evidenceId): evidenceId is string => (
      typeof evidenceId === "string"
      && generalEvidenceOptions.some((evidence) => evidence.id === evidenceId)
    ));
  const generationEvidenceIds = approvedGeneralEvidenceIds.filter((evidenceId) => (
    generalEvidenceOptions.some((evidence) => evidence.id === evidenceId)
  ));
  const approvedEvidenceCurrent = approvalEvidenceValid
    && generationEvidenceIds.length === approvedGeneralEvidenceIds.length;
  const approvalWarningRuleIds = serverApprovalWarningRuleIds(design.rule_evaluations);
  const acknowledgementWarningRuleIds = [...new Set(
    design.rule_evaluations
      .filter((evaluation) => evaluation.status === "WARNING")
      .map((evaluation) => evaluation.rule_id),
  )].sort((left, right) => left.localeCompare(right));
  const blockingEvaluations = design.rule_evaluations
    .filter((evaluation) => evaluation.status === "BLOCK");
  const stocklessReviewExpected = permitsStocklessDesignReview(design.rule_evaluations);
  const hardBlockingEvaluations = stocklessReviewExpected ? [] : blockingEvaluations;
  const partCustomizationBlocked = hasPartCustomization(spec);
  const partCustomizationBlockReason = partCustomizationBlocked
    ? "Fria deländringar ingår bara i arbetsytans konceptmodell och är inte serverauktoritativa. Återställ deländringarna eller bygg samma ändring med de parametriska möbelvalen innan du sparar en revision eller skapar CNC-verkstadsunderlag."
    : undefined;
  const current = Boolean(version && !stale);
  const stockProfileBlockedFailure = Boolean(
    job?.status === "failed" && productionErrorHasCode(job.error, STOCK_PROFILE_MISSING_CODE),
  );
  const productionBundle = artifacts.find((artifact) => artifact.kind === "production_bundle");
  const bundleSha256 = reviewBundleSha256FromJob(job);
  const reviewPackageStatus = designReviewPackageStatusFromJob(job);
  const reviewPackageStatusClaimed = Boolean(
    job?.result_json
    && typeof job.result_json === "object"
    && !Array.isArray(job.result_json)
    && Object.prototype.hasOwnProperty.call(
      job.result_json,
      "design_review_package_status",
    ),
  );
  const camBlocked = reviewPackageStatus?.cam_status === "BLOCKED";
  const expectedReviewArtifactKinds = reviewArtifactKindsFromJob(job);
  const requiredArtifactsComplete = reviewPackageArtifactInventoryIsTruthful(
    artifacts,
    reviewPackageStatus,
    reviewPackageStatusClaimed,
    expectedReviewArtifactKinds,
  ) && reviewBundleArtifactMatchesJob(artifacts, bundleSha256);
  const workshopReadiness = workshopReadinessFromJob(job);
  const softwareStatusByCode = new Map(
    workshopReadiness?.software_evidence.map((item) => [item.code, item.status]) ?? [],
  );
  const workshopStatusByCode = new Map(
    workshopReadiness?.workshop_evidence.map((item) => [item.code, item.status]) ?? [],
  );
  const blockedCamCode = reviewPackageStatus?.blocker_codes.length === 1
    ? reviewPackageStatus.blocker_codes[0]
    : undefined;
  const stockBlocked = blockedCamCode === STOCK_PROFILE_MISSING_CODE;
  const grainBlocked = blockedCamCode === DFM_GRAIN_MISSING_CODE;
  const registrationBlocked = blockedCamCode === TWO_SIDED_REGISTRATION_MISSING_CODE;
  const dadoRetentionBlocked = blockedCamCode === DADO_RETENTION_EVIDENCE_MISSING_CODE;
  const backPanelRetentionBlocked = (
    blockedCamCode === BACK_PANEL_RETENTION_EVIDENCE_MISSING_CODE
  );
  const retentionBlocked = dadoRetentionBlocked || backPanelRetentionBlocked;
  const dfmBlocked = stockBlocked || grainBlocked;
  const jobDfmStatus = job?.result_json?.dfm_status;
  const blockedCamReviewIsTruthful = Boolean(
    camBlocked
    && (dfmBlocked || registrationBlocked || retentionBlocked)
    && job?.result_json?.authoritative_geometry === true
    && (dfmBlocked ? jobDfmStatus === "BLOCK" : jobDfmStatus === "PASS" || jobDfmStatus === "WARNING")
    && job?.result_json?.nesting_utilization_ppm === null
    && job.result_json.used_sheet_count === 0
    && Array.isArray(job.result_json.nesting_layouts)
    && job.result_json.nesting_layouts.length === 0
    && workshopReadiness?.design_review_ready === false
    && softwareStatusByCode.get("AUTHORITATIVE_CAD") === "VERIFIED"
    && softwareStatusByCode.get("DFM_SCREEN") === (dfmBlocked ? "MISSING" : "VERIFIED")
    && (!grainBlocked || workshopStatusByCode.get("MATERIAL_GRAIN") === "EXTERNAL_EVIDENCE_REQUIRED")
    && softwareStatusByCode.get("SEMANTIC_OPERATIONS") === "MISSING"
    && softwareStatusByCode.get("SETUP_SHEETS") === "MISSING"
    && softwareStatusByCode.get("VALIDATION_BACKPLOT") === "MISSING"
    && softwareStatusByCode.get("NON_CUTTING_PROGRAM") === "MISSING",
  );
  const missingWorkshopRequirements = workshopReadiness?.workshop_evidence
    .filter((item) => item.status !== "VERIFIED") ?? [];
  const groupedMissingWorkshopRequirements = WORKSHOP_REQUIREMENT_GROUPS
    .map((group) => ({
      ...group,
      requirements: missingWorkshopRequirements
        .map((requirement) => ({
          requirement,
          presentation: workshopRequirementPresentation(requirement.code),
        }))
        .filter((item) => item.presentation.group === group.id),
    }))
    .filter((group) => group.requirements.length > 0);
  const manifestSha256 = typeof job?.result_json?.manifest_sha256 === "string"
    && /^[a-f0-9]{64}$/.test(job.result_json.manifest_sha256)
    ? job.result_json.manifest_sha256
    : undefined;
  const artifactInventory = artifacts
    .map((artifact) => ({
      artifact,
      label: artifactRoleLabel(artifact.kind),
      format: artifactFormatLabel(artifact),
      reviewUse: artifactReviewUseLabel(artifact.kind),
    }))
    .sort((left, right) => (
      left.label.localeCompare(right.label, "sv")
      || left.artifact.id.localeCompare(right.artifact.id)
    ));
  const completedArtifactSet = Boolean(
    job?.status === "succeeded" && requiredArtifactsComplete && productionBundle,
  );
  const designApprovalBlockReason = !mayReview
    ? "Endast reviewer, admin eller owner får godkänna designkontrollen."
    : workshopContextBlockReason
      ? workshopContextBlockReason
    : partCustomizationBlockReason
    ?? (hardBlockingEvaluations.length > 0
    ? `${hardBlockingEvaluations.length} blockerande krav måste åtgärdas i ritningen och kontrolleras igen innan underlaget kan skapas.`
    : acknowledgementWarningRuleIds.length > 0 && !warningsAcknowledged
      ? "Bekräfta att du har läst och kontrollerat varningarna för att fortsätta."
      : undefined);
  const designReviewReady = Boolean(
    completedArtifactSet
    && workshopReadiness
    && (camBlocked ? blockedCamReviewIsTruthful : workshopReadiness.design_review_ready),
  );
  const camValidationPackageEligible = Boolean(
    designReviewReady
    && !camBlocked
    && job?.status === "succeeded"
    && job.result_json?.authoritative_geometry === true
    && job.result_json.machine_program_mode === "VALIDATION_DRY_RUN"
    && job.result_json.production_machine_program === false
    && reviewPackageStatus?.cam_status === "VALIDATION_GENERATED"
    && workshopReadiness?.physical_cutting_authorized === false
    && bundleSha256
    && manifestSha256,
  );
  const camApprovalCurrent = Boolean(
    camValidationPackageEligible
    && job
    && camApprovedJobId === job.id
    && designApproverId
    && camApproverId
    && designApproverId !== camApproverId,
  );
  const immutableReviewReleased = Boolean(
    release
    && camApprovalCurrent
    && release.status === "released"
    && release.release_kind === "design_review"
    && release.machine_use === "validation_only"
    && release.bundle_sha256 === bundleSha256
    && release.manifest_sha256 === manifestSha256
    && release.physical_cutting_authorized === false
    && version?.status === "released"
    && version.immutable,
  );
  const camApprovalBlockReason = immutableReviewReleased
    ? undefined
    : !mayReview
      ? "Endast reviewer, admin eller owner får godkänna CAM-valideringspaketet."
      : !designApproverId
        ? "Serverns designgranskare kunde inte identifieras. Hämta om projektstatus innan CAM godkänns."
        : principal?.user_id === designApproverId
          ? "Maker–checker kräver att en annan person än designgranskaren godkänner CAM-valideringspaketet."
          : !camValidationPackageEligible
            ? "Paketet saknar en komplett serververifierad bindning till auktoritativ geometri, manifest eller icke-skärande valideringsprogram."
            : !camApprovalConfirmed
              ? "Bekräfta att exakt paket och manifest har granskats och att programmet inte är skärande CNC-kod."
              : undefined;
  const releaseBlockReason = immutableReviewReleased
    ? undefined
    : !mayReview
      ? "Endast reviewer, admin eller owner får låsa en designgranskningsrevision."
      : !camApprovalCurrent
        ? "En aktuell CAM-granskning bunden till exakt detta jobb och manifest krävs före revisionslåset."
        : !releaseConfirmed
          ? "Bekräfta att låset endast gäller designgranskning och aldrig auktoriserar fysisk kapning."
          : undefined;
  const missingSoftwareEvidenceCount = workshopReadiness?.software_evidence
    .filter((item) => item.status !== "VERIFIED").length ?? 0;
  const completedPackageIssue = !completedArtifactSet
    ? "Granskningspaketet blev inte komplett. Skapa om det och försök igen."
    : !workshopReadiness
      ? "Paketets verifierbara readinessbevis saknas eller är ogiltigt. Skapa om granskningspaketet innan det hämtas."
      : !workshopReadiness.design_review_ready && !blockedCamReviewIsTruthful
        ? `Serverns designgranskning är inte klar. ${missingSoftwareEvidenceCount} ${missingSoftwareEvidenceCount === 1 ? "programvarukrav återstår" : "programvarukrav återstår"}.`
        : undefined;
  const verificationStatusLabel = stocklessReviewExpected
    ? "Lagerprofil saknas · CAM blockeras"
    : design.status === "BLOCK"
      ? "Måste lösas"
    : design.status === "WARNING"
      ? "Behöver beslut"
      : "Designkontroll klar";
  const activeProductionStep = busy === "save" || !current || version?.status === "draft"
    ? 1
    : designReviewReady
      ? 3
      : 2;
  const saveBlockReason = busy
    ? "En serveråtgärd pågår. Vänta tills den är klar."
    : !mayDesign
      ? "Endast designer, admin eller owner får ändra, spara och kontrollera designrevisioner."
    : workshopContextBlockReason
      ? workshopContextBlockReason
    : partCustomizationBlockReason
      ? partCustomizationBlockReason
    : !serverSynchronized
      ? "Hämtar projektet från servern."
      : design.source !== "server-preview"
        ? "Inväntar serverns auktoritativa konstruktionsmodell för de senaste ändringarna."
        : version?.immutable && !stale
          ? "Den sparade modellen kan inte ändras. Gör en ändring i modellen och spara igen."
          : undefined;
  const generationBlockReason = !mayGenerate
    ? "Designen är godkänd. En designer, admin eller owner måste nu skapa underlaget."
    : workshopContextBlockReason
      ? workshopContextBlockReason
    : immutableReviewReleased || version?.immutable
      ? "Revisionen är immutable och kan inte genereras om. Skapa en ny designrevision för nästa paket."
    : !approvalEvidenceValid
      ? "Godkännandets evidenslista är ogiltig och kan inte användas för generation."
      : !approvedEvidenceCurrent
        ? "Godkänd serverevidens kan inte längre verifieras mot den aktuella revisionen."
        : partCustomizationBlockReason;

  useEffect(() => {
    if (!stepFocusInitializedRef.current) {
      stepFocusInitializedRef.current = true;
      previousActiveStepRef.current = activeProductionStep;
      return;
    }
    if (previousActiveStepRef.current !== activeProductionStep) {
      nextActionHeadingRef.current?.focus({ preventScroll: true });
    }
    previousActiveStepRef.current = activeProductionStep;
  }, [activeProductionStep]);

  useEffect(() => {
    onSummaryChange({
      revision: version?.revision,
      status: synchronizing ? "syncing" : stale ? "stale" : (version?.status ?? "unsaved"),
      stale,
      designReviewReady,
      physicalCuttingAuthorized: workshopReadiness?.physical_cutting_authorized,
    });
  }, [
    designReviewReady,
    onSummaryChange,
    stale,
    synchronizing,
    version?.revision,
    version?.status,
    workshopReadiness?.physical_cutting_authorized,
  ]);

  async function perform(
    action: BusyAction,
    pendingMessage: string,
    operation: () => Promise<string>,
    failureMessage?: (error: unknown) => string,
  ) {
    setBusy(action);
    setError(undefined);
    setActionFeedback({ tone: "busy", message: pendingMessage });
    try {
      const successMessage = await operation();
      setActionFeedback({ tone: "success", message: successMessage });
    } catch (caught) {
      setError(errorMessage(caught));
      setActionFeedback({
        tone: "error",
        message: failureMessage?.(caught) ?? actionFailureMessage(action, caught),
      });
    } finally {
      setBusy(undefined);
    }
  }

  function resetDownstream() {
    setJob(undefined);
    setArtifacts([]);
    setDesignApproved(false);
    setDesignApproverId(undefined);
    setCamApprovedJobId(undefined);
    setCamApproverId(undefined);
    setRelease(undefined);
    setApprovedGeneralEvidenceIds([]);
    setApprovalEvidenceValid(true);
    setWarningsAcknowledged(false);
    setCamApprovalConfirmed(false);
    setReleaseConfirmed(false);
  }

  function updateWorkshopContext(
    context: WorkshopProductionContext | undefined,
    legacyPatch: Partial<Pick<
      DesignSpec,
      | "stock_width_mm"
      | "stock_height_mm"
      | "stock_count"
      | "back_stock_width_mm"
      | "back_stock_height_mm"
      | "back_stock_count"
      | "machine_profile_id"
    >>,
  ) {
    if (!mayDesign || !onApplyDesignChange) {
      setActionFeedback({
        tone: "error",
        message: "Endast en behörig designer kan ändra den revisionsbundna verkstadsprofilen.",
      });
      return;
    }
    resetDownstream();
    onApplyDesignChange(
      { ...legacyPatch, workshop_context: context },
      context
        ? "Verkstadsprofilen ändrades. En ny designrevision krävs före nästa generering."
        : legacyPatch.machine_profile_id
          ? "Maskinens versionslåsta valideringsprofil ändrades. En ny designrevision krävs före nästa generering."
        : "Verkstadsprofilen togs bort. En ny lagerobunden designrevision krävs.",
    );
  }

  function selectRetentionEvidence(evidenceId: string) {
    if (!mayDesign || !serverProjectId || !api.setJointRetentionEvidence) {
      setActionFeedback({
        tone: "error",
        message: "Endast designer, admin eller owner kan binda retention till nästa designrevision.",
      });
      return;
    }
    const normalizedEvidenceId = evidenceId || undefined;
    if (
      normalizedEvidenceId
      && !retentionEvidenceOptions.some((evidence) => evidence.id === normalizedEvidenceId)
    ) {
      setActionFeedback({
        tone: "error",
        message: "Den valda retentionsevidensen är inte aktuell för exakt denna design.",
      });
      return;
    }
    api.setJointRetentionEvidence(serverProjectId, normalizedEvidenceId);
    setSelectedRetentionEvidenceId(normalizedEvidenceId);
    setSelectedGeneralEvidence({});
    resetDownstream();
    setActionFeedback({
      tone: "busy",
      message: normalizedEvidenceId
        ? "Retentionsevidensen serververifieras mot exakt geometri och versionsbundna regler…"
        : "Retentionsevidensen har kopplats bort. En obunden serverpreview hämtas…",
    });
    onRequestServerPreviewRetry?.();
  }

  function uploadRetentionEvidence(file: File | undefined) {
    if (!file) return;
    if (
      !reviewerMaySelectEvidence
      || !serverProjectId
      || !api.uploadExternalEvidence
      || design.source !== "server-preview"
      || !/^[a-f0-9]{64}$/.test(design.design_hash)
    ) {
      setActionFeedback({
        tone: "error",
        message: "Uppladdning kräver reviewer-, admin- eller ownerbehörighet, ett serverprojekt och den aktuella serverberäknade designhashen.",
      });
      return;
    }

    void perform(
      "evidence-upload",
      "Den certifierarsignerade retentionsevidensen laddas upp till serverregistret…",
      async () => {
        const metadata = await retentionEvidenceUploadMetadata(file);
        const uploaded = await api.uploadExternalEvidence!(serverProjectId, {
          document: file,
          evidenceType: "joint_retention",
          ruleId: RETENTION_RULE_ID,
          catalogId: metadata.catalogId,
          catalogVersion: metadata.catalogVersion,
          designHash: design.design_hash,
          expiresAt: metadata.expiresAt,
        });
        setExternalEvidence((current) => [
          uploaded,
          ...current.filter((evidence) => evidence.id !== uploaded.id),
        ]);
        setEvidenceLoadError(undefined);
        return "Filen finns nu i serverregistret. Uppladdningen godkände eller band den inte; en designer måste välja posten för nästa revision, där servern verifierar hela trustkedjan.";
      },
    );
  }

  function selectGeneralEvidence(evidenceType: GeneralEvidenceType, evidenceId: string) {
    if (!reviewerMaySelectEvidence) {
      setActionFeedback({
        tone: "error",
        message: "Endast en behörig granskare kan välja kompletterande serverevidens.",
      });
      return;
    }
    if (
      evidenceId
      && !generalEvidenceOptions.some((evidence) => (
        evidence.id === evidenceId && evidence.evidence_type === evidenceType
      ))
    ) {
      setActionFeedback({
        tone: "error",
        message: "Det kompletterande beviset är inte aktuellt för den sparade revisionen.",
      });
      return;
    }
    setSelectedGeneralEvidence((currentSelection) => ({
      ...currentSelection,
      [evidenceType]: evidenceId || undefined,
    }));
    resetDownstream();
    setActionFeedback({
      tone: "success",
      message: evidenceId
        ? "Kompletterande evidens har valts och kontrolleras åter av servern vid generation."
        : "Det kompletterande beviset har kopplats bort.",
    });
  }

  function saveRevision() {
    if (!mayDesign || workshopContextBlocked) {
      setError(undefined);
      setActionFeedback({ tone: "error", message: saveBlockReason ?? "Designrevisionen kan inte sparas." });
      return;
    }
    if (partCustomizationBlockReason) {
      setError(undefined);
      setActionFeedback({ tone: "error", message: partCustomizationBlockReason });
      return;
    }
    if (design.source !== "server-preview") {
      setError(undefined);
      setActionFeedback({
        tone: "error",
        message: "Modellen är ännu inte synkroniserad med servern. Vänta tills serverpreviewn är klar och välj sedan Spara och kontrollera igen.",
      });
      return;
    }
    let savedRevision: DesignVersionRead | undefined;
    void perform("save", "Sparar och kontrollerar modellen…", async () => {
      if (!api.configured) throw new ApiError("Underlags-API:t är inte konfigurerat.");
      const resolvedProjectId = projectId ?? (await api.ensureProject(projectName)).id;
      let saved: DesignVersionRead;
      try {
        const createVersionArguments = [
          resolvedProjectId,
          spec,
          design.design_hash,
          version?.revision ?? 0,
          templateId,
        ] as const;
        saved = selectedRetentionEvidenceId
          ? await api.createVersion(...createVersionArguments, selectedRetentionEvidenceId)
          : await api.createVersion(...createVersionArguments);
      } catch (caught) {
        if (
          caught instanceof ApiError
          && caught.status === 409
          && caught.message.includes("EXPECTED_DESIGN_HASH_MISMATCH")
        ) {
          onRequestServerPreviewRetry?.();
        } else if (
          caught instanceof ApiError
          && caught.status === 409
          && caught.message.includes("EXPECTED_CURRENT_REVISION_MISMATCH")
        ) {
          // Never retry a write automatically. Refresh only the optimistic
          // revision guard and discard all evidence derived from the stale revision.
          resetDownstream();
          const state = await api.getProductionState(resolvedProjectId);
          setVersion(state.version ?? undefined);
        }
        throw caught;
      }
      savedRevision = saved;
      resetDownstream();
      setSelectedGeneralEvidence({});
      setVersion(saved);
      if (design.status !== "BLOCK" || stocklessReviewExpected) {
        const validated = await api.validateVersion(saved.project_id, saved.revision);
        setVersion(validated);
        return stocklessReviewExpected
          ? "Designen sparades och kontrollerades. Ett lagerobundet granskningspaket kan skapas, men nesting och CAM förblir blockerade."
          : "Designen sparades och kontrollen är klar. Fortsätt med Skapa underlag.";
      }
      return "Utkastet sparades. Lös de blockerande kraven och spara sedan modellen igen.";
    }, (caught) => {
      if (savedRevision) {
        return `${errorMessage(caught)} Modellen är redan sparad. Välj Kontrollera igen.`;
      }
      if (
        caught instanceof ApiError
        && caught.status === 409
        && caught.message.includes("EXPECTED_DESIGN_HASH_MISMATCH")
      ) {
        return "Modellen hann ändras. Serverpreviewn hämtas om; granska den och välj sedan Spara och kontrollera igen.";
      }
      if (
        caught instanceof ApiError
        && caught.status === 409
        && caught.message.includes("EXPECTED_CURRENT_REVISION_MISMATCH")
      ) {
        return "Modellen har uppdaterats på ett annat ställe. Den senaste serverrevisionen har hämtats; granska läget och välj sedan Spara och kontrollera igen.";
      }
      return actionFailureMessage("save", caught);
    });
  }

  function validateRevision() {
    if (!mayDesign) {
      setError(undefined);
      setActionFeedback({
        tone: "error",
        message: "Endast designer, admin eller owner får kontrollera en designrevision.",
      });
      return;
    }
    if (workshopContextBlockReason) {
      setError(undefined);
      setActionFeedback({ tone: "error", message: workshopContextBlockReason });
      return;
    }
    if (partCustomizationBlockReason) {
      setError(undefined);
      setActionFeedback({ tone: "error", message: partCustomizationBlockReason });
      return;
    }
    void perform("validate", "Kontrollerar den sparade modellen…", async () => {
      if (!version) throw new ApiError("Ingen sparad modell finns att kontrollera.");
      const validated = await api.validateVersion(version.project_id, version.revision);
      setVersion(validated);
      return "Kontrollen är klar. Fortsätt med Skapa underlag.";
    });
  }

  function approveDesign() {
    if (!mayReview) {
      setError(undefined);
      setActionFeedback({
        tone: "error",
        message: designApprovalBlockReason ?? "Din roll får inte godkänna designkontrollen.",
      });
      return;
    }
    if (workshopContextBlockReason) {
      setError(undefined);
      setActionFeedback({ tone: "error", message: workshopContextBlockReason });
      return;
    }
    if (partCustomizationBlockReason) {
      setError(undefined);
      setActionFeedback({ tone: "error", message: partCustomizationBlockReason });
      return;
    }
    void perform("design-approval", "Godkänner designkontrollen…", async () => {
      if (!version) throw new ApiError("Ingen kontrollerad modell finns att godkänna.");
      if (hardBlockingEvaluations.length > 0) {
        throw new ApiError("Blockerande konstruktionskrav måste åtgärdas och kontrolleras igen innan underlaget kan skapas.");
      }
      if (acknowledgementWarningRuleIds.length > 0 && !warningsAcknowledged) {
        throw new ApiError("Bekräfta att du har läst och kontrollerat varningarna innan underlaget skapas.");
      }
      const approved = await api.approveVersion(version.project_id, version.revision, {
        approval_type: "design",
        reason: stocklessReviewExpected
          ? "Designkontroll godkänd för ett lagerobundet granskningspaket. Lagerprofil, nesting och CAM är uttryckligen inte godkända."
          : acknowledgementWarningRuleIds.length > 0
          ? `Designkontroll godkänd efter granskning av varningar: ${acknowledgementWarningRuleIds.join(", ")}.`
          : "Designkontroll godkänd. Inga varningar krävde verifiering.",
        generation_job_id: null,
        warning_overrides: approvalWarningRuleIds.map((ruleId) => ({
          rule_id: ruleId,
          reason: "Varningen har granskats och godkänts i designkontrollen.",
          evidence_ids: generalEvidenceOptions
            .filter((evidence) => (
              evidence.rule_id === ruleId
              && selectedGeneralEvidenceIds.includes(evidence.id)
            ))
            .map((evidence) => evidence.id),
        })),
      });
      setVersion(approved);
      setDesignApproved(true);
      setDesignApproverId(principal?.user_id);
      setCamApprovedJobId(undefined);
      setCamApproverId(undefined);
      setRelease(undefined);
      setCamApprovalConfirmed(false);
      setReleaseConfirmed(false);
      setApprovedGeneralEvidenceIds(selectedGeneralEvidenceIds);
      setApprovalEvidenceValid(true);
      return "Designkontrollen är godkänd. En behörig designer kan nu skapa underlaget utan att ändra revisionen.";
    });
  }

  function generatePackage() {
    if (generationBlockReason) {
      setError(undefined);
      setActionFeedback({ tone: "error", message: generationBlockReason });
      return;
    }
    if (partCustomizationBlockReason) {
      setError(undefined);
      setActionFeedback({ tone: "error", message: partCustomizationBlockReason });
      return;
    }
    void perform("generation", "Skapar ett nytt underlag…", async () => {
      if (!version) throw new ApiError("Ingen kontrollerad modell finns att skapa underlag för.");
      const queued = await api.generateVersion(version.project_id, version.revision, {
        ...productionContextFromSpec(spec),
        machine_profile_id: generationMachineProfileId(spec.machine_profile_id),
        postprocessor_id: "linuxcnc-validation-1.1.0",
        include_step: true,
        include_freecad_project: false,
        include_validation_program: true,
        external_evidence_ids: generationEvidenceIds,
      });
      setJob(queued);
      setArtifacts([]);
      setCamApprovedJobId(undefined);
      setCamApproverId(undefined);
      setRelease(undefined);
      setCamApprovalConfirmed(false);
      setReleaseConfirmed(false);
      return "Ett nytt underlag skapas nu.";
    });
  }

  function approveCamPackage() {
    if (camApprovalBlockReason) {
      setError(undefined);
      setActionFeedback({ tone: "error", message: camApprovalBlockReason });
      return;
    }
    void perform("cam-approval", "Binder CAM-granskningen till exakt jobb och manifest…", async () => {
      if (!version || !job || job.status !== "succeeded") {
        throw new ApiError("Ett slutfört serverjobb krävs för CAM-granskningen.");
      }
      if (!camValidationPackageEligible || !manifestSha256) {
        throw new ApiError("CAM-valideringspaketet är inte komplett eller serververifierbart.");
      }
      if (!principal?.user_id || !designApproverId || principal.user_id === designApproverId) {
        throw new ApiError("Maker–checker kräver en annan CAM-granskare än designgranskaren.");
      }
      if (!camApprovalConfirmed) {
        throw new ApiError("Bekräfta den icke-skärande CAM-granskningen innan du fortsätter.");
      }
      const approved = await api.approveVersion(version.project_id, version.revision, {
        approval_type: "cam",
        reason: "Exakt genererat CAM-valideringspaket och manifest granskat. Godkännandet gäller endast icke-skärande validering och auktoriserar inte fysisk kapning.",
        generation_job_id: job.id,
        warning_overrides: [],
      });
      if (approved.status !== "approved" || approved.immutable) {
        throw new ApiError("Servern bekräftade inte en aktuell, ännu olåst CAM-granskning.");
      }
      setVersion(approved);
      setCamApprovedJobId(job.id);
      setCamApproverId(principal.user_id);
      setCamApprovalConfirmed(false);
      setRelease(undefined);
      setReleaseConfirmed(false);
      return "CAM-valideringspaketet är godkänt och bundet till detta jobb. Nästa steg låser endast designgranskningen.";
    });
  }

  function releaseDesignReview() {
    if (releaseBlockReason) {
      setError(undefined);
      setActionFeedback({ tone: "error", message: releaseBlockReason });
      return;
    }
    void perform("release", "Verifierar och låser designgranskningsrevisionen…", async () => {
      if (!version || !job || !bundleSha256 || !manifestSha256 || !camApprovalCurrent) {
        throw new ApiError("Aktuell CAM-granskning samt ZIP- och manifestbindning krävs före revisionslåset.");
      }
      if (!releaseConfirmed) {
        throw new ApiError("Bekräfta designgranskningens begränsade användning innan du låser revisionen.");
      }
      const releaseNumber = `R${version.revision}`;
      const released = await api.releaseVersion(
        version.project_id,
        version.revision,
        releaseNumber,
      );
      if (
        released.status !== "released"
        || released.release_kind !== "design_review"
        || released.machine_use !== "validation_only"
        || released.bundle_sha256 !== bundleSha256
        || released.manifest_sha256 !== manifestSha256
        || released.physical_cutting_authorized !== false
        || released.release_number !== releaseNumber
      ) {
        throw new ApiError(
          "Serverns revisionsbevis matchar inte exakt designgranskningspaket eller valideringsgräns.",
        );
      }
      setRelease(released);
      setVersion((currentVersion) => currentVersion
        ? { ...currentVersion, status: "released", immutable: true }
        : currentVersion);
      setReleaseConfirmed(false);
      return `Designgranskningsrevision ${released.release_number} är immutable och verifierbart bunden till manifestet. Den auktoriserar inte fysisk kapning.`;
    });
  }

  function startVerifiedBrowserDownload(blob: Blob, fileName: string) {
    const objectUrl = URL.createObjectURL(blob);
    pendingDownloadUrlsRef.current.set(objectUrl, undefined);
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = fileName;
    link.rel = "noopener noreferrer";
    try {
      document.body.append(link);
      link.click();
    } finally {
      link.remove();
      if (pendingDownloadUrlsRef.current.has(objectUrl)) {
        const timer = window.setTimeout(() => {
          if (!pendingDownloadUrlsRef.current.delete(objectUrl)) return;
          URL.revokeObjectURL(objectUrl);
        }, 250);
        pendingDownloadUrlsRef.current.set(objectUrl, timer);
      }
    }
  }

  function downloadRetentionCertificationRequest() {
    const request = design.retention_certification_request;
    if (
      design.source !== "server-preview"
      || !request
    ) {
      setActionFeedback({
        tone: "error",
        message: "Ingen aktuell serverutfärdad certifieringsbegäran finns för designen.",
      });
      return;
    }
    try {
      const blob = new Blob(
        [canonicalRetentionCertificationRequestJson(request)],
        { type: "application/json" },
      );
      startVerifiedBrowserDownload(
        blob,
        `custombuild-retention-certification-request-${request.source_design_hash}.json`,
      );
      setActionFeedback({
        tone: "success",
        message: "Serverns certifieringsbegäran har hämtats. Den är underlag till en extern certifierare, inte retentionsevidens eller ett godkännande.",
      });
    } catch (caught) {
      setActionFeedback({ tone: "error", message: errorMessage(caught) });
    }
  }

  function downloadBoundRetentionEvidence() {
    if (retentionDownloadBlockReason) {
      setError(undefined);
      setActionFeedback({ tone: "error", message: retentionDownloadBlockReason });
      return;
    }
    void perform("evidence-download", "Verifierar signerade originalbytes mot serverregistret…", async () => {
      if (
        !version
        || !boundRetentionEvidence
        || !api.downloadJointRetentionEvidence
      ) {
        throw new ApiError("Ingen aktuell serverbunden retentionsevidens finns att hämta.");
      }
      const blob = await api.downloadJointRetentionEvidence(
        version.project_id,
        boundRetentionEvidence,
      );
      startVerifiedBrowserDownload(
        blob,
        `custombuild-joint-retention-${boundRetentionEvidence.id}.json`,
      );
      return "Den certifierarsignerade originalfilen är hämtad och verifierad byte för byte. Hämtningen ändrar inte godkännande eller fysisk frisläppning.";
    });
  }

  async function currentVerifiedArtifacts(): Promise<ArtifactRead[]> {
    if (!job || !version || !designReviewReady) {
      throw new ApiError("Det finns inget verifierat granskningspaket att hämta.");
    }
    const currentArtifacts = await api.listArtifacts(job.id);
    setArtifacts(currentArtifacts);
    if (!reviewPackageArtifactInventoryIsTruthful(
      currentArtifacts,
      reviewPackageStatus,
      reviewPackageStatusClaimed,
      expectedReviewArtifactKinds,
    ) || !reviewBundleArtifactMatchesJob(currentArtifacts, bundleSha256)) {
      throw new ApiError(
        "Granskningspaketets aktuella artefaktlista är inte längre verifierbar. Skapa om paketet.",
      );
    }
    return currentArtifacts;
  }

  function downloadPackage() {
    void perform("download", "Förbereder hämtningen…", async () => {
      if (!version) throw new ApiError("Det finns ingen aktuell revision att hämta.");
      const currentArtifacts = await currentVerifiedArtifacts();
      const artifact = currentArtifacts.find((candidate) => candidate.kind === "production_bundle");
      if (!artifact) throw new ApiError("Granskningspaketet saknas eller är inte längre tillgängligt.");
      const verifiedArtifact = await api.downloadArtifact(artifact);
      startVerifiedBrowserDownload(
        verifiedArtifact,
        artifactDownloadFileName(artifact, version.project_id, version.revision),
      );
      return "Granskningspaketet har hämtats.";
    });
  }

  function downloadIndividualArtifact(selectedArtifact: ArtifactRead) {
    setDownloadingArtifactId(selectedArtifact.id);
    void perform(
      "download",
      `Verifierar ${artifactRoleLabel(selectedArtifact.kind)} före hämtning…`,
      async () => {
        if (!version) throw new ApiError("Det finns ingen aktuell revision att hämta.");
        const currentArtifacts = await currentVerifiedArtifacts();
        const matches = currentArtifacts.filter((candidate) => candidate.id === selectedArtifact.id);
        if (matches.length !== 1 || !sameArtifactIdentity(matches[0]!, selectedArtifact)) {
          throw new ApiError(
            "Filen har ändrats eller är inte längre entydigt bunden till paketet. Skapa om granskningspaketet.",
          );
        }
        const currentArtifact = matches[0]!;
        const verifiedArtifact = await api.downloadArtifact(currentArtifact);
        startVerifiedBrowserDownload(
          verifiedArtifact,
          artifactDownloadFileName(currentArtifact, version.project_id, version.revision),
        );
        return `${artifactRoleLabel(currentArtifact.kind)} har hämtats för designgranskning.`;
      },
    ).finally(() => {
      setDownloadingArtifactId((current) => (
        current === selectedArtifact.id ? undefined : current
      ));
    });
  }

  if (!api.configured) {
    return (
      <>
        <div className="production-empty" role="status">
          Underlag är inte tillgängligt i lokalt previewläge. Anslut projektet till servern för att
          skapa och hämta filer.
        </div>
        {showRevisionHistory ? (
          <RevisionHistory
            active={active}
            api={api}
            projectId={projectId}
            localDesignHash={design.design_hash}
          />
        ) : null}
      </>
    );
  }

  return (
    <div className="production-workflow" id="production-review-package">
      <section className="production-steps" aria-label="Status för verifieringen">
        <div className={`status-badge status-${design.status.toLowerCase()}`}>
          {design.status === "PASS"
            ? <Check aria-hidden="true" size={14} />
            : <ShieldAlert aria-hidden="true" size={14} />}
          <span>{verificationStatusLabel}</span>
        </div>
        <p>
          {stocklessReviewExpected
            ? "Lager- och maskinprofilen blockerar nesting och CAM. Designen kan ändå gå vidare till ett lagerobundet granskningspaket utan att mått eller profiler ändras."
            : design.status === "BLOCK"
            ? "Ändra designen innan underlaget kan skapas."
            : design.status === "WARNING"
              ? "Läs varningarna och ta ställning innan du fortsätter."
              : "Designen kan gå vidare till ett granskningspaket. Det är inte ett tillstånd för fysisk tillverkning."}
        </p>
      </section>

      {showRevisionHistory ? (
        <RevisionHistory
          active={active}
          api={api}
          projectId={projectId}
          localDesignHash={design.design_hash}
          currentRevision={version?.revision}
          revisionRefreshKey={`${version?.id ?? "none"}:${version?.status ?? "none"}:${version?.immutable ? "1" : "0"}`}
        />
      ) : null}

      {stale ? (
        <p className="production-warning" role="alert">
          Modellen har ändrats. Spara och kontrollera den igen innan du skapar ett nytt underlag.
        </p>
      ) : null}
      {partCustomizationBlockReason ? (
        <p className="production-warning" role="alert">
          <strong>Deländringarna ingår inte i serverunderlaget.</strong>{" "}
          {partCustomizationBlockReason}
        </p>
      ) : null}
      {blockingEvaluations.length > 0 ? (
        <section
          className="production-blocking-rules"
          role="alert"
          aria-label={stocklessReviewExpected ? "Krav som blockerar CAM" : "Krav som måste lösas"}
        >
          <div>
            <ShieldAlert aria-hidden="true" size={18} />
            <span>
              <strong>
                {stocklessReviewExpected ? "Blockerar CAM" : "Måste lösas"} · {blockingEvaluations.length} krav
              </strong>
              <small>
                {stocklessReviewExpected
                  ? "Kraven ligger kvar och blockerar lagerläggning, nesting och CAM. Endast ett lagerobundet designgranskningspaket får skapas."
                  : "Lös kraven nedan innan underlaget kan skapas."}
              </small>
            </span>
          </div>
          <ul>
            {blockingEvaluations.map((evaluation) => {
              const patch = productionSuggestionPatch(evaluation);
              return (
                <li key={evaluation.rule_id}>
                  <div>
                    <strong>{evaluation.title}</strong>
                    <span>{evaluation.suggestion?.explanation || evaluation.summary}</span>
                  </div>
                  {onApplyDesignChange && patch && mayDesign ? (
                    <button
                      type="button"
                      onClick={() => {
                        setError(undefined);
                        onApplyDesignChange(
                          patch,
                          evaluation.suggestion?.explanation || evaluation.summary,
                        );
                      }}
                    >
                      {evaluation.suggestion?.label || "Tillämpa föreslagen åtgärd"}
                    </button>
                  ) : null}
                </li>
              );
            })}
          </ul>
        </section>
      ) : null}
      {error && actionFeedback?.tone !== "error"
        ? <p className="production-error" role="alert">{error}</p>
        : null}

      <WorkshopContextEditor
        spec={spec}
        value={spec.workshop_context}
        frozenContext={frozenProductionContext}
        disabled={Boolean(busy) || !mayDesign || !onApplyDesignChange}
        onChange={updateWorkshopContext}
        draftState={activeWorkshopContextDraftState}
        onDraftStateChange={updateWorkshopContextDraftState}
      />
      {!mayDesign ? (
        <p className="production-action-guidance" role="status">
          Verkstadsprofilen är skrivskyddad för din roll. En designer, admin eller owner måste
          binda eller ändra råmaterial och tvåsidig registrering i en ny revision.
        </p>
      ) : null}

      <section className="warning-acknowledgement" aria-label="Serververifierad tillverkningsevidens">
        <header>
          <h3>Serververifierad evidens</h3>
          <p>
            Endast aktuella, ej utgångna poster för detta projekt och rätt designhash visas.
            Servern kontrollerar ändå signatur, checksumma, geometri, katalog och revokering igen;
            ett val i gränssnittet är aldrig i sig ett godkännande.
          </p>
        </header>
        {evidenceLoading ? (
          <p className="production-action-guidance" role="status">
            <LoaderCircle className="spin" aria-hidden="true" size={16} /> Hämtar aktuell serverevidens…
          </p>
        ) : null}
        {evidenceLoadError ? <p className="production-error" role="alert">{evidenceLoadError}</p> : null}
        {!api.listExternalEvidence ? (
          <p className="production-warning" role="status">
            Evidensregistret är inte tillgängligt i den här klientanslutningen. Retention och
            evidensberoende steg förblir blockerade.
          </p>
        ) : null}
        {design.retention_certification_request ? (
          <div
            className="production-guided-step"
            role="region"
            aria-label="Certifieringsbegäran för joint-retention"
          >
            <strong>Serverutfärdat underlag till extern certifierare</strong>
            <p>
              Begäran innehåller serverns exakta designhash, foggeometrifingerprint,
              motor-/mallversioner, material och lastfall för denna design.
            </p>
            <button
              type="button"
              disabled={Boolean(busy)}
              onClick={downloadRetentionCertificationRequest}
            >
              <Download aria-hidden="true" size={16} />
              Hämta certifieringsbegäran (.json)
            </button>
            <small>
              Detta är en begäran och ett provningsunderlag, inte signerad evidens och inte ett
              godkännande. Filen ska skickas till en oberoende extern certifierare utan att
              klienten återskapar eller ändrar hash- och versionsfälten.
            </small>
          </div>
        ) : null}
        <div className="production-guided-step">
          <label htmlFor={retentionEvidenceUploadId}>
            <strong>Ladda upp certifierarsignerad retention-JSON</strong>
          </label>
          <input
            id={retentionEvidenceUploadId}
            type="file"
            accept=".json,application/json"
            aria-label="Certifierarsignerad retention-JSON"
            aria-describedby={retentionEvidenceUploadHelpId}
            disabled={
              evidenceLoading
              || Boolean(busy)
              || !reviewerMaySelectEvidence
              || !serverProjectId
              || !api.uploadExternalEvidence
              || design.source !== "server-preview"
            }
            onChange={(event) => {
              const input = event.currentTarget;
              uploadRetentionEvidence(input.files?.[0]);
              input.value = "";
            }}
          />
          <small id={retentionEvidenceUploadHelpId}>
            Filen måste komma direkt från en extern certifierare, vara application/json med
            filändelsen .json och vara högst 20 MiB. Uppladdningen registrerar endast filen;
            den godkänner eller binder inte retention till designen.
          </small>
          {!reviewerMaySelectEvidence ? (
            <small>
              Endast reviewer, admin eller owner får registrera certifierarens fil. En designer
              kan därefter binda en serververifierbar post till nästa revision.
            </small>
          ) : null}
        </div>
        <div className="production-guided-step">
          <label htmlFor={retentionEvidenceSelectId}>
            <strong>Signerad retention för not/spår</strong>
          </label>
          <select
            id={retentionEvidenceSelectId}
            aria-label="Signerad retentionsevidens"
            value={selectedRetentionEvidenceId ?? ""}
            disabled={
              evidenceLoading
              || Boolean(busy)
              || !mayDesign
              || !serverProjectId
              || !api.setJointRetentionEvidence
              || stale
            }
            onChange={(event) => selectRetentionEvidence(event.target.value)}
          >
            <option value="">Ingen serververifierad retention vald</option>
            {retentionEvidenceOptions.map((evidence) => (
              <option key={evidence.id} value={evidence.id}>
                {evidenceOptionLabel(evidence)}
              </option>
            ))}
          </select>
          <small>
            {mayDesign
              ? retentionEvidenceOptions.length > 0
                ? "Valet hämtar en ny serverpreview och binds därefter till nästa revision."
                : "Ingen aktuell signerad retentionsevidens matchar exakt denna design. CAM förblir blockerat."
              : "Du kan läsa serverposterna, men endast designer, admin eller owner får binda retention till en ny revision."}
          </small>
        </div>
        {restoredRetentionBinding ? (
          <div
            className="production-guided-step"
            role="region"
            aria-label="Signerad retention för verkstadsverifiering"
          >
            <strong>Certifierarens signerade originalfil</strong>
            <p>
              Hämta de exakt lagrade JSON-bytes som är bundna till den sparade revisionen. API:t
              kontrollerar aktuell revision, Ed25519-signatur, aktiverat certifierarregister,
              tenant, projekt, revokering, giltighet, lagringsledger, storlek och SHA-256 på nytt
              före överföringen; webbläsaren verifierar sedan hela filen igen.
            </p>
            <button
              type="button"
              onClick={downloadBoundRetentionEvidence}
              disabled={Boolean(busy) || Boolean(retentionDownloadBlockReason)}
              aria-busy={busy === "evidence-download"}
            >
              {busy === "evidence-download"
                ? <LoaderCircle className="spin" aria-hidden="true" size={16} />
                : <Download aria-hidden="true" size={16} />}
              Hämta signerad retention-JSON (originalbytes)
            </button>
            {boundRetentionEvidence ? (
              <small>
                Evidens-ID {boundRetentionEvidence.id} · SHA-256 {boundRetentionEvidence.sha256}
              </small>
            ) : null}
            {retentionDownloadBlockReason ? <small>{retentionDownloadBlockReason}</small> : null}
            <small>
              Hämtningen är verifiering och arkivering. Den ändrar inget godkännande och
              auktoriserar aldrig fysisk kapning.
            </small>
          </div>
        ) : null}
        {version ? (
          <fieldset>
            <legend>Kompletterande evidens för sparad revision</legend>
            {GENERAL_EVIDENCE_TYPES.filter((evidenceType) => (
              generalEvidenceOptions.some((evidence) => evidence.evidence_type === evidenceType)
            )).map((evidenceType) => {
              const options = generalEvidenceOptions.filter(
                (evidence) => evidence.evidence_type === evidenceType,
              );
              return (
                <label key={evidenceType}>
                  <span>{GENERAL_EVIDENCE_LABELS[evidenceType]}</span>
                  <select
                    aria-label={`Kompletterande evidens: ${GENERAL_EVIDENCE_LABELS[evidenceType]}`}
                    value={selectedGeneralEvidence[evidenceType] ?? ""}
                    disabled={evidenceLoading || Boolean(busy) || !reviewerMaySelectEvidence || stale}
                    onChange={(event) => selectGeneralEvidence(evidenceType, event.target.value)}
                  >
                    <option value="">Inget aktuellt bevis valt</option>
                    {options.map((evidence) => (
                      <option key={evidence.id} value={evidence.id}>
                        {evidenceOptionLabel(evidence)}
                      </option>
                    ))}
                  </select>
                </label>
              );
            })}
            {generalEvidenceOptions.length === 0 ? (
              <p>Ingen aktuell kompletterande evidens matchar den sparade versionshashen.</p>
            ) : null}
            <small>
              Dessa poster måste matcha den sparade versionshashen och skickas separat vid
              generation. Retention-ID:t dupliceras aldrig till denna lista.
            </small>
          </fieldset>
        ) : null}
      </section>

      <section className="production-next-action" aria-labelledby="production-next-action-heading">
        <header>
          <span>Nästa steg</span>
          <h3 id="production-next-action-heading" ref={nextActionHeadingRef} tabIndex={-1}>
            {!serverSynchronized
              ? "Hämtar projektet"
              : busy === "save" || !version || stale || version.status === "draft"
                ? "Kontrollera designen"
                : designReviewReady
                  ? immutableReviewReleased
                    ? "Hämta låst granskningspaket"
                    : camApprovalCurrent
                      ? "Lås designgranskningsrevisionen"
                      : camBlocked
                        ? "Hämta granskningspaket"
                        : "Granska CAM-valideringspaketet"
                  : "Skapa underlag"}
          </h3>
        </header>

        {actionFeedback ? (
          <p
            className={actionFeedback.tone === "error"
              ? "production-error"
              : actionFeedback.tone === "success"
                ? "production-check-passed"
                : "production-action-guidance"}
            role={actionFeedback.tone === "error" ? "alert" : "status"}
            aria-live={actionFeedback.tone === "error" ? "assertive" : "polite"}
          >
            {actionFeedback.tone === "busy" ? <LoaderCircle className="spin" aria-hidden="true" size={16} /> : null}
            {actionFeedback.tone === "success" ? <Check aria-hidden="true" size={16} /> : null}
            {actionFeedback.tone === "error" ? <ShieldAlert aria-hidden="true" size={16} /> : null}
            <span>{actionFeedback.message}</span>
          </p>
        ) : null}

        {!serverSynchronized ? (
          <p className="production-action-guidance" role="status" aria-live="polite">
            Vänta medan projektet hämtas.
          </p>
        ) : null}

        {serverSynchronized && (!version || stale || busy === "save") ? (
          <div className="production-guided-step">
            <p>
              {stocklessReviewExpected
                ? "Spara och kontrollera modellen med oförändrade lager- och maskinmått. Underlaget skapas utan nesting eller CAM."
                : design.status === "BLOCK"
                ? "Spara modellen som utkast. De blockerande kraven ovan måste lösas innan kontrollen kan fortsätta."
                : "Modellen sparas och kontrolleras automatiskt."}
            </p>
            <button
              type="button"
              className="production-primary-action"
              onClick={saveRevision}
              disabled={Boolean(busy) || partCustomizationBlocked || !mayDesign || workshopContextBlocked}
              aria-busy={busy === "save"}
              aria-describedby={saveBlockReason ? saveBlockReasonId : undefined}
            >
              {busy === "save" ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}
              {stocklessReviewExpected
                ? "Spara för lagerobunden granskning"
                : design.status === "BLOCK"
                  ? "Spara granskningsutkast"
                  : "Spara och kontrollera"}
            </button>
            {saveBlockReason ? <small id={saveBlockReasonId}>{saveBlockReason}</small> : null}
          </div>
        ) : null}

        {serverSynchronized && current && version?.status === "draft" ? (
          <div className="production-guided-step">
            {hardBlockingEvaluations.length > 0 ? (
              <p>Åtgärda de blockerande konstruktionskraven ovan. Kontrollen fortsätter när modellen sparas på nytt.</p>
            ) : (
              <>
                <p>Modellen är sparad men kontrollen slutfördes inte. Försök igen.</p>
                <button
                  type="button"
                  className="production-primary-action"
                  onClick={validateRevision}
                  disabled={Boolean(busy) || partCustomizationBlocked || !mayDesign || workshopContextBlocked}
                  aria-busy={busy === "validate"}
                >
                  {busy === "validate" ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}
                  Kontrollera igen
                </button>
              </>
            )}
          </div>
        ) : null}

        {serverSynchronized && current && version?.status === "design_validated" && !designApproved ? (
          <div className="production-guided-step">
            <p>
              {stocklessReviewExpected
                ? "Designkontrollen är klar för granskning. Paketet skapas utan lagerläggning, nesting eller CAM tills en exakt lagerprofil har bundits på servern."
                : "Kontrollen är klar. Läs igenom eventuella varningar och skapa sedan underlaget."}
            </p>
            {acknowledgementWarningRuleIds.length > 0 ? (
              <section className="warning-acknowledgement" aria-label="Varningar att kontrollera">
                <ul className="warning-acknowledgement-list">
                  {acknowledgementWarningRuleIds.map((ruleId) => {
                    const evaluation = design.rule_evaluations.find((item) => item.rule_id === ruleId);
                    const guidance = evaluation ? validationGuidance(evaluation, spec) : undefined;
                    return (
                      <li className="warning-acknowledgement-item" key={ruleId}>
                        <ShieldAlert aria-hidden="true" size={17} />
                        <span>
                          <strong>{evaluation?.title ?? "Kontrollera konstruktionen"}</strong>
                          <small><b>Behöver beslut.</b> {guidance?.solution ?? evaluation?.summary ?? "Kontrollera punkten innan tillverkning."}</small>
                        </span>
                      </li>
                    );
                  })}
                </ul>
                <label className="warning-acknowledgement-confirmation">
                  <input
                    type="checkbox"
                    checked={warningsAcknowledged}
                    disabled={Boolean(busy)}
                    onChange={(event) => setWarningsAcknowledged(event.target.checked)}
                  />
                  <span>Jag har läst och kontrollerat varningarna ovan.</span>
                </label>
              </section>
            ) : (
              <p className={stocklessReviewExpected ? "production-warning" : "production-check-passed"}>
                {stocklessReviewExpected ? <ShieldAlert size={16} /> : <Check size={16} />}
                <strong>Designkontroll klar.</strong>{" "}
                {stocklessReviewExpected
                  ? "Lagerprofilen är fortfarande blockerad; endast designgranskningspaketet skapas."
                  : "Kontrollen är klar utan varningar."}
              </p>
            )}
            <button
              type="button"
              className="production-primary-action"
              onClick={approveDesign}
              disabled={Boolean(busy) || Boolean(designApprovalBlockReason)}
              aria-busy={busy === "design-approval"}
              aria-describedby={designApprovalBlockReason ? warningAcknowledgementHelpId : undefined}
            >
              {busy === "design-approval" ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}
              Godkänn designkontroll
            </button>
            {designApprovalBlockReason ? <small id={warningAcknowledgementHelpId}>{designApprovalBlockReason}</small> : null}
          </div>
        ) : null}

        {serverSynchronized && current && designApproved && job?.status !== "succeeded" ? (
          <div className="production-guided-step">
            {job?.status === "queued" || job?.status === "running" ? (
              <>
                <p className="production-job-progress" role="status" aria-live="polite">
                  <LoaderCircle className="spin" aria-hidden="true" size={18} /> Underlaget skapas. Det kan ta några minuter.
                </p>
                {jobPollingIssue?.jobId === job.id ? (
                  <>
                    <p className="production-action-guidance" role="alert">
                      <strong>Anslutningen återställs.</strong> Vi försöker igen automatiskt.
                    </p>
                    <button
                      type="button"
                      className="production-primary-action"
                      onClick={() => {
                        setJobPollingIssue(undefined);
                        setJobPollRetryRequest((request) => request + 1);
                      }}
                    >
                      <RefreshCw aria-hidden="true" size={16} /> Försök igen nu
                    </button>
                  </>
                ) : null}
              </>
            ) : (
              <>
                <p>
                  {job?.status === "failed" || error
                    ? productionFailureMessage(job?.error ?? "Underlaget kunde inte skapas. Försök igen när orsaken är åtgärdad.")
                    : "Skapa underlaget igen."}
                </p>
                <button
                  type="button"
                  className="production-primary-action"
                  onClick={generatePackage}
                  disabled={Boolean(busy) || Boolean(generationBlockReason)}
                  aria-busy={busy === "generation"}
                >
                  {busy === "generation" ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}
                  {stockProfileBlockedFailure
                    ? "Skapa lagerobundet granskningspaket"
                    : job?.status === "failed" || error
                      ? "Försök skapa underlag igen"
                      : "Skapa underlag"}
                </button>
                {generationBlockReason ? <small>{generationBlockReason}</small> : null}
              </>
            )}
          </div>
        ) : null}

        {serverSynchronized && current && job?.status === "succeeded" ? (
          <div className="production-guided-step production-export-card">
            {!designReviewReady ? (
              <>
                <p className="production-warning" role="alert">{completedPackageIssue}</p>
                <button
                  type="button"
                  className="production-primary-action"
                  onClick={generatePackage}
                  disabled={Boolean(busy) || Boolean(generationBlockReason)}
                  aria-busy={busy === "generation"}
                >
                  {busy === "generation" ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}
                  Skapa om underlag
                </button>
                {generationBlockReason ? <small>{generationBlockReason}</small> : null}
              </>
            ) : (
              <>
                <div className="production-export-card-header">
                  <span className="status-badge status-pass"><Check aria-hidden="true" size={14} /> Designgranskning klar</span>
                  <h4>Granskningspaketet är klart</h4>
                </div>
                <p>
                  {camBlocked
                    ? "En ZIP-fil med design, ritningar och verifieringsrapporter är redo att hämtas. CAM-filer ingår inte."
                    : "En ZIP-fil med ritningar, verifieringsrapporter och valideringsfiler är redo att hämtas."}
                </p>
                {camBlocked ? (
                  <p className="production-warning" role="status" aria-label="Status för CAM">
                    <strong>CAM är blockerat.</strong>{" "}
                    {stockBlocked
                      ? "En exakt serverbunden lagerprofil saknas. Lagerinköp, nesting, operationer, setupblad, backplot och maskinvalideringskod har därför avsiktligt utelämnats. Modellens mått och valda profiler har inte ändrats eller ersatts med antagna storformat."
                      : grainBlocked
                        ? "En riktad materialprofil saknar en exakt, strukturerad X/Y-bindning till den verkliga råskivan. Nesting, operationer, setupblad, backplot och maskinvalideringskod har därför avsiktligt utelämnats. Uppladdade dokument och varningsgodkännanden kan inte ange eller låsa denna axel."
                        : dadoRetentionBlocked
                          ? "Not/spår-förbanden saknar en versionsbunden, checksummeadresserad torr självlåsning eller mekanisk retention. Operationer, setupblad, backplot och maskinvalideringskod har därför avsiktligt utelämnats. Lim, bärande geometri och granskningsgodkännanden ersätter inte retentionsevidens."
                          : backPanelRetentionBlocked
                            ? "Bakstycket saknar den kanoniska fyrsidiga mekaniska infångningen eller separat autentiserad retentionsevidens. Operationer, setupblad, backplot och maskinvalideringskod har därför avsiktligt utelämnats. Paketet är fortfarande tillgängligt för designgranskning, inte kapning."
                          : "Tvåsidiga delar saknar en verifierad registrerings- och fixturplan. Nesting, operationer, setupblad, backplot och maskinvalideringskod har därför avsiktligt utelämnats. Inga WCS-, pinn- eller fixturdata har antagits."}
                  </p>
                ) : null}
                <section
                  className="warning-acknowledgement"
                  aria-label="CAM-granskning och immutable designrevision"
                >
                  <header>
                    <h5>CAM-granskning och revisionslås</h5>
                    <p>
                      Två separata reviewers måste först godkänna design respektive exakt genererat
                      CAM-valideringspaket. Revisionslåset fryser därefter designgranskningspaketet;
                      det skapar aldrig en arbetsorder eller skärande CNC-kod.
                    </p>
                  </header>
                  {immutableReviewReleased ? (
                    <>
                      <p className="production-check-passed" role="status">
                        <Check aria-hidden="true" size={16} />
                        <span>
                          <strong>Immutable designgranskningsrevision {release?.release_number}.</strong>{" "}
                          Manifest <code>{release?.manifest_sha256}</code> och ZIP{" "}
                          <code>{release?.bundle_sha256}</code> är låsta för designgranskning och
                          icke-skärande validering. Fysisk kapning är fortfarande inte auktoriserad.
                        </span>
                      </p>
                      <section aria-label="Verifiera frisläppt ZIP">
                        <p>
                          Verifiera den hämtade ZIP-filen med den separat betrodda verifieraren och
                          exakt denna frisläppningsbindning:
                        </p>
                        <code>--expect-bundle-sha256 {release?.bundle_sha256}</code>
                        <p>
                          Ett verifierings-PASS bekräftar filidentiteten men auktoriserar inte fysisk
                          kapning.
                        </p>
                      </section>
                    </>
                  ) : camBlocked ? (
                    <p className="production-warning" role="status">
                      CAM-granskning och revisionslås är blockerade eftersom paketet sanningsenligt
                      saknar CAM-valideringsfiler. Designgranskningspaketet kan fortfarande hämtas.
                    </p>
                  ) : !camApprovalCurrent ? (
                    <>
                      <label className="warning-acknowledgement-confirmation">
                        <input
                          type="checkbox"
                          checked={camApprovalConfirmed}
                          disabled={
                            Boolean(busy)
                            || !mayReview
                            || !camValidationPackageEligible
                            || !designApproverId
                            || principal?.user_id === designApproverId
                          }
                          onChange={(event) => setCamApprovalConfirmed(event.target.checked)}
                        />
                        <span>
                          Jag har granskat exakt jobb, manifest och maskinbunden validering. Jag
                          förstår att programmet inte är skärande CNC-kod.
                        </span>
                      </label>
                      <button
                        type="button"
                        className="production-primary-action"
                        onClick={approveCamPackage}
                        disabled={Boolean(busy) || Boolean(camApprovalBlockReason)}
                        aria-busy={busy === "cam-approval"}
                      >
                        {busy === "cam-approval"
                          ? <LoaderCircle className="spin" aria-hidden="true" size={16} />
                          : <Check aria-hidden="true" size={16} />}
                        Godkänn CAM-valideringspaket
                      </button>
                      {camApprovalBlockReason ? <small>{camApprovalBlockReason}</small> : null}
                    </>
                  ) : (
                    <>
                      <p className="production-check-passed" role="status">
                        <Check aria-hidden="true" size={16} />
                        <span>
                          <strong>CAM-granskningen är bunden till aktuellt jobb och manifest.</strong>{" "}
                          Revisionen är ännu inte låst.
                        </span>
                      </p>
                      <label className="warning-acknowledgement-confirmation">
                        <input
                          type="checkbox"
                          checked={releaseConfirmed}
                          disabled={Boolean(busy) || !mayReview}
                          onChange={(event) => setReleaseConfirmed(event.target.checked)}
                        />
                        <span>
                          Jag bekräftar att revisionslåset endast gäller immutable designgranskning
                          och validering. Det auktoriserar aldrig fysisk kapning.
                        </span>
                      </label>
                      <button
                        type="button"
                        className="production-primary-action"
                        onClick={releaseDesignReview}
                        disabled={Boolean(busy) || Boolean(releaseBlockReason)}
                        aria-busy={busy === "release"}
                      >
                        {busy === "release"
                          ? <LoaderCircle className="spin" aria-hidden="true" size={16} />
                          : <Check aria-hidden="true" size={16} />}
                        Lås designgranskningsrevision R{version?.revision}
                      </button>
                      {releaseBlockReason ? <small>{releaseBlockReason}</small> : null}
                    </>
                  )}
                </section>
                <p
                  className="production-warning"
                  role="status"
                  aria-label="Status för fysisk tillverkning"
                >
                  <strong>Ej frisläppt för fysisk kapning.</strong>{" "}
                  {missingWorkshopRequirements.length} externa verkstadskrav återstår.
                  Paketet är endast avsett för designgranskning och validering;
                  {camBlocked
                    ? " det innehåller inga CAM- eller maskinvalideringsfiler."
                    : " använd inte valideringsprogrammet som skärande CNC-kod."}
                </p>
                <section
                  className="production-release-boundary"
                  aria-label="Skillnad mellan designgranskning och fysisk frisläppning"
                >
                  <article className="review-ready">
                    <span>1 · Designgranskning</span>
                    <strong>Klar för revision {version?.revision ?? "–"}</strong>
                    <small>Filerna får öppnas, jämföras och kommenteras som granskningsunderlag.</small>
                  </article>
                  <article className="physical-blocked">
                    <span>2 · Fysisk tillverkning</span>
                    <strong>Ej frisläppt</strong>
                    <small>
                      {missingWorkshopRequirements.length} externa krav återstår. Filerna är inte
                      en arbetsorder eller skärande CNC-kod.
                    </small>
                  </article>
                </section>
                <section
                  className="production-handoff-guide"
                  aria-label="Vägledning för hur delarna ska tas fram"
                >
                  <header>
                    <h5>Hur ska delarna tas fram?</h5>
                    <p>Välj bara vilken vägledning du vill läsa. Valet ändrar inte modellen eller granskningspaketet.</p>
                  </header>
                  <fieldset>
                    <legend>Tillverkningssätt för vägledningen</legend>
                    <div className="production-handoff-options">
                      <label className={handoffGuidanceMode === "self_build" ? "active" : undefined}>
                        <input
                          type="radio"
                          name={handoffGuidanceName}
                          value="self_build"
                          checked={handoffGuidanceMode === "self_build"}
                          onChange={() => setHandoffGuidanceMode("self_build")}
                        />
                        <span>
                          <strong>Jag kapar och bygger själv</strong>
                          <small>Handverktyg, bordssåg eller egen verkstad</small>
                        </span>
                      </label>
                      <label className={handoffGuidanceMode === "workshop" ? "active" : undefined}>
                        <input
                          type="radio"
                          name={handoffGuidanceName}
                          value="workshop"
                          checked={handoffGuidanceMode === "workshop"}
                          onChange={() => setHandoffGuidanceMode("workshop")}
                        />
                        <span>
                          <strong>En verkstad kapar eller bearbetar</strong>
                          <small>Sågservice, snickeri eller CNC-verkstad</small>
                        </span>
                      </label>
                    </div>
                  </fieldset>
                  <div className="production-handoff-message" role="status" aria-live="polite">
                    {handoffGuidanceMode === "self_build" ? (
                      <>
                        <strong>Självbygget är inte frisläppt.</strong>
                        <p>
                          ZIP-filen innehåller bland annat BOM, kaplista, delritningar och monteringsmanual för designgranskning.
                          De är inte en verifierad arbetsinstruktion för handverktyg. Kontrollera material, limfria förband,
                          toleranser, verktyg och en fysisk prototyp innan du kapar.
                        </p>
                      </>
                    ) : (
                      <>
                        <strong>Verkstadsöverlämningen är inte körklar.</strong>
                        <p>
                          Skicka ZIP-filen som granskningsunderlag, inte som ett färdigt maskinjobb. Verkstaden måste binda
                          exakt råmaterial, fiberriktning, maskin, verktyg, nollpunkt och fixturering samt godkänna den
                          slutliga körningen.
                        </p>
                      </>
                    )}
                  </div>
                  <small>
                    Valet ändrar endast vägledningen. Design, filer, blockerare och frisläppningsstatus förblir oförändrade.
                  </small>
                </section>
                <section className="production-package-identity" aria-label="Paketidentitet">
                  <h5>Paketidentitet</h5>
                  <dl>
                    <div>
                      <dt>Revision</dt>
                      <dd>{version?.revision ?? "–"}</dd>
                    </div>
                    <div>
                      <dt>Omfattning</dt>
                      <dd>Designgranskning</dd>
                    </div>
                    <div>
                      <dt>Status</dt>
                      <dd>Designgranskning klar</dd>
                    </div>
                    <div>
                      <dt>Fysisk frisläppning</dt>
                      <dd>Ej frisläppt</dd>
                    </div>
                    <div>
                      <dt>Dokumentspråk</dt>
                      <dd>Svenska PDF:er · tekniska datafält på engelska</dd>
                    </div>
                    <div>
                      <dt>ZIP-storlek</dt>
                      <dd>{productionBundle ? formatArtifactSize(productionBundle.size_bytes) : "–"}</dd>
                    </div>
                    <div>
                      <dt>Artefakter</dt>
                      <dd>{artifacts.length}</dd>
                    </div>
                    <div className="production-package-hash">
                      <dt>ZIP SHA-256</dt>
                      <dd><code>{bundleSha256 ?? "Saknas"}</code></dd>
                    </div>
                    <div className="production-package-hash">
                      <dt>Manifest SHA-256</dt>
                      <dd><code>{manifestSha256 ?? "Saknas"}</code></dd>
                    </div>
                  </dl>
                  <section
                    className="production-customer-documents"
                    aria-label="Kunddokument i granskningspaketet"
                  >
                    <header>
                      <span>
                        <strong>Förväntade kunddokument i ZIP-filen</strong>
                        <small>Svenska PDF:er · revision {version?.revision ?? "–"} · endast designgranskning</small>
                      </span>
                      <b>{CUSTOMER_REVIEW_DOCUMENTS.length}</b>
                    </header>
                    <ul>
                      {CUSTOMER_REVIEW_DOCUMENTS.map(([name, format, purpose]) => (
                        <li key={name}>
                          <span>
                            <strong>{name}</strong>
                            <small>{purpose}</small>
                          </span>
                          <b>{format}</b>
                        </li>
                      ))}
                    </ul>
                    <p>
                      Börja med <code>START-HERE.md</code> och kontrollera <code>manifest.json</code>.
                      Paketet kan innehålla STEP-modellen, delarnas sida A/B som DXF och SVG samt
                      versionslåsta JSON-scheman. När en exakt verkstadsprofil är bunden och
                      bearbetningsgrinden passerar listas även råmaterialval, generationsplan,
                      operationer och setupblad.
                      Manifestet är den auktoritativa inventeringen för faktisk förekomst, identitet
                      och checksumma; den här listan är inte ett tillstånd att kapa.
                    </p>
                  </section>
                  <section
                    className="production-artifact-inventory"
                    aria-label="Separat verifierbara serverfiler"
                  >
                    <header>
                      <span>
                        <strong>Separat verifierbara serverfiler</strong>
                        <small>Varje hämtning verifieras på nytt mot checksummebunden serverinventering.</small>
                      </span>
                      <b>{artifactInventory.length}</b>
                    </header>
                    <ul>
                      {artifactInventory.map((item) => (
                        <li key={item.artifact.id}>
                          <span>
                            <strong>{item.label}</strong>
                            <small>
                              {item.format} · revision {version?.revision ?? "–"} · {formatArtifactSize(item.artifact.size_bytes)} · {item.reviewUse}
                            </small>
                          </span>
                          <button
                            type="button"
                            onClick={() => downloadIndividualArtifact(item.artifact)}
                            disabled={Boolean(busy)}
                            aria-busy={busy === "download" && downloadingArtifactId === item.artifact.id}
                            aria-label={`Hämta fil – ${item.label}`}
                          >
                            {busy === "download" && downloadingArtifactId === item.artifact.id
                              ? <LoaderCircle className="spin" aria-hidden="true" size={14} />
                              : <FileDown aria-hidden="true" size={14} />}
                            Hämta fil
                          </button>
                        </li>
                      ))}
                    </ul>
                  </section>
                </section>
                {!workshopReadiness?.physical_cutting_authorized && missingWorkshopRequirements.length > 0 ? (
                  <section
                    className="production-workshop-requirements"
                    aria-label="Återstående externa verkstadskrav"
                  >
                    <h5>Återstår före fysisk kapning</h5>
                    <p>
                      Följande krav är fortfarande öppna. De grupperas här för planering, men måste verifieras utanför
                      designgranskningsflödet och kan inte markeras klara i gränssnittet.
                    </p>
                    <div className="production-requirement-groups">
                      {groupedMissingWorkshopRequirements.map((group) => (
                        <section key={group.id} aria-label={group.title}>
                          <header>
                            <span>
                              <strong>{group.title}</strong>
                              <small>{group.description}</small>
                            </span>
                            <b>{group.requirements.length}</b>
                          </header>
                          <ul>
                            {group.requirements.map(({ requirement, presentation }) => (
                              <li key={requirement.code}>
                                <span>
                                  <strong>{presentation.title}</strong>
                                  <small>Ansvar: {presentation.owner}</small>
                                </span>
                                <p>{presentation.nextAction}</p>
                                <code>{requirement.code}</code>
                              </li>
                            ))}
                          </ul>
                        </section>
                      ))}
                    </div>
                  </section>
                ) : null}
                <button
                  type="button"
                  className="production-primary-action"
                  onClick={downloadPackage}
                  disabled={Boolean(busy)}
                  aria-busy={busy === "download"}
                >
                  {busy === "download" ? <LoaderCircle className="spin" size={16} /> : <Download size={16} />}
                  Ladda ned granskningspaket (.zip)
                </button>
                <button type="button" onClick={generatePackage} disabled={Boolean(busy) || Boolean(generationBlockReason)}>
                  <RefreshCw aria-hidden="true" size={15} /> Skapa om granskningspaket
                </button>
                {generationBlockReason ? <small>{generationBlockReason}</small> : null}
              </>
            )}
          </div>
        ) : null}
      </section>
    </div>
  );
}

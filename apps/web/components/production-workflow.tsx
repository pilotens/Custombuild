"use client";

import { Check, Download, LoaderCircle, RefreshCw, ShieldAlert } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import {
  ApiError,
  CustombuildApiClient,
  type ArtifactRead,
  type DesignVersionRead,
  type JobRead,
  versionProductionContextMatches,
} from "@/lib/api-client";
import type { DesignSpec, ResolvedDesign, RuleEvaluation } from "@/lib/design-types";
import {
  clearLegacyProductionStorage,
  productionSessionKey,
  readProductionSession,
  writeProductionSession,
} from "@/lib/production-session-storage";
import type { WorkspaceIdentity } from "@/lib/workspace-draft-storage";
import type { FurnitureTemplateId } from "@/lib/furniture-templates";
import {
  permitsStocklessDesignReview,
  validationGuidance,
} from "@/lib/validation-guidance";
import { RevisionHistory } from "./revision-history";

type BusyAction =
  | "save"
  | "validate"
  | "design-approval"
  | "generation"
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
  | "getJob"
  | "listArtifacts"
> & Partial<Pick<CustombuildApiClient, "listVersions">>;

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
  principal?: WorkspaceIdentity;
  showRevisionHistory?: boolean;
}

const PROJECT_NAME = "Arkitektväggen";
const ARTIFACT_INTEGRITY_API_MESSAGE = (
  "API 409: Production evidence failed integrity verification; regenerate the package"
);
const STOCK_PROFILE_MISSING_CODE = "STOCK_PROFILE_MISSING";
const DFM_GRAIN_MISSING_CODE = "DFM-GRAIN-001";
const TWO_SIDED_REGISTRATION_MISSING_CODE = "TWO_SIDED_REGISTRATION_MISSING";
const DADO_RETENTION_EVIDENCE_MISSING_CODE = "DADO_RETENTION_EVIDENCE_MISSING";
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
    "Bind a versioned, checksum-addressed dry self-locking joint or mechanical "
    + "retention system for every DADO joint; a review acknowledgement, adhesive or "
    + "geometric bearing check is not retention evidence."
  ),
};

function productionErrorHasCode(message: string | null | undefined, code: string): boolean {
  return message?.split(/[^A-Za-z0-9_]+/).includes(code) ?? false;
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
    generation: "Kontrollera orsaken ovan och välj Försök skapa underlag igen.",
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

const GENERATED_CAM_REVIEW_ARTIFACT_KINDS = [
  "production_bundle",
  "manifest",
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
): boolean {
  if (statusClaimed !== (status !== undefined)) return false;
  if (!status || !statusClaimed) return false;
  const kinds = artifacts.map((artifact) => artifact.kind);
  if (
    kinds.some((kind) => !kind)
    || new Set(kinds.map((kind) => kind.toLowerCase())).size !== kinds.length
  ) return false;

  const requiredKinds = status.cam_status === "BLOCKED"
    ? BLOCKED_CAM_REVIEW_ARTIFACT_KINDS
    : [...GENERATED_CAM_REVIEW_ARTIFACT_KINDS, "design_review_package_status"];
  if (!requiredKinds.every((kind) => kinds.includes(kind))) return false;

  if (status.cam_status === "BLOCKED") {
    return !kinds.some(blockedCamEvidenceKindIsForbidden);
  }
  return status.validation_program_included === true;
}

function hasExactKeys(
  value: Record<string, unknown>,
  expectedKeys: readonly string[],
): boolean {
  const actualKeys = Object.keys(value);
  return actualKeys.length === expectedKeys.length
    && expectedKeys.every((key) => Object.prototype.hasOwnProperty.call(value, key));
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

function artifactRoleLabel(kind: string): string {
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
  if (kind.startsWith("setup_sheet_")) return "Setupblad";
  return kind.replaceAll("_", " ");
}

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
}: ProductionWorkflowProps) {
  const defaultApi = useMemo(() => new CustombuildApiClient(), []);
  const api = apiClient ?? defaultApi;
  const [version, setVersion] = useState<DesignVersionRead>();
  const [job, setJob] = useState<JobRead>();
  const [artifacts, setArtifacts] = useState<ArtifactRead[]>([]);
  const [designApproved, setDesignApproved] = useState(false);
  const [warningsAcknowledged, setWarningsAcknowledged] = useState(false);
  const [busy, setBusy] = useState<BusyAction>();
  const [error, setError] = useState<string>();
  const [actionFeedback, setActionFeedback] = useState<ActionFeedback>();
  const [jobPollingIssue, setJobPollingIssue] = useState<JobPollingIssue>();
  const [jobPollRetryRequest, setJobPollRetryRequest] = useState(0);
  const [handoffGuidanceMode, setHandoffGuidanceMode] = useState<HandoffGuidanceMode>("self_build");
  const [loadedStorageKey, setLoadedStorageKey] = useState<string>();
  const [serverSynchronized, setServerSynchronized] = useState(false);
  const [synchronizing, setSynchronizing] = useState(false);
  const saveBlockReasonId = useId();
  const warningAcknowledgementHelpId = useId();
  const handoffGuidanceName = useId();
  const nextActionHeadingRef = useRef<HTMLHeadingElement>(null);
  const previousActiveStepRef = useRef<number | undefined>(undefined);
  const stepFocusInitializedRef = useRef(false);
  const storageKey = productionSessionKey(principal, projectId, spec.design_id);

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
      setWarningsAcknowledged(false);
      setActionFeedback(undefined);
      setError(undefined);
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
          setWarningsAcknowledged(false);
          setServerSynchronized(true);
          setError(undefined);
          return;
        }

        const state = await api.getProductionState(resolvedProjectId);
        if (cancelled) return;
        const restoredVersion = state.version ?? undefined;
        let restoredJob = state.latest_job ?? undefined;
        const designApproval = state.approvals.find((approval) => approval.approval_type === "design");
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
        setJob(restoredJob);
        setArtifacts(restoredArtifacts);
        setDesignApproved(Boolean(designApproval));
        setWarningsAcknowledged(false);
        setError(undefined);
        setServerSynchronized(true);
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
  }, [active, api, loadedStorageKey, projectId, projectName, storageKey]);

  useEffect(() => {
    if (loadedStorageKey !== storageKey) return;
    writeProductionSession(window.sessionStorage, storageKey, {
      version,
      job,
      release: undefined,
      designApproved,
      // Kept only for backward compatibility with already persisted v2 sessions.
      // The simplified user flow no longer exposes release numbers or locking.
      releaseNumber: "R1",
    });
  }, [designApproved, job, loadedStorageKey, storageKey, version]);

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
  const current = Boolean(version && !stale);
  const stockProfileBlockedFailure = Boolean(
    job?.status === "failed" && productionErrorHasCode(job.error, STOCK_PROFILE_MISSING_CODE),
  );
  const productionBundle = artifacts.find((artifact) => artifact.kind === "production_bundle");
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
  const requiredArtifactsComplete = reviewPackageArtifactInventoryIsTruthful(
    artifacts,
    reviewPackageStatus,
    reviewPackageStatusClaimed,
  );
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
  const retentionBlocked = blockedCamCode === DADO_RETENTION_EVIDENCE_MISSING_CODE;
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
  const artifactInventory = [...artifacts.reduce((inventory, artifact) => {
    inventory.set(artifact.kind, (inventory.get(artifact.kind) ?? 0) + 1);
    return inventory;
  }, new Map<string, number>())]
    .map(([kind, count]) => ({ kind, count, label: artifactRoleLabel(kind) }))
    .sort((left, right) => left.label.localeCompare(right.label, "sv"));
  const completedArtifactSet = Boolean(
    job?.status === "succeeded" && requiredArtifactsComplete && productionBundle,
  );
  const designApprovalBlockReason = hardBlockingEvaluations.length > 0
    ? `${hardBlockingEvaluations.length} blockerande krav måste åtgärdas i ritningen och kontrolleras igen innan underlaget kan skapas.`
    : acknowledgementWarningRuleIds.length > 0 && !warningsAcknowledged
      ? "Bekräfta att du har läst och kontrollerat varningarna för att fortsätta."
      : undefined;
  const designReviewReady = Boolean(
    completedArtifactSet
    && workshopReadiness
    && (camBlocked ? blockedCamReviewIsTruthful : workshopReadiness.design_review_ready),
  );
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
    : !serverSynchronized
      ? "Hämtar projektet från servern."
      : design.source !== "server-preview"
        ? "Inväntar serverns auktoritativa konstruktionsmodell för de senaste ändringarna."
        : version?.immutable && !stale
          ? "Den sparade modellen kan inte ändras. Gör en ändring i modellen och spara igen."
          : undefined;

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
    setWarningsAcknowledged(false);
  }

  function saveRevision() {
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
        saved = await api.createVersion(
          resolvedProjectId,
          spec,
          design.design_hash,
          version?.revision ?? 0,
          templateId,
        );
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
    void perform("validate", "Kontrollerar den sparade modellen…", async () => {
      if (!version) throw new ApiError("Ingen sparad modell finns att kontrollera.");
      const validated = await api.validateVersion(version.project_id, version.revision);
      setVersion(validated);
      return "Kontrollen är klar. Fortsätt med Skapa underlag.";
    });
  }

  function approveDesign() {
    void perform("design-approval", "Godkänner kontrollen och skapar underlaget…", async () => {
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
          evidence_ids: [],
        })),
      });
      setVersion(approved);
      setDesignApproved(true);
      const queued = await api.generateVersion(version.project_id, version.revision, {
        stock_width_mm: spec.stock_width_mm,
        stock_height_mm: spec.stock_height_mm,
        stock_count: spec.stock_count,
        back_stock_width_mm: spec.back_stock_width_mm,
        back_stock_height_mm: spec.back_stock_height_mm,
        back_stock_count: spec.back_stock_count,
        machine_profile_id: spec.machine_profile_id,
        postprocessor_id: "linuxcnc-validation-1.0.0",
        include_step: true,
        include_freecad_project: false,
        include_validation_program: true,
        external_evidence_ids: [],
      });
      setJob(queued);
      setArtifacts([]);
      return "Designkontrollen är godkänd och underlaget skapas nu.";
    });
  }

  function generatePackage() {
    void perform("generation", "Skapar ett nytt underlag…", async () => {
      if (!version) throw new ApiError("Ingen kontrollerad modell finns att skapa underlag för.");
      const queued = await api.generateVersion(version.project_id, version.revision, {
        stock_width_mm: spec.stock_width_mm,
        stock_height_mm: spec.stock_height_mm,
        stock_count: spec.stock_count,
        back_stock_width_mm: spec.back_stock_width_mm,
        back_stock_height_mm: spec.back_stock_height_mm,
        back_stock_count: spec.back_stock_count,
        machine_profile_id: spec.machine_profile_id,
        postprocessor_id: "linuxcnc-validation-1.0.0",
        include_step: true,
        include_freecad_project: false,
        include_validation_program: true,
        external_evidence_ids: [],
      });
      setJob(queued);
      setArtifacts([]);
      return "Ett nytt underlag skapas nu.";
    });
  }

  function downloadPackage() {
    void perform("download", "Förbereder hämtningen…", async () => {
      if (!job || !designReviewReady) {
        throw new ApiError("Det finns inget verifierat granskningspaket att hämta.");
      }
      const currentArtifacts = await api.listArtifacts(job.id);
      setArtifacts(currentArtifacts);
      if (!reviewPackageArtifactInventoryIsTruthful(
        currentArtifacts,
        reviewPackageStatus,
        reviewPackageStatusClaimed,
      )) {
        throw new ApiError(
          "Granskningspaketets aktuella artefaktlista är inte längre verifierbar. Skapa om paketet.",
        );
      }
      const artifact = currentArtifacts.find((candidate) => candidate.kind === "production_bundle");
      if (!artifact) throw new ApiError("Granskningspaketet saknas eller är inte längre tillgängligt.");
      const link = document.createElement("a");
      link.href = artifact.download_url;
      link.download = "designgranskningspaket.zip";
      link.rel = "noopener noreferrer";
      document.body.append(link);
      link.click();
      link.remove();
      return "Granskningspaketet har hämtats.";
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
                  {onApplyDesignChange && patch ? (
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

      <section className="production-next-action" aria-labelledby="production-next-action-heading">
        <header>
          <span>Nästa steg</span>
          <h3 id="production-next-action-heading" ref={nextActionHeadingRef} tabIndex={-1}>
            {!serverSynchronized
              ? "Hämtar projektet"
              : busy === "save" || !version || stale || version.status === "draft"
                ? "Kontrollera designen"
                : designReviewReady
                  ? "Hämta granskningspaket"
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
              disabled={Boolean(busy)}
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
                  disabled={Boolean(busy)}
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
              Skapa underlag
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
                  disabled={Boolean(busy)}
                  aria-busy={busy === "generation"}
                >
                  {busy === "generation" ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}
                  {stockProfileBlockedFailure
                    ? "Skapa lagerobundet granskningspaket"
                    : job?.status === "failed" || error
                      ? "Försök skapa underlag igen"
                      : "Skapa underlag"}
                </button>
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
                  disabled={Boolean(busy)}
                  aria-busy={busy === "generation"}
                >
                  {busy === "generation" ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}
                  Skapa om underlag
                </button>
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
                        : retentionBlocked
                          ? "Not/spår-förbanden saknar en versionsbunden, checksummeadresserad torr självlåsning eller mekanisk retention. Operationer, setupblad, backplot och maskinvalideringskod har därför avsiktligt utelämnats. Lim, bärande geometri och granskningsgodkännanden ersätter inte retentionsevidens."
                          : "Tvåsidiga delar saknar en verifierad registrerings- och fixturplan. Nesting, operationer, setupblad, backplot och maskinvalideringskod har därför avsiktligt utelämnats. Inga WCS-, pinn- eller fixturdata har antagits."}
                  </p>
                ) : null}
                <p
                  className={workshopReadiness?.physical_cutting_authorized
                    ? "production-check-passed"
                    : "production-warning"}
                  role="status"
                  aria-label="Status för fysisk tillverkning"
                >
                  {workshopReadiness?.physical_cutting_authorized ? (
                    <><strong>Fysisk kapning är auktoriserad av servern.</strong> Följ fortfarande verkstadens arbetsorder och säkerhetsrutiner.</>
                  ) : (
                    <>
                      <strong>Ej frisläppt för fysisk kapning.</strong>{" "}
                      {missingWorkshopRequirements.length} externa verkstadskrav återstår.
                      Paketet är endast avsett för designgranskning och validering;
                      {camBlocked
                        ? " det innehåller inga CAM- eller maskinvalideringsfiler."
                        : " använd inte valideringsprogrammet som skärande CNC-kod."}
                    </>
                  )}
                </p>
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
                      <dt>ZIP-storlek</dt>
                      <dd>{productionBundle ? formatArtifactSize(productionBundle.size_bytes) : "–"}</dd>
                    </div>
                    <div>
                      <dt>Artefakter</dt>
                      <dd>{artifacts.length}</dd>
                    </div>
                    <div className="production-package-hash">
                      <dt>Manifest SHA-256</dt>
                      <dd><code>{manifestSha256 ?? "Saknas"}</code></dd>
                    </div>
                  </dl>
                  <div className="production-artifact-inventory">
                    <strong>Tillgängligt innehåll</strong>
                    <ul>
                      {artifactInventory.map((item) => (
                        <li key={item.kind}>
                          <span>{item.label}</span>
                          <small>{item.count} {item.count === 1 ? "fil" : "filer"}</small>
                        </li>
                      ))}
                    </ul>
                  </div>
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
                <button type="button" onClick={generatePackage} disabled={Boolean(busy)}>
                  <RefreshCw aria-hidden="true" size={15} /> Skapa om granskningspaket
                </button>
              </>
            )}
          </div>
        ) : null}
      </section>
    </div>
  );
}

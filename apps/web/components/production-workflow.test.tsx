import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  productionContextFromSpec,
  type ArtifactRead,
  type DesignVersionRead,
  type ExternalEvidenceRead,
  type JobRead,
  type ProductionStateRead,
  type ProjectRead,
  type ReleaseRead,
} from "@/lib/api-client";
import { resolveDesign } from "@/lib/design-engine";
import {
  DEFAULT_DESIGN_SPEC,
  MACHINES,
  type DesignSpec,
  type ResolvedDesign,
  type RuleEvaluation,
} from "@/lib/design-types";
import { productionSessionKey, readProductionSession } from "@/lib/production-session-storage";
import {
  ProductionWorkflow,
  approvalExternalEvidenceIds,
  artifactDownloadFileName,
  artifactFileExtension,
  artifactReviewUseLabel,
  artifactRoleLabel,
  blockedCamEvidenceKindIsForbidden,
  canonicalRetentionCertificationRequestJson,
  designReviewPackageStatusFromJob,
  generationProgressMessage,
  permitsStocklessDesignReview,
  productionSuggestionPatch,
  retentionEvidenceUploadMetadata,
  reviewArtifactKindsFromJob,
  reviewPackageArtifactInventoryIsTruthful,
  serverApprovalWarningRuleIds,
  workshopRequirementPresentation,
  workshopReadinessFromJob,
  type ProductionApi,
} from "./production-workflow";

const project: ProjectRead = {
  id: "11111111-1111-4111-8111-111111111111",
  name: "Arkitektväggen",
  description: "",
  furniture_type: "bookcase",
  current_revision: 1,
  archived: false,
  created_at: "2026-08-01T08:00:00Z",
  updated_at: "2026-08-01T08:00:00Z",
};

const designerPrincipal = {
  organization_id: "org-1",
  user_id: "designer-1",
  role: "designer",
} as const;

const reviewerPrincipal = {
  organization_id: "org-1",
  user_id: "reviewer-1",
  role: "reviewer",
} as const;

const secondReviewerPrincipal = {
  organization_id: "org-1",
  user_id: "reviewer-2",
  role: "reviewer",
} as const;

const ownerPrincipal = {
  organization_id: "org-1",
  user_id: "owner-1",
  role: "owner",
} as const;

const operatorPrincipal = {
  organization_id: "org-1",
  user_id: "operator-1",
  role: "operator",
} as const;

function structuredWorkshopSpec(): DesignSpec {
  return {
    ...DEFAULT_DESIGN_SPEC,
    workshop_context: {
      stock_profiles: [
        {
          role: "carcass",
          declaration_authority: "CLIENT_DECLARED",
          supplier_profile_id: "supplier-birch-18",
          supplier_profile_version: "batch-2026.09",
          material_id: "birch-plywood",
          material_version: "screening-2026.1",
          sheet_width_um: 2_440_000,
          sheet_height_um: 1_220_000,
          thickness_um: 17_800,
          sheet_count: 4,
          trim_margin_um: 10_000,
          kerf_um: 6_000,
          grain_direction: "X",
          allow_rotation: false,
          defect_zones: [],
          fixture_keep_out_zones: [],
        },
        {
          role: "back",
          declaration_authority: "CLIENT_DECLARED",
          supplier_profile_id: "supplier-birch-6",
          supplier_profile_version: "batch-2026.09",
          material_id: "birch-plywood-6",
          material_version: "screening-2026.1",
          sheet_width_um: 2_440_000,
          sheet_height_um: 1_220_000,
          thickness_um: 6_000,
          sheet_count: 2,
          trim_margin_um: 10_000,
          kerf_um: 6_000,
          grain_direction: "X",
          allow_rotation: false,
          defect_zones: [],
          fixture_keep_out_zones: [],
        },
      ],
      two_sided_registrations: [{
        stock_role: "carcass",
        sheet_index: 0,
        declaration_authority: "CLIENT_DECLARED",
        flip_axis: "X",
        fixture_method_id: "shop-pin-fixture",
        fixture_method_version: "v1.2",
        pin_diameter_um: 10_000,
        position_tolerance_um: 1_000,
        pins: [{ x_um: 80_000, y_um: 30_000 }, { x_um: 2_360_000, y_um: 30_000 }],
      }],
    },
  };
}

function version(status: DesignVersionRead["status"]): DesignVersionRead {
  return {
    id: "version-1",
    project_id: project.id,
    revision: 1,
    status,
    immutable: false,
    design_hash: resolveDesign(DEFAULT_DESIGN_SPEC).design_hash,
    context_hash: "b".repeat(64),
    engine_version: "1.0.0",
    template_version: "1.0.0",
    template_id: "shelving",
    template_capability_fingerprint: "e".repeat(64),
    rule_version: "1.0.0",
    spec_json: {},
    source_provenance_json: {},
    source_import_id: null,
    result_json: { production_context: productionContextFromSpec(DEFAULT_DESIGN_SPEC) },
    created_at: "2026-08-01T08:00:00Z",
  };
}

const queuedJob: JobRead = {
  id: "job-1",
  design_version_id: "version-1",
  status: "queued",
  production_context_hash: "c".repeat(64),
  production_engine_context_json: {},
  attempts: 0,
  error: null,
  result_json: null,
  started_at: null,
  lease_expires_at: null,
  deadline_at: "2026-08-01T10:01:00Z",
  next_attempt_at: null,
  finished_at: null,
  created_at: "2026-08-01T08:01:00Z",
  updated_at: "2026-08-01T08:01:00Z",
};

type ReadinessFixtureStatus = "VERIFIED" | "MISSING" | "EXTERNAL_EVIDENCE_REQUIRED";

interface ReadinessFixtureRequirement {
  code: string;
  title: string;
  status: ReadinessFixtureStatus;
  evidence: string;
  required_action: string;
}

interface ReadinessFixture {
  schema_version: string;
  release_scope?: string;
  machine_use?: string;
  edge_band_selection_required?: boolean;
  design_review_ready: boolean;
  physical_cutting_authorized: boolean;
  missing_evidence_count: number;
  software_evidence: ReadinessFixtureRequirement[];
  workshop_evidence: ReadinessFixtureRequirement[];
}

interface ReviewPackageStatusFixture {
  schema_version: string;
  package_status: string;
  cam_status: "VALIDATION_GENERATED" | "BLOCKED";
  blocker_codes: string[];
  operations_included: boolean;
  setup_sheets_included: boolean;
  nesting_included: boolean;
  validation_backplot_included: boolean;
  validation_program_included: boolean;
  physical_cutting_authorized: boolean;
  required_action: string;
}

const softwareReadinessIdentities = [
  ["AUTHORITATIVE_CAD", "Authoritative CAD geometry"],
  ["DFM_SCREEN", "Manufacturing feasibility screen"],
  ["SEMANTIC_OPERATIONS", "Semantic machining operations"],
  ["SETUP_SHEETS", "Setup sheets"],
  ["VALIDATION_BACKPLOT", "Independent review backplot"],
  ["NON_CUTTING_PROGRAM", "Non-cutting controller validation"],
] as const;

const workshopReadinessIdentities = [
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

const edgeBandReadinessIdentity = [
  "EDGE_BAND_SYSTEM",
  "Adhesive-free mechanical edge protection and cut-size compensation",
] as const;

function readinessFixtureRequirement(
  identity: readonly [string, string],
  status: ReadinessFixtureStatus,
): ReadinessFixtureRequirement {
  return {
    code: identity[0],
    title: identity[1],
    status,
    evidence: status === "VERIFIED"
      ? "Checksum-bound evidence generated by the server."
      : "No checksum-bound evidence is present in this generation job.",
    required_action: status === "VERIFIED"
      ? "None for design review."
      : "Bind the required evidence outside this design-review flow.",
  };
}

function designReviewReadinessFixture({
  edgeBand = false,
  legacy = false,
}: {
  edgeBand?: boolean;
  legacy?: boolean;
} = {}): ReadinessFixture {
  const softwareEvidence = softwareReadinessIdentities.map((identity) => (
    readinessFixtureRequirement(identity, "VERIFIED")
  ));
  const workshopIdentities = edgeBand
    ? [...workshopReadinessIdentities, edgeBandReadinessIdentity]
    : workshopReadinessIdentities;
  const workshopEvidence = workshopIdentities.map((identity) => (
    readinessFixtureRequirement(identity, "EXTERNAL_EVIDENCE_REQUIRED")
  ));
  const common = {
    schema_version: legacy
      ? "custombuild.workshop-readiness.v1"
      : "custombuild.workshop-readiness.v2",
    design_review_ready: true,
    physical_cutting_authorized: false,
    missing_evidence_count: workshopEvidence.length,
    software_evidence: softwareEvidence,
    workshop_evidence: workshopEvidence,
  };
  return legacy
    ? common
    : {
        ...common,
        release_scope: "design_review",
        machine_use: "validation_only",
        edge_band_selection_required: edgeBand,
      };
}

const designReviewReadiness = designReviewReadinessFixture();

function blockedCamPackageStatusFixture(
  blockerCode = "TWO_SIDED_REGISTRATION_MISSING",
): ReviewPackageStatusFixture {
  const requiredAction = blockerCode === "STOCK_PROFILE_MISSING"
    ? (
        "Select and server-bind an exact stock profile for every part material, version, "
        + "thickness, blank size and quantity; do not infer sheet size, stock identity or "
        + "machine capacity."
      )
    : blockerCode === "DFM-GRAIN-001"
      ? (
          "Bind an exact, structured X or Y stock-grain axis for every directional material "
          + "stock profile; opaque evidence or acknowledgement cannot resolve this blocker."
        )
      : blockerCode === "DADO_RETENTION_EVIDENCE_MISSING"
        ? (
            "Bind current certifier-signed, checksum-addressed mechanical retention evidence "
            + "to every load-bearing carcass DADO application, including exact geometry, compiler, "
            + "hardware quantity, material/thickness and shear/withdrawal capacity; a review "
            + "acknowledgement, adhesive or geometric bearing check cannot replace that evidence."
          )
        : blockerCode === "BACK_PANEL_RETENTION_EVIDENCE_MISSING"
          ? (
              "Use only the canonical inset back whose four boundary grooves and multi-direction "
              + "closing sequence prove mechanical capture, or bind independently authenticated "
              + "back-panel retention evidence when that application class is implemented."
            )
        : (
            "Bind an externally specified two-sided registration and fixture plan; "
            + "do not infer WCS, pins, fixtures or registration coordinates."
          );
  return {
    schema_version: "custombuild.design-review-package-status.v1",
    package_status: "READY_FOR_DESIGN_REVIEW",
    cam_status: "BLOCKED",
    blocker_codes: [blockerCode],
    operations_included: false,
    setup_sheets_included: false,
    nesting_included: false,
    validation_backplot_included: false,
    validation_program_included: false,
    physical_cutting_authorized: false,
    required_action: requiredAction,
  };
}

function generatedCamPackageStatusFixture(): ReviewPackageStatusFixture {
  return {
    schema_version: "custombuild.design-review-package-status.v1",
    package_status: "READY_FOR_DESIGN_REVIEW",
    cam_status: "VALIDATION_GENERATED",
    blocker_codes: [],
    operations_included: true,
    setup_sheets_included: true,
    nesting_included: true,
    validation_backplot_included: true,
    validation_program_included: true,
    physical_cutting_authorized: false,
    required_action: "None for design review; physical workshop evidence remains required.",
  };
}

function blockedCamReadinessFixture(dfmBlocked = false): ReadinessFixture {
  const readiness = designReviewReadinessFixture();
  readiness.software_evidence = softwareReadinessIdentities.map((identity, index) => (
    readinessFixtureRequirement(identity, index < (dfmBlocked ? 1 : 2) ? "VERIFIED" : "MISSING")
  ));
  readiness.design_review_ready = false;
  readiness.missing_evidence_count = readiness.workshop_evidence.length + (dfmBlocked ? 5 : 4);
  return readiness;
}

function stockBlockedReadinessWithVerifiedDfm(): ReadinessFixture {
  const readiness = blockedCamReadinessFixture(true);
  const dfmIndex = readiness.software_evidence.findIndex((item) => item.code === "DFM_SCREEN");
  readiness.software_evidence[dfmIndex] = readinessFixtureRequirement(
    softwareReadinessIdentities[dfmIndex]!,
    "VERIFIED",
  );
  readiness.missing_evidence_count -= 1;
  return readiness;
}

function reviewJobArtifactResult(camBlocked: boolean): Record<string, unknown> {
  const artifactKinds = [
    "manufacturing_intent",
    "supplier_handoff",
    "dfm_report",
    "stock_selection",
    "generation_plan",
    ...(camBlocked ? [] : ["operations", "validation_backplot", "setup_sheet_001"]),
    "design_glb",
    "workshop_readiness",
    "design_review_package_status",
  ];
  const contentType = (kind: string) => (
    kind === "validation_backplot" || kind.startsWith("setup_sheet_")
      ? "image/svg+xml"
      : kind === "design_glb"
        ? "model/gltf-binary"
        : "application/json"
  );
  return {
    bundle_object_key: "tenant/review/production.zip",
    bundle_sha256: "b".repeat(64),
    bundle_size_bytes: 2_400_000,
    manifest_object_key: "tenant/review/manifest.json",
    manifest_sha256: "d".repeat(64),
    manifest_size_bytes: 4_096,
    evidence_artifacts: artifactKinds.map((kind, index) => ({
      kind,
      object_key: `tenant/review/evidence/${index}-${kind}`,
      sha256: (index % 10).toString().repeat(64),
      size_bytes: 1_024,
      content_type: contentType(kind),
    })),
  };
}

const succeededJob: JobRead = {
  ...queuedJob,
  status: "succeeded",
  attempts: 1,
  started_at: "2026-08-01T08:01:05Z",
  finished_at: "2026-08-01T08:02:00Z",
  result_json: {
    ...reviewJobArtifactResult(false),
    authoritative_geometry: true,
    machine_program_mode: "VALIDATION_DRY_RUN",
    production_machine_program: false,
    design_review_package_status: generatedCamPackageStatusFixture(),
    workshop_readiness: designReviewReadiness,
  },
};

const immutableDesignReviewRelease: ReleaseRead = {
  release_id: "22222222-2222-4222-8222-222222222222",
  release_number: "R1",
  status: "released",
  manifest_sha256: "d".repeat(64),
  release_kind: "design_review",
  machine_use: "validation_only",
};

const blockedCamJob: JobRead = {
  ...succeededJob,
  result_json: {
    ...reviewJobArtifactResult(true),
    manifest_sha256: "d".repeat(64),
    authoritative_geometry: true,
    dfm_status: "PASS",
    machine_program_mode: "CAM_BLOCKED",
    production_machine_program: false,
    design_review_package_status: blockedCamPackageStatusFixture(),
    workshop_readiness: blockedCamReadinessFixture(),
    nesting_utilization_ppm: null,
    used_sheet_count: 0,
    nesting_layouts: [],
  },
};

const stockBlockedCamJob: JobRead = {
  ...blockedCamJob,
  result_json: {
    ...blockedCamJob.result_json,
    dfm_status: "BLOCK",
    design_review_package_status: blockedCamPackageStatusFixture("STOCK_PROFILE_MISSING"),
    workshop_readiness: blockedCamReadinessFixture(true),
  },
};

const grainBlockedCamJob: JobRead = {
  ...blockedCamJob,
  result_json: {
    ...blockedCamJob.result_json,
    dfm_status: "BLOCK",
    design_review_package_status: blockedCamPackageStatusFixture("DFM-GRAIN-001"),
    workshop_readiness: blockedCamReadinessFixture(true),
  },
};

const retentionBlockedCamJob: JobRead = {
  ...blockedCamJob,
  result_json: {
    ...blockedCamJob.result_json,
    design_review_package_status: blockedCamPackageStatusFixture(
      "DADO_RETENTION_EVIDENCE_MISSING",
    ),
  },
};

const backPanelRetentionBlockedCamJob: JobRead = {
  ...blockedCamJob,
  result_json: {
    ...blockedCamJob.result_json,
    design_review_package_status: blockedCamPackageStatusFixture(
      "BACK_PANEL_RETENTION_EVIDENCE_MISSING",
    ),
  },
};

const safeMachineProgramFields = {
  machine_program_mode: "VALIDATION_DRY_RUN",
  production_machine_program: false,
} as const;

function jobWithReadiness(
  workshopReadiness: unknown,
  machineProgramFields: Record<string, unknown> = safeMachineProgramFields,
): JobRead {
  return {
    ...succeededJob,
    result_json: {
      manifest_sha256: "d".repeat(64),
      ...machineProgramFields,
      design_review_package_status: generatedCamPackageStatusFixture(),
      workshop_readiness: workshopReadiness,
    },
  };
}

const bundle: ArtifactRead = {
  id: "artifact-bundle",
  kind: "production_bundle",
  sha256: "d".repeat(64),
  size_bytes: 2_400_000,
  content_type: "application/zip",
  download_url: "https://artifacts.example.test/underlag.zip?signature=fresh",
  download_path: "/v1/artifacts/artifact-bundle/download?signature=signed",
};

const completeArtifacts: ArtifactRead[] = [
  bundle,
  { ...bundle, id: "manifest", kind: "manifest", content_type: "application/json" },
  {
    ...bundle,
    id: "manufacturing-intent",
    kind: "manufacturing_intent",
    content_type: "application/json",
  },
  {
    ...bundle,
    id: "supplier-handoff",
    kind: "supplier_handoff",
    content_type: "application/json",
  },
  { ...bundle, id: "dfm", kind: "dfm_report", content_type: "application/json" },
  { ...bundle, id: "stock-selection", kind: "stock_selection", content_type: "application/json" },
  { ...bundle, id: "generation-plan", kind: "generation_plan", content_type: "application/json" },
  { ...bundle, id: "operations", kind: "operations", content_type: "application/json" },
  { ...bundle, id: "backplot", kind: "validation_backplot", content_type: "image/svg+xml" },
  { ...bundle, id: "setup", kind: "setup_sheet_001", content_type: "image/svg+xml" },
  { ...bundle, id: "glb", kind: "design_glb", content_type: "model/gltf-binary" },
  { ...bundle, id: "readiness", kind: "workshop_readiness", content_type: "application/json" },
  { ...bundle, id: "review-status", kind: "design_review_package_status", content_type: "application/json" },
];

const blockedReviewArtifacts: ArtifactRead[] = [
  bundle,
  { ...bundle, id: "manifest", kind: "manifest", content_type: "application/json" },
  {
    ...bundle,
    id: "manufacturing-intent",
    kind: "manufacturing_intent",
    content_type: "application/json",
  },
  {
    ...bundle,
    id: "supplier-handoff",
    kind: "supplier_handoff",
    content_type: "application/json",
  },
  { ...bundle, id: "dfm", kind: "dfm_report", content_type: "application/json" },
  { ...bundle, id: "stock-selection", kind: "stock_selection", content_type: "application/json" },
  { ...bundle, id: "generation-plan", kind: "generation_plan", content_type: "application/json" },
  { ...bundle, id: "glb", kind: "design_glb", content_type: "model/gltf-binary" },
  { ...bundle, id: "readiness", kind: "workshop_readiness", content_type: "application/json" },
  {
    ...bundle,
    id: "review-status",
    kind: "design_review_package_status",
    content_type: "application/json",
  },
];

const completeArtifactKinds = completeArtifacts.map((artifact) => artifact.kind);
const blockedArtifactKinds = blockedReviewArtifacts.map((artifact) => artifact.kind);

function apiClient(state?: Partial<ProductionStateRead>): ProductionApi {
  return {
    configured: true,
    listProjects: vi.fn(async () => [project]),
    getProductionState: vi.fn(async () => ({
      project_id: project.id,
      version: null,
      approvals: [],
      latest_job: null,
      release: null,
      ...state,
    })),
    ensureProject: vi.fn(async () => project),
    createVersion: vi.fn(async () => version("draft")),
    validateVersion: vi.fn(async () => version("design_validated")),
    approveVersion: vi.fn(async () => version("design_validated")),
    generateVersion: vi.fn(async () => queuedJob),
    releaseVersion: vi.fn(async () => immutableDesignReviewRelease),
    getJob: vi.fn(async () => succeededJob),
    listArtifacts: vi.fn(async () => completeArtifacts),
    listExternalEvidence: vi.fn(async () => []),
    uploadExternalEvidence: vi.fn(),
    downloadJointRetentionEvidence: vi.fn(async () => new Blob(["signed evidence"], {
      type: "application/json",
    })),
    setJointRetentionEvidence: vi.fn(),
    downloadArtifact: vi.fn(async () => new Blob(["verified bundle"], {
      type: "application/zip",
    })),
  };
}

function warning(ruleId: string, title: string, summary: string): RuleEvaluation {
  return {
    rule_id: ruleId,
    rule_version: "1.0.0",
    status: "WARNING",
    title,
    summary,
    calculation: "screening",
    assumptions: [],
    affected_part_ids: [],
  };
}

function stocklessBlocker(ruleId: "DFM-MACHINE-001" | "DFM-STOCK-001"): RuleEvaluation {
  return {
    ...warning(ruleId, "Lager- och maskinprofil", "Profilen räcker inte för delen."),
    status: "BLOCK",
    affected_part_ids: ["side-left"],
    suggestion: {
      action: "create_stockless_review_package",
      label: "Skapa lagerobundet granskningsunderlag",
      value: true,
      explanation: "Behåll verkliga mått och blockera nesting och CAM.",
    },
  };
}

function designWith(evaluations: RuleEvaluation[], status: ResolvedDesign["status"]): ResolvedDesign {
  return {
    ...resolveDesign(DEFAULT_DESIGN_SPEC),
    source: "server-preview",
    status,
    rule_evaluations: evaluations,
  };
}

function designWithCertificationRequest(boundDesignHash?: string): ResolvedDesign {
  const design = designWith([], "PASS");
  const request: NonNullable<ResolvedDesign["retention_certification_request"]> = {
    schema_version: "custombuild.joint-retention-certification-request.v2",
    signed_evidence_schema_version: "custombuild.joint-retention-signed-evidence.v2",
    application_class: "load_bearing_carcass_dado",
    joint_geometry_fingerprint_schema: "custombuild.joint-retention-application-geometry.v1",
    source_design_hash: design.design_hash,
    joint_geometry_sha256: "b".repeat(64),
    engine_version: "bookcase-engine-6.0.0",
    template_version: "bookcase-template-5.0.0",
    eligible_for_current_binding: true,
    blocking_issue: null,
    excluded_applications: [{
      application_class: "captive_inset_back_groove",
      joint_count: 4,
      retention_basis: "canonical_four_boundary_geometric_capture",
      capture_proven: true,
    }],
    required_materials: [{
      material_id: "birch-plywood",
      material_version: "screening-2026.1",
      actual_thickness_um: 17_800,
    }],
    required_load_cases: [
      { mode: "shear", rated_design_load_n: 785 },
      { mode: "withdrawal", rated_design_load_n: 250 },
    ],
    minimum_safety_factor_permille: 2_000,
  };
  return {
    ...design,
    ...(boundDesignHash ? { design_hash: boundDesignHash } : {}),
    retention_certification_request: request,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  if (typeof window !== "undefined") {
    window.localStorage.clear();
    window.sessionStorage.clear();
  }
});

interface InvalidReadinessCase {
  name: string;
  create?: () => ReadinessFixture;
  mutate: (payload: ReadinessFixture) => void;
}

const invalidV2ReadinessCases: InvalidReadinessCase[] = [
  {
    name: "an empty software array",
    mutate: (payload) => { payload.software_evidence = []; },
  },
  {
    name: "a partial workshop array",
    mutate: (payload) => { payload.workshop_evidence.pop(); },
  },
  {
    name: "a duplicated requirement",
    mutate: (payload) => {
      payload.software_evidence[1] = { ...payload.software_evidence[0]! };
    },
  },
  {
    name: "an unknown requirement code",
    mutate: (payload) => { payload.workshop_evidence[0]!.code = "UNKNOWN_REQUIREMENT"; },
  },
  {
    name: "noncanonical requirement order",
    mutate: (payload) => {
      [payload.software_evidence[0], payload.software_evidence[1]] = [
        payload.software_evidence[1]!,
        payload.software_evidence[0]!,
      ];
    },
  },
  {
    name: "a noncanonical title",
    mutate: (payload) => { payload.workshop_evidence[0]!.title = "Anchor evidence"; },
  },
  {
    name: "a blank evidence string",
    mutate: (payload) => { payload.software_evidence[0]!.evidence = "  "; },
  },
  {
    name: "an extra requirement key",
    mutate: (payload) => {
      Object.assign(payload.software_evidence[0]!, { untrusted: "value" });
    },
  },
  {
    name: "a missing requirement key",
    mutate: (payload) => {
      delete (payload.workshop_evidence[0] as Partial<ReadinessFixtureRequirement>).required_action;
    },
  },
  {
    name: "an external-only status in software evidence",
    mutate: (payload) => {
      payload.software_evidence[0]!.status = "EXTERNAL_EVIDENCE_REQUIRED";
    },
  },
  {
    name: "a software-only status in workshop evidence",
    mutate: (payload) => { payload.workshop_evidence[0]!.status = "MISSING"; },
  },
  {
    name: "an unknown status",
    mutate: (payload) => {
      (payload.software_evidence[0] as { status: string }).status = "UNKNOWN";
    },
  },
  {
    name: "a mismatched missing count",
    mutate: (payload) => { payload.missing_evidence_count += 1; },
  },
  {
    name: "a boolean missing count",
    mutate: (payload) => {
      (payload as unknown as { missing_evidence_count: unknown }).missing_evidence_count = true;
    },
  },
  {
    name: "a fractional missing count",
    mutate: (payload) => { payload.missing_evidence_count = 1.5; },
  },
  {
    name: "a negative missing count",
    mutate: (payload) => { payload.missing_evidence_count = -1; },
  },
  {
    name: "a mismatched ready flag",
    mutate: (payload) => { payload.design_review_ready = false; },
  },
  {
    name: "an unsafe release scope",
    mutate: (payload) => { payload.release_scope = "physical_release"; },
  },
  {
    name: "an unsafe machine use",
    mutate: (payload) => { payload.machine_use = "production"; },
  },
  {
    name: "a missing v2 scope key",
    mutate: (payload) => { delete payload.release_scope; },
  },
  {
    name: "an unknown top-level key",
    mutate: (payload) => { Object.assign(payload, { untrusted_scope: "design_review" }); },
  },
  {
    name: "a non-boolean edge-band flag",
    mutate: (payload) => {
      (payload as unknown as { edge_band_selection_required: unknown })
        .edge_band_selection_required = 0;
    },
  },
  {
    name: "an edge flag without the canonical edge requirement",
    mutate: (payload) => { payload.edge_band_selection_required = true; },
  },
  {
    name: "an edge requirement while the edge flag is false",
    create: () => designReviewReadinessFixture({ edgeBand: true }),
    mutate: (payload) => { payload.edge_band_selection_required = false; },
  },
  {
    name: "physical cutting authorization",
    mutate: (payload) => { payload.physical_cutting_authorized = true; },
  },
];

interface InvalidMachineProgramCase {
  name: string;
  fields: Record<string, unknown>;
}

const invalidV2MachineProgramCases: InvalidMachineProgramCase[] = [
  {
    name: "both job-level program fields are missing",
    fields: {},
  },
  {
    name: "only machine_program_mode is present",
    fields: { machine_program_mode: "VALIDATION_DRY_RUN" },
  },
  {
    name: "only production_machine_program is present",
    fields: { production_machine_program: false },
  },
  {
    name: "machine_program_mode is not validation-only",
    fields: {
      machine_program_mode: "PRODUCTION",
      production_machine_program: false,
    },
  },
  {
    name: "production_machine_program is true",
    fields: {
      machine_program_mode: "VALIDATION_DRY_RUN",
      production_machine_program: true,
    },
  },
];

const invalidLegacyMachineProgramCases: InvalidMachineProgramCase[] = [
  {
    name: "only machine_program_mode is present",
    fields: { machine_program_mode: "VALIDATION_DRY_RUN" },
  },
  {
    name: "only production_machine_program is present",
    fields: { production_machine_program: false },
  },
  {
    name: "the pair describes production use",
    fields: {
      machine_program_mode: "PRODUCTION",
      production_machine_program: true,
    },
  },
  {
    name: "machine_program_mode has the wrong type",
    fields: {
      machine_program_mode: 1,
      production_machine_program: false,
    },
  },
  {
    name: "production_machine_program has the wrong type",
    fields: {
      machine_program_mode: "VALIDATION_DRY_RUN",
      production_machine_program: 0,
    },
  },
];

describe("designReviewPackageStatusFromJob", () => {
  it("accepts the canonical generated-validation status", () => {
    const status = generatedCamPackageStatusFixture();
    const job: JobRead = {
      ...succeededJob,
      result_json: {
        ...succeededJob.result_json,
        design_review_package_status: status,
      },
    };

    expect(designReviewPackageStatusFromJob(job)).toEqual(status);
  });

  it("accepts a canonical blocked-CAM status without mutating server evidence", () => {
    const job = structuredClone(blockedCamJob);
    const untouched = structuredClone(job.result_json);

    expect(designReviewPackageStatusFromJob(job)).toEqual(blockedCamPackageStatusFixture());
    expect(job.result_json).toEqual(untouched);
  });

  it("accepts the exact stockless review status", () => {
    expect(designReviewPackageStatusFromJob(stockBlockedCamJob)).toEqual(
      blockedCamPackageStatusFixture("STOCK_PROFILE_MISSING"),
    );
  });

  it("accepts the exact server-owned grain blocker status", () => {
    expect(designReviewPackageStatusFromJob(grainBlockedCamJob)).toEqual(
      blockedCamPackageStatusFixture("DFM-GRAIN-001"),
    );
  });

  it("accepts the exact server-owned DADO retention blocker status", () => {
    expect(designReviewPackageStatusFromJob(retentionBlockedCamJob)).toEqual(
      blockedCamPackageStatusFixture("DADO_RETENTION_EVIDENCE_MISSING"),
    );
  });

  it("accepts the exact server-owned back-panel retention blocker status", () => {
    expect(designReviewPackageStatusFromJob(backPanelRetentionBlockedCamJob)).toEqual(
      blockedCamPackageStatusFixture("BACK_PANEL_RETENTION_EVIDENCE_MISSING"),
    );
  });

  it.each([
    {
      name: "physical cutting is claimed",
      mutate: (status: ReviewPackageStatusFixture) => {
        status.physical_cutting_authorized = true;
      },
    },
    {
      name: "a manufacturing artifact is claimed",
      mutate: (status: ReviewPackageStatusFixture) => {
        status.operations_included = true;
      },
    },
    {
      name: "the blocker list is empty",
      mutate: (status: ReviewPackageStatusFixture) => {
        status.blocker_codes = [];
      },
    },
    {
      name: "the blocker list is duplicated",
      mutate: (status: ReviewPackageStatusFixture) => {
        status.blocker_codes = [
          "TWO_SIDED_REGISTRATION_MISSING",
          "TWO_SIDED_REGISTRATION_MISSING",
        ];
      },
    },
    {
      name: "the required action drifts from the blocker",
      mutate: (status: ReviewPackageStatusFixture) => {
        status.required_action = "Use an invented large-format profile.";
      },
    },
    {
      name: "the blocker is unsupported",
      mutate: (status: ReviewPackageStatusFixture) => {
        status.blocker_codes = ["UNSUPPORTED_BLOCKER"];
      },
    },
    {
      name: "an unknown top-level claim is added",
      mutate: (status: ReviewPackageStatusFixture) => {
        Object.assign(status, { machine_ready: true });
      },
    },
  ])("rejects blocked status when $name", ({ mutate }) => {
    const status = blockedCamPackageStatusFixture();
    mutate(status);
    const job: JobRead = {
      ...blockedCamJob,
      result_json: {
        ...blockedCamJob.result_json,
        design_review_package_status: status,
      },
    };

    expect(designReviewPackageStatusFromJob(job)).toBeUndefined();
  });
});

describe("CNC-shop review artifact presentation", () => {
  it.each([
    {
      kind: "supplier_handoff",
      role: "Leverantörsöverlämning",
      use: "Överlämning till CNC-verkstad för granskning – inte arbetsorder eller körbar CNC-kod",
      fileName: `custombuild-project-${project.id}-cnc-shop-handoff-rev-12.json`,
    },
    {
      kind: "manufacturing_intent",
      role: "Maskinneutralt bearbetningsunderlag",
      use: "Maskinneutralt bearbetningsunderlag för CNC-verkstadens granskning – inte körbar CNC-kod",
      fileName: `custombuild-project-${project.id}-manufacturing-intent-rev-12.json`,
    },
  ])("presents $kind as review material rather than executable machine code", ({
    kind,
    role,
    use,
    fileName,
  }) => {
    const artifact = {
      kind,
      content_type: "application/json",
    } satisfies Pick<ArtifactRead, "kind" | "content_type">;

    expect(artifactRoleLabel(kind)).toBe(role);
    expect(artifactReviewUseLabel(kind)).toBe(use);
    expect(artifactFileExtension(artifact)).toBe("json");
    expect(artifactDownloadFileName(artifact, project.id, 12)).toBe(fileName);
  });

  it("mirrors the server's project-unique artifact filename boundary fail-closed", () => {
    expect(artifactDownloadFileName(
      { kind: "setup_sheet_012", content_type: "image/svg+xml" },
      project.id,
      3,
    )).toBe(`custombuild-project-${project.id}-setup-sheet-012-rev-3.svg`);
    expect(() => artifactDownloadFileName(
      { kind: "production_bundle", content_type: "application/json" },
      project.id,
      3,
    )).toThrow(/medieformat/i);
    expect(() => artifactDownloadFileName(
      { kind: "production_bundle", content_type: "application/zip" },
      "project-1",
      3,
    )).toThrow(/projektidentitet/i);
    expect(() => artifactDownloadFileName(
      { kind: "production_bundle", content_type: "application/zip" },
      project.id,
      0,
    )).toThrow(/revision/i);
  });
});

describe("review package artifact inventory", () => {
  it("derives the exact persisted inventory from bounded successful-job metadata", () => {
    expect(reviewArtifactKindsFromJob(succeededJob)).toEqual(completeArtifactKinds);
    expect(reviewArtifactKindsFromJob(blockedCamJob)).toEqual(blockedArtifactKinds);
  });

  it.each([
    ["unknown kind", (result: Record<string, unknown>) => {
      const rows = result.evidence_artifacts as Array<Record<string, unknown>>;
      rows[0] = { ...rows[0], kind: "unexpected_machine_program" };
    }],
    ["case alias", (result: Record<string, unknown>) => {
      const rows = result.evidence_artifacts as Array<Record<string, unknown>>;
      rows[0] = { ...rows[0], kind: "Manufacturing_Intent" };
    }],
    ["duplicate kind", (result: Record<string, unknown>) => {
      const rows = result.evidence_artifacts as Array<Record<string, unknown>>;
      rows.push({ ...rows[0], object_key: "tenant/review/evidence/duplicate" });
    }],
    ["wrong content type", (result: Record<string, unknown>) => {
      const rows = result.evidence_artifacts as Array<Record<string, unknown>>;
      rows[0] = { ...rows[0], content_type: "application/octet-stream" };
    }],
    ["extra field", (result: Record<string, unknown>) => {
      const rows = result.evidence_artifacts as Array<Record<string, unknown>>;
      rows[0] = { ...rows[0], provider_url: "https://storage.invalid/private" };
    }],
    ["uppercase digest", (result: Record<string, unknown>) => {
      const rows = result.evidence_artifacts as Array<Record<string, unknown>>;
      rows[0] = { ...rows[0], sha256: "A".repeat(64) };
    }],
    ["oversized readiness document", (result: Record<string, unknown>) => {
      const rows = result.evidence_artifacts as Array<Record<string, unknown>>;
      const index = rows.findIndex((row) => row.kind === "workshop_readiness");
      rows[index] = { ...rows[index], size_bytes: 64 * 1024 + 1 };
    }],
  ])("rejects %s in job-owned artifact metadata", (_label, mutate) => {
    const job = structuredClone(succeededJob);
    const result = job.result_json as Record<string, unknown>;
    mutate(result);

    expect(reviewArtifactKindsFromJob(job)).toBeUndefined();
  });

  it("rejects an unexpected generated artifact even when every required kind remains", () => {
    expect(reviewPackageArtifactInventoryIsTruthful(
      [...completeArtifacts, { ...bundle, id: "rogue", kind: "machine_program" }],
      generatedCamPackageStatusFixture(),
      true,
      completeArtifactKinds,
    )).toBe(false);
  });

  it.each([
    "manufacturing_intent",
    "supplier_handoff",
  ])("allows and requires the CNC-shop review artifact %s", (kind) => {
    expect(blockedCamEvidenceKindIsForbidden(kind)).toBe(false);
    expect(reviewPackageArtifactInventoryIsTruthful(
      blockedReviewArtifacts,
      blockedCamPackageStatusFixture(),
      true,
      blockedArtifactKinds,
    )).toBe(true);
    expect(reviewPackageArtifactInventoryIsTruthful(
      blockedReviewArtifacts.filter((artifact) => artifact.kind !== kind),
      blockedCamPackageStatusFixture(),
      true,
      blockedArtifactKinds,
    )).toBe(false);
    expect(reviewPackageArtifactInventoryIsTruthful(
      completeArtifacts.filter((artifact) => artifact.kind !== kind),
      generatedCamPackageStatusFixture(),
      true,
      completeArtifactKinds,
    )).toBe(false);
  });

  it("allows and requires the checksum-bound stock-selection snapshot", () => {
    expect(blockedCamEvidenceKindIsForbidden("stock_selection")).toBe(false);
    expect(reviewPackageArtifactInventoryIsTruthful(
      blockedReviewArtifacts,
      blockedCamPackageStatusFixture(),
      true,
      blockedArtifactKinds,
    )).toBe(true);
    expect(reviewPackageArtifactInventoryIsTruthful(
      blockedReviewArtifacts.filter((artifact) => artifact.kind !== "stock_selection"),
      blockedCamPackageStatusFixture(),
      true,
      blockedArtifactKinds,
    )).toBe(false);
  });

  it("allows and requires the checksum-bound generation plan", () => {
    expect(blockedCamEvidenceKindIsForbidden("generation_plan")).toBe(false);
    expect(reviewPackageArtifactInventoryIsTruthful(
      blockedReviewArtifacts,
      blockedCamPackageStatusFixture(),
      true,
      blockedArtifactKinds,
    )).toBe(true);
    expect(reviewPackageArtifactInventoryIsTruthful(
      blockedReviewArtifacts.filter((artifact) => artifact.kind !== "generation_plan"),
      blockedCamPackageStatusFixture(),
      true,
      blockedArtifactKinds,
    )).toBe(false);
  });

  it.each([
    "operations",
    "validation_backplot",
    "setup_sheet_001",
    "cam/rogue",
    "cam_rogue",
    "nesting/rogue",
    "nesting_rogue",
    "placement/rogue",
    "placement_map",
    "stock/rogue",
    "stock_profile",
    "machine-validation/rogue",
    "machine_validation_rogue",
    "rogue.NGC",
    "non_cutting_validation_program",
    "tool_list",
    "stock_purchase_schedule",
    "quality_measurement_plan",
    "gcode",
    "toolpath",
    "machine_program",
    "operations_plan",
    "setup_plan",
    "tooling_plan",
    "Design_Glb",
    "assembly_manual",
  ])("rejects blocked-CAM evidence kind %s", (kind) => {
    expect(blockedCamEvidenceKindIsForbidden(kind)).toBe(true);
    expect(reviewPackageArtifactInventoryIsTruthful(
      [...blockedReviewArtifacts, { ...bundle, id: `rogue-${kind}`, kind }],
      blockedCamPackageStatusFixture(),
      true,
      blockedArtifactKinds,
    )).toBe(false);
  });

  it("allows a machine-independent document in a blocked review package", () => {
    expect(reviewPackageArtifactInventoryIsTruthful(
      [
        ...blockedReviewArtifacts,
        { ...bundle, id: "assembly-readiness", kind: "assembly_readiness" },
      ],
      blockedCamPackageStatusFixture(),
      true,
      [...blockedArtifactKinds, "assembly_readiness"],
    )).toBe(true);
  });

  it("requires the validation program claim for generated CAM", () => {
    const status = generatedCamPackageStatusFixture();
    status.validation_program_included = false;
    expect(reviewPackageArtifactInventoryIsTruthful(
      completeArtifacts,
      status,
      true,
      completeArtifactKinds,
    )).toBe(false);
  });

  it("rejects an unparseable claimed status instead of downgrading to legacy", () => {
    expect(reviewPackageArtifactInventoryIsTruthful(
      completeArtifacts.filter((artifact) => artifact.kind !== "design_review_package_status"),
      undefined,
      true,
      completeArtifactKinds,
    )).toBe(false);
  });

  it("rejects a statusless v4 inventory", () => {
    expect(reviewPackageArtifactInventoryIsTruthful(
      completeArtifacts.filter((artifact) => artifact.kind !== "design_review_package_status"),
      undefined,
      false,
      completeArtifactKinds,
    )).toBe(false);
  });

  it("rejects a case-aliased status artifact from a statusless legacy list", () => {
    expect(reviewPackageArtifactInventoryIsTruthful(
      [
        ...completeArtifacts.filter((artifact) => artifact.kind !== "design_review_package_status"),
        { ...bundle, id: "status-alias", kind: "Design_Review_Package_Status" },
      ],
      undefined,
      false,
      completeArtifactKinds,
    )).toBe(false);
  });
});

describe("workshopReadinessFromJob", () => {
  it("accepts canonical blocked-CAM job fields while keeping review and cutting false", () => {
    const normalized = workshopReadinessFromJob(blockedCamJob);

    expect(normalized).toEqual(blockedCamReadinessFixture());
    expect(normalized?.design_review_ready).toBe(false);
    expect(normalized?.physical_cutting_authorized).toBe(false);
    expect(normalized?.software_evidence.map(({ status }) => status)).toEqual([
      "VERIFIED",
      "VERIFIED",
      "MISSING",
      "MISSING",
      "MISSING",
      "MISSING",
    ]);
  });

  it.each([false, true])(
    "accepts canonical v2 and preserves edge-band selection required=%s",
    (edgeBand) => {
      const payload = designReviewReadinessFixture({ edgeBand });
      const untouched = structuredClone(payload);
      const job = jobWithReadiness(payload);
      const untouchedResult = structuredClone(job.result_json);

      const normalized = workshopReadinessFromJob(job);

      expect(normalized).toEqual(payload);
      expect(normalized?.schema_version).toBe("custombuild.workshop-readiness.v2");
      expect(normalized?.release_scope).toBe("design_review");
      expect(normalized?.machine_use).toBe("validation_only");
      expect(normalized?.edge_band_selection_required).toBe(edgeBand);
      expect(normalized?.physical_cutting_authorized).toBe(false);
      expect(payload).toEqual(untouched);
      expect(job.result_json).toEqual(untouchedResult);
    },
  );

  it.each(invalidV2MachineProgramCases)(
    "rejects canonical v2 when $name",
    ({ fields }) => {
      const payload = designReviewReadinessFixture();

      expect(workshopReadinessFromJob(jobWithReadiness(payload, fields))).toBeUndefined();
    },
  );

  it("rejects v2 program fields inherited only through the job-result prototype without mutation", () => {
    const payload = designReviewReadinessFixture();
    const programPrototype = {
      machine_program_mode: "VALIDATION_DRY_RUN",
      production_machine_program: false,
    };
    const result = Object.assign(
      Object.create(programPrototype) as Record<string, unknown>,
      {
        manifest_sha256: "d".repeat(64),
        workshop_readiness: payload,
      },
    );
    const job: JobRead = { ...succeededJob, result_json: result };
    const ownKeys = Object.keys(result);
    const untouchedPayload = structuredClone(payload);

    expect(result.machine_program_mode).toBe("VALIDATION_DRY_RUN");
    expect(Object.prototype.hasOwnProperty.call(result, "machine_program_mode")).toBe(false);
    expect(Object.prototype.hasOwnProperty.call(result, "production_machine_program")).toBe(false);

    expect(workshopReadinessFromJob(job)).toBeUndefined();
    expect(Object.keys(result)).toEqual(ownKeys);
    expect(Object.getPrototypeOf(result)).toBe(programPrototype);
    expect(payload).toEqual(untouchedPayload);
  });

  it("accepts only scope-appropriate non-default statuses and their exact derived values", () => {
    const payload = designReviewReadinessFixture();
    payload.software_evidence[0]!.status = "MISSING";
    payload.workshop_evidence[0]!.status = "VERIFIED";
    payload.design_review_ready = false;

    const normalized = workshopReadinessFromJob(jobWithReadiness(payload));

    expect(normalized?.design_review_ready).toBe(false);
    expect(normalized?.missing_evidence_count).toBe(14);
    expect(normalized?.software_evidence[0]?.status).toBe("MISSING");
    expect(normalized?.workshop_evidence[0]?.status).toBe("VERIFIED");
  });

  it.each([false, true])(
    "accepts only complete legacy v1 and normalizes safe v2 scopes with edge-band inferred=%s",
    (edgeBand) => {
      const legacy = designReviewReadinessFixture({ edgeBand, legacy: true });
      const untouched = structuredClone(legacy);
      const job = jobWithReadiness(legacy);
      const untouchedResult = structuredClone(job.result_json);

      const normalized = workshopReadinessFromJob(job);

      expect(normalized).toEqual({
        ...legacy,
        schema_version: "custombuild.workshop-readiness.v2",
        release_scope: "design_review",
        machine_use: "validation_only",
        edge_band_selection_required: edgeBand,
      });
      expect(legacy).toEqual(untouched);
      expect(job.result_json).toEqual(untouchedResult);
    },
  );

  it("accepts historical legacy v1 when both job-level program fields are absent", () => {
    const legacy = designReviewReadinessFixture({ legacy: true });
    const job = jobWithReadiness(legacy, {});
    const untouchedResult = structuredClone(job.result_json);

    const normalized = workshopReadinessFromJob(job);

    expect(normalized?.schema_version).toBe("custombuild.workshop-readiness.v2");
    expect(normalized?.release_scope).toBe("design_review");
    expect(normalized?.machine_use).toBe("validation_only");
    expect(normalized?.physical_cutting_authorized).toBe(false);
    expect(job.result_json).toEqual(untouchedResult);
  });

  it.each(invalidLegacyMachineProgramCases)(
    "rejects legacy v1 when $name",
    ({ fields }) => {
      const legacy = designReviewReadinessFixture({ legacy: true });

      expect(workshopReadinessFromJob(jobWithReadiness(legacy, fields))).toBeUndefined();
    },
  );

  it("rejects incomplete or extended legacy v1 instead of inventing compatibility", () => {
    const incomplete = designReviewReadinessFixture({ legacy: true });
    incomplete.software_evidence.pop();
    const extended = designReviewReadinessFixture({ legacy: true });
    extended.release_scope = "design_review";
    const inventedEdge = designReviewReadinessFixture({ edgeBand: true, legacy: true });
    inventedEdge.workshop_evidence.at(-1)!.code = "INVENTED_EDGE_SYSTEM";

    expect(workshopReadinessFromJob(jobWithReadiness(incomplete))).toBeUndefined();
    expect(workshopReadinessFromJob(jobWithReadiness(extended))).toBeUndefined();
    expect(workshopReadinessFromJob(jobWithReadiness(inventedEdge))).toBeUndefined();
  });

  it.each(invalidV2ReadinessCases)("rejects $name", ({ create, mutate }) => {
    const payload = create?.() ?? designReviewReadinessFixture();
    mutate(payload);

    expect(workshopReadinessFromJob(jobWithReadiness(payload))).toBeUndefined();
  });
});

describe("workshopRequirementPresentation", () => {
  it("gives every accepted external requirement Swedish, actionable guidance", () => {
    for (const [code, serverTitle] of [
      ...workshopReadinessIdentities,
      edgeBandReadinessIdentity,
    ]) {
      const presentation = workshopRequirementPresentation(code);
      expect(presentation.title).not.toBe(serverTitle);
      expect(presentation.title).not.toMatch(/^Okänt externt krav/);
      expect(presentation.owner.length).toBeGreaterThan(3);
      expect(presentation.nextAction.length).toBeGreaterThan(20);
      expect(["furniture", "materials", "workshop", "verification"]).toContain(
        presentation.group,
      );
    }
  });

  it("keeps an unexpected requirement visibly blocking instead of treating it as complete", () => {
    expect(workshopRequirementPresentation("UNEXPECTED_REQUIREMENT")).toEqual({
      title: "Okänt externt krav (UNEXPECTED_REQUIREMENT)",
      group: "verification",
      owner: "Ansvarig granskare",
      nextAction: "Stoppa fysisk tillverkning och utred det okända kravet innan arbetet fortsätter.",
    });
  });
});

describe("approvalExternalEvidenceIds", () => {
  const evidenceId = "33333333-3333-4333-8333-333333333333";
  const canonicalOverride = {
    rule_id: "CB-TIP-001",
    rule_version: "1.3.0",
    reason: "Extern kontroll verifierad för exakt revision.",
    approved_by: "reviewer-1",
    approved_at: "2026-09-03T08:00:00+00:00",
    evidence_status: "verified",
    external_evidence: [{
      evidence_id: evidenceId,
      evidence_type: "wall_anchor",
      rule_id: "CB-TIP-001",
      catalog_id: "ANCHOR-M8",
      catalog_version: "2026.2",
      design_hash: "d".repeat(64),
      sha256: "e".repeat(64),
      size_bytes: 2_048,
      content_type: "application/pdf",
      created_by: "reviewer-1",
      created_at: "2026-09-03T07:55:00+00:00",
      expires_at: null,
    }],
  };

  it("restores IDs from the exact response shape persisted by approve_version", () => {
    expect(approvalExternalEvidenceIds([canonicalOverride])).toEqual([evidenceId]);
  });

  it("retains the intentional request-shaped legacy read path", () => {
    expect(approvalExternalEvidenceIds([{
      rule_id: "CB-TIP-001",
      reason: "Äldre godkännande med verifierad evidens.",
      evidence_ids: [evidenceId],
    }])).toEqual([evidenceId]);
  });

  it("fails closed on partial snapshots, duplicate IDs and mixed response/legacy shapes", () => {
    expect(approvalExternalEvidenceIds([{
      ...canonicalOverride,
      external_evidence: [{
        ...canonicalOverride.external_evidence[0],
        sha256: undefined,
      }],
    }])).toBeUndefined();
    expect(approvalExternalEvidenceIds([{
      ...canonicalOverride,
      external_evidence: [
        canonicalOverride.external_evidence[0],
        canonicalOverride.external_evidence[0],
      ],
    }])).toBeUndefined();
    expect(approvalExternalEvidenceIds([
      canonicalOverride,
      {
        rule_id: "CB-HARDWARE-001",
        reason: "Äldre form får inte blandas med ny form.",
        evidence_ids: [],
      },
    ])).toBeUndefined();
  });
});

describe("retentionEvidenceUploadMetadata", () => {
  function signedRetentionFile(
    payload: unknown = {
      catalogue_entry: {
        system_id: "mechanical-dado-lock",
        system_version: "1.0.0",
      },
      expires_at: "2099-02-01T23:59:59.999Z",
      design_hash: "must-not-be-read-from-the-file",
    },
    name = "signed-retention.json",
    type = "application/json",
  ): File {
    const contents = JSON.stringify(payload);
    const file = new File([contents], name, { type });
    Object.defineProperty(file, "text", { value: async () => contents });
    return file;
  }

  it("extracts only exact catalogue and expiry metadata from a valid JSON file", async () => {
    await expect(retentionEvidenceUploadMetadata(signedRetentionFile(), Date.parse("2026-09-03T00:00:00Z")))
      .resolves.toEqual({
        catalogId: "mechanical-dado-lock",
        catalogVersion: "1.0.0",
        expiresAt: "2099-02-01T23:59:59.999Z",
      });
  });

  it("rejects the wrong suffix, media type, oversize data and malformed signed metadata", async () => {
    await expect(retentionEvidenceUploadMetadata(signedRetentionFile(undefined, "signed-retention.txt")))
      .rejects.toThrow(/\.json-fil/);
    await expect(retentionEvidenceUploadMetadata(signedRetentionFile(undefined, "signed-retention.json", "text/plain")))
      .rejects.toThrow(/application\/json/);

    const oversized = {
      name: "signed-retention.json",
      type: "application/json",
      size: 20 * 1024 * 1024 + 1,
      text: vi.fn(async () => "{}"),
    } as unknown as File;
    await expect(retentionEvidenceUploadMetadata(oversized)).rejects.toThrow(/högst 20 MiB/);
    expect(oversized.text).not.toHaveBeenCalled();

    await expect(retentionEvidenceUploadMetadata(signedRetentionFile({
      catalogue_entry: { system_id: " mechanical-dado-lock ", system_version: "1.0.0" },
      expires_at: "2099-02-01T23:59:59.999Z",
    }))).rejects.toThrow(/system_id/);
    await expect(retentionEvidenceUploadMetadata(signedRetentionFile({
      catalogue_entry: { system_id: "mechanical-dado-lock", system_version: "1.0.0" },
      expires_at: "2020-02-01T23:59:59.999Z",
    }), Date.parse("2026-09-03T00:00:00Z"))).rejects.toThrow(/framtida/);
  });
});

describe("canonicalRetentionCertificationRequestJson", () => {
  it("serializes the untouched server request with recursively sorted compact keys", () => {
    const request = designWithCertificationRequest().retention_certification_request!;
    const serialized = canonicalRetentionCertificationRequestJson(request);
    const parsed = JSON.parse(serialized) as Record<string, unknown>;

    expect(parsed).toEqual(request);
    expect(serialized).not.toMatch(/\n|\s{2}/);
    expect(Object.keys(parsed)).toEqual([...Object.keys(parsed)].sort());
    expect(Object.keys((parsed.required_materials as Array<Record<string, unknown>>)[0]!))
      .toEqual(["actual_thickness_um", "material_id", "material_version"]);
  });
});

describe("ProductionWorkflow", () => {
  it("downloads the exact server-issued certification request without calling an evidence API", async () => {
    const design = designWithCertificationRequest("f".repeat(64));
    const api = apiClient();
    let downloadedName: string | undefined;
    let downloadedBlob: Blob | undefined;
    vi.spyOn(URL, "createObjectURL").mockImplementation((blob) => {
      if (!(blob instanceof Blob)) throw new Error("Expected a JSON Blob download.");
      downloadedBlob = blob;
      return "blob:retention-certification-request";
    });
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      downloadedName = this.download;
    });

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={design}
        onSummaryChange={vi.fn()}
        principal={reviewerPrincipal}
      />,
    );

    const region = await screen.findByRole("region", {
      name: "Certifieringsbegäran för joint-retention",
    });
    await waitFor(() => expect(api.getProductionState).toHaveBeenCalled());
    vi.mocked(api.setJointRetentionEvidence!).mockClear();
    expect(within(region).getByText(/begäran och ett provningsunderlag, inte signerad evidens/i))
      .toBeVisible();
    fireEvent.click(within(region).getByRole("button", {
      name: "Hämta certifieringsbegäran (.json)",
    }));

    expect(downloadedBlob).toBeInstanceOf(Blob);
    expect(downloadedBlob?.type).toBe("application/json");
    expect(downloadedName).toBe(
      `custombuild-retention-certification-request-${design.retention_certification_request!.source_design_hash}.json`,
    );
    expect(api.uploadExternalEvidence).not.toHaveBeenCalled();
    expect(api.setJointRetentionEvidence).not.toHaveBeenCalled();
    expect(screen.getByText(/inte retentionsevidens eller ett godkännande/i)).toBeVisible();
  });

  it("lets a reviewer register an externally certified retention JSON without binding it", async () => {
    const currentDesign = designWith([], "PASS");
    const evidenceId = "22222222-2222-4222-8222-222222222222";
    const uploadedEvidence: ExternalEvidenceRead = {
      id: evidenceId,
      project_id: project.id,
      evidence_type: "joint_retention",
      rule_id: "CB-JOINT-001",
      catalog_id: "mechanical-dado-lock",
      catalog_version: "1.0.0",
      design_hash: currentDesign.design_hash,
      sha256: "a".repeat(64),
      size_bytes: 2_048,
      content_type: "application/json",
      created_by: "reviewer-1",
      expires_at: "2099-02-01T23:59:59.999Z",
      created_at: "2026-09-03T08:00:00Z",
    };
    const api = apiClient();
    vi.mocked(api.uploadExternalEvidence!).mockResolvedValue(uploadedEvidence);
    const contents = JSON.stringify({
      catalogue_entry: {
        system_id: uploadedEvidence.catalog_id,
        system_version: uploadedEvidence.catalog_version,
      },
      expires_at: uploadedEvidence.expires_at,
      design_hash: "untrusted-file-value",
    });
    const file = new File([contents], "certifier-signed-retention.json", {
      type: "application/json",
    });
    Object.defineProperty(file, "text", { value: async () => contents });

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={currentDesign}
        onSummaryChange={vi.fn()}
        principal={reviewerPrincipal}
      />,
    );

    const upload = await screen.findByLabelText("Certifierarsignerad retention-JSON");
    await waitFor(() => expect(upload).toBeEnabled());
    vi.mocked(api.setJointRetentionEvidence!).mockClear();
    fireEvent.change(upload, { target: { files: [file] } });

    await waitFor(() => expect(api.uploadExternalEvidence).toHaveBeenCalledWith(
      project.id,
      {
        document: file,
        evidenceType: "joint_retention",
        ruleId: "CB-JOINT-001",
        catalogId: "mechanical-dado-lock",
        catalogVersion: "1.0.0",
        designHash: currentDesign.design_hash,
        expiresAt: "2099-02-01T23:59:59.999Z",
      },
    ));
    expect(api.setJointRetentionEvidence).not.toHaveBeenCalled();
    expect(await within(screen.getByRole("combobox", { name: "Signerad retentionsevidens" }))
      .findByRole("option", { name: /mechanical-dado-lock/ })).toBeVisible();
    expect(screen.getByText(/Uppladdningen godkände eller band den inte/i)).toBeVisible();
    expect(screen.getByText(/måste komma direkt från en extern certifierare/i)).toBeVisible();
  });

  it("lets a workshop operator download the exact signed evidence bound to the saved revision", async () => {
    const evidenceId = "22222222-2222-4222-8222-222222222222";
    const baseDesignHash = "a".repeat(64);
    const savedVersion: DesignVersionRead = {
      ...version("design_validated"),
      result_json: {
        production_context: productionContextFromSpec(DEFAULT_DESIGN_SPEC),
        retention_trust: {
          base_design_hash: baseDesignHash,
          storage_evidence_id: evidenceId,
        },
      },
    };
    const evidence: ExternalEvidenceRead = {
      id: evidenceId,
      project_id: project.id,
      evidence_type: "joint_retention",
      rule_id: "CB-JOINT-001",
      catalog_id: "mechanical-dado-lock",
      catalog_version: "1.0.0",
      design_hash: baseDesignHash,
      sha256: "a".repeat(64),
      size_bytes: 2_048,
      content_type: "application/json",
      created_by: "reviewer-1",
      expires_at: "2099-02-01T23:59:59.999Z",
      created_at: "2026-09-03T08:00:00Z",
    };
    const api = apiClient({ version: savedVersion });
    vi.mocked(api.listExternalEvidence!).mockResolvedValue([evidence]);
    const verifiedBlob = new Blob(["signed evidence"], { type: "application/json" });
    vi.mocked(api.downloadJointRetentionEvidence!).mockResolvedValue(verifiedBlob);
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:verified-retention");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    let downloadedName: string | undefined;
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(
      function (this: HTMLAnchorElement) {
        downloadedName = this.download;
      },
    );

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
        principal={operatorPrincipal}
      />,
    );

    const region = await screen.findByRole("region", {
      name: "Signerad retention för verkstadsverifiering",
    });
    const download = within(region).getByRole("button", {
      name: "Hämta signerad retention-JSON (originalbytes)",
    });
    await waitFor(() => expect(download).toBeEnabled());
    expect(within(region).getByText(/auktoriserar aldrig fysisk kapning/i)).toBeVisible();
    fireEvent.click(download);

    await waitFor(() => expect(api.downloadJointRetentionEvidence).toHaveBeenCalledWith(
      project.id,
      evidence,
    ));
    expect(downloadedName).toBe(`custombuild-joint-retention-${evidenceId}.json`);
    expect(await screen.findByText(/verifierad byte för byte/i)).toBeVisible();
  });

  it("does not expose signed retention bytes to a designer", async () => {
    const evidenceId = "22222222-2222-4222-8222-222222222222";
    const baseDesignHash = "a".repeat(64);
    const savedVersion: DesignVersionRead = {
      ...version("design_validated"),
      result_json: {
        production_context: productionContextFromSpec(DEFAULT_DESIGN_SPEC),
        retention_trust: {
          base_design_hash: baseDesignHash,
          storage_evidence_id: evidenceId,
        },
      },
    };
    const evidence: ExternalEvidenceRead = {
      id: evidenceId,
      project_id: project.id,
      evidence_type: "joint_retention",
      rule_id: "CB-JOINT-001",
      catalog_id: "mechanical-dado-lock",
      catalog_version: "1.0.0",
      design_hash: baseDesignHash,
      sha256: "a".repeat(64),
      size_bytes: 2_048,
      content_type: "application/json",
      created_by: "reviewer-1",
      expires_at: "2099-02-01T23:59:59.999Z",
      created_at: "2026-09-03T08:00:00Z",
    };
    const api = apiClient({ version: savedVersion });
    vi.mocked(api.listExternalEvidence!).mockResolvedValue([evidence]);

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
        principal={designerPrincipal}
      />,
    );

    const region = await screen.findByRole("region", {
      name: "Signerad retention för verkstadsverifiering",
    });
    const download = within(region).getByRole("button", {
      name: "Hämta signerad retention-JSON (originalbytes)",
    });
    expect(download).toBeDisabled();
    expect(within(region).getByText(/Endast reviewer, operator, production, admin eller owner/i))
      .toBeVisible();
    fireEvent.click(download);
    expect(api.downloadJointRetentionEvidence).not.toHaveBeenCalled();
  });

  it("stops a strict reviewer after approval and requires a generator handoff", async () => {
    const api = apiClient({ version: version("design_validated") });
    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
        principal={reviewerPrincipal}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Godkänn designkontroll" }));
    await waitFor(() => expect(api.approveVersion).toHaveBeenCalledOnce());
    expect(api.generateVersion).not.toHaveBeenCalled();
    expect(screen.getByRole("combobox", { name: "Signerad retentionsevidens" })).toBeDisabled();

    const generate = await screen.findByRole("button", { name: "Skapa underlag" });
    expect(generate).toBeDisabled();
    expect(screen.getByText(/En designer, admin eller owner måste nu skapa underlaget/)).toBeVisible();
  });

  it("restores approved evidence for a strict designer without exposing reviewer controls", async () => {
    const evidenceId = "33333333-3333-4333-8333-333333333333";
    const currentDesign = designWith([
      warning("CB-TIP-001", "Vältrisk", "Förankringen ska kontrolleras."),
    ], "WARNING");
    const api = apiClient({
      version: version("design_validated"),
      approvals: [{
        approval_type: "design",
        approved_by: "reviewer-1",
        reason: "Kontrollerad med serverevidens.",
        generation_job_id: null,
        production_context_hash: null,
        manifest_sha256: null,
        overrides_json: [{
          rule_id: "CB-TIP-001",
          rule_version: "1.0.0",
          reason: "Verifierad för revisionen.",
          approved_by: "reviewer-1",
          approved_at: "2026-08-01T08:00:00+00:00",
          evidence_status: "verified",
          external_evidence: [{
            evidence_id: evidenceId,
            evidence_type: "wall_anchor",
            rule_id: "CB-TIP-001",
            catalog_id: "ANCHOR-M8",
            catalog_version: "2026.2",
            design_hash: currentDesign.design_hash,
            sha256: "b".repeat(64),
            size_bytes: 2_048,
            content_type: "application/pdf",
            created_by: "reviewer-1",
            created_at: "2026-08-01T08:00:00+00:00",
            expires_at: null,
          }],
        }],
        created_at: "2026-08-01T08:00:00Z",
        updated_at: "2026-08-01T08:00:00Z",
      }],
    });
    vi.mocked(api.listExternalEvidence!).mockResolvedValue([{
      id: evidenceId,
      project_id: project.id,
      evidence_type: "wall_anchor",
      rule_id: "CB-TIP-001",
      catalog_id: "ANCHOR-M8",
      catalog_version: "2026.2",
      design_hash: currentDesign.design_hash,
      sha256: "b".repeat(64),
      size_bytes: 2_048,
      content_type: "application/pdf",
      created_by: "reviewer-1",
      expires_at: null,
      created_at: "2026-08-01T08:00:00Z",
    }]);

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={currentDesign}
        onSummaryChange={vi.fn()}
        principal={designerPrincipal}
      />,
    );

    const generate = await screen.findByRole("button", { name: "Skapa underlag" });
    await waitFor(() => expect(generate).toBeEnabled());
    expect(screen.getByRole("combobox", { name: "Signerad retentionsevidens" })).toBeEnabled();
    fireEvent.click(generate);

    await waitFor(() => expect(api.generateVersion).toHaveBeenCalledWith(
      project.id,
      1,
      expect.objectContaining({ external_evidence_ids: [evidenceId] }),
    ));
    expect(api.approveVersion).not.toHaveBeenCalled();
  });

  it("generates with the exact frozen structured workshop context", async () => {
    const spec = structuredWorkshopSpec();
    const resolved = { ...resolveDesign(spec), source: "server-preview" as const };
    const structuredVersion: DesignVersionRead = {
      ...version("design_validated"),
      design_hash: resolved.design_hash,
      result_json: { production_context: productionContextFromSpec(spec) },
    };
    const api = apiClient({
      version: structuredVersion,
      approvals: [{
        approval_type: "design",
        approved_by: "reviewer-1",
        reason: "Designkontroll godkänd.",
        generation_job_id: null,
        production_context_hash: null,
        manifest_sha256: null,
        overrides_json: [],
        created_at: "2026-08-01T08:00:00Z",
        updated_at: "2026-08-01T08:00:00Z",
      }],
    });

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={spec}
        design={resolved}
        onSummaryChange={vi.fn()}
        onApplyDesignChange={vi.fn()}
        principal={designerPrincipal}
      />,
    );

    const frozenContext = await screen.findByRole("region", { name: "Fryst verkstadskontext" });
    await waitFor(() => expect(frozenContext).toHaveTextContent("supplier-birch-18"));
    fireEvent.click(screen.getByRole("button", { name: "Skapa underlag" }));
    await waitFor(() => expect(api.generateVersion).toHaveBeenCalledWith(
      project.id,
      1,
      expect.objectContaining(productionContextFromSpec(spec)),
    ));
  });

  it("blocks generation when a newer workshop draft removes a required supplier ID", async () => {
    const spec = structuredWorkshopSpec();
    const resolved = { ...resolveDesign(spec), source: "server-preview" as const };
    const structuredVersion: DesignVersionRead = {
      ...version("design_validated"),
      design_hash: resolved.design_hash,
      result_json: { production_context: productionContextFromSpec(spec) },
    };
    const api = apiClient({
      version: structuredVersion,
      approvals: [{
        approval_type: "design",
        approved_by: "reviewer-1",
        reason: "Designkontroll godkänd.",
        generation_job_id: null,
        production_context_hash: null,
        manifest_sha256: null,
        overrides_json: [],
        created_at: "2026-08-01T08:00:00Z",
        updated_at: "2026-08-01T08:00:00Z",
      }],
    });
    const draftStateChange = vi.fn();

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={spec}
        design={resolved}
        onSummaryChange={vi.fn()}
        onApplyDesignChange={vi.fn()}
        onWorkshopContextDraftStateChange={draftStateChange}
        principal={designerPrincipal}
      />,
    );

    const generate = await screen.findByRole("button", { name: "Skapa underlag" });
    await waitFor(() => expect(generate).toBeEnabled());
    fireEvent.change(screen.getAllByRole("textbox", {
      name: "Leverantörens profil-ID (deklarerat)",
    })[0]!, { target: { value: "" } });

    await waitFor(() => expect(generate).toBeDisabled());
    expect(draftStateChange).toHaveBeenLastCalledWith(expect.objectContaining({
      dirty: true,
      valid: false,
    }));
    expect(screen.getByText(/osparade eller ofullständiga uppgifter/i)).toBeVisible();
    fireEvent.click(generate);
    expect(api.generateVersion).not.toHaveBeenCalled();
  });

  it("blocks revision save while a new workshop binding is only partially filled", async () => {
    const api = apiClient();
    const draftStateChange = vi.fn();
    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
        onApplyDesignChange={vi.fn()}
        onWorkshopContextDraftStateChange={draftStateChange}
        principal={designerPrincipal}
      />,
    );

    const save = await screen.findByRole("button", {
      name: /Spara (och kontrollera|för lagerobunden granskning)/,
    });
    fireEvent.click(screen.getByRole("button", {
      name: "Bind leverantörsdeklarerad verkstadsprofil",
    }));
    fireEvent.change(screen.getAllByRole("textbox", {
      name: "Leverantörens profil-ID (deklarerat)",
    })[0]!, { target: { value: "partial-profile" } });

    await waitFor(() => expect(save).toBeDisabled());
    expect(draftStateChange).toHaveBeenLastCalledWith(expect.objectContaining({
      dirty: true,
      valid: false,
    }));
    expect(screen.getAllByText(/Återgå till lagerobundet paket/i)).not.toHaveLength(0);
    fireEvent.click(save);
    expect(api.createVersion).not.toHaveBeenCalled();
  });

  it("uses the selected large-format catalog profile unchanged for a larger design", async () => {
    const largeMachine = MACHINES[1]!;
    const spec: DesignSpec = {
      ...DEFAULT_DESIGN_SPEC,
      width_mm: 4_200,
      stock_width_mm: largeMachine.workAreaMm.x,
      stock_height_mm: largeMachine.workAreaMm.y,
      back_stock_width_mm: largeMachine.workAreaMm.x,
      back_stock_height_mm: largeMachine.workAreaMm.y,
      machine_profile_id: largeMachine.id,
    };
    const resolved: ResolvedDesign = {
      ...resolveDesign(spec),
      source: "server-preview",
      status: "PASS",
      rule_evaluations: [],
    };
    const machineVersion: DesignVersionRead = {
      ...version("design_validated"),
      design_hash: resolved.design_hash,
      result_json: { production_context: productionContextFromSpec(spec) },
    };
    const api = apiClient({
      version: machineVersion,
      approvals: [{
        approval_type: "design",
        approved_by: "reviewer-1",
        reason: "Designkontroll godkänd.",
        generation_job_id: null,
        production_context_hash: null,
        manifest_sha256: null,
        overrides_json: [],
        created_at: "2026-09-03T08:00:00Z",
        updated_at: "2026-09-03T08:00:00Z",
      }],
    });

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={spec}
        design={resolved}
        onSummaryChange={vi.fn()}
        onApplyDesignChange={vi.fn()}
        principal={designerPrincipal}
      />,
    );

    expect(screen.getByRole("radio", {
      name: (accessibleName) => accessibleName.includes(largeMachine.name),
    })).toBeChecked();
    expect(screen.getByText(/X 5100 × Y 2600 × Z 150 mm/)).toBeVisible();
    const generate = await screen.findByRole("button", { name: "Skapa underlag" });
    await waitFor(() => expect(generate).toBeEnabled());
    fireEvent.click(generate);

    await waitFor(() => expect(api.generateVersion).toHaveBeenCalledWith(
      project.id,
      1,
      expect.objectContaining({
        machine_profile_id: largeMachine.id,
        stock_width_mm: largeMachine.workAreaMm.x,
        stock_height_mm: largeMachine.workAreaMm.y,
      }),
    ));
  });

  it("fails closed when persisted approval overrides are malformed", async () => {
    const api = apiClient({
      version: version("design_validated"),
      approvals: [{
        approval_type: "design",
        approved_by: "reviewer-1",
        reason: "Felaktigt lagrad override.",
        generation_job_id: null,
        production_context_hash: null,
        manifest_sha256: null,
        overrides_json: [{ rule_id: "CB-TIP-001", evidence_ids: ["not-a-uuid"] }],
        created_at: "2026-08-01T08:00:00Z",
        updated_at: "2026-08-01T08:00:00Z",
      }],
    });
    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
        principal={designerPrincipal}
      />,
    );

    const generate = await screen.findByRole("button", { name: "Skapa underlag" });
    expect(generate).toBeDisabled();
    expect(screen.getByText(/evidenslista är ogiltig/i)).toBeVisible();
    fireEvent.click(generate);
    expect(api.generateVersion).not.toHaveBeenCalled();
  });

  it("binds review-authorized retention to the revision and keeps it out of general generation evidence", async () => {
    const retentionId = "22222222-2222-4222-8222-222222222222";
    const wallAnchorId = "33333333-3333-4333-8333-333333333333";
    const design = designWith([
      warning("CB-TIP-001", "Vältrisk", "Förankringen ska kontrolleras."),
    ], "WARNING");
    const evidence = [
      {
        id: retentionId,
        project_id: project.id,
        evidence_type: "joint_retention",
        rule_id: "CB-JOINT-001",
        catalog_id: "mechanical-dado-lock",
        catalog_version: "1.0.0",
        design_hash: design.design_hash,
        sha256: "a".repeat(64),
        size_bytes: 1_024,
        content_type: "application/json",
        created_by: "reviewer-1",
        expires_at: "2099-02-01T23:59:59.999Z",
        created_at: "2026-08-11T12:00:00Z",
      },
      {
        id: wallAnchorId,
        project_id: project.id,
        evidence_type: "wall_anchor",
        rule_id: "CB-TIP-001",
        catalog_id: "ANCHOR-M8",
        catalog_version: "2026.2",
        design_hash: design.design_hash,
        sha256: "b".repeat(64),
        size_bytes: 2_048,
        content_type: "application/pdf",
        created_by: "reviewer-1",
        expires_at: null,
        created_at: "2026-08-11T12:01:00Z",
      },
    ] satisfies ExternalEvidenceRead[];
    const api = apiClient();
    vi.mocked(api.listExternalEvidence!).mockResolvedValue(evidence);
    const requestPreview = vi.fn();

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={design}
        onSummaryChange={vi.fn()}
        onRequestServerPreviewRetry={requestPreview}
        principal={ownerPrincipal}
        pollIntervalMs={60_000}
      />,
    );

    const retentionSelect = await screen.findByRole("combobox", {
      name: "Signerad retentionsevidens",
    });
    expect(await within(retentionSelect).findByRole("option", { name: /mechanical-dado-lock/ })).toBeVisible();
    fireEvent.change(retentionSelect, { target: { value: retentionId } });
    expect(api.setJointRetentionEvidence).toHaveBeenCalledWith(project.id, retentionId);
    expect(requestPreview).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Spara och kontrollera" }));
    await waitFor(() => expect(api.createVersion).toHaveBeenCalledWith(
      project.id,
      DEFAULT_DESIGN_SPEC,
      design.design_hash,
      0,
      "shelving",
      retentionId,
    ));

    const generalSelect = await screen.findByRole("combobox", {
      name: "Kompletterande evidens: Väggförankring",
    });
    fireEvent.change(generalSelect, { target: { value: wallAnchorId } });
    fireEvent.click(screen.getByRole("checkbox", {
      name: "Jag har läst och kontrollerat varningarna ovan.",
    }));
    fireEvent.click(screen.getByRole("button", { name: "Godkänn designkontroll" }));

    await waitFor(() => expect(api.approveVersion).toHaveBeenCalled());
    expect(api.generateVersion).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole("button", { name: "Skapa underlag" }));

    await waitFor(() => expect(api.generateVersion).toHaveBeenCalledWith(
      project.id,
      1,
      expect.objectContaining({ external_evidence_ids: [wallAnchorId] }),
    ));
    const generationRequest = vi.mocked(api.generateVersion).mock.calls.at(-1)?.[2];
    expect(generationRequest?.external_evidence_ids).not.toContain(retentionId);
    expect(api.approveVersion).toHaveBeenCalledWith(
      project.id,
      1,
      expect.objectContaining({
        warning_overrides: [expect.objectContaining({
          rule_id: "CB-TIP-001",
          evidence_ids: [wallAnchorId],
        })],
      }),
    );
  });

  it("shows warnings as information and requires one acknowledgement before creating", async () => {
    const api = apiClient();
    const design = designWith([
      warning("CB-TIP-001", "Vältrisk", "Förankringen ska kontrolleras."),
      warning("CB-HARDWARE-001", "Beslag", "Beslagen ska kontrolleras."),
      warning("DFM-GRAIN-001", "Fiberriktning", "Fiberriktningen ska kontrolleras."),
    ], "WARNING");

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={design}
        onSummaryChange={vi.fn()}
        principal={ownerPrincipal}
        pollIntervalMs={60_000}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Spara och kontrollera" }));
    const approve = await screen.findByRole("button", { name: "Godkänn designkontroll" });
    const confirmation = screen.getByRole("checkbox", {
      name: "Jag har läst och kontrollerat varningarna ovan.",
    });

    expect(screen.getByText("Vältrisk")).toBeVisible();
    expect(screen.getByText("Beslag")).toBeVisible();
    expect(screen.getByText("Fiberriktning")).toBeVisible();
    expect(screen.queryByLabelText("Dokument")).not.toBeInTheDocument();
    expect(screen.queryByText("Bevis saknas")).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Status för verifieringen" })).toHaveTextContent("Behöver beslut");
    expect(approve).toBeDisabled();

    fireEvent.click(confirmation);
    expect(approve).toBeEnabled();
    fireEvent.click(approve);

    await waitFor(() => expect(api.approveVersion).toHaveBeenCalledWith(project.id, 1, {
      approval_type: "design",
      reason: "Designkontroll godkänd efter granskning av varningar: CB-HARDWARE-001, CB-TIP-001, DFM-GRAIN-001.",
      generation_job_id: null,
      warning_overrides: [
        {
          rule_id: "CB-HARDWARE-001",
          reason: "Varningen har granskats och godkänts i designkontrollen.",
          evidence_ids: [],
        },
        {
          rule_id: "CB-TIP-001",
          reason: "Varningen har granskats och godkänts i designkontrollen.",
          evidence_ids: [],
        },
        {
          rule_id: "DFM-GRAIN-001",
          reason: "Varningen har granskats och godkänts i designkontrollen.",
          evidence_ids: [],
        },
      ],
    }));
    expect(api.generateVersion).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole("button", { name: "Skapa underlag" }));
    await waitFor(() => expect(api.generateVersion).toHaveBeenCalledWith(project.id, 1, expect.objectContaining({
      include_freecad_project: false,
      external_evidence_ids: [],
    })));
  });

  it("keeps a structural BLOCK disabled and never approves or generates", async () => {
    const api = apiClient({ version: version("design_validated") });
    const blocked = designWith([{
      ...warning("CB-SUPPORT-001", "Lodrät lastväg", "En bärande sida saknas."),
      status: "BLOCK",
    }], "BLOCK");

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={blocked}
        onSummaryChange={vi.fn()}
        principal={reviewerPrincipal}
      />,
    );

    const create = await screen.findByRole("button", { name: "Godkänn designkontroll" });
    expect(create).toBeDisabled();
    expect(screen.getByRole("alert", { name: "Krav som måste lösas" })).toBeVisible();
    expect(screen.getByRole("region", { name: "Status för verifieringen" })).toHaveTextContent("Måste lösas");
    fireEvent.click(create);
    expect(api.approveVersion).not.toHaveBeenCalled();
    expect(api.generateVersion).not.toHaveBeenCalled();
  });

  it("never saves a revision whose free part edits would be dropped by the server", async () => {
    const api = apiClient();
    const customizedSpec = {
      ...DEFAULT_DESIGN_SPEC,
      part_overrides: { "side-left": { width_mm: 2_000 } },
    };

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={customizedSpec}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
        principal={reviewerPrincipal}
      />,
    );

    const save = await screen.findByRole("button", { name: "Spara och kontrollera" });
    const alert = screen.getByRole("alert");
    expect(save).toBeDisabled();
    expect(alert).toHaveTextContent(
      /Deländringarna ingår inte i serverunderlaget/,
    );
    expect(alert).toHaveTextContent(/bygg samma ändring med de parametriska möbelvalen/i);
    fireEvent.click(save);
    expect(api.createVersion).not.toHaveBeenCalled();
  });

  it("never approves or generates from a revision when current part edits are local-only", async () => {
    const api = apiClient({ version: version("design_validated") });
    const customizedSpec = {
      ...DEFAULT_DESIGN_SPEC,
      removed_part_ids: ["shelf-1-bay-1"],
    };

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={customizedSpec}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
        principal={reviewerPrincipal}
      />,
    );

    const create = await screen.findByRole("button", { name: "Godkänn designkontroll" });
    expect(create).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent(
      /inte serverauktoritativa/,
    );
    fireEvent.click(create);
    expect(api.approveVersion).not.toHaveBeenCalled();
    expect(api.generateVersion).not.toHaveBeenCalled();
  });

  it("permits only the exact local stock and machine blockers for stockless review", () => {
    const stock = stocklessBlocker("DFM-STOCK-001");
    const machine = stocklessBlocker("DFM-MACHINE-001");
    const structural = {
      ...stock,
      rule_id: "CB-SUPPORT-001",
    };

    expect(permitsStocklessDesignReview([stock, machine])).toBe(true);
    expect(permitsStocklessDesignReview([stock])).toBe(true);
    expect(permitsStocklessDesignReview([machine])).toBe(false);
    expect(permitsStocklessDesignReview([stock, stock])).toBe(false);
    expect(permitsStocklessDesignReview([stock, machine, machine])).toBe(false);
    expect(permitsStocklessDesignReview([{ ...stock, affected_part_ids: [] }])).toBe(false);
    expect(permitsStocklessDesignReview([{
      ...stock,
      suggestion: { ...stock.suggestion!, action: "manual_review" },
    }])).toBe(false);
    expect(permitsStocklessDesignReview([stock, structural])).toBe(false);
  });

  it("saves, approves and generates an unchanged stockless review request", async () => {
    const api = apiClient();
    const applyDesignChange = vi.fn();
    const stocklessDesign = designWith([
      stocklessBlocker("DFM-MACHINE-001"),
      stocklessBlocker("DFM-STOCK-001"),
    ], "BLOCK");

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={stocklessDesign}
        onSummaryChange={vi.fn()}
        onApplyDesignChange={applyDesignChange}
        principal={ownerPrincipal}
      />,
    );

    expect(screen.getByRole("alert", { name: "Krav som blockerar CAM" })).toBeVisible();
    expect(screen.getByText("Blockerar CAM · 2 krav")).toBeVisible();
    fireEvent.click(await screen.findByRole("button", {
      name: "Spara för lagerobunden granskning",
    }));
    await waitFor(() => expect(api.validateVersion).toHaveBeenCalledWith(project.id, 1));

    const approve = await screen.findByRole("button", { name: "Godkänn designkontroll" });
    expect(approve).toBeEnabled();
    fireEvent.click(approve);

    await waitFor(() => expect(api.approveVersion).toHaveBeenCalledWith(project.id, 1, {
      approval_type: "design",
      reason: "Designkontroll godkänd för ett lagerobundet granskningspaket. Lagerprofil, nesting och CAM är uttryckligen inte godkända.",
      generation_job_id: null,
      warning_overrides: [],
    }));
    expect(api.generateVersion).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole("button", { name: "Skapa underlag" }));
    await waitFor(() => expect(api.generateVersion).toHaveBeenCalledWith(project.id, 1, expect.objectContaining({
      stock_width_mm: DEFAULT_DESIGN_SPEC.stock_width_mm,
      stock_height_mm: DEFAULT_DESIGN_SPEC.stock_height_mm,
      back_stock_width_mm: DEFAULT_DESIGN_SPEC.back_stock_width_mm,
      back_stock_height_mm: DEFAULT_DESIGN_SPEC.back_stock_height_mm,
      machine_profile_id: DEFAULT_DESIGN_SPEC.machine_profile_id,
    })));
    expect(applyDesignChange).not.toHaveBeenCalled();
    expect(productionSuggestionPatch(stocklessBlocker("DFM-STOCK-001"))).toBeUndefined();
  });

  it("retries a missing stock profile as a stockless review without mutating the design", async () => {
    const failedStockJob: JobRead = {
      ...queuedJob,
      status: "failed",
      attempts: 1,
      error: "ProductionBlockedError: production bundle blocked by DFM: STOCK_PROFILE_MISSING",
      started_at: "2026-08-01T08:01:05Z",
      finished_at: "2026-08-01T08:02:00Z",
    };
    const api = apiClient({
      version: version("design_validated"),
      approvals: [{
        approval_type: "design",
        approved_by: "reviewer-1",
        reason: "Designkontroll godkänd.",
        generation_job_id: null,
        production_context_hash: null,
        manifest_sha256: null,
        overrides_json: [],
        created_at: "2026-08-01T08:00:00Z",
        updated_at: "2026-08-01T08:00:00Z",
      }],
      latest_job: failedStockJob,
    });
    const applyDesignChange = vi.fn();

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
        onApplyDesignChange={applyDesignChange}
        principal={designerPrincipal}
      />,
    );

    const retry = await screen.findByRole("button", {
      name: "Skapa lagerobundet granskningspaket",
    });
    expect(screen.queryByRole("button", { name: "Försök skapa underlag igen" })).not.toBeInTheDocument();

    fireEvent.click(retry);

    await waitFor(() => expect(api.generateVersion).toHaveBeenCalledWith(
      project.id,
      1,
      expect.objectContaining({
        stock_width_mm: DEFAULT_DESIGN_SPEC.stock_width_mm,
        stock_height_mm: DEFAULT_DESIGN_SPEC.stock_height_mm,
        back_stock_width_mm: DEFAULT_DESIGN_SPEC.back_stock_width_mm,
        back_stock_height_mm: DEFAULT_DESIGN_SPEC.back_stock_height_mm,
        machine_profile_id: DEFAULT_DESIGN_SPEC.machine_profile_id,
      }),
    ));
    expect(applyDesignChange).not.toHaveBeenCalled();
    expect(api.approveVersion).not.toHaveBeenCalled();
    expect(document.body).not.toHaveTextContent("5000");
    expect(screen.getByRole("radio", {
      name: (accessibleName) => accessibleName.includes(MACHINES[0]!.name),
    })).toBeChecked();
    expect(screen.getByRole("radio", {
      name: (accessibleName) => accessibleName.includes(MACHINES[1]!.name),
    })).not.toBeChecked();
  });

  it("does not offer stock-profile recovery for an unrelated generation error", async () => {
    const api = apiClient({
      version: version("design_validated"),
      approvals: [{
        approval_type: "design",
        approved_by: "reviewer-1",
        reason: "Designkontroll godkänd.",
        generation_job_id: null,
        production_context_hash: null,
        manifest_sha256: null,
        overrides_json: [],
        created_at: "2026-08-01T08:00:00Z",
        updated_at: "2026-08-01T08:00:00Z",
      }],
      latest_job: {
        ...queuedJob,
        status: "failed",
        attempts: 1,
        error: "Worker process exited before completion.",
        started_at: "2026-08-01T08:01:05Z",
        finished_at: "2026-08-01T08:02:00Z",
      },
    });

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
        onApplyDesignChange={vi.fn()}
      />,
    );

    expect(await screen.findByRole("button", { name: "Försök skapa underlag igen" })).toBeVisible();
    expect(screen.queryByRole("button", {
      name: "Skapa lagerobundet granskningspaket",
    })).not.toBeInTheDocument();
  });

  it("does not treat a longer error identifier as STOCK_PROFILE_MISSING", async () => {
    const api = apiClient({
      version: version("design_validated"),
      approvals: [{
        approval_type: "design",
        approved_by: "reviewer-1",
        reason: "Designkontroll godkänd.",
        generation_job_id: null,
        production_context_hash: null,
        manifest_sha256: null,
        overrides_json: [],
        created_at: "2026-08-01T08:00:00Z",
        updated_at: "2026-08-01T08:00:00Z",
      }],
      latest_job: {
        ...queuedJob,
        status: "failed",
        attempts: 1,
        error: "Worker metadata error: STOCK_PROFILE_MISSING_METADATA",
        started_at: "2026-08-01T08:01:05Z",
        finished_at: "2026-08-01T08:02:00Z",
      },
    });

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
        onApplyDesignChange={vi.fn()}
      />,
    );

    expect(await screen.findByText("Worker metadata error: STOCK_PROFILE_MISSING_METADATA")).toBeVisible();
    expect(screen.getByRole("button", { name: "Försök skapa underlag igen" })).toBeVisible();
    expect(screen.queryByRole("button", {
      name: "Skapa lagerobundet granskningspaket",
    })).not.toBeInTheDocument();
  });

  it("replaces failed-poll feedback by retrying the unchanged stockless request", async () => {
    const failedStockJob: JobRead = {
      ...queuedJob,
      status: "failed",
      attempts: 1,
      error: "ProductionBlockedError: STOCK_PROFILE_MISSING",
      started_at: "2026-08-01T08:01:05Z",
      finished_at: "2026-08-01T08:02:00Z",
    };
    const api = apiClient({
      version: version("design_validated"),
      approvals: [{
        approval_type: "design",
        approved_by: "reviewer-1",
        reason: "Designkontroll godkänd.",
        generation_job_id: null,
        production_context_hash: null,
        manifest_sha256: null,
        overrides_json: [],
        created_at: "2026-08-01T08:00:00Z",
        updated_at: "2026-08-01T08:00:00Z",
      }],
      latest_job: queuedJob,
    });
    vi.mocked(api.getJob).mockResolvedValue(failedStockJob);
    const applyDesignChange = vi.fn();
    const currentDesign = designWith([], "PASS");
    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={currentDesign}
        onSummaryChange={vi.fn()}
        onApplyDesignChange={applyDesignChange}
        pollIntervalMs={1}
        principal={designerPrincipal}
      />,
    );

    const retry = await screen.findByRole("button", {
      name: "Skapa lagerobundet granskningspaket",
    });
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Kontrollera orsaken och välj Försök skapa underlag igen.",
    );

    fireEvent.click(retry);

    await waitFor(() => expect(api.generateVersion).toHaveBeenCalledWith(
      project.id,
      1,
      expect.objectContaining({
        stock_width_mm: DEFAULT_DESIGN_SPEC.stock_width_mm,
        stock_height_mm: DEFAULT_DESIGN_SPEC.stock_height_mm,
        machine_profile_id: DEFAULT_DESIGN_SPEC.machine_profile_id,
      }),
    ));
    expect(applyDesignChange).not.toHaveBeenCalled();
    expect(screen.queryByText(/anpassats enbart för validering/i)).not.toBeInTheDocument();
  });

  it("offers a truthful design-review ZIP without claiming physical authorization", async () => {
    const api = apiClient({
      version: version("design_validated"),
      approvals: [{
        approval_type: "design",
        approved_by: "reviewer-1",
        reason: "Designkontroll godkänd.",
        generation_job_id: null,
        production_context_hash: null,
        manifest_sha256: null,
        overrides_json: [],
        created_at: "2026-08-01T08:00:00Z",
        updated_at: "2026-08-01T08:00:00Z",
      }],
      latest_job: succeededJob,
    });
    const verifiedBlob = new Blob(["verified bundle"], { type: "application/zip" });
    vi.mocked(api.downloadArtifact).mockResolvedValue(verifiedBlob);
    const createObjectUrl = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:verified-bundle");
    const revokeObjectUrl = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    let suggestedFileName: string | undefined;
    let clickedUrl: string | undefined;
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      suggestedFileName = this.download;
      clickedUrl = this.href;
      expect(revokeObjectUrl).not.toHaveBeenCalled();
    });

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
      />,
    );

    expect(await screen.findByText("Granskningspaketet är klart")).toBeVisible();
    expect(screen.getByRole("heading", {
      level: 3,
      name: "Granska CAM-valideringspaketet",
    })).toBeVisible();
    expect(screen.getByRole("heading", { level: 4, name: "Granskningspaketet är klart" })).toBeVisible();
    expect(screen.getByRole("status", { name: "Status för fysisk tillverkning" })).toHaveTextContent(
      "Ej frisläppt för fysisk kapning",
    );
    expect(screen.getByRole("status", { name: "Status för fysisk tillverkning" })).toHaveTextContent(
      "14 externa verkstadskrav återstår",
    );
    expect(screen.getByText(/endast avsett för designgranskning och validering/i)).toBeVisible();
    const releaseBoundary = screen.getByRole("region", {
      name: "Skillnad mellan designgranskning och fysisk frisläppning",
    });
    expect(within(releaseBoundary).getByText("Klar för revision 1")).toBeVisible();
    expect(within(releaseBoundary).getByText("Ej frisläppt")).toBeVisible();
    expect(within(releaseBoundary).getByText(/inte en arbetsorder eller skärande CNC-kod/i)).toBeVisible();
    const packageIdentity = screen.getByRole("region", { name: "Paketidentitet" });
    expect(within(packageIdentity).getByText("Designgranskning")).toBeVisible();
    expect(within(packageIdentity).getByText("Designgranskning klar")).toBeVisible();
    expect(
      within(packageIdentity).getByText("Svenska PDF:er · tekniska datafält på engelska"),
    ).toBeVisible();
    expect(within(packageIdentity).getByText("2.4 MB")).toBeVisible();
    expect(within(packageIdentity).getByText("d".repeat(64))).toBeVisible();
    expect(within(packageIdentity).getByText("Designgranskningspaket (ZIP)")).toBeVisible();
    expect(within(packageIdentity).getByText("Lagerurval")).toBeVisible();
    expect(within(packageIdentity).getByText("Genereringsplan")).toBeVisible();
    expect(within(packageIdentity).getByText("Readinessbevis")).toBeVisible();
    const customerDocuments = screen.getByRole("region", {
      name: "Kunddokument i granskningspaketet",
    });
    expect(within(customerDocuments).getByText("Monteringsmanual")).toBeVisible();
    expect(within(customerDocuments).getByText(/inte frisläppt monteringsinstruktion/i)).toBeVisible();
    expect(within(customerDocuments).getByText(/START-HERE\.md/)).toBeVisible();
    expect(within(customerDocuments).getByText(/sida A\/B som DXF och SVG/)).toBeVisible();
    expect(within(customerDocuments).getByText(/råmaterialval, generationsplan, operationer och setupblad/)).toBeVisible();
    const serverFiles = screen.getByRole("region", {
      name: "Separat verifierbara serverfiler",
    });
    expect(within(serverFiles).getByRole("button", { name: "Hämta fil – Manifest" })).toBeEnabled();
    expect(within(serverFiles).getAllByText(/JSON · revision 1 · 2.4 MB · Verifieringsbevis/i).length).toBeGreaterThan(0);
    const workshopRequirements = screen.getByRole("region", {
      name: "Återstående externa verkstadskrav",
    });
    expect(within(workshopRequirements).getByRole("region", { name: "Möbelbeslut och infästning" })).toBeVisible();
    expect(within(workshopRequirements).getByRole("region", { name: "Material och limfria förband" })).toBeVisible();
    expect(within(workshopRequirements).getByRole("region", { name: "Verkstad och maskin" })).toBeVisible();
    expect(within(workshopRequirements).getByRole("region", { name: "Provning och godkännande" })).toBeVisible();
    expect(within(workshopRequirements).getByText("Väggtyp och förankringssystem")).toBeVisible();
    expect(within(workshopRequirements).getByText("Namngivet godkännande från CNC-operatör")).toBeVisible();
    expect(within(workshopRequirements).getAllByText(/^Ansvar:/)).toHaveLength(14);
    expect(within(workshopRequirements).queryByText("Wall substrate and anchor system")).not.toBeInTheDocument();

    const handoffGuide = screen.getByRole("region", {
      name: "Vägledning för hur delarna ska tas fram",
    });
    const selfBuild = within(handoffGuide).getByRole("radio", { name: /Jag kapar och bygger själv/ });
    const workshop = within(handoffGuide).getByRole("radio", { name: /En verkstad kapar eller bearbetar/ });
    expect(selfBuild).toBeChecked();
    expect(within(handoffGuide).getByRole("status")).toHaveTextContent("Självbygget är inte frisläppt");
    expect(within(handoffGuide).getByRole("status")).toHaveTextContent(/inte en verifierad arbetsinstruktion för handverktyg/i);
    fireEvent.click(workshop);
    expect(workshop).toBeChecked();
    expect(within(handoffGuide).getByRole("status")).toHaveTextContent("Verkstadsöverlämningen är inte körklar");
    expect(within(handoffGuide).getByText(/Valet ändrar endast vägledningen/)).toBeVisible();
    expect(screen.getByRole("status", { name: "Status för fysisk tillverkning" })).toHaveTextContent(
      "Ej frisläppt för fysisk kapning",
    );
    expect(screen.queryByText("Underlaget är klart")).not.toBeInTheDocument();
    expect(screen.queryByText(/ritningar och tillverkningsfiler/i)).not.toBeInTheDocument();
    const camReview = screen.getByRole("region", {
      name: "CAM-granskning och immutable designrevision",
    });
    expect(within(camReview).getByText(/aldrig en arbetsorder eller skärande CNC-kod/i)).toBeVisible();
    expect(within(camReview).getByRole("button", {
      name: "Godkänn CAM-valideringspaket",
    })).toBeDisabled();
    expect(within(camReview).getByText(/Endast reviewer, admin eller owner/i)).toBeVisible();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Certifierarsignerad retention-JSON")).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Ladda ned granskningspaket (.zip)" }));
    await waitFor(() => expect(api.listArtifacts).toHaveBeenCalledTimes(2));
    expect(anchorClick).toHaveBeenCalledOnce();
    expect(api.downloadArtifact).toHaveBeenCalledWith(bundle);
    expect(createObjectUrl).toHaveBeenCalledWith(verifiedBlob);
    expect(clickedUrl).toBe("blob:verified-bundle");
    expect(suggestedFileName).toBe(
      `custombuild-project-${project.id}-design-review-rev-1.zip`,
    );
    await waitFor(() => expect(revokeObjectUrl).toHaveBeenCalledExactlyOnceWith(
      "blob:verified-bundle",
    ));
    expect(api.approveVersion).not.toHaveBeenCalled();
  });

  it("enforces maker-checker before CAM approval and creates only an immutable design-review release", async () => {
    const designApproval = {
      approval_type: "design" as const,
      approved_by: reviewerPrincipal.user_id,
      reason: "Designkontroll godkänd.",
      generation_job_id: null,
      production_context_hash: null,
      manifest_sha256: null,
      overrides_json: [],
      created_at: "2026-08-01T08:00:00Z",
      updated_at: "2026-08-01T08:00:00Z",
    };
    const api = apiClient({
      version: version("design_validated"),
      approvals: [designApproval],
      latest_job: succeededJob,
    });
    vi.mocked(api.approveVersion).mockResolvedValue(version("approved"));

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
        principal={secondReviewerPrincipal}
      />,
    );

    expect(await screen.findByText("Granskningspaketet är klart")).toBeVisible();
    const camConfirmation = screen.getByRole("checkbox", {
      name: /Jag har granskat exakt jobb, manifest och maskinbunden validering/i,
    });
    const approveCam = screen.getByRole("button", {
      name: "Godkänn CAM-valideringspaket",
    });
    expect(camConfirmation).toBeEnabled();
    expect(approveCam).toBeDisabled();

    fireEvent.click(camConfirmation);
    expect(approveCam).toBeEnabled();
    fireEvent.click(approveCam);

    await waitFor(() => expect(api.approveVersion).toHaveBeenCalledWith(project.id, 1, {
      approval_type: "cam",
      reason: "Exakt genererat CAM-valideringspaket och manifest granskat. Godkännandet gäller endast icke-skärande validering och auktoriserar inte fysisk kapning.",
      generation_job_id: succeededJob.id,
      warning_overrides: [],
    }));
    expect(await screen.findByText(/CAM-granskningen är bunden till aktuellt jobb och manifest/i)).toBeVisible();

    const releaseConfirmation = screen.getByRole("checkbox", {
      name: /Jag bekräftar att revisionslåset endast gäller immutable designgranskning/i,
    });
    const releaseButton = screen.getByRole("button", {
      name: "Lås designgranskningsrevision R1",
    });
    expect(releaseButton).toBeDisabled();
    fireEvent.click(releaseConfirmation);
    expect(releaseButton).toBeEnabled();
    fireEvent.click(releaseButton);

    await waitFor(() => expect(api.releaseVersion).toHaveBeenCalledWith(project.id, 1, "R1"));
    expect(await screen.findByText(/Immutable designgranskningsrevision R1/i)).toBeVisible();
    expect(screen.getByRole("status", { name: "Status för fysisk tillverkning" })).toHaveTextContent(
      "Ej frisläppt för fysisk kapning",
    );
    expect(screen.queryByText(/Fysisk kapning är auktoriserad/i)).not.toBeInTheDocument();
  });

  it("does not let the design reviewer approve the CAM validation package", async () => {
    const api = apiClient({
      version: version("design_validated"),
      approvals: [{
        approval_type: "design",
        approved_by: reviewerPrincipal.user_id,
        reason: "Designkontroll godkänd.",
        generation_job_id: null,
        production_context_hash: null,
        manifest_sha256: null,
        overrides_json: [],
        created_at: "2026-08-01T08:00:00Z",
        updated_at: "2026-08-01T08:00:00Z",
      }],
      latest_job: succeededJob,
    });

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
        principal={reviewerPrincipal}
      />,
    );

    expect(await screen.findByText("Granskningspaketet är klart")).toBeVisible();
    expect(screen.getByRole("checkbox", {
      name: /Jag har granskat exakt jobb, manifest och maskinbunden validering/i,
    })).toBeDisabled();
    expect(screen.getByRole("button", {
      name: "Godkänn CAM-valideringspaket",
    })).toBeDisabled();
    expect(screen.getByText(/Maker–checker kräver att en annan person än designgranskaren/i)).toBeVisible();
    expect(api.approveVersion).not.toHaveBeenCalled();
    expect(api.releaseVersion).not.toHaveBeenCalled();
  });

  it("downloads an individually listed server artifact only after re-verifying its identity", async () => {
    const api = apiClient({
      version: version("design_validated"),
      approvals: [{
        approval_type: "design",
        approved_by: "reviewer-1",
        reason: "Designkontroll godkänd.",
        generation_job_id: null,
        production_context_hash: null,
        manifest_sha256: null,
        overrides_json: [],
        created_at: "2026-08-01T08:00:00Z",
        updated_at: "2026-08-01T08:00:00Z",
      }],
      latest_job: succeededJob,
    });
    const manifest = completeArtifacts.find((artifact) => artifact.kind === "manifest")!;
    const verifiedBlob = new Blob(["{}"], { type: "application/json" });
    vi.mocked(api.downloadArtifact).mockResolvedValue(verifiedBlob);
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:verified-manifest");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    let suggestedFileName: string | undefined;
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(
      function (this: HTMLAnchorElement) {
        suggestedFileName = this.download;
      },
    );

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
      />,
    );

    expect(await screen.findByText("Granskningspaketet är klart")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Hämta fil – Manifest" }));

    await waitFor(() => expect(api.listArtifacts).toHaveBeenCalledTimes(2));
    expect(api.downloadArtifact).toHaveBeenCalledExactlyOnceWith(manifest);
    expect(anchorClick).toHaveBeenCalledOnce();
    expect(suggestedFileName).toBe(
      `custombuild-project-${project.id}-design-review-manifest-rev-1.json`,
    );
    expect(await screen.findByText("Manifest har hämtats för designgranskning.")).toBeVisible();
  });

  it("fails closed when an individually listed artifact changes before download", async () => {
    const api = apiClient({
      version: version("design_validated"),
      approvals: [{
        approval_type: "design",
        approved_by: "reviewer-1",
        reason: "Designkontroll godkänd.",
        generation_job_id: null,
        production_context_hash: null,
        manifest_sha256: null,
        overrides_json: [],
        created_at: "2026-08-01T08:00:00Z",
        updated_at: "2026-08-01T08:00:00Z",
      }],
      latest_job: succeededJob,
    });
    const changedArtifacts = completeArtifacts.map((artifact) => (
      artifact.kind === "manifest" ? { ...artifact, sha256: "e".repeat(64) } : artifact
    ));
    vi.mocked(api.listArtifacts)
      .mockResolvedValueOnce(completeArtifacts)
      .mockResolvedValueOnce(changedArtifacts);
    const createObjectUrl = vi.spyOn(URL, "createObjectURL");
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(
      () => undefined,
    );

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
      />,
    );

    expect(await screen.findByText("Granskningspaketet är klart")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Hämta fil – Manifest" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Filen har ändrats eller är inte längre entydigt bunden till paketet/i,
    );
    expect(api.downloadArtifact).not.toHaveBeenCalled();
    expect(createObjectUrl).not.toHaveBeenCalled();
    expect(anchorClick).not.toHaveBeenCalled();
  });

  it("revokes a pending verified Blob URL exactly once when the workflow unmounts", async () => {
    const api = apiClient({
      version: version("design_validated"),
      approvals: [{
        approval_type: "design",
        approved_by: "reviewer-1",
        reason: "Designkontroll godkänd.",
        generation_job_id: null,
        production_context_hash: null,
        manifest_sha256: null,
        overrides_json: [],
        created_at: "2026-08-01T08:00:00Z",
        updated_at: "2026-08-01T08:00:00Z",
      }],
      latest_job: succeededJob,
    });
    const createObjectUrl = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:pending-bundle");
    const revokeObjectUrl = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    const rendered = render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
      />,
    );
    expect(await screen.findByText("Granskningspaketet är klart")).toBeVisible();

    const clearTimeout = vi.spyOn(window, "clearTimeout");
    fireEvent.click(screen.getByRole("button", { name: "Ladda ned granskningspaket (.zip)" }));
    await waitFor(() => expect(createObjectUrl).toHaveBeenCalledOnce());
    expect(revokeObjectUrl).not.toHaveBeenCalled();

    rendered.unmount();
    expect(clearTimeout).toHaveBeenCalled();
    expect(revokeObjectUrl).toHaveBeenCalledExactlyOnceWith("blob:pending-bundle");
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(revokeObjectUrl).toHaveBeenCalledOnce();
  });

  it("fails closed without creating a browser download when byte verification fails", async () => {
    const api = apiClient({
      version: version("design_validated"),
      approvals: [{
        approval_type: "design",
        approved_by: "reviewer-1",
        reason: "Designkontroll godkänd.",
        generation_job_id: null,
        production_context_hash: null,
        manifest_sha256: null,
        overrides_json: [],
        created_at: "2026-08-01T08:00:00Z",
        updated_at: "2026-08-01T08:00:00Z",
      }],
      latest_job: succeededJob,
    });
    vi.mocked(api.downloadArtifact).mockRejectedValue(new ApiError(
      "Artefaktens innehåll matchar inte den förväntade SHA-256-identiteten.",
      409,
    ));
    const createObjectUrl = vi.spyOn(URL, "createObjectURL");
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(
      () => undefined,
    );
    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
      />,
    );
    expect(await screen.findByText("Granskningspaketet är klart")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Ladda ned granskningspaket (.zip)" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/SHA-256-identiteten/i);
    expect(api.downloadArtifact).toHaveBeenCalledWith(bundle);
    expect(createObjectUrl).not.toHaveBeenCalled();
    expect(anchorClick).not.toHaveBeenCalled();
  });

  it("accepts a complete versioned generated-CAM review package", async () => {
    const versionedJob: JobRead = {
      ...succeededJob,
      result_json: {
        ...succeededJob.result_json,
        design_review_package_status: generatedCamPackageStatusFixture(),
      },
    };
    const api = apiClient({
      version: version("design_validated"),
      approvals: [{
        approval_type: "design",
        approved_by: "reviewer-1",
        reason: "Designkontroll godkänd.",
        generation_job_id: null,
        production_context_hash: null,
        manifest_sha256: null,
        overrides_json: [],
        created_at: "2026-08-01T08:00:00Z",
        updated_at: "2026-08-01T08:00:00Z",
      }],
      latest_job: versionedJob,
    });
    vi.mocked(api.listArtifacts).mockResolvedValue(completeArtifacts);

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
      />,
    );

    expect(await screen.findByText("Granskningspaketet är klart")).toBeVisible();
    const packageIdentity = screen.getByRole("region", { name: "Paketidentitet" });
    expect(within(packageIdentity).getByText("Status för designgranskningspaket")).toBeVisible();
    expect(screen.queryByRole("status", { name: "Status för CAM" })).not.toBeInTheDocument();
  });

  it("offers the core review ZIP while CAM is explicitly blocked and omitted", async () => {
    const api = apiClient({
      version: version("design_validated"),
      approvals: [{
        approval_type: "design",
        approved_by: "reviewer-1",
        reason: "Designkontroll godkänd.",
        generation_job_id: null,
        production_context_hash: null,
        manifest_sha256: null,
        overrides_json: [],
        created_at: "2026-08-01T08:00:00Z",
        updated_at: "2026-08-01T08:00:00Z",
      }],
      latest_job: blockedCamJob,
    });
    vi.mocked(api.listArtifacts).mockResolvedValue(blockedReviewArtifacts);
    const suggestedFileNames: string[] = [];
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(
      function (this: HTMLAnchorElement) {
        suggestedFileNames.push(this.download);
      },
    );

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
      />,
    );

    expect(await screen.findByText("Granskningspaketet är klart")).toBeVisible();
    const camStatus = screen.getByRole("status", { name: "Status för CAM" });
    expect(camStatus).toHaveTextContent("CAM är blockerat");
    expect(camStatus).toHaveTextContent(
      "Nesting, operationer, setupblad, backplot och maskinvalideringskod har därför avsiktligt utelämnats",
    );
    expect(camStatus).toHaveTextContent("Inga WCS-, pinn- eller fixturdata har antagits");
    const physicalStatus = screen.getByRole("status", {
      name: "Status för fysisk tillverkning",
    });
    expect(physicalStatus).toHaveTextContent("Ej frisläppt för fysisk kapning");
    expect(physicalStatus).toHaveTextContent("14 externa verkstadskrav återstår");
    expect(physicalStatus).toHaveTextContent("inga CAM- eller maskinvalideringsfiler");
    const packageIdentity = screen.getByRole("region", { name: "Paketidentitet" });
    expect(within(packageIdentity).getByText("Status för designgranskningspaket")).toBeVisible();
    expect(within(packageIdentity).getByText("Leverantörsöverlämning")).toBeVisible();
    expect(within(packageIdentity).getByText("Maskinneutralt bearbetningsunderlag")).toBeVisible();
    expect(within(packageIdentity).getByRole("button", {
      name: "Hämta fil – Leverantörsöverlämning",
    })).toBeEnabled();
    expect(within(packageIdentity).getByRole("button", {
      name: "Hämta fil – Maskinneutralt bearbetningsunderlag",
    })).toBeEnabled();
    expect(within(packageIdentity).queryByText("Semantiska operationer")).not.toBeInTheDocument();
    expect(within(packageIdentity).queryByText("Valideringsbackplot")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Skapa om underlag" })).not.toBeInTheDocument();

    fireEvent.click(within(packageIdentity).getByRole("button", {
      name: "Hämta fil – Leverantörsöverlämning",
    }));
    await waitFor(() => expect(api.listArtifacts).toHaveBeenCalledTimes(2));
    expect(api.downloadArtifact).toHaveBeenLastCalledWith(
      blockedReviewArtifacts.find((artifact) => artifact.kind === "supplier_handoff"),
    );
    expect(suggestedFileNames).toEqual([
      `custombuild-project-${project.id}-cnc-shop-handoff-rev-1.json`,
    ]);

    fireEvent.click(screen.getByRole("button", { name: "Ladda ned granskningspaket (.zip)" }));
    await waitFor(() => expect(api.listArtifacts).toHaveBeenCalledTimes(3));
    expect(anchorClick).toHaveBeenCalledTimes(2);
    expect(suggestedFileNames).toEqual([
      `custombuild-project-${project.id}-cnc-shop-handoff-rev-1.json`,
      `custombuild-project-${project.id}-design-review-rev-1.zip`,
    ]);
    expect(api.approveVersion).not.toHaveBeenCalled();
  });

  it("offers the stockless review ZIP without inventing stock, nesting or CAM", async () => {
    const api = apiClient({
      version: version("design_validated"),
      approvals: [{
        approval_type: "design",
        approved_by: "reviewer-1",
        reason: "Lagerobunden designkontroll godkänd.",
        generation_job_id: null,
        production_context_hash: null,
        manifest_sha256: null,
        overrides_json: [],
        created_at: "2026-08-01T08:00:00Z",
        updated_at: "2026-08-01T08:00:00Z",
      }],
      latest_job: stockBlockedCamJob,
    });
    vi.mocked(api.listArtifacts).mockResolvedValue(blockedReviewArtifacts);
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(
      () => undefined,
    );

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
      />,
    );

    expect(await screen.findByText("Granskningspaketet är klart")).toBeVisible();
    const camStatus = screen.getByRole("status", { name: "Status för CAM" });
    expect(camStatus).toHaveTextContent("En exakt serverbunden lagerprofil saknas");
    expect(camStatus).toHaveTextContent("har inte ändrats eller ersatts med antagna storformat");
    expect(camStatus).toHaveTextContent(
      "Lagerinköp, nesting, operationer, setupblad, backplot och maskinvalideringskod",
    );
    expect(document.body).not.toHaveTextContent("5000");
    expect(screen.getByRole("radio", {
      name: (accessibleName) => accessibleName.includes(MACHINES[0]!.name),
    })).toBeChecked();
    expect(screen.getByRole("radio", {
      name: (accessibleName) => accessibleName.includes(MACHINES[1]!.name),
    })).not.toBeChecked();

    fireEvent.click(screen.getByRole("button", { name: "Ladda ned granskningspaket (.zip)" }));
    await waitFor(() => expect(api.listArtifacts).toHaveBeenCalledTimes(2));
    expect(anchorClick).toHaveBeenCalledOnce();
    expect(api.approveVersion).not.toHaveBeenCalled();
  });

  it("offers a grain-blocked review ZIP without treating documents as an axis binding", async () => {
    const api = apiClient({
      version: version("design_validated"),
      approvals: [{
        approval_type: "design",
        approved_by: "reviewer-1",
        reason: "Serverns fiberriktningsvarning har granskats.",
        generation_job_id: null,
        production_context_hash: null,
        manifest_sha256: null,
        overrides_json: [],
        created_at: "2026-08-01T08:00:00Z",
        updated_at: "2026-08-01T08:00:00Z",
      }],
      latest_job: grainBlockedCamJob,
    });
    vi.mocked(api.listArtifacts).mockResolvedValue(blockedReviewArtifacts);
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(
      () => undefined,
    );

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
      />,
    );

    expect(await screen.findByText("Granskningspaketet är klart")).toBeVisible();
    const camStatus = screen.getByRole("status", { name: "Status för CAM" });
    expect(camStatus).toHaveTextContent("strukturerad X/Y-bindning");
    expect(camStatus).toHaveTextContent("Uppladdade dokument och varningsgodkännanden kan inte");
    expect(camStatus).toHaveTextContent(
      "Nesting, operationer, setupblad, backplot och maskinvalideringskod",
    );

    fireEvent.click(screen.getByRole("button", { name: "Ladda ned granskningspaket (.zip)" }));
    await waitFor(() => expect(api.listArtifacts).toHaveBeenCalledTimes(2));
    expect(anchorClick).toHaveBeenCalledOnce();
    expect(api.approveVersion).not.toHaveBeenCalled();
  });

  it("offers a DADO-retention-blocked review ZIP without treating acknowledgement as retention", async () => {
    const onSummaryChange = vi.fn();
    const api = apiClient({
      version: version("design_validated"),
      approvals: [{
        approval_type: "design",
        approved_by: "reviewer-1",
        reason: "Designvarningarna har granskats.",
        generation_job_id: null,
        production_context_hash: null,
        manifest_sha256: null,
        overrides_json: [],
        created_at: "2026-08-01T08:00:00Z",
        updated_at: "2026-08-01T08:00:00Z",
      }],
      latest_job: retentionBlockedCamJob,
    });
    vi.mocked(api.listArtifacts).mockResolvedValue(blockedReviewArtifacts);

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={onSummaryChange}
      />,
    );

    expect(await screen.findByText("Granskningspaketet är klart")).toBeVisible();
    expect(screen.getByRole("button", {
      name: "Ladda ned granskningspaket (.zip)",
    })).toBeVisible();
    const camStatus = screen.getByRole("status", { name: "Status för CAM" });
    expect(camStatus).toHaveTextContent("versionsbunden, checksummeadresserad");
    expect(camStatus).toHaveTextContent("torr självlåsning eller mekanisk retention");
    expect(camStatus).toHaveTextContent(
      "Lim, bärande geometri och granskningsgodkännanden ersätter inte retentionsevidens",
    );
    expect(screen.getByRole("status", {
      name: "Status för fysisk tillverkning",
    })).toHaveTextContent("Ej frisläppt för fysisk kapning");
    await waitFor(() => expect(onSummaryChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ designReviewReady: true, physicalCuttingAuthorized: false }),
    ));
  });

  it("keeps a back-panel-retention-blocked review ZIP downloadable", async () => {
    const api = apiClient({
      version: version("design_validated"),
      approvals: [{
        approval_type: "design",
        approved_by: "reviewer-1",
        reason: "Designkontrollen har granskats.",
        generation_job_id: null,
        production_context_hash: null,
        manifest_sha256: null,
        overrides_json: [],
        created_at: "2026-08-01T08:00:00Z",
        updated_at: "2026-08-01T08:00:00Z",
      }],
      latest_job: backPanelRetentionBlockedCamJob,
    });
    vi.mocked(api.listArtifacts).mockResolvedValue(blockedReviewArtifacts);
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(
      () => undefined,
    );

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
      />,
    );

    expect(await screen.findByText("Granskningspaketet är klart")).toBeVisible();
    const camStatus = screen.getByRole("status", { name: "Status för CAM" });
    expect(camStatus).toHaveTextContent("fyrsidiga mekaniska infångningen");
    expect(camStatus).toHaveTextContent("fortfarande tillgängligt för designgranskning");

    fireEvent.click(screen.getByRole("button", { name: "Ladda ned granskningspaket (.zip)" }));
    await waitFor(() => expect(api.listArtifacts).toHaveBeenCalledTimes(2));
    expect(api.downloadArtifact).toHaveBeenCalledOnce();
    expect(anchorClick).toHaveBeenCalledOnce();
  });

  it.each([
    {
      name: "the MATERIAL_GRAIN row is missing",
      expectedAlert: /readinessbevis saknas eller är ogiltigt/i,
      mutate: (readiness: ReadinessFixture) => {
        readiness.workshop_evidence = readiness.workshop_evidence.filter(
          (item) => item.code !== "MATERIAL_GRAIN",
        );
        readiness.missing_evidence_count -= 1;
      },
    },
    {
      name: "MATERIAL_GRAIN is falsely VERIFIED",
      expectedAlert: /designgranskning är inte klar/i,
      mutate: (readiness: ReadinessFixture) => {
        const grain = readiness.workshop_evidence.find((item) => item.code === "MATERIAL_GRAIN")!;
        grain.status = "VERIFIED";
        readiness.missing_evidence_count -= 1;
      },
    },
    {
      name: "MATERIAL_GRAIN has a software-only MISSING status",
      expectedAlert: /readinessbevis saknas eller är ogiltigt/i,
      mutate: (readiness: ReadinessFixture) => {
        const grain = readiness.workshop_evidence.find((item) => item.code === "MATERIAL_GRAIN")!;
        grain.status = "MISSING";
      },
    },
  ])("fails closed when a grain-blocked result says $name", async ({ mutate, expectedAlert }) => {
    const readiness = blockedCamReadinessFixture(true);
    mutate(readiness);
    const tampered: JobRead = {
      ...grainBlockedCamJob,
      result_json: {
        ...grainBlockedCamJob.result_json,
        workshop_readiness: readiness,
      },
    };
    const api = apiClient({
      version: version("design_validated"),
      approvals: [{
        approval_type: "design",
        approved_by: "reviewer-1",
        reason: "Serverns fiberriktningsvarning har granskats.",
        generation_job_id: null,
        production_context_hash: null,
        manifest_sha256: null,
        overrides_json: [],
        created_at: "2026-08-01T08:00:00Z",
        updated_at: "2026-08-01T08:00:00Z",
      }],
      latest_job: tampered,
    });
    vi.mocked(api.listArtifacts).mockResolvedValue(blockedReviewArtifacts);

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(expectedAlert);
    expect(screen.queryByRole("button", {
      name: "Ladda ned granskningspaket (.zip)",
    })).not.toBeInTheDocument();
  });

  it.each([
    ["authoritative geometry is false", { authoritative_geometry: false }],
    ["authoritative geometry is missing", { authoritative_geometry: undefined }],
    ["DFM status is not BLOCK", { dfm_status: "PASS" }],
    ["DFM readiness is VERIFIED", { workshop_readiness: stockBlockedReadinessWithVerifiedDfm() }],
    ["nesting utilization is claimed", { nesting_utilization_ppm: 1 }],
    ["a used sheet is claimed", { used_sheet_count: 1 }],
    ["used sheets is a boolean", { used_sheet_count: false }],
    ["a nesting layout is claimed", { nesting_layouts: [{ stock_id: "invented" }] }],
  ])("fails closed when a stockless result says %s", async (_label, claims) => {
    const tampered: JobRead = {
      ...stockBlockedCamJob,
      result_json: {
        ...stockBlockedCamJob.result_json,
        ...claims,
      },
    };
    const api = apiClient({
      version: version("design_validated"),
      approvals: [{
        approval_type: "design",
        approved_by: "reviewer-1",
        reason: "Lagerobunden designkontroll godkänd.",
        generation_job_id: null,
        production_context_hash: null,
        manifest_sha256: null,
        overrides_json: [],
        created_at: "2026-08-01T08:00:00Z",
        updated_at: "2026-08-01T08:00:00Z",
      }],
      latest_job: tampered,
    });
    vi.mocked(api.listArtifacts).mockResolvedValue(blockedReviewArtifacts);

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(/designgranskning är inte klar/i);
    expect(screen.queryByRole("button", {
      name: "Ladda ned granskningspaket (.zip)",
    })).not.toBeInTheDocument();
  });

  it("revalidates the fresh artifact list before downloading a blocked-CAM package", async () => {
    const api = apiClient({
      version: version("design_validated"),
      approvals: [{
        approval_type: "design",
        approved_by: "reviewer-1",
        reason: "Designkontroll godkänd.",
        generation_job_id: null,
        production_context_hash: null,
        manifest_sha256: null,
        overrides_json: [],
        created_at: "2026-08-01T08:00:00Z",
        updated_at: "2026-08-01T08:00:00Z",
      }],
      latest_job: blockedCamJob,
    });
    vi.mocked(api.listArtifacts)
      .mockResolvedValueOnce(blockedReviewArtifacts)
      .mockResolvedValueOnce([
        ...blockedReviewArtifacts,
        { ...bundle, id: "late-operations", kind: "operations" },
      ]);
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(
      () => undefined,
    );

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
      />,
    );

    expect(await screen.findByText("Granskningspaketet är klart")).toBeVisible();
    fireEvent.click(screen.getByRole("button", {
      name: "Ladda ned granskningspaket (.zip)",
    }));

    expect(await screen.findByText(/aktuella artefaktlista/i)).toBeVisible();
    expect(api.listArtifacts).toHaveBeenCalledTimes(2);
    expect(anchorClick).not.toHaveBeenCalled();
  });

  it("fails closed when blocked-CAM status claims an omitted manufacturing artifact", async () => {
    const tamperedStatus = blockedCamPackageStatusFixture();
    tamperedStatus.operations_included = true;
    const tamperedJob: JobRead = {
      ...blockedCamJob,
      result_json: {
        ...blockedCamJob.result_json,
        design_review_package_status: tamperedStatus,
      },
    };
    const api = apiClient({
      version: version("design_validated"),
      approvals: [{
        approval_type: "design",
        approved_by: "reviewer-1",
        reason: "Designkontroll godkänd.",
        generation_job_id: null,
        production_context_hash: null,
        manifest_sha256: null,
        overrides_json: [],
        created_at: "2026-08-01T08:00:00Z",
        updated_at: "2026-08-01T08:00:00Z",
      }],
      latest_job: tamperedJob,
    });
    vi.mocked(api.listArtifacts).mockResolvedValue(blockedReviewArtifacts);

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Granskningspaketet blev inte komplett/i,
    );
    expect(screen.queryByText("Granskningspaketet är klart")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", {
      name: "Ladda ned granskningspaket (.zip)",
    })).not.toBeInTheDocument();
  });

  it("rejects contradictory physical authorization evidence and fails closed", () => {
    const contradictoryJob: JobRead = {
      ...succeededJob,
      result_json: {
        manifest_sha256: "d".repeat(64),
        ...safeMachineProgramFields,
        workshop_readiness: {
          ...designReviewReadiness,
          physical_cutting_authorized: true,
        },
      },
    };

    expect(workshopReadinessFromJob(contradictoryJob)).toBeUndefined();
  });

  it("does not advertise a completed package when backend design review is not ready", async () => {
    const incompleteReadiness = designReviewReadinessFixture();
    incompleteReadiness.design_review_ready = false;
    incompleteReadiness.software_evidence[0] = {
      ...incompleteReadiness.software_evidence[0]!,
      status: "MISSING",
      evidence: "No bound evidence in this generation job.",
      required_action: "Generate authoritative CAD.",
    };
    incompleteReadiness.missing_evidence_count += 1;
    const api = apiClient({
      version: version("design_validated"),
      approvals: [{
        approval_type: "design",
        approved_by: "reviewer-1",
        reason: "Designkontroll genomförd.",
        generation_job_id: null,
        production_context_hash: null,
        manifest_sha256: null,
        overrides_json: [],
        created_at: "2026-08-01T08:00:00Z",
        updated_at: "2026-08-01T08:00:00Z",
      }],
      latest_job: {
        ...succeededJob,
        result_json: {
          ...reviewJobArtifactResult(false),
          ...safeMachineProgramFields,
          design_review_package_status: generatedCamPackageStatusFixture(),
          workshop_readiness: incompleteReadiness,
        },
      },
    });

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Serverns designgranskning är inte klar/i,
    );
    expect(screen.queryByRole("button", { name: "Ladda ned granskningspaket (.zip)" })).not.toBeInTheDocument();
    expect(screen.queryByText("Granskningspaketet är klart")).not.toBeInTheDocument();
  });

  it("keeps polling a healthy long-running server job", async () => {
    const api = apiClient({
      version: version("design_validated"),
      approvals: [{
        approval_type: "design",
        approved_by: "reviewer-1",
        reason: "Designkontroll godkänd.",
        generation_job_id: null,
        production_context_hash: null,
        manifest_sha256: null,
        overrides_json: [],
        created_at: "2026-08-01T08:00:00Z",
        updated_at: "2026-08-01T08:00:00Z",
      }],
      latest_job: { ...queuedJob, status: "running" },
    });
    vi.mocked(api.getJob).mockResolvedValue({ ...queuedJob, status: "running" });

    const rendered = render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
        pollIntervalMs={1}
      />,
    );

    expect(await screen.findByText("Underlaget skapas. Det kan ta några minuter.")).toBeVisible();
    await waitFor(() => expect(api.getJob).toHaveBeenCalled());
    expect(screen.queryByText(/överskred väntetiden/i)).not.toBeInTheDocument();
    rendered.unmount();
  });

  it("keeps deterministic helper behavior", () => {
    expect(generationProgressMessage({
      ...queuedJob,
      created_at: "2026-08-01T08:01:00Z",
    }, Date.parse("2026-08-01T08:11:00Z"))).toContain("10 min");

    const base = warning("CB-TIP-001", "Varning", "Kontrollera.");
    expect(serverApprovalWarningRuleIds([
      base,
      { ...base, rule_id: "DFM-GRAIN-001" },
      { ...base, rule_id: "CB-SUPPORT-001", status: "PASS" },
    ])).toEqual(["CB-TIP-001", "DFM-GRAIN-001"]);
    expect(productionSuggestionPatch({
      ...base,
      status: "BLOCK",
      suggestion: {
        action: "align_base_cabinets",
        label: "Anpassa",
        value: 6,
        explanation: "Rikta stöden.",
      },
    })).toEqual({ base_cabinet_count: 6 });
  });

  it("loads read-only server versions for the embedded Underlag view", async () => {
    const saved = version("design_validated");
    const listVersions = vi.fn(async () => [saved]);
    const api: ProductionApi = {
      ...apiClient({ version: saved }),
      listVersions,
    };

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
        projectId={project.id}
        showRevisionHistory
      />,
    );

    expect(screen.getByRole("heading", { level: 3, name: "Versionshistorik" })).toBeVisible();
    expect(await screen.findByRole("list", { name: "Serverrevisioner" })).toBeVisible();
    expect(listVersions).toHaveBeenCalledWith(project.id);
    expect(screen.getByRole("status", { name: "Lokal modell och serverrevision" })).toHaveTextContent(
      "Matchar serverrevision R1",
    );
  });

  it("requests exactly one fresh server preview after a design-hash conflict and waits for another click", async () => {
    const staleHash = "1".repeat(64);
    const refreshedHash = "2".repeat(64);
    const staleDesign = { ...designWith([], "PASS"), design_hash: staleHash };
    const refreshedDesign = { ...staleDesign, design_hash: refreshedHash };
    const api = apiClient();
    const retryServerPreview = vi.fn();
    vi.mocked(api.createVersion)
      .mockRejectedValueOnce(new ApiError(
        "API 409: EXPECTED_DESIGN_HASH_MISMATCH",
        409,
      ))
      .mockResolvedValueOnce({
        ...version("draft"),
        revision: 1,
        design_hash: refreshedHash,
      });

    const rendered = render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={staleDesign}
        onSummaryChange={vi.fn()}
        onRequestServerPreviewRetry={retryServerPreview}
        projectId={project.id}
        principal={designerPrincipal}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Spara och kontrollera" }));
    await waitFor(() => expect(retryServerPreview).toHaveBeenCalledOnce());
    expect(api.createVersion).toHaveBeenCalledTimes(1);
    expect(api.createVersion).toHaveBeenNthCalledWith(
      1,
      project.id,
      DEFAULT_DESIGN_SPEC,
      staleHash,
      0,
      "shelving",
    );

    rendered.rerender(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={refreshedDesign}
        onSummaryChange={vi.fn()}
        onRequestServerPreviewRetry={retryServerPreview}
        projectId={project.id}
        principal={designerPrincipal}
      />,
    );
    const retry = await screen.findByRole("button", { name: "Spara och kontrollera" });
    expect(api.createVersion).toHaveBeenCalledTimes(1);

    fireEvent.click(retry);
    await waitFor(() => expect(api.createVersion).toHaveBeenCalledTimes(2));
    expect(api.createVersion).toHaveBeenNthCalledWith(
      2,
      project.id,
      DEFAULT_DESIGN_SPEC,
      refreshedHash,
      0,
      "shelving",
    );
    expect(retryServerPreview).toHaveBeenCalledOnce();
  });

  it("refreshes the revision guard, clears downstream evidence and waits for another save click", async () => {
    const currentDesign = designWith([], "PASS");
    const staleVersion = {
      ...version("design_validated"),
      design_hash: "3".repeat(64),
    };
    const refreshedVersion = {
      ...version("design_validated"),
      id: "version-2",
      revision: 2,
      design_hash: "4".repeat(64),
    };
    const approvedState: Partial<ProductionStateRead> = {
      version: staleVersion,
      approvals: [{
        approval_type: "design",
        approved_by: "reviewer-1",
        reason: "Gammalt godkännande.",
        generation_job_id: null,
        production_context_hash: null,
        manifest_sha256: null,
        overrides_json: [],
        created_at: "2026-08-01T08:00:00Z",
        updated_at: "2026-08-01T08:00:00Z",
      }],
      latest_job: succeededJob,
    };
    const api = apiClient(approvedState);
    vi.mocked(api.getProductionState)
      .mockResolvedValueOnce({
        project_id: project.id,
        version: staleVersion,
        approvals: approvedState.approvals ?? [],
        latest_job: succeededJob,
        release: null,
      })
      .mockResolvedValueOnce({
        project_id: project.id,
        version: refreshedVersion,
        // Recovery must not restore downstream evidence from this response.
        approvals: approvedState.approvals ?? [],
        latest_job: succeededJob,
        release: null,
      });
    vi.mocked(api.createVersion)
      .mockRejectedValueOnce(new ApiError(
        "API 409: EXPECTED_CURRENT_REVISION_MISMATCH",
        409,
      ))
      .mockResolvedValueOnce({
        ...version("draft"),
        id: "version-3",
        revision: 3,
        design_hash: currentDesign.design_hash,
      });

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={currentDesign}
        onSummaryChange={vi.fn()}
        projectId={project.id}
        principal={designerPrincipal}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Spara och kontrollera" }));
    await waitFor(() => expect(api.getProductionState).toHaveBeenCalledTimes(2));
    expect(api.createVersion).toHaveBeenCalledTimes(1);
    expect(api.createVersion).toHaveBeenNthCalledWith(
      1,
      project.id,
      DEFAULT_DESIGN_SPEC,
      currentDesign.design_hash,
      1,
      "shelving",
    );

    const sessionKey = productionSessionKey(designerPrincipal, project.id, DEFAULT_DESIGN_SPEC.design_id);
    await waitFor(() => {
      const recovered = readProductionSession(window.sessionStorage, sessionKey);
      expect(recovered?.version?.revision).toBe(2);
      expect(recovered?.job).toBeUndefined();
      expect(recovered?.designApproved).toBe(false);
    });
    expect(api.listArtifacts).toHaveBeenCalledOnce();
    const retry = await screen.findByRole("button", { name: "Spara och kontrollera" });
    expect(api.createVersion).toHaveBeenCalledTimes(1);

    fireEvent.click(retry);
    await waitFor(() => expect(api.createVersion).toHaveBeenCalledTimes(2));
    expect(api.createVersion).toHaveBeenNthCalledWith(
      2,
      project.id,
      DEFAULT_DESIGN_SPEC,
      currentDesign.design_hash,
      2,
      "shelving",
    );
  });

  it("shows an explicit disabled state without a configured API", () => {
    render(
      <ProductionWorkflow
        apiClient={{ ...apiClient(), configured: false }}
        spec={DEFAULT_DESIGN_SPEC}
        design={designWith([], "PASS")}
        onSummaryChange={vi.fn()}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent(/Underlag är inte tillgängligt/i);
    expect(screen.queryByRole("button", { name: "Spara och kontrollera" })).not.toBeInTheDocument();
  });
});

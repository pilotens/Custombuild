import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  productionContextFromSpec,
  type ArtifactRead,
  type DesignVersionRead,
  type JobRead,
  type ProductionStateRead,
  type ProjectRead,
} from "@/lib/api-client";
import { resolveDesign } from "@/lib/design-engine";
import { DEFAULT_DESIGN_SPEC, type ResolvedDesign, type RuleEvaluation } from "@/lib/design-types";
import { productionSessionKey, readProductionSession } from "@/lib/production-session-storage";
import {
  ProductionWorkflow,
  blockedCamEvidenceKindIsForbidden,
  designReviewPackageStatusFromJob,
  generationProgressMessage,
  permitsStocklessDesignReview,
  productionSuggestionPatch,
  reviewPackageArtifactInventoryIsTruthful,
  serverApprovalWarningRuleIds,
  workshopRequirementPresentation,
  workshopReadinessFromJob,
  type ProductionApi,
} from "./production-workflow";

const project: ProjectRead = {
  id: "project-1",
  name: "Arkitektväggen",
  description: "",
  furniture_type: "bookcase",
  current_revision: 1,
  archived: false,
  created_at: "2026-08-01T08:00:00Z",
  updated_at: "2026-08-01T08:00:00Z",
};

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
            "The current MVP cannot resolve this blocker because it has no authenticated "
            + "catalogue/evidence boundary. Such a server-side boundary must bind a versioned, "
            + "checksum-addressed mechanical retention contract to every DADO joint, including "
            + "exact geometry, hardware quantity, material/thickness applicability and separate "
            + "shear/withdrawal capacity data; a review acknowledgement, adhesive or geometric "
            + "bearing check is not retention evidence."
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

const succeededJob: JobRead = {
  ...queuedJob,
  status: "succeeded",
  attempts: 1,
  started_at: "2026-08-01T08:01:05Z",
  finished_at: "2026-08-01T08:02:00Z",
  result_json: {
    manifest_sha256: "d".repeat(64),
    machine_program_mode: "VALIDATION_DRY_RUN",
    production_machine_program: false,
    design_review_package_status: generatedCamPackageStatusFixture(),
    workshop_readiness: designReviewReadiness,
  },
};

const blockedCamJob: JobRead = {
  ...succeededJob,
  result_json: {
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
    getJob: vi.fn(async () => succeededJob),
    listArtifacts: vi.fn(async () => completeArtifacts),
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

describe("review package artifact inventory", () => {
  it("allows and requires the checksum-bound stock-selection snapshot", () => {
    expect(blockedCamEvidenceKindIsForbidden("stock_selection")).toBe(false);
    expect(reviewPackageArtifactInventoryIsTruthful(
      blockedReviewArtifacts,
      blockedCamPackageStatusFixture(),
      true,
    )).toBe(true);
    expect(reviewPackageArtifactInventoryIsTruthful(
      blockedReviewArtifacts.filter((artifact) => artifact.kind !== "stock_selection"),
      blockedCamPackageStatusFixture(),
      true,
    )).toBe(false);
  });

  it("allows and requires the checksum-bound generation plan", () => {
    expect(blockedCamEvidenceKindIsForbidden("generation_plan")).toBe(false);
    expect(reviewPackageArtifactInventoryIsTruthful(
      blockedReviewArtifacts,
      blockedCamPackageStatusFixture(),
      true,
    )).toBe(true);
    expect(reviewPackageArtifactInventoryIsTruthful(
      blockedReviewArtifacts.filter((artifact) => artifact.kind !== "generation_plan"),
      blockedCamPackageStatusFixture(),
      true,
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
    )).toBe(true);
  });

  it("requires the validation program claim for generated CAM", () => {
    const status = generatedCamPackageStatusFixture();
    status.validation_program_included = false;
    expect(reviewPackageArtifactInventoryIsTruthful(
      completeArtifacts,
      status,
      true,
    )).toBe(false);
  });

  it("rejects an unparseable claimed status instead of downgrading to legacy", () => {
    expect(reviewPackageArtifactInventoryIsTruthful(
      completeArtifacts.filter((artifact) => artifact.kind !== "design_review_package_status"),
      undefined,
      true,
    )).toBe(false);
  });

  it("rejects a statusless v4 inventory", () => {
    expect(reviewPackageArtifactInventoryIsTruthful(
      completeArtifacts.filter((artifact) => artifact.kind !== "design_review_package_status"),
      undefined,
      false,
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

describe("ProductionWorkflow", () => {
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
        pollIntervalMs={60_000}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Spara och kontrollera" }));
    const create = await screen.findByRole("button", { name: "Skapa underlag" });
    const confirmation = screen.getByRole("checkbox", {
      name: "Jag har läst och kontrollerat varningarna ovan.",
    });

    expect(screen.getByText("Vältrisk")).toBeVisible();
    expect(screen.getByText("Beslag")).toBeVisible();
    expect(screen.getByText("Fiberriktning")).toBeVisible();
    expect(screen.queryByLabelText("Dokument")).not.toBeInTheDocument();
    expect(screen.queryByText("Bevis saknas")).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Status för verifieringen" })).toHaveTextContent("Behöver beslut");
    expect(create).toBeDisabled();

    fireEvent.click(confirmation);
    expect(create).toBeEnabled();
    fireEvent.click(create);

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
    expect(api.generateVersion).toHaveBeenCalledWith(project.id, 1, expect.objectContaining({
      include_freecad_project: false,
      external_evidence_ids: [],
    }));
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
      />,
    );

    const create = await screen.findByRole("button", { name: "Skapa underlag" });
    expect(create).toBeDisabled();
    expect(screen.getByRole("alert", { name: "Krav som måste lösas" })).toBeVisible();
    expect(screen.getByRole("region", { name: "Status för verifieringen" })).toHaveTextContent("Måste lösas");
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
      />,
    );

    expect(screen.getByRole("alert", { name: "Krav som blockerar CAM" })).toBeVisible();
    expect(screen.getByText("Blockerar CAM · 2 krav")).toBeVisible();
    fireEvent.click(await screen.findByRole("button", {
      name: "Spara för lagerobunden granskning",
    }));
    await waitFor(() => expect(api.validateVersion).toHaveBeenCalledWith(project.id, 1));

    const create = await screen.findByRole("button", { name: "Skapa underlag" });
    expect(create).toBeEnabled();
    fireEvent.click(create);

    await waitFor(() => expect(api.approveVersion).toHaveBeenCalledWith(project.id, 1, {
      approval_type: "design",
      reason: "Designkontroll godkänd för ett lagerobundet granskningspaket. Lagerprofil, nesting och CAM är uttryckligen inte godkända.",
      generation_job_id: null,
      warning_overrides: [],
    }));
    expect(api.generateVersion).toHaveBeenCalledWith(project.id, 1, expect.objectContaining({
      stock_width_mm: DEFAULT_DESIGN_SPEC.stock_width_mm,
      stock_height_mm: DEFAULT_DESIGN_SPEC.stock_height_mm,
      back_stock_width_mm: DEFAULT_DESIGN_SPEC.back_stock_width_mm,
      back_stock_height_mm: DEFAULT_DESIGN_SPEC.back_stock_height_mm,
      machine_profile_id: DEFAULT_DESIGN_SPEC.machine_profile_id,
    }));
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
    expect(document.body).not.toHaveTextContent("5125");
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
    let suggestedFileName: string | undefined;
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      suggestedFileName = this.download;
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
    expect(screen.getByRole("heading", { level: 3, name: "Hämta granskningspaket" })).toBeVisible();
    expect(screen.getByRole("heading", { level: 4, name: "Granskningspaketet är klart" })).toBeVisible();
    expect(screen.getByRole("status", { name: "Status för fysisk tillverkning" })).toHaveTextContent(
      "Ej frisläppt för fysisk kapning",
    );
    expect(screen.getByRole("status", { name: "Status för fysisk tillverkning" })).toHaveTextContent(
      "14 externa verkstadskrav återstår",
    );
    expect(screen.getByText(/endast avsett för designgranskning och validering/i)).toBeVisible();
    const packageIdentity = screen.getByRole("region", { name: "Paketidentitet" });
    expect(within(packageIdentity).getByText("Designgranskning")).toBeVisible();
    expect(within(packageIdentity).getByText("2.4 MB")).toBeVisible();
    expect(within(packageIdentity).getByText("d".repeat(64))).toBeVisible();
    expect(within(packageIdentity).getByText("Designgranskningspaket (ZIP)")).toBeVisible();
    expect(within(packageIdentity).getByText("Lagerurval")).toBeVisible();
    expect(within(packageIdentity).getByText("Genereringsplan")).toBeVisible();
    expect(within(packageIdentity).getByText("Readinessbevis")).toBeVisible();
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
    expect(screen.queryByText(/CAM|lås revision|frisläpp revision/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(document.querySelector('input[type="file"]')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Ladda ned granskningspaket (.zip)" }));
    await waitFor(() => expect(api.listArtifacts).toHaveBeenCalledTimes(2));
    expect(anchorClick).toHaveBeenCalledOnce();
    expect(suggestedFileName).toBe("designgranskningspaket.zip");
    expect(api.approveVersion).not.toHaveBeenCalled();
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
    expect(within(packageIdentity).queryByText("Semantiska operationer")).not.toBeInTheDocument();
    expect(within(packageIdentity).queryByText("Valideringsbackplot")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Skapa om underlag" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Ladda ned granskningspaket (.zip)" }));
    await waitFor(() => expect(api.listArtifacts).toHaveBeenCalledTimes(2));
    expect(anchorClick).toHaveBeenCalledOnce();
    expect(suggestedFileName).toBe("designgranskningspaket.zip");
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
    expect(document.body).not.toHaveTextContent("5125");

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
          manifest_sha256: "d".repeat(64),
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

    const sessionKey = productionSessionKey(undefined, project.id, DEFAULT_DESIGN_SPEC.design_id);
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

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  ArtifactRead,
  DesignVersionRead,
  JobRead,
  ProjectRead,
  ReleaseRead,
} from "@/lib/api-client";
import { resolveDesign } from "@/lib/design-engine";
import { DEFAULT_DESIGN_SPEC, type DesignSpec, type ResolvedDesign } from "@/lib/design-types";
import { ProductionWorkflow, type ProductionApi } from "./production-workflow";

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
    immutable: status === "released",
    design_hash: "a".repeat(64),
    context_hash: "b".repeat(64),
    engine_version: "1.0.0",
    template_version: "1.0.0",
    rule_version: "1.0.0",
    spec_json: {},
    result_json: {},
    created_at: "2026-08-01T08:00:00Z",
  };
}

const succeededJob: JobRead = {
  id: "job-1",
  design_version_id: "version-1",
  status: "succeeded",
  production_context_hash: "c".repeat(64),
  production_engine_context_json: {
    schema_version: "custombuild.production-engine-context.v1",
  },
  attempts: 1,
  error: null,
  result_json: { manifest_sha256: "d".repeat(64) },
  created_at: "2026-08-01T08:01:00Z",
  updated_at: "2026-08-01T08:02:00Z",
};

const queuedJob: JobRead = {
  ...succeededJob,
  status: "queued",
  attempts: 0,
  result_json: null,
};

const bundle: ArtifactRead = {
  id: "artifact-1",
  kind: "production_bundle",
  sha256: "d".repeat(64),
  size_bytes: 2_400_000,
  content_type: "application/zip",
  download_url: "https://artifacts.example.test/custombuild.zip?signature=fresh",
  download_path: "/v1/artifacts/artifact-1/download?signature=signed",
};

const released: ReleaseRead = {
  release_id: "release-1",
  release_number: "R1",
  status: "released",
  manifest_sha256: "d".repeat(64),
  machine_use: "validation_only",
};

function authoritativeDesign(spec: DesignSpec = DEFAULT_DESIGN_SPEC): ResolvedDesign {
  return {
    ...resolveDesign(spec),
    source: "server-preview",
    status: "PASS",
    rule_evaluations: [],
  };
}

function productionApi(): ProductionApi {
  return {
    configured: true,
    ensureProject: vi.fn(async () => project),
    createVersion: vi.fn(async () => version("draft")),
    validateVersion: vi.fn(async () => version("design_validated")),
    approveVersion: vi.fn(async (_projectId, _revision, approval) =>
      version(approval.approval_type === "cam" ? "approved" : "design_validated")),
    generateVersion: vi.fn(async () => queuedJob),
    getJob: vi.fn(async () => succeededJob),
    listArtifacts: vi.fn(async () => [bundle]),
    releaseVersion: vi.fn(async () => released),
  };
}

afterEach(() => vi.restoreAllMocks());

describe("ProductionWorkflow", () => {
  it("runs the real revision gates in order and refreshes the signed ZIP link", async () => {
    const api = productionApi();
    const summary = vi.fn();
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);

    render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={authoritativeDesign()}
        onSummaryChange={summary}
        pollIntervalMs={0}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Spara revision" }));
    await waitFor(() => expect(api.createVersion).toHaveBeenCalledWith(project.id, DEFAULT_DESIGN_SPEC));

    fireEvent.click(screen.getByRole("button", { name: "Validera design" }));
    await waitFor(() => expect(api.validateVersion).toHaveBeenCalledWith(project.id, 1));

    fireEvent.change(screen.getByLabelText("Designgranskarens motivering"), {
      target: { value: "Konstruktion och regelspår granskade." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Godkänn design" }));
    await waitFor(() => expect(api.approveVersion).toHaveBeenCalledWith(
      project.id,
      1,
      expect.objectContaining({ approval_type: "design", generation_job_id: null }),
    ));

    fireEvent.click(screen.getByRole("button", { name: "Generera paket" }));
    await waitFor(() => expect(api.generateVersion).toHaveBeenCalledWith(
      project.id,
      1,
      expect.objectContaining({
        machine_profile_id: DEFAULT_DESIGN_SPEC.machine_profile_id,
        stock_width_mm: DEFAULT_DESIGN_SPEC.stock_width_mm,
        include_validation_program: true,
      }),
    ));
    expect(api.getJob).toHaveBeenCalledWith(queuedJob.id);
    await screen.findByRole("button", { name: "Godkänn CAM för detta jobb" });

    fireEvent.change(screen.getByLabelText("CAM-granskarens motivering"), {
      target: { value: "Setup, verktyg och backplot granskade." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Godkänn CAM för detta jobb" }));
    await waitFor(() => expect(api.approveVersion).toHaveBeenCalledWith(
      project.id,
      1,
      expect.objectContaining({ approval_type: "cam", generation_job_id: succeededJob.id }),
    ));

    fireEvent.click(screen.getByLabelText(/Jag bekräftar att revisionen ska låsas/));
    fireEvent.click(screen.getByRole("button", { name: "Frisläpp revision" }));
    await waitFor(() => expect(api.releaseVersion).toHaveBeenCalledWith(project.id, 1, "R1"));
    expect(await screen.findByText("R1 frisläppt")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: /Ladda ned ZIP/ }));
    await waitFor(() => expect(api.listArtifacts).toHaveBeenCalledTimes(2));
    expect(anchorClick).toHaveBeenCalledOnce();
    expect(summary).toHaveBeenLastCalledWith({ revision: 1, status: "released", stale: false });
  });

  it("invalidates every downstream gate when the local design changes", async () => {
    const api = productionApi();
    const { rerender } = render(
      <ProductionWorkflow
        apiClient={api}
        spec={DEFAULT_DESIGN_SPEC}
        design={authoritativeDesign()}
        onSummaryChange={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Spara revision" }));
    await screen.findByText(/Rev 1/);

    const changedSpec = { ...DEFAULT_DESIGN_SPEC, width_mm: DEFAULT_DESIGN_SPEC.width_mm + 50 };
    rerender(
      <ProductionWorkflow
        apiClient={api}
        spec={changedSpec}
        design={authoritativeDesign(changedSpec)}
        onSummaryChange={vi.fn()}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(/tidigare revisionens underlag får inte användas/i);
    expect(screen.getByRole("button", { name: "Validera design" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Spara ny revision" })).toBeEnabled();
  });

  it("shows an explicit disabled state without a configured API", () => {
    render(
      <ProductionWorkflow
        apiClient={{ ...productionApi(), configured: false }}
        spec={DEFAULT_DESIGN_SPEC}
        design={authoritativeDesign()}
        onSummaryChange={vi.fn()}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent(/Produktionsflödet är avstängt/i);
    expect(screen.queryByRole("button", { name: "Spara revision" })).not.toBeInTheDocument();
  });
});

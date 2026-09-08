import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import fixture from "../e2e/fixtures/furniture-production-preview.json";
import { ApiError, CustombuildApiClient, type CurrentPrincipal, type DesignVersionRead } from "@/lib/api-client";
import type { FurnitureProductionPreview } from "@/lib/furniture-workspace";
import { FurnitureProduction } from "./furniture-production";

const preview = fixture as unknown as FurnitureProductionPreview;
const source = preview.source_furniture;
const projectId = source.workspace.design.design_id;
const principal: CurrentPrincipal = {
  user_id: "fixture-owner", organization_id: "fixture-org", role: "owner", name: "Designer", email: "fixture@example.test",
};
function versionFixture(patch: Partial<DesignVersionRead> = {}): DesignVersionRead {
  return {
    id: "production-version", project_id: projectId, revision: 1, status: "draft",
    design_hash: fixture.preview.design_hash, context_hash: "d".repeat(64),
    spec_json: fixture.preview.spec, result_json: fixture.preview,
    source_provenance_json: {}, source_import_id: null,
    engine_version: fixture.preview.engine_version,
    template_version: `bookcase@${fixture.preview.template_version}`,
    rule_version: "bookcase-rules@1.4.0", template_id: "shelving",
    template_capability_fingerprint: "e".repeat(64), immutable: false,
    created_at: "2026-09-08T10:00:00Z", ...patch,
  };
}
function setup() {
  const api = new CustombuildApiClient("https://api.example.test");
  vi.spyOn(api, "previewFurnitureProduction").mockResolvedValue(preview);
  vi.spyOn(api, "getProductionState").mockResolvedValue({ project_id: projectId,
    version: null, approvals: [], latest_job: null, release: null });
  vi.spyOn(api, "listExternalEvidence").mockResolvedValue([]);
  vi.spyOn(api, "listVersions").mockResolvedValue([]);
  return api;
}
function show(api: CustombuildApiClient, onClose = vi.fn()) {
  render(<FurnitureProduction api={api} principal={principal} workspace={source.workspace}
    designHash={source.furniture_design_hash} projectName="Mitt hyllsystem" onClose={onClose} />);
}
afterEach(() => { vi.restoreAllMocks(); window.sessionStorage.clear(); });

describe("beredning från sparad möbel", () => {
  it("skickar den faktiska servermodellen och ursprungsrevisionen till befintligt produktionsflöde", async () => {
    const api = setup();
    const create = vi.spyOn(api, "createVersion").mockImplementation(async (_id, _spec, hash, _revision, _template, _retention, bound) => versionFixture({
      design_hash: hash,
      result_json: { ...preview.preview, source_furniture: bound }, immutable: false,
    }));
    vi.spyOn(api, "validateVersion").mockRejectedValue(new ApiError("Blocking construction or DFM rules remain", 409));
    show(api);
    const save = await screen.findByRole("button", { name: "Spara och kontrollera" });
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);
    await waitFor(() => expect(create).toHaveBeenCalledOnce());
    const call = create.mock.calls[0]!;
    expect(call[1]).toMatchObject({ measured_thickness_mm: 17.801, width_mm: 700.001,
      height_mm: 1000.003, divider_count: 0, shelf_count: 2, plinth: false,
      machine_profile_id: source.workspace.manufacturing!.machine_profile_id });
    expect(call[2]).toBe(preview.preview.design_hash);
    expect(call[6]).toEqual(source);
    expect(screen.queryByRole("button", { name: "Godkänn CAM" })).not.toBeInTheDocument();
  });

  it("varnar innan en ändrad verkstadsprofil kastas bort", async () => {
    const api = setup(); const close = vi.fn();
    show(api, close);
    await screen.findByRole("button", { name: "Spara och kontrollera" });
    const machines = screen.getAllByRole("radio");
    const larger = machines.find(input => (input as HTMLInputElement).value === "custombuild-router-5125-linuxcnc")!;
    expect(larger).toBeEnabled();
    fireEvent.click(larger);
    fireEvent.click(screen.getByRole("button", { name: "Tillbaka till möbeln" }));
    expect(await screen.findByRole("alertdialog", { name: "Osparad beredning" })).toBeVisible();
    expect(close).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Lämna utan att spara beredningen" }));
    expect(close).toHaveBeenCalledOnce();
  });

  it("visar ett serverfel och kan hämta den sparade modellen igen", async () => {
    const api = setup();
    vi.mocked(api.previewFurnitureProduction).mockRejectedValueOnce(new ApiError("Möbelrevisionen har ändrats", 409));
    show(api);
    expect(await screen.findByRole("alert")).toHaveTextContent("Möbelrevisionen har ändrats");
    expect(screen.queryByRole("button", { name: "Spara och kontrollera" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Försök igen" }));
    expect(await screen.findByRole("button", { name: "Spara och kontrollera" })).toBeVisible();
  });

  it("återställer exakt den sparade verkstadsprofilen och varnar inte för oförändrat underlag", async () => {
    const api = setup(); const close = vi.fn();
    vi.mocked(api.getProductionState).mockResolvedValue({ project_id: projectId, approvals: [], latest_job: null, release: null,
      version: versionFixture({ id: "saved-production", revision: 3, status: "design_validated",
        result_json: { ...preview.preview, source_furniture: source, production_context: {
          machine_profile_id: "custombuild-router-5125-linuxcnc", stock_width_mm: 2500,
          stock_height_mm: 1300, stock_count: 3, back_stock_width_mm: 2500,
          back_stock_height_mm: 1300, back_stock_count: 2,
        } }, immutable: false }) });
    show(api, close);
    const larger = await screen.findByRole("radio", { name: /5125/ });
    expect(larger).toBeChecked();
    expect(screen.queryByText(/Modellen har ändrats\. Spara/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Tillbaka till möbeln" }));
    expect(close).toHaveBeenCalledOnce();
  });

  it("återanvänder inte en beredning från en annan materialbatch med samma geometri", async () => {
    const api = setup();
    vi.mocked(api.getProductionState).mockResolvedValue({ project_id: projectId, approvals: [], latest_job: null, release: null,
      version: versionFixture({ id: "old-production", revision: 1, status: "design_validated",
        result_json: { ...preview.preview, source_furniture: { ...source, workspace_sha256: "0".repeat(64) }, production_context: {
          machine_profile_id: source.workspace.manufacturing!.machine_profile_id,
          stock_width_mm: 2440, stock_height_mm: 1220, stock_count: 1,
          back_stock_width_mm: 2440, back_stock_height_mm: 1220, back_stock_count: 1,
        } }, immutable: false }) });
    show(api);
    expect(await screen.findByText(/Modellen har ändrats\. Spara/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Spara och kontrollera" })).toBeVisible();
  });
});

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CustombuildApiClient, type CurrentPrincipal } from "@/lib/api-client";
import { newFurnitureWorkspace, type FurniturePreview, type FurnitureWorkspace } from "@/lib/furniture-workspace";
import { FurnitureStudio } from "./furniture-workspace";

vi.mock("next/dynamic", () => ({ default: () => function Viewer() { return <div>Modell</div>; } }));

const principal: CurrentPrincipal = {
  user_id: "user", organization_id: "organization", role: "owner", name: "Designer", email: "designer@example.test",
};
function preview(workspace: FurnitureWorkspace, parts = 5): FurniturePreview {
  return {
    schema_version: "custombuild.furniture-preview.v1", workspace,
    design: { design_hash: "a".repeat(64), intent_hash: "b".repeat(64), total_weight_g: 10_000,
      parts: Array.from({ length: parts }, (_, index) => ({ part_id: `p-${index}`, semantic_key: `part-${index}`,
        role: "top", material_id: "birch-plywood", actual_thickness_um: 18_000, weight_g: 2_000,
        finished_size: { width_um: 100_000, depth_um: 100_000, height_um: 18_000 },
        placement: { x_um: index * 100_000, y_um: 0, z_um: 0 } })), moving_groups: [] },
    rules: { overall_status: "BLOCK", evaluations: [] },
    manufacturing: { state: "not_selected", geometry_compatible: null, issues: [] },
    physical_cutting_authorized: false, production_qualified: false,
  };
}
function setup() {
  const api = new CustombuildApiClient("https://api.example.test");
  vi.spyOn(api, "listProjects").mockResolvedValue([]);
  vi.spyOn(api, "furnitureCatalog").mockResolvedValue({ families: [], materials: [
    { material_id: "birch-plywood", name: "Björkplywood", version: "screening-2026.1",
      nominal_thickness_um: 18_000, min_supported_thickness_um: 17_000, max_supported_thickness_um: 19_000 },
  ], hardware: [{ catalog_id: "panel-table-connectors-layout", version: "layout-1.0.0",
    name: "Gavelbord", family: "table", production_qualified: false }], machines: [] });
  vi.spyOn(api, "previewFurniture").mockImplementation(async workspace => preview(workspace));
  vi.spyOn(api, "furnitureHistory").mockResolvedValue({ items: [], next_offset: null });
  return api;
}
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

describe("möbelstudions revisions- och profilflöde", () => {
  it("tillämpar uppmätt tjocklek först efter konsekvenskontroll och användarens val", async () => {
    const api = setup();
    const compare = vi.spyOn(api, "compareFurnitureProfiles").mockImplementation(async (current, proposed) => ({
      state: "not_qualified", can_apply: true, intent_preserved: true, message: "Ny granskning krävs.",
      proposed: preview(proposed), original_design_hash: preview(current).design.design_hash,
      changed_part_ids: ["p-0"], changed_dependencies: ["material"], invalidated_reviews: ["construction", "cam"],
    }));
    render(<FurnitureStudio api={api} principal={principal} />);
    await screen.findByText("5 delar");
    fireEvent.change(screen.getByLabelText("Uppmätt skivtjocklek (mm)"), { target: { value: "17.801" } });
    expect(api.previewFurniture).toHaveBeenLastCalledWith(newFurnitureWorkspace("table"));
    fireEvent.click(screen.getByRole("button", { name: "Kontrollera profilbyte" }));
    await screen.findByText("Konsekvenser av profilbytet");
    expect(compare.mock.calls[0]![1].design.material.measured_thickness_um).toBe(17_801);
    expect(api.previewFurniture).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Använd profilbytet i designen" }));
    await waitFor(() => expect(api.previewFurniture).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.previewFurniture).mock.calls[1]![0].design.material.measured_thickness_um).toBe(17_801);
    expect(screen.getByRole("button", { name: "Skapa granskningspaket" })).toBeDisabled();
  });

  it("låter aldrig ett sent beräkningssvar ersätta en nyare design", async () => {
    vi.useFakeTimers();
    const api = setup();
    let releaseOld!: (value: FurniturePreview) => void;
    vi.mocked(api.previewFurniture).mockImplementationOnce(() => new Promise(resolve => { releaseOld = resolve; }));
    render(<FurnitureStudio api={api} principal={principal} />);
    await act(() => vi.advanceTimersByTimeAsync(200));
    fireEvent.change(screen.getByLabelText("Bredd (mm)"), { target: { value: "1100.001" } });
    await act(() => vi.advanceTimersByTimeAsync(200));
    expect(screen.getByText("5 delar")).toBeInTheDocument();
    await act(async () => releaseOld(preview(newFurnitureWorkspace("table"), 99)));
    expect(screen.queryByText("99 delar")).not.toBeInTheDocument();
    expect(vi.mocked(api.previewFurniture).mock.calls[1]![0].design.intent.width_um).toBe(1_100_001);
  });

  it("kräver sparad revision för export och behåller ändringar vid revisionskonflikt", async () => {
    const api = setup();
    vi.spyOn(api, "createProject").mockResolvedValue({ id: "project", name: "Mitt bord", furniture_type: "table",
      current_revision: 0, description: "", archived: false, created_at: "2026-09-07T12:00:00Z",
      updated_at: "2026-09-07T12:00:00Z" });
    const save = vi.spyOn(api, "saveFurnitureDraft").mockImplementationOnce(async (id, revision, workspace) => {
      const saved = { ...workspace, design: { ...workspace.design, design_id: id, revision: revision+1 } };
      return { project_id: id, revision: revision+1, workspace: saved, preview: preview(saved) };
    }).mockRejectedValueOnce(new Error("Utkastet har ändrats. Hämta aktuell revision före nästa sparning."));
    render(<FurnitureStudio api={api} principal={principal} />);
    await screen.findByText("5 delar");
    expect(screen.getByRole("button", { name: "Skapa granskningspaket" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Spara revision" }));
    await screen.findByText("Revision 1 är sparad.");
    await waitFor(() => expect(screen.getByRole("button", { name: "Skapa granskningspaket" })).toBeEnabled());
    fireEvent.change(screen.getByLabelText("Bredd (mm)"), { target: { value: "1200" } });
    await screen.findByText("5 delar");
    fireEvent.click(screen.getByRole("button", { name: "Spara revision" }));
    await screen.findByRole("alert");
    expect(screen.getByLabelText("Bredd (mm)")).toHaveValue(1200);
    expect(save.mock.calls[1]![1]).toBe(1);
    expect(screen.getByRole("button", { name: "Skapa granskningspaket" })).toBeDisabled();
  });

  it("avrundar aldrig inmatade CAD-mått till en annan geometri", async () => {
    const api = setup();
    render(<FurnitureStudio api={api} principal={principal} />);
    await screen.findByText("5 delar");
    fireEvent.change(screen.getByLabelText("Bredd (mm)"), { target: { value: "1100.0001" } });
    expect(screen.getByRole("alert")).toHaveTextContent("högst tre decimaler");
    expect(screen.getByRole("button", { name: "Spara revision" })).toBeDisabled();
  });
});

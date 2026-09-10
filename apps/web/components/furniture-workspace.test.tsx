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
  it("sparar en mätbar listprofil och reserverar endast valda väggar innan stommen räknas om", async () => {
    const api = setup();
    render(<FurnitureStudio api={api} principal={principal} />);
    await screen.findByText("5 delar");
    fireEvent.click(screen.getByLabelText("Ange separata kundmått"));
    fireEvent.click(screen.getByLabelText("Ange listprofil"));
    expect(screen.getByLabelText("Listhöjd (mm)")).toHaveValue(null);
    expect(screen.getByLabelText("Listbredd/utstick (mm)")).toHaveValue(null);
    fireEvent.change(screen.getByLabelText("Listhöjd (mm)"), { target: { value: "90" } });
    fireEvent.change(screen.getByLabelText("Listbredd/utstick (mm)"), { target: { value: "20" } });
    expect(screen.getByLabelText("Bredd (mm)")).toHaveValue(1000);
    fireEvent.change(screen.getByLabelText("Listens funktion"), { target: { value: "existing_room_trim" } });
    fireEvent.click(screen.getByLabelText("Vänster vägg"));
    const reserve = screen.getByRole("button", { name: "Reservera frigång för befintlig list", hidden: true });
    fireEvent.click(reserve); fireEvent.click(reserve);
    expect(screen.getByLabelText("Vänster · reserverat (mm)")).toHaveValue(20);
    expect(screen.getByLabelText("Höger · reserverat (mm)")).toHaveValue(null);
    expect(screen.getByLabelText("Bredd (mm)")).toHaveValue(1000);
    expect(screen.getByRole("button", { name: "Räkna om stommen från kundmåtten", hidden: true })).toBeDisabled();
  });

  it("skiljer radlast från meterlast och bevarar meterlasten när kundens bredd ändras", async () => {
    const api = setup();
    render(<FurnitureStudio api={api} principal={principal} />);
    await screen.findByText("5 delar");
    fireEvent.change(screen.getByLabelText("Möbeltyp"), { target: { value: "shelving" } });
    expect(screen.getByLabelText("Last per hel hyllrad (kg)")).toBeVisible();
    fireEvent.change(screen.getByLabelText("Hur anges hyllasten?"), { target: { value: "per_metre" } });
    fireEvent.change(screen.getByLabelText("Last per meter hyllrad (kg/m)"), { target: { value: "30.6" } });
    fireEvent.change(screen.getByLabelText("Bredd (mm)"), { target: { value: "4340" } });
    await waitFor(() => expect(vi.mocked(api.previewFurniture).mock.lastCall?.[0].design.intent).toMatchObject({
      width_um: 4_340_000, shelf_load_basis: "per_metre", shelf_load_per_metre_n: 300,
    }));
  });

  it("kräver att varje felaktigt indelningsfält rättas även efter en annan giltig måttändring", async () => {
    const api = setup();
    render(<FurnitureStudio api={api} principal={principal} />);
    await screen.findByText("5 delar");
    fireEvent.change(screen.getByLabelText("Möbeltyp"), { target: { value: "shelving" } });
    await screen.findByText("5 delar");
    fireEvent.change(screen.getByLabelText("Fackbredder (%)"), { target: { value: "20" } });
    fireEvent.change(screen.getByLabelText("Hyllcentrum från botten (%)"), { target: { value: "10" } });
    expect(screen.getAllByRole("alert")).toHaveLength(2);
    fireEvent.change(screen.getByLabelText("Höjd (mm)"), { target: { value: "1900" } });
    await screen.findByText("5 delar");
    expect(screen.getByRole("button", { name: "Spara revision" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Fackbredder (%)"), { target: { value: "50; 50" } });
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Spara revision" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Hyllcentrum från botten (%)"), { target: { value: "20; 40; 60; 80" } });
    await screen.findByText("5 delar");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("button", { name: "Spara revision" })).toBeEnabled();
    expect(vi.mocked(api.previewFurniture).mock.lastCall?.[0].design.intent.shelf_height_ratios_ppm).toEqual([200_000, 400_000, 600_000, 800_000]);
  });

  it("räknar kundmått till stomme först när alla reserverade mått anges och användaren tillämpar dem", async () => {
    const api = setup();
    render(<FurnitureStudio api={api} principal={principal} />);
    await screen.findByText("5 delar");
    fireEvent.click(screen.getByLabelText("Ange separata kundmått"));
    fireEvent.change(screen.getByLabelText("Kundlängd inklusive reserverat utrymme (mm)"), { target: { value: "4340.007" } });
    expect(screen.getByLabelText("Bredd (mm)")).toHaveValue(1000);
    expect(screen.getByRole("button", { name: "Räkna om stommen från kundmåtten", hidden: true })).toBeDisabled();
    for (const side of ["Vänster", "Höger", "Ovanför", "Under", "Framför", "Bakom"]) {
      fireEvent.change(screen.getByLabelText(`${side} · reserverat (mm)`), { target: { value: side === "Vänster" ? "50.001" : "0" } });
    }
    fireEvent.click(screen.getByRole("button", { name: "Räkna om stommen från kundmåtten", hidden: true }));
    expect(screen.getByLabelText("Bredd (mm)")).toHaveValue(4290.006);
    expect(screen.getByLabelText("Kundlängd inklusive reserverat utrymme (mm)")).toHaveValue(4340.007);
  });

  it("serverkontrollerar arbetsfiler och skapar alltid ett nytt osparat projekt", async () => {
    const api = setup();
    const create = vi.spyOn(api, "createProject");
    render(<FurnitureStudio api={api} principal={principal} />);
    await screen.findByText("5 delar");
    const imported = newFurnitureWorkspace("shelving");
    imported.design.design_id = "original-project"; imported.design.revision = 99;
    imported.design.intent.width_um = 4_340_000;
    const file = new File([], "kundbokhylla.json", { type: "application/json" });
    Object.defineProperty(file, "text", { value: async () => JSON.stringify(imported) });
    fireEvent.change(screen.getByLabelText("Läs arbetsfil (JSON)"), { target: { files: [file] } });
    await screen.findByText(/Arbetsfilen har kontrollerats/);
    const checked = vi.mocked(api.previewFurniture).mock.lastCall?.[0];
    expect(checked?.design.design_id).toBe("furniture");
    expect(checked?.design.revision).toBe(1);
    expect(screen.getByLabelText("Bredd (mm)")).toHaveValue(4340);
    expect(create).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Skapa granskningspaket" })).toBeDisabled();
  });

  it("ersätter även osparad indelningstext när en kontrollerad arbetsfil med samma grundmått öppnas", async () => {
    const api = setup();
    render(<FurnitureStudio api={api} principal={principal} />);
    await screen.findByText("5 delar");
    fireEvent.change(screen.getByLabelText("Möbeltyp"), { target: { value: "shelving" } });
    fireEvent.change(screen.getByLabelText("Fackbredder (%)"), { target: { value: "20" } });
    fireEvent.change(screen.getByLabelText("Hyllcentrum från botten (%)"), { target: { value: "10" } });
    fireEvent.change(screen.getByLabelText("Uppmätt skivtjocklek (mm)"), { target: { value: "17.801" } });
    expect(screen.getAllByRole("alert")).toHaveLength(2);
    const file = new File([], "kontrollerad-hylla.json", { type: "application/json" });
    Object.defineProperty(file, "text", { value: async () => JSON.stringify(newFurnitureWorkspace("shelving")) });
    fireEvent.change(screen.getByLabelText("Läs arbetsfil (JSON)"), { target: { files: [file] } });
    fireEvent.click(screen.getByRole("button", { name: "Fortsätt utan att spara" }));
    await screen.findByText(/Arbetsfilen har kontrollerats/);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByLabelText("Fackbredder (%)")).toHaveValue("");
    expect(screen.getByLabelText("Hyllcentrum från botten (%)")).toHaveValue("");
    expect(screen.getByLabelText("Uppmätt skivtjocklek (mm)")).toHaveValue(18);
    expect(screen.getByRole("button", { name: "Spara revision" })).toBeEnabled();
  });

  it("återbinder profilförslaget när ett projekt med samma form öppnas", async () => {
    const api = setup();
    vi.mocked(api.listProjects).mockResolvedValue([{ id: "saved-table", name: "Sparat bord", furniture_type: "table",
      current_revision: 0, description: "", archived: false, created_at: "2026-09-07T12:00:00Z",
      updated_at: "2026-09-07T12:00:00Z" }]);
    const saved = newFurnitureWorkspace("table");
    saved.design.design_id = "saved-table"; saved.design.revision = 6;
    vi.spyOn(api, "loadFurnitureDraft").mockResolvedValue({ project_id: "saved-table", revision: 6,
      workspace: saved, preview: preview(saved) });
    const compare = vi.spyOn(api, "compareFurnitureProfiles").mockResolvedValue({ state: "not_qualified",
      can_apply: false, proposed: null, message: "Granskning krävs.", changed_dependencies: [], invalidated_reviews: [] });
    render(<FurnitureStudio api={api} principal={principal} />);
    await screen.findByText("5 delar");
    fireEvent.change(screen.getByLabelText("Öppna möbelprojekt"), { target: { value: "saved-table" } });
    await screen.findByText(/senast sparad revision 6/);
    expect(screen.getByRole("button", { name: "Kontrollera profilbyte" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Uppmätt skivtjocklek (mm)"), { target: { value: "17.801" } });
    fireEvent.click(screen.getByRole("button", { name: "Kontrollera profilbyte" }));
    await waitFor(() => expect(compare).toHaveBeenCalledOnce());
    const [current, proposed] = compare.mock.calls[0]!;
    expect(proposed.design.design_id).toBe(current.design.design_id);
    expect(proposed.design.revision).toBe(6);
  });

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
    expect(screen.getByRole("button", { name: "Spara revision" })).toBeDisabled();
    expect(screen.getByLabelText("Bredd (mm)")).toBeDisabled();
    expect(api.previewFurniture).toHaveBeenLastCalledWith(newFurnitureWorkspace("table"));
    fireEvent.click(screen.getByRole("button", { name: "Kontrollera profilbyte" }));
    await screen.findByText("Konsekvenser av profilbytet");
    expect(compare.mock.calls[0]![1].design.material.measured_thickness_um).toBe(17_801);
    expect(api.previewFurniture).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Använd profilbytet i designen" }));
    await waitFor(() => expect(api.previewFurniture).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.previewFurniture).mock.calls[1]![0].design.material.measured_thickness_um).toBe(17_801);
    expect(screen.getByLabelText("Bredd (mm)")).toBeEnabled();
    expect(screen.getByRole("button", { name: "Spara revision" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Återställ profilförslag" })).toBeNull();
    expect(screen.getByRole("button", { name: "Skapa granskningspaket" })).toBeDisabled();
  });

  it("bevarar ett väntande profilförslag och blockerar sparning, formändring och tillverkning tills det återställs", async () => {
    const api = setup();
    const saved = newFurnitureWorkspace("shelving");
    saved.design.design_id = "saved-shelf"; saved.design.revision = 2;
    saved.manufacturing = { machine_profile_id: "reference-router", machine_profile_version: "1.0.0-validation",
      stock_width_um: 1_220_000, stock_height_um: 2_440_000, stock_grain_axis: "y" };
    vi.mocked(api.listProjects).mockResolvedValue([{ id: "saved-shelf", name: "Sparad hylla", furniture_type: "shelving",
      current_revision: 0, description: "", archived: false, created_at: "2026-09-07T12:00:00Z",
      updated_at: "2026-09-07T12:00:00Z" }]);
    vi.spyOn(api, "loadFurnitureDraft").mockResolvedValue({ project_id: "saved-shelf", revision: 2,
      workspace: saved, preview: preview(saved) });
    const save = vi.spyOn(api, "saveFurnitureDraft");
    render(<FurnitureStudio api={api} principal={principal} />);
    await screen.findByText("5 delar");
    fireEvent.change(screen.getByLabelText("Öppna möbelprojekt"), { target: { value: "saved-shelf" } });
    await screen.findByText(/senast sparad revision 2/);
    expect(screen.getByRole("button", { name: "Förbered tillverkning" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Skapa granskningspaket" })).toBeEnabled();
    fireEvent.change(screen.getByLabelText("Uppmätt skivtjocklek (mm)"), { target: { value: "17.801" } });
    expect(screen.getByLabelText("Bredd (mm)")).toBeDisabled();
    for (const name of ["Spara revision", "Spara arbetsfil", "Skapa granskningspaket", "Förbered tillverkning", "Föreslå fackindelning"]) {
      expect(screen.getByRole("button", { name })).toBeDisabled();
    }
    const leaving = new Event("beforeunload", { cancelable: true });
    fireEvent(window, leaving);
    expect(leaving.defaultPrevented).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Spara revision" }));
    expect(save).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("Möbeltyp"), { target: { value: "table" } });
    expect(screen.getByRole("alertdialog", { name: "Osparad design" })).toHaveTextContent("profilförslag som inte är tillämpat");
    fireEvent.click(screen.getByRole("button", { name: "Tillbaka" }));
    expect(screen.getByLabelText("Möbeltyp")).toHaveValue("shelving");
    expect(screen.getByLabelText("Uppmätt skivtjocklek (mm)")).toHaveValue(17.801);
    fireEvent.click(screen.getByRole("button", { name: "Återställ profilförslag" }));
    expect(screen.getByLabelText("Uppmätt skivtjocklek (mm)")).toHaveValue(18);
    expect(screen.getByLabelText("Bredd (mm)")).toBeEnabled();
    expect(screen.getByRole("button", { name: "Förbered tillverkning" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Skapa granskningspaket" })).toBeEnabled();
    const cleanLeaving = new Event("beforeunload", { cancelable: true });
    fireEvent(window, cleanLeaving);
    expect(cleanLeaving.defaultPrevented).toBe(false);
    expect(vi.mocked(api.previewFurniture).mock.lastCall?.[0].design.material.measured_thickness_um).toBe(18_000);
  });

  it("byter möbeltyp först efter att användaren valt att lämna profilförslaget", async () => {
    const api = setup();
    render(<FurnitureStudio api={api} principal={principal} />);
    await screen.findByText("5 delar");
    fireEvent.change(screen.getByLabelText("Uppmätt skivtjocklek (mm)"), { target: { value: "17.801" } });
    fireEvent.change(screen.getByLabelText("Möbeltyp"), { target: { value: "shelving" } });
    expect(screen.getByLabelText("Möbeltyp")).toHaveValue("table");
    fireEvent.click(screen.getByRole("button", { name: "Fortsätt utan att spara" }));
    await screen.findByText("5 delar");
    expect(screen.getByLabelText("Möbeltyp")).toHaveValue("shelving");
    expect(screen.getByLabelText("Uppmätt skivtjocklek (mm)")).toHaveValue(18);
    expect(screen.getByLabelText("Bredd (mm)")).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Återställ profilförslag" })).toBeNull();
    expect(screen.getByRole("button", { name: "Spara revision" })).toBeEnabled();
  });

  it("behåller ogiltiga profilfält som väntande ändringar även när ett annat fält ändras", async () => {
    const api = setup();
    render(<FurnitureStudio api={api} principal={principal} />);
    await screen.findByText("5 delar");
    fireEvent.change(screen.getByLabelText("Uppmätt skivtjocklek (mm)"), { target: { value: "18.0001" } });
    expect(screen.getByRole("alert")).toHaveTextContent("högst tre decimaler");
    expect(screen.getByLabelText("Uppmätt skivtjocklek (mm)")).toHaveValue(18.0001);
    expect(screen.getByLabelText("Bredd (mm)")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Spara revision" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/Batch-ID/), { target: { value: "ny-batch" } });
    expect(screen.getByRole("button", { name: "Kontrollera profilbyte" })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("högst tre decimaler");
    fireEvent.change(screen.getByLabelText("Uppmätt skivtjocklek (mm)"), { target: { value: "18" } });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("button", { name: "Kontrollera profilbyte" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Spara revision" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Återställ profilförslag" }));
    expect(screen.getByLabelText(/Batch-ID/)).toHaveValue("");
    expect(screen.getByRole("button", { name: "Spara revision" })).toBeEnabled();
  });

  it("ignorerar ett sent profilkontrollsvar efter att förslaget återställts", async () => {
    const api = setup();
    let finish!: (value: Awaited<ReturnType<CustombuildApiClient["compareFurnitureProfiles"]>>) => void;
    vi.spyOn(api, "compareFurnitureProfiles").mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    render(<FurnitureStudio api={api} principal={principal} />);
    await screen.findByText("5 delar");
    fireEvent.change(screen.getByLabelText("Uppmätt skivtjocklek (mm)"), { target: { value: "17.801" } });
    fireEvent.click(screen.getByRole("button", { name: "Kontrollera profilbyte" }));
    expect(screen.getByRole("button", { name: "Kontrollerar…" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Återställ profilförslag" }));
    const proposed = newFurnitureWorkspace("table"); proposed.design.material.measured_thickness_um = 17_801;
    await act(async () => finish({ state: "not_qualified", can_apply: true, intent_preserved: true,
      message: "Sent förslag", proposed: preview(proposed), changed_part_ids: [], changed_dependencies: ["material"],
      invalidated_reviews: ["construction", "cam"] }));
    expect(screen.queryByRole("button", { name: "Använd profilbytet i designen" })).toBeNull();
    expect(screen.getByLabelText("Uppmätt skivtjocklek (mm)")).toHaveValue(18);
    expect(screen.getByLabelText("Bredd (mm)")).toBeEnabled();
    expect(screen.getByRole("button", { name: "Spara revision" })).toBeEnabled();
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

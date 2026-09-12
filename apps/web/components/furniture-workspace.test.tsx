import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CustombuildApiClient, type CurrentPrincipal } from "@/lib/api-client";
import { newFurnitureWorkspace, type FurniturePreview, type FurnitureWorkspace } from "@/lib/furniture-workspace";
import { furnitureDraftRecoveryKey, parseFurnitureDraftRecovery } from "@/lib/furniture-draft-recovery";
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
  vi.spyOn(api, "validateFurnitureWorkspace").mockImplementation(async workspace => ({ workspace, validation_scope: "workspace_structure", production_qualified: false, physical_cutting_authorized: false }));
  vi.spyOn(api, "previewFurniture").mockImplementation(async workspace => preview(workspace));
  vi.spyOn(api, "furnitureHistory").mockResolvedValue({ items: [], next_offset: null });
  return api;
}
beforeEach(() => window.localStorage.clear());
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

describe("möbelstudions revisions- och profilflöde", () => {
  it("behåller materialens råformat och ogiltiga måttfält vid maskinbyte tills profilen granskas", async () => {
    const api = setup();
    const catalog = await api.furnitureCatalog();
    vi.mocked(api.furnitureCatalog).mockResolvedValue({ ...catalog, machines: [
      { profile_id: "small", version: "1.0.0-validation", name: "Liten", production_qualified: false },
      { profile_id: "large", version: "1.0.0-validation", name: "Stor", production_qualified: false },
    ] });
    const compare = vi.spyOn(api, "compareFurnitureProfiles").mockImplementation(async (_before, proposed) => ({
      state: "not_qualified", message: "Granska format", can_apply: true, changed_dependencies: ["manufacturing"],
      invalidated_reviews: ["cam"], proposed: preview(proposed),
    }));
    render(<FurnitureStudio api={api} principal={principal} />);
    await screen.findByText("5 delar");
    fireEvent.change(screen.getByLabelText("Tillverkningsprofil"), { target: { value: "small" } });
    fireEvent.click(screen.getByRole("checkbox", { name: "Eget råformat för birch-plywood · 18 mm" }));
    const own = within(screen.getByRole("group", { name: "birch-plywood · 18 mm" }));
    fireEvent.change(own.getByLabelText("Råskivans bredd (mm)"), { target: { value: "1100.001" } });
    fireEvent.change(own.getByLabelText("Fiberriktning på råskivan"), { target: { value: "x" } });
    fireEvent.change(own.getByLabelText("Kantmarginal per sida (mm)"), { target: { value: "5.0001" } });
    fireEvent.change(screen.getByLabelText("Tillverkningsprofil"), { target: { value: "large" } });
    expect(own.getByLabelText("Råskivans bredd (mm)")).toHaveValue(1100.001);
    expect(own.getByLabelText("Kantmarginal per sida (mm)")).toHaveValue(5.0001);
    expect(screen.getByRole("button", { name: "Kontrollera profilbyte" })).toBeDisabled();
    fireEvent.change(own.getByLabelText("Kantmarginal per sida (mm)"), { target: { value: "5" } });
    fireEvent.click(screen.getByRole("button", { name: "Kontrollera profilbyte" }));
    await screen.findByText("Konsekvenser av profilbytet");
    expect(compare.mock.lastCall?.[1].manufacturing).toMatchObject({ machine_profile_id: "large",
      material_stocks: [{ material_id: "birch-plywood", measured_thickness_um: 18_000,
        stock_width_um: 1_100_001, stock_grain_axis: "x", edge_margin_um: 5_000 }] });
    expect(api.previewFurniture).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Spara revision" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Använd profilbytet i designen" }));
    await waitFor(() => expect(api.previewFurniture).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.previewFurniture).mock.lastCall?.[0].design).toEqual(newFurnitureWorkspace("table").design);
    expect(vi.mocked(api.previewFurniture).mock.lastCall?.[0].manufacturing).toEqual(compare.mock.lastCall?.[1].manufacturing);
  });

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
    const checked = vi.mocked(api.validateFurnitureWorkspace).mock.lastCall?.[0];
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
    await waitFor(() => expect(screen.getByRole("button", { name: "Spara revision" })).toBeEnabled());
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

// Recovery must preserve editing intent without converting it into a reviewed revision.
describe("lokal återställning", () => {
  it("återställer ogiltiga kundmått, indelning och profilförslag efter omladdning utan att tillämpa dem", async () => {
    const api = setup();
    let view = render(<FurnitureStudio api={api} principal={principal} />);
    await screen.findByText("5 delar");
    fireEvent.change(screen.getByLabelText("Möbeltyp"), { target: { value: "shelving" } });
    fireEvent.click(screen.getByLabelText("Ange separata kundmått"));
    fireEvent.change(screen.getByLabelText("Kundlängd inklusive reserverat utrymme (mm)"), { target: { value: "4340.0001" } });
    fireEvent.change(screen.getByLabelText("Bredd (mm)"), { target: { value: "1100.0001" } });
    fireEvent.change(screen.getByLabelText("Fackbredder (%)"), { target: { value: "17; 81" } });
    fireEvent.change(screen.getByLabelText("Uppmätt skivtjocklek (mm)"), { target: { value: "18.0001" } });
    fireEvent.change(screen.getAllByLabelText(/Batch-ID/)[0]!, { target: { value: "Min obearbetade batch" } });
    await waitFor(() => expect(window.localStorage.getItem(furnitureDraftRecoveryKey(api.baseUrl, principal))).toContain("Min obearbetade batch"));
    const saved = parseFurnitureDraftRecovery(window.localStorage.getItem(furnitureDraftRecoveryKey(api.baseUrl, principal))!);
    expect(saved.workspace.design.material.batch_id).toBeNull();
    view.unmount();
    vi.mocked(api.previewFurniture).mockClear();
    view = render(<FurnitureStudio api={api} principal={principal} />);
    expect(screen.getByLabelText("Bredd (mm)")).toBeDisabled();
    expect(api.previewFurniture).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Återställ utkast" }));
    await screen.findByText(/Utkastet är återställt/);
    expect(screen.getByLabelText("Kundlängd inklusive reserverat utrymme (mm)")).toHaveValue(4340.0001);
    expect(screen.getByLabelText("Bredd (mm)")).toHaveValue(1100.0001);
    expect(screen.getByLabelText("Fackbredder (%)")).toHaveValue("17; 81");
    expect(screen.getByLabelText("Uppmätt skivtjocklek (mm)")).toHaveValue(18.0001);
    expect(screen.getAllByLabelText(/Batch-ID/)[0]).toHaveValue("Min obearbetade batch");
    expect(screen.getByRole("button", { name: "Spara revision" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Skapa granskningspaket" })).toBeDisabled();
    expect(api.validateFurnitureWorkspace).toHaveBeenCalledWith(saved.workspace);
    expect(api.validateFurnitureWorkspace).toHaveBeenCalledWith(saved.profile!.proposed);
    fireEvent.click(screen.getByRole("button", { name: "Återställ profilförslag" }));
    fireEvent.change(screen.getByLabelText("Bredd (mm)"), { target: { value: "1100" } });
    fireEvent.change(screen.getByLabelText("Kundlängd inklusive reserverat utrymme (mm)"), { target: { value: "4340" } });
    fireEvent.change(screen.getByLabelText("Fackbredder (%)"), { target: { value: "50; 50" } });
    await screen.findByText("5 delar");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("button", { name: "Spara revision" })).toBeEnabled();
    view.unmount();
  });

  it("bevarar nedladdningsbar arbetsfil vid API-fel och kan försöka igen utan att nollställa måtten", async () => {
    const api = setup();
    vi.mocked(api.previewFurniture).mockRejectedValue(new Error("Nätverket saknas"));
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const url = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:local-workspace");
    render(<FurnitureStudio api={api} principal={principal} />);
    fireEvent.change(screen.getByLabelText("Bredd (mm)"), { target: { value: "1437.001" } });
    await screen.findByText(/Nätverket saknas/);
    fireEvent.click(screen.getByRole("button", { name: "Spara arbetsfil" }));
    expect(url).toHaveBeenCalled(); expect(click).toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Spara revision" })).toBeDisabled();
    vi.mocked(api.previewFurniture).mockImplementation(async workspace => preview(workspace));
    fireEvent.click(screen.getByRole("button", { name: "Försök igen" }));
    await screen.findByText("5 delar");
    expect(screen.getByLabelText("Bredd (mm)")).toHaveValue(1437.001);
    expect(screen.getByRole("button", { name: "Spara revision" })).toBeEnabled();
  });

  it("bevarar korrupta kopior och läser inte en annan användares, organisations eller API:s utkast", async () => {
    const api = setup();
    const key = furnitureDraftRecoveryKey(api.baseUrl, principal);
    window.localStorage.setItem(key, "korrupt men viktig fil");
    let view = render(<FurnitureStudio api={api} principal={principal} />);
    fireEvent.click(screen.getByRole("button", { name: "Återställ utkast" }));
    await screen.findByRole("alert");
    expect(window.localStorage.getItem(key)).toBe("korrupt men viktig fil");
    expect(screen.getByRole("button", { name: "Hämta återställningskopian" })).toBeEnabled();
    view.unmount();
    for (const changed of [{ ...principal, user_id: "annan" }, { ...principal, organization_id: "annan" }]) {
      view = render(<FurnitureStudio api={api} principal={changed} />);
      await screen.findByText("5 delar");
      expect(screen.queryByRole("button", { name: "Återställ utkast" })).toBeNull();
      view.unmount();
    }
    const otherApi = setup(); Object.defineProperty(otherApi, "baseUrl", { value: "https://other.example.test" });
    view = render(<FurnitureStudio api={otherApi} principal={principal} />);
    await screen.findByText("5 delar");
    expect(screen.queryByRole("button", { name: "Återställ utkast" })).toBeNull();
    expect(window.localStorage.getItem(key)).toBe("korrupt men viktig fil");
    view.unmount();
  });

  it("varnar för lagringsfel och skriver aldrig över en nyare kopia från en annan flik", async () => {
    const api = setup();
    const key = furnitureDraftRecoveryKey(api.baseUrl, principal);
    render(<FurnitureStudio api={api} principal={principal} />);
    await screen.findByText("5 delar");
    window.localStorage.setItem(key, "arbete i annan flik");
    fireEvent.change(screen.getByLabelText("Bredd (mm)"), { target: { value: "1300" } });
    await screen.findByText(/En annan flik har ändrat/);
    expect(window.localStorage.getItem(key)).toBe("arbete i annan flik");
    expect(screen.getByRole("button", { name: "Spara återställningsfil" })).toBeEnabled();
  });

  it("skyddar namnädringar och interna länkar utan att stänga av framtida navigationsvakter", async () => {
    const api = setup();
    render(<><a href="/different-workspace">Annan arbetsyta</a><FurnitureStudio api={api} principal={principal} /></>);
    await screen.findByText("5 delar");
    fireEvent.change(screen.getByLabelText("Projektnamn"), { target: { value: "Viktigt namn" } });
    const before = new Event("beforeunload", { cancelable: true }); fireEvent(window, before);
    expect(before.defaultPrevented).toBe(true);
    fireEvent.click(screen.getByRole("link", { name: "Annan arbetsyta" }));
    expect(screen.getByRole("alertdialog", { name: "Osparad design" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Tillbaka" }));
    fireEvent.change(screen.getByLabelText("Möbeltyp"), { target: { value: "shelving" } });
    fireEvent.click(screen.getByRole("button", { name: "Fortsätt utan att spara" }));
    fireEvent.change(screen.getByLabelText("Bredd (mm)"), { target: { value: "1700" } });
    const next = new Event("beforeunload", { cancelable: true }); fireEvent(window, next);
    expect(next.defaultPrevented).toBe(true);
  });
});


describe("råa antal och last", () => {
  it("bevarar tomt antal och tom last efter återställning utan att skriva noll till designen", async () => {
    const api = setup();
    let view = render(<FurnitureStudio api={api} principal={principal} />);
    await screen.findByText("5 delar");
    fireEvent.change(screen.getByLabelText("Möbeltyp"), { target: { value: "chest_of_drawers" } });
    await screen.findByText("5 delar");
    const before = vi.mocked(api.previewFurniture).mock.lastCall![0].design.intent;
    fireEvent.change(screen.getByLabelText("Antal lådor"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Last per låda (kg)"), { target: { value: "" } });
    const key = furnitureDraftRecoveryKey(api.baseUrl, principal);
    await waitFor(() => expect(window.localStorage.getItem(key)).toContain('"carcass.drawer_load_n":""'));
    expect(parseFurnitureDraftRecovery(window.localStorage.getItem(key)!).workspace.design.intent).toEqual(before);
    expect(screen.getByLabelText("Antal lådor")).toHaveValue(null);
    expect(screen.getByLabelText("Last per låda (kg)")).toHaveValue(null);
    expect(screen.getByRole("button", { name: "Spara revision" })).toBeDisabled();
    view.unmount();
    view = render(<FurnitureStudio api={api} principal={principal} />);
    fireEvent.click(screen.getByRole("button", { name: "Återställ utkast" }));
    await screen.findByText(/Utkastet är återställt/);
    expect(screen.getByLabelText("Antal lådor")).toHaveValue(null);
    expect(screen.getByLabelText("Last per låda (kg)")).toHaveValue(null);
    expect(screen.getByRole("button", { name: "Spara revision" })).toBeDisabled();
    for (const raw of ["0", "9", "2.5"]) {
      fireEvent.change(screen.getByLabelText("Antal lådor"), { target: { value: raw } });
      expect(screen.getByRole("button", { name: "Spara revision" })).toBeDisabled();
    }
    fireEvent.change(screen.getByLabelText("Antal lådor"), { target: { value: "3" } });
    fireEvent.change(screen.getByLabelText("Last per låda (kg)"), { target: { value: "0" } });
    await screen.findByText("5 delar");
    expect(vi.mocked(api.previewFurniture).mock.lastCall![0].design.intent).toMatchObject({ drawer_count: 3, drawer_load_n: 0 });
    expect(screen.getByRole("button", { name: "Spara revision" })).toBeEnabled();
    view.unmount();
  });
});


it("låter en schemagiltig men omöjlig form återställas och rättas även när CAD-förhandsvisningen misslyckas", async () => {
  const api = setup();
  const workspace = newFurnitureWorkspace("table");
  workspace.design.intent.width_um = 20_000;
  const key = furnitureDraftRecoveryKey(api.baseUrl, principal);
  window.localStorage.setItem(key, JSON.stringify({ version: 1, workspace,
    name: "För smalt bord", revision: 0, updatedAt: new Date().toISOString() }));
  vi.mocked(api.previewFurniture).mockImplementation(async value => {
    if (value.design.intent.width_um < 36_000) throw new Error("Bredden rymmer inte gavlarna.");
    return preview(value);
  });
  render(<FurnitureStudio api={api} principal={principal} />);
  fireEvent.click(screen.getByRole("button", { name: "Återställ utkast" }));
  await screen.findByText(/Bredden rymmer inte gavlarna/);
  expect(screen.getByLabelText("Bredd (mm)")).toBeEnabled();
  expect(screen.getByLabelText("Bredd (mm)")).toHaveValue(20);
  expect(screen.getByRole("button", { name: "Spara revision" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Bredd (mm)"), { target: { value: "1000" } });
  await screen.findByText("5 delar");
  expect(screen.getByRole("button", { name: "Spara revision" })).toBeEnabled();
});


describe("återställning bevarar schemagiltigt arbetsutkast vid motstridiga fält", () => {
  it("behåller för stort montageutrymme som råfält även när motsatt sida är okänd", async () => {
    const api = setup();
    let view = render(<FurnitureStudio api={api} principal={principal} />);
    await screen.findByText("5 delar");
    fireEvent.change(screen.getByLabelText("Djup (mm)"), { target: { value: "280" } });
    fireEvent.click(screen.getByLabelText("Ange separata kundmått"));
    fireEvent.change(screen.getByLabelText("Bakom · reserverat (mm)"), { target: { value: "300" } });
    const key = furnitureDraftRecoveryKey(api.baseUrl, principal);
    await waitFor(() => expect(window.localStorage.getItem(key)).toContain('"installation.rear_allowance_um":"300"'));
    const saved = parseFurnitureDraftRecovery(window.localStorage.getItem(key)!);
    expect(saved.workspace.design.installation).toMatchObject({ depth_um: 280_000,
      rear_allowance_um: null, front_allowance_um: null });
    view.unmount();
    view = render(<FurnitureStudio api={api} principal={principal} />);
    fireEvent.click(screen.getByRole("button", { name: "Återställ utkast" }));
    await screen.findByText(/Utkastet är återställt/);
    expect(screen.getByLabelText("Bakom · reserverat (mm)")).toHaveValue(300);
    expect(screen.getByRole("button", { name: "Spara revision" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Bakom · reserverat (mm)"), { target: { value: "20" } });
    fireEvent.change(screen.getByLabelText("Kunddjup (mm)"), { target: { value: "10" } });
    expect(screen.getByLabelText("Kunddjup (mm)")).toHaveValue(10);
    expect(screen.getByRole("button", { name: "Spara revision" })).toBeDisabled();
    await waitFor(() => expect(parseFurnitureDraftRecovery(window.localStorage.getItem(key)!).workspace.design.installation)
      .toMatchObject({ depth_um: 280_000, rear_allowance_um: 20_000 }));
    view.unmount();
  });

  it("tillämpar inte listreservationer som tar hela utrymmet", async () => {
    const api = setup();
    render(<FurnitureStudio api={api} principal={principal} />);
    await screen.findByText("5 delar");
    fireEvent.change(screen.getByLabelText("Djup (mm)"), { target: { value: "50" } });
    fireEvent.click(screen.getByLabelText("Ange separata kundmått"));
    fireEvent.click(screen.getByLabelText("Ange listprofil"));
    fireEvent.change(screen.getByLabelText("Listbredd/utstick (mm)"), { target: { value: "60" } });
    fireEvent.change(screen.getByLabelText("Listens funktion"), { target: { value: "existing_room_trim" } });
    fireEvent.click(screen.getByLabelText("Bakom möbeln"));
    fireEvent.click(screen.getByRole("button", { name: "Reservera frigång för befintlig list", hidden: true }));
    expect(screen.getByLabelText("Bakom · reserverat (mm)")).toHaveValue(null);
    expect(screen.getByRole("alert")).toHaveTextContent("positivt stommått");
    fireEvent.change(screen.getByLabelText("Listbredd/utstick (mm)"), { target: { value: "20" } });
    fireEvent.click(screen.getByRole("button", { name: "Reservera frigång för befintlig list", hidden: true }));
    expect(screen.getByLabelText("Bakom · reserverat (mm)")).toHaveValue(20);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it.each([
    { width: "4340", load: "200", field: "carcass.shelf_load_per_metre_n", raw: "200", changed: "load" },
    { width: "6000", load: "100", field: "carcass.width_um", raw: "6000", changed: "width" },
  ])("bevarar en för stor radlast från $changed som råtext utan schemabrott", async ({ width, load, field, raw, changed }) => {
    const api = setup();
    let view = render(<FurnitureStudio api={api} principal={principal} />);
    await screen.findByText("5 delar");
    fireEvent.change(screen.getByLabelText("Möbeltyp"), { target: { value: "shelving" } });
    if (changed === "load") fireEvent.change(screen.getByLabelText("Bredd (mm)"), { target: { value: width } });
    fireEvent.change(screen.getByLabelText("Hur anges hyllasten?"), { target: { value: "per_metre" } });
    fireEvent.change(screen.getByLabelText("Last per meter hyllrad (kg/m)"), { target: { value: load } });
    if (changed === "width") fireEvent.change(screen.getByLabelText("Bredd (mm)"), { target: { value: width } });
    const key = furnitureDraftRecoveryKey(api.baseUrl, principal);
    await waitFor(() => expect(parseFurnitureDraftRecovery(window.localStorage.getItem(key)!).inputDrafts?.[field]).toBe(raw));
    const saved = parseFurnitureDraftRecovery(window.localStorage.getItem(key)!);
    expect(Math.ceil(saved.workspace.design.intent.width_um * (saved.workspace.design.intent.shelf_load_per_metre_n ?? 0)/1_000_000)).toBeLessThanOrEqual(5_000);
    view.unmount();
    view = render(<FurnitureStudio api={api} principal={principal} />);
    fireEvent.click(screen.getByRole("button", { name: "Återställ utkast" }));
    await screen.findByText(/Utkastet är återställt/);
    expect(screen.getByLabelText(changed === "width" ? "Bredd (mm)" : "Last per meter hyllrad (kg/m)")).toHaveValue(Number(raw));
    expect(screen.getByRole("button", { name: "Spara revision" })).toBeDisabled();
    view.unmount();
  });

  it("återställer ogiltig tjocklek och batchtext utan att förgifta profilförslaget", async () => {
    const api = setup();
    let view = render(<FurnitureStudio api={api} principal={principal} />);
    await screen.findByText("5 delar");
    fireEvent.change(screen.getByLabelText("Uppmätt skivtjocklek (mm)"), { target: { value: "101" } });
    fireEvent.change(screen.getByLabelText(/Batch-ID/), { target: { value: "Batch med mellanslag" } });
    const key = furnitureDraftRecoveryKey(api.baseUrl, principal);
    await waitFor(() => expect(window.localStorage.getItem(key)).toContain("Batch med mellanslag"));
    const saved = parseFurnitureDraftRecovery(window.localStorage.getItem(key)!);
    expect(saved.profile!.proposed.design.material).toMatchObject({ measured_thickness_um: 18_000, batch_id: null });
    view.unmount();
    view = render(<FurnitureStudio api={api} principal={principal} />);
    fireEvent.click(screen.getByRole("button", { name: "Återställ utkast" }));
    await screen.findByText(/Utkastet är återställt/);
    expect(screen.getByLabelText("Uppmätt skivtjocklek (mm)")).toHaveValue(101);
    expect(screen.getByLabelText(/Batch-ID/)).toHaveValue("Batch med mellanslag");
    expect(screen.getByRole("button", { name: "Kontrollera profilbyte" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Uppmätt skivtjocklek (mm)"), { target: { value: "0.5" } });
    expect(screen.getByRole("button", { name: "Kontrollera profilbyte" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Uppmätt skivtjocklek (mm)"), { target: { value: "17.9" } });
    fireEvent.change(screen.getByLabelText(/Batch-ID/), { target: { value: "Batch-42:a" } });
    expect(screen.getByRole("button", { name: "Kontrollera profilbyte" })).toBeEnabled();
    expect(screen.queryByRole("alert")).toBeNull();
    view.unmount();
  });
});

it("avvisar lastöverskridanden från byte av lastbasis och omräkning av kundmått", async () => {
  const api = setup();
  render(<FurnitureStudio api={api} principal={principal} />);
  await screen.findByText("5 delar");
  fireEvent.change(screen.getByLabelText("Möbeltyp"), { target: { value: "shelving" } });
  fireEvent.change(screen.getByLabelText("Bredd (mm)"), { target: { value: "20" } });
  fireEvent.change(screen.getByLabelText("Hur anges hyllasten?"), { target: { value: "per_metre" } });
  expect(screen.getByLabelText("Hur anges hyllasten?")).toHaveValue("per_row");
  expect(screen.getByRole("alert")).toHaveTextContent("5000 N");
  fireEvent.change(screen.getByLabelText("Bredd (mm)"), { target: { value: "900" } });
  fireEvent.change(screen.getByLabelText("Hur anges hyllasten?"), { target: { value: "per_metre" } });
  fireEvent.change(screen.getByLabelText("Last per meter hyllrad (kg/m)"), { target: { value: "100" } });
  fireEvent.click(screen.getByLabelText("Ange separata kundmått"));
  fireEvent.change(screen.getByLabelText("Kundlängd inklusive reserverat utrymme (mm)"), { target: { value: "6000" } });
  for (const side of ["Vänster", "Höger", "Ovanför", "Under", "Framför", "Bakom"]) {
    fireEvent.change(screen.getByLabelText(`${side} · reserverat (mm)`), { target: { value: "0" } });
  }
  fireEvent.click(screen.getByRole("button", { name: "Räkna om stommen från kundmåtten", hidden: true }));
  expect(screen.getByLabelText("Bredd (mm)")).toHaveValue(900);
  expect(screen.getByRole("alert")).toHaveTextContent("5000 N");
  const key = furnitureDraftRecoveryKey(api.baseUrl, principal);
  await waitFor(() => expect(parseFurnitureDraftRecovery(window.localStorage.getItem(key)!).workspace.design)
    .toMatchObject({ intent: { width_um: 900_000, shelf_load_per_metre_n: 981 }, installation: { width_um: 6_000_000 } }));
});


describe("Web Locks in the furniture editor", () => {
  it("shows unsupported coordination without blocking editing or offering false local-save success", async () => {
    Object.defineProperty(navigator, "locks", { configurable: true, value: undefined });
    const api = setup();
    render(<FurnitureStudio api={api} principal={principal} />);
    fireEvent.change(screen.getByLabelText("Bredd (mm)"), { target: { value: "1437" } });
    await screen.findByText(/stöder inte säker samordning/);
    expect(screen.getByLabelText("Bredd (mm)")).toHaveValue(1437);
    expect(screen.getByRole("button", { name: "Spara återställningsfil" })).toBeEnabled();
    expect(window.localStorage.getItem(furnitureDraftRecoveryKey(api.baseUrl, principal))).toBeNull();
  });

  it("cancels a queued discard on unmount and keeps the stored recovery intact", async () => {
    const api = setup();
    const key = furnitureDraftRecoveryKey(api.baseUrl, principal);
    const raw = JSON.stringify({ version: 1, workspace: newFurnitureWorkspace("table"),
      name: "Behåll mitt utkast", revision: 0, updatedAt: new Date().toISOString() });
    window.localStorage.setItem(key, raw);
    let release!: () => void;
    const holder = navigator.locks.request(`custombuild:recovery-write:${key}`, { mode: "exclusive" },
      () => new Promise<void>(resolve => { release = resolve; }));
    await waitFor(() => expect(release).toBeTypeOf("function"));
    const mounted = render(<FurnitureStudio api={api} principal={principal} />);
    fireEvent.click(screen.getByRole("button", { name: "Ta bort återställningskopian" }));
    expect(screen.getByRole("button", { name: "Ta bort återställningskopian" })).toBeDisabled();
    expect(window.localStorage.getItem(key)).toBe(raw);
    mounted.unmount();
    await act(async () => { release(); await holder; });
    expect(window.localStorage.getItem(key)).toBe(raw);
  });

  it("cancels obsolete queued autosaves and persists only the current edit", async () => {
    const api = setup();
    const key = furnitureDraftRecoveryKey(api.baseUrl, principal);
    let release!: () => void;
    const holder = navigator.locks.request(`custombuild:recovery-write:${key}`, { mode: "exclusive" },
      () => new Promise<void>(resolve => { release = resolve; }));
    await waitFor(() => expect(release).toBeTypeOf("function"));
    render(<FurnitureStudio api={api} principal={principal} />);
    fireEvent.change(screen.getByLabelText("Bredd (mm)"), { target: { value: "1200" } });
    await act(async () => { await Promise.resolve(); });
    fireEvent.change(screen.getByLabelText("Bredd (mm)"), { target: { value: "1400" } });
    await act(async () => { await Promise.resolve(); });
    expect(window.localStorage.getItem(key)).toBeNull();
    await act(async () => { release(); await holder; });
    await waitFor(() => expect(parseFurnitureDraftRecovery(window.localStorage.getItem(key)!)
      .workspace.design.intent.width_um).toBe(1_400_000));
    expect(screen.queryByText(/En annan flik har ändrat/)).toBeNull();
  });
});


describe("save acknowledgement and recovery completion", () => {
  function saveApi() {
    const api = setup();
    vi.spyOn(api, "createProject").mockResolvedValue({ id: "saved-project", name: "Mitt bord", furniture_type: "table",
      current_revision: 0, description: "", archived: false, created_at: "2026-09-07T12:00:00Z",
      updated_at: "2026-09-07T12:00:00Z" });
    const save = vi.spyOn(api, "saveFurnitureDraft").mockImplementation(async (id, revision, workspace) => {
      const saved = { ...workspace, design: { ...workspace.design, design_id: id, revision: revision + 1 } };
      return { project_id: id, revision: revision + 1, workspace: saved, preview: preview(saved) };
    });
    return { api, save };
  }

  async function holdRecovery(key: string) {
    let release!: () => void;
    const holder = navigator.locks.request(`custombuild:recovery-write:${key}`, { mode: "exclusive" },
      () => new Promise<void>(resolve => { release = resolve; }));
    await waitFor(() => expect(release).toBeTypeOf("function"));
    return async () => { release(); await holder; };
  }

  it("announces a saved revision only after its recovery cleanup completes, so immediate reload is clean", async () => {
    const { api, save } = saveApi();
    let mounted = render(<FurnitureStudio api={api} principal={principal} />);
    await screen.findByText("5 delar");
    fireEvent.change(screen.getByLabelText("Bredd (mm)"), { target: { value: "1437" } });
    await screen.findByText("5 delar");
    const key = furnitureDraftRecoveryKey(api.baseUrl, principal);
    await waitFor(() => expect(window.localStorage.getItem(key)).toContain('"width_um":1437000'));
    const before = window.localStorage.getItem(key);
    const release = await holdRecovery(key);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Spara revision" })); });
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    expect(screen.queryByText("Revision 1 är sparad.")).toBeNull();
    expect(screen.getByRole("button", { name: "Arbetar…" })).toBeDisabled();
    expect(window.localStorage.getItem(key)).toBe(before);
    const unload = new Event("beforeunload", { cancelable: true });
    fireEvent(window, unload);
    expect(unload.defaultPrevented).toBe(true);
    await act(release);
    await screen.findByText("Revision 1 är sparad.");
    expect(window.localStorage.getItem(key)).toBeNull();
    mounted.unmount();
    mounted = render(<FurnitureStudio api={api} principal={principal} />);
    await screen.findByText("5 delar");
    expect(screen.queryByRole("region", { name: "Återställ ditt utkast" })).toBeNull();
    expect(screen.getByLabelText("Öppna möbelprojekt")).toBeEnabled();
    mounted.unmount();
  });

  it("reports server success with a cleanup warning when another tab owns the newer copy", async () => {
    const { api, save } = saveApi();
    render(<FurnitureStudio api={api} principal={principal} />);
    await screen.findByText("5 delar");
    fireEvent.change(screen.getByLabelText("Bredd (mm)"), { target: { value: "1437" } });
    await screen.findByText("5 delar");
    const key = furnitureDraftRecoveryKey(api.baseUrl, principal);
    await waitFor(() => expect(window.localStorage.getItem(key)).toContain('"width_um":1437000'));
    const release = await holdRecovery(key);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Spara revision" })); });
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    const foreign = "a newer copy owned by the other lock holder";
    window.localStorage.setItem(key, foreign);
    await act(release);
    await screen.findByText(/sparad på servern, men återställningskopian kunde inte rensas/);
    expect(screen.queryByText("Revision 1 är sparad.")).toBeNull();
    expect(window.localStorage.getItem(key)).toBe(foreign);
    expect(screen.getByLabelText("Bredd (mm)")).toHaveValue(1437);
  });

  it.each(["server", "cleanup"] as const)("preserves newer raw fields and profile proposals arriving during %s", async phase => {
    const { api, save } = saveApi();
    let finishServer!: (value: Awaited<ReturnType<CustombuildApiClient["saveFurnitureDraft"]>>) => void;
    if (phase === "server") save.mockImplementationOnce(() => new Promise(resolve => { finishServer = resolve; }));
    render(<FurnitureStudio api={api} principal={principal} />);
    await screen.findByText("5 delar");
    fireEvent.change(screen.getByLabelText("Bredd (mm)"), { target: { value: "1437" } });
    await screen.findByText("5 delar");
    const key = furnitureDraftRecoveryKey(api.baseUrl, principal);
    await waitFor(() => expect(window.localStorage.getItem(key)).toContain('"width_um":1437000'));
    const release = phase === "cleanup" ? await holdRecovery(key) : undefined;
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Spara revision" })); });
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    // Model an input event already queued when the save action disabled its controls.
    fireEvent.change(screen.getByLabelText("Bredd (mm)"), { target: { value: "1437.0001" } });
    fireEvent.change(screen.getByLabelText(/Batch-ID/), { target: { value: "Ny batchtext" } });
    if (phase === "server") {
      const submitted = save.mock.calls[0]![2];
      const saved = { ...submitted, design: { ...submitted.design, design_id: "saved-project", revision: 1 } };
      await act(async () => finishServer({ project_id: "saved-project", revision: 1, workspace: saved, preview: preview(saved) }));
    } else {
      await act(release!);
    }
    await screen.findByText(/Nyare ändringar finns kvar i utkastet/);
    expect(screen.queryByText("Revision 1 är sparad.")).toBeNull();
    expect(screen.getByLabelText("Bredd (mm)")).toHaveValue(1437.0001);
    expect(screen.getByLabelText(/Batch-ID/)).toHaveValue("Ny batchtext");
    await waitFor(() => {
      const recovered = parseFurnitureDraftRecovery(window.localStorage.getItem(key)!);
      expect(recovered.revision).toBe(1);
      expect(recovered.workspace.design.intent.width_um).toBe(1_437_000);
      expect(recovered.inputDrafts?.["carcass.width_um"]).toBe("1437.0001");
      expect(recovered.profile?.inputDrafts["material.batch_id"]).toBe("Ny batchtext");
      expect(recovered.profile?.proposed.design.material.batch_id).toBeNull();
    });
    expect(screen.getByRole("button", { name: "Spara revision" })).toBeDisabled();
  });
});

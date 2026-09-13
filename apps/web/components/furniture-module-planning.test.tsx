import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CustombuildApiClient } from "@/lib/api-client";
import { newFurnitureWorkspace, type FurniturePreview, type FurnitureWorkspace } from "@/lib/furniture-workspace";
import type { FurnitureModuleGrid, FurnitureModulePlan } from "@/lib/furniture-module-plan";
import { FurnitureModulePlanningPanel } from "./furniture-module-planning";

const designHash = "a".repeat(64);
const initialGrid: FurnitureModuleGrid = { columns: 2, rows: 1, gap_um: 0, shelf_count_per_row: [4], divider_count_per_module: 0 };

function sourceWorkspace(): FurnitureWorkspace {
  const source = newFurnitureWorkspace("shelving");
  Object.assign(source.design.intent, { width_um: 4_340_000, height_um: 2_540_000, depth_um: 280_000 });
  source.design.installation = { width_um: 4_340_000, height_um: 2_540_000, depth_um: 280_000,
    width_includes_trim: true, trim_profile: { height_um: 90_000, width_um: 20_000, use: "unassigned", walls: [] },
    left_allowance_um: null, right_allowance_um: null, top_allowance_um: null, bottom_allowance_um: null,
    front_allowance_um: null, rear_allowance_um: null };
  source.manufacturing = { machine_profile_id: "custombuild-router-1325-linuxcnc",
    machine_profile_version: "1.0.0-validation", stock_width_um: 1_220_000, stock_height_um: 2_440_000, stock_grain_axis: "y" };
  return source;
}

function plan(source = sourceWorkspace(), grid = initialGrid): FurnitureModulePlan {
  const sourceHash = "b".repeat(64), planHash = "c".repeat(64);
  const allocate = (total: number, count: number) => Array.from({ length: count }, (_, index) =>
    Math.floor((total - grid.gap_um * (count - 1)) / count) + (index < (total - grid.gap_um * (count - 1)) % count ? 1 : 0));
  const widths = allocate(source.design.intent.width_um, grid.columns), heights = allocate(source.design.intent.height_um, grid.rows);
  const requirements = [{ code: "SOURCE_INSTALLATION_ALLOWANCES_REQUIRED", status: "blocked" as const,
    message: "Montageutrymmet behöver mätas." }, { code: "MODULE_CONNECTIONS_REQUIRED", status: "required" as const, message: "Förband återstår." }];
  const result: FurnitureModulePlan = {
    schema_version: "custombuild.furniture-module-plan.v1", state: "available", code: "MODULE_PLAN_AVAILABLE",
    message: `${grid.rows * grid.columns} nya stommoduler är beräknade som separata utkast.`, source_design_hash: designHash,
    source: { workspace: structuredClone(source), workspace_hash: sourceHash, design_hash: designHash, installation: source.design.installation ?? null },
    grid: structuredClone(grid), plan_hash: planHash, modules: [],
    layout: { column_widths_um: widths, row_heights_um: heights, gap_um: grid.gap_um,
      envelope: { width_um: source.design.intent.width_um, height_um: source.design.intent.height_um, depth_um: source.design.intent.depth_um } },
    requirements, review_status: "blocked", can_export_drafts: true, can_apply: false,
    production_qualified: false, physical_cutting_authorized: false,
  };
  for (let row = 0; row < grid.rows; row++) for (let column = 0; column < grid.columns; column++) {
    const moduleId = `module-${planHash.slice(0, 24)}-r${row + 1}-c${column + 1}`;
    const workspace = structuredClone(source);
    Object.assign(workspace.design, { design_id: moduleId, revision: 1 });
    Object.assign(workspace.design.intent, { width_um: widths[column]!, height_um: heights[row]!,
      shelf_count: grid.shelf_count_per_row[row], divider_count: grid.divider_count_per_module,
      ...(source.design.intent.shelf_load_basis !== "per_metre" ? {
        shelf_load_n: Math.ceil((source.design.intent.shelf_load_n ?? 200) * widths[column]! / widths.reduce((sum, value) => sum + value, 0)),
      } : {}),
    });
    const moduleHash = (row * grid.columns + column + 1).toString(16).padStart(64, "0");
    const preview: FurniturePreview = { schema_version: "custombuild.furniture-preview.v1", workspace: structuredClone(workspace),
      design: { design_hash: moduleHash, intent_hash: "d".repeat(64), parts: [], moving_groups: [], total_weight_g: 12_500 },
      rules: { overall_status: column === 0 ? "WARNING" : "BLOCK", evaluations: [] },
      manufacturing: { state: "requires_change", geometry_compatible: column === 0 ? true : false, issues: [] },
      production_qualified: false, physical_cutting_authorized: false };
    result.modules.push({ module_id: moduleId, row, column,
      dimensions_um: { width_um: widths[column]!, height_um: heights[row]!, depth_um: source.design.intent.depth_um },
      placement: { x_um: widths.slice(0, column).reduce((sum, value) => sum + value, 0) + grid.gap_um * column,
        y_um: 0, z_um: heights.slice(0, row).reduce((sum, value) => sum + value, 0) + grid.gap_um * row },
      workspace, workspace_hash: moduleHash, design_hash: moduleHash, source_workspace_hash: sourceHash,
      source_design_hash: designHash, plan_hash: planHash, preview, weight_g: 12_500, requirements: [...requirements,
        { code: "RULE_FIT", status: "blocked", message: "Fogpassningen behöver kontrolleras." }],
      review_status: "requires_review", production_qualified: false, physical_cutting_authorized: false });
  }
  return result;
}

function client(response = plan()) {
  const api = new CustombuildApiClient("https://api.example.test");
  vi.spyOn(api, "planFurnitureModules").mockResolvedValue(response);
  return api;
}

function panel(api: CustombuildApiClient, workspace = sourceWorkspace(), disabled = false) {
  return <FurnitureModulePlanningPanel api={api} workspace={workspace} designHash={designHash} disabled={disabled} />;
}

function enterGrid(grid = initialGrid) {
  fireEvent.change(screen.getByLabelText("Kolumner, från vänster"), { target: { value: String(grid.columns) } });
  fireEvent.change(screen.getByLabelText("Rader, nerifrån och upp"), { target: { value: String(grid.rows) } });
  fireEvent.change(screen.getByLabelText("Mellanrum mellan moduler (mm)"), { target: { value: String(grid.gap_um / 1_000) } });
  grid.shelf_count_per_row.forEach((count, row) => {
    fireEvent.change(screen.getByLabelText(`Hyllplan per modul, rad ${row + 1}${row === 0 ? " (nederst)" : ""}`), { target: { value: String(count) } });
  });
  fireEvent.change(screen.getByLabelText("Invändiga avdelare per modul"), { target: { value: String(grid.divider_count_per_module) } });
}

const calculate = () => fireEvent.click(screen.getByRole("button", { name: "Beräkna modulplan" }));
const downloadPlan = () => screen.getByRole("button", { name: "Hämta hela modulplanen" });
const defer = <T,>() => {
  let resolve!: (value: T) => void;
  return { promise: new Promise<T>(done => { resolve = done; }), resolve: (value: T) => resolve(value) };
};

function blobText(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result)); reader.onerror = reject; reader.readAsText(blob);
  });
}

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("planering av separata stommoduler", () => {
  it("kräver uttryckligt mellanrum och hyllantal innan någon beräkning skickas", () => {
    const api = client();
    render(panel(api));
    expect(screen.getByRole("spinbutton", { name: "Mellanrum mellan moduler (mm)" })).toHaveValue(null);
    expect(screen.getByRole("button", { name: "Beräkna modulplan" })).toBeDisabled();
    expect(screen.getByText(/Skriv 0 om modulerna/)).toBeVisible();
    fireEvent.change(screen.getByLabelText("Mellanrum mellan moduler (mm)"), { target: { value: "0" } });
    expect(screen.getByRole("button", { name: "Beräkna modulplan" })).toBeDisabled();
    expect(api.planFurnitureModules).not.toHaveBeenCalled();
  });

  it.each([
    ["Kolumner, från vänster", "1", "Välj sammanlagt 2–16 moduler."],
    ["Kolumner, från vänster", "1.5", "Ange 1–8 kolumner"],
    ["Rader, nerifrån och upp", "5", "Ange 1–8 kolumner"],
    ["Invändiga avdelare per modul", "17", "Ange 1–8 kolumner"],
    ["Mellanrum mellan moduler (mm)", "0.0001", "Ange ett mellanrum på 0–100 mm"],
    ["Mellanrum mellan moduler (mm)", "-1", "Ange ett mellanrum på 0–100 mm"],
    ["Hyllplan per modul, rad 1 (nederst)", "3", "Summan av hyllplanen över raderna måste vara minst 4"],
  ])("avvisar ogiltiga val för %s: %s", (label, value, message) => {
    const api = client(); render(panel(api)); enterGrid();
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
    expect(screen.getByText(text => text.startsWith(message))).toBeVisible();
    expect(screen.getByRole("button", { name: "Beräkna modulplan" })).toBeDisabled();
    expect(api.planFurnitureModules).not.toHaveBeenCalled();
  });

  it("begränsar rutnätet till 16 moduler och summerar hyllantal nerifrån och upp", () => {
    const api = client(); render(panel(api));
    enterGrid({ ...initialGrid, columns: 8, rows: 3, shelf_count_per_row: [2, 2, 0] });
    expect(screen.getByText("Välj sammanlagt 2–16 moduler.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Beräkna modulplan" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Kolumner, från vänster"), { target: { value: "5" } });
    expect(screen.getByRole("button", { name: "Beräkna modulplan" })).toBeEnabled();
    fireEvent.change(screen.getByLabelText("Hyllplan per modul, rad 1 (nederst)"), { target: { value: "1" } });
    expect(screen.getByRole("button", { name: "Beräkna modulplan" })).toBeDisabled();
  });

  it("visar måttriktigt rutnät och status, och exporterar granskbara utkast trots installationsblock", async () => {
    const grid = { ...initialGrid, rows: 2, gap_um: 10_001, shelf_count_per_row: [1, 3] };
    const source = sourceWorkspace(), original = structuredClone(source), response = plan(source, grid), api = client(response);
    const create = vi.fn<(blob: Blob) => string>().mockReturnValue("blob:module-plan"), revoke = vi.fn(), clicked: { name: string; href: string }[] = [];
    vi.stubGlobal("URL", { createObjectURL: create, revokeObjectURL: revoke });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) { clicked.push({ name: this.download, href: this.href }); });
    render(panel(api, source)); enterGrid(grid); calculate();
    const table = await screen.findByRole("table", { name: "Modulmått och kontroller" });
    expect(api.planFurnitureModules).toHaveBeenCalledWith(source, grid);
    expect(within(table).getAllByText("12,5")).toHaveLength(4);
    expect(within(table).getAllByText("Kräver granskning")).toHaveLength(2);
    expect(within(table).getAllByText("Kräver åtgärd")).toHaveLength(2);
    expect(within(table).getAllByText("Delarna ryms enskilt")).toHaveLength(2);
    expect(within(table).getAllByText("Format behöver ändras")).toHaveLength(2);
    const drawing = screen.getByRole("img", { name: /Modulindelning/ });
    expect(drawing).toHaveAttribute("viewBox", "0 0 4340 2540");
    const cells = drawing.querySelectorAll("rect");
    expect(cells).toHaveLength(4);
    expect(cells[0]).toHaveAttribute("y", "1275");
    expect(cells[2]).toHaveAttribute("y", "0");
    expect(cells[0]).toHaveAttribute("width", "2165");
    expect(cells[1]).toHaveAttribute("x", "2175.001");
    expect(screen.getByText("Montageutrymmet behöver mätas.", { exact: false })).toBeVisible();
    expect(downloadPlan()).toBeEnabled();
    fireEvent.click(downloadPlan());
    fireEvent.click(screen.getByRole("button", { name: "Hämta arbetsfil för rad 1, kolumn 1" }));
    expect(create).toHaveBeenCalledTimes(2);
    expect(JSON.parse(await blobText(create.mock.calls[0]![0]))).toEqual(response);
    expect(JSON.parse(await blobText(create.mock.calls[1]![0]))).toEqual(response.modules[0]!.workspace);
    expect(clicked.map(file => file.name)).toEqual([
      `custombuild-modulplan-${response.plan_hash.slice(0, 12)}.json`, `custombuild-${response.modules[0]!.module_id}.json`,
    ]);
    expect(source).toEqual(original);
    expect(document.querySelector("a[download]")).toBeNull();
    await act(async () => { await new Promise(done => setTimeout(done, 1)); });
    expect(revoke).toHaveBeenCalledTimes(2);
  });

  it("respekterar att servern nekar utkastexport", async () => {
    const response = plan(); response.can_export_drafts = false;
    render(panel(client(response))); enterGrid(); calculate();
    await screen.findByRole("table");
    expect(downloadPlan()).toBeDisabled();
    expect(screen.getByRole("button", { name: "Hämta arbetsfil för rad 1, kolumn 1" })).toBeDisabled();
  });

  it("visar ett fullständigt skäl när ingen modulplan kan beräknas", async () => {
    const response: FurnitureModulePlan = { ...plan(), state: "unavailable", code: "CUSTOM_BAY_LAYOUT",
      message: "Valda fackproportioner kan inte omvandlas exakt.", can_export_drafts: false, modules: [], layout: null };
    render(panel(client(response))); enterGrid(); calculate();
    expect(await screen.findByText(response.message)).toBeVisible();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByRole("button", { name: "Hämta hela modulplanen" })).toBeNull();
  });

  it.each([
    ["designidentitet", (response: FurnitureModulePlan) => { response.source_design_hash = "f".repeat(64); }],
    ["råformat", (response: FurnitureModulePlan) => { response.source.workspace.manufacturing!.stock_width_um += 1; }],
    ["installationsutrymme", (response: FurnitureModulePlan) => { response.source.workspace.design.installation!.left_allowance_um = 0; }],
    ["rutnät", (response: FurnitureModulePlan) => { response.grid.gap_um = 1; }],
    ["saknad modul", (response: FurnitureModulePlan) => { response.modules.pop(); }],
    ["upprepad position", (response: FurnitureModulePlan) => { response.modules[1]!.column = 0; }],
    ["modulens källhash", (response: FurnitureModulePlan) => { response.modules[0]!.source_workspace_hash = "f".repeat(64); }],
    ["modulens planhash", (response: FurnitureModulePlan) => { response.modules[0]!.plan_hash = "f".repeat(64); }],
    ["modulens design-ID", (response: FurnitureModulePlan) => { response.modules[0]!.workspace.design.design_id = "another"; }],
    ["modulens materialbatch", (response: FurnitureModulePlan) => {
      response.modules[0]!.workspace.design.material.batch_id = "another";
      response.modules[0]!.preview.workspace.design.material.batch_id = "another";
    }],
    ["modulens installation", (response: FurnitureModulePlan) => {
      response.modules[0]!.workspace.design.installation = null;
      response.modules[0]!.preview.workspace.design.installation = null;
    }],
    ["minskad hyllast", (response: FurnitureModulePlan) => {
      response.modules[0]!.workspace.design.intent.shelf_load_n = 0;
      response.modules[0]!.preview.workspace.design.intent.shelf_load_n = 0;
    }],
    ["felaktigt krav", (response: FurnitureModulePlan) => { Object.assign(response.requirements[0]!, { message: {} }); }],
    ["bearbetningstillstånd", (response: FurnitureModulePlan) => { Object.assign(response.modules[0]!, { physical_cutting_authorized: true }); }],
    ["ofullständigt svar", (response: FurnitureModulePlan) => { Object.assign(response, { source: null }); }],
  ])("avvisar motsägande %s utan att erbjuda export", async (_, alter) => {
    const response = plan(); alter(response);
    render(panel(client(response))); enterGrid(); calculate();
    expect(await screen.findByRole("alert")).toBeVisible();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByRole("button", { name: "Hämta hela modulplanen" })).toBeNull();
    expect(screen.getByRole("button", { name: "Beräkna modulplan" })).toBeEnabled();
  });

  it("accepterar API:ets utelämnade standardvärden och sorterade materialspecifika råformat", async () => {
    const source = sourceWorkspace();
    Object.assign(source.design.intent, { shelf_load_basis: "per_row", shelf_load_per_metre_n: 0,
      bay_width_ratios_ppm: [], back_panel: "inset_groove", shelf_mount: "fixed", plinth_height_um: 0 });
    source.manufacturing!.edge_margin_um = 0;
    source.manufacturing!.material_stocks = [
      { material_id: "birch-plywood-6", material_version: "screening-2026.1", measured_thickness_um: 6_000,
        stock_width_um: 800_000, stock_height_um: 1_220_000, stock_grain_axis: "x", edge_margin_um: 0 },
      { material_id: "birch-plywood", material_version: "screening-2026.1", measured_thickness_um: 18_000,
        stock_width_um: 1_220_000, stock_height_um: 2_440_000, stock_grain_axis: "y", edge_margin_um: 0 },
    ];
    const response = plan(source);
    for (const workspace of [response.source.workspace, ...response.modules.flatMap(module => [module.workspace, module.preview.workspace])]) {
      for (const field of ["shelf_load_basis", "shelf_load_per_metre_n", "bay_width_ratios_ppm", "back_panel", "shelf_mount", "plinth_height_um"] as const) delete workspace.design.intent[field];
      delete workspace.manufacturing!.edge_margin_um;
      workspace.manufacturing!.material_stocks!.reverse();
      workspace.manufacturing!.material_stocks!.forEach(stock => { delete stock.edge_margin_um; });
    }
    render(panel(client(response), source)); enterGrid(); calculate();
    await screen.findByRole("table"); expect(downloadPlan()).toBeEnabled();
  });

  it("återhämtar ett nätverksfel och ett nedladdningsfel utan att tappa utkastet", async () => {
    const response = plan(), api = client(response);
    vi.mocked(api.planFurnitureModules).mockRejectedValueOnce(new Error("Nätverket svarar inte."));
    const create = vi.fn().mockImplementationOnce(() => { throw new Error("Browser refused"); }).mockReturnValue("blob:retry");
    vi.stubGlobal("URL", { createObjectURL: create, revokeObjectURL: vi.fn() });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    render(panel(api)); enterGrid(); calculate();
    expect(await screen.findByRole("alert")).toHaveTextContent("Nätverket svarar inte.");
    calculate(); await screen.findByRole("table");
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.click(downloadPlan());
    expect(screen.getByRole("alert")).toHaveTextContent("Arbetsfilen kunde inte hämtas");
    expect(downloadPlan()).toBeEnabled();
    fireEvent.click(downloadPlan()); expect(create).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("bevarar även inaktiva lastvärden när lasten anges per meter", async () => {
    const source = sourceWorkspace();
    Object.assign(source.design.intent, { shelf_load_basis: "per_metre", shelf_load_per_metre_n: 300, shelf_load_n: 250 });
    const response = plan(source), api = client(response);
    render(panel(api, source)); enterGrid(); calculate();
    await screen.findByRole("table"); expect(downloadPlan()).toBeEnabled();
    const changed = structuredClone(response);
    changed.modules[0]!.workspace.design.intent.shelf_load_n = 0;
    changed.modules[0]!.preview.workspace.design.intent.shelf_load_n = 0;
    vi.mocked(api.planFurnitureModules).mockResolvedValue(changed);
    calculate(); expect(await screen.findByRole("alert")).toBeVisible();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it.each(["pågående", "redan visat"])("inaktiverar ett %s svar när arbetsfilens råformat ändras trots samma designhash", async stage => {
    const response = plan(), api = client(response), pending = defer<FurnitureModulePlan>();
    if (stage === "pågående") vi.mocked(api.planFurnitureModules).mockReturnValueOnce(pending.promise);
    const view = render(panel(api)); enterGrid(); calculate();
    if (stage === "redan visat") await screen.findByRole("table");
    const changed = sourceWorkspace(); changed.manufacturing!.stock_width_um += 1;
    view.rerender(panel(api, changed));
    if (stage === "pågående") await act(async () => { pending.resolve(response); });
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.getByRole("button", { name: "Beräkna modulplan" })).toBeEnabled();
    vi.mocked(api.planFurnitureModules).mockResolvedValue(plan(changed));
    calculate(); await screen.findByRole("table"); expect(downloadPlan()).toBeEnabled();
  });

  it("ignorerar tidigare svar efter ändrade val, även när ett nytt svar har hunnit visas", async () => {
    const response = plan(), api = client(response), pending = defer<FurnitureModulePlan>();
    vi.mocked(api.planFurnitureModules).mockReturnValueOnce(pending.promise);
    render(panel(api)); enterGrid(); calculate();
    const changed = { ...initialGrid, gap_um: 1_000 };
    enterGrid(changed);
    vi.mocked(api.planFurnitureModules).mockResolvedValue(plan(sourceWorkspace(), changed));
    calculate(); await screen.findByRole("table");
    await act(async () => { pending.resolve(response); });
    expect(screen.getByText(/Mellanrum: 1 mm/)).toBeVisible();
    expect(downloadPlan()).toBeEnabled();
    fireEvent.change(screen.getByLabelText("Mellanrum mellan moduler (mm)"), { target: { value: "" } });
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.getByRole("button", { name: "Beräkna modulplan" })).toBeDisabled();
  });

  it("släpper pågående vänteläge när API-klienten byts och ignorerar det gamla svaret", async () => {
    const response = plan(), oldApi = client(response), newApi = client(response), pending = defer<FurnitureModulePlan>();
    vi.mocked(oldApi.planFurnitureModules).mockReturnValueOnce(pending.promise);
    const view = render(panel(oldApi)); enterGrid(); calculate();
    view.rerender(panel(newApi));
    expect(screen.getByRole("button", { name: "Beräkna modulplan" })).toBeEnabled();
    calculate(); await screen.findByRole("table");
    await act(async () => { pending.resolve({ ...response, message: "Gammal klient" }); });
    expect(screen.queryByText("Gammal klient")).toBeNull();
    expect(downloadPlan()).toBeEnabled();
  });

  it("återanvänder inte en tidigare plan om användaren återgår till samma val", async () => {
    render(panel(client())); enterGrid(); calculate(); await screen.findByRole("table");
    fireEvent.change(screen.getByLabelText("Mellanrum mellan moduler (mm)"), { target: { value: "1" } });
    fireEvent.change(screen.getByLabelText("Mellanrum mellan moduler (mm)"), { target: { value: "0" } });
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.getByRole("button", { name: "Beräkna modulplan" })).toBeEnabled();
    calculate(); await screen.findByRole("table"); expect(downloadPlan()).toBeEnabled();
  });

  it("återställer efter disabled och låter inte svar från en avmonterad panel påverka nästa", async () => {
    const response = plan(), api = client(response), pending = defer<FurnitureModulePlan>();
    vi.mocked(api.planFurnitureModules).mockReturnValueOnce(pending.promise);
    const view = render(panel(api)); enterGrid(); calculate();
    view.rerender(panel(api, sourceWorkspace(), true));
    expect(screen.getByRole("button", { name: "Beräkna modulplan" })).toBeDisabled();
    expect(screen.queryByRole("table")).toBeNull();
    view.rerender(panel(api));
    expect(screen.getByRole("button", { name: "Beräkna modulplan" })).toBeEnabled();
    view.unmount();
    render(panel(api)); enterGrid(); calculate(); await screen.findByRole("table");
    await act(async () => { pending.resolve({ ...response, message: "Avmonterad panel" }); });
    expect(screen.queryByText("Avmonterad panel")).toBeNull();
    expect(downloadPlan()).toBeEnabled();
  });
});

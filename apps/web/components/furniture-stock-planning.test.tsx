import { useState } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { newFurnitureWorkspace, type FurnitureWorkspace } from "@/lib/furniture-workspace";
import { FurnitureStockEditor, FurnitureStockPlan } from "./furniture-stock-planning";

function workspace(): FurnitureWorkspace {
  return { ...newFurnitureWorkspace("shelving"), manufacturing: {
    machine_profile_id: "custombuild-router-1325-linuxcnc", machine_profile_version: "1.0.0-validation",
    stock_width_um: 1_220_000, stock_height_um: 2_440_000, stock_grain_axis: "y",
  } };
}

describe("råformat per material", () => {
  it("låter ryggen ha eget format och fiberaxel och återgår uttryckligen till gemensamt format", () => {
    const changed = vi.fn();
    function Harness() {
      const [value, setValue] = useState(workspace());
      return <FurnitureStockEditor workspace={value} inputDrafts={{}} onInputError={vi.fn()}
        onChange={manufacturing => { changed(manufacturing); setValue({ ...value, manufacturing }); }} />;
    }
    render(<Harness />);
    const back = within(screen.getByRole("group", { name: "birch-plywood-6 · 6 mm" }));
    fireEvent.click(back.getByRole("checkbox"));
    fireEvent.change(back.getByLabelText("Råskivans bredd (mm)"), { target: { value: "800.001" } });
    fireEvent.change(back.getByLabelText("Fiberriktning på råskivan"), { target: { value: "x" } });
    expect(changed.mock.lastCall?.[0]).toMatchObject({ stock_width_um: 1_220_000, stock_grain_axis: "y",
      material_stocks: [{ material_id: "birch-plywood-6", material_version: "screening-2026.1",
        measured_thickness_um: 6_000, stock_width_um: 800_001, stock_grain_axis: "x" }] });
    fireEvent.click(back.getByRole("checkbox"));
    expect(changed.mock.lastCall?.[0]).not.toHaveProperty("material_stocks");
    expect(back.getByText("Använder gemensamt råformat.")).toBeVisible();
  });

  it("avvisar för många decimaler utan att ersätta råmåttet", () => {
    const changed = vi.fn(), invalid = vi.fn();
    render(<FurnitureStockEditor workspace={workspace()} inputDrafts={{}} onChange={changed} onInputError={invalid} />);
    fireEvent.change(screen.getByLabelText("Råskivans bredd (mm)"), { target: { value: "1200.0001" } });
    expect(changed).not.toHaveBeenCalled();
    expect(invalid).toHaveBeenCalledWith("manufacturing.width", expect.any(Error), "1200.0001");
  });

  it("återanvänder inte en tjockleksbunden skiva efter materialbyte", () => {
    const value = workspace(), changed = vi.fn();
    value.manufacturing!.material_stocks = [{ material_id: "birch-plywood-6", material_version: "screening-2026.1",
      measured_thickness_um: 6_001, stock_width_um: 800_000, stock_height_um: 300_000, stock_grain_axis: "x" }];
    render(<FurnitureStockEditor workspace={value} inputDrafts={{}} onChange={changed} onInputError={vi.fn()} />);
    expect(screen.getByRole("checkbox", { name: "Eget råformat för birch-plywood-6 · 6 mm" })).not.toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Ta bort oanvänt råformat" }));
    expect(changed.mock.lastCall?.[0]).not.toHaveProperty("material_stocks");
  });

  it("visar exakta formatbrister, maskinområde och okänd fiberriktning", () => {
    render(<FurnitureStockPlan manufacturing={{ state: "requires_change", geometry_compatible: false, issues: [],
      stock_groups: [{ material_id: "birch-plywood", material_version: "screening-2026.1", measured_thickness_um: 18_000,
        selection_source: "material", stock: { stock_width_um: 800_000, stock_height_um: 300_000, stock_grain_axis: "x" },
        geometry_compatible: false, required_formats: [{ stock_width_um: 800_001, stock_height_um: 300_000, fits_machine: false }],
        parts: [{ part_id: "one", semantic_key: "top", fits_stock: false, orientations: [{ rotation_deg: 90,
          required_stock_width_um: 800_001, required_stock_height_um: 300_000, width_shortfall_um: 1,
          height_shortfall_um: 0, fits_stock: false, fits_machine: false }] },
        { part_id: "two", semantic_key: "side", fits_stock: null, orientations: [] }] }] }} />);
    fireEvent.click(screen.getByText(/Format behöver granskas/));
    expect(screen.getByText(/kräver större arbetsområde/)).toBeVisible();
    expect(screen.getByText(/Saknas i X: 0,001 mm/)).toBeVisible();
    expect(screen.getByText("Fiberriktning saknas.")).toBeVisible();
  });
});

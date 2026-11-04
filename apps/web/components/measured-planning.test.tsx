import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { sampleMeasuredWork, type MeasuredResult } from "@/lib/measured-assembly";
import { MeasuredRoomPlanning, MeasuredStockEdit } from "./measured-planning";

function report(): MeasuredResult {
  return {
    assembly: sampleMeasuredWork(), assembly_sha256: "a".repeat(64), cells: {}, issues: {},
    joints: [], bearings: [], overlaps: [], overlaps_truncated: false,
    fastener_interferences: [], back_coverage: [], unresolved_back_parts: [], stock_checks: [],
    physical_cutting_authorized: false,
  };
}

function enterRoom() {
  fireEvent.change(screen.getByLabelText("Fri bredd (mm)"), { target: { value: "4340" } });
  fireEvent.change(screen.getByLabelText("Fritt djup (mm)"), { target: { value: "280" } });
  fireEvent.change(screen.getByLabelText("Fri takhöjd (mm)"), { target: { value: "2540,001" } });
}

describe("measured installation and raw formats", () => {
  it("records precise room inputs for the server and leaves unknown tool clearance unset", () => {
    const original = report();
    const snapshot = JSON.stringify(original);
    const changed = vi.fn();
    render(<MeasuredRoomPlanning report={original} change={changed} />);
    expect(screen.getByText(/Monteringsutrymmet är ej verifierat/)).toBeVisible();
    enterRoom();
    fireEvent.click(screen.getByRole("button", { name: "Kontrollera monteringsutrymmet" }));
    expect(changed).toHaveBeenCalledWith(expect.objectContaining({
      room: { size_um: [4_340_000, 280_000, 2_540_001],
        rear_tool_depth_um: null, source: "Inmatade fria rumsmått" },
      parts: original.assembly.parts,
    }));
    expect(JSON.stringify(original)).toBe(snapshot);
  });

  it("rejects submicrometre room inputs and does not send a rounded replacement", () => {
    const changed = vi.fn();
    render(<MeasuredRoomPlanning report={report()} change={changed} />);
    enterRoom();
    fireEvent.change(screen.getByLabelText("Fri takhöjd (mm)"), { target: { value: "2540.0001" } });
    fireEvent.click(screen.getByRole("button", { name: "Kontrollera monteringsutrymmet" }));
    expect(screen.getByRole("alert")).toBeVisible();
    expect(changed).not.toHaveBeenCalled();
  });

  it("shows a blocked tipping direction beside an otherwise fitting final pose", () => {
    const value = report();
    value.installation = {
      state: "fits", known_parts: 1, unresolved_parts: [],
      extent_xyz_um: [900_000, 3_000_000, 4_000_000], margin_xyz_um: [0, 0, 999_999],
      raise_height_um: [5_000_000, 5_000_000], raise_states: ["blocked", "blocked"],
      rear_access: [],
    };
    render(<MeasuredRoomPlanning report={value} change={vi.fn()} />);
    expect(screen.getByText("Slutläge · Ryms nominellt")).toBeVisible();
    expect(screen.getByText(/Resning från framsidan · Ryms inte/)).toBeVisible();
    expect(screen.getByText(/Resning från baksidan · Ryms inte/)).toBeVisible();
  });

  it("changes only the selected piece and keeps grain dimensions in their original order", () => {
    const value = report();
    const changed = vi.fn();
    const piece = value.assembly.parts[0]!;
    render(<MeasuredStockEdit report={value} piece={piece} change={changed} />);
    fireEvent.click(screen.getByText("Ändra inköpsformat för " + piece.id));
    for (const [name, v] of [["Inköpslängd", "2400"], ["Inköpsbredd", "45"], ["Inköpstjocklek", "39,999"]]) {
      fireEvent.change(screen.getByLabelText(name + " (mm)"), { target: { value: v } });
    }
    fireEvent.click(screen.getByRole("button", { name: "Kontrollera inköpsformatet" }));
    const [work, selected] = changed.mock.lastCall!;
    expect(selected).toBe(piece.id);
    expect(work.parts[0].stock_lbt_um).toEqual([2_400_000, 45_000, 39_999]);
    expect(work.parts.slice(1)).toEqual(value.assembly.parts.slice(1));
    expect(value.assembly.parts[0]).toBe(piece);
  });

  it("removes old room data explicitly when measurements are no longer valid", () => {
    const value = report();
    value.assembly.room = { size_um: [4_000_000, 1_000_000, 2_000_000], source: "Tidigare rum" };
    const changed = vi.fn();
    render(<MeasuredRoomPlanning report={value} change={changed} />);
    fireEvent.click(screen.getByRole("button", { name: "Ta bort rumsmåtten" }));
    expect(changed).toHaveBeenCalledWith(expect.objectContaining({ room: null }));
  });
});

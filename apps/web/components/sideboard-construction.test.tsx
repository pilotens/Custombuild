import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CustombuildApiClient } from "@/lib/api-client";
import type { components } from "@/lib/api-schema";
import { sampleMeasuredWork } from "@/lib/measured-assembly";
import type { PublicRuntimeConfig } from "@/lib/runtime-config";
import { SideboardConstruction } from "./sideboard-construction";

type Construction = components["schemas"]["SideboardConstruction"];
const config = { apiUrl: "http://localhost:8000", developmentToken: "demo-nordic-owner" } as PublicRuntimeConfig;
function fixture(): Construction {
  return {
    schema_version: "custombuild.sideboard-construction.v1", assembly: sampleMeasuredWork(),
    assembly_sha256: "a".repeat(64), construction_sha256: "c".repeat(64), physical_cutting_authorized: false,
    request: { width_mm: 2400, depth_mm: 450, height_mm: 750, leg_height_mm: 180,
      door_count: 5, material: "walnut-veneered-plywood", dimensions_confirmed: false },
    fittings: [], purchase_blanks: [], hardware: { "175H7100": 10 },
    assembly_sequence: ["Montera"], assumptions: ["Valda mått"], sources: [],
    outstanding_acceptance: ["Full gångjärnsrörelse återstår"],
  };
}
afterEach(() => vi.restoreAllMocks());

describe("sideboard construction", () => {
  it("passes chosen dimensions and invalidates the old drawing after an edit", async () => {
    const result = fixture();
    const call = vi.spyOn(CustombuildApiClient.prototype, "constructSideboard").mockResolvedValue(result);
    const open = vi.fn();
    render(<SideboardConstruction runtimeConfig={config} open={open} openVerified={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Skänkens bredd (mm)"), { target: { value: "2200" } });
    fireEvent.click(screen.getByRole("button", { name: "Skapa skänk" }));
    fireEvent.click(await screen.findByRole("button", { name: "Öppna skänkens delar" }));
    expect(open).toHaveBeenCalledWith(result.assembly);
    expect(call).toHaveBeenCalledWith(expect.objectContaining({ width_mm: 2200, door_count: 5 }));
    fireEvent.change(screen.getByLabelText("Benhöjd (mm)"), { target: { value: "190" } });
    expect(screen.queryByRole("button", { name: "Öppna skänkens delar" })).toBeNull();
  });
  it("discards late responses when dimensions change", async () => {
    let finish!: (result: Construction) => void;
    vi.spyOn(CustombuildApiClient.prototype, "constructSideboard").mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    render(<SideboardConstruction runtimeConfig={config} open={vi.fn()} openVerified={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Skapa skänk" }));
    fireEvent.change(screen.getByLabelText("Skänkens höjd (mm)"), { target: { value: "780" } });
    await act(async () => finish(fixture()));
    expect(screen.queryByRole("button", { name: "Öppna skänkens delar" })).toBeNull();
  });
  it("uses the sideboard export route and never invents cutting permission", async () => {
    vi.spyOn(CustombuildApiClient.prototype, "constructSideboard").mockResolvedValue(fixture());
    const call = vi.spyOn(CustombuildApiClient.prototype, "requestSideboardExport").mockRejectedValue(new Error("Testfel"));
    const trestle = vi.spyOn(CustombuildApiClient.prototype, "requestTrestleExport");
    render(<SideboardConstruction runtimeConfig={config} open={vi.fn()} openVerified={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Skapa skänk" }));
    fireEvent.click(await screen.findByRole("button", { name: "Skapa CAD-kontrollerat ritningspaket" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Testfel");
    expect(call).toHaveBeenCalledOnce(); expect(trestle).not.toHaveBeenCalled();
    expect(screen.getByText("Full gångjärnsrörelse återstår")).toBeVisible();
  });
});

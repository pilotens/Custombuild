import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CustombuildApiClient } from "@/lib/api-client";
import type { components } from "@/lib/api-schema";
import { sampleMeasuredWork } from "@/lib/measured-assembly";
import type { PublicRuntimeConfig } from "@/lib/runtime-config";
import { TrestleConstruction } from "./trestle-construction";

type Construction = components["schemas"]["TrestleConstruction"];
const config = { apiUrl: "http://localhost:8000", developmentToken: "demo-nordic-owner" } as PublicRuntimeConfig;
function fixture(): Construction {
  return {
    schema_version: "custombuild.trestle-construction.v1", assembly: sampleMeasuredWork(),
    assembly_sha256: "a".repeat(64), construction_sha256: "c".repeat(64), physical_cutting_authorized: false,
    request: { length_mm: 2200, width_mm: 950, height_mm: 750, central_load_n: 300,
      material: "american-black-walnut", moisture_min_pct: 6, moisture_max_pct: 12, dimensions_confirmed: false },
    sizing: { top_thickness_mm: 36, span_mm: 1490, strip_width_mm: 300, elastic_modulus_mpa: 6950.4,
      predicted_deflection_mm: 4.59, deflection_target_mm: 4.96, bending_stress_mpa: 2,
      width_change_mm: 15.618, mounting_travel_each_way_mm: 3.466, mounting_slot_length_mm: 16,
      load_rating_verified: false, assumptions: ["Avgränsad skivremsa"], sources: [] },
    purchase_blanks: [], hardware: [{ specification: "M8", quantity: 24 }],
    assembly_sequence: ["Montera"], outstanding_acceptance: ["Kontrollera förbandens bärförmåga"],
  };
}
afterEach(() => vi.restoreAllMocks());

describe("automatic trestle construction", () => {
  it("sends chosen dimensions and opens the exact returned parts", async () => {
    const result = fixture();
    const call = vi.spyOn(CustombuildApiClient.prototype, "constructTrestle").mockResolvedValue(result);
    const open = vi.fn(); render(<TrestleConstruction runtimeConfig={config} open={open} />);
    fireEvent.change(screen.getByLabelText("Längd (mm)"), { target: { value: "2400" } });
    fireEvent.click(screen.getByRole("button", { name: "Skapa konstruktion" }));
    await screen.findByText(/Vald skivtjocklek: 36 mm/);
    expect(call).toHaveBeenCalledWith(expect.objectContaining({ length_mm: 2400, dimensions_confirmed: false }));
    fireEvent.click(screen.getByRole("button", { name: "Öppna delarna och bearbetningarna" }));
    expect(open).toHaveBeenCalledWith(result.assembly);
    expect(screen.getByText(/inte en lastklassning/)).toBeVisible();
    fireEvent.change(screen.getByLabelText("Bredd (mm)"), { target: { value: "1000" } });
    expect(screen.queryByText(/Vald skivtjocklek/)).toBeNull();
  });

  it("discards a late result after the input changed", async () => {
    let finish!: (result: Construction) => void;
    vi.spyOn(CustombuildApiClient.prototype, "constructTrestle").mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    render(<TrestleConstruction runtimeConfig={config} open={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Skapa konstruktion" }));
    fireEvent.change(screen.getByLabelText("Höjd (mm)"), { target: { value: "780" } });
    await act(async () => finish(fixture()));
    expect(screen.queryByText(/Vald skivtjocklek/)).toBeNull();
  });

  it("rejects a response claiming cutting permission", async () => {
    const result = { ...fixture(), physical_cutting_authorized: true } as unknown as Construction;
    vi.spyOn(CustombuildApiClient.prototype, "constructTrestle").mockResolvedValue(result);
    render(<TrestleConstruction runtimeConfig={config} open={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Skapa konstruktion" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("identitet saknas");
  });

  it("uses the durable export and cancels late download bytes after editing", async () => {
    vi.spyOn(CustombuildApiClient.prototype, "constructTrestle").mockResolvedValue(fixture());
    const jobId = "d4fe7ac5-a886-4294-8fb0-c0c7fc36175c";
    vi.spyOn(CustombuildApiClient.prototype, "requestTrestleExport").mockResolvedValue({ project_id: "project", job_id: jobId });
    vi.spyOn(CustombuildApiClient.prototype, "furnitureExportResult").mockResolvedValue({
      job_id: jobId, state: "succeeded", artifact_id: "392bc496-f75c-4474-abd9-453f40a17561",
      file_name: "review.zip", download_path: `/v1/furniture/projects/project/exports/${jobId}/download`,
      design_hash: "a".repeat(64), revision: 1, sha256: "b".repeat(64), size_bytes: 3,
      content_type: "application/zip", physical_cutting_authorized: false,
    });
    let finish!: (blob: Blob) => void;
    const download = vi.spyOn(CustombuildApiClient.prototype, "downloadFurnitureExport")
      .mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    const create = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:review");
    render(<TrestleConstruction runtimeConfig={config} open={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Skapa konstruktion" }));
    fireEvent.click(await screen.findByRole("button", { name: "Skapa CAD-kontrollerat ritningspaket" }));
    await waitFor(() => expect(download).toHaveBeenCalledOnce());
    const signal = download.mock.calls[0]![5]!;
    expect(signal.aborted).toBe(false);
    fireEvent.change(screen.getByLabelText("Längd (mm)"), { target: { value: "2300" } });
    expect(signal.aborted).toBe(true);
    await act(async () => finish(new Blob(["zip"])));
    expect(create).not.toHaveBeenCalled();
  });
});

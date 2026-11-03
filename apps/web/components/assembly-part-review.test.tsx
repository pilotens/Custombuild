import { act, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { PartInspection } from "@/lib/part-inspection";
import { AssemblyPartReview } from "./assembly-part-review";

function sample(name: string): PartInspection {
  const size = { width_um: 600_000, depth_um: 300_000, height_um: 18_000 };
  // The inspector reads these fields from the server contract. Manufacturing object
  // construction is independently tested through the authenticated endpoints.
  return {
    part: {
      part_id: "part-" + name,
      semantic_key: name,
      finished_size: size,
      raw_size: size,
      material_id: "birch-plywood",
      material_version: "test",
      grain_direction: "x",
      features: [],
    },
    design_hash: "a".repeat(64),
    sheet_sha256: "b".repeat(64),
    physical_cutting_authorized: false,
    mates: [],
    hardware: [],
    stock: [],
    rear_openings: [],
    reference_objects: [],
    part_names: {},
    removal: {
      separated_groups: [],
      remaining_frame_feet: null,
      affected_moving_groups: [],
    },
    drawings: [
      {
        view: "A",
        physical_face: "top",
        u_axis: "x",
        v_axis: "y",
        holes: [],
        svg: '<svg xmlns="http://www.w3.org/2000/svg" />',
      },
    ],
  } as unknown as PartInspection;
}

it("keeps the selected shelf and its drawing when an earlier component finishes later", async () => {
  let finishOld!: (value: PartInspection) => void;
  const first = new Promise<PartInspection>((done) => {
    finishOld = done;
  });
  const oldLoad = vi.fn(() => first);
  const show = vi.fn();
  const common = {
    mode: "whole" as const,
    changeMode: vi.fn(),
    selectMate: vi.fn(),
    onInspection: show,
  };
  const rendered = render(<AssemblyPartReview {...common} load={oldLoad} />);
  await act(async () => {
    await Promise.resolve();
  });
  const newSheet = sample("shelf-r1-b2");
  const newLoad = vi.fn(async () => newSheet);
  rendered.rerender(<AssemblyPartReview {...common} load={newLoad} />);
  await screen.findByRole("heading", { name: newSheet.part.semantic_key });
  await act(async () => {
    finishOld(sample("left-side"));
    await first;
  });
  expect(screen.queryByRole("heading", { name: "left-side" })).toBeNull();
  expect(
    screen.getByRole("link", { name: /Hämta visad delritning/ }),
  ).toHaveAttribute("download", expect.stringContaining(newSheet.part.part_id));
  expect(show).toHaveBeenCalledTimes(1);
  expect(show).toHaveBeenCalledWith(newSheet);
});

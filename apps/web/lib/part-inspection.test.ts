import { describe, expect, it } from "vitest";
import {
  checkedInspection,
  inspectionDrillMarkers,
  type PartInspection,
} from "./part-inspection";

describe("selected part identity and bore display", () => {
  const receipt = {
    part: {
      part_id: "par_test12",
      finished_size: { width_um: 18000, depth_um: 300000, height_um: 1200000 },
    },
    physical_cutting_authorized: false,
    design_hash: "a".repeat(64),
    mates: [],
    drawings: [
      {
        view: "B",
        physical_face: "right",
        u_axis: "y",
        v_axis: "z",
        holes: [
          { u_um: 37000, v_um: 110000, diameter_um: 5501, feature_id: "bore" },
          { u_um: 37000, v_um: 142000, diameter_um: 5501, feature_id: "bore" },
        ],
      },
    ],
  } as unknown as PartInspection;
  it("places each right-face drill in the selected board's local 3D frame", () => {
    const holes = inspectionDrillMarkers(receipt);
    expect(holes.map((h) => h.center)).toEqual([
      [0.00905, -0.49, 0.113],
      [0.00905, -0.458, 0.113],
    ]);
    expect(holes[0]?.radius).toBe(0.0027505);
    expect(new Set(holes.map((h) => h.key)).size).toBe(2);
  });
  it("rejects a sheet from a different part, model or release context", () => {
    expect(checkedInspection(receipt, "par_test12", "a".repeat(64))).toBe(
      receipt,
    );
    expect(() =>
      checkedInspection(receipt, "par_other", "a".repeat(64)),
    ).toThrow();
    expect(() =>
      checkedInspection(receipt, "par_test12", "b".repeat(64)),
    ).toThrow();
    expect(() =>
      checkedInspection(
        {
          ...receipt,
          physical_cutting_authorized: true,
        } as unknown as PartInspection,
        "par_test12",
        receipt.design_hash,
      ),
    ).toThrow();
  });
});

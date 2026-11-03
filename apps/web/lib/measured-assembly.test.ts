import { describe, expect, it } from "vitest";
import {
  measuredFaceSvg,
  measuredGroups,
  measuredPartSheet,
  measuredViews,
  sampleMeasuredWork,
  type MeasuredResult,
} from "./measured-assembly";

describe("measured working drawings", () => {
  it("keeps a drill's centre and half-micrometre radius on the stated face", () => {
    const part = sampleMeasuredWork().parts[0]!;
    const drill = {
      ...part.operations[0]!,
      shape: "drill" as const,
      id: "pilot",
      width_um: 5101,
      height_um: 5101,
      u_um: 7777,
      v_um: 11111,
    };
    const piece = { ...part, operations: [drill] };
    const views = measuredViews(piece);
    const face = views.find((v) => v.face === "back")!;
    expect(face.axes).toBe("xz");
    expect(measuredFaceSvg(piece, face)).toContain(
      'cx="7.777" cy="11.111" r="2.5505"',
    );
    expect(views.filter((v) => v.face === "back")).toHaveLength(1);
  });
  it("prints the selected pieces, fittings, operation source and current revision safely", () => {
    const assembly = sampleMeasuredWork();
    const part = assembly.parts[0]!;
    part.operations[0]!.provenance = "transfer_template";
    part.operations[0]!.source = "<script>private source</script>";
    assembly.joints[0]!.article = "V-1";
    assembly.joints[0]!.fittings = ["M-1"];
    const report: MeasuredResult = {
      assembly,
      assembly_sha256: "a".repeat(64),
      cells: {},
      issues: {},
      overlaps: [],
      overlaps_truncated: false,
      back_coverage: [],
      unresolved_back_parts: [],
      bearings: [],
      physical_cutting_authorized: false,
      joints: [
        {
          id: "fog-1",
          state: "aligned",
          reason: "local",
          uses_transfer_template: true,
          frame_id: "door",
        },
      ],
    };
    assembly.parts.push({ ...part, id: "loose-door" });
    expect(measuredGroups(report, null)).toEqual([
      ["post-1", "rail-1"],
      ["loose-door"],
    ]);
    expect(measuredGroups(report, null, "post-1")).toEqual([
      ["rail-1"],
      ["loose-door"],
    ]);
    report.joints[0]!.state = "mismatch";
    expect(measuredGroups(report, null)).toHaveLength(3);
    report.joints[0]!.state = "aligned";
    const html = measuredPartSheet(report, part);
    for (const text of [
      "rail-1/tapp",
      "post-1/ficka",
      "1 × M-1",
      "1 × V-1",
      "läge härlett från motdelen",
      "lokalt i door",
      "a".repeat(64),
      "15 / 15",
      "&lt;script&gt;",
    ]) {
      expect(html).toContain(text);
    }
    expect(html).not.toContain("<script>");
  });
});

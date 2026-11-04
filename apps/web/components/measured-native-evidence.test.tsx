import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { sampleMeasuredWork, type MeasuredResult } from "@/lib/measured-assembly";
import { MeasuredRegister } from "./measured-register";

function report(native: boolean): MeasuredResult {
  const assembly = sampleMeasuredWork();
  assembly.joints = [];
  assembly.parts[0]!.profile = { kind: "rounded_rectangle", axes: "xy", radius_um: 1000, source: "Prov" };
  return {
    assembly, assembly_sha256: "a".repeat(64), cells: {}, issues: {}, joints: [], bearings: [],
    overlaps: [], overlaps_truncated: false, fastener_interferences: [], back_coverage: [],
    unresolved_back_parts: [], stock_checks: [],
    unreviewed_profile_parts: native ? [] : [assembly.parts[0]!.id],
    ...(native ? { geometry_method: "exact_cad" as const } : {}),
    physical_cutting_authorized: false,
  };
}

describe("profile inspection evidence", () => {
  it("describes exact material removal after the server supplies CAD evidence", () => {
    render(<MeasuredRegister report={report(true)} />);
    expect(screen.getByText(/Profil, bearbetningar och nominell förbandspassning är kontrollerade i CAD/)).toBeVisible();
    expect(screen.queryByText(/Förband och bearbetningar mot profilen är ej verifierade/)).toBeNull();
    expect(screen.getByText(/CAD-kontrollen använder bearbetade solider/)).toBeVisible();
  });
  it("keeps the unverified profile message for an ordinary measured review", () => {
    render(<MeasuredRegister report={report(false)} />);
    expect(screen.getByText(/Förband och bearbetningar mot profilen är ej verifierade/)).toBeVisible();
    expect(screen.queryByText(/Profil, bearbetningar och nominell förbandspassning är kontrollerade i CAD/)).toBeNull();
  });
});

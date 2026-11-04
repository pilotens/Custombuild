import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { measuredGroups, measuredHardware, measuredPartSheet, sampleMeasuredWork, type MeasuredResult } from "@/lib/measured-assembly";
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
  it("traces fittings into part sheets, hardware counts and removal without claiming hinge motion", () => {
    const value = report(true); value.geometry_method = "machined_wood";
    value.assembly.fittings = [{id:"hinge-1",kind:"hinge",article:"70T3750.TL",connects:["post-1","rail-1"],
      mounts:[{part:"post-1",operation:"ficka"}],accessories:["609.1500","609.1500"],source:"Catalog",specification:"<img>"}];
    expect(measuredGroups(value,null)).toHaveLength(2);
    value.fitting_checks = [{id:"hinge-1",state:"aligned",uses_transfer_template:false,reason:"Borrbild kontrollerad; rörelse återstår"}];
    expect(measuredGroups(value,null)).toHaveLength(1);
    expect(measuredGroups(value,null,"post-1")).toEqual([["rail-1"]]);
    expect(measuredHardware(value,"post-1")).toContainEqual(["609.1500",2]);
    const sheet = measuredPartSheet(value,value.assembly.parts[0]!);
    expect(sheet).toContain("70T3750.TL"); expect(sheet).toContain("&lt;img&gt;");
    expect(sheet).toContain("Borrbild kontrollerad; rörelse återstår");
    render(<MeasuredRegister report={value}/>);
    expect(screen.getByText(/Beslagens fulla rörelse och volymer är inte verifierade/)).toBeVisible();
    fireEvent.click(screen.getByRole("button",{name:"Visa utan delen"}));
    expect(screen.getByRole("status")).toHaveTextContent("1 beslagsanslutningar berörs");
  });
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

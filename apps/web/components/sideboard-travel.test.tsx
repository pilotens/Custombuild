import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { components } from "@/lib/api-schema";
import { sampleMeasuredWork } from "@/lib/measured-assembly";
import { SideboardTravel, trackPosition } from "./sideboard-travel";

type Construction = components["schemas"]["SideboardConstruction"];
const path: components["schemas"]["HingePath"] = {
  article: "70T3750.TL", plate: "175H7100", front_mm: 19, cup_distance_mm: 7, source: "Testprofil",
  poses: Array.from({length:23}, (_, i) => ({angle_deg:5*i, dx_mm:i, dy_mm:-2*i})),
};

describe("sideboard plan view", () => {
  it("interpolates measured translations and keeps both end positions", () => {
    expect(trackPosition(path,0)).toEqual({dx:0,dy:0});
    expect(trackPosition(path,2.5)).toEqual({dx:.5,dy:-1});
    expect(trackPosition(path,110)).toEqual({dx:22,dy:-44});
  });

  it("draws an imported track only when the current door has a matching article", () => {
    const assembly=sampleMeasuredWork();
    assembly.parts[0]={...assembly.parts[0]!,id:"DOOR-1",origin_um:[27000,0,207000],size_um:[490140,19000,516000]};
    const construction={
      assembly,request:{width_mm:2400,depth_mm:450,review:{hinge_paths:[path]}},
      assessment:{door_motion:[{article:path.article,door_id:"DOOR-1",state:"clear_in_model"}]},
    } as Construction;
    const {rerender}=render(<SideboardTravel construction={construction}/>);
    fireEvent.click(screen.getByText("Visa luckrörelsen ovanifrån"));
    fireEvent.change(screen.getByRole("slider"),{target:{value:"35"}});
    const image=screen.getByRole("img",{name:"DOOR-1 vid 35 grader enligt Testprofil"});
    expect(image.querySelector("rect[transform]")).toHaveAttribute("transform","translate(34 -14) rotate(-35)");
    rerender(<SideboardTravel construction={{...construction,request:{...construction.request,review:null}}}/>);
    expect(screen.queryByRole("slider")).toBeNull();
  });
});

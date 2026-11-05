import { describe, expect, it } from "vitest";
import { measuredFaceSvg, measuredViews, sampleMeasuredWork } from "./measured-assembly";
import { orderSceneFaces, profileFaces } from "./measured-profiles";

describe("explicit shaped reference parts", () => {
  it.each([30,-30])("keeps an outer horizontal surface over interior rails at pitch %s", pitch => {
    const angle=pitch*Math.PI/180;
    const face=(id:string,y0:number,y1:number,z:number)=>{
      const points=[[0,y0],[20,y0],[20,y1],[0,y1]].map(([x,y])=>[x!,-z*Math.cos(angle)-y!*Math.sin(angle),y!*Math.cos(angle)-z*Math.sin(angle)]);
      return {id,points,depth:points.reduce((s,p)=>s+p[2]!,0)/4};
    };
    const outer=face("outer",0,100,pitch>0?10:0);
    const interior=face("interior",0,10,5);
    expect(outer.depth).toBeGreaterThan(interior.depth);
    expect(orderSceneFaces([outer,interior],pitch).map(p=>p.id)).toEqual(["interior","outer"]);
  });
  it("keeps a sloped leg as a prism in the assembly and on its drawing", () => {
    const piece = {...sampleMeasuredWork().parts[0]!, operations:[],
      size_um:[70000,150000,640000] as [number,number,number],
      profile:{kind:"convex_prism" as const, axes:"yz" as const,
        points_um:[[0,0],[70000,0],[150000,640000],[80000,640000]] as [number,number][],source:"Prov"}};
    const faces = profileFaces(piece)!;
    expect(faces).toHaveLength(6);
    expect(faces[0]).toEqual([[0,0,0],[0,70000,0],[0,150000,640000],[0,80000,640000]]);
    expect(measuredViews(piece)).toHaveLength(6);
    const drawing = measuredFaceSvg(piece,{face:"left",axes:"yz",cuts:[]});
    expect(drawing).toContain('points="0,0 70,0 150,640 80,640"');
    expect(drawing).not.toContain('<rect');
  });
  it("retains the exact corner radius in SVG and all six millimetre views", () => {
    const piece = {...sampleMeasuredWork().parts[0]!, operations:[],
      size_um:[2200000,950000,32000] as [number,number,number],
      profile:{kind:"rounded_rectangle" as const, axes:"xy" as const,radius_um:60000,source:"Prov"}};
    expect(measuredFaceSvg(piece,{face:"top",axes:"xy",cuts:[]})).toContain('rx="60"');
    expect(measuredViews(piece)).toHaveLength(6);
    const vertices = profileFaces(piece)!.flat();
    expect(Math.max(...vertices.map(p=>p[0]!))).toBe(2200000);
    expect(Math.max(...vertices.map(p=>p[1]!))).toBe(950000);
    expect(Math.max(...vertices.map(p=>p[2]!))).toBe(32000);
    expect(profileFaces({...piece,profile:null})).toBeNull();
  });
});

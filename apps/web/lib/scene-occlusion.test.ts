import { describe, expect, it } from "vitest";
import { orderConvexFaces } from "../../../packages/scene-geometry/occlusion.mjs";

describe("screen overlap ordering", () => {
  it("keeps a small front over a long board even when their average depths say the opposite", () => {
    const board={id:"board",points:[[0,0,0],[100,0,100],[100,20,100],[0,20,0]]};
    const front={id:"front",points:[[80,2,70],[90,2,70],[90,10,70],[80,10,70]]};
    const ordered=orderConvexFaces([front,board]);
    expect(ordered.unresolved).toBe(0);
    expect(ordered.faces.map(f=>f.id)).toEqual(["board","front"]);
  });
  it("handles either winding, coplanar faces, edge-on faces and disjoint projections", () => {
    const face={points:[[0,0,3],[2,0,3],[2,2,3],[0,2,3]]};
    const near={points:face.points.map(p=>[p[0]!,p[1]!,1]).reverse()};
    const edge={points:[[3,0,2],[3,2,2],[3,2,4],[3,0,4]]};
    const result=orderConvexFaces([near,face,edge,{...face}]);
    expect(result.unresolved).toBe(0);expect(result.faces).toHaveLength(4);
    expect(result.faces.indexOf(face)).toBeLessThan(result.faces.indexOf(near));
  });
});

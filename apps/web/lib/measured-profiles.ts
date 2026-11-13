import type { MeasuredPiece } from "./measured-assembly";

// Average-depth painting alone can put an underlying rail in front of a wide top.
// The extreme horizontal face facing the camera occludes every lower/higher face.
// This is a preview ordering correction, not a collision or structural calculation.
export function orderSceneFaces<T extends {points:number[][]; depth:number}>(faces:T[], pitch:number):T[] {
  const angle=pitch*Math.PI/180;
  if(!faces.length || Math.abs(Math.sin(angle))<1e-8) return faces.sort((a,b)=>b.depth-a.depth);
  const height=(p:number[])=>-p[1]!*Math.cos(angle)-p[2]!*Math.sin(angle);
  let frontier=pitch>0?-Infinity:Infinity;
  for(const face of faces) for(const point of face.points)
    frontier=pitch>0?Math.max(frontier,height(point)):Math.min(frontier,height(point));
  const outer=new Set(faces.filter(f=>f.points.every(p=>Math.abs(height(p)-frontier)<1e-6)));
  return faces.sort((a,b)=>Number(outer.has(a))-Number(outer.has(b)) || b.depth-a.depth);
}

// Curves are tessellated only for the screen. The 2D profile retains an exact SVG radius.
export function profileFaces(piece: MeasuredPiece): number[][][] | null {
  const p = piece.profile;
  if (!p) return null;
  const [u, v] = p.axes.split("").map(a => "xyz".indexOf(a));
  const normal = [0, 1, 2].find(a => a !== u && a !== v)!;
  const points = p.kind === "convex_prism" ? p.points_um : (() => {
    const w = piece.size_um[u!]!, h = piece.size_um[v!]!, r = p.radius_um;
    return [[w-r,r,-90], [w-r,h-r,0], [r,h-r,90], [r,r,180]].flatMap(([x,y,start]) =>
      Array.from({length: 12}, (_, i) => {
        const angle = (start! + i * 7.5) * Math.PI / 180;
        return [x! + r * Math.cos(angle), y! + r * Math.sin(angle)];
      }));
  })();
  const sides = [0, piece.size_um[normal]!].map(depth => points.map(([x,y]) => {
    const point = [0,0,0];
    point[u!] = x!; point[v!] = y!; point[normal] = depth;
    return point;
  }));
  return [sides[0]!, sides[1]!, ...points.map((_, i) => {
    const j = (i+1) % points.length;
    return [sides[0]![i]!, sides[1]![i]!, sides[1]![j]!, sides[0]![j]!];
  })];
}

function hull(points: number[][]): number[][] {
  const sorted = [...new Map(points.map(p => [p.join(","),p])).values()]
    .sort((a,b) => a[0]!-b[0]! || a[1]!-b[1]!);
  const halves: number[][] = [];
  for (const sequence of [sorted,[...sorted].reverse()]) {
    const edge: number[][]=[];
    for (const p of sequence) {
      while(edge.length>=2) {
        const a=edge[edge.length-2]!, b=edge[edge.length-1]!;
        if((b[0]!-a[0]!)*(p[1]!-a[1]!)-(b[1]!-a[1]!)*(p[0]!-a[0]!)>0) break;
        edge.pop();
      }
      edge.push(p);
    }
    halves.push(...edge.slice(0,-1));
  }
  return halves;
}

export function profileSvgOutline(piece: MeasuredPiece, axes: string): string | null {
  const p=piece.profile;
  if(!p) return null;
  if(p.kind === "rounded_rectangle" && p.axes === axes) {
    const [u,v]=axes.split("").map(a=>"xyz".indexOf(a));
    return `<rect data-profile="rounded_rectangle" width="${piece.size_um[u!]!/1000}" height="${piece.size_um[v!]!/1000}" rx="${p.radius_um/1000}" fill="#fffefa" stroke="#473e31" stroke-width="1.2" vector-effect="non-scaling-stroke"/>`;
  }
  const [u,v]=axes.split("").map(a=>"xyz".indexOf(a));
  const points=hull(profileFaces(piece)!.flat().map(p=>[p[u!]!,p[v!]!]))
    .map(p=>p.map(n=>n/1000).join(",")).join(" ");
  return `<polygon data-profile="${p.kind}" points="${points}" fill="#fffefa" stroke="#473e31" stroke-width="1.2" vector-effect="non-scaling-stroke"/>`;
}

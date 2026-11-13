"use client";

import { useState } from "react";
import type { components } from "@/lib/api-schema";

type Construction = components["schemas"]["SideboardConstruction"];
type Path = components["schemas"]["HingePath"];

export function trackPosition(path: Path, angle: number) {
  const end = path.poses.findIndex(p => p.angle_deg >= angle);
  const b = path.poses[end]!;
  const a = path.poses[Math.max(0, end - 1)]!;
  const u = a === b ? 0 : (angle - a.angle_deg) / (b.angle_deg - a.angle_deg);
  return { dx: a.dx_mm + u * (b.dx_mm - a.dx_mm), dy: a.dy_mm + u * (b.dy_mm - a.dy_mm) };
}

export function SideboardTravel({ construction }: { construction: Construction }) {
  const [selected, setSelected] = useState("");
  const [angle, setAngle] = useState(0);
  const paths = construction.request.review?.hinge_paths ?? [];
  const available = (construction.assessment?.door_motion ?? []).filter(m => paths.some(p => p.article === m.article));
  const active = available.find(m => m.door_id === selected) ?? available[0];
  if (!active) return null;
  const parts = construction.assembly.parts;
  const door = parts.find(p => p.id === active.door_id)!;
  const path = paths.find(p => p.article === active.article)!;
  const [x,y] = door.origin_um!.map(v => v/1000) as [number,number,number];
  const {dx,dy} = trackPosition(path, angle);
  const width = construction.request.width_mm, depth = construction.request.depth_mm;
  // Include all allowed track translations and door reaches so the view stays fixed while scrubbing.
  const reach = Math.max(...parts.filter(p=>p.id.startsWith("DOOR-")).map(p=>p.size_um[0]/1000)) + 125;
  const visibleParts = parts.filter(p => p.id !== door.id &&
    /^(SIDE-|DIV-|DOOR-|BACK-)/.test(p.id));
  return <details><summary>Visa luckrörelsen ovanifrån</summary>
    <label>Lucka <select value={active.door_id} onChange={e => {setSelected(e.target.value);setAngle(0);}}>
      {available.map(m => <option key={m.door_id}>{m.door_id}</option>)}
    </select></label>
    <label>Öppningsvinkel {angle}° <input type="range" min={0} max={110} step={1} value={angle}
      onChange={e => setAngle(Number(e.target.value))}/></label>
    <svg role="img" aria-label={`${door.id} vid ${angle} grader enligt ${path.source}`}
      width="100%" viewBox={`${-reach} ${-depth-25} ${width+reach+180} ${depth+reach+50}`}>
      <g transform="scale(1,-1)" stroke="#35473c" strokeWidth="2">
        {visibleParts.map(p => <rect key={p.id} x={p.origin_um![0]/1000} y={p.origin_um![1]/1000}
          width={p.size_um[0]/1000} height={p.size_um[1]/1000} fill={p.id.startsWith("DOOR")?"#b98d6c":"#d9d6c8"}/>)}
        <rect width={door.size_um[0]/1000} height={door.size_um[1]/1000} fill="#477d68"
          transform={`translate(${x+dx} ${y+dy}) rotate(${-angle})`}/>
      </g>
    </svg>
    <p>{path.article} · {path.source}. En lucka rör sig, övriga är stängda.</p>
  </details>;
}
